"""
Resource path resolution that works both in development and when frozen by
PyInstaller (onedir). PyInstaller unpacks bundled data under `sys._MEIPASS`;
in dev we resolve relative to the repo root.
"""

from __future__ import annotations

import os
import sys


def resource_path(*parts: str) -> str:
    """Absolute path to a bundled resource, dev or frozen."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def app_icon_path() -> str | None:
    """Path to the app .ico if it exists, else None."""
    p = resource_path("assets", "rapid-pdf.ico")
    return p if os.path.exists(p) else None


def bundled_tessdata_dir() -> str | None:
    """Folder with the shipped Tesseract language data, or None.

    assets/tessdata/eng.traineddata ships with the app (PyInstaller bundles
    the whole assets tree) so OCR works on machines without a Tesseract-OCR
    install. Returns the folder only if the English data is really there, so
    a broken bundle degrades to PyMuPDF's own system-Tesseract discovery
    instead of a hard OCR failure."""
    d = resource_path("assets", "tessdata")
    return d if os.path.exists(os.path.join(d, "eng.traineddata")) else None
