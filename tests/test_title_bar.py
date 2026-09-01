"""The window has no system title bar, and the tabs are the top row of it.

WHAT THIS PHASE HAD TO NOT BREAK, which is what most of the file is about. A
frameless window is easy; a frameless window that still behaves like a Windows
window is the whole job, and every one of the behaviours the system used to
supply for free is now a line of our code that can be wrong:

  1. THE ORDER OF THE ROWS. The complaint that started this was that the tabs
     sat under the menu bar. Nothing may be above them now.
  2. DRAGGING. Bare strip moves the window; a tab does not, or the tabs stop
     being clickable, which is the classic version of this bug.
  3. DOUBLE-CLICK. Maximise, and restore again.
  4. RESIZING FROM ALL EIGHT DIRECTIONS. The other classic version of this bug,
     and the reason `resize_edges_at` is a plain function: the eight zones can
     be walked exhaustively without a pointer to drag.
  5. THE WINDOW CONTROLS. Three of them, Windows' sizes, Windows' close red,
     wired to the three things they say they do.
  6. THE MENU. It moved down a row rather than into a hamburger, so every menu
     that existed still exists and is still reachable through `menuBar()`.
  7. THE SYSTEM MENU. The app icon and Alt+Space.

HOW A NATIVE BEHAVIOUR IS TESTED WITHOUT A NATIVE WINDOW. The suite runs on the
offscreen platform (see conftest), where there is no HWND and `nativeEvent` is
never called, so nothing here can drive a Win32 message. That is why
ui/frameless.py is built the way it is: every decision the native path takes is
also a function that takes numbers, and `FramelessHelper.hit_test` answers the
same question `WM_NCHITTEST` asks, in Qt coordinates, on any platform. What is
asserted below is that decision. That the decision is DELIVERED on Windows was
checked against a real window, by sending WM_NCHITTEST to the live HWND; see the
report for this change.

The 150% scaling check is the one thing that cannot be faked in-process, because
Qt reads the scale factor once at startup, so it runs a second interpreter.
"""

import os
import subprocess
import sys
import textwrap

import fitz
import pytest

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QMenu, QMenuBar, QMessageBox

from core.settings import Settings, set_settings
from ui.frameless import (
    FramelessHelper, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT, HTCAPTION, HTCLIENT,
    HTLEFT, HTMAXBUTTON, HTRIGHT, HTSYSMENU, HTTOP, HTTOPLEFT, HTTOPRIGHT,
    RESIZE_BORDER, hit_test_code, resize_edges_at,
)
from ui.title_bar import (
    CAPTION_BUTTON_HEIGHT, CAPTION_BUTTON_WIDTH, CLOSE_HOVER, CLOSE_PRESSED,
    TITLE_BAR_HEIGHT,
)
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


