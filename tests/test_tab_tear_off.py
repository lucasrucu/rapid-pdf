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
7. THE ZONES AND THE GHOST SLOT. Getting a tab in is a large target and
   getting one out is a small one, and the gap the tabs part into is what
   says where it will land.
"""

import fitz
import pytest

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QTabBar

from core.settings import Settings, set_settings
from ui.main_window import MainWindow
from ui.tab_tear_off import (
    DETACH_MARGIN, DOCK_MARGIN, INCOMING_SLACK, REDOCK_MARGIN, insertion_index,
    tab_pixmap,
)
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


def _bar_bottom(window) -> int:
    """The global y of the bottom edge of `window`'s tab bar."""
    bar = window.document_area().bar()
    return bar.mapToGlobal(QPoint(0, bar.rect().bottom())).y()


def _in_zone(window, dx, below):
    """A global point `dx` in from the left edge of `window` and `below`
    pixels under the bottom of its tab bar.

    Built off the WINDOW's own left edge rather than off the bar's, because
    what is being tested is that the zone is the window's full width and the
    bar's is a fraction of it.
    """
    return QPoint(window.frameGeometry().left() + dx, _bar_bottom(window) + below)


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

def test_crossing_the_threshold_creates_nothing_and_the_drop_creates_the_window(
        qt_app, store, registry, tmp_path):
    """The window is made ON THE DROP now, not on the crossing.

    Mid-drag NOTHING has moved: the document is still in the window it came
    from, still in its tab, and the only new object on screen is a ghost. That
    is what removes the destroy-inside-the-release-handler crash path. The
    document still has to survive the reparent, so every check that used to run
    mid-drag runs after the drop instead.
    """
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    bar = area.bar()
    moving = area.view_at(1)
    canvas = moving._canvas
    scene, doc = canvas.scene(), moving._doc.doc

    start = _tab_point(bar, 1)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))

    # Mid-drag: a ghost, and not one thing else.
    assert bar.tear_off().is_dragging()
    assert bar.tear_off().ghost() is not None
    assert bar.tear_off().floating_window() is None
    assert registry.count() == 1
    assert area.count() == 3
    assert moving.window() is window
    area.check_invariant()

    far = QPoint(3000, 2000)          # nowhere near any window
    _move(bar, far)
    _release(bar, far)

    assert not bar.tear_off().is_dragging()
    assert bar.tear_off().ghost() is None
    assert registry.count() == 2
    assert area.count() == 2
    torn = moving.window()
    assert torn is not window
    assert torn.document_area().count() == 1
    assert torn.document_area().view_at(0) is moving
    area.check_invariant()
    torn.document_area().check_invariant()

    # The document survived the reparent. Same scene, same fitz doc, no native
    # handle, and the undo stack is the WINDOW's, so it joins the torn one.
    assert canvas.scene() is scene
    assert canvas.undo_stack is torn.undo_stack()
    assert moving._doc.doc is doc
    assert canvas.internalWinId() == 0


def test_the_ghost_follows_the_cursor_with_no_clearance(
        qt_app, store, registry, tmp_path):
    """The grab point stays under the pointer, so the offset is constant AND
    it is the grab offset alone.

    The old floating window was held 46 px below the cursor so it would not
    cover the strip its own drop feedback was painted on. The ghost needs no
    such dodge: it is tab-sized and the OS hit test passes through it. So the
    thing being dragged is finally where the pointer is, which is the first of
    the four symptoms this rewrite was for.
    """
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"])
    bar = window.document_area().bar()
    tear = bar.tear_off()
    start = _tab_point(bar, 0)

    _press(bar, start)
    first = _below_bar(bar, start)
    _move(bar, first)
    ghost = tear.ghost()
    assert ghost is not None
    offset = first - tear.ghost_position(first)

    moved = first + QPoint(300, 220)
    _move(bar, moved)
    assert moved - tear.ghost_position(moved) == offset

    # No downward clearance: the ghost sits at the cursor less the grab point,
    # and nothing else is added to it.
    assert tear.ghost_position(moved).y() <= moved.y()
    _release(bar, moved)


