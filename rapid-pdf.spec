# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for rapid-pdf — onedir, windowed (no console), Qori icon +
version metadata. See docs/build.md for the full build + installer steps.

Build:  .venv\\Scripts\\pyinstaller rapid-pdf.spec --noconfirm
Output: dist\\rapid-pdf\\rapid-pdf.exe  (a folder, fed to Inno Setup)

Notes baked in from docs/build.md research:
- onedir (not onefile): faster start, far fewer AV false positives, and it's the
  natural input to the installer. Also sidesteps the PyMuPDF onefile+windowed
  "No output specified" gotcha.
- qtawesome ships its glyph FONTS as package data; collect_data_files pulls them
  so the toolbar icons render in the frozen build.
- Build from the project's clean PySide6-only venv (no global PySide6/PyQt), or
  PyInstaller may grab the wrong Qt binding.
"""

from PyInstaller.utils.hooks import collect_data_files

datas = [("assets", "assets")]
datas += collect_data_files("qtawesome")  # bundle the icon-font files

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the bundle lean / avoid Qt-binding collisions.
        "PyQt5", "PyQt6", "PySide2", "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rapid-pdf",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                      # windowed app, no console window
    disable_windowed_traceback=False,
    icon="assets/rapid-pdf.ico",
    version="packaging/version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="rapid-pdf",
)
