"""Ctrl+wheel / Ctrl +-0 zoom on the Organizer's page thumbnails.

The parts worth pinning down are the ladder (clamps, reset, persistence), the
fact that a zoom actually changes what gets asked of the renderer rather than
stretching the bitmap it already has, and the two things easy to break by
accident: plain wheel scrolling, and the left page strip, which shares the
thumbnail helpers but must keep its own size.

Qt's real wheel never arrives under the offscreen platform, so the gesture is
delivered the way Qt would deliver it, straight into _DragList.wheelEvent with
a genuine QWheelEvent. Everything either side of that is the real widget.
"""

import pytest

from PySide6.QtCore import Qt, QPoint, QPointF, QSettings
from PySide6.QtGui import QPixmap, QWheelEvent
from PySide6.QtWidgets import QApplication

from ui import organizer as organizer_mod
from ui.organizer import PageOrganizer, ZOOM_STEPS, DEFAULT_ZOOM_INDEX, THUMB_W

# Settings the tests are allowed to scribble on, so a run never disturbs the
# zoom level Lucas actually chose in the app.
_TEST_ORG = "Lucas"
_TEST_APP = "Rapid PDF Tests"


class _FakeDoc:
    """Just enough document for the grid, plus a log of every render request so
    a test can see what width the thumbnails were actually asked for."""

    def __init__(self, count: int = 40):
        self._count = count
        self.doc = object()
        self.renders: list[tuple[int, int]] = []

    def page_count(self):
        return self._count

    def get_page_size(self, page_num):
        return (612.0, 792.0)

    def render_thumbnail(self, page_num, max_width=110):
        self.renders.append((page_num, max_width))
        pm = QPixmap(max_width, max(1, round(max_width * 792 / 612)))
        pm.fill(Qt.GlobalColor.white)
        return pm


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Point the remembered-zoom key at a throwaway store, and start empty."""
    monkeypatch.setattr(organizer_mod, "_SETTINGS_ORG", _TEST_ORG)
    monkeypatch.setattr(organizer_mod, "_SETTINGS_APP", _TEST_APP)
    QSettings(_TEST_ORG, _TEST_APP).clear()
    yield
    QSettings(_TEST_ORG, _TEST_APP).clear()


@pytest.fixture
def doc():
    return _FakeDoc()


@pytest.fixture
def org(qt_app, doc):
    o = PageOrganizer()
    o.resize(900, 700)
    o.set_document(doc)
    o.show()
    QApplication.processEvents()
    # The grid lays out on the next event-loop turn, which never comes in a
    # test; without this every rect below is measured against geometry the user
    # would never see.
    o._list.doItemsLayout()
    o._render_visible()
    yield o
    o.close()
    o.deleteLater()


def _ctrl_wheel(org, delta: int, pos: QPoint = None):
    _wheel(org, delta, Qt.KeyboardModifier.ControlModifier, pos)


def _wheel(org, delta: int, mods, pos: QPoint = None):
    if pos is None:
        pos = org._list.viewport().rect().center()
    event = QWheelEvent(
        QPointF(pos), QPointF(pos), QPoint(0, 0), QPoint(0, delta),
        Qt.MouseButton.NoButton, mods, Qt.ScrollPhase.NoScrollPhase, False,
    )
    org._list.wheelEvent(event)
    QApplication.processEvents()


def _visible_rows(org):
    vp = org._list.viewport().rect()
    return [i for i in range(org._list.count())
            if org._list.visualItemRect(org._list.item(i)).intersects(vp)]


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

def test_the_grid_opens_at_the_reference_size(org):
    assert org.zoom_index() == DEFAULT_ZOOM_INDEX
    assert org.zoom_factor() == 1.0
    assert org.thumb_width() == THUMB_W


def test_zooming_in_walks_up_the_ladder_and_stops_at_the_top(org):
    for _ in range(len(ZOOM_STEPS) + 5):
        org.zoom_in()
    assert org.zoom_index() == len(ZOOM_STEPS) - 1
    assert org.zoom_factor() == max(ZOOM_STEPS)


def test_zooming_out_walks_down_the_ladder_and_stops_at_the_bottom(org):
    for _ in range(len(ZOOM_STEPS) + 5):
        org.zoom_out()
    assert org.zoom_index() == 0
    assert org.zoom_factor() == min(ZOOM_STEPS)


def test_each_step_visits_the_next_rung_in_order(org):
    org.zoom_out()
    org.zoom_out()
    org.zoom_out()
    seen = [org.zoom_factor()]
    for _ in range(len(ZOOM_STEPS) - 1):
        org.zoom_in()
        seen.append(org.zoom_factor())
    assert seen == list(ZOOM_STEPS)


def test_reset_returns_to_the_default_from_either_direction(org):
    org.zoom_in()
    org.zoom_in()
    org.zoom_reset()
    assert org.zoom_index() == DEFAULT_ZOOM_INDEX
    org.zoom_out()
    org.zoom_out()
    org.zoom_reset()
    assert org.zoom_index() == DEFAULT_ZOOM_INDEX
    assert org.thumb_width() == THUMB_W


def test_a_zoom_past_either_end_changes_nothing(org):
    for _ in range(len(ZOOM_STEPS)):
        org.zoom_in()
    assert org._set_zoom_index(org.zoom_index() + 1) is False
    for _ in range(len(ZOOM_STEPS)):
        org.zoom_out()
    assert org._set_zoom_index(org.zoom_index() - 1) is False


# ---------------------------------------------------------------------------
# The size has to reach the renderer
# ---------------------------------------------------------------------------

def test_zooming_in_re_renders_the_thumbnails_bigger(org, doc):
    doc.renders.clear()
    org.zoom_in()
    assert doc.renders, "a zoom must re-rasterise, not stretch the old bitmap"
    widths = {w for _, w in doc.renders}
    assert widths == {org.thumb_width()}
    assert org.thumb_width() > THUMB_W


def test_zooming_out_re_renders_the_thumbnails_smaller(org, doc):
    doc.renders.clear()
    org.zoom_out()
    assert {w for _, w in doc.renders} == {org.thumb_width()}
    assert org.thumb_width() < THUMB_W


def test_the_cells_grow_with_the_thumbnails(org):
    before = org._list.item(0).sizeHint()
    org.zoom_in()
    after = org._list.item(0).sizeHint()
    assert after.width() > before.width()
    assert after.height() > before.height()
    # Every cell moves together, and the view's grid moves with them.
    assert org._list.item(org._list.count() - 1).sizeHint() == after
    assert org._list.gridSize() == after


def test_a_zoom_only_pays_for_what_is_on_screen(qt_app, doc):
    """The whole point of the lazy renderer: a zoom on a long document must not
    re-rasterise all 200 pages."""
    doc = _FakeDoc(200)
    o = PageOrganizer()
    o.resize(900, 700)
    o.set_document(doc)
    o.show()
    QApplication.processEvents()
    o._list.doItemsLayout()
    o._render_visible()
    try:
        doc.renders.clear()
        o.zoom_in()
        assert len(doc.renders) < 60, (
            f"{len(doc.renders)} of 200 pages re-rendered for one zoom step")
        assert doc.renders, "nothing rendered at all"
    finally:
        o.close()
        o.deleteLater()


# ---------------------------------------------------------------------------
# Wheel and scroll position
# ---------------------------------------------------------------------------

def test_ctrl_wheel_up_zooms_in_and_down_zooms_out(org):
    _ctrl_wheel(org, 120)
    assert org.zoom_index() == DEFAULT_ZOOM_INDEX + 1
    _ctrl_wheel(org, -120)
    _ctrl_wheel(org, -120)
    assert org.zoom_index() == DEFAULT_ZOOM_INDEX - 1


def test_a_part_notch_does_not_zoom_until_it_adds_up(org):
    """A high-resolution wheel sends many small deltas; one step per notch of
    travel, not one per event."""
    for _ in range(3):
        _ctrl_wheel(org, 30)
    assert org.zoom_index() == DEFAULT_ZOOM_INDEX
    _ctrl_wheel(org, 30)
    assert org.zoom_index() == DEFAULT_ZOOM_INDEX + 1


def test_a_plain_wheel_still_scrolls_and_never_zooms(org):
    bar = org._list.verticalScrollBar()
    assert bar.maximum() > 0, "need a scrollable grid for this to mean anything"
    before = bar.value()
    _wheel(org, -120, Qt.KeyboardModifier.NoModifier)
    assert bar.value() > before
    assert org.zoom_index() == DEFAULT_ZOOM_INDEX


def test_a_wheel_zoom_keeps_the_page_under_the_cursor_in_place(org):
    bar = org._list.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    QApplication.processEvents()
    rows = _visible_rows(org)
    anchor = rows[len(rows) // 2]
    rect = org._list.visualItemRect(org._list.item(anchor))
    top_before = rect.top()
    _ctrl_wheel(org, 120, rect.center())
    assert org.zoom_index() == DEFAULT_ZOOM_INDEX + 1
    top_after = org._list.visualItemRect(org._list.item(anchor)).top()
    assert abs(top_after - top_before) <= 2


def test_a_keyboard_zoom_does_not_jump_back_to_the_top(org):
    bar = org._list.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    QApplication.processEvents()
    anchor = _visible_rows(org)[0]
    org.zoom_in()
    assert bar.value() > 0, "zooming threw the view back to page 1"
    assert anchor in _visible_rows(org)


# ---------------------------------------------------------------------------
# Remembering it, and leaving the page strip alone
# ---------------------------------------------------------------------------

def test_the_zoom_level_is_remembered_for_the_next_run(qt_app, org):
    org.zoom_in()
    org.zoom_in()
    chosen = org.zoom_index()
    assert QSettings(_TEST_ORG, _TEST_APP).value(
        organizer_mod._ZOOM_SETTING, type=int) == chosen
    fresh = PageOrganizer()
    try:
        assert fresh.zoom_index() == chosen
        assert fresh.thumb_width() == org.thumb_width()
    finally:
        fresh.deleteLater()


def test_a_junk_setting_falls_back_to_the_default(qt_app):
    QSettings(_TEST_ORG, _TEST_APP).setValue(organizer_mod._ZOOM_SETTING, "banana")
    assert PageOrganizer._load_zoom_index() == DEFAULT_ZOOM_INDEX
    QSettings(_TEST_ORG, _TEST_APP).setValue(organizer_mod._ZOOM_SETTING, 999)
    assert PageOrganizer._load_zoom_index() == len(ZOOM_STEPS) - 1


def test_zooming_the_organizer_leaves_the_page_strip_alone(qt_app, org):
    """The two widgets share ui/thumbnails helpers but not their sizes. If the
    strip ever starts following the Organizer's zoom, that is a bug."""
    from ui.page_panel import PagePanel
    import ui.page_panel as page_panel_mod

    panel = PagePanel()
    panel.resize(150, 700)
    panel.set_document(_FakeDoc(8))
    panel.show()
    QApplication.processEvents()
    panel._apply_layout()
    try:
        before = panel._list.iconSize()
        for _ in range(len(ZOOM_STEPS)):
            org.zoom_in()
        assert panel._list.iconSize() == before
        assert page_panel_mod.THUMB_W == 100
    finally:
        panel.close()
        panel.deleteLater()
