"""Wheel and trackpad scrolling on the canvas, and the page turn at the edges.

A real trackpad can't be driven from a test, but the only thing that separates
one from a mouse at this level is the shape of the QWheelEvent: a mouse fills
angleDelta with 120-unit notches, a precision trackpad fills pixelDelta with a
stream of small deltas. Both shapes are constructed here and handed to the
genuine wheelEvent.

The assertions that matter most are the mouse ones. Nothing about the wheel path
was supposed to change, and the numbers below (117px per notch, one page turn
per notch at the edge) are what it did before any of this.
"""

import pytest

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QListWidget

from ui.canvas import PDFCanvas
from ui.scrolling import TRACKPAD_NOTCH_PX, TrackpadScrollFilter

# One mouse-wheel notch, and what that notch scrolls a QGraphicsView whose
# viewport is 800px tall: 3 lines (Qt's default wheelScrollLines) times the
# view's own single step of height/20 = 40, minus rounding = 117.
NOTCH = 120
NOTCH_PX = 117


class _FakeDoc:
    """Just enough document for page turning: a count."""

    def __init__(self, count: int):
        self._count = count
        self.doc = object()

    def page_count(self):
        return self._count


def _wheel(pixel_y=0, angle_y=0, ctrl=False, pos=(300, 400)):
    """A wheel event of one shape or the other (or, deliberately, both)."""
    p = QPointF(*pos)
    return QWheelEvent(
        p, p, QPoint(0, pixel_y), QPoint(0, angle_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def canvas(qt_app):
    """A canvas on page 2 of 5, with a page four viewports tall to scroll through."""
    c = PDFCanvas()
    c.resize(600, 800)
    c._doc = _FakeDoc(5)
    c._current_page = 2
    c._scene.setSceneRect(QRectF(0, 0, 500, 4000))
    c.show()
    QApplication.processEvents()
    yield c
    c.close()


@pytest.fixture
def turns(canvas):
    """Records every page the canvas turns to."""
    seen = []
    canvas.page_changed.connect(seen.append)
    return seen


# ---------------------------------------------------------------------------
# The regression guard: a mouse wheel behaves exactly as it always did
# ---------------------------------------------------------------------------

def test_mouse_notch_scrolls_what_it_always_did(canvas):
    vbar = canvas.verticalScrollBar()
    vbar.setValue(1000)
    canvas.wheelEvent(_wheel(angle_y=-NOTCH))
    assert vbar.value() == 1000 + NOTCH_PX


def test_mouse_notch_up_scrolls_the_same_the_other_way(canvas):
    vbar = canvas.verticalScrollBar()
    vbar.setValue(1000)
    canvas.wheelEvent(_wheel(angle_y=NOTCH))
    assert vbar.value() == 1000 - NOTCH_PX


def test_mouse_notch_at_the_bottom_turns_one_page(canvas, turns):
    vbar = canvas.verticalScrollBar()
    vbar.setValue(vbar.maximum())
    canvas.wheelEvent(_wheel(angle_y=-NOTCH))
    assert turns == [3]


def test_mouse_notch_at_the_top_turns_back_one_page(canvas, turns):
    vbar = canvas.verticalScrollBar()
    vbar.setValue(vbar.minimum())
    canvas.wheelEvent(_wheel(angle_y=NOTCH))
    assert turns == [1]


def test_every_mouse_notch_at_the_edge_turns_another_page(canvas, turns):
    """Held against the bottom, a wheel still turns a page per click.

    The accumulator that tames the trackpad must not make a mouse wheel need two
    clicks for the second page, or every long document gets slower to page.
    """
    vbar = canvas.verticalScrollBar()
    for _ in range(3):
        vbar.setValue(vbar.maximum())
        canvas.wheelEvent(_wheel(angle_y=-NOTCH))
    assert turns == [3, 3, 3]   # _current_page isn't advanced by the fake host


def test_mouse_notch_short_of_the_edge_does_not_turn(canvas, turns):
    vbar = canvas.verticalScrollBar()
    vbar.setValue(vbar.maximum() - 500)
    canvas.wheelEvent(_wheel(angle_y=-NOTCH))
    assert turns == []
    assert vbar.value() == vbar.maximum() - 500 + NOTCH_PX


def test_mouse_ctrl_notch_zooms_by_exactly_the_old_factor(canvas):
    before = canvas.transform().m11()
    canvas.wheelEvent(_wheel(angle_y=NOTCH, ctrl=True))
    assert canvas.transform().m11() == pytest.approx(before * 1.15, rel=1e-12)


def test_mouse_ctrl_notch_down_zooms_out_by_exactly_the_old_factor(canvas):
    canvas.scale(4.0, 4.0)   # room to come back down without hitting the floor
    before = canvas.transform().m11()
    canvas.wheelEvent(_wheel(angle_y=-NOTCH, ctrl=True))
    assert canvas.transform().m11() == pytest.approx(before / 1.15, rel=1e-12)


# ---------------------------------------------------------------------------
# The trackpad path
# ---------------------------------------------------------------------------

def test_trackpad_scrolls_by_the_pixels_it_reported(canvas):
    """40px of finger moves the page 40px, not a whole notch of 117."""
    vbar = canvas.verticalScrollBar()
    vbar.setValue(1000)
    canvas.wheelEvent(_wheel(pixel_y=-40))
    assert vbar.value() == 1040


def test_trackpad_scroll_accumulates_one_for_one(canvas):
    """Ten small events move the page by their sum, the way a finger drags a page."""
    vbar = canvas.verticalScrollBar()
    vbar.setValue(1000)
    for _ in range(10):
        canvas.wheelEvent(_wheel(pixel_y=-12))
    assert vbar.value() == 1120


def test_trackpad_scrolls_back_up(canvas):
    vbar = canvas.verticalScrollBar()
    vbar.setValue(1000)
    canvas.wheelEvent(_wheel(pixel_y=25))
    assert vbar.value() == 975


def test_pixel_delta_wins_when_qt_sends_both(canvas):
    """Some platforms fill both. The pixel one is the precise one, so it wins."""
    vbar = canvas.verticalScrollBar()
    vbar.setValue(1000)
    canvas.wheelEvent(_wheel(pixel_y=-30, angle_y=-NOTCH))
    assert vbar.value() == 1030   # 30px of finger, not 117px of notch


def test_trackpad_flick_at_the_bottom_does_not_fly_through_the_document(canvas, turns):
    """The bug, stated as a test.

    Fifty small events used to be fifty page turns. Now the travel is measured,
    and 50 x 10px is 500px, which is four notches' worth.
    """
    vbar = canvas.verticalScrollBar()
    for _ in range(50):
        vbar.setValue(vbar.maximum())
        canvas.wheelEvent(_wheel(pixel_y=-10))
    assert len(turns) == 500 // TRACKPAD_NOTCH_PX == 4


def test_trackpad_turns_the_page_once_a_notch_of_travel_lands(canvas, turns):
    vbar = canvas.verticalScrollBar()
    vbar.setValue(vbar.maximum())
    for _ in range(TRACKPAD_NOTCH_PX // 10 - 1):
        canvas.wheelEvent(_wheel(pixel_y=-10))
    assert turns == []             # 110px of travel: not there yet
    canvas.wheelEvent(_wheel(pixel_y=-10))
    assert turns == [3]            # 120px: turned


def test_travel_short_of_the_edge_is_not_banked(canvas, turns):
    """Scrolling down a long page must not arrive at the bottom with a turn owed.

    Otherwise the page flips the instant you reach the end of the one you were
    reading, which is exactly the feeling being fixed.
    """
    vbar = canvas.verticalScrollBar()
    vbar.setValue(1000)
    for _ in range(30):            # 300px of travel, none of it at the edge
        canvas.wheelEvent(_wheel(pixel_y=-10))
    assert turns == []
    vbar.setValue(vbar.maximum())
    canvas.wheelEvent(_wheel(pixel_y=-10))
    assert turns == []             # the tally restarted at the edge


def test_jitter_back_and_forth_never_adds_up_to_a_page_turn(canvas, turns):
    """Two fingers wobbling on a stationary page must not walk through it.

    Forty events, 2000px of travel between them, and not one page turn: every
    backward event pushes against an edge the view is nowhere near, which clears
    what the forward events had banked.
    """
    vbar = canvas.verticalScrollBar()
    vbar.setValue(vbar.maximum())
    for _ in range(20):
        canvas.wheelEvent(_wheel(pixel_y=-50))
        vbar.setValue(vbar.maximum())
        canvas.wheelEvent(_wheel(pixel_y=50))
        vbar.setValue(vbar.maximum())
    assert turns == []


def test_trackpad_ctrl_zoom_steps_smoothly_instead_of_a_notch_each(canvas):
    """One small pinch delta is a fraction of a zoom step, not a whole one."""
    before = canvas.transform().m11()
    canvas.wheelEvent(_wheel(pixel_y=12, ctrl=True))
    after = canvas.transform().m11()
    assert before < after < before * 1.15


def test_trackpad_ctrl_zoom_reaches_a_full_step_over_a_full_notch(canvas):
    before = canvas.transform().m11()
    for _ in range(TRACKPAD_NOTCH_PX // 12):
        canvas.wheelEvent(_wheel(pixel_y=12, ctrl=True))
    assert canvas.transform().m11() == pytest.approx(before * 1.15, rel=1e-9)


# ---------------------------------------------------------------------------
# The Windows shape: a precision touchpad that reports no pixelDelta at all
# ---------------------------------------------------------------------------

def test_a_windows_touchpad_flick_does_not_fly_through_the_document(canvas, turns):
    """Windows hands Qt WM_MOUSEWHEEL, so a touchpad arrives as angleDelta.

    It is still a stream: fractions of a notch, dozens of events per flick,
    nothing like the single 120 a wheel click sends. Each of those used to turn
    a page on its own, which is the same bug wearing the other delta.
    """
    vbar = canvas.verticalScrollBar()
    for _ in range(60):
        vbar.setValue(vbar.maximum())
        canvas.wheelEvent(_wheel(angle_y=-20))
    assert len(turns) == 60 * 20 // NOTCH == 10


def test_a_fraction_of_a_notch_scrolls_a_fraction_of_the_distance(canvas):
    """Qt's own handling banks the remainder, so six sixths add up to one notch."""
    vbar = canvas.verticalScrollBar()
    vbar.setValue(1000)
    for _ in range(6):
        canvas.wheelEvent(_wheel(angle_y=-NOTCH // 6))
    assert vbar.value() == pytest.approx(1000 + NOTCH_PX, abs=3)


def test_a_wheel_event_with_no_delta_at_all_does_nothing(canvas, turns):
    vbar = canvas.verticalScrollBar()
    vbar.setValue(1000)
    canvas.wheelEvent(_wheel())
    canvas.wheelEvent(_wheel(ctrl=True))
    assert vbar.value() == 1000
    assert turns == []


# ---------------------------------------------------------------------------
# The shared filter, on the lists that use it
# ---------------------------------------------------------------------------

@pytest.fixture
def scrolling_list(qt_app):
    lst = QListWidget()
    lst.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
    for i in range(200):
        lst.addItem(f"row {i}")
    lst.resize(200, 400)
    lst.show()
    QApplication.processEvents()
    filt = TrackpadScrollFilter(lst)
    yield lst, filt
    lst.close()


def test_filter_scrolls_a_list_by_trackpad_pixels(scrolling_list):
    """Without this the list would not move at all: Qt reads angleDelta only."""
    lst, _ = scrolling_list
    vbar = lst.verticalScrollBar()
    vbar.setValue(500)
    QApplication.sendEvent(lst.viewport(), _wheel(pixel_y=-45))
    assert vbar.value() == 545


def test_filter_leaves_the_mouse_wheel_to_qt(scrolling_list):
    """An angleDelta event is not consumed, so the list's own handling still runs."""
    lst, filt = scrolling_list
    assert filt.eventFilter(lst.viewport(), _wheel(angle_y=-NOTCH)) is False


def test_filter_leaves_ctrl_wheel_alone(scrolling_list):
    """Ctrl belongs to the zoom handlers, including the one on an unmerged branch."""
    lst, filt = scrolling_list
    assert filt.eventFilter(lst.viewport(), _wheel(pixel_y=-45, ctrl=True)) is False
