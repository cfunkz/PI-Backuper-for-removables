"""Backup, restore, clone and verify for raw disk images.

Nothing in here knows about Windows. A "device" is anything with a `size`,
a `sector` size, `read(offset, length)`, `write(offset, data)` and `flush()`,
so the same code runs against a real SD card or a plain file in the tests.

Two rules keep a failed or cancelled job from leaving a card that looks fine
but isn't:

* The first block of the target (partition table, boot sector) is wiped
  before anything else and the real one is written last. A restore that
  stops halfway leaves a card with no partitions, not a half-written one
  that boots and then falls over.
* Backups go to "<name>.partial" and only get their real name once the
  whole file has been written, flushed and (optionally) read back.
"""

import errno
import gzip
import hashlib
import os
import queue
import threading
import time
import zlib

CHUNK = 4 * 1024 * 1024  # multiple of every sector size we'll meet (512, 4096)
QUEUE_DEPTH = 8          # chunks buffered between reader and writer (32 MiB)
GZIP_MAGIC = b"\x1f\x8b"
# Only plain digests. A sidecar naming something like shake_128 would pass
# hashlib.new() and then blow up at the very end of a restore.
HASHES = ("md5", "sha1", "sha256", "sha512")
# What a truncated or corrupt .gz throws while it's being read.
GZIP_ERRORS = (EOFError, gzip.BadGzipFile, zlib.error)


class Cancelled(Exception):
    pass


class VerifyError(Exception):
    pass


def damaged_image():
    return VerifyError("The image file is damaged or incomplete (the gzip data is cut "
                       "short or corrupt).")


def output_error(e):
    """Explain a failed write to the backup file in plain words."""
    if e.errno == errno.EFBIG or getattr(e, "winerror", None) == 223:
        return OSError(e.errno, "The drive you're saving to can't hold files over 4 GB "
                                "(it's probably FAT32). Save to an exFAT, NTFS, APFS or "
                                "ext4 drive instead.")
    if e.errno == errno.ENOSPC or getattr(e, "winerror", None) == 112:
        return OSError(e.errno, "The drive you're saving to ran out of space.")
    return e


def drop_cache(f):
    """Forget a file's cached pages so the next read really comes from the disk."""
    if hasattr(os, "posix_fadvise"):
        try:
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass


class Job:
    """Cancel flag plus a throttled progress callback, shared with the GUI.

    on_progress(phase, bytes_done, fraction) is called from the worker
    thread at most a few times a second.
    """

    def __init__(self, on_progress=None):
        self._cancel = threading.Event()
        self._on_progress = on_progress
        self._last = 0.0
        self.phase = None  # last phase reported, so a cancel can say how far it got

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def check(self):
        if self._cancel.is_set():
            raise Cancelled()

    def progress(self, phase, done, fraction, force=False):
        self.phase = phase
        if not self._on_progress:
            return
        now = time.monotonic()
        if force or now - self._last >= 0.25:
            self._last = now
            self._on_progress(phase, done, min(max(fraction, 0.0), 1.0))


# ---------- hash sidecar ("image.img.gz.hash.txt") ----------
#
# One line: "<algo> <uncompressed bytes> <hex digest>". Same format older versions used,
# so old backups (including md5 ones) still verify.

def sidecar_path(image_path):
    return image_path + ".hash.txt"


def read_sidecar(image_path):
    try:
        with open(sidecar_path(image_path), encoding="utf-8") as f:
            algo, nbytes, digest = f.readline(512).split()
        algo, nbytes, digest = algo.lower(), int(nbytes), digest.lower()
        if algo not in HASHES or nbytes < 0 or len(digest) != hashlib.new(algo).digest_size * 2:
            return None
        int(digest, 16)
        return algo, nbytes, digest
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def write_sidecar(image_path, algo, nbytes, digest):
    path = sidecar_path(image_path)
    tmp = path + ".partial"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"{algo} {nbytes} {digest}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------- image file helpers ----------

def is_gzip(path):
    with open(path, "rb") as f:
        return f.read(2) == GZIP_MAGIC


def image_size(path):
    """Uncompressed size of an image, or None if we can't know it up front.

    Plain images: the file size. Gzip images: taken from the hash sidecar.
    (The size field in the gzip trailer wraps at 4 GiB, so it's useless here.)
    """
    if not is_gzip(path):
        return os.path.getsize(path)
    meta = read_sidecar(path)
    return meta[1] if meta else None


