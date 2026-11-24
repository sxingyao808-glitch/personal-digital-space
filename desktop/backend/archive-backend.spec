# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Archive desktop sidecar.

One-directory build on purpose: the shell spawns one process and stops one
process tree. A one-file build would unpack to a temp directory on every launch
and add a bootloader parent, which makes "no leftover backend" harder to prove.

Run through desktop/scripts/build-backend.ps1 to use the verified runtime
and the expected dist and work paths.
"""

from pathlib import Path

DESKTOP = Path(SPECPATH).resolve().parent
ROOT = DESKTOP.parent
WEB_ROOT = ROOT / "archive" / "web"

a = Analysis(
    [str(ROOT / "run_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # The Flask app resolves templates and static files from
    # ``Path(__file__).parent / "web"``, so the directory must ship verbatim.
    datas=[(str(WEB_ROOT), "archive/web")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "_pytest"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="archive-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="archive-backend",
)
