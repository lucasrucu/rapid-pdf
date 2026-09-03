"""The 0xC000041D tear-off crash, and the class of defect behind it.

WHAT THE CRASH WAS. Drag a tab into a window whose only tab is an empty
placeholder. `MainWindow.adopt` retires the placeholder, so that window is now
holding exactly the tab being dragged. Keep the button down and drag it on to a
third window: the second window empties and closes, and it does so from inside
`DocumentTabBar.mouseMoveEvent`, which Windows is running as a callback out of
the window procedure with the mouse captured.

Closing the window destroys its native handle. The next message delivered for
it reached `FramelessHelper.native_event`, which asked `self._window.winId()`
for the handle to compare against. `QWidget.winId()` CREATES the native window
when the widget does not have one, and creating an HWND dispatches WM_NCCREATE
and WM_NCCALCSIZE synchronously back into the window procedure, which came
straight back into `native_event`, which called `winId()` again. The stack ran
out, and Windows reports a stack overflow inside a window procedure as
STATUS_FATAL_USER_CALLBACK_EXCEPTION, 0xC000041D: no traceback, no dialog, the
window simply vanishes. That is the code in the user's Application event log.

WHAT THESE TESTS CAN AND CANNOT DO, SAID PLAINLY.

None of them can fail on the 0xC000041D itself. The offscreen platform this
suite runs on has no window procedure, no native handles, no mouse grab and no
activation, so the boundary the crash lives on does not exist here. `on_windows()`
is False under offscreen, so `native_event` returns None on its first line and
never reaches the handle lookup at all. A green run of this file is NOT
evidence that the crash is gone.

What they do instead is fail on the three CAUSES, each of which is visible
without a display:

  1. the source-level rule that nothing on the message path may call `winId()`,
     checked by reading the module rather than by running it. This is the one
     that would have caught the real bug, and it fails against the old code.
  2. `_hwnd()` returning 0 rather than conjuring a handle.
  3. the two window mutations that used to happen inside a mouse-move dispatch
     now being deferred off it.

The crash itself is reproduced by `tools/repro_tearoff_crash.py`, which needs a
real display and drives real Windows input. Before the fix it killed the
process with 0xC000041D on every run; after it, it completes cleanly. Run that,
not this, to check the crash.
"""

import ast
import pathlib

import fitz
import pytest

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from core.settings import Settings, set_settings
from ui.frameless import FramelessHelper
from ui.main_window import MainWindow
from ui.window_registry import WindowRegistry

REPO = pathlib.Path(__file__).resolve().parent.parent


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


def a_pdf(path, pages=2):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()
    return str(path)


# ----------------------------------------------------------------------
# 1. The rule: nothing on the message path may call winId()
# ----------------------------------------------------------------------