def read_full(f, n):
    """Read exactly n bytes unless the stream ends first."""
    data = f.read(n)
    if len(data) == n or not data:
        return data
    parts = [data]
    got = len(data)
    while got < n:
        more = f.read(n - got)
        if not more:
            break
        parts.append(more)
        got += len(more)
    return b"".join(parts)


# ---------- the copy loop ----------

_END = object()


def pump(read_next, write, job, phase, fraction):
    """Copy chunks from read_next() to write() until read_next() returns b"".

    Reading happens on a helper thread so the source and the target are busy
    at the same time. hashlib and zlib release the GIL on big buffers, so
    hashing on the reader side and gzip on the writer side overlap too.

    fraction(bytes_done) gives the progress bar value. Returns bytes copied.
    Any error on either side stops both sides and is re-raised here.
    """
    q = queue.Queue(QUEUE_DEPTH)
    stop = threading.Event()
    failure = []

    def put(item):
        while not stop.is_set():
            try:
                q.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def reader():
        try:
            while not stop.is_set():
                data = read_next()
                if not data:
                    break
                put(data)
        except BaseException as e:
            failure.append(e)
        finally:
            put(_END)

    t = threading.Thread(target=reader, name="reader", daemon=True)
    t.start()
    done = 0
    try:
        while True:
            job.check()
            try:
                data = q.get(timeout=0.1)
            except queue.Empty:
                continue
            if data is _END:
                break
            write(data)
            done += len(data)
            job.progress(phase, done, fraction(done))
    finally:
        stop.set()
        # Wait for the reader so nobody closes a handle it's still using.
        t.join()

    if failure:
        raise failure[0]
    job.progress(phase, done, fraction(done), force=True)
    return done


# ---------- device helpers ----------

