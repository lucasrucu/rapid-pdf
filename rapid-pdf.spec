# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for rapid-pdf: onedir, windowed (no console), Qori icon +
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

# assets/ includes assets/tessdata/eng.traineddata (~4 MB, tessdata_fast,
# Apache-2.0): the Tesseract language data the OCR feature needs at runtime.
# PyMuPDF embeds the OCR engine itself, so this file is the ONLY OCR
# dependency that has to ship; without it, OCR only works on machines that
# happen to have Tesseract-OCR installed. See assets/tessdata/README.txt.
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

# ---------------------------------------------------------------------------
# The .pdf FILE icon, placed at the onedir ROOT, next to the exe.
#
# WHY IT CANNOT BE A `datas` ENTRY. PyInstaller 6 puts every bundled data file
# under `_internal/`, whatever destination you name: ("assets", "assets")
# lands at `_internal/assets/`, and ("...ico", ".") would land at
# `_internal/`, not beside the exe. There is no datas destination that reaches
# the onedir root, so the copy has to happen after COLLECT has built it.
#
# WHY IT HAS TO BE AT THE ROOT AT ALL. The ProgID's DefaultIcon is
# `{app}\pdf-document.ico` (see rapid-pdf.iss). The shell stores that as a
# literal path and keeps it forever, so it must point somewhere a PyInstaller
# layout change can never move.
#
# WHY THE INSTALLER'S [Files] LINE IS NOT ENOUGH. Inno copies this file to
# {app} at INSTALL time, but an in-app update never runs Inno: it downloads
# the portable zip, which is exactly this onedir folder, and robocopies it
# over the install (core/update/swap.py). So anything that reaches users only
# through the installer never reaches anyone who updates from inside the app.
# That is the real 1.7.0 defect: the DefaultIcon fix shipped, and the file it
# points at did not. Putting the icon in the onedir folder puts it in the zip,
# which puts it on both paths.
# ---------------------------------------------------------------------------
import shutil
from pathlib import Path

_document_icon = Path(SPECPATH) / "assets" / "pdf-document.ico"
_onedir_root = Path(DISTPATH) / "rapid-pdf"
if not _document_icon.is_file():
    raise SystemExit(
        f"missing {_document_icon}: the .pdf document icon is what "
        "RapidPDF.Document\\DefaultIcon points at, and a build without it "
        "leaves every PDF on the machine with a blank icon. "
        "Regenerate it with: python tools/make_document_icon.py"
    )
shutil.copy2(_document_icon, _onedir_root / "pdf-document.ico")