def test_frameless_only_calls_winid_where_it_means_to_create_a_handle():
    """`winId()` has a side effect, so it is banned outside window setup.

    THIS IS THE TEST THAT WOULD HAVE CAUGHT IT, and it is a source check on
    purpose. The defect is not a wrong value, it is a side effect: `winId()`
    creates a native window when there is not one, and every use of it in this
    module except `_ensure_native` sits on a path that Windows calls INTO the
    process to run. Asking whether the call is there is a question about the
    text, and the text is readable everywhere, including on the offscreen
    platform where the behaviour is unreachable.

    `FramelessHelper.apply` is the single legitimate caller: it runs once, at
    window setup, right after the frameless flag is set, and forcing the handle
    into existence so the style bits can be put on it is the entire point of
    it.
    """
    source = (REPO / "ui" / "frameless.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "winId"):
                offenders.append((node.name, inner.lineno))

    unexpected = [(name, line) for name, line in offenders
                  if name != "apply"]
    assert not unexpected, (
        "winId() creates a native window as a side effect, and these calls are "
        "on paths Windows dispatches into. Use _hwnd(), which wraps "
        "internalWinId() and returns 0 when there is no handle: "
        f"{unexpected}"
    )
    assert offenders, (
        "the setup call in FramelessHelper.apply went missing, so this test is "
        "now vacuous and would pass against anything"
    )


# ----------------------------------------------------------------------
# 2. _hwnd() answers without conjuring a handle
# ----------------------------------------------------------------------

def test_hwnd_returns_zero_and_creates_nothing(qt_app):
    """A widget with no native handle must stay that way after being asked."""
    widget = QWidget()
    helper = FramelessHelper(widget)

    assert int(widget.internalWinId() or 0) == 0, "precondition: no handle yet"
    assert helper._hwnd() == 0
    assert int(widget.internalWinId() or 0) == 0, (
        "asking for the handle created one, which is the side effect that "
        "recursed the window procedure to death"
    )
    widget.deleteLater()


def test_native_event_ignores_a_window_with_no_handle(qt_app):
    """The message path answers "not ours" rather than making a window.

    Reaches `_hwnd` only because `_native` is forced on; under offscreen the
    real `native_event` returns at its first line, which is exactly why this
    file says out loud that it cannot fail on the crash itself.
    """
    widget = QWidget()
    helper = FramelessHelper(widget)
    helper._native = True

    assert helper.native_event(b"windows_generic_MSG", 0) is None
    assert int(widget.internalWinId() or 0) == 0
    widget.deleteLater()


# ----------------------------------------------------------------------
# 3. The two window mutations are off the native event stack
# ----------------------------------------------------------------------

def test_emptied_window_does_not_close_inside_the_move(qt_app, store, tmp_path):
    """`move_view_to_window` must not close the source on its own stack.

    The caller is `TabTearOff._attach_to_strip`, running inside
    `mouseMoveEvent`. A window closing there is a window being destroyed
    underneath the message dispatch it is still inside. The close is deferred
    to the next pass of the event loop instead, so this asserts on the SEAM:
    still open when the call returns, gone once the loop has run.
    """
    WindowRegistry.instance().quit_on_last_window = False
    source = MainWindow()
    target = MainWindow()
    source.show()
    target.show()
    source.open_paths([a_pdf(tmp_path / "one.pdf")])

    view = source.document_area().view_at(0)
    assert source.document_area().count() == 1

    assert source.move_view_to_window(view, target) is True
    assert source.document_area().count() == 0

    assert source.isVisible(), (
        "the emptied window closed synchronously, inside the mouse-move "
        "dispatch that called this. That is the 0xC000041D."
    )

    qt_app.processEvents()

    assert not source.isVisible(), (
        "the deferred close never happened, so the emptied window is stranded "
        "on screen"
    )
    assert target.document_area().count() == 1
    target.close()


def test_retiring_a_view_never_promotes_it_to_a_top_level(qt_app, store, tmp_path):
    """`remove_view` must not `setParent(None)` on the way to deleting.

    On Windows that promotes the widget to a top-level with a real HWND and the
    delete behind it destroys that native window again. `remove_view` runs
    inside a mouse-move dispatch on the ordinary tear-off path, because
    dropping a tab into a window whose only tab is an empty placeholder retires
    the placeholder through here.
    """
    WindowRegistry.instance().quit_on_last_window = False
    window = MainWindow()
    window.show()
    window.open_paths([a_pdf(tmp_path / "a.pdf"), a_pdf(tmp_path / "b.pdf")])
    area = window.document_area()
    assert area.count() == 2

    victim = area.view_at(0)
    area.remove_view(0)

    assert area.count() == 1
    assert victim.parent() is not None, (
        "the removed view was promoted to a top-level widget, which on Windows "
        "means a native window was created and then destroyed inside a mouse "
        "handler"
    )
    window.close()


def test_placeholder_window_survives_adopting_and_giving_up_a_tab(
        qt_app, store, tmp_path):
    """The exact window sequence the crash needed, driven through the API.

    Not a drag: offscreen cannot deliver one. This is the state machine the
    drag walks, which is what makes the deferral assertions above meaningful,
    and it pins the placeholder replacement that empties the middle window.
    """
    WindowRegistry.instance().quit_on_last_window = False
    a = MainWindow()
    b = MainWindow()          # comes up holding one empty placeholder
    c = MainWindow()
    for w in (a, b, c):
        w.show()
    a.open_paths([a_pdf(tmp_path / "x.pdf"), a_pdf(tmp_path / "y.pdf")])
    c.open_paths([a_pdf(tmp_path / "z.pdf")])

    assert b.document_area().count() == 1
    assert b.document_area().view_at(0).is_empty()

    view = a.document_area().view_at(0)
    assert a.move_view_to_window(view, b, 0) is True
    # The placeholder was retired, so b holds exactly the arriving document.
    assert b.document_area().count() == 1
    assert b.document_area().view_at(0) is view

    # And now b empties, which is the close that used to kill the process.
    assert b.move_view_to_window(view, c, 0) is True
    assert b.document_area().count() == 0
    assert b.isVisible(), "b closed on the calling stack"

    qt_app.processEvents()

    assert not b.isVisible()
    assert a.document_area().count() == 1
    assert c.document_area().count() == 2
    a.close()
    c.close()
