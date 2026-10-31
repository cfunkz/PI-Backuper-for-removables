<p align="center">
  <img src="favicon.ico" width="96" height="96" alt="PI-Backuper logo">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/GUI-Tkinter-blue?style=flat-square" alt="Tkinter">
  <img src="https://img.shields.io/badge/Built%20With-PyInstaller-3C873A?style=flat-square" alt="PyInstaller">
  <img src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows">
</p>

<p align="center">
  <img src="https://img.shields.io/github/stars/cfunkz/PI-Backuper?style=flat-square" alt="Stars">
  <img src="https://img.shields.io/github/issues/cfunkz/PI-Backuper?style=flat-square" alt="Open Issues">
  <img src="https://img.shields.io/github/release/cfunkz/PI-Backuper?style=flat-square" alt="Latest Release">
  <img src="https://img.shields.io/github/downloads/cfunkz/PI-Backuper/total?style=flat-square" alt="Downloads">
</p>

<p align="center">
  <a href="https://ko-fi.com/cfunkz81112">
    <strong>Buy me a coffee</strong>
  </a>
</p>

# PI-Backuper

Removable media backup & restore tool for **Windows**.

---

## Features

- Back up, restore and clone whole SD cards and USB drives (raw, sector by sector)
- Only lists removable / USB disks, and never the disk Windows runs from
- Optional gzip compression (empty space on a card shrinks to almost nothing)
- SHA-256 hash saved next to every backup (`image.img.gz.hash.txt`)
- Verify after writing: reads everything back and compares hashes, which catches dying and fake cards
- Reader and writer run in parallel, with live speed and ETA
- Portable `.exe`, no Python needed

## Safety nets

Writing raw disks goes badly when it goes wrong, so the app is careful about it:

- **Right disk, every time.** Each disk is identified by its number, hardware ID, serial and size. That's checked again right after you confirm, so swapping cards while the dialog is open can't point the restore at a different one.
- **Never your own files.** It refuses to save a backup onto the card being backed up, or to restore an image that lives on the target card.
- **No half-restored cards.** The start of the card (partition table) is wiped first and written last. If a restore or clone is cancelled, fails, or the power goes, the card shows up as blank instead of booting into something broken.
- **Damaged images are caught.** If the image doesn't match its `.hash.txt`, the restore stops and the partition table is never written.
- **No half-written backups.** Backups are written to `name.partial` and renamed only once they're complete and verified. Cancel or error, and the partial file is deleted.
- **Nothing else touches the card.** Target volumes are locked and dismounted. Source volumes are locked too when Windows allows it.
- **Free space check** before a backup starts. The PC is also kept from going to sleep while a job runs.
- **Old backups still work.** `.img`, `.img.gz` and the old `.hash.txt` files (including MD5 ones) from 1.0.x restore and verify as before.

---

## Screenshots

<img width="850" height="528" alt="image" src="https://github.com/user-attachments/assets/7f8e7f03-c2aa-4733-8d75-8ac1b64c2840" />

---

## Requirements

- Windows 10 / 11
- **Administrator privileges**
- USB SD card reader

---

## What It Can Back Up

PI-Backuper performs **raw, full-device imaging**.  
It reads the entire removable device **sector-by-sector**, not files.

This means it can back up **any removable USB media**, including:

- SD cards (Raspberry Pi OS, Ubuntu, LibreELEC, custom images)
- USB flash drives (FAT32, exFAT, NTFS, unknown filesystems)
- External USB HDDs / SSDs (full disk, all partitions)
- Bootable installers and recovery media
- Encrypted or unsupported filesystems (data is copied as-is)

PI-Backuper does **not** depend on the filesystem or OS on the device.  
If Windows can see it as a **removable USB disk**, it can be backed up.

⚠ Internal system drives are intentionally **not supported** for safety.

---

## Usage

1. Download the latest release from **Releases**
2. Run `PI-Backuper.exe` (UAC prompt required)
3. Select the device
4. Backup, restore or clone

⚠ **Restore and clone erase the whole target device.**

"Verify after writing" is on by default. It roughly doubles the time a job takes, but it's the only way to know the card really holds what was written. Turn it off if you're in a hurry.

---

## Build (from source)

```powershell
pip install -r requirements.txt pyinstaller

pyinstaller --onefile --noconsole --uac-admin --name PI-Backuper ^
  --icon favicon.ico ^
  --add-data "favicon.ico;." ^
  main.py
```

Or run it straight from source in an Administrator terminal: `python main.py`

## Project layout

```
main.py                  entry point (what PyInstaller builds)
pi_backuper/imaging.py   backup / restore / clone / verify, no Windows code
pi_backuper/windisk.py   finding disks, raw access, volume locking (Windows only)
pi_backuper/gui.py       Tkinter window
tests/                   tests for imaging.py using fake in-memory disks
```

The tests run on any OS: `pip install pytest && python -m pytest`

## Trademark Notice

Raspberry Pi is a trademark of the Raspberry Pi Foundation.  
PI-Backuper is an independent, community-developed project and is **not affiliated with or endorsed by** the Raspberry Pi Foundation.
