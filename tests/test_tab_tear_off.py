"""Dragging a tab out of a window, and the MRU Ctrl+Tab order. Phase 4.

HOW A DRAG IS TESTED WITHOUT A DRAG LOOP. A real Qt mouse grab across two
top-level windows cannot run in a headless suite: there is no pointer to grab
and no loop to deliver the moves. So the gesture is driven the way the repo
already drives the canvas (see tests/test_canvas_undo.py): synthesised
QMouseEvents are handed straight to `mousePressEvent` / `mouseMoveEvent` /
`mouseReleaseEvent` with the global positions a real pointer would have carried,
and what is asserted is the resulting WINDOW AND REGISTRY STATE.

That is a thinner thing to test than it sounds, and deliberately so. Phase 3 put
the whole move behind `MainWindow.move_view_to_window` / `move_view_to_new_window`
and pinned it in tests/test_multi_window.py. Phase 4 adds when to call them and
with what, so these tests are about the threshold, the hit-testing and the
bookkeeping, not about whether a reparent keeps a live document alive.

WHAT OFFSCREEN DOES AND DOES NOT GIVE US. Geometry is real: `move`, `resize`,
`mapToGlobal`, `mapFromGlobal` and `tabRect` all agree with each other, which is
what makes a hit-test assertable. `move()` positions the FRAME and the widget
origin sits inside it, which is why every position below is built by mapping
through a widget rather than by adding numbers to a window position. What
offscreen will not do is activation, the event loop, or a real mouse grab, so
nothing here asserts on focus and `grabMouse` is exercised only in the sense
that the code path runs it and gives it back.

SECTIONS.

1. THE THRESHOLD. The whole risk of this phase in one place: a sloppy reorder
   must not become a tear.
2. TEARING OFF. Crossing makes the window, the document arrives intact.
3. DOCKING. Dropping on another window's bar, at the index under the cursor.
4. ESCAPE. Back to the window and the index it came from.
5. THE SINGLE-TAB CASE. One tab drags its own window, and never spawns an
   empty one.
6. THE MRU. A visit history for tabs, frozen while the walk is in flight.
"""

import fitz
import pytest

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from core.settings import Settings, set_settings
from ui.main_window import MainWindow
from ui.tab_tear_off import DETACH_MARGIN, insertion_index
from ui.window_registry import WindowRegistry


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    s = Settings(tmp_path / "settings.json", debounce_ms=0, migrate_legacy=False)
    # A window emptied by a drop closes itself, and "N documents are open"
    # would be in the way of every test here. Phase 2 pins that prompt.
    s.close.confirm_multiple_tabs = False
    previous = set_settings(s)
    yield s
    set_settings(previous)


@pytest.fixture(autouse=True)
def never_opens_a_dialog(monkeypatch):
    """Offscreen still runs a real modal loop, so an unexpected message box
    hangs the suite instead of failing it."""
    for name in ("question", "warning", "critical", "information", "about"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, n=name, **k: pytest.fail(
                f"QMessageBox.{n} opened: {a[1:3]}")))


@pytest.fixture
def registry():
    return WindowRegistry.instance()


def _pdf(tmp_path, name, pages=1):
    path = tmp_path / name
    raw = fitz.open()
    for i in range(pages):
        page = raw.new_page(width=400, height=500)
        page.insert_text((20, 100), f"{name} p{i}", fontsize=24)
    raw.save(str(path))
    raw.close()
    return str(path)


def _window(registry, tmp_path, names, at=(100, 100), size=(1200, 800)):
    """A shown window holding one tab per name, positioned somewhere real."""
    window = registry.create_window(show=False)
    window.resize(*size)
    window.move(*at)
    window.show()
    window.open_paths([_pdf(tmp_path, n) for n in names])
    return window


# ----------------------------------------------------------------------
# Driving the gesture
# ----------------------------------------------------------------------

