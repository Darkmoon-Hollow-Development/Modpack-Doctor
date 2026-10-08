# PyInstaller spec: builds a windowed "onedir" app (dist/ModpackDoctor/ModpackDoctor.exe on Windows).
# Run from the project folder:  python -m PyInstaller --noconfirm --clean ModpackDoctor.spec
import sys

block_cipher = None
icon = "installer/ModpackDoctor.ico" if sys.platform == "win32" else None

a = Analysis(
    ["ModpackDoctor.pyw"],
    pathex=["."],
    datas=[("modpack_doctor/data", "modpack_doctor/data")],
    hiddenimports=[],
    excludes=["unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ModpackDoctor",
    console=False,          # no black console window
    icon=icon,
    upx=False,              # UPX-packed executables trigger more antivirus false positives
)
coll = COLLECT(exe, a.binaries, a.datas, name="ModpackDoctor", upx=False)
