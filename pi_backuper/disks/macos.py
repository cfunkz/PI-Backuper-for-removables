"""macOS side: finding external disks with diskutil, unmounting, raw access.

Disks are opened as /dev/rdiskN, the raw (uncached) node. It's several times
faster than /dev/diskN and only wants sector-aligned I/O, which imaging.py
already does.
"""

import fcntl
import os
import plistlib
import re
import shlex
import struct
import subprocess
from contextlib import contextmanager

from .common import Disk, PosixDisk, inhibit_sleep, is_root, run

DKIOCSYNCHRONIZECACHE = 0x20006416
DKIOCGETBLOCKSIZE = 0x40046418
DKIOCGETBLOCKCOUNT = 0x40086419

is_admin = is_root


def _diskutil(*args):
    return plistlib.loads(run(["diskutil", *args]))


def _info(target):
    try:
        return _diskutil("info", "-plist", target)
    except (RuntimeError, plistlib.InvalidFileException):
        return {}


def _whole(identifier):
    """disk4s2 -> disk4"""
    m = re.match(r"disk\d+", identifier or "")
    return m.group(0) if m else None


def _physical_disks(identifier, depth=0):
    """The physical disks under a disk or volume. APFS volumes live on a
    synthesized container disk, which in turn sits on a partition of a real one."""
    info = _info(identifier)
    stores = info.get("APFSPhysicalStores") or []
    if stores and depth < 4:
        found = set()
        for s in stores:
            store = s.get("APFSPhysicalStore") or s.get("DeviceIdentifier")
            if store:
                found |= _physical_disks(store, depth + 1)
        return found
    whole = info.get("ParentWholeDisk") or _whole(identifier)
    if whole and whole != identifier and depth < 4:
        parent = _info(whole)
        if parent.get("APFSPhysicalStores"):
            return _physical_disks(whole, depth + 1)
    return {whole} if whole else set()


def mounts_by_disk(listing):
    """{physical whole disk: [mount points]} from `diskutil list -plist` output."""
    store_owner = {}  # APFS container disk -> physical disk it lives on
    for entry in listing.get("AllDisksAndPartitions", []):
        for store in entry.get("APFSPhysicalStores", []):
            owner = _whole(store.get("DeviceIdentifier"))
            if owner:
                store_owner[entry.get("DeviceIdentifier")] = owner

    mounts = {}
    for entry in listing.get("AllDisksAndPartitions", []):
        disk = entry.get("DeviceIdentifier")
        disk = store_owner.get(disk, disk)
        found = mounts.setdefault(disk, [])
        for item in [entry] + entry.get("Partitions", []) + entry.get("APFSVolumes", []):
            if item.get("MountPoint"):
                found.append(item["MountPoint"])
    return mounts


def is_candidate(info):
    """External, removable or SD/USB, physical, with media in it."""
    if info.get("VirtualOrPhysical") == "Virtual" or info.get("APFSPhysicalStores"):
        return False
    bus = info.get("BusProtocol", "")
    if bus == "Disk Image" or not (info.get("TotalSize") or info.get("Size")):
        return False
    removable = info.get("RemovableMedia") or info.get("Removable") or info.get("Ejectable")
    return bool(not info.get("Internal", True) or removable or bus in ("USB", "Secure Digital"))


def list_disks():
    listing = _diskutil("list", "-plist")
    mounts = mounts_by_disk(listing)
    system = disks_holding("/")
    disks = []
    for whole in listing.get("WholeDisks", []):
        if whole in system:
            continue
        info = _info(whole)
        if not is_candidate(info):
            continue
        bus = info.get("BusProtocol", "")
        disks.append(Disk(
            id=whole,
            name=whole,
            path=f"/dev/r{whole}",
            size=int(info.get("TotalSize") or info.get("Size")),
            model=(info.get("MediaName") or info.get("IORegistryEntryName") or "USB disk").strip(),
            serial="",
            hw_id=f"{bus}:{info.get('DeviceTreePath') or info.get('IORegistryEntryName', '')}",
            mounts=tuple(sorted(mounts.get(whole, []))),
        ))
    disks.sort(key=lambda d: int(d.id[4:]))
    return disks


def _mount_point(path):
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def disks_holding(path):
    info = _info(_mount_point(path))
    identifier = info.get("DeviceIdentifier")
    return _physical_disks(identifier) if identifier else set()


@contextmanager
def claim_source(disk):
    """Unmount the source so nothing changes mid-backup, and mount it again after.

    If it won't unmount, the backup still runs; the mount points are yielded
    so the user can be told to leave them alone.
    """
    try:
        run(["diskutil", "unmountDisk", f"/dev/{disk.id}"])
    except RuntimeError:
        yield list(disk.mounts)
        return
    try:
        yield []
    finally:
        if disk.mounts:
            try:
                run(["diskutil", "mountDisk", f"/dev/{disk.id}"])
            except RuntimeError:
                pass


@contextmanager
def claim_target(disk):
    try:
        run(["diskutil", "unmountDisk", f"/dev/{disk.id}"])
    except RuntimeError as e:
        raise RuntimeError(f"Couldn't unmount {disk.name}: {e}\n\nClose any Finder windows "
                           "or programs using it and try again.") from None
    yield


class MacDisk(PosixDisk):
    def _probe(self):
        try:
            sector = struct.unpack("I", fcntl.ioctl(self._fd, DKIOCGETBLOCKSIZE, bytes(4)))[0]
            count = struct.unpack("Q", fcntl.ioctl(self._fd, DKIOCGETBLOCKCOUNT, bytes(8)))[0]
            return sector * count, sector or 512
        except OSError:
            return super()._probe()

    def _after_flush(self):
        try:
            fcntl.ioctl(self._fd, DKIOCSYNCHRONIZECACHE)  # flush the card's own write cache
        except OSError:
            pass


def open_disk(disk, writable=False):
    return MacDisk(disk.path, writable)


def keep_awake():
    return inhibit_sleep(["caffeinate", "-i", "-s", "-w", str(os.getpid())])


def _applescript_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def relaunch_as_admin(argv):
    """Ask for the admin password the usual macOS way and run the app again as root."""
    shell = " ".join(shlex.quote(a) for a in argv)
    script = f"do shell script {_applescript_string(shell)} with administrator privileges"
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if p.returncode != 0:
        if "-128" in p.stderr:  # user pressed Cancel
            raise RuntimeError("Didn't get administrator access, so PI-Backuper can't open disks.")
        raise RuntimeError(p.stderr.strip() or "Couldn't restart with administrator rights.")