def _mouse(kind, bar, global_pos, button=Qt.MouseButton.LeftButton):
    """One QMouseEvent aimed at a global point, as the pointer would carry it.

    The local position is derived by mapping, never assumed: the bar sits
    inside a header inside a layout, and a hand-built local position would be
    testing arithmetic rather than the widget.
    """
    held = (Qt.MouseButton.NoButton
            if kind == QMouseEvent.Type.MouseButtonRelease else button)
    local = QPointF(bar.mapFromGlobal(global_pos))
    return QMouseEvent(kind, local, QPointF(global_pos), button, held,
                       Qt.KeyboardModifier.NoModifier)


def _tab_point(bar, index, dx=10, dy=None):
    """A global position `dx` into tab `index`, vertically centred by default."""
    rect = bar.tabRect(index)
    if dy is None:
        dy = rect.height() // 2
    return bar.mapToGlobal(rect.topLeft() + QPoint(dx, dy))


def _press(bar, global_pos):
    bar.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, bar, global_pos))


def _move(bar, global_pos):
    bar.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, bar, global_pos))


def _release(bar, global_pos):
    bar.mouseReleaseEvent(
        _mouse(QMouseEvent.Type.MouseButtonRelease, bar, global_pos))


def _escape(bar):
    bar.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                Qt.KeyboardModifier.NoModifier))


def _below_bar(bar, global_pos, extra=DETACH_MARGIN + 20):
    """The same x, far enough below the bar to be a tear rather than a reorder."""
    return QPoint(global_pos.x(), bar.mapToGlobal(
        QPoint(0, bar.rect().bottom())).y() + extra)


# ======================================================================
# 1. The threshold
# ======================================================================

def test_sideways_drag_is_a_reorder_and_never_a_tear(qt_app, store, registry, tmp_path):
    """The whole risk of this phase. Dragging a tab along the bar, however far,
    stays a reorder: overshooting the last tab by half a screen is something
    people do every time they reorder."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    bar = window.document_area().bar()
    start = _tab_point(bar, 0)

    _press(bar, start)
    for dx in (40, 200, 900, 2000):
        _move(bar, start + QPoint(dx, 0))
        assert not bar.tear_off().is_dragging()
    _release(bar, start + QPoint(2000, 0))

    assert registry.count() == 1
    assert window.document_area().count() == 3
    window.document_area().check_invariant()


def test_a_small_vertical_wobble_is_not_a_tear(qt_app, store, registry, tmp_path):
    """DETACH_MARGIN is what separates a shaky hand from an intention."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"])
    bar = window.document_area().bar()
    start = _tab_point(bar, 0)

    _press(bar, start)
    _move(bar, _below_bar(bar, start, extra=DETACH_MARGIN - 6))
    assert not bar.tear_off().is_dragging()
    _release(bar, start)
    assert registry.count() == 1


def test_a_press_on_empty_bar_space_arms_nothing(qt_app, store, registry, tmp_path):
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"])
    bar = window.document_area().bar()
    empty = bar.mapToGlobal(QPoint(bar.width() - 4, bar.height() // 2))

    _press(bar, empty)
    _move(bar, _below_bar(bar, empty))
    assert not bar.tear_off().is_dragging()
    _release(bar, empty)
    assert registry.count() == 1


# ======================================================================
# 2. Tearing off
# ======================================================================

def test_crossing_the_threshold_creates_a_window_with_the_document_intact(
        qt_app, store, registry, tmp_path):
    """The window is made ON THE CROSSING, not on the drop, which is what makes
    the drop a no-op. The document has to survive the reparent."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    bar = area.bar()
    moving = area.view_at(1)
    canvas = moving._canvas
    scene, doc = canvas.scene(), moving._doc.doc

    start = _tab_point(bar, 1)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))

    assert bar.tear_off().is_dragging()
    torn = bar.tear_off().floating_window()
    assert torn is not None and torn is not window
    assert registry.count() == 2
    assert torn.document_area().count() == 1
    assert torn.document_area().view_at(0) is moving
    assert area.count() == 2
    area.check_invariant()
    torn.document_area().check_invariant()

    # Finding 2, re-verified through the gesture rather than the menu item.
    assert canvas.scene() is scene
    # The stack is the WINDOW's since phase 5, so a torn-off view joins the
    # torn window's history rather than carrying its own across (ui/undo.py).
    assert canvas.undo_stack is torn.undo_stack()
    assert moving._doc.doc is doc
    assert canvas.internalWinId() == 0
    assert moving.window() is torn

    _release(bar, _below_bar(bar, start))
    assert registry.count() == 2
    assert not bar.tear_off().is_dragging()


def test_the_torn_window_follows_the_cursor(qt_app, store, registry, tmp_path):
    """The real window moves, which is the reason this is not a QDrag. The grab
    point stays under the pointer, so the offset between them is constant."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"])
    bar = window.document_area().bar()
    start = _tab_point(bar, 0)

    _press(bar, start)
    first = _below_bar(bar, start)
    _move(bar, first)
    torn = bar.tear_off().floating_window()
    offset = first - torn.frameGeometry().topLeft()

    _move(bar, first + QPoint(300, 220))
    assert (first + QPoint(300, 220)) - torn.frameGeometry().topLeft() == offset
    _release(bar, first + QPoint(300, 220))


