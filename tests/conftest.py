"""Test setup.

Qt needs a platform plugin even to paint into a QImage, so force the offscreen
one before PySide6 is imported anywhere. That keeps the whole set runnable on a
headless box and, more usefully, on a laptop without popping windows.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
