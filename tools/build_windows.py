"""Build the Windows program and, when Inno Setup is installed, a real installer.

Run this on Windows, from the repository folder:

    pip install pyinstaller
    python tools/build_windows.py

Output:
    dist/ModpackDoctor/ModpackDoctor.exe     the program (a folder; it also runs on its own)
    dist/ModpackDoctor-Setup.exe             the installer (asks where to install, adds Start Menu + Desktop shortcuts)

The installer needs Inno Setup 6 (https://jrsoftware.org/isinfo.php). Without it only the program folder is built.
Nothing here is Windows-only code, so the PyInstaller step also works on Linux/macOS for a quick check.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "modpack_doctor" / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1) if m else "0"


def build_program() -> Path:
    data = ROOT / "modpack_doctor" / "data"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
        "--name", "ModpackDoctor", "--icon", str(ROOT / "assets" / "icon.ico"),
        "--add-data", f"{data}{os.pathsep}modpack_doctor/data",
        "--distpath", str(DIST), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
        str(ROOT / "ModpackDoctor.pyw"),
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)
    return DIST / "ModpackDoctor"


def find_iscc() -> str | None:
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            for sub in ("Inno Setup 6", "Programs/Inno Setup 6"):
                p = Path(base) / sub / "ISCC.exe"
                if p.exists():
                    return str(p)
    return None


def build_installer() -> Path | None:
    iscc = find_iscc()
    if not iscc:
        print("Inno Setup was not found, so only the program folder was built (install Inno Setup 6 for the installer).")
        return None
    subprocess.run([iscc, f"/DAppVersion={version()}", str(ROOT / "tools" / "installer.iss")], check=True, cwd=ROOT)
    return DIST / "ModpackDoctor-Setup.exe"


if __name__ == "__main__":
    folder = build_program()
    print(f"Built {folder}")
    setup = build_installer()
    if setup:
        print(f"Built {setup}")
