"""The tab strip's chrome: the close control, the silhouette, the drag feedback.

WHAT THIS FILE CANNOT TEST, AND IT IS THE INTERESTING HALF.

conftest.py forces QT_QPA_PLATFORM=offscreen. Offscreen has no window
procedure, no compositor and no z-order, so it can tell you a widget's state
and its geometry but it cannot tell you that a single pixel reached a screen.
Specifically, nothing below proves:

  - that the drag feedback is VISIBLE. These tests assert that `ghost_index`
    names the right tab and that `drop_indicator` is at the right x. Whether
    the veil and the line actually paint, in the accent, over the frameless
    chrome, and legibly in both themes, is a question about pixels on a screen
    and this suite cannot ask it. `test_the_ghost_paints_on_its_own_tab_and_
    nowhere_else` gets closest: it compares two grabs of the widget, which
    proves the paint code ran and where, not that a screen received it.
  - that the close button LOOKS centred. The arithmetic is checked here; how it
    reads against a rounded tab is not.
  - anything involving real mouse capture, `grabMouse`, or the OS hit test.

For those, drive the real thing: `tools/shoot_tab_drag.py` performs the gesture
with Win32 SendInput and photographs the composited desktop in both themes. Two
defects in one day were hidden by a green offscreen run, so a green run here is
the floor and not the evidence.

WHAT IT DOES TEST is everything that is pure arithmetic or pure state, which is
where the close-button bug actually lived: Qt's own placement, not the painting.
"""

import fitz
import pytest

from PySide6.QtWidgets import QApplication, QMessageBox

from core.settings import Settings, set_settings
from ui.document_area import (
    CLOSE_BUTTON_RIGHT_INSET, CLOSE_BUTTON_SIZE, DROP_FEEDBACK_MIN_WIDTH,
    TAB_SHAPE_MARGIN_X, TAB_SHAPE_MARGIN_Y,
)
from ui.main_window import MainWindow
from ui.theme import DARK, LIGHT, build_qss
from ui.window_registry import WindowRegistry


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    s = Settings(tmp_path / "settings.json", debounce_ms=0, migrate_legacy=False)
    s.close.confirm_multiple_tabs = False
    previous = set_settings(s)
    yield s
    set_settings(previous)


@pytest.fixture(autouse=True)
def never_opens_a_dialog(monkeypatch):
    for name in ("question", "warning", "critical", "information", "about"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, n=name, **k: pytest.fail(
                f"QMessageBox.{n} opened: {a[1:3]}")))


def _pdf(tmp_path, name, pages=1):
    path = tmp_path / name
    raw = fitz.open()
    for i in range(pages):
        page = raw.new_page(width=400, height=500)
        page.insert_text((20, 100), f"{name} p{i}", fontsize=24)
    raw.save(str(path))
    raw.close()
    return str(path)


def _dispose(window):
    for view in window.document_area().views():
        view.clear_document()
        view.teardown()
    window._force_quit = True
    window.close()
    window.deleteLater()


@pytest.fixture
def win(qt_app, store, tmp_path):
    QApplication.instance().setStyleSheet(build_qss(LIGHT))
    window = MainWindow()
    window.resize(1200, 800)
    window.show()
    window.open_paths([_pdf(tmp_path, "alpha.pdf"),
                       _pdf(tmp_path, "bravo.pdf"),
                       _pdf(tmp_path, "charlie.pdf")])
    qt_app.processEvents()
    yield window
    _dispose(window)
    QApplication.instance().setStyleSheet("")


# ---------------------------------------------------------------------------
# The close control
# ---------------------------------------------------------------------------

def test_the_close_glyph_is_centred_on_the_button_to_the_pixel(win):
    """The half-pixel half of "the x is not centered".

    `QRect(0, 0, 16, 16).center()` is QPoint(7, 7), because a QRect's right is
    its last pixel and not its bound. Building the X from that put it half a
    pixel up and left of true centre, which on an antialiased 1.3px pen is a
    soft, lopsided mark. The float centre is exactly the middle.
    """
    button = win.document_area().bar().close_button(0)
    centre = button.glyph_centre()
    assert centre.x() == CLOSE_BUTTON_SIZE / 2
    assert centre.y() == CLOSE_BUTTON_SIZE / 2