def _pdf(tmp_path, name):
    path = tmp_path / name
    raw = fitz.open()
    page = raw.new_page(width=400, height=500)
    page.insert_text((20, 100), name, fontsize=24)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def window(qt_app, store, tmp_path):
    """A shown window with two documents in it, so the tab strip is up."""
    win = WindowRegistry.instance().create_window(show=False)
    win.resize(1200, 800)
    win.move(120, 120)
    win.show()
    win.open_paths([_pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")])
    yield win


def _press(widget, local: QPoint, kind=QMouseEvent.Type.MouseButtonPress):
    held = (Qt.MouseButton.NoButton
            if kind == QMouseEvent.Type.MouseButtonRelease
            else Qt.MouseButton.LeftButton)
    event = QMouseEvent(kind, QPointF(local),
                        QPointF(widget.mapToGlobal(local)),
                        Qt.MouseButton.LeftButton, held,
                        Qt.KeyboardModifier.NoModifier)
    return event


# ======================================================================
# 1. The order of the rows
# ======================================================================

def test_the_title_bar_hosts_the_document_tab_strip(window):
    """The same widget, not a copy of it. DocumentArea still owns every gesture
    on the bar; the title bar only holds it."""
    bar = window.title_bar()
    assert bar.tab_strip() is window.document_area().header()
    assert bar.tab_strip().isVisible()
    assert window.document_area().bar().parent() is not None
    # And it is genuinely inside the title bar, not merely referenced by it.
    assert bar.isAncestorOf(window.document_area().bar())


def test_nothing_in_the_window_sits_above_the_tabs(window):
    """The complaint this whole phase answers, as an assertion.

    Before: title bar, then the menu bar, then the tabs. After: the tabs ARE the
    title bar, and the menu bar is the row underneath.
    """
    title_bar = window.title_bar()
    strip = window.document_area().header()
    menu = window.menuBar()

    assert title_bar.mapTo(window, QPoint(0, 0)).y() == 0
    strip_y = strip.mapTo(window, QPoint(0, 0)).y()
    menu_y = menu.mapTo(window, QPoint(0, 0)).y()
    assert strip_y < menu_y, "the menu bar is still above the tabs"
    assert strip_y < TITLE_BAR_HEIGHT


def test_the_menu_is_still_reachable_where_it_was_put(window):
    """A row below the tabs, not a hamburger. Every menu still there, still a
    real QMenuBar, so Alt+F and the screen reader still work."""
    menu = window.menuBar()
    assert isinstance(menu, QMenuBar)
    titles = [m.title() for m in menu.findChildren(QMenu) if m.title()]
    assert titles == ["File", "Edit", "Page", "View", "Help"]
    assert menu.isVisible()
    # Asking twice returns the one that is on screen, rather than QMainWindow
    # quietly building a second empty one (which is what evicts the title bar).
    assert window.menuBar() is menu


# ======================================================================
# 2. Dragging the window by the strip
# ======================================================================

def test_dragging_empty_title_bar_space_moves_the_window(window, monkeypatch):
    moved = []
    monkeypatch.setattr(FramelessHelper, "start_move",
                        lambda self: moved.append(True) or True)
    bar = window.title_bar()
    gap = bar._drag_gap
    point = gap.mapTo(bar, QPoint(gap.width() // 2, gap.height() // 2))

    assert bar.is_drag_area(point)
    bar.mousePressEvent(_press(bar, point))
    assert moved == [True]


def test_empty_tab_strip_space_drags_and_a_tab_does_not(window, monkeypatch):
    """The Chrome rule. Past the last tab is strip; a tab is a tab.

    Getting this backwards is the classic frameless bug in both directions: make
    the whole row draggable and the tabs stop being clickable, make none of it
    draggable and a window with two documents open has almost nothing to grab.
    """
    moved = []
    monkeypatch.setattr(FramelessHelper, "start_move",
                        lambda self: moved.append(True) or True)
    title_bar = window.title_bar()
    tabs = window.document_area().bar()
    assert tabs.count() == 2

    on_tab = tabs.mapTo(title_bar, tabs.tabRect(0).center())
    assert not title_bar.is_drag_area(on_tab)
    title_bar.mousePressEvent(_press(title_bar, on_tab))
    assert moved == []

    past_last = tabs.tabRect(tabs.count() - 1).right() + 20
    if past_last < tabs.width():
        empty = tabs.mapTo(title_bar, QPoint(past_last, tabs.height() // 2))
        assert title_bar.is_drag_area(empty)
        title_bar.mousePressEvent(_press(title_bar, empty))
        assert moved == [True]


def test_the_buttons_on_the_strip_are_never_drag_area(window):
    """Every control on the row, checked at its own centre. One of these
    returning True is a button that cannot be pressed."""
    bar = window.title_bar()
    controls = [bar.new_tab_button()] + bar.controls().buttons()
    for widget in controls:
        centre = widget.mapTo(bar, widget.rect().center())
        assert not bar.is_drag_area(centre), f"{widget.objectName()} drags"


def test_double_click_on_bare_strip_maximises_then_restores(window):
    bar = window.title_bar()
    gap = bar._drag_gap
    point = gap.mapTo(bar, QPoint(gap.width() // 2, gap.height() // 2))

    assert not window.isMaximized()
    bar.mouseDoubleClickEvent(
        _press(bar, point, QMouseEvent.Type.MouseButtonDblClick))
    assert window.isMaximized()
    bar.mouseDoubleClickEvent(
        _press(bar, point, QMouseEvent.Type.MouseButtonDblClick))
    assert not window.isMaximized()


# ======================================================================
# 3. Resizing, from all eight directions
# ======================================================================

#: (name, the point in a 800x600 window, the code Windows must be told).
_EIGHT_WAYS = [
    ("top left", (1, 1), HTTOPLEFT),
    ("top", (400, 1), HTTOP),
    ("top right", (798, 1), HTTOPRIGHT),
    ("left", (1, 300), HTLEFT),
    ("right", (798, 300), HTRIGHT),
    ("bottom left", (1, 598), HTBOTTOMLEFT),
    ("bottom", (400, 598), HTBOTTOM),
    ("bottom right", (798, 598), HTBOTTOMRIGHT),
]


def test_every_edge_and_corner_is_a_resize_zone(subtests):
    """The eight zones, exhaustively. No window needed: this is the function the
    native hit test and the Qt fallback both go through."""
    for name, (x, y), code in _EIGHT_WAYS:
        with subtests.test(name):
            assert hit_test_code(800, 600, x, y) == code


def test_the_middle_of_the_window_is_not_a_resize_zone():
    assert hit_test_code(800, 600, 400, 300) == HTCLIENT
    assert hit_test_code(800, 600, RESIZE_BORDER + 20, 300) == HTCLIENT


def test_corners_are_a_bigger_target_than_the_edges_that_make_them():
    """Aiming at a corner is aiming at a point, and a point the size of the
    border is a point nobody hits."""
    just_inside = RESIZE_BORDER + 3
    assert hit_test_code(800, 600, just_inside, 1) == HTTOPLEFT
    assert hit_test_code(800, 600, 1, just_inside) == HTTOPLEFT
    # Far enough along the same edge and it is only that edge again.
    assert hit_test_code(800, 600, 200, 1) == HTTOP


def test_a_press_on_any_edge_starts_a_system_resize(window, subtests):
    """The Qt fallback path, which is what runs everywhere `WM_NCHITTEST` does
    not. What is checked is that the gesture is STARTED, and with the edges the
    point actually named."""
    helper = window.frameless_helper()
    started = []
    helper.start_resize = lambda edges: started.append(int(edges.value)) or True

    # The eight points again, on the real window rather than the 800x600 one the
    # table is written against: the same corner, the same edge, this window's
    # width and height.
    edge_x = {1: 1, 400: window.width() // 2, 798: window.width() - 2}
    edge_y = {1: 1, 300: window.height() // 2, 598: window.height() - 2}
    for name, (x, y), _code in _EIGHT_WAYS:
        with subtests.test(name):
            started.clear()
            local = QPoint(edge_x[x], edge_y[y])
            expected = resize_edges_at(window.width(), window.height(),
                                       local.x(), local.y())
            assert int(expected.value) != 0, f"{name} is not on the rim"
            taken = helper.eventFilter(window, _press(window, local))
            assert taken is True
            assert started == [int(expected.value)]


def test_a_maximised_window_has_no_resize_rim(window):
    """Dragging the edge of a maximised window resizes nothing on Windows, and
    an HTLEFT there would put a resize cursor on a border that cannot move."""
    window.showMaximized()
    helper = window.frameless_helper()
    assert helper.hit_test(QPoint(1, 300)) == HTCLIENT
    window.showNormal()


# ======================================================================
# 4. The window controls
# ======================================================================

def test_there_are_three_window_controls_at_windows_sizes(window):
    controls = window.title_bar().controls()
    assert [b.kind() for b in controls.buttons()] == \
        ["minimise", "maximise", "close"]
    for button in controls.buttons():
        assert button.width() == CAPTION_BUTTON_WIDTH
        assert button.height() == CAPTION_BUTTON_HEIGHT


def test_the_window_controls_are_wired_to_the_window(window):
    controls = window.title_bar().controls()

    controls.maximise_button().click()
    assert window.isMaximized()
    assert controls.maximise_button().glyph_name() == "restore"
    controls.maximise_button().click()
    assert not window.isMaximized()
    assert controls.maximise_button().glyph_name() == "maximise"

    controls.minimise_button().click()
    assert window.isMinimized()
    window.showNormal()

    # Close last, because it takes the window with it. Watched through the
    # signal AND through the result, since the signal alone would pass on a
    # button wired to nothing but a spy.
    fired = []
    controls.close_requested.connect(lambda: fired.append(True))
    controls.close_button().click()
    assert fired == [True]
    assert not window.isVisible()


def test_the_close_button_is_the_windows_red_and_the_others_are_not(window):
    controls = window.title_bar().controls()
    close = controls.close_button()
    minimise = controls.minimise_button()

    assert close.background() is None, "at rest it is bare"
    close.set_native_hover(True)
    assert close.background().name().upper() == CLOSE_HOVER.upper()
    assert close.glyph_colour().name().upper() == "#FFFFFF"
    close.set_native_pressed(True)
    assert close.background().name().upper() == CLOSE_PRESSED.upper()
    close.set_native_pressed(False)
    close.set_native_hover(False)

    minimise.set_native_hover(True)
    assert minimise.background().name().upper() != CLOSE_HOVER.upper()


def test_the_maximise_button_is_a_snap_layouts_target(window):
    """`HTMAXBUTTON` is the whole of what makes the Windows 11 Snap Layouts
    flyout appear when the pointer rests on the maximise button."""
    helper = window.frameless_helper()
    button = window.title_bar().maximise_button()
    centre = button.mapTo(window, button.rect().center())
    assert helper.hit_test(centre) == HTMAXBUTTON


def test_the_other_two_controls_stay_qt_s(window):
    """Only the maximise button is handed to Windows. Minimise and close keep
    Qt's own hover and press, which is why they are HTCLIENT."""
    helper = window.frameless_helper()
    for button in (window.title_bar().controls().minimise_button(),
                   window.title_bar().controls().close_button()):
        centre = button.mapTo(window, button.rect().center())
        assert helper.hit_test(centre) == HTCLIENT


def test_the_state_follows_the_window_and_not_only_the_button(window):
    """Win+Up, Aero Snap and a drag to the top of the screen all maximise
    without the button being touched, so the glyph follows the WINDOW."""
    button = window.title_bar().maximise_button()
    window.showMaximized()
    window.changeEvent(QEvent(QEvent.Type.WindowStateChange))
    assert button.glyph_name() == "restore"
    window.showNormal()
    window.changeEvent(QEvent(QEvent.Type.WindowStateChange))
    assert button.glyph_name() == "maximise"


# ======================================================================
# 5. The system menu, and the icon
# ======================================================================

def test_the_app_icon_is_the_system_menu(window):
    helper = window.frameless_helper()
    icon = window.title_bar()._icon
    centre = icon.mapTo(window, icon.rect().center())
    assert helper.hit_test(centre) == HTSYSMENU


def test_right_clicking_bare_strip_asks_for_the_system_menu(window):
    from PySide6.QtGui import QContextMenuEvent

    asked = []
    window.title_bar().system_menu_requested.connect(asked.append)
    bar = window.title_bar()
    gap = bar._drag_gap
    point = gap.mapTo(bar, QPoint(gap.width() // 2, gap.height() // 2))
    bar.contextMenuEvent(QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, point, bar.mapToGlobal(point)))
    assert len(asked) == 1


def test_alt_space_is_bound(window):
    """Windows would answer Alt+Space itself, through DefWindowProc, if Qt did
    not eat the key first. It does, so the binding has to be ours."""
    from PySide6.QtGui import QKeySequence, QShortcut

    bound = [s.key().toString() for s in window.findChildren(QShortcut)]
    assert QKeySequence("Alt+Space").toString() in bound


# ======================================================================
# 6. New tab, which is what the strip lost when it became the caption
# ======================================================================

def test_the_title_bar_has_a_new_tab_button(window):
    before = window.document_area().count()
    window.title_bar().new_tab_button().click()
    assert window.document_area().count() == before + 1
    window.document_area().check_invariant()


# ======================================================================
# 7. Scaling, which needs its own interpreter
# ======================================================================

_SCALED = textwrap.dedent(
    """
    import os
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = "1.5"
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from ui.title_bar import (CAPTION_BUTTON_HEIGHT, CAPTION_BUTTON_WIDTH,
                              TITLE_BAR_HEIGHT, WindowControls)
    controls = WindowControls()
    controls.resize(controls.sizeHint())
    button = controls.close_button()
    print(button.width(), button.height(),
          round(button.devicePixelRatioF(), 3),
          CAPTION_BUTTON_WIDTH, CAPTION_BUTTON_HEIGHT, TITLE_BAR_HEIGHT)
    """
)


def test_the_caption_buttons_keep_windows_metrics_at_150_percent(tmp_path):
    """Sizes are in LOGICAL pixels, so Qt scales them and the buttons come out
    at 69x48 device pixels at 150%, which is what Windows draws.

    Its own interpreter because Qt reads the scale factor once, at startup, and
    nothing after that can change it.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-c", _SCALED], capture_output=True, text=True,
        cwd=root, timeout=180)
    assert result.returncode == 0, result.stderr
    width, height, ratio, want_w, want_h, bar_h = result.stdout.split()
    assert (int(width), int(height)) == (int(want_w), int(want_h))
    assert float(ratio) == 1.5
    # And the row is still taller than the buttons standing in it.
    assert int(bar_h) >= int(want_h)
