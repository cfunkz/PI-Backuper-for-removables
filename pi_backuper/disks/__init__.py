"""Disk access for whichever OS we're running on.

Each backend (windows.py, linux.py, macos.py) has the same functions:

    list_disks()             removable disks, never the system disk
    disks_holding(path)      ids of the disks a file or folder is stored on
    claim_source(disk)       context manager while reading; yields mounts it
                             couldn't lock, so the user can be warned
    claim_target(disk)       context manager while writing; unmounts/locks
                             everything on the disk or raises
    open_disk(disk, writable)
    keep_awake()             context manager that stops the machine sleeping
    is_admin()
    relaunch_as_admin(argv)
"""

import os
import sys

if sys.platform == "win32":
    from . import windows as _backend
elif sys.platform == "darwin":
    from . import macos as _backend
elif sys.platform.startswith("linux"):
    from . import linux as _backend
else:
    raise ImportError(f"PI-Backuper doesn't support {sys.platform} yet.")

from .common import Disk  # noqa: E402

list_disks = _backend.list_disks
disks_holding = _backend.disks_holding
claim_source = _backend.claim_source
claim_target = _backend.claim_target
open_disk = _backend.open_disk
keep_awake = _backend.keep_awake
is_admin = _backend.is_admin

__all__ = ["Disk", "list_disks", "disks_holding", "claim_source", "claim_target",
           "open_disk", "keep_awake", "is_admin", "recheck", "relaunch_as_admin"]


def recheck(disk):
    """Make sure `disk` is still the same physical device it was when listed.

    Disk numbers and names get reused when cards are swapped, so a stale one
    could point at a different card. Returns the fresh entry (mounts may change).
    """
    for d in list_disks():
        if d.id == disk.id:
            if d.same_hardware(disk):
                return d
            break
    raise RuntimeError(f"{disk.name} changed or was unplugged since the list was "
                       "refreshed. Press Refresh and pick it again.")


def relaunch_as_admin():
    """Start another copy of the app with admin/root rights.

    On Linux and macOS this waits for that copy to exit; on Windows it returns
    straight away. Raises RuntimeError if the rights weren't granted.
    """
    if getattr(sys, "frozen", False):
        argv = [sys.executable]
    else:
        # Start from main.py by full path: the elevated copy may begin in another
        # folder with a fresh environment (UAC does that), so "-m" could miss us.
        main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "main.py")
        argv = [sys.executable, main_py] if os.path.exists(main_py) else \
            [sys.executable, "-m", "pi_backuper"]
    _backend.relaunch_as_admin(argv + sys.argv[1:])
