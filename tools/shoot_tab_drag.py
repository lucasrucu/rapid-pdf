"""Drive a real tab drag with real mouse input and photograph the result.

WHY THIS EXISTS. The test suite runs under QT_QPA_PLATFORM=offscreen, forced in
tests/conftest.py, and offscreen has no window procedure, no compositor and no
z-order. It will happily tell you that `ghost_index()` names a tab, or that
`drop_indicator()` is set, while nothing whatsoever is on screen, so a green
suite is not evidence that drag feedback is visible. Two defects in one day got
through exactly that gap.

WHAT IT IS PHOTOGRAPHING IS A GHOST. Nothing is torn out until the button comes
up: a carried tab is a tab-sized picture following the cursor, the strip it is
near parts around it and paints the arriving tab dimmed, and the window is only
built on the release. So the shots are, in order, the strip at rest, the ghost
in mid-air, the target strip parted with the ghost slot open, and the tab
landed. See ui/tab_tear_off.py.

So this does the two things the suite cannot. It drives the gesture through
Win32 SendInput, which is the same real input path a hand produces, and it
grabs the COMPOSITED DESKTOP rather than asking a widget to paint itself into a
pixmap. `QWidget.grab()` would prove only that the paint code runs; grabbing the
screen proves the pixels reached it, past the frameless chrome, the drag ghost
and the window manager.

It deliberately does NOT call `winId()` on anything. `QScreen.grabWindow(0)` is
the whole desktop and needs no handle from us. See ui/frameless.py: creating a
native handle from the wrong place is what produced today's 0xC000041D.

Usage:  .venv\\Scripts\\python.exe tools\\shoot_tab_drag.py <out_dir>

Exit codes:  0 the drag engaged, landed, and the feedback was up when
                photographed
             13 INCONCLUSIVE, the drag never engaged, so the run says nothing
             12 watchdog
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from ui.theme import ThemeMode, apply_theme
from ui.window_registry import WindowRegistry

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

user32 = ctypes.windll.user32
DPR = [1.0]


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
    mi = MOUSEINPUT(x, y, 0, flags, 0, None)
    inp = INPUT(0, INPUT._U(mi))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def move_to(x, y):
    # Qt hands out LOGICAL pixels, SendInput takes PHYSICAL ones. Getting this
    # wrong lands the cursor short of the widget and the gesture silently never
    # starts, which looks exactly like "the feature is broken".
    span_x = user32.GetSystemMetrics(78) or 1
    span_y = user32.GetSystemMetrics(79) or 1
    px = int(round(x * DPR[0]))
    py = int(round(y * DPR[0]))
    _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
          int(px * 65535 / span_x), int(py * 65535 / span_y))


def left_down():
    _send(MOUSEEVENTF_LEFTDOWN)


def left_up():
    _send(MOUSEEVENTF_LEFTUP)


def say(*a):
    print(*a, flush=True)


def a_pdf(path, pages=2):
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 120), path.stem, fontsize=28)
    doc.save(str(path))
    doc.close()
    return str(path)


def main():
    if len(sys.argv) < 2:
        say(__doc__)
        return 2
    out_dir = pathlib.Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Rapid PDF")
    app.setOrganizationName("Lucas")
    app.setQuitOnLastWindowClosed(False)

    DPR[0] = float(QGuiApplication.primaryScreen().devicePixelRatio())
    say(f"device pixel ratio: {DPR[0]}")

    scratch = pathlib.Path(tempfile.mkdtemp(prefix="rapidpdf-shots-"))
    docs = [a_pdf(scratch / f"{n}.pdf")
            for n in ("alpha", "bravo", "charlie", "delta", "echo")]

    theme = apply_theme(app)
    registry = WindowRegistry.instance()
    registry.set_theme(theme)

    win_a = registry.create_window(theme=theme, show=False)
    win_a.setGeometry(80, 90, 900, 420)
    win_a.show()
    win_a.open_paths(docs[:3])

    win_b = registry.create_window(theme=theme, show=False)
    win_b.setGeometry(80, 580, 900, 380)
    win_b.show()
    win_b.open_paths(docs[3:])

    for _ in range(80):
        app.processEvents()

    def bar_point(window, index=0):
        bar = window.document_area().bar()
        return bar.mapToGlobal(bar.tabRect(index).center())

    def shoot(tag):
        app.processEvents()
        shot = QGuiApplication.primaryScreen().grabWindow(0)
        path = out_dir / f"{tag}.png"
        shot.save(str(path), "PNG")
        say(f"    wrote {path}")

    state = {"mode": ThemeMode.LIGHT, "ok": {}, "phase": 0}

    def feedback_of(window):
        """(ghost tab index, insertion line x, parted slot rect) for that strip.

        THE STRIP PARTS FOR BOTH GESTURES NOW, and which field answers says
        which one is in flight. A carried TAB joins the target strip live, so
        it is a real tab painted as a ghost and `ghost_index` names it. A
        carried WINDOW has not joined anything yet, so the strip reserves a
        tab's width and paints an empty slot, and `ghost_slot_rect` is the one
        with the answer.

        The line is the third value and nothing sets it during a drag any more.
        It is kept as a live primitive rather than deleted, and read here, so
        that a regression which brings it back is photographed rather than
        passing quietly.

        The wash and the outline that used to come back alongside are both
        gone; they read as a highlight box around the tab on a strip that hugs
        its tabs.
        """
        bar = window.document_area().bar()
        # Three now, not two. A carried TAB sets ghost_index; a carried WINDOW
        # parts the strip and sets ghost_slot_rect instead, because its tab has
        # not joined the target yet. drop_indicator stays a live primitive but
        # nothing sets it during a drag any more, so photographing only the
        # first two would catch nothing on a whole-window gesture.
        return bar.ghost_index(), bar.drop_indicator(), bar.ghost_slot_rect()

    def run_pass(mode, done):
        """One full drag in one theme, photographed at the moment of truth."""
        theme.set_mode(mode)
        for _ in range(40):
            app.processEvents()
        name = mode.value
        say(f"--- {name}: driving the drag")

        start = bar_point(win_a, 0)
        bar = win_a.document_area().bar()
        below = bar.mapToGlobal(QPoint(bar.rect().center().x(),
                                       bar.rect().bottom()))
        target = bar_point(win_b, 0)
        # Read before the button goes down, so the landing probe has something
        # to compare against. A tab joins the target strip LIVE, on approach,
        # so B is already up by one by the time the drop happens; what the
        # release has to leave behind is that move made permanent, with the
        # feedback off both strips and the grab given back.
        counts = (win_a.document_area().count(), win_b.document_area().count())

        script = [
            ("shot", f"{name}-1-before"),
            ("move", start),
            ("down", None),
            ("move", QPoint(start.x(), start.y() + 12)),
            ("move", QPoint(start.x(), below.y() + 40)),
            ("move", QPoint(start.x(), below.y() + 90)),
            ("shot", f"{name}-2-ghost-in-flight"),
            ("move", QPoint(target.x() + 140, target.y() - 120)),
            ("aim", None),
            ("probe", name),
            ("shot", f"{name}-3-over-target-strip"),
            ("up", None),
            ("landed", name),
            ("shot", f"{name}-4-after-drop"),
        ]

        step = {"i": 0}
        timer = QTimer()
        timer.setInterval(140)

        def tick():
            if step["i"] >= len(script):
                timer.stop()
                done()
                return
            kind, arg = script[step["i"]]
            step["i"] += 1
            try:
                if kind == "move":
                    move_to(arg.x(), arg.y())
                elif kind == "down":
                    left_down()
                elif kind == "up":
                    left_up()
                elif kind == "shot":
                    shoot(arg)
                elif kind == "aim":
                    # Recomputed late: B has already adopted the tab by now, so
                    # its strip has reflowed and the old point is stale.
                    #
                    # Aimed at the SECOND tab, not the first. A drop at index 0
                    # puts the insertion line hard against the frame, which is
                    # the one position that proves least about whether the line
                    # is legible. Landing it between two tabs is the case worth
                    # photographing.
                    area = win_b.document_area()
                    spot = bar_point(win_b, min(1, max(0, area.count() - 1)))
                    for dx in range(5):
                        move_to(spot.x() - (4 - dx) * 8, spot.y())
                elif kind == "probe":
                    ghost, line, slot = feedback_of(win_b)
                    tear = win_a.document_area().bar()._tear_off
                    say(f"    [{arg}] dragging={tear.is_dragging()} "
                        f"A={win_a.document_area().count()} "
                        f"B={win_b.document_area().count()} "
                        f"B.ghost_index={ghost} B.drop_indicator={line} B.slot={slot} "
                        f"carrying_ghost={tear.ghost() is not None} "
                        f"cursor={QCursor.pos()}")
                    # A TAB is being carried here, so the ghost slot is the
                    # feedback that has to be up. The line belongs to the
                    # whole-window merge and would be wrong on this path.
                    state["ok"][arg] = ghost is not None
                elif kind == "landed":
                    # THE EXIT CODE CLAIMS THE DRAG LANDED, so something has to
                    # ask. Nothing did until now, and a tool that reports on a
                    # thing it never checked is worse than one that says less.
                    app.processEvents()
                    tear = win_a.document_area().bar()._tear_off
                    a_now = win_a.document_area().count()
                    b_now = win_b.document_area().count()
                    ghost, line, slot = feedback_of(win_b)
                    _, a_line = feedback_of(win_a)
                    landed = (not tear.is_dragging()
                              and tear.ghost() is None
                              and a_now == counts[0] - 1
                              and b_now == counts[1] + 1
                              and ghost is None and line is None
                              and slot.isEmpty() and a_slot.isEmpty()
                              and a_line is None
                              and QWidget.mouseGrabber() is None)
                    say(f"    [{arg}] landed: dragging={tear.is_dragging()} "
                        f"A={a_now} (was {counts[0]}) "
                        f"B={b_now} (was {counts[1]}) "
                        f"B.ghost_index={ghost} B.drop_indicator={line} B.slot={slot} "
                        f"grabber={QWidget.mouseGrabber()}")
                    state["ok"][f"{arg}-landed"] = landed
            except RuntimeError as exc:
                say(f"!!! RuntimeError at step {kind}: {exc}")
                timer.stop()
                done()

        timer.timeout.connect(tick)
        timer.start()
        state["timer"] = timer

    def after_light():
        # Put the tab back so the dark pass starts from the same board.
        try:
            view = win_b.document_area().view_at(0)
            win_b.move_view_to_window(view, win_a, 0)
        except Exception as exc:
            say(f"    could not reset between passes: {exc}")
        for _ in range(40):
            app.processEvents()
        run_pass(ThemeMode.DARK, after_dark)

    def after_dark():
        say("--- done")
        good = state["ok"]
        say(f"feedback was up when photographed: {good}")
        left_up()
        app.exit(0 if good and all(good.values()) else 13)

    QTimer.singleShot(400, lambda: run_pass(ThemeMode.LIGHT, after_light))

    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (left_up(), app.exit(12)))
    watchdog.start(60000)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