def test_dropping_on_empty_desktop_makes_the_window_where_it_was_let_go(
        qt_app, store, registry, tmp_path):
    """No target means make one, at the point the button came up.

    The old design got this for free by creating the window on the crossing.
    The new one has to place it deliberately, and the guarantee the user cares
    about is unchanged: the window appears where they let go, not at a corner.
    """
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    bar = window.document_area().bar()
    tear = bar.tear_off()
    moving = window.document_area().view_at(0)
    start = _tab_point(bar, 0)

    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    far = QPoint(3000, 2000)          # nowhere near any window
    _move(bar, far)
    assert tear.drop_target() is None
    expected = tear.ghost_position(far)
    _release(bar, far)

    assert registry.count() == 2
    torn = moving.window()
    assert torn is not window
    assert torn.document_area().count() == 1
    assert torn.pos() == expected


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

    # IT IS ALREADY THERE, before the button comes up. That is the change: an
    # insertion line promising where it would go has been replaced by the tab
    # actually going there, and the strip reflowing around it.
    assert other.document_area().count() == 4
    assert other.document_area().view_at(1) is moving
    assert source.document_area().count() == 1

    _release(bar, over)

    assert other.document_area().count() == 4
    assert other.document_area().view_at(1) is moving
    assert other.document_area().view_at(1).document_path() == name
    other.document_area().check_invariant()
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
    # No aim, so no claim about where: it went on the end. The index was
    # computed against the bar BEFORE the tab joined it, so it is one less than
    # the count now, which is the arithmetic of a live insert.
    assert target[1] == other_bar.count() - 1
    # And it is already in, which is what the accent wash used to be promising.
    assert other.document_area().count() == 3
    assert other.document_area().view_at(2) is moving

    _release(bar, body)

    assert other.document_area().count() == 3
    assert other.document_area().view_at(2) is moving
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


def test_the_ghost_cannot_hide_the_landing_spot(
        qt_app, store, registry, tmp_path):
    """Two guarantees, and the second one is the stronger.

    It is one tab wide and one tab tall, which is what removed the 46 px
    downward dodge the old floating WINDOW needed so as not to cover the strip
    its own feedback was painted on.

    And it is not on screen at all while the cursor is anywhere in the tab zone
    of the window holding the tab. That is what answers "it almost even blocks
    the view of the highlithed bar and where it will fall": the parting tabs
    and the ghost slot only ever happen inside that zone, and the picture under
    the cursor is gone for the whole of it.
    """
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf"], at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()

    start = _tab_point(bar, 0)
    _press(bar, start)
    mid_air = _below_bar(bar, start)
    _move(bar, mid_air)

    ghost = bar.tear_off().ghost()
    assert ghost is not None
    assert ghost.height() <= bar.tabRect(0).height() + 2
    assert ghost.width() <= bar.tabRect(0).width() + 2

    over = _tab_point(other_bar, 0)
    _move(bar, over)
    assert bar.tear_off().drop_target() is not None
    assert bar.tear_off().ghost() is None
    _release(bar, over)


def test_the_ghost_is_never_a_drop_target(qt_app, store, registry, tmp_path):
    """The ghost is not in the registry and is transparent to the OS hit test.

    This is what makes cursor-based targeting possible at all. The old
    objection to `QApplication.topLevelAt` was that it answered with the window
    being dragged every time; a ghost carrying WindowTransparentForInput is
    invisible to it, so the answer is the window underneath.
    """
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"])
    bar = window.document_area().bar()
    tear = bar.tear_off()
    start = _tab_point(bar, 0)

    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    ghost = tear.ghost()
    assert ghost is not None
    assert ghost.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert bool(ghost.windowFlags() & Qt.WindowType.WindowTransparentForInput)
    assert ghost not in list(registry.windows())

    far = QPoint(3000, 2000)
    _move(bar, far)
    assert tear.drop_target() is None
    _release(bar, far)
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

