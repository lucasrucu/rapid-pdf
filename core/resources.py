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
