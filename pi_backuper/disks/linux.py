"""Linux side: finding removable disks through sysfs, unmounting, raw access.

Everything comes from /sys and /proc rather than from lsblk or udisks, so it
behaves the same on a desktop, a server or a Raspberry Pi with nothing extra
installed.
"""

import fcntl
import os
import re
import shutil
import struct
import subprocess
from contextlib import contextmanager

from .common import Disk, PosixDisk, inhibit_sleep, is_root, run

SYS = "/sys"
DEV = "/dev"
MOUNTINFO = "/proc/self/mountinfo"
SWAPS = "/proc/swaps"

BLKRRPART = 0x125F
BLKFLSBUF = 0x1261
BLKSSZGET = 0x1268
BLKGETSIZE64 = 0x80081272

# Never offered, wherever they live: the running system is on these.
SYSTEM_MOUNTS = {"/", "/boot", "/boot/firmware", "/boot/efi", "/efi", "/usr", "/var", "/home",
                 "[SWAP]"}
SKIP_PREFIXES = ("loop", "ram", "zram", "dm-", "md", "sr", "nbd", "fd", "zd")

is_admin = is_root


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _unescape(s):
    # mountinfo writes spaces and friends as \040 etc.
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _mounts():
    """[(block device name, mount point)] for everything mounted, swap included."""
    out = []
    try:
        with open(MOUNTINFO, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    for line in lines:
        left, _, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if len(fields) < 5 or len(tail) < 2:
            continue
        name = _device_name(_unescape(tail[1]), fields[2])
        if name:
            out.append((name, _unescape(fields[4])))
    for line in _read(SWAPS).splitlines()[1:]:
        parts = line.split()
        if parts and parts[0].startswith("/dev/"):
            out.append((os.path.basename(os.path.realpath(parts[0])), "[SWAP]"))
    return out


def _device_name(source, majmin):
    """Kernel name ("sdb1", "dm-0") of a mount's source, or None if it isn't a block device."""
    # major:minor is what the kernel actually mounted, so trust it first. The
    # source name can mislead: "/dev/root" may not exist, or point somewhere odd.
    link = f"{SYS}/dev/block/{majmin}"
    if os.path.exists(link):
        return os.path.basename(os.path.realpath(link))
    # btrfs and friends report a made-up major:minor; fall back to the source.
    if source.startswith("/dev/") and os.path.exists(source):
        return os.path.basename(os.path.realpath(source))
    return None


def _whole_disks(name, depth=0):
    """The physical disks a block device lives on: sdb1 -> {sdb}, dm-0 -> {sda, sdb}."""
    node = f"{SYS}/class/block/{name}"
    if depth > 8 or not os.path.exists(node):
        return set()
    slaves = f"{node}/slaves"
    if os.path.isdir(slaves) and os.listdir(slaves):
        found = set()
        for s in os.listdir(slaves):
            found |= _whole_disks(s, depth + 1)
        return found
    if os.path.exists(f"{node}/partition"):
        return {os.path.basename(os.path.dirname(os.path.realpath(node)))}
    return {name}


def _mounts_by_disk():
    by_disk = {}
    for name, mountpoint in _mounts():
        for disk in _whole_disks(name):
            by_disk.setdefault(disk, set()).add(mountpoint)
    return by_disk


def _serial(base, usb):
    serial = _read(f"{base}/device/serial")  # SD cards in a built-in slot have their own
    if serial or not usb:
        return serial
    # USB sticks: the serial sits on the USB device a few levels up.
    path = os.path.realpath(f"{base}/device")
    for _ in range(8):
        path = os.path.dirname(path)
        if os.path.basename(path) in ("", "devices"):
            break
        serial = _read(f"{path}/serial")
        if serial:
            return serial
    return ""


def list_disks():
    """USB disks and SD cards, never one the running system uses."""
    by_disk = _mounts_by_disk()
    disks = []
    try:
        names = sorted(os.listdir(f"{SYS}/block"))
    except OSError:
        return []
    for name in names:
        if name.startswith(SKIP_PREFIXES) or "boot" in name or "rpmb" in name:
            continue
        base = f"{SYS}/block/{name}"
        size = int(_read(f"{base}/size") or 0) * 512  # always 512-byte units here
        if not size:
            continue  # card reader with no card in it
        usb = "/usb" in os.path.realpath(base)
        sd = name.startswith("mmcblk")
        if not (usb or sd or _read(f"{base}/removable") == "1"):
            continue
        mounts = by_disk.get(name, set())
        if mounts & SYSTEM_MOUNTS:
            continue
        model = " ".join(filter(None, (_read(f"{base}/device/vendor"),
                                       _read(f"{base}/device/model") or _read(f"{base}/device/name"))))
        disks.append(Disk(
            id=name,
            name=f"/dev/{name}",
            path=f"{DEV}/{name}",
            size=size,
            model=model or ("SD card" if sd else "USB disk"),
            serial=_serial(base, usb),
            hw_id=os.path.realpath(f"{base}/device"),
            mounts=tuple(sorted(mounts)),
        ))
    return disks


def disks_holding(path):
    """Names of the physical disks the file or folder `path` is stored on."""
    path = os.path.realpath(path)
    best_name, best_len = None, -1
    for name, mountpoint in _mounts():
        if mountpoint == "[SWAP]":
            continue
        inside = mountpoint == "/" or path == mountpoint or path.startswith(mountpoint + "/")
        if inside and len(mountpoint) > best_len:
            best_name, best_len = name, len(mountpoint)
    return _whole_disks(best_name) if best_name else set()


def _holders(name):
    """Device-mapper / RAID devices sitting on the disk or its partitions."""
    found = []
    base = f"{SYS}/block/{name}"
    for d in [base] + [f"{base}/{p}" for p in os.listdir(base) if p.startswith(name)]:
        try:
            found += os.listdir(f"{d}/holders")
        except OSError:
            pass
    return found


@contextmanager
def claim_source(disk):
    """Nothing to lock on Linux. Yields the mount points the user should leave alone."""
    yield sorted(_mounts_by_disk().get(disk.id, set()))


@contextmanager
def claim_target(disk):
    """Unmount everything on the disk. Opening it with O_EXCL keeps it that way."""
    mounts = _mounts_by_disk().get(disk.id, set())
    if "[SWAP]" in mounts:
        raise RuntimeError(f"{disk.name} is being used as swap. Run 'swapoff' on it first.")
    # Deepest first, so /media/x/inner goes before /media/x.
    for mountpoint in sorted(mounts, key=len, reverse=True):
        try:
            run(["umount", mountpoint])
        except RuntimeError as e:
            raise RuntimeError(f"Couldn't unmount {mountpoint}: {e}\n\nClose any file manager "
                               "windows or programs using it and try again.") from None
    holders = _holders(disk.id)
    if holders:
        raise RuntimeError(f"{disk.name} is in use by {', '.join(holders)} (LVM, RAID or "
                           "encryption). Close that first.")
    yield


class LinuxDisk(PosixDisk):
    # O_EXCL on a block device fails if anything has it mounted, and stops it
    # being mounted while we hold it. O_DSYNC makes each write land on the card
    # before the next, so the progress bar shows real progress instead of the
    # page cache filling up and then a long hang at the end.
    extra_flags = os.O_EXCL | os.O_DSYNC

    def _probe(self):
        try:
            size = struct.unpack("Q", fcntl.ioctl(self._fd, BLKGETSIZE64, bytes(8)))[0]
            sector = struct.unpack("i", fcntl.ioctl(self._fd, BLKSSZGET, bytes(4)))[0]
            return size, sector or 512
        except OSError:
            return super()._probe()

    def read(self, offset, length):
        data = super().read(offset, length)
        # Don't push the user's own files out of the page cache with a 64 GB card.
        os.posix_fadvise(self._fd, offset, length, os.POSIX_FADV_DONTNEED)
        return data

    def _after_flush(self):
        # Drop cached sectors so the verify pass reads what's really on the card.
        try:
            fcntl.ioctl(self._fd, BLKFLSBUF)
        except OSError:
            pass
        os.posix_fadvise(self._fd, 0, 0, os.POSIX_FADV_DONTNEED)

    def _before_close(self):
        if self.writable:
            try:
                fcntl.ioctl(self._fd, BLKRRPART)  # pick up the new partition table
            except OSError:
                pass


def open_disk(disk, writable=False):
    return LinuxDisk(disk.path, writable)


def keep_awake():
    return inhibit_sleep(["systemd-inhibit", "--what=sleep:idle", "--who=PI-Backuper",
                          "--why=Disk backup or restore running", "--mode=block",
                          "sleep", "infinity"])


# Passed through pkexec so the root copy of the app can open a window.
DISPLAY_VARS = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "LANG")


def relaunch_as_admin(argv):
    """Run the app again as root through pkexec and wait for it to finish."""
    if not shutil.which("pkexec"):
        raise RuntimeError(f"Start it as root instead:\n\nsudo {' '.join(argv)}")
    passed = {k: os.environ[k] for k in DISPLAY_VARS if os.environ.get(k)}
    if "XAUTHORITY" not in passed and os.path.exists(os.path.expanduser("~/.Xauthority")):
        passed["XAUTHORITY"] = os.path.expanduser("~/.Xauthority")
    cmd = ["pkexec", "env"] + [f"{k}={v}" for k, v in passed.items()] + argv
    code = subprocess.call(cmd)
    if code in (126, 127):  # dialog dismissed / not authorised
        raise RuntimeError("Didn't get root access, so PI-Backuper can't open disks.")
