"""
Generate assets/rapid-pdf.ico — a Qori-branded multi-size app icon.

Design: a rounded amber/gold tile (Qori Sovereign accent #F1AE04) with a white
document glyph and a folded corner, the "PDF" wordmark across the bottom. Drawn
once at high resolution, then downscaled to the standard icon sizes so it stays
crisp in the taskbar, Start menu, title bar, and Add/Remove Programs.

Run:  .venv\\Scripts\\python.exe tools\\make_icon.py
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import (
    QGuiApplication, QPixmap, QPainter, QColor, QBrush, QPen, QFont,
    QPainterPath, QLinearGradient,
)

GOLD = "#F1AE04"
GOLD_DEEP = "#D6970A"
INK = "#2A2010"
PAPER = "#FFFFFF"

SIZES = [256, 128, 64, 48, 32, 16]


def _render(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = size

    # Rounded gold tile with a subtle vertical gradient.
    grad = QLinearGradient(0, 0, 0, s)
    grad.setColorAt(0, QColor(GOLD))
    grad.setColorAt(1, QColor(GOLD_DEEP))
    radius = s * 0.22
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    inset = s * 0.06
    p.drawRoundedRect(QRectF(inset, inset, s - 2 * inset, s - 2 * inset),
                      radius, radius)

    # Document glyph (white sheet with a folded top-right corner).
    if s >= 32:
        dw, dh = s * 0.40, s * 0.50
        dx, dy = (s - dw) / 2, s * 0.20
        fold = dw * 0.32
        path = QPainterPath()
        path.moveTo(dx, dy)
        path.lineTo(dx + dw - fold, dy)
        path.lineTo(dx + dw, dy + fold)
        path.lineTo(dx + dw, dy + dh)
        path.lineTo(dx, dy + dh)
        path.closeSubpath()
        p.setBrush(QColor(PAPER))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(path)
        # Folded corner triangle (slightly darker).
        corner = QPainterPath()
        corner.moveTo(dx + dw - fold, dy)
        corner.lineTo(dx + dw - fold, dy + fold)
        corner.lineTo(dx + dw, dy + fold)
        corner.closeSubpath()
        p.setBrush(QColor("#E7DFCB"))
        p.drawPath(corner)
        # "PDF" wordmark.
        f = QFont("Arial", 1)
        f.setBold(True)
        f.setPixelSize(int(s * 0.16))
        p.setFont(f)
        p.setPen(QPen(QColor(GOLD_DEEP)))
        p.drawText(QRectF(dx, dy + dh * 0.42, dw, dh * 0.5),
                   Qt.AlignmentFlag.AlignCenter, "PDF")
    else:
        # At 16px a document glyph is mush; draw a bold ink "P" instead.
        f = QFont("Arial", 1)
        f.setBold(True)
        f.setPixelSize(int(s * 0.62))
        p.setFont(f)
        p.setPen(QPen(QColor(INK)))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "P")
    p.end()
    return pm


def main() -> int:
    QGuiApplication(sys.argv)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "assets")
    os.makedirs(out_dir, exist_ok=True)

    pixmaps = [_render(s) for s in SIZES]
    # Also export the 256px PNG for docs / Inno wizard image use.
    pixmaps[0].save(os.path.join(out_dir, "rapid-pdf.png"), "PNG")

    ico_path = os.path.join(out_dir, "rapid-pdf.ico")
    # QImageWriter can't author a multi-size .ico directly; use Pillow if present,
    # else fall back to a single 256px .ico via Qt (still valid, just one size).
    try:
        from PIL import Image
        import io
        imgs = []
        for pm in pixmaps:
            pm.save_to_png = None  # noqa
            ba = _qpixmap_to_png_bytes(pm)
            imgs.append(Image.open(io.BytesIO(ba)).convert("RGBA"))
        imgs[0].save(ico_path, format="ICO",
                     sizes=[(s, s) for s in SIZES])
        print(f"Wrote multi-size .ico (Pillow): {ico_path}")
    except Exception as exc:
        pixmaps[0].save(ico_path, "ICO")
        print(f"Wrote single-size .ico (Qt fallback, no Pillow: {exc}): {ico_path}")
    print(f"Wrote PNG: {os.path.join(out_dir, 'rapid-pdf.png')}")
    return 0


def _qpixmap_to_png_bytes(pm: QPixmap) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    pm.save(buf, "PNG")
    return bytes(ba)


if __name__ == "__main__":
    raise SystemExit(main())
