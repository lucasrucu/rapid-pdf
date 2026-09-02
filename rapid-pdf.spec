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
# NO .pdf DOCUMENT ICON AT THE ONEDIR ROOT, deliberately.
#
# 1.7.0 ended with a post-COLLECT file copy that put assets/pdf-document.ico
# beside the exe, because RapidPDF.Document\DefaultIcon pointed at
# {app}\pdf-document.ico and an in-app update, which is a robocopy of this
# folder and never runs the installer, was the only delivery path that did not
# carry it.
#
# The installer no longer claims DefaultIcon at all (see rapid-pdf.iss: it
# deletes the key so a PDF keeps whatever icon its real handler gives it), so
# there is nothing pointing at that path and nothing to deliver. The copy is
# gone with it. The .ico itself stays in assets/, and still ships inside
# _internal/assets/ through the datas entry above, so putting it back is one
# registry line and not a redraw.
#
# If it ever DOES come back, it has to come back here as well as in [Files].
# PyInstaller 6 puts every datas entry under _internal/, whatever destination
# is named, so there is no datas line that reaches the onedir root; the copy
# has to happen after COLLECT, and the installer's [Files] line alone reaches
# nobody who updates from inside the app.
# ---------------------------------------------------------------------------
