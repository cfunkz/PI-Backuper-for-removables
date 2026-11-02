"""Windows side: finding removable disks, opening them raw, locking volumes."""

import ctypes
import os
import struct
import subprocess
import time
from contextlib import contextmanager

import pywintypes
import win32con
import win32file
import wmi

from .common import Disk

IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020

SHARE_RW = win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE

# Our own handle on kernel32, so setting argtypes here can't upset anyone else's.
_k32 = ctypes.WinDLL("kernel32")
_k32.FindFirstVolumeW.restype = ctypes.c_void_p
_k32.FindFirstVolumeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
_k32.FindNextVolumeW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
_k32.FindVolumeClose.argtypes = [ctypes.c_void_p]
_k32.GetVolumeNameForVolumeMountPointW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                                    ctypes.c_uint]
INVALID_HANDLE = ctypes.c_void_p(-1).value

# Windows error codes worth explaining in plain words.
FRIENDLY_ERRORS = {
    5: "Access denied. Something still has the disk open, or it's write-protected.",
    19: "The device is write-protected. Check the lock switch on the SD card or adapter.",
    21: "The device isn't ready. Was it unplugged?",
    23: "Data error (CRC). The card has bad sectors.",
    112: "Not enough free space.",
    483: "The device reported a hardware error.",
    1117: "I/O error. The card or reader is failing, or it was unplugged.",
    1167: "The device was disconnected.",
}


def explain(e):
    """Turn a pywintypes.error into a readable OSError."""
    msg = FRIENDLY_ERRORS.get(e.winerror) or (e.strerror or "").strip() or "Unknown error"
    return OSError(f"{msg} ({e.funcname}, code {e.winerror})")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


