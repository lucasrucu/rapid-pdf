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

THE TWO ANTI-FALSE-POSITIVE SETTINGS IN THIS FILE, AND WHY THEY ARE WRITTEN OUT
RATHER THAN LEFT TO A DEFAULT. On 4 September 2026 Sophos blocked the 1.8.1
installer on download, on a work laptop, before anybody had run it. Nothing was
wrong with the build. Sophos's machine-learning engine convicts on a shape, and
the shape it is tuned for is an unsigned executable that almost nobody has
downloaded yet. A PyInstaller app matches that shape on its own, and two build
options make the match tighter, so both are turned off here on purpose:

- `upx=False` on EXE and on COLLECT. UPX is a runtime packer, and self-modifying
  compressed code that unpacks itself in memory is what actual malware does to
  hide, so a packed section is close to a straight conviction in a heuristic
  engine. It buys tens of megabytes off an install that is already going to be
  around 200 MB, which is not a trade worth making. Left unset PyInstaller
  defaults to using UPX when it finds it on PATH, so a machine that happens to
  have UPX installed would silently start packing the build. Stating it here
  means the build does the same thing on every machine.
- onedir, again. onefile writes a self-extracting stub that unpacks 100+ MB to a
  temp folder and executes it from there on every launch, which is both the
  packer behaviour above and the dropper behaviour scanners watch for. onedir
  has neither.

Signing is the actual fix for the prevalence half of it and is a separate
decision. These two are free and worth having either way. The third free
mitigation, building the PyInstaller bootloader from source so the exe does not
carry the stock bootloader bytes that every PyInstaller app on earth shares, is
a property of how PyInstaller was INSTALLED rather than of this spec. It is
documented in docs/build.md under "Reducing antivirus false positives".
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
    upx=False,                          # never pack: see the module docstring
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
    upx=False,                          # and not on the DLLs either, same reason
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
# {app} at INSTALL time, and there is a whole class of user who never runs
# Inno: a portable copy is this onedir folder, unzipped by hand, and it is
# updated by unzipping the next one over it. So anything that reaches users
# only through the installer never reaches any of them. That is the real 1.7.0
# defect: the DefaultIcon fix shipped, and the file it points at did not.
# Putting the icon in the onedir folder puts it in the zip, which puts it on
# both paths.
#
# It was worse before 1.10.0, when the in-app updater laid the zip over an
# INSTALLED copy too, so the installer's [Files] line reached nobody who
# updated from inside the app. That updater is gone (core/update/installer.py)
# and an installed copy now runs setup like everybody else, but the rule below
# is unchanged: the portable zip still has to be complete on its own.
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