def test_dropping_on_empty_desktop_leaves_the_window_where_it_was_let_go(
        qt_app, store, registry, tmp_path):
    """No target is not a failure. The window is already exactly where they
    dropped it, which is what creating it up front buys."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    bar = window.document_area().bar()
    start = _tab_point(bar, 0)

    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    torn = bar.tear_off().floating_window()
    far = QPoint(3000, 2000)          # nowhere near any window
    _move(bar, far)
    assert bar.tear_off().drop_target() is None
    where = torn.frameGeometry().topLeft()
    _release(bar, far)

    assert registry.count() == 2
    assert torn.frameGeometry().topLeft() == where
    assert torn.document_area().count() == 1


# ======================================================================
# 3. Docking into another window
# ======================================================================

def test_dropping_on_another_bar_docks_at_the_index_under_the_cursor(
        qt_app, store, registry, tmp_path):
    """The insertion index is the point of the hit-test. Dropping on the left
    half of tab 1 puts the arriving document at 1, not at the end."""
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf", "z.pdf"], at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()
    moving = source.document_area().view_at(0)
    name = moving.document_path()

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    # The left third of the other window's second tab: before it, not after.
    over = _tab_point(other_bar, 1, dx=other_bar.tabRect(1).width() // 4)
    _move(bar, over)

    target = bar.tear_off().drop_target()
    assert target is not None
    assert target[0] is other and target[1] == 1
    assert other_bar.drop_indicator() is not None

    _release(bar, over)

    assert other.document_area().count() == 4
    assert other.document_area().view_at(1) is moving
    assert other.document_area().view_at(1).document_path() == name
    assert other_bar.drop_indicator() is None
    other.document_area().check_invariant()
    # The window that was created on the crossing is emptied and closes itself.
    assert registry.count() == 2
    assert source.document_area().count() == 1


def test_dropping_past_the_last_tab_appends(qt_app, store, registry, tmp_path):
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf"], at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()
    moving = source.document_area().view_at(0)

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    empty = other_bar.mapToGlobal(
        QPoint(other_bar.width() - 4, other_bar.height() // 2))
    _move(bar, empty)
    assert bar.tear_off().drop_target() == (other, 2)
    _release(bar, empty)

    assert other.document_area().count() == 3
    assert other.document_area().view_at(2) is moving


def test_the_whole_target_window_is_a_dock_zone(qt_app, store, registry,
                                                tmp_path):
    """Over the body of another window, nowhere near its tab strip.

    The dock zone used to be the target's bar plus twelve pixels either side,
    which is a strip about 46 px tall on a window that the floating one was
    sitting on top of. Missing it did not fail loudly: the document became a
    second window on the desktop, and nothing had said why. The whole window is
    the zone now, and only the INDEX still depends on aiming.
    """
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf"], at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()
    moving = source.document_area().view_at(0)

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    # The middle of the other window's PAGE area, hundreds of pixels below its
    # tab bar.
    body = other.mapToGlobal(QPoint(other.width() // 2, other.height() // 2))
    _move(bar, body)

    target = bar.tear_off().drop_target()
    assert target is not None and target[0] is other
    # No aim, so no claim about where: it goes on the end.
    assert target[1] == other_bar.count()
    # And the strip says so, which the 2px line alone never did.
    assert other_bar.drop_active()

    _release(bar, body)

    assert other.document_area().count() == 3
    assert other.document_area().view_at(2) is moving
    assert not other_bar.drop_active()
    assert other_bar.drop_indicator() is None
    other.document_area().check_invariant()


def test_the_window_it_came_from_keeps_the_narrow_band(qt_app, store, registry,
                                                       tmp_path):
    """The one window the widened zone must NOT apply to.

    Tearing a tab off is dragging it DOWN out of the bar, and down out of the
    bar is still inside the window it came from. Give that window a body-sized
    dock zone and the tab re-docks the instant it leaves the bar, which is to
    say the tear-off stops working at all.
    """
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    bar = source.document_area().bar()

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    body = source.mapToGlobal(QPoint(source.width() // 2,
                                     source.height() // 2))
    _move(bar, body)

    assert bar.tear_off().drop_target() is None
    _release(bar, body)
    assert registry.count() == 2


def test_going_back_up_to_the_source_bar_still_re_docks(qt_app, store, registry,
                                                        tmp_path):
    """Changing your mind mid-tear: the source's own bar is still a target, so
    the narrow band above is a rule about the BODY, not about the window."""
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    bar = source.document_area().bar()
    moving = source.document_area().view_at(0)

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    back = _tab_point(bar, 0)
    _move(bar, back)

    target = bar.tear_off().drop_target()
    assert target is not None and target[0] is source
    _release(bar, back)

    assert registry.count() == 1
    assert source.document_area().count() == 2
    assert moving.window() is source


def test_the_floating_window_is_held_clear_of_the_drop_feedback(
        qt_app, store, registry, tmp_path):
    """It used to sit exactly under the cursor, covering the strip the insertion
    line and the highlight are painted on. It is held below the cursor now."""
    from ui.tab_tear_off import DROP_CLEARANCE

    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf"], at=(2000, 100))
    bar = source.document_area().bar()

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    over = _tab_point(other.document_area().bar(), 0)
    _move(bar, over)

    torn = bar.tear_off().floating_window()
    assert torn.frameGeometry().top() > over.y()
    assert torn.frameGeometry().top() - over.y() >= DROP_CLEARANCE - \
        torn.document_area().header().height()
    _release(bar, over)


def test_the_floating_window_is_never_its_own_drop_target(
        qt_app, store, registry, tmp_path):
    """`QApplication.widgetAt()` would answer with the window being dragged
    every single time, which is why the hit-test walks the registry instead."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"])
    bar = window.document_area().bar()
    start = _tab_point(bar, 0)

    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    torn = bar.tear_off().floating_window()
    # Straight onto the floating window's own tab bar.
    onto_itself = _tab_point(torn.document_area().bar(), 0)
    _move(bar, onto_itself)
    assert bar.tear_off().drop_target() is None
    _release(bar, onto_itself)
    assert registry.count() == 2


