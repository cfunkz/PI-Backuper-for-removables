import gzip
import hashlib
import os
import threading

import pytest

from pi_backuper import imaging
from pi_backuper.imaging import CHUNK, Cancelled, Job, VerifyError


class FakeDisk:
    """In-memory disk that behaves like a raw Windows disk: whole sectors only."""

    def __init__(self, size, sector=512, data=None):
        assert size % sector == 0
        self.size = size
        self.sector = sector
        self.buf = bytearray(data if data is not None else os.urandom(size))
        self.writes = []
        self.flushed = False

    def _check(self, offset, length):
        assert offset % self.sector == 0, f"unaligned offset {offset}"
        assert length % self.sector == 0, f"unaligned length {length}"
        assert offset + length <= self.size, "past end of disk"

    def read(self, offset, length):
        self._check(offset, length)
        return bytes(self.buf[offset:offset + length])

    def write(self, offset, data):
        self._check(offset, len(data))
        self.buf[offset:offset + len(data)] = data
        self.writes.append(offset)

    def flush(self):
        self.flushed = True


class LossyDisk(FakeDisk):
    """A fake card: accepts writes but silently drops some of them."""

    def write(self, offset, data):
        if offset == CHUNK * 2:
            return
        super().write(offset, data)


class BrokenDisk(FakeDisk):
    def read(self, offset, length):
        if offset >= CHUNK * 2:
            raise OSError("I/O error")
        return super().read(offset, length)


SIZE = CHUNK * 5 + 512 * 7  # deliberately not a whole number of chunks


def no_reader_threads():
    return not any(t.name == "reader" for t in threading.enumerate())


@pytest.mark.parametrize("compress", [True, False])
def test_backup_then_restore_round_trip(tmp_path, compress):
    src = FakeDisk(SIZE)
    out = str(tmp_path / ("card.img.gz" if compress else "card.img"))

    digest = imaging.backup(src, out, Job(), compress=compress)

    assert digest == hashlib.sha256(src.buf).hexdigest()
    assert imaging.is_gzip(out) == compress
    assert imaging.read_sidecar(out) == ("sha256", SIZE, digest)
    assert not os.path.exists(out + ".partial")

    dst = FakeDisk(SIZE * 2)
    imaging.restore(out, dst, Job())
    assert dst.buf[:SIZE] == src.buf
    assert dst.writes[-1] == 0, "boot sector must be written last"


def test_restore_unaligned_gzip_keeps_rest_of_sector(tmp_path):
    payload = os.urandom(CHUNK + 1000)
    img = tmp_path / "odd.img.gz"
    img.write_bytes(gzip.compress(payload))

    dst = FakeDisk(CHUNK * 2)
    before = bytes(dst.buf)
    imaging.restore(str(img), dst, Job())

    assert dst.buf[:len(payload)] == payload
    assert dst.buf[len(payload):] == before[len(payload):]


def test_restore_reads_old_md5_sidecar(tmp_path):
    payload = os.urandom(CHUNK * 2)
    img = tmp_path / "old.img"
    img.write_bytes(payload)
    (tmp_path / "old.img.hash.txt").write_text(f"md5 {len(payload)} {hashlib.md5(payload).hexdigest()}\n")

    dst = FakeDisk(CHUNK * 2)
    imaging.restore(str(img), dst, Job())
    assert dst.buf == payload


def test_restore_rejects_damaged_image_and_leaves_disk_blank(tmp_path):
    payload = os.urandom(CHUNK * 3)
    img = tmp_path / "bad.img"
    img.write_bytes(payload)
    (tmp_path / "bad.img.hash.txt").write_text(f"sha256 {len(payload)} {'0' * 64}\n")

    dst = FakeDisk(CHUNK * 4)
    with pytest.raises(VerifyError):
        imaging.restore(str(img), dst, Job())
    assert dst.buf[:CHUNK] == bytes(CHUNK), "partition table should be wiped, not half-restored"


def test_restore_refuses_image_bigger_than_device_before_writing(tmp_path):
    img = tmp_path / "big.img"
    img.write_bytes(os.urandom(CHUNK * 3))
    dst = FakeDisk(CHUNK * 2)
    with pytest.raises(ValueError):
        imaging.restore(str(img), dst, Job())
    assert dst.writes == []