def round_up(n, sector):
    return -(-n // sector) * sector


def write_any(dev, offset, data):
    """Write data that may end partway through a sector.

    Raw disks only take whole sectors, and a gzip image doesn't have to be a
    multiple of 512. The last partial sector is merged with what's already on
    the device, so bytes past the end of the image are left alone.
    """
    whole = len(data) - len(data) % dev.sector
    if whole:
        dev.write(offset, data[:whole] if whole < len(data) else data)
    if whole < len(data):
        tail = data[whole:]
        old = dev.read(offset + whole, dev.sector)
        dev.write(offset + whole, tail + old[len(tail):])


def device_digest(dev, length, algo, job, phase="Verifying"):
    """Hash the first `length` bytes of a device."""
    h = hashlib.new(algo)
    pos = 0
    while pos < length:
        job.check()
        n = min(CHUNK, length - pos)
        data = dev.read(pos, round_up(n, dev.sector))[:n]
        h.update(data)
        pos += n
        job.progress(phase, pos, pos / length)
    job.progress(phase, pos, 1.0, force=True)
    return h.hexdigest()


def file_digest(path, algo, job, phase="Verifying"):
    """Hash the uncompressed contents of an image file. Returns (digest, bytes)."""
    h = hashlib.new(algo)
    total = os.path.getsize(path) or 1
    count = 0
    with open(path, "rb") as raw:
        drop_cache(raw)
        src = gzip.GzipFile(fileobj=raw, mode="rb") if is_gzip(path) else raw
        try:
            while True:
                job.check()
                data = src.read(CHUNK)
                if not data:
                    break
                h.update(data)
                count += len(data)
                job.progress(phase, count, raw.tell() / total)
        except GZIP_ERRORS:
            raise damaged_image() from None
    job.progress(phase, count, 1.0, force=True)
    return h.hexdigest(), count


def _device_reader(dev, size, start, hasher):
    pos = start

    def read_next():
        nonlocal pos
        if pos >= size:
            return b""
        data = dev.read(pos, min(CHUNK, size - pos))
        pos += len(data)
        hasher.update(data)
        return data

    return read_next


# ---------- jobs ----------

def backup(dev, out_path, job, compress=True, verify=True):
    """Image the whole device to out_path. Returns the sha256 hex digest."""
    partial = out_path + ".partial"
    size = dev.size
    hasher = hashlib.sha256()

    try:
        try:
            with open(partial, "wb") as raw:
                gz = None
                if compress:
                    # Level 1: SD cards are mostly empty space, which compresses
                    # just as well at level 1, and anything higher is CPU-bound.
                    gz = gzip.GzipFile(filename=os.path.basename(out_path), mode="wb",
                                       fileobj=raw, compresslevel=1, mtime=0)
                try:
                    pump(_device_reader(dev, size, 0, hasher), (gz or raw).write, job,
                         "Reading", lambda done: done / size)
                finally:
                    if gz:
                        gz.close()
                raw.flush()
                os.fsync(raw.fileno())
        except OSError as e:
            raise output_error(e) from None

        digest = hasher.hexdigest()
        if verify:
            got, count = file_digest(partial, "sha256", job)
            if count != size or got != digest:
                raise VerifyError("The image file doesn't match what was read from the "
                                  "device. The disk you saved to may be failing.")

        # Drop any old sidecar first so it can never sit next to a new image.
        try:
            os.remove(sidecar_path(out_path))
        except FileNotFoundError:
            pass
        os.replace(partial, out_path)
        write_sidecar(out_path, "sha256", size, digest)
        return digest
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise


def _restore_body(raw, dev, job, hasher, file_size, gz):
    """Blank the first chunk, then stream the rest of the image in behind it.

    Returns (first chunk, bytes written). The caller writes the first chunk
    last, once it knows the whole image went down cleanly.
    """
    src = gzip.GzipFile(fileobj=raw, mode="rb") if gz else raw
    first = read_full(src, CHUNK)
    if not first:
        raise ValueError("The image file is empty.")
    if len(first) > dev.size:
        raise ValueError("The image is bigger than the device.")
    hasher.update(first)

    job.check()
    write_any(dev, 0, bytes(len(first)))
    written = len(first)

    def read_next():
        data = read_full(src, CHUNK)
        hasher.update(data)
        return data

    def write(data):
        nonlocal written
        if written + len(data) > dev.size:
            raise ValueError("The image is bigger than the device.")
        write_any(dev, written, data)
        written += len(data)

    pump(read_next, write, job, "Writing", lambda _: raw.tell() / file_size)
    return first, written


def restore(image_path, dev, job, verify=True):
    """Write an image (plain or gzip) to the device, starting at sector 0."""
    meta = read_sidecar(image_path)
    algo = meta[0] if meta else "sha256"
    expected = image_size(image_path)
    if expected is not None and expected > dev.size:
        raise ValueError(f"The image ({expected:,} bytes) is bigger than the "
                         f"device ({dev.size:,} bytes).")

    file_size = os.path.getsize(image_path) or 1
    hasher = hashlib.new(algo)

    with open(image_path, "rb") as raw:
        try:
            first, written = _restore_body(raw, dev, job, hasher, file_size, is_gzip(image_path))
        except GZIP_ERRORS:
            # The gzip check failed before sector 0 was written, so the device is blank.
            raise damaged_image() from None

    digest = hasher.hexdigest()
    if meta and (written != meta[1] or digest != meta[2]):
        dev.flush()
        raise VerifyError("The image doesn't match its .hash.txt file, so it's damaged. "
                          "The device was left blank instead of half-restored.")

    job.check()
    write_any(dev, 0, first)
    dev.flush()

    if verify and device_digest(dev, written, algo, job) != digest:
        raise VerifyError("Read-back check failed: the device didn't keep what was "
                          "written. The card may be worn out or fake.")
    return digest


def clone(src, dst, job, verify=True):
    """Copy src onto dst sector for sector."""
    size = src.size
    if dst.size < size:
        raise ValueError(f"Target ({dst.size:,} bytes) is smaller than source ({size:,} bytes).")

    hasher = hashlib.sha256()
    first = src.read(0, min(CHUNK, size))
    hasher.update(first)

    job.check()
    write_any(dst, 0, bytes(len(first)))
    offset = len(first)

    def write(data):
        nonlocal offset
        write_any(dst, offset, data)
        offset += len(data)

    pump(_device_reader(src, size, len(first), hasher), write, job,
         "Cloning", lambda done: (done + len(first)) / size)

    job.check()
    write_any(dst, 0, first)
    dst.flush()

    digest = hasher.hexdigest()
    if verify and device_digest(dst, size, "sha256", job) != digest:
        raise VerifyError("Read-back check failed: the target didn't keep what was "
                          "written. The card may be worn out or fake.")
    return digest