def test_the_close_button_is_inset_from_the_tab_edge(win):
    """The horizontal half, and the one the stylesheet cannot reach.

    Qt pins the button one pixel inside the tab rect and reads neither the
    `::tab` padding nor its margin to do it, so against 10px of left padding
    the tab had breathing room down one side and none down the other. See
    `DocumentTabBar._place_close_buttons`.
    """
    bar = win.document_area().bar()
    side = bar._button_side()
    assert bar.count() == 3
    for i in range(bar.count()):
        rect = bar.tabRect(i)
        geometry = bar.tabButton(i, side).geometry()
        gap = rect.right() - geometry.right()
        assert gap == CLOSE_BUTTON_RIGHT_INSET + 1, f"tab {i} right gap {gap}"


def test_the_close_button_is_centred_on_the_tab_vertically(win):
    """Qt centres on the tab RECT, and the stylesheet draws the tab inset by
    its margin, so the two agree only while the top and bottom margins match.
    They did not: it was 5px over and 0 under, putting the X two and a half
    pixels high in every tab. This is the assertion that keeps them matched."""
    bar = win.document_area().bar()
    side = bar._button_side()
    for i in range(bar.count()):
        rect = bar.tabRect(i)
        geometry = bar.tabButton(i, side).geometry()
        above = geometry.top() - rect.top()
        below = rect.bottom() - geometry.bottom()
        assert above == below, f"tab {i} sits {above} from the top, {below} below"


def test_the_close_button_stays_placed_across_a_relayout(win):
    """`setTabButton` re-lays out one tab WITHOUT raising `tabLayoutChange`,
    and a window resize re-lays out every tab. Both used to leave the button at
    Qt's own position, so the placement is driven from three hooks and this
    exercises two of them."""
    bar = win.document_area().bar()
    side = bar._button_side()
    win.resize(900, 700)
    QApplication.instance().processEvents()
    bar.resize(bar.sizeHint())
    QApplication.instance().processEvents()
    for i in range(bar.count()):
        gap = bar.tabRect(i).right() - bar.tabButton(i, side).geometry().right()
        assert gap == CLOSE_BUTTON_RIGHT_INSET + 1


# ---------------------------------------------------------------------------
# The silhouette
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_the_tab_is_a_closed_shape(palette):
    """All four corners rounded and no open bottom edge. The reference is
    Claude's own tab bar: "instead of leaving the line below as open lets close
    it off to form a square"."""
    qss = build_qss(palette)
    start = qss.index("QTabBar#documentTabBar::tab {")
    block = qss[start:qss.index("}", start)]
    assert "border-radius: 8px" in block
    assert "border-bottom: none" not in block
    assert "border-top-left-radius" not in block


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_tabs_are_separated_by_space_and_not_by_hairlines(palette):
    """The closed shape plus a horizontal margin does the separator's job, and
    a vertical rule butting into a rounded corner reads as a smudge."""
    qss = build_qss(palette)
    assert "next-selected" not in qss
    assert "border-right: 1px solid" not in qss


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_the_vertical_tab_margin_is_symmetric(palette):
    """Not taste. Qt centres the close button on the tab rect while the
    stylesheet draws the tab inset by its margin, so an asymmetric vertical
    margin is a close button that sits off centre. See the test above."""
    qss = build_qss(palette)
    start = qss.index("QTabBar#documentTabBar::tab {")
    block = qss[start:qss.index("}", start)]
    margin = [line for line in block.splitlines() if "margin:" in line][0]
    parts = margin.split(":")[1].strip().rstrip(";").split()
    assert len(parts) == 4, margin
    assert parts[0] == parts[2], f"top and bottom margin differ: {margin}"


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_only_the_selected_tab_is_filled_and_bordered(palette):
    """Inactive tabs are bare, so the fill and the border together are the
    whole active signal. The transparent border on the base rule is what stops
    the label shifting a pixel when a tab becomes selected."""
    qss = build_qss(palette)
    start = qss.index("QTabBar#documentTabBar::tab {")
    base = qss[start:qss.index("}", start)]
    assert "background: transparent" in base
    assert "border: 1px solid transparent" in base

    start = qss.index("QTabBar#documentTabBar::tab:selected {")
    selected = qss[start:qss.index("}", start)]
    assert "border: 1px solid" in selected
    assert "background-color" in selected


# ---------------------------------------------------------------------------
# The drag feedback
# ---------------------------------------------------------------------------

