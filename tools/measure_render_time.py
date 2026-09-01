"""Measure what a raster scale actually costs, per page size. Feeds core.render_scale.

The app rasterises every page at a fixed 1.5 and has done since the first
build. That number was never measured, it was picked, and it is the reason a
scanned page of small text cannot be read: zooming does not re-rasterise, it
only scales the QGraphicsView transform, so zooming into a 108 DPI bitmap
magnifies the blur and can never resolve more detail than the bitmap holds.

Raising it for everything is not the answer either. Raster cost is quadratic in
scale and linear in page area, and an A1 engineering drawing is already eight
times the area of an A4. Going from 1.5 to 3.0 on A1 is a 4x cost on the most
expensive page in the app, and rendering runs on the GUI thread (there is no
QThreadPool anywhere in this codebase), so that cost is a frozen window, not a
spinner. The budget has to be in PIXELS, which is what both time and memory
actually track, and this script is what measures the constant.

What it reports, for each of A4 and A1 across the scale ladder:

    scale, output pixel dimensions, megapixels, wall time per render, and the
    QPixmap bytes those pixels occupy

Two details that make the numbers mean something:

  * Each cell is the median of several renders after a warm-up render, because
    the first rasterisation of a page pulls in fitz font and colorspace setup
    that never repeats and would otherwise be charged to scale 1.0 alone.
  * The synthetic pages carry real text at a small point size rather than being
    blank. A blank page renders far faster than any page a user opens, and
    tuning a budget against a blank page would set it too high.

Pixmap bytes are computed from the pixel count at 4 bytes per pixel (Qt stores
a 24-bit RGB888 QImage as 32-bit pixels once it becomes a QPixmap), not read
back from Qt, so the figure is the same on any platform and can be reasoned
about arithmetically against RENDER_CACHE_MAX.

    .venv\\Scripts\\python tools\\measure_render_time.py [repeats]
"""

import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Page sizes in PDF points, the two the app actually sees. A4 is what a scanned
# document, a datasheet or a checksheet arrives as; A1 is the engineering
# drawing that dominates the cost table.
PAGE_SIZES = {
    "A4": (595, 842),
    "A1": (1684, 2384),
}

# The sweep. 1.5 is what ships today, 3.0 is where small text becomes properly
# legible (216 DPI), and the steps between are there so the shape of the curve
# is visible rather than just its ends.
SCALES = (1.0, 1.5, 2.0, 2.5, 3.0)

DEFAULT_REPEATS = 5

# Qt stores an RGB888 QImage as 32-bit pixels once converted to a QPixmap, so
# the memory a rendered page occupies is 4 bytes per output pixel.
BYTES_PER_PIXEL = 4


def build_page(doc, width_pt: int, height_pt: int):
    """A page with enough small text on it to render like a real one.

    The text is the point. A blank page skips most of the rasteriser's work,
    and a budget tuned against a blank page would be set far too high for the
    documents this is meant to fix.
    """
    page = doc.new_page(width=width_pt, height=height_pt)
    y = 40
    while y < height_pt - 20:
        page.insert_text((30, y), "Rapid PDF render timing sample line 0123456789 "
                                  "the quick brown fox jumps over the lazy dog",
                         fontsize=7)
        y += 11
    return page


def measure(repeats: int) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    import fitz
    from PySide6.QtWidgets import QApplication

    # A QPixmap cannot be constructed without a QApplication, and
    # _render_page_at_zoom ends in QPixmap.fromImage, so the app comes first.
    QApplication([])
    from core.pdf_document import PDFDocument

    print(f"median of {repeats} renders per cell, after one warm-up render")
    print("page\tscale\tpixels\t\tMpx\tms\tMB")

    results = {}
    for name, (width_pt, height_pt) in PAGE_SIZES.items():
        doc = fitz.open()
        page = build_page(doc, width_pt, height_pt)
        for scale in SCALES:
            PDFDocument._render_page_at_zoom(page, scale)   # warm-up, discarded
            times = []
            for _ in range(repeats):
                start = time.perf_counter()
                pixmap = PDFDocument._render_page_at_zoom(page, scale)
                times.append((time.perf_counter() - start) * 1000.0)
            width_px, height_px = pixmap.width(), pixmap.height()
            megapixels = width_px * height_px / 1e6
            megabytes = width_px * height_px * BYTES_PER_PIXEL / (1024 * 1024)
            ms = statistics.median(times)
            results[(name, scale)] = (megapixels, ms, megabytes)
            print(f"{name}\t{scale}\t{width_px}x{height_px}\t{megapixels:.1f}\t"
                  f"{ms:.1f}\t{megabytes:.1f}")
        doc.close()

    print()
    print("What the numbers mean for the budget")
    for name in PAGE_SIZES:
        shipped = results[(name, 1.5)]
        sharpest = results[(name, 3.0)]
        print(f"  {name}: 1.5 costs {shipped[1]:.1f} ms / {shipped[2]:.1f} MB "
              f"({shipped[0]:.1f} Mpx), 3.0 costs {sharpest[1]:.1f} ms / "
              f"{sharpest[2]:.1f} MB ({sharpest[0]:.1f} Mpx)")

    # Six is core.pdf_document.RENDER_CACHE_MAX. The cache is a PAGE COUNT, so
    # raising the scale raises what six entries cost with nothing to stop it,
    # which is the number worth printing next to the timings.
    print()
    print("A full 6-entry render cache, per page size and scale (MB)")
    print("page\t" + "\t".join(str(s) for s in SCALES))
    for name in PAGE_SIZES:
        row = "\t".join(f"{results[(name, s)][2] * 6:.0f}" for s in SCALES)
        print(f"{name}\t{row}")
    return 0


def main():
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REPEATS
    return measure(repeats)


if __name__ == "__main__":
    sys.exit(main())
