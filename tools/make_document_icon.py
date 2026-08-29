"""
Generate assets/pdf-document.ico — the icon Explorer paints on .pdf FILES when
Rapid PDF is the default handler.

This is deliberately NOT the app icon. assets/rapid-pdf.ico is the gold Qori
tile that identifies the *application* (exe, taskbar, Start menu, title bar).
Pointing the ProgID's DefaultIcon at that tile repainted every PDF on the
machine gold, which is confusing: PDFs have looked like a white page with a red
PDF label forever, and that is the file-type reading users rely on. So the
document icon keeps the universal page silhouette and carries only a small nod
to the brand (the folded corner is tinted with the Qori accent).

Original artwork. The page-with-folded-corner plus a red "PDF" label is a
generic file-type convention, drawn here from scratch; nothing is traced from
or derived from Adobe's icon.

Every size is drawn at its own resolution rather than downscaled from 256, so
the small ones can be hand-tuned. Below 48px the "PDF" lettering turns to mush,
so those sizes drop the text and keep a solid red band: at 16px, which is what
Explorer's details view actually shows, the page shape and the red stripe are
the whole message.

Run:  .venv\\Scripts\\python.exe tools\\make_document_icon.py
"""

from __future__ import annotations

import os
import struct
import sys

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import (
    QGuiApplication, QPixmap, QPainter, QColor, QPen, QFont, QPainterPath,
)

# Page.
PAPER = "#FFFFFF"
PAPER_EDGE = "#9AA2AE"     # the page must separate from a white Explorer row
RULE = "#C9CED6"           # faint text rules on the upper half
# The red PDF label. Chosen to sit clearly in "PDF red" territory while being
# our own value, not a sampled one.
RED = "#C5281C"
RED_DEEP = "#9E1F16"
# Brand nod: the folded corner takes the Qori accent (#F1AE04) knocked back to
# a paper tint, so it reads as the back of the sheet and not as a gold blob.
FOLD = "#F3E6C4"
FOLD_EDGE = "#DCC996"

SIZES = [256, 128, 64, 48, 32, 24, 16]

# Below this the "PDF" lettering is unreadable; the band goes solid instead.
TEXT_FLOOR = 48
# At or below this everything is drawn on whole pixels with hairline strokes.
SMALL = 32


