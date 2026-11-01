"""Build the app for the OS this runs on and pack it ready for a release.

    pip install -r requirements.txt pyinstaller pillow
    python build.py

Output lands in dist/release/:

    Windows  PI-Backuper-<version>-windows-x64.exe   single exe, asks for admin on start
    macOS    PI-Backuper-<version>-macos-arm64.zip   zipped .app (x64 on Intel Macs)
    Linux    PI-Backuper-<version>-linux-x64.tar.gz  single executable (arm64 on a Pi)
"""

import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
NAME = "PI-Backuper"
DIST = ROOT / "dist"
OUT = DIST / "release"


def version():
    text = (ROOT / "pi_backuper" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def arch():
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine


def pyinstaller():
    data = os.pathsep.join  # PyInstaller wants "src;dest" on Windows, "src:dest" elsewhere
    args = [
        str(ROOT / "main.py"),
        "--name", NAME,
        "--noconfirm", "--clean",
        "--distpath", str(DIST),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--add-data", data([str(ROOT / "favicon.ico"), "."]),
        "--add-data", data([str(ROOT / "icon.png"), "."]),
        "--icon", str(ROOT / "favicon.ico"),  # converted to .icns on macOS (needs Pillow)
    ]
    if sys.platform == "win32":
        args += ["--onefile", "--noconsole", "--uac-admin"]
    elif sys.platform == "darwin":
        # A normal .app folder. One-file .app bundles are on their way out in PyInstaller.
        args += ["--windowed", "--osx-bundle-identifier", "tech.routezero.pi-backuper"]
    else:
        args += ["--onefile"]
    PyInstaller.__main__.run(args)


def package(ver):
    OUT.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        target = OUT / f"{NAME}-{ver}-windows-{arch()}.exe"
        shutil.copy2(DIST / f"{NAME}.exe", target)
    elif sys.platform == "darwin":
        target = OUT / f"{NAME}-{ver}-macos-{arch()}.zip"
        # ditto keeps the bundle's symlinks and permissions, which zipfile doesn't.
        subprocess.run(["ditto", "-c", "-k", "--keepParent",
                        str(DIST / f"{NAME}.app"), str(target)], check=True)
    else:
        target = OUT / f"{NAME}-{ver}-linux-{arch()}.tar.gz"
        with tarfile.open(target, "w:gz") as tar:
            tar.add(DIST / NAME, arcname=NAME)  # tar keeps the executable bit
    return target


def main():
    ver = version()
    if OUT.exists():
        shutil.rmtree(OUT)
    pyinstaller()
    print(f"\nBuilt {package(ver)}")


if __name__ == "__main__":
    main()