@contextmanager
def keep_awake():
    """Stop Windows sleeping in the middle of a long job (call from the worker thread)."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    kernel32 = ctypes.windll.kernel32
    kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def disks_holding(path):
    """{disk number} of the physical disk `path` is on; empty for network shares etc."""
    n = _disk_number_of(path)
    return set() if n is None else {n}


def _disk_number_of(path):
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if len(drive) != 2 or drive[1] != ":":
        return None
    try:
        h = win32file.CreateFile(rf"\\.\{drive}", 0, SHARE_RW, None,
                                 win32con.OPEN_EXISTING, 0, None)
    except pywintypes.error:
        return None
    try:
        out = win32file.DeviceIoControl(h, IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 12)
        return struct.unpack("<III", bytes(out)[:12])[1]
    except pywintypes.error:
        return None
    finally:
        h.Close()


# ---------- listing ----------

def list_disks():
    """Removable / USB disks, never the one Windows runs from."""
    system_drive = os.environ.get("SystemDrive", "C:").upper()
    system_disk = _disk_number_of(system_drive + "\\")

    disks = []
    for d in wmi.WMI().Win32_DiskDrive():
        # Card readers with no card in them show up with no size.
        if d.Index is None or not d.Size:
            continue
        interface = (d.InterfaceType or "").upper()
        media = (d.MediaType or "").lower()
        # USB SSDs on UAS report "SCSI" and "External hard disk media".
        if interface != "USB" and "removable" not in media and "external" not in media:
            continue
        number = int(d.Index)
        letters = _letters(d)
        if number == system_disk or system_drive in letters:
            continue
        disks.append(Disk(
            id=number,
            name=f"Disk {number}",
            path=rf"\\.\PhysicalDrive{number}",
            size=int(d.Size),
            model=(d.Model or "USB disk").strip(),
            serial=(d.SerialNumber or "").strip(),
            hw_id=(d.PNPDeviceID or "").strip(),
            mounts=tuple(sorted(letters)),
        ))
    disks.sort(key=lambda x: x.id)
    return disks


def _letters(drive):
    letters = []
    try:
        for part in drive.associators("Win32_DiskDriveToDiskPartition"):
            for logical in part.associators("Win32_LogicalDiskToPartition"):
                if logical.DeviceID:
                    letters.append(str(logical.DeviceID).upper())
    except Exception:
        pass
    return letters


# ---------- raw access ----------

class WinDisk:
    """\\\\.\\PhysicalDriveN opened for raw, sector-aligned I/O."""

    def __init__(self, path, writable=False):
        self.path = path
        self.writable = writable
        access = win32con.GENERIC_READ | (win32con.GENERIC_WRITE if writable else 0)
        try:
            self._h = win32file.CreateFile(self.path, access, SHARE_RW, None,
                                           win32con.OPEN_EXISTING, 0, None)
        except pywintypes.error as e:
            raise explain(e) from None
        try:
            # WMI's size is rounded down to whole "cylinders"; this is the real one.
            out = win32file.DeviceIoControl(self._h, IOCTL_DISK_GET_LENGTH_INFO, None, 8)
            self.size = struct.unpack("<q", bytes(out))[0]
            out = win32file.DeviceIoControl(self._h, IOCTL_DISK_GET_DRIVE_GEOMETRY, None, 24)
            self.sector = struct.unpack("<qIIII", bytes(out))[4] or 512
        except pywintypes.error as e:
            self._h.Close()
            raise explain(e) from None
        if self.size <= 0:
            self._h.Close()
            raise OSError(f"{self.path} reports no media. Is the card inserted?")

    def read(self, offset, length):
        try:
            win32file.SetFilePointer(self._h, offset, win32file.FILE_BEGIN)
            _, data = win32file.ReadFile(self._h, length)
        except pywintypes.error as e:
            raise explain(e) from None
        if len(data) != length:
            raise OSError(f"Short read at byte {offset:,}. Was the device unplugged?")
        return bytes(data)

    def write(self, offset, data):
        try:
            win32file.SetFilePointer(self._h, offset, win32file.FILE_BEGIN)
            _, n = win32file.WriteFile(self._h, data)
        except pywintypes.error as e:
            raise explain(e) from None
        if n != len(data):
            raise OSError(f"Short write at byte {offset:,}. Was the device unplugged?")

    def flush(self):
        try:
            win32file.FlushFileBuffers(self._h)
        except pywintypes.error as e:
            raise explain(e) from None

    def close(self):
        if self._h is None:
            return
        if self.writable:
            # Ask Windows to re-read the new partition table.
            try:
                win32file.DeviceIoControl(self._h, IOCTL_DISK_UPDATE_PROPERTIES, None, 0)
            except pywintypes.error:
                pass
        self._h.Close()
        self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _volumes_on(number, letters):
    """[(shown as, path to open)] for every volume on disk `number`.

    Drive letters alone miss volumes that have none (a second partition
    with its letter removed, say), and Windows blocks raw writes over any
    mounted volume it hasn't been told to let go of. So walk all volumes and
    ask each which disk it lives on. Falls back to the letters if that fails.
    """
    fallback = [(letter, rf"\\.\{letter}") for letter in letters]
    try:
        found = _all_volumes_on(number, letters)
    except Exception:
        return fallback
    # Every lettered volume should have turned up; if not, trust the letters.
    if not set(letters) <= {shown for shown, _ in found}:
        return fallback
    return found


def _all_volumes_on(number, letters):
    shown = {}  # volume GUID name -> drive letter, so each volume is listed once
    for letter in letters:
        buf = ctypes.create_unicode_buffer(64)
        if _k32.GetVolumeNameForVolumeMountPointW(letter + "\\", buf, len(buf)):
            shown[buf.value] = letter

    buf = ctypes.create_unicode_buffer(64)
    find = _k32.FindFirstVolumeW(buf, len(buf))
    if find in (None, INVALID_HANDLE):
        return []
    found = []
    try:
        while True:
            name = buf.value  # \\?\Volume{guid}\
            if number in _volume_disks(name.rstrip("\\")):
                found.append((shown.get(name, f"a volume on Disk {number}"), name.rstrip("\\")))
            if not _k32.FindNextVolumeW(find, buf, len(buf)):
                break
    finally:
        _k32.FindVolumeClose(find)
    return found


def _volume_disks(path):
    """Disk numbers a volume is stored on (more than one for spanned volumes)."""
    try:
        h = win32file.CreateFile(path, 0, SHARE_RW, None, win32con.OPEN_EXISTING, 0, None)
    except pywintypes.error:
        return set()
    try:
        out = bytes(win32file.DeviceIoControl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, 1024))
    except pywintypes.error:
        return set()  # empty card reader, CD drive with no disc and so on
    finally:
        h.Close()
    if len(out) < 8:
        return set()
    count = struct.unpack_from("<I", out, 0)[0]
    # Header is 8 bytes; each extent is DiskNumber (padded to 8), offset, length.
    return {struct.unpack_from("<I", out, 8 + 24 * i)[0]
            for i in range(count) if 8 + 24 * i + 4 <= len(out)}


class VolumeLocks:
    """Lock (and optionally dismount) every volume on a disk while we work on it.

    Target disks need this: Windows refuses raw writes over a mounted volume,
    and anything writing to the card mid-restore would corrupt it.
    Source disks get a best-effort lock so nothing changes under a backup;
    letters that couldn't be locked end up in `failed`.
    """

    def __init__(self, volumes, dismount, required):
        self.volumes = volumes  # [(shown as, path)]
        self.dismount = dismount
        self.required = required
        self.failed = []
        self._handles = []

    def __enter__(self):
        try:
            for shown, path in self.volumes:
                h = self._lock(path)
                if h:
                    self._handles.append(h)
                elif self.required:
                    raise RuntimeError(f"Couldn't lock {shown}. Close any Explorer windows "
                                       "or programs using it and try again.")
                else:
                    self.failed.append(shown)
        except BaseException:
            self.release()
            raise
        return self

    def __exit__(self, *exc):
        self.release()

    def _lock(self, path):
        try:
            h = win32file.CreateFile(path,
                                     win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                     SHARE_RW, None, win32con.OPEN_EXISTING, 0, None)
        except pywintypes.error:
            return None
        # Explorer and antivirus often hold a handle for a moment, so retry.
        for _ in range(10):
            try:
                win32file.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0)
                if self.dismount:
                    win32file.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0)
                return h
            except pywintypes.error:
                time.sleep(0.3)
        h.Close()
        return None

    def release(self):
        for h in self._handles:
            try:
                h.Close()
            except Exception:
                pass
        self._handles = []


def open_disk(disk, writable=False):
    return WinDisk(disk.path, writable)


@contextmanager
def claim_source(disk):
    """Best-effort lock so nothing writes to the source mid-backup.
    Yields the letters that couldn't be locked."""
    with VolumeLocks(_volumes_on(disk.id, disk.mounts), dismount=False, required=False) as locks:
        yield locks.failed


def claim_target(disk):
    return VolumeLocks(_volumes_on(disk.id, disk.mounts), dismount=True, required=True)


def relaunch_as_admin(argv):
    """Start the app again through the UAC prompt. Doesn't wait for it."""
    params = subprocess.list2cmdline(argv[1:])
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", argv[0], params, None, 1)
    if result <= 32:
        raise RuntimeError("Didn't get administrator rights, so PI-Backuper can't open disks.")
