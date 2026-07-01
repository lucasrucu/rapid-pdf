"""Shared thumbnail helpers for the page panel and the organizer.

Both widgets show a grey placeholder sized to each page's real aspect ratio
until the actual thumbnail rasterises, so a landscape drawing's cell doesn't
visibly change shape when it renders. The math + per-height pixmap cache lived
duplicated in both; it's factored here. The two callers differ only in their
thumbnail dimensions and which document they read page sizes from, so those
stay parameters.
"""

from PySide6.QtGui import QPixmap, QColor


def aspect_ratio_placeholder(doc, page_num: int, thumb_w: int, thumb_h: int,
                             color: QColor, cache: dict) -> QPixmap:
    """A `color`-filled placeholder sized to the page's real aspect ratio.

    `doc` is any PDFDocument-like object exposing `get_page_size(page_num)`
    (or None). Page size is read without rasterising, so this stays cheap even
    for big documents. Pixmaps are memoised in `cache` keyed by pixel height,
    so the caller must clear that cache when `color` changes.
    """
    h = thumb_h
    if doc:
        w_pt, h_pt = doc.get_page_size(page_num)
        if w_pt > 0 and h_pt > 0:
            h = max(1, min(thumb_h, round(thumb_w * h_pt / w_pt)))
    pm = cache.get(h)
    if pm is None:
        pm = QPixmap(thumb_w, h)
        pm.fill(color)
        cache[h] = pm
    return pm
