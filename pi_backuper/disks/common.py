"""Bits shared by the per-OS disk code."""

import errno
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass

from ..fmt import fmt_bytes


@dataclass(frozen=True)
class Disk:
    """One removable disk, as it looked when the list was refreshed.

    id      what the OS calls it: 2 (PhysicalDrive2), "sdb", "disk4"
    name    how it's shown: "Disk 2", "/dev/sdb", "disk4"
    path    the raw device that gets opened
    hw_id   where it's plugged in. Together with serial and size this is
            how a card swapped behind our back gets noticed.
    mounts  drive letters or mount points of its volumes
    """
    id: object
    name: str
    path: str
    size: int
    model: str
    serial: str
    hw_id: str
    mounts: tuple

    @property
    def label(self):
        mounts = " ".join(self.mounts) or "not mounted"
        serial = f" #{self.serial}" if self.serial else ""
        return f"{self.name}: {self.model}{serial}  |  {fmt_bytes(self.size)}  |  {mounts}"

    def same_hardware(self, other):
        return (self.id, self.hw_id, self.serial, self.size) == \
               (other.id, other.hw_id, other.serial, other.size)


# errno values worth explaining in plain words.
FRIENDLY_ERRORS = {
    errno.EACCES: "Permission denied. PI-Backuper has to run as root (sudo) for raw disk access.",
    errno.EPERM: "Permission denied. PI-Backuper has to run as root (sudo) for raw disk access.",
    errno.EBUSY: "The device is busy. Something still has it mounted or open.",
    errno.EROFS: "The device is write-protected. Check the lock switch on the SD card or adapter.",
    errno.EIO: "I/O error. The card or reader is failing, or it was unplugged.",
    errno.ENXIO: "The device was disconnected.",
    errno.ENODEV: "The device was disconnected.",
    errno.ENOENT: "The device was disconnected.",
    errno.ENOSPC: "Not enough free space.",
}
if hasattr(errno, "ENOMEDIUM"):  # Linux only
    FRIENDLY_ERRORS[errno.ENOMEDIUM] = "There's no card in the reader."


def explain(e, path):
    msg = FRIENDLY_ERRORS.get(e.errno) or e.strerror or str(e)
    code = errno.errorcode.get(e.errno, e.errno)
    return OSError(e.errno, f"{msg} ({path}, {code})")


class PosixDisk:
    """A block device opened with plain file descriptors (Linux and macOS).

    Subclasses fill in the OS-specific parts: how big the device is, what
    else to do on flush (drop caches) and on close (re-read partitions).
    """

    extra_flags = 0

    def __init__(self, path, writable=False):
        self.path = path
        self.writable = writable
        mode = os.O_RDWR if writable else os.O_RDONLY
        flags = mode | getattr(os, "O_CLOEXEC", 0) | (self.extra_flags if writable else 0)
        try:
            self._fd = os.open(path, flags)
        except OSError as e:
            raise explain(e, path) from None
        try:
            self.size, self.sector = self._probe()
        except OSError as e:
            os.close(self._fd)
            raise explain(e, path) from None
        if self.size <= 0:
            os.close(self._fd)
            raise OSError(f"{path} reports no media. Is the card inserted?")

    def _probe(self):
        """(size, sector size). Plain files work too, which the tests rely on."""
        size = os.lseek(self._fd, 0, os.SEEK_END)
        os.lseek(self._fd, 0, os.SEEK_SET)
        return size, 512

    def _after_flush(self):
        pass

    def _before_close(self):
        pass

    def read(self, offset, length):
        parts = []
        got = 0
        try:
            while got < length:
                data = os.pread(self._fd, length - got, offset + got)
                if not data:
                    raise OSError(f"Short read at byte {offset + got:,}. "
                                  "Was the device unplugged?")
                parts.append(data)
                got += len(data)
        except OSError as e:
            if e.errno is None:
                raise
            raise explain(e, self.path) from None
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def write(self, offset, data):
        view = memoryview(data)
        done = 0
        try:
            while done < len(view):
                n = os.pwrite(self._fd, view[done:], offset + done)
                if n <= 0:
                    raise OSError(f"Short write at byte {offset + done:,}. "
                                  "Was the device unplugged?")
                done += n
        except OSError as e:
            if e.errno is None:
                raise
            raise explain(e, self.path) from None

    def flush(self):
        try:
            os.fsync(self._fd)
        except OSError as e:
            # Raw character devices may not do fsync; their writes are synchronous anyway.
            if e.errno not in (errno.EINVAL, errno.ENOTSUP, errno.ENOTTY):
                raise explain(e, self.path) from None
        self._after_flush()

    def close(self):
        if self._fd is None:
            return
        try:
            self._before_close()
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def is_root():
    return os.geteuid() == 0


@contextmanager
def inhibit_sleep(cmd):
    """Run a helper like `caffeinate` for the length of a job. Fine if it's missing."""
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        pass
    try:
        yield
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def run(cmd, timeout=30):
    """Run a command, return stdout. Raises RuntimeError with its stderr on failure."""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"{cmd[0]} failed: {e}") from None
    if p.returncode != 0:
        err = (p.stderr or p.stdout).decode(errors="replace").strip()
        raise RuntimeError(err or f"{' '.join(cmd)} exited with code {p.returncode}")
    return p.stdout