def _render(size: int) -> QPixmap:
    """Draw the document icon at exactly `size` px."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = float(size)
    small = size <= SMALL

    # Page geometry. Taller than wide, the way a sheet of paper reads. The
    # small sizes get a slightly larger page: there is no room to spare and
    # the margin buys nothing at 16px.
    wf, hf = (0.72, 0.94) if small else (0.66, 0.90)
    pw, ph = s * wf, s * hf
    px, py = (s - pw) / 2.0, s * (1.0 - hf) / 2.0
    stroke = max(1.0, s * 0.012)
    if small:
        # Snap to whole pixels, then push the stroked outline half a pixel so a
        # 1px pen lands ON the grid. Without this the page picks up a two-pixel
        # grey halo on every side and reads as a grey box, not a white sheet.
        pw, ph = float(round(pw)), float(round(ph))
        px, py = float(int((s - pw) / 2)) + 0.5, float(int(s * 0.03)) + 0.5
        pw, ph = pw - 1.0, ph - 1.0
        stroke = 1.0
    fold = round(pw * 0.30) if small else pw * 0.32

    # Sheet with the top-right corner folded away.
    sheet = QPainterPath()
    sheet.moveTo(px, py)
    sheet.lineTo(px + pw - fold, py)
    sheet.lineTo(px + pw, py + fold)
    sheet.lineTo(px + pw, py + ph)
    sheet.lineTo(px, py + ph)
    sheet.closeSubpath()
    p.setBrush(QColor(PAPER))
    p.setPen(QPen(QColor(PAPER_EDGE), stroke))
    p.drawPath(sheet)

    # The fold itself. Below 32px its outline is pure noise, so it is filled
    # flat and left to read as a pale notch in the corner.
    corner = QPainterPath()
    corner.moveTo(px + pw - fold, py)
    corner.lineTo(px + pw - fold, py + fold)
    corner.lineTo(px + pw, py + fold)
    corner.closeSubpath()
    p.setBrush(QColor(FOLD))
    p.setPen(Qt.PenStyle.NoPen if small
             else QPen(QColor(FOLD_EDGE), max(1.0, s * 0.010)))
    p.drawPath(corner)

    # Text rules on the upper half. Pure decoration, and at small sizes they
    # only add noise, so they start at 48.
    if size >= TEXT_FLOOR:
        p.setPen(QPen(QColor(RULE), max(1.0, s * 0.018)))
        for i in range(3):
            ry = py + ph * (0.30 + i * 0.10)
            p.drawLine(int(px + pw * 0.16), int(ry),
                       int(px + pw * 0.72), int(ry))

    # The red label across the lower third. This is the part that survives the
    # drop to 16px and says "PDF" without any lettering.
    bh, by = ph * 0.24, py + ph * 0.60
    if small:
        bh = max(3.0, round(ph * 0.26))
        by = py - 0.5 + round(ph * 0.58)
        band = QRectF(px - 0.5, by, pw + 1.0, bh)
    else:
        band = QRectF(px, by, pw, bh)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(RED))
    p.drawRect(band)
    # A darker foot line gives the band a little weight at 128/256 without
    # muddying it small.
    if size >= TEXT_FLOOR:
        p.setBrush(QColor(RED_DEEP))
        p.drawRect(QRectF(px, by + bh - max(1.0, s * 0.012),
                          pw, max(1.0, s * 0.012)))

        f = QFont("Arial", 1)
        f.setBold(True)
        f.setPixelSize(max(6, int(bh * 0.78)))
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 96)
        # The wordmark goes down as a filled PATH, not as drawText. Qt's glyph
        # cache hands back subpixel (LCD) coverage here, which bakes red and
        # blue fringes into the .ico and makes "PDF" look like a misprint at
        # 48px. Filling an outline uses plain shape antialiasing, so the edges
        # come out neutral grey at every size.
        word = QPainterPath()
        word.addText(0.0, 0.0, f, "PDF")
        wb = word.boundingRect()
        word.translate(band.center().x() - wb.center().x(),
                       band.center().y() - wb.center().y())
        p.fillPath(word, QColor(PAPER))

    p.end()
    return pm


def _png_bytes(pm: QPixmap) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    pm.save(buf, "PNG")
    return bytes(ba)


def _write_ico(path: str, frames: list[tuple[int, bytes]]) -> None:
    """Assemble a multi-resolution .ico from PNG-encoded frames.

    Written by hand rather than through Pillow so the icon can be regenerated
    from the project venv, which carries no imaging library. PNG-compressed
    entries are what assets/rapid-pdf.ico already uses and what every Windows
    since Vista reads; each is declared 32bpp (RGBA).
    """
    frames = sorted(frames, key=lambda f: f[0], reverse=True)
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = len(header) + 16 * len(frames)
    entries, blobs = b"", b""
    for size, blob in frames:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 0 means 256 in the ICO directory
            0 if size >= 256 else size,
            0,                            # palette colours (0 = truecolour)
            0,                            # reserved
            1,                            # colour planes
            32,                           # bits per pixel
            len(blob),
            offset,
        )
        blobs += blob
        offset += len(blob)
    with open(path, "wb") as fh:
        fh.write(header + entries + blobs)


def main() -> int:
    QGuiApplication(sys.argv)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "assets")
    os.makedirs(out_dir, exist_ok=True)

    frames = [(s, _png_bytes(_render(s))) for s in SIZES]

    ico_path = os.path.join(out_dir, "pdf-document.ico")
    _write_ico(ico_path, frames)
    print(f"Wrote multi-size .ico: {ico_path} ({', '.join(str(s) for s in SIZES)})")

    png_path = os.path.join(out_dir, "pdf-document.png")
    with open(png_path, "wb") as fh:
        fh.write(dict(frames)[256])
    print(f"Wrote PNG: {png_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
