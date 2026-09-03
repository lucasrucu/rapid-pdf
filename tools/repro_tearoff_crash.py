"""Reproduce the 0xC000041D tear-off crash on a REAL Windows display.

NEEDS A REAL DESKTOP, AND TAKES OVER THE MOUSE for about five seconds. It
drives genuine Windows input with SendInput, so the events arrive through the
real window procedure with a real SetCapture mouse grab in effect. That is the
whole point: the 31 offscreen tear-off tests all pass against the broken code,
because the offscreen platform has no window procedure, no native handles, no
mouse grab and no activation, so the boundary the crash lives on does not exist
in the harness. A green test run is not evidence about this crash. This is.

    .venv\\Scripts\\python.exe tools\\repro_tearoff_crash.py

The scenario, which is the one that empties a window mid-drag:

    window A  two documents          the tab is torn out of here
    window B  one EMPTY placeholder  adopting a document RETIRES the
                                     placeholder, so B is left holding exactly
                                     the tab being dragged
    window C  one document           dragging on to here empties B and closes
                                     it, from inside A's mouseMoveEvent

Exit codes:
    0            the gesture completed and the end state was right
    10           the gesture completed but a window object was already dead
    11           a Python exception escaped into a Qt handler
    12           the watchdog fired
    13           INCONCLUSIVE: the drag never engaged. Driving real input is
                 flaky when three windows are activated in quick succession,
                 and a drag that never started looks exactly like a drag that
                 survived, so it gets its own code instead of a false pass.
                 Run it again.
    -1073740771  0xC000041D, STATUS_FATAL_USER_CALLBACK_EXCEPTION: the bug.

Set RAPID_PDF_TRACE=1 to wrap every method of the windowing classes and write a
call trace to trace.log beside this script, plus a faulthandler dump to
fault.log. The last unmatched "->" in trace.log names the call that killed the
process, and fault.log is what identified the real cause: unbounded recursion
between MainWindow.nativeEvent and FramelessHelper.native_event, because
native_event asked winId() for a handle on a window that had just been closed
and winId() CREATES one, dispatching messages straight back into itself.
"""

import ctypes
import ctypes.wintypes as wt
import inspect
import os
import pathlib
import sys
import tempfile
import traceback

os.environ.pop("QT_QPA_PLATFORM", None)

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import fitz                                                        # noqa: E402
from PySide6.QtCore import QPoint, QTimer                          # noqa: E402
from PySide6.QtGui import QCursor, QGuiApplication                 # noqa: E402
from PySide6.QtWidgets import QApplication                         # noqa: E402

from ui.theme import apply_theme                                   # noqa: E402
from ui.window_registry import WindowRegistry                      # noqa: E402

user32 = ctypes.windll.user32
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def _send(flags, x=0, y=0):
    if flags & MOUSEEVENTF_ABSOLUTE:
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        x = int((x - vx) * 65535 / max(1, vw - 1))
        y = int((y - vy) * 65535 / max(1, vh - 1))
    inp = INPUT(type=0, mi=MOUSEINPUT(x, y, 0, flags, 0, None))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


DPR = [1.0]


def move_to(x, y):
    # Qt hands out LOGICAL pixels and SendInput takes PHYSICAL ones. Getting
    # this wrong lands the cursor short of the widget you aimed at and the
    # gesture silently never starts, which looks exactly like "fixed".
    _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
          int(round(x * DPR[0])), int(round(y * DPR[0])))


def left_down():
    _send(MOUSEEVENTF_LEFTDOWN)


def left_up():
    _send(MOUSEEVENTF_LEFTUP)


def say(*a):
    print(*a, flush=True)


