"""The tab strip's chrome: the close control, the silhouette, the drag feedback.

WHAT THIS FILE CANNOT TEST, AND IT IS THE INTERESTING HALF.

conftest.py forces QT_QPA_PLATFORM=offscreen. Offscreen has no window
procedure, no compositor and no z-order, so it can tell you a widget's state
and its geometry but it cannot tell you that a single pixel reached a screen.
Specifically, nothing below proves:

  - that the drag feedback is VISIBLE. These tests assert that `drop_active` is
    set and that `drop_indicator` is at the right x. Whether the wash, the
    outline and the line actually paint, in the accent, over the frameless
    chrome, under a drag ghost, and legibly in both themes, is a question about
    pixels on a screen and this suite cannot ask it.
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


def test_the_receiving_strip_lights_up(two_windows):
    a, b = two_windows
    tear = a.document_area().bar()._tear_off
    _pretend_dragging(tear, a, a.document_area().view_at(0))
    tear._show_drop_feedback((b, 1))
    assert b.document_area().bar().drop_active() is True
    assert b.document_area().bar().drop_indicator() is not None
    assert a.document_area().bar().drop_active() is False


def test_only_one_strip_is_lit_at_a_time(two_windows):
    """Moving from one window to another has to take the paint off the first,
    or a drag across three windows leaves a trail of highlighted strips."""
    a, b = two_windows
    tear = a.document_area().bar()._tear_off
    _pretend_dragging(tear, a, a.document_area().view_at(0))
    tear._show_drop_feedback((b, 0))
    assert b.document_area().bar().drop_active() is True
    tear._show_drop_feedback((a, 0))
    assert b.document_area().bar().drop_active() is False
    assert a.document_area().bar().drop_active() is True


def test_the_feedback_is_cleared_when_the_drag_ends(two_windows):
    a, b = two_windows
    tear = a.document_area().bar()._tear_off
    _pretend_dragging(tear, a, a.document_area().view_at(0))
    tear._show_drop_feedback((b, 1))
    tear._clear_drop_feedback()
    for window in (a, b):
        assert window.document_area().bar().drop_active() is False
        assert window.document_area().bar().drop_indicator() is None


def test_the_line_marks_the_tab_being_carried(two_windows):
    """The index is read AFTER the live attach, not before.

    The tab joins the target strip on approach, so by the time the feedback
    goes up the carried tab is already sitting at its landing position and
    `insertion_x` of that index is its own left edge. Reading the hit test's
    index instead would put the line one position stale on every frame.
    """
    a, b = two_windows
    view = a.document_area().view_at(0)
    assert a.move_view_to_window(view, b, 1)
    tear = a.document_area().bar()._tear_off
    _pretend_dragging(tear, a, view)
    tear._attached_to = b
    tear._show_drop_feedback((b, 0))     # a deliberately stale index
    bar = b.document_area().bar()
    landed = b.document_area().index_of(view)
    assert bar.drop_indicator() == bar.insertion_x(landed)


def test_an_empty_strip_is_never_washed(two_windows):
    """THE LITTLE GOLD BOX. A wash plus a 2px outline around a bar with nothing
    in it is not a highlighted strip, it is a small accent box floating in the
    caption, which is what got reported. The bar refuses to paint it."""
    a, b = two_windows
    bar = b.document_area().bar()
    while b.document_area().count() > 0:
        b.document_area().remove_view(0)
    assert bar.count() == 0
    assert bar._can_paint_drop_feedback() is False


def test_a_hidden_strip_is_never_lit(two_windows):
    """The source-level half of the same fix: a window holding one empty
    document hides its whole header, so there is no strip on screen to light
    and the state is not set rather than being set and then not drawn."""
    a, b = two_windows
    bar = b.document_area().bar()
    b.document_area().header().setVisible(False)
    tear = a.document_area().bar()._tear_off
    _pretend_dragging(tear, a, a.document_area().view_at(0))
    tear._show_drop_feedback((b, 0))
    assert bar.drop_active() is False


def test_a_narrow_strip_is_never_washed(two_windows):
    a, b = two_windows
    bar = b.document_area().bar()
    bar.resize(DROP_FEEDBACK_MIN_WIDTH - 1, bar.height())
    assert bar._can_paint_drop_feedback() is False