def test_restore_stops_gzip_without_sidecar_at_end_of_device(tmp_path):
    img = tmp_path / "big.img.gz"
    img.write_bytes(gzip.compress(os.urandom(CHUNK * 3)))
    dst = FakeDisk(CHUNK * 2)
    with pytest.raises(ValueError):
        imaging.restore(str(img), dst, Job())
    assert no_reader_threads()


def test_verify_catches_a_card_that_drops_writes(tmp_path):
    img = tmp_path / "x.img"
    img.write_bytes(os.urandom(CHUNK * 4))
    with pytest.raises(VerifyError):
        imaging.restore(str(img), LossyDisk(CHUNK * 4), Job())


def test_clone_copies_everything_and_verifies():
    src = FakeDisk(SIZE)
    # 4K-sector target with a 512-byte-sector source whose size isn't a multiple of 4K.
    dst = FakeDisk(imaging.round_up(SIZE, 4096) + CHUNK, sector=4096)
    imaging.clone(src, dst, Job())
    assert dst.buf[:SIZE] == src.buf
    assert dst.writes[-1] == 0


def test_clone_refuses_smaller_target():
    with pytest.raises(ValueError):
        imaging.clone(FakeDisk(CHUNK * 2), FakeDisk(CHUNK), Job())


def test_cancelled_backup_leaves_no_files(tmp_path):
    seen = []

    def on_progress(phase, done, fraction):
        seen.append(done)
        job.cancel()

    job = Job(on_progress)
    out = str(tmp_path / "c.img")
    with pytest.raises(Cancelled):
        imaging.backup(FakeDisk(CHUNK * 20), out, job, compress=False)
    assert os.listdir(tmp_path) == []
    assert no_reader_threads()


def test_read_error_is_reported_and_partial_removed(tmp_path):
    out = str(tmp_path / "e.img.gz")
    with pytest.raises(OSError, match="I/O error"):
        imaging.backup(BrokenDisk(CHUNK * 4), out, Job())
    assert os.listdir(tmp_path) == []
    assert no_reader_threads()


def test_write_error_stops_the_reader():
    def read_next():
        return b"x" * 512

    def write(_):
        raise OSError("device gone")

    with pytest.raises(OSError, match="device gone"):
        imaging.pump(read_next, write, Job(), "test", lambda d: 0)
    assert no_reader_threads()


def test_new_backup_replaces_old_sidecar(tmp_path):
    out = str(tmp_path / "card.img")
    first = imaging.backup(FakeDisk(CHUNK), out, Job(), compress=False)
    second = imaging.backup(FakeDisk(CHUNK), out, Job(), compress=False)
    assert first != second
    assert imaging.read_sidecar(out)[2] == second


def test_truncated_gzip_is_reported_and_disk_left_blank(tmp_path):
    img = tmp_path / "cut.img.gz"
    whole = gzip.compress(os.urandom(CHUNK * 3))
    img.write_bytes(whole[:-1000])  # download that stopped early

    dst = FakeDisk(CHUNK * 4)
    with pytest.raises(VerifyError, match="damaged or incomplete"):
        imaging.restore(str(img), dst, Job())
    assert dst.buf[:CHUNK] == bytes(CHUNK)
    assert no_reader_threads()


@pytest.mark.parametrize("line", [
    "shake_128 100 abcd",            # would pass hashlib.new() but has no plain hexdigest
    "sha256 100 nothex",
    "sha256 100 " + "0" * 10,        # wrong length for sha256
    "sha256 -5 " + "0" * 64,
    "",
])
def test_bad_sidecars_are_ignored(tmp_path, line):
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 512)
    (tmp_path / "x.img.hash.txt").write_text(line + "\n")
    assert imaging.read_sidecar(str(img)) is None


def test_full_fat32_drive_gets_a_plain_message(tmp_path, monkeypatch):
    import errno

    def too_big(self, data):
        raise OSError(errno.EFBIG, "File too large")

    monkeypatch.setattr(imaging.gzip.GzipFile, "write", too_big)
    out = str(tmp_path / "card.img.gz")
    with pytest.raises(OSError, match="over 4 GB"):
        imaging.backup(FakeDisk(SIZE), out, Job())
    assert not os.path.exists(out + ".partial")
    assert no_reader_threads()