def test_escape_leaves_everything_exactly_where_it_was(
        qt_app, store, registry, tmp_path):
    """There is nothing to undo, and that is the point.

    Escape used to reverse a window that had already been created and a view
    that had already been reparented. Nothing leaves its window until the drop
    now, so a cancel is the ghost going away. The observable guarantee is the
    same and strictly harder: not "put back at index 1" but "never moved".
    """
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    area = window.document_area()
    bar = area.bar()
    moving = area.view_at(1)

    start = _tab_point(bar, 1)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    assert registry.count() == 1          # nothing was created
    assert area.index_of(moving) == 1     # and nothing moved

    _escape(bar)

    assert not bar.tear_off().is_dragging()
    assert bar.tear_off().ghost() is None
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

    # A lone tab drags the WINDOW, live, and makes no ghost. There is no second
    # window to preview, so a picture of a tab floating over the window it never
    # left would be a lie about what is happening.
    assert bar.tear_off().is_dragging()
    assert bar.tear_off().ghost() is None
    assert registry.count() == 1

    drop = _below_bar(bar, start) + QPoint(400, 300)
    _move(bar, drop)
    expected = bar.tear_off().ghost_position(drop)
    # It has ALREADY moved, before the button comes up.
    assert window.pos() == expected
    _release(bar, drop)

    assert registry.count() == 1
    assert area.count() == 1
    assert window.pos() == expected
    area.check_invariant()


def test_a_lone_tab_dropped_on_another_bar_empties_its_window(
        qt_app, store, registry, tmp_path):
    """The one case where the single-tab drag still moves a document: the
    source is emptied and closes, which is right because it is going away and
    not being handed back.

    The close is DEFERRED by one pass of the event loop, hence the
    `processEvents` before the registry is counted. A window that closes on the
    stack of the mouse event that emptied it is 0xC000041D: see
    `MainWindow.move_view_to_window` and tests/test_tear_off_crash.py.
    """
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
    assert source.document_area().count() == 0
    qt_app.processEvents()
    assert registry.count() == 1
    other.document_area().check_invariant()


def test_escape_on_a_lone_tab_puts_the_window_back(qt_app, store, registry,
                                                   tmp_path):
    window = _window(registry, tmp_path, ["a.pdf"], at=(100, 100))
    bar = window.document_area().bar()
    home = window.frameGeometry().topLeft()

    start = _tab_point(bar, 0)
    _press(bar, start)
    # TWO moves, and the second one is the point. The first crosses the
    # threshold and takes the grab offset from wherever the window already is,
    # which is what stops it jumping under the cursor the instant a drag
    # begins; only travel AFTER that displaces it.
    _move(bar, _below_bar(bar, start))
    _move(bar, _below_bar(bar, start) + QPoint(500, 400))
    # The window itself is what moves in the lone-tab case, so escape has
    # something real to undo here, unlike every other cancel path.
    assert window.frameGeometry().topLeft() != home
    assert bar.tear_off().ghost() is None

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


def test_a_lone_tab_moves_the_window_sideways_with_no_vertical_overshoot(
        qt_app, store, registry, tmp_path):
    """THE COMPLAINT, AS AN ASSERTION. Lucas, with two one-tab windows open:
    "if i grab the tab and move it around i should be moving the window aroun,
    right now the current action is the tab slides."

    A row of one tab has no order, so a sideways drag on it had nothing to
    reorder and nothing to do; DETACH_MARGIN then made you pull forty pixels
    DOWN before the window would move at all. With one tab the vertical
    requirement is gone and Qt's own drag distance is the whole threshold, in
    any direction.
    """
    window = _window(registry, tmp_path, ["a.pdf"], at=(100, 100))
    bar = window.document_area().bar()
    home = window.frameGeometry().topLeft()
    start = _tab_point(bar, 0)

    _press(bar, start)
    _move(bar, start + QPoint(QApplication.startDragDistance() + 4, 0))
    assert bar.tear_off().is_dragging(), "a sideways drag on a lone tab"

    _move(bar, start + QPoint(300, 0))
    assert window.frameGeometry().topLeft() == home + QPoint(300, 0)
    # The window moved. The tab did not: it is still the only tab, still at 0.
    assert window.document_area().count() == 1
    assert bar.tabAt(bar.tabRect(0).center()) == 0

    _release(bar, start + QPoint(300, 0))
    assert registry.count() == 1
    window.document_area().check_invariant()


