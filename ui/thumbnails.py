"""Shared thumbnail helpers for the page panel and the organizer.

Both widgets show a grey placeholder sized to each page's real aspect ratio
until the actual thumbnail rasterises, so a landscape drawing's cell doesn't
visibly change shape when it renders. The math + pixmap cache lived duplicated
in both; it's factored here. The two callers differ only in their thumbnail
dimensions and which document they read page sizes from, so those stay
parameters.

Both also draw the thumbnail centred inside its cell, and both used to do that
with their own copy of the same arithmetic. That arithmetic mixed device pixels
with logical pixels and broke on any display scaled above 100%: see
`draw_thumbnail` for what goes wrong. It lives here now so the two delegates
cannot drift apart again.
"""

from PySide6.QtCore import QRect
from PySide6.QtGui import QPixmap, QColor


def fit_size(w: float, h: float, box_w: float, box_h: float) -> tuple[int, int]:
    """Largest (width, height) with the aspect of `w`x`h` that fits `box_w`x`box_h`.

    Only ever shrinks: something already inside the box is returned unchanged,
    so a small thumbnail is never blown up to fill its cell.
    """
    if w <= 0 or h <= 0:
        return max(1, round(box_w)), max(1, round(box_h))
    scale = min(box_w / w, box_h / h, 1.0)
    return max(1, round(w * scale)), max(1, round(h * scale))


def aspect_ratio_placeholder(doc, page_num: int, thumb_w: int, thumb_h: int,
                             color: QColor, cache: dict) -> QPixmap:
    """A `color`-filled placeholder sized to the page's real aspect ratio.

    `doc` is any PDFDocument-like object exposing `get_page_size(page_num)`
    (or None). Page size is read without rasterising, so this stays cheap even
    for big documents. Pixmaps are memoised in `cache` keyed by pixel size, so
    the caller must clear that cache when `color` changes.

    The placeholder is fitted inside the whole `thumb_w` x `thumb_h` box, which
    is what the real thumbnail gets fitted to as well. Clamping only the height
    (what this used to do) left a portrait page's placeholder too wide, so the
    cell visibly changed shape the moment the page rasterised, which is exactly
    what the placeholder exists to prevent.
    """
    w, h = thumb_w, thumb_h
    if doc:
        w_pt, h_pt = doc.get_page_size(page_num)
        if w_pt > 0 and h_pt > 0:
            w, h = fit_size(w_pt, h_pt, thumb_w, thumb_h)
    pm = cache.get((w, h))
    if pm is None:
        pm = QPixmap(w, h)
        pm.fill(color)
        cache[(w, h)] = pm
    return pm


def draw_thumbnail(painter, icon, area: QRect):
    """Draw `icon` centred inside `area`, never spilling outside it.

    Two traps this exists to avoid, both of which put the thumbnail outside its
    own selection border on a scaled display:

    1. `QIcon.pixmap(size)` treats `size` as LOGICAL pixels and asks its engine
       for `size * devicePixelRatio` device pixels. When the stored pixmap is
       smaller than that it comes back unscaled with a devicePixelRatio of its
       own, so `pm.width()` is a device-pixel count while `area` is measured in
       logical pixels. Centring one against the other lands the thumbnail off
       to the side, or above the top of the cell.
    2. Even sized correctly, `QIcon.pixmap` only ever scales DOWN to the
       requested box in device pixels, so a pixmap whose device-independent
       size still overhangs `area` gets drawn overhanging.

    So: take the device-independent size, fit that to `area`, and draw into an
    explicit target rect. Works at any devicePixelRatio, for portrait,
    landscape and rotated pages alike.
    """
    if icon is None or area.width() <= 0 or area.height() <= 0:
        return
    pm = icon.pixmap(area.size())
    if pm.isNull():
        return
    size = pm.deviceIndependentSize()
    w, h = fit_size(size.width(), size.height(), area.width(), area.height())
    x = area.x() + (area.width() - w) // 2
    y = area.y() + (area.height() - h) // 2
    if (w, h) == (round(size.width()), round(size.height())):
        # Already the right size: blit it rather than paying for a rescale on
        # every repaint (thumbnails repaint on hover, selection and scroll).
        painter.drawPixmap(x, y, pm)
    else:
        painter.drawPixmap(QRect(x, y, w, h), pm)
