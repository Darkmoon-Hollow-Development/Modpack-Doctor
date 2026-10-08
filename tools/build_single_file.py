"""Pack the whole app into ONE file: dist/ModpackDoctor.pyzw (a Python zipapp, standard library only).

Anyone with Python 3.11+ can double-click it (Windows) or run ``python ModpackDoctor.pyzw`` (any system).
Run:  python tools/build_single_file.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist"


def build() -> Path:
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "app"
        shutil.copytree(ROOT / "modpack_doctor", stage / "modpack_doctor",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        target = OUT / "ModpackDoctor.pyzw"
        zipapp.create_archive(stage, target, main="modpack_doctor.cli:main",
                              interpreter="/usr/bin/env python3", compressed=True)
    return target


if __name__ == "__main__":
    t = build()
    print(f"Built {t} ({t.stat().st_size // 1024} KB)")
    sys.exit(0)