def test_two_tabs_still_reorder_sideways_and_never_move_the_window(
        qt_app, store, registry, tmp_path):
    """The other side of the same rule, and the one that must not regress.
    "sliding tab is correct animation only if in one window it has 2 or more
    tabs." So with two tabs a sideways drag is still QTabBar's reorder, the
    gesture never engages, and the window stays where it is."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    area = window.document_area()
    bar = area.bar()
    first, second = area.view_at(0), area.view_at(1)
    home = window.frameGeometry().topLeft()

    start = _tab_point(bar, 0)
    _press(bar, start)
    for dx in (QApplication.startDragDistance() + 4, 120, 400):
        _move(bar, start + QPoint(dx, 0))
        assert not bar.tear_off().is_dragging()
    _release(bar, start + QPoint(400, 0))

    assert window.frameGeometry().topLeft() == home
    assert registry.count() == 1
    assert area.count() == 2
    assert set(area.views()) == {first, second}
    area.check_invariant()


def test_a_lone_tab_hangs_from_the_point_it_was_grabbed_by(
        qt_app, store, registry, tmp_path):
    """FIX 2, FOR THE WINDOW CASE. "the tab should stay at the curors point...
    when i grab and pull the curor is displaed beneath the tab."

    The offset was taken from wherever the cursor had got to when the gesture
    engaged, which under the old vertical threshold was forty pixels below the
    tab bar, so the window then followed the cursor with the tab hanging above
    it for the rest of the drag. It is taken from the PRESS now, so the point
    of the window under the cursor never changes.
    """
    window = _window(registry, tmp_path, ["a.pdf"], at=(400, 300))
    bar = window.document_area().bar()
    start = _tab_point(bar, 0, dx=30, dy=8)
    grab = start - window.frameGeometry().topLeft()

    _press(bar, start)
    _move(bar, start + QPoint(20, 0))
    for step in (QPoint(120, 90), QPoint(600, 40), QPoint(-200, 260)):
        here = start + step
        _move(bar, here)
        assert here - window.frameGeometry().topLeft() == grab
    _release(bar, start + QPoint(-200, 260))


def test_the_ghost_hangs_from_the_point_in_the_tab_it_was_grabbed_by(
        qt_app, store, registry, tmp_path):
    """FIX 2, FOR THE TAB CASE. The hotspot is the grab offset and nothing
    else, so the picture stays pinned under the pointer at the spot it was
    picked up from rather than merely travelling with it."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"])
    bar = window.document_area().bar()
    tear = bar.tear_off()
    grab = QPoint(37, 9)
    start = _tab_point(bar, 0, dx=grab.x(), dy=grab.y())

    _press(bar, start)
    first = _below_bar(bar, start)
    _move(bar, first)
    assert tear.ghost() is not None
    assert first - tear.ghost_position(first) == grab

    moved = first + QPoint(430, 260)
    _move(bar, moved)
    assert moved - tear.ghost_position(moved) == grab
    _release(bar, moved)


@pytest.mark.parametrize("ratio", [1.0, 1.5, 2.0])
def test_the_ghost_is_tab_sized_at_any_device_pixel_ratio(
        qt_app, store, registry, tmp_path, monkeypatch, ratio):
    """THE OTHER HALF OF THE HOTSPOT, and the classic cause of a drag image
    that will not stay under the pointer on a scaled screen.

    The grab offset is in logical pixels, so it only lands on the right part of
    the ghost while the ghost is the same LOGICAL size as the tab. On a 150%
    display `QWidget.grab` hands back a pixmap half again as large in raw
    pixels, and if it comes back tagged 1.0 the ghost is built half again too
    big and the grab point slides down it. `tab_pixmap` measures the ratio off
    the pixmap instead of trusting the tag, which is what this drives: the
    grab is faked at each ratio, untagged, exactly as the bad case looks.

    Offscreen runs everything at 1.0, so the scaling cannot be asked for and
    has to be handed in.
    """
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"])
    bar = window.document_area().bar()
    tear = bar.tear_off()
    rect = bar.tabRect(0)
    real = QTabBar.grab

    def scaled(self, *args, **kwargs):
        pixmap = real(self, *args, **kwargs)
        if not args or pixmap.isNull():
            return pixmap
        # As an untagged grab on a scaled screen arrives: raw device pixels,
        # still claiming a ratio of 1.0.
        return pixmap.scaled(round(pixmap.width() * ratio),
                             round(pixmap.height() * ratio))

    monkeypatch.setattr(QTabBar, "grab", scaled)
    pixmap = tab_pixmap(bar, rect)
    assert abs(pixmap.devicePixelRatio() - ratio) < 1e-3
    assert round(pixmap.width() / pixmap.devicePixelRatio()) == rect.width()

    grab = QPoint(25, 7)
    start = _tab_point(bar, 0, dx=grab.x(), dy=grab.y())
    _press(bar, start)
    here = _below_bar(bar, start)
    _move(bar, here)
    ghost = tear.ghost()
    assert ghost is not None
    assert ghost.size() == rect.size()
    assert here - tear.ghost_position(here) == grab
    _release(bar, here)