@pytest.fixture
def two_windows(qt_app, store, tmp_path):
    QApplication.instance().setStyleSheet(build_qss(LIGHT))
    registry = WindowRegistry.instance()
    a = MainWindow()
    a.resize(1200, 800)
    a.show()
    a.open_paths([_pdf(tmp_path, "alpha.pdf"), _pdf(tmp_path, "bravo.pdf")])
    b = registry.create_window(show=False)
    b.resize(1200, 800)
    b.show()
    b.open_paths([_pdf(tmp_path, "delta.pdf"), _pdf(tmp_path, "echo.pdf")])
    qt_app.processEvents()
    yield a, b
    _dispose(a)
    _dispose(b)
    QApplication.instance().setStyleSheet("")


def _pretend_dragging(tear, window, view):
    """Put a tear-off into the state `_track` would have left it in.

    The gesture itself needs real mouse capture, which offscreen does not have,
    so the state is set directly and only the feedback is exercised. That is
    the seam this file can reach; the gesture is `tools/shoot_tab_drag.py`.
    """
    tear._dragging = True
    tear._view = view
    tear._source_window = window
    tear._attached_to = window
    tear._whole_window = False


def _carried_into(tear, source, target, view, at):
    """The state after the live attach has put `view` into `target` at `at`.

    The approach moves the tab for real (`TabTearOff._attach_to_strip`), and
    the ghost slot is a fact about a tab that is already in the strip, so a
    test of the feedback has to start from a strip that has parted.
    """
    assert source.move_view_to_window(view, target, at)
    _pretend_dragging(tear, source, view)
    tear._attached_to = target


def test_the_receiving_strip_gets_a_ghost_slot_and_no_line(two_windows):
    """THE LINE IS GONE FOR A CARRIED TAB, and this is where that is pinned.

    The strip used to be washed in the accent and outlined in it, which on a
    bar that hugs its tabs read as an amber box around the tab. That was cut
    back to a 4px insertion line, and the line was then reported as something
    the drag ghost sat half on top of: "it almost even blocks the view of the
    highlithed bar and where it will fall."

    So the feedback is the SHAPE of the strip rather than a mark on it. The
    tabs part, the carried tab sits in the gap, and it is painted as a ghost
    until the button comes up. See `TabTearOff._show_drop_feedback`.
    """
    a, b = two_windows
    bar = b.document_area().bar()
    view = a.document_area().view_at(0)
    tear = a.document_area().bar()._tear_off
    _carried_into(tear, a, b, view, 1)

    tear._show_drop_feedback((b, 1))
    assert bar.ghost_index() == 1
    assert bar.drop_indicator() is None
    assert a.document_area().bar().ghost_index() is None
    # There is no strip-wide state left to set, so nothing can paint one.
    assert not hasattr(bar, "drop_active")
    assert not hasattr(bar, "set_drop_active")


def test_the_ghost_lands_on_the_shape_the_stylesheet_paints(two_windows):
    """The slot is the TAB's shape, not the tab's rect.

    `::tab` carries `margin: 4px 2px`, so a rectangle drawn on the bare rect
    would stand two pixels proud of the tab on each side and four above and
    below it, which reads as a box around the tab rather than as the tab faded
    out. This is the arithmetic; the look is `tools/shoot_tab_drag.py`.
    """
    a, b = two_windows
    bar = b.document_area().bar()
    view = a.document_area().view_at(0)
    tear = a.document_area().bar()._tear_off
    _carried_into(tear, a, b, view, 1)
    tear._show_drop_feedback((b, 1))

    rect = bar.tabRect(1)
    slot = bar.ghost_slot_rect()
    assert not slot.isEmpty()
    assert slot == rect.adjusted(TAB_SHAPE_MARGIN_X, TAB_SHAPE_MARGIN_Y,
                                 -TAB_SHAPE_MARGIN_X, -TAB_SHAPE_MARGIN_Y)
    assert rect.contains(slot)