def a_pdf(path, pages=2):
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()
    return str(path)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Rapid PDF")
    app.setOrganizationName("Lucas")
    app.setQuitOnLastWindowClosed(False)

    failed = {"code": None}

    def hook(kind, value, tb):
        say("!!! EXCEPTION ESCAPED INTO A QT HANDLER:\n"
            + "".join(traceback.format_exception(kind, value, tb)))
        failed["code"] = 11

    sys.excepthook = hook

    DPR[0] = float(QGuiApplication.primaryScreen().devicePixelRatio())
    say(f"device pixel ratio: {DPR[0]}")

    if os.environ.get("RAPID_PDF_TRACE"):
        _install_tracing()

    # Into the temp directory, not the repo: this is a tool that gets run by
    # hand and it should not leave three PDFs in tools/ behind it.
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="rapidpdf-repro-"))
    docs = [a_pdf(scratch / f"{n}.pdf") for n in ("alpha", "beta", "gamma")]

    theme = apply_theme(app)
    registry = WindowRegistry.instance()
    registry.set_theme(theme)

    win_a = registry.create_window(theme=theme, show=False)
    win_a.setGeometry(60, 80, 760, 460)
    win_a.show()
    win_a.open_paths(docs[:2])

    win_b = registry.create_window(theme=theme, show=False)
    win_b.setGeometry(60, 600, 760, 300)
    win_b.show()

    win_c = registry.create_window(theme=theme, show=False)
    win_c.setGeometry(900, 80, 760, 460)
    win_c.show()
    win_c.open_paths(docs[2:])

    for _ in range(60):
        app.processEvents()

    say(f"A tabs={win_a.document_area().count()} "
        f"B tabs={win_b.document_area().count()} "
        f"C tabs={win_c.document_area().count()}")
    say("B front is an empty placeholder: "
        f"{win_b.document_area().view_at(0).is_empty()}")

    def bar_point(window, index=0):
        bar = window.document_area().bar()
        return bar.mapToGlobal(bar.tabRect(index).center())

    def body_point(window):
        return window.mapToGlobal(
            QPoint(window.width() // 2, window.height() // 2))

    start = bar_point(win_a, 0)
    bar = win_a.document_area().bar()
    below = bar.mapToGlobal(QPoint(bar.rect().center().x(), bar.rect().bottom()))

    path = [
        ("move", start),
        ("down", None),
        ("move", QPoint(start.x(), start.y() + 12)),
        ("move", QPoint(start.x(), below.y() + 30)),
        ("move", QPoint(start.x(), below.y() + 70)),     # past DETACH_MARGIN
        ("mark", "torn"),
        ("move", QPoint(start.x() + 20, below.y() + 160)),
        ("move", body_point(win_b) - QPoint(0, 40)),
        ("move", body_point(win_b)),                      # docks into empty B
        ("mark", "in-b"),
        ("move", body_point(win_b) + QPoint(30, 0)),
        ("cross", None),                                  # on to C's strip
        ("mark", "in-c"),
        # Re-aimed immediately before the button comes up. SendInput is
        # asynchronous, so the cursor can still be settling a tick after the
        # move that emptied B, and releasing off the strip makes a new window
        # instead of dropping into C, which reads like a bug and is not one.
        ("cross", None),
        ("up", None),
    ]

    step = {"i": 0}
    timer = QTimer()
    timer.setInterval(120)

    def finish(code, why):
        say(f"--- {why} (exit {code})")
        timer.stop()
        try:
            left_up()
        except Exception:
            pass
        app.exit(code)

    def tick():
        if step["i"] >= len(path):
            try:
                counts = [w.document_area().count()
                          for w in (win_a, win_b, win_c)]
            except RuntimeError as exc:
                finish(10, f"a window object was already dead: {exc}")
                return
            say(f"end state: A={counts[0]} B={counts[1]} C={counts[2]} "
                f"registry={registry.count()}")
            if failed["code"]:
                finish(failed["code"], "an exception escaped a Qt handler")
                return
            # DID THE GESTURE ACTUALLY HAPPEN? Driving real input is flaky
            # when three windows are created and activated in quick
            # succession, and a drag that never engaged looks identical to a
            # drag that survived. It proves nothing either way, so it gets its
            # own exit code rather than being counted as a pass.
            if counts[0] != 1 or counts[2] != 2:
                finish(13, "INCONCLUSIVE: the drag never engaged, so this run "
                           "says nothing about the crash. Run it again.")
                return
            finish(0, "gesture completed without a crash, and landed correctly")
            return

        kind, arg = path[step["i"]]
        step["i"] += 1
        try:
            if kind == "move":
                move_to(arg.x(), arg.y())
            elif kind == "down":
                left_down()
            elif kind == "up":
                left_up()
            elif kind == "mark":
                tear = win_a.document_area().bar()._tear_off
                say(f"    [{arg}] A={win_a.document_area().count()} "
                    f"B={win_b.document_area().count()} "
                    f"C={win_c.document_area().count()} "
                    f"registry={registry.count()} "
                    f"dragging={tear.is_dragging()} cursor={QCursor.pos()}")
            elif kind == "cross":
                # Computed late: B is holding the document by now, and C's
                # strip is where the fatal move_view_to_window lands.
                target = bar_point(win_c, 0)
                say(f"    aiming at C strip {target.x()},{target.y()}")
                for dx in range(5):
                    move_to(target.x() - (4 - dx) * 6, target.y())
        except RuntimeError as exc:
            say(f"!!! RuntimeError driving step {kind}: {exc}")
            finish(10, "dead C++ object mid-gesture")

    timer.timeout.connect(tick)
    timer.start()

    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: finish(12, "watchdog"))
    watchdog.start(40000)

    say("--- driving the drag now")
    return app.exec()


def _install_tracing():
    """Wrap every method of the windowing classes, writing to trace.log.

    Unbuffered, because the process dies without unwinding and a buffered
    write is a write that never happened.
    """
    import faulthandler
    faulthandler.enable(open(HERE / "fault.log", "wb", buffering=0))
    log = open(HERE / "trace.log", "wb", buffering=0)

    def wrap(cls, name):
        original = getattr(cls, name)

        def wrapper(self, *a, **kw):
            log.write(f"  -> {cls.__name__}.{name}\n".encode())
            try:
                return original(self, *a, **kw)
            finally:
                log.write(f"  <- {cls.__name__}.{name}\n".encode())
        setattr(cls, name, wrapper)

    def wrap_all(cls):
        for name, value in list(vars(cls).items()):
            # ONLY plain functions. Qt Signals are callable class attributes
            # and replacing one with a wrapper breaks every connect() on it.
            if name.startswith("__") or not inspect.isfunction(value):
                continue
            try:
                wrap(cls, name)
            except Exception:
                pass

    from ui.canvas import PDFCanvas
    from ui.document_area import DocumentArea, DocumentTabBar
    from ui.document_view import DocumentView
    from ui.main_window import MainWindow
    from ui.tab_tear_off import TabTearOff
    from ui.title_bar import TitleBar
    from ui.window_registry import WindowRegistry as Registry

    for cls in (MainWindow, DocumentArea, DocumentTabBar, TabTearOff,
                DocumentView, Registry, PDFCanvas, TitleBar):
        wrap_all(cls)
    wrap(MainWindow, "close")   # QWidget's, not redefined by MainWindow


if __name__ == "__main__":
    sys.exit(main())