def test_a_lone_tab_over_another_window_s_body_does_not_merge(
        qt_app, store, registry, tmp_path):
    """THE JUDGEMENT CALL. A tab being carried can land anywhere over a window,
    because the thing under the cursor is tab-sized and aimed. A whole WINDOW
    cannot: it covers what it is over, and two windows overlapping is what
    moving a window across a desk looks like. Charging that a merge would make
    windows impossible to arrange, so for the lone-tab drag the target's TAB
    STRIP is the only thing that accepts a drop. That is Edge's line too.
    """
    source = _window(registry, tmp_path, ["a.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf"], at=(2000, 100))
    bar = source.document_area().bar()

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, start + QPoint(40, 0))
    # Well down inside the other window, nowhere near its strip.
    body = other.mapToGlobal(QPoint(other.width() // 2, other.height() // 2))
    _move(bar, body)
    assert bar.tear_off().drop_target() is None
    _release(bar, body)

    qt_app.processEvents()
    assert registry.count() == 2
    assert source.document_area().count() == 1
    assert other.document_area().count() == 2


def test_a_lone_tab_never_treats_its_own_window_as_a_target(
        qt_app, store, registry, tmp_path):
    """It is the thing in flight, it is on top, and the cursor is pinned inside
    it for the whole drag, so leaving it in the walk would have it answer every
    hit test and nothing underneath would ever be reachable. That is what the
    merge below depends on."""
    window = _window(registry, tmp_path, ["a.pdf"], at=(100, 100))
    bar = window.document_area().bar()
    start = _tab_point(bar, 0)

    _press(bar, start)
    _move(bar, start + QPoint(60, 0))
    assert bar.tear_off().is_dragging()
    # The cursor is still on this window's own tab, and it is not a target.
    assert bar.tear_off().drop_target() is None
    _release(bar, start + QPoint(60, 0))


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


# ======================================================================
# 7. The zones, and the ghost slot
#
# TWO SIZES ON PURPOSE. Getting a tab INTO a window is a large target, the
# window's full width and a whole tab row below the strip; getting one OUT of a
# window is a small one, four pixels, so the tear registers as fast as it can
# without a reorder ever reaching it. Everything below is one half of that
# asymmetry, or the ghost slot that makes the large half readable.
# ======================================================================

def test_the_incoming_zone_spans_the_full_width_of_the_window(
        qt_app, store, registry, tmp_path):
    """THE COMPLAINT, AS AN ASSERTION. Lucas: "when i drag a tab to another
    window so its added there, i cant drop it in the tab area, i need o bring
    it as close as posible to the only tab in the window."

    The bar hugs its tabs, so with one document open it is about 250 px wide on
    a 1200 px window, and the old zone was that rect plus 18 px. Two thirds of
    the way across the caption was not the tab area at all.
    """
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf"], at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()
    moving = source.document_area().view_at(0)

    # Far to the right of the only tab, and a little below the row: nowhere
    # near the bar, and squarely inside the window's tab area.
    over = _in_zone(other, other.width() - 60, 6)
    old_band = other_bar.rect().adjusted(0, -DOCK_MARGIN, 0, DOCK_MARGIN)
    assert not old_band.contains(other_bar.mapFromGlobal(over)), \
        "the point has to be outside the band this replaces"

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    _move(bar, over)

    target = bar.tear_off().drop_target()
    assert target is not None and target[0] is other
    assert other.document_area().count() == 2
    assert other.document_area().view_at(1) is moving

    _release(bar, over)
    assert other.document_area().view_at(1) is moving
    other.document_area().check_invariant()


def test_the_incoming_zone_reaches_well_below_the_tab_row(
        qt_app, store, registry, tmp_path):
    """His words for the tab area: the full width of the window "plus a
    generous buffer below the tab row".

    Below the row AND still precise, which is the half that matters. A point
    that was not on a tab used to fall through to "append on the end", so
    aiming at a position meant aiming at a 28 px strip.
    """
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf"], at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()
    moving = source.document_area().view_at(0)

    # Under the left quarter of the FIRST tab, well below the row.
    quarter = other_bar.tabRect(0).width() // 4
    deep = _in_zone(other, 0, INCOMING_SLACK - 6)
    over = QPoint(other_bar.mapToGlobal(QPoint(quarter, 0)).x(), deep.y())
    assert other_bar.tabAt(other_bar.mapFromGlobal(over)) < 0, \
        "the point has to be off the tabs themselves"

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    _move(bar, over)

    assert bar.tear_off().drop_target() == (other, 0)
    assert other.document_area().index_of(moving) == 0
    _release(bar, over)
    assert other.document_area().view_at(0) is moving
    other.document_area().check_invariant()


def test_the_tear_registers_a_short_way_out_of_the_row(
        qt_app, store, registry, tmp_path):
    """It was forty pixels, which is a third of the way down the toolbar, and
    it read as the app not responding: pulling a tab out of a two-tab window
    took too long to become a ghost."""
    window = _window(registry, tmp_path, ["a.pdf", "b.pdf"])
    bar = window.document_area().bar()
    start = _tab_point(bar, 0)

    _press(bar, start)
    _move(bar, _below_bar(bar, start, extra=DETACH_MARGIN + 1))
    assert bar.tear_off().is_dragging()
    assert bar.tear_off().ghost() is not None

    _escape(bar)
    assert window.document_area().count() == 2


def test_the_outgoing_zone_is_smaller_than_the_incoming_one(
        qt_app, store, registry, tmp_path):
    """THE ASYMMETRY ITSELF, at one depth, on two windows.

    The same distance below the tab row is inside another window's tab area and
    outside the source's. That is the whole design in one assertion: a tab
    arriving is offered a large target and a tab leaving is let go of quickly.
    """
    assert REDOCK_MARGIN < DETACH_MARGIN < INCOMING_SLACK
    depth = (REDOCK_MARGIN + INCOMING_SLACK) // 2

    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf"], at=(2000, 100))
    bar = source.document_area().bar()

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))

    _move(bar, _in_zone(source, 300, depth))
    assert bar.tear_off().drop_target() is None, "the source let go"

    _move(bar, _in_zone(other, 300, depth))
    target = bar.tear_off().drop_target()
    assert target is not None and target[0] is other, "the other window caught"

    _escape(bar)