def test_a_minimised_window_is_not_a_drop_target(qt_app, store, registry, tmp_path):
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf"], at=(2000, 100))
    other_bar = other.document_area().bar()
    over = _tab_point(other_bar, 0)
    other.showMinimized()

    bar = source.document_area().bar()
    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    _move(bar, over)
    assert bar.tear_off().drop_target() is None
    _release(bar, over)
    assert other.document_area().count() == 1


def test_insertion_index_picks_the_half_the_cursor_is_in(qt_app, store, registry,
                                                         tmp_path):
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    bar = window.document_area().bar()
    rect = bar.tabRect(1)
    assert insertion_index(bar, rect.topLeft() + QPoint(4, 4)) == 1
    assert insertion_index(bar, rect.center() + QPoint(rect.width() // 4, 0)) == 2
    assert insertion_index(bar, QPoint(bar.width() - 2, 4)) == bar.count()


# ======================================================================
# 4. Escape
# ======================================================================

def test_escape_re_docks_at_the_original_index(qt_app, store, registry, tmp_path):
    """Back where it came from, at the number it came from, not appended."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    bar = area.bar()
    moving = area.view_at(1)

    start = _tab_point(bar, 1)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    assert registry.count() == 2

    _escape(bar)

    assert not bar.tear_off().is_dragging()
    assert area.count() == 3
    assert area.index_of(moving) == 1
    assert moving.window() is window
    assert registry.count() == 1
    area.check_invariant()


def test_escape_before_the_threshold_does_nothing(qt_app, store, registry, tmp_path):
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"])
    bar = window.document_area().bar()
    _press(bar, _tab_point(bar, 0))
    _escape(bar)
    assert window.document_area().count() == 2
    assert registry.count() == 1


# ======================================================================
# 5. The single-tab case
# ======================================================================

def test_a_lone_tab_drags_its_own_window_and_spawns_nothing(
        qt_app, store, registry, tmp_path):
    """Without this you tear the only document out, close the window behind it,
    and hand back the window you started with minus its size and position."""
    window = _window(registry, tmp_path, ["a.pdf"], at=(100, 100))
    area = window.document_area()
    bar = area.bar()
    assert area.count() == 1

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))

    assert bar.tear_off().is_dragging()
    assert bar.tear_off().floating_window() is window
    assert registry.count() == 1

    _move(bar, _below_bar(bar, start) + QPoint(400, 300))
    _release(bar, _below_bar(bar, start) + QPoint(400, 300))

    assert registry.count() == 1
    assert area.count() == 1
    area.check_invariant()


def test_a_lone_tab_dropped_on_another_bar_empties_its_window(
        qt_app, store, registry, tmp_path):
    """The one case where the single-tab drag still moves a document: the
    source is emptied and closes, which is right because it is going away and
    not being handed back."""
    source = _window(registry, tmp_path, ["a.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf"], at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()
    moving = source.document_area().view_at(0)

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    over = _tab_point(other_bar, 0, dx=4)
    _move(bar, over)
    assert bar.tear_off().drop_target() == (other, 0)
    _release(bar, over)

    assert other.document_area().count() == 3
    assert other.document_area().view_at(0) is moving
    assert registry.count() == 1
    other.document_area().check_invariant()


def test_escape_on_a_lone_tab_puts_the_window_back(qt_app, store, registry,
                                                   tmp_path):
    window = _window(registry, tmp_path, ["a.pdf"], at=(100, 100))
    bar = window.document_area().bar()
    home = window.frameGeometry().topLeft()

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start) + QPoint(500, 400))
    assert window.frameGeometry().topLeft() != home

    _escape(bar)

    assert window.frameGeometry().topLeft() == home
    assert window.document_area().count() == 1
    assert registry.count() == 1


def test_dropping_into_an_empty_window_replaces_its_placeholder_tab(
        qt_app, store, registry, tmp_path):
    """A window with one empty tab hides its bar, so the drop zone is the strip
    where the bar would be. MainWindow.adopt does the replacing."""
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    empty = registry.create_window(show=False)
    empty.resize(1200, 800)
    empty.move(2000, 100)
    empty.show()
    assert not empty.document_area().bar().isVisible()

    bar = source.document_area().bar()
    moving = source.document_area().view_at(0)
    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    over = empty.document_area().mapToGlobal(QPoint(40, 8))
    _move(bar, over)
    assert bar.tear_off().drop_target() == (empty, 0)
    _release(bar, over)

    assert empty.document_area().count() == 1
    assert empty.document_area().view_at(0) is moving
    empty.document_area().check_invariant()


# ======================================================================
# 6. The MRU order behind Ctrl+Tab
# ======================================================================

def test_mru_is_visit_order_not_tab_order(qt_app, store, registry, tmp_path):
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    a, b, c = area.view_at(0), area.view_at(1), area.view_at(2)

    area.set_current_index(0)
    area.set_current_index(2)
    area.set_current_index(1)
    assert area.mru_order() == [b, c, a]


def test_ctrl_tab_goes_to_the_tab_you_were_just_in(qt_app, store, registry,
                                                   tmp_path):
    """Not the one to the right. That is Ctrl+PgDn and it is a different key."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    a, b, c = area.view_at(0), area.view_at(1), area.view_at(2)

    area.set_current_index(0)       # a
    area.set_current_index(2)       # c, having come from a
    window.next_recent_tab()
    assert area.current_view() is a

    window._end_mru_walk()
    assert area.mru_order()[0] is a


def test_holding_ctrl_walks_back_through_the_stack(qt_app, store, registry,
                                                   tmp_path):
    """The list is FROZEN while Ctrl is down. Without the freeze the second tap
    would come straight back to where the first one started."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    a, b, c = area.view_at(0), area.view_at(1), area.view_at(2)

    area.set_current_index(0)       # a
    area.set_current_index(1)       # b
    area.set_current_index(2)       # c
    assert area.mru_order() == [c, b, a]

    window.next_recent_tab()
    assert area.current_view() is b
    assert area.is_walking_mru()
    window.next_recent_tab()
    assert area.current_view() is a
    window.next_recent_tab()
    assert area.current_view() is c     # wraps

    window._end_mru_walk()
    assert not area.is_walking_mru()


def test_releasing_ctrl_commits_the_landing_tab(qt_app, store, registry, tmp_path):
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    a, b, c = area.view_at(0), area.view_at(1), area.view_at(2)

    area.set_current_index(0)
    area.set_current_index(1)
    area.set_current_index(2)
    window.next_recent_tab()
    window.next_recent_tab()
    assert area.current_view() is a

    # The release the application filter is watching for.
    window.eventFilter(window, QKeyEvent(
        QEvent.Type.KeyRelease, Qt.Key.Key_Control,
        Qt.KeyboardModifier.NoModifier))

    assert not area.is_walking_mru()
    assert area.mru_order() == [a, c, b]


def test_ctrl_shift_tab_walks_the_other_way(qt_app, store, registry, tmp_path):
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    a, b, c = area.view_at(0), area.view_at(1), area.view_at(2)

    area.set_current_index(0)
    area.set_current_index(1)
    area.set_current_index(2)       # order is [c, b, a]
    window.previous_recent_tab()
    assert area.current_view() is a


def test_ctrl_pgdn_stays_positional(qt_app, store, registry, tmp_path):
    """The two orders must not be conflated. Ctrl+PgDn is the tab to the right
    whatever the visit history says."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    a, b, c = area.view_at(0), area.view_at(1), area.view_at(2)

    area.set_current_index(2)
    area.set_current_index(0)       # MRU says a then c then b
    window.next_tab()
    assert area.current_view() is b


def test_a_closed_tab_leaves_the_mru(qt_app, store, registry, tmp_path):
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    a, b, c = area.view_at(0), area.view_at(1), area.view_at(2)

    area.set_current_index(1)
    area.set_current_index(2)
    for view in area.views():
        view.mark_clean()
    area.remove_view(area.index_of(b))

    assert b not in area.mru_order()
    assert len(area.mru_order()) == 2
    area.check_invariant()


def test_a_torn_off_tab_leaves_the_source_windows_mru(qt_app, store, registry,
                                                      tmp_path):
    """`detach` is the source half of a move, so the history has to let go of
    the view even though nothing was destroyed."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    bar = area.bar()
    moving = area.view_at(1)
    area.set_current_index(1)

    start = _tab_point(bar, 1)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    _release(bar, _below_bar(bar, start))

    assert moving not in area.mru_order()
    assert len(area.mru_order()) == 2


def test_one_tab_has_no_mru_walk(qt_app, store, registry, tmp_path):
    window = _window(registry, tmp_path, ["a.pdf"])
    area = window.document_area()
    window.next_recent_tab()
    assert not area.is_walking_mru()
    assert window._mru_filter_on is False
