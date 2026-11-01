"""Disk listing and raw access, tested against fake /sys trees and plain files."""

import os
import sys

import pytest

from pi_backuper import imaging
from pi_backuper.disks.common import Disk

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX backends")


def make_disk(**kw):
    base = dict(id="sdb", name="/dev/sdb", path="/dev/sdb", size=8 << 30, model="Card",
                serial="123", hw_id="usb-port-1", mounts=())
    base.update(kw)
    return Disk(**base)


def test_label_and_same_hardware():
    d = make_disk(mounts=("/media/pi/boot",))
    assert d.label.startswith("/dev/sdb: Card #123")
    assert "8.0 GB" in d.label and "/media/pi/boot" in d.label
    assert d.same_hardware(make_disk(mounts=("/elsewhere",)))
    assert not d.same_hardware(make_disk(size=16 << 30))
    assert not d.same_hardware(make_disk(serial="999"))


def test_recheck_spots_a_swapped_card(monkeypatch):
    disks = pytest.importorskip("pi_backuper.disks")
    listed = make_disk()
    monkeypatch.setattr(disks, "list_disks", lambda: [make_disk(mounts=("/new",))])
    assert disks.recheck(listed).mounts == ("/new",)

    monkeypatch.setattr(disks, "list_disks", lambda: [make_disk(serial="other")])
    with pytest.raises(RuntimeError, match="changed or was unplugged"):
        disks.recheck(listed)


# ---------- raw access on POSIX ----------

@posix_only
def test_posix_disk_round_trip(tmp_path):
    from pi_backuper.disks.common import PosixDisk

    card = tmp_path / "card.bin"
    payload = os.urandom(imaging.CHUNK * 2 + 4096)
    card.write_bytes(payload)
    img = str(tmp_path / "card.img.gz")

    with PosixDisk(str(card)) as dev:
        assert dev.size == len(payload)
        imaging.backup(dev, img, imaging.Job())

    target = tmp_path / "target.bin"
    target.write_bytes(bytes(len(payload) + 8192))
    with PosixDisk(str(target), writable=True) as dev:
        imaging.restore(img, dev, imaging.Job())
    assert target.read_bytes()[:len(payload)] == payload


@posix_only
def test_posix_disk_explains_errors(tmp_path):
    from pi_backuper.disks.common import PosixDisk

    with pytest.raises(OSError, match="disconnected"):
        PosixDisk(str(tmp_path / "missing"))
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    with pytest.raises(OSError, match="no media"):
        PosixDisk(str(empty))


# ---------- Linux listing ----------

def _write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _link(link, target):
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)