def test_the_tabs_part_and_a_ghost_takes_the_first_slot(
        qt_app, store, registry, tmp_path):
    """TWO tabs in the target and not three, deliberately. At three the bar is
    past its width and scrolls, so `tabRect(0)` is a rectangle off the left of
    the widget and a point built from it is not on screen at all. That is a
    property of the strip, not of this gesture, and aiming at it would test the
    scroll offset instead of the ghost."""
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf"], at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()
    moving = source.document_area().view_at(0)
    was = [other.document_area().view_at(i) for i in range(2)]

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    over = _tab_point(other_bar, 0, dx=4)
    _move(bar, over)

    assert other.document_area().count() == 3
    assert other.document_area().index_of(moving) == 0
    assert other_bar.ghost_index() == 0
    # The strip PARTED rather than shuffled: the two that were there are still
    # in order, one place to the right.
    assert [other.document_area().view_at(i) for i in (1, 2)] == was

    _release(bar, over)
    assert other_bar.ghost_index() is None
    assert other.document_area().view_at(0) is moving
    other.document_area().check_invariant()


def test_the_tabs_part_and_a_ghost_takes_the_last_slot(
        qt_app, store, registry, tmp_path):
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf", "z.pdf"],
                    at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()
    moving = source.document_area().view_at(0)
    was = [other.document_area().view_at(i) for i in range(3)]

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    # Past the right-hand end of the last tab, still inside the zone.
    over = _in_zone(other, other.width() - 80, 4)
    _move(bar, over)

    assert other.document_area().count() == 4
    assert other.document_area().index_of(moving) == 3
    assert other_bar.ghost_index() == 3
    assert [other.document_area().view_at(i) for i in (0, 1, 2)] == was

    _release(bar, over)
    assert other_bar.ghost_index() is None
    assert other.document_area().view_at(3) is moving
    other.document_area().check_invariant()


