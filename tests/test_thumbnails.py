"""The thumbnail arithmetic that broke at 6f884e3.

`draw_thumbnail` centres a page thumbnail inside its cell. It used to mix device
pixels with logical pixels, so above 100% display scaling the thumbnail drew
outside its own selection border. Nobody reviewing at 100% could see it, which
is why it needs a test rather than an eye.
"""

import pytest

from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from ui.thumbnails import draw_thumbnail, fit_size


# ---------------------------------------------------------------------------
# fit_size: pure arithmetic, no Qt, no display.
# ---------------------------------------------------------------------------

def test_portrait_fits_by_height():
    assert fit_size(200, 400, 100, 100) == (50, 100)


def test_landscape_fits_by_width():
    assert fit_size(400, 200, 100, 100) == (100, 50)


def test_never_upscales():
    # Already inside the box: returned unchanged, not blown up to fill the cell.
    assert fit_size(40, 30, 100, 100) == (40, 30)


def test_degenerate_input_returns_the_box():
    assert fit_size(0, 100, 80, 60) == (80, 60)
    assert fit_size(-5, -5, 80, 60) == (80, 60)


# ---------------------------------------------------------------------------
# draw_thumbnail: geometry at a faked devicePixelRatio.
# ---------------------------------------------------------------------------

class _FakeIcon:
    """Returns a fixed pixmap from `pixmap()`, whatever size is asked for.

    This is the shape QIcon hands back on a scaled display: a pixmap holding
    MORE device pixels than the logical size requested, with the ratio recorded
    on the pixmap. Faking it keeps the test independent of the machine's real
    scaling, which is the whole point.
    """

    def __init__(self, pm: QPixmap):
        self._pm = pm

    def pixmap(self, size: QSize) -> QPixmap:
        return self._pm


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


def _painted_bounds(img: QImage) -> QRect:
    """Bounding box of everything that is not the background fill."""
    bg = img.pixel(0, 0)
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixel(x, y) != bg:
                xs.append(x)
                ys.append(y)
    assert xs, "nothing was painted"
    return QRect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def test_thumbnail_stays_inside_its_area_at_dpr_1_5(qt_app):
    pm = QPixmap(300, 400)          # device pixels
    pm.setDevicePixelRatio(1.5)     # -> 200 x 266.67 logical
    pm.fill(QColor("#FF0000"))

    area = QRect(10, 10, 100, 130)
    img = QImage(120, 150, QImage.Format.Format_RGB32)
    img.fill(QColor("#FFFFFF"))
    painter = QPainter(img)
    draw_thumbnail(painter, _FakeIcon(pm), area)
    painter.end()

    drawn = _painted_bounds(img)
    assert area.contains(drawn), f"{drawn} spills outside {area}"
    # Centred: equal slack on both sides, within the odd-pixel rounding.
    assert abs((drawn.left() - area.left()) - (area.right() - drawn.right())) <= 1
    assert abs((drawn.top() - area.top()) - (area.bottom() - drawn.bottom())) <= 1
    # And it fills the axis it was fitted on, so it is not silently tiny.
    assert drawn.height() == area.height()