@pytest.fixture
def fake_linux(tmp_path, monkeypatch):
    """A Raspberry Pi booted from its SD card, with a USB card reader and an NVMe drive."""
    linux = pytest.importorskip("pi_backuper.disks.linux")
    if sys.platform == "win32":
        pytest.skip("needs symlinks")
    sysfs = tmp_path / "sys"
    devices = sysfs / "devices"

    def block(name, devpath, size, removable="0", parts=(), majmins=()):
        node = devices / devpath / "block" / name
        _write(node / "size", str(size // 512))
        _write(node / "removable", removable)
        _link(sysfs / "block" / name, node)
        _link(sysfs / "class" / "block" / name, node)
        _link(node / "device", devices / devpath)
        for part, majmin in zip(parts, majmins):
            _write(node / part / "partition", "1")
            _link(sysfs / "class" / "block" / part, node / part)
            _link(sysfs / "dev" / "block" / majmin, node / part)
        return node

    # The card the Pi runs from.
    block("mmcblk0", "platform/emmc2/mmc_host/mmc0/mmc0:aaaa", 32 << 30,
          parts=["mmcblk0p1", "mmcblk0p2"], majmins=["179:1", "179:2"])
    _write(devices / "platform/emmc2/mmc_host/mmc0/mmc0:aaaa/name", "SD32G")
    # USB card reader with a card in it.
    usb = "platform/scb/pcie/usb2/2-1/2-1:1.0/host0/target0:0:0/0:0:0:0"
    block("sda", usb, 8 << 30, removable="1", parts=["sda1", "sda2"], majmins=["8:1", "8:2"])
    _write(devices / usb / "vendor", "Generic ")
    _write(devices / usb / "model", "STORAGE DEVICE")
    _write(devices / "platform/scb/pcie/usb2/2-1/serial", "000000001206")
    # Empty second slot of the same reader.
    block("sdb", usb.replace("0:0:0:0", "0:0:0:1"), 0, removable="1")
    # An NVMe drive on the PCIe bus.
    block("nvme0n1", "platform/pcie/nvme0", 256 << 30)
    # Not real disks at all.
    block("loop0", "virtual/loop0", 100 << 20)

    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 179:2 / / rw,noatime shared:1 - ext4 /dev/root rw\n"
        "25 22 179:1 / /boot/firmware rw shared:2 - vfat /dev/mmcblk0p1 rw\n"
        "40 22 8:1 / /media/pi/my\\040boot rw shared:3 - vfat /dev/sda1 rw\n"
        "41 22 8:2 / /media/pi/rootfs rw shared:4 - ext4 /dev/sda2 rw\n"
        "50 22 0:5 / /proc rw - proc proc rw\n"
    )
    swaps = tmp_path / "swaps"
    swaps.write_text("Filename\tType\tSize\tUsed\tPriority\n")

    monkeypatch.setattr(linux, "SYS", str(sysfs))
    monkeypatch.setattr(linux, "MOUNTINFO", str(mountinfo))
    monkeypatch.setattr(linux, "SWAPS", str(swaps))
    return linux


def test_linux_lists_only_the_card_reader(fake_linux):
    disks = fake_linux.list_disks()
    assert [d.id for d in disks] == ["sda"]
    d = disks[0]
    assert d.path == "/dev/sda"
    assert d.size == 8 << 30
    assert d.model == "Generic STORAGE DEVICE"
    assert d.serial == "000000001206"
    assert d.mounts == ("/media/pi/my boot", "/media/pi/rootfs")


def test_linux_finds_which_disk_holds_a_path(fake_linux):
    assert fake_linux.disks_holding("/media/pi/rootfs/home/pi") == {"sda"}
    assert fake_linux.disks_holding("/media/pi/my boot/x.img") == {"sda"}
    assert fake_linux.disks_holding("/home/pi/backups") == {"mmcblk0"}


def test_linux_trusts_major_minor_over_a_misleading_source(fake_linux, tmp_path):
    # Some machines have a real /dev/root that points somewhere unexpected.
    (tmp_path / "mountinfo").write_text("22 1 179:2 / / rw shared:1 - ext4 /dev/null rw\n")
    assert fake_linux.disks_holding("/home/pi") == {"mmcblk0"}


def test_linux_never_lists_the_disk_holding_swap(fake_linux, tmp_path):
    (tmp_path / "swaps").write_text("Filename\tType\n/dev/sda2 partition 0 0 -2\n")
    assert fake_linux.list_disks() == []


def test_linux_follows_device_mapper_to_the_disk(fake_linux, tmp_path):
    sysfs = tmp_path / "sys"
    dm = sysfs / "devices" / "virtual" / "block" / "dm-0"
    _write(dm / "size", "0")
    _link(sysfs / "class" / "block" / "dm-0", dm)
    _link(dm / "slaves" / "sda2", sysfs / "class" / "block" / "sda2")
    assert fake_linux._whole_disks("dm-0") == {"sda"}


# ---------- macOS listing ----------

def test_macos_maps_apfs_volumes_to_the_physical_disk():
    macos = pytest.importorskip("pi_backuper.disks.macos")
    listing = {
        "WholeDisks": ["disk0", "disk3", "disk4", "disk5"],
        "AllDisksAndPartitions": [
            {"DeviceIdentifier": "disk4", "Partitions": [
                {"DeviceIdentifier": "disk4s1", "MountPoint": "/Volumes/bootfs"},
                {"DeviceIdentifier": "disk4s2"},
            ]},
            {"DeviceIdentifier": "disk5",
             "APFSPhysicalStores": [{"DeviceIdentifier": "disk4s2"}],
             "APFSVolumes": [{"DeviceIdentifier": "disk5s1", "MountPoint": "/Volumes/Data"}]},
        ],
    }
    mounts = macos.mounts_by_disk(listing)
    assert sorted(mounts["disk4"]) == ["/Volumes/Data", "/Volumes/bootfs"]


def test_macos_candidates():
    macos = pytest.importorskip("pi_backuper.disks.macos")
    sd_slot = {"Internal": True, "RemovableMedia": True, "BusProtocol": "Secure Digital",
               "TotalSize": 32 << 30, "VirtualOrPhysical": "Physical"}
    usb = {"Internal": False, "BusProtocol": "USB", "TotalSize": 8 << 30}
    internal_ssd = {"Internal": True, "BusProtocol": "Apple Fabric", "TotalSize": 512 << 30}
    container = {"Internal": False, "VirtualOrPhysical": "Virtual", "TotalSize": 8 << 30}
    dmg = {"Internal": False, "BusProtocol": "Disk Image", "TotalSize": 1 << 30}
    empty_reader = {"Internal": False, "BusProtocol": "USB", "TotalSize": 0}
    assert macos.is_candidate(sd_slot) and macos.is_candidate(usb)
    for info in (internal_ssd, container, dmg, empty_reader):
        assert not macos.is_candidate(info)