def test_the_ghost_moves_slot_as_the_cursor_does(qt_app, store, registry,
                                                 tmp_path):
    """The gap follows the cursor along the strip, which is the whole reason
    the feedback is worth having: it answers "where will it fall" before the
    button comes up, at every position and not only at the ends."""
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf"], at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()
    moving = source.document_area().view_at(0)

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))

    # Global x, not a tab index: the strip relayouts under the tab it has just
    # taken in, so an index read before the move names a different tab after
    # it. The left edge of the strip and the far right of the caption are two
    # fixed points that mean the same thing on both sides of the move.
    left = other_bar.mapToGlobal(QPoint(4, 0)).x()
    right = other.frameGeometry().left() + other.width() - 80
    seen = []
    for x in (left, right):
        _move(bar, QPoint(x, _bar_bottom(other) + 4))
        assert other_bar.ghost_index() == other.document_area().index_of(moving)
        seen.append(other_bar.ghost_index())
    assert seen == [0, other.document_area().count() - 1], seen

    _escape(bar)


def test_leaving_the_tab_area_closes_the_gap(qt_app, store, registry, tmp_path):
    """Approaching a strip opens a gap in it, so leaving has to close one.

    Without this a tab that brushed past a window on its way to the desktop
    stayed parked in that window's bar, ghosted, for the rest of the drag, and
    the drop then made its new window out of the wrong one.
    """
    source = _window(registry, tmp_path, ["a.pdf", "b.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf"], at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()
    moving = source.document_area().view_at(0)

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, _below_bar(bar, start))
    over = _tab_point(other_bar, 1, dx=4)
    _move(bar, over)
    assert other.document_area().count() == 3
    assert other_bar.ghost_index() == 1

    far = QPoint(3000, 2000)          # nowhere near any window
    _move(bar, far)

    assert bar.tear_off().drop_target() is None
    assert other_bar.ghost_index() is None
    assert other.document_area().count() == 2
    assert source.document_area().count() == 2
    assert source.document_area().index_of(moving) == 0
    assert bar.tear_off().ghost() is not None, "mid-air again, so on screen again"
    other.document_area().check_invariant()
    source.document_area().check_invariant()

    _release(bar, far)
    assert registry.count() == 3
    assert moving.window() not in (source, other)


def test_a_whole_window_drag_still_only_merges_on_the_strip(
        qt_app, store, registry, tmp_path):
    """THE ZONE THAT MUST NOT GROW. A tab under the cursor is tab-sized and
    aimed; a window under the cursor covers whatever it is over, and sliding
    one window across another is what arranging a desk looks like. Charging
    that gesture a merge would make windows impossible to place.

    So the wide incoming zone is a rule about carrying a TAB. A carried WINDOW
    still has to be over the target's strip, and the middle assertion is what
    proves the new zone did not leak into it: a depth that a carried tab would
    call the tab area is not one a carried window may merge from.
    """
    source = _window(registry, tmp_path, ["a.pdf"], at=(100, 100))
    other = _window(registry, tmp_path, ["x.pdf", "y.pdf"], at=(2000, 100))
    bar = source.document_area().bar()
    other_bar = other.document_area().bar()

    start = _tab_point(bar, 0)
    _press(bar, start)
    _move(bar, start + QPoint(QApplication.startDragDistance() + 4, 0))
    assert bar.tear_off().is_dragging()

    body = other.mapToGlobal(QPoint(other.width() // 2, other.height() // 2))
    _move(bar, body)
    assert bar.tear_off().drop_target() is None, "the body is not a merge"

    between = _in_zone(other, other.width() // 2, DOCK_MARGIN + 8)
    assert between.y() < _bar_bottom(other) + INCOMING_SLACK, \
        "the point has to be inside what a carried TAB would call the zone"
    _move(bar, between)
    assert bar.tear_off().drop_target() is None, "and neither is the zone"

    over = _tab_point(other_bar, 0, dx=4)
    _move(bar, over)
    assert bar.tear_off().drop_target() == (other, 0)

    _escape(bar)
    assert source.document_area().count() == 1
    assert other.document_area().count() == 2