def test_the_ghost_paints_on_its_own_tab_and_nowhere_else(two_windows):
    """The paint itself, as far as offscreen can reach it.

    A wash covers the whole bar rect, so the way to ask "is there a box"
    without a compositor is to paint the bar into a pixmap twice, with the
    feedback down and then up, and compare. Everything outside the ghost's own
    tab has to come out identical, and the ghost's own tab has to come out
    different, because a veil that changes nothing is not a veil.

    This proves the paint and not merely the state, which is the half this
    file's docstring says offscreen usually cannot reach. It works because the
    question is about one widget painting itself, not about pixels arriving on
    a screen.
    """
    a, b = two_windows
    bar = b.document_area().bar()
    view = a.document_area().view_at(0)
    tear = a.document_area().bar()._tear_off
    _carried_into(tear, a, b, view, 1)

    clean = bar.grab().toImage()
    tear._show_drop_feedback((b, 1))
    slot = bar.ghost_slot_rect()
    assert not slot.isEmpty()
    lit = bar.grab().toImage()

    probes = [(x, y)
              for x in (4, slot.left() - 4, slot.right() + 4, bar.width() - 5)
              for y in (0, 1, bar.height() // 2, bar.height() - 1)
              if not slot.contains(x, y) and 0 <= x < bar.width()]
    assert probes, "every probe landed on the ghost itself"
    for x, y in probes:
        assert lit.pixel(x, y) == clean.pixel(x, y), f"painted at ({x}, {y})"

    inside = slot.center()
    assert lit.pixel(inside) != clean.pixel(inside), "the veil painted nothing"


def test_only_one_strip_is_marked_at_a_time(two_windows):
    """Moving from one window to another has to take the mark off the first,
    or a drag across three windows leaves a trail of ghost slots."""
    a, b = two_windows
    view = a.document_area().view_at(0)
    tear = a.document_area().bar()._tear_off
    _carried_into(tear, a, b, view, 0)
    tear._show_drop_feedback((b, 0))
    assert b.document_area().bar().ghost_index() == 0

    assert b.move_view_to_window(view, a, 0)
    tear._attached_to = a
    tear._show_drop_feedback((a, 0))
    assert b.document_area().bar().ghost_index() is None
    assert a.document_area().bar().ghost_index() == 0


def test_the_feedback_is_cleared_when_the_drag_ends(two_windows):
    a, b = two_windows
    view = a.document_area().view_at(0)
    tear = a.document_area().bar()._tear_off
    _carried_into(tear, a, b, view, 1)
    tear._show_drop_feedback((b, 1))
    assert b.document_area().bar().ghost_index() is not None
    tear._clear_drop_feedback()
    for window in (a, b):
        bar = window.document_area().bar()
        assert bar.drop_indicator() is None
        assert bar.ghost_index() is None


def test_the_drop_turns_the_ghost_into_the_tab_it_was_over(two_windows):
    """The whole promise of the ghost in one assertion.

    Nothing moves on the drop. The tab is already at the index the ghost was
    drawn on, and clearing the ghost is the entire transition from "about to
    land here" to "landed here".
    """
    a, b = two_windows
    view = a.document_area().view_at(0)
    tear = a.document_area().bar()._tear_off
    _carried_into(tear, a, b, view, 1)
    tear._show_drop_feedback((b, 1))
    bar = b.document_area().bar()
    ghosted = bar.ghost_index()

    tear._clear_drop_feedback()

    assert bar.ghost_index() is None
    assert b.document_area().index_of(view) == ghosted


def test_the_ghost_marks_the_tab_being_carried(two_windows):
    """The index is read AFTER the live attach, not before.

    The tab joins the target strip on approach, so by the time the feedback
    goes up it is already sitting at its landing position and its own index is
    the answer. Reading the hit test's index instead would ghost the wrong tab
    on every frame.
    """
    a, b = two_windows
    view = a.document_area().view_at(0)
    tear = a.document_area().bar()._tear_off
    _carried_into(tear, a, b, view, 1)
    tear._show_drop_feedback((b, 0))     # a deliberately stale index
    bar = b.document_area().bar()
    assert bar.ghost_index() == b.document_area().index_of(view)
    assert bar.ghost_index() == 1


def test_a_whole_window_being_carried_gets_an_empty_ghost_slot(two_windows):
    """THE CASE THAT USED TO GET THE LINE, AND WHY IT DOES NOT ANY MORE.

    A lone tab drags its own window and the merge is deferred to the release,
    so nothing has joined the target strip and there is no tab to ghost. That
    was taken as a reason to fall back to a four-pixel insertion line, and the
    line is what Lucas saw every single time he merged two windows: the ghost
    "never appears". The slot is now held open empty instead, which is the same
    gap in the same place, minus only the picture of the tab.
    """
    a, b = two_windows
    bar = b.document_area().bar()
    tear = a.document_area().bar()._tear_off
    _pretend_dragging(tear, a, a.document_area().view_at(0))
    tear._whole_window = True

    tear._show_drop_feedback((b, 1))
    assert bar.drop_indicator() is None, "no line, that was the complaint"
    assert bar.ghost_index() is None, "and no real tab has joined this strip"
    slot = bar.ghost_slot_rect()
    assert not slot.isEmpty()
    assert slot.width() >= bar.tabRect(0).width() - 2 * TAB_SHAPE_MARGIN_X,         "a tab-sized gap, not a hairline"
    # And it is where the tab would go, not merely somewhere on the strip.
    assert slot.left() == bar.insertion_x(1) + TAB_SHAPE_MARGIN_X


def test_the_empty_slot_makes_the_strip_wider_rather_than_covering_a_tab(
        two_windows):
    """The slot is REAL WIDTH. A strip that hugs its tabs has ten pixels of
    spare room, so a tab-sized gap drawn into it without one would be clipped
    to a sliver or painted straight over the tab next door."""
    a, b = two_windows
    bar = b.document_area().bar()
    tear = a.document_area().bar()._tear_off
    _pretend_dragging(tear, a, a.document_area().view_at(0))
    tear._whole_window = True
    was = bar.width()

    tear._show_drop_feedback((b, bar.count()))
    assert bar.width() >= was + bar.ghost_slot_width(), "the strip parted"
    last = bar.tabRect(bar.count() - 1)
    assert bar.ghost_slot_rect().left() >= last.right(),         "and the gap is past the last tab rather than on top of it"

    tear._clear_drop_feedback_on(b)
    assert bar.ghost_slot_rect().isEmpty()


def test_a_lone_tab_and_a_carried_tab_get_the_same_kind_of_feedback(two_windows):
    """Lucas does not make the distinction and should not have to. Both
    gestures put a tab-shaped gap in the target strip, in the same place, and
    neither draws a line."""
    a, b = two_windows
    bar = b.document_area().bar()
    tear = a.document_area().bar()._tear_off

    _pretend_dragging(tear, a, a.document_area().view_at(0))
    tear._whole_window = True
    tear._show_drop_feedback((b, bar.count()))
    lone = bar.ghost_slot_rect()
    tear._clear_drop_feedback_on(b)

    view = a.document_area().view_at(0)
    _carried_into(tear, a, b, view, b.document_area().count())
    tear._whole_window = False
    tear._show_drop_feedback((b, bar.count() - 1))
    carried = bar.ghost_slot_rect()

    # Both are a tab-shaped gap of the same size, and neither is a line. The
    # POSITIONS cannot be compared directly: the carried case has really moved
    # its view into this strip, so the strip it opens a gap in has one more tab
    # in it than the lone case does.
    assert not lone.isEmpty() and not carried.isEmpty()
    assert bar.drop_indicator() is None
    assert lone.size() == carried.size()


def test_an_empty_strip_is_never_marked(two_windows):
    """THE LITTLE GOLD BOX. The wash and the outline that made it are gone, but
    a full-height 4px line on a bar with nothing in it is the same artifact in
    a thinner shape, so the gate stays."""
    a, b = two_windows
    bar = b.document_area().bar()
    while b.document_area().count() > 0:
        b.document_area().remove_view(0)
    assert bar.count() == 0
    assert bar._can_paint_drop_feedback() is False


def test_a_hidden_strip_is_never_marked(two_windows):
    """The source-level half of the same fix: a window holding one empty
    document hides its whole header, so there is no strip on screen to mark
    and the state is not set rather than being set and then not drawn."""
    a, b = two_windows
    bar = b.document_area().bar()
    view = a.document_area().view_at(0)
    tear = a.document_area().bar()._tear_off
    _carried_into(tear, a, b, view, 1)
    b.document_area().header().setVisible(False)
    tear._show_drop_feedback((b, 1))
    assert bar.drop_indicator() is None
    assert bar.ghost_index() is None


def test_a_narrow_strip_is_never_marked(two_windows):
    a, b = two_windows
    bar = b.document_area().bar()
    bar.resize(DROP_FEEDBACK_MIN_WIDTH - 1, bar.height())
    assert bar._can_paint_drop_feedback() is False
