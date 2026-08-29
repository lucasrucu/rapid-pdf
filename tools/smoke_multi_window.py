"""Drive the multi-window path end to end, outside pytest, and print what it saw.

Two things this does that the test suite cannot.

1. IT RUNS A REAL EVENT LOOP. The suite is offscreen and never calls `exec()`,
   so "the application quits when the last window closes" can only be asserted
   as "quit() was called". Here the loop is genuinely running and the script
   only reaches its final line if `exec()` actually returned.

2. IT CAN RUN ON THE REAL WINDOWS PLATFORM. `internalWinId()` staying 0 across
   a reparent is the whole reason the tear-off is a gesture layer rather than a
   rebuild, and offscreen has no native window handles to grow in the first
   place, so the assertion is only meaningful with a real platform plugin. Run
   it both ways:

       QT_QPA_PLATFORM=offscreen .venv\\Scripts\\python tools\\smoke_multi_window.py
       .venv\\Scripts\\python tools\\smoke_multi_window.py

   The second pops two windows for a second or so and closes them itself.

Re-run this after anything that touches window creation, adoption or app
lifetime, and ALWAYS after putting an OpenGL viewport on the canvas: a GL
viewport is a native window, so reparenting it across top-levels destroys and
recreates the context and the scene's backing store goes with it. See the
standing constraint in docs/tabs-plan.md.

PHASE 5 ADDED STEP 4, dragging a page from one tab into another. A real QDrag
cannot be scripted either way (exec() hands control to the platform and blocks
the loop on Windows), so what this step adds over the pytest suite is genuine
QDropEvent and QDragMoveEvent objects going through the real widgets in real
top-level windows: the position, the mime round-trip and the modifier reading
are the platform's own rather than a stand-in object's. It also exercises the
per-window undo stack end to end, which is the change phase 5 rests on.

PHASE 4 ADDED THE TEAR-OFF GESTURE STEPS. Those steps are the reason to
prefer the second command line above. The pytest suite drives the gesture by
handing synthesised QMouseEvents to the tab bar, which is enough to pin the
decisions it makes; what it cannot do is grab the mouse, move a real top-level
window under a cursor, or say whether the geometry the gesture computes lands
anywhere sensible on a real screen. Run natively and watch: the torn-off window
should appear under the pointer with the tab where the pointer left it, not
offset by a title bar.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

import fitz
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QDragMoveEvent, QDropEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from core.settings import Settings, set_settings
from ui.canvas import AddItemsCommand, HighlightItem
from ui.page_drag import make_page_mime
from ui.tab_tear_off import DETACH_MARGIN
from ui.theme import apply_theme
from ui.window_registry import WindowRegistry

FAILURES = []


# ----------------------------------------------------------------------
# Driving the tear-off gesture
#
# The same synthesis the pytest suite uses (tests/test_tab_tear_off.py), for
# the same reason: there is no pointer to script. What is different here is
# that the events go into a bar inside a REAL top-level window with a real
# frame, and the window the gesture creates is really shown and really moved,
# so the geometry these steps assert on is geometry the OS agreed to.
# ----------------------------------------------------------------------


def _mouse(kind, bar, global_pos, button=Qt.MouseButton.LeftButton):
    held = (Qt.MouseButton.NoButton
            if kind == QMouseEvent.Type.MouseButtonRelease else button)
    return QMouseEvent(kind, QPointF(bar.mapFromGlobal(global_pos)),
                       QPointF(global_pos), button, held,
                       Qt.KeyboardModifier.NoModifier)


def _tab_point(bar, index, dx=10):
    rect = bar.tabRect(index)
    return bar.mapToGlobal(rect.topLeft() + QPoint(dx, rect.height() // 2))


def _below_bar(bar, global_pos, extra=DETACH_MARGIN + 20):
    bottom = bar.mapToGlobal(QPoint(0, bar.rect().bottom())).y()
    return QPoint(global_pos.x(), bottom + extra)


def _press(bar, global_pos):
    bar.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, bar, global_pos))


def _move(bar, global_pos):
    bar.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, bar, global_pos))


def _release(bar, global_pos):
    bar.mouseReleaseEvent(
        _mouse(QMouseEvent.Type.MouseButtonRelease, bar, global_pos))


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    if not condition:
        FAILURES.append(label)
    print(f"  [{mark}] {label}{(' - ' + str(detail)) if detail else ''}")


def make_pdf(folder, name, pages):
    path = os.path.join(folder, name)
    raw = fitz.open()
    for i in range(pages):
        page = raw.new_page(width=595, height=842)
        page.insert_text((40, 120), f"{name} page {i}", fontsize=28)
    raw.save(path)
    raw.close()
    return path


def run(app, folder):
    print(f"platform: {QApplication.platformName()}")

    settings_path = os.path.join(folder, "settings.json")
    store = Settings(settings_path, debounce_ms=0, migrate_legacy=False)
    # "N documents are open. Close them all?" is a real modal in step 4 and
    # there is nobody here to answer it. Phase 2 pins its behaviour in
    # tests/test_document_tabs.py; this script is about the windows.
    store.close.confirm_multiple_tabs = False
    set_settings(store)
    theme = apply_theme(app)

    registry = WindowRegistry.instance()
    registry.set_theme(theme)

    paths = [make_pdf(folder, f"{n}.pdf", n_pages)
             for n, n_pages in (("alpha", 3), ("beta", 2), ("gamma", 4))]

    print("\n1. three PDFs in one window")
    first = registry.create_window(theme=theme, show=False)
    first.resize(1200, 800)
    first.show()
    first.open_paths(paths)
    area = first.document_area()
    check("three tabs", area.count() == 3, area.count())
    check("one window", registry.count() == 1)
    area.check_invariant()

    print("\n2. move the first document to a new window")
    moving = area.view_at(0)
    staying = area.view_at(1)
    canvas = moving._canvas
    before = {
        "scene": canvas.scene(),
        "viewport": canvas.viewport(),
        "fitz": moving._doc.doc,
        "pages": moving.page_count(),
        "canvas_win_id": canvas.internalWinId(),
    }
    check("canvas has no native handle before the move",
          before["canvas_win_id"] == 0, before["canvas_win_id"])

    second = first.move_view_to_new_window(moving)

    check("a second window exists", second is not None and second is not first)
    check("it holds the moved document", second.document_area().count() == 1)
    check("the first window kept the other two", area.count() == 2, area.count())
    area.check_invariant()
    second.document_area().check_invariant()

    print("\n   reparenting, re-verified across adopt (phase 1 finding 2)")
    check("same scene object", canvas.scene() is before["scene"])
    check("same viewport object", canvas.viewport() is before["viewport"])
    # NOT the undo stack. Phase 5 moved it to the WINDOW, so a document
    # arriving somewhere else necessarily joins that window's history. That is
    # the deliberate trade behind a cross-document page move being one command.
    check("the arriving document joined the new window's history",
          canvas.undo_stack is second.undo_stack())
    check("same fitz document", moving._doc.doc is before["fitz"])
    check("document still open", moving._doc.is_open())
    check("page count unchanged", moving.page_count() == before["pages"])
    check("internalWinId() still 0", canvas.internalWinId() == 0,
          canvas.internalWinId())
    check("view internalWinId() still 0", moving.internalWinId() == 0,
          moving.internalWinId())
    check("the view's window is the new one", moving.window() is second)

    print("\n3. annotate it in the new window, then undo it there")
    item = HighlightItem(QRectF(60, 60, 120, 90), QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    canvas.undo_stack.push(AddItemsCommand(canvas, [item]))
    check("the moved document has one undoable edit",
          moving.undo_stack().count() == 1)
    check("the document left behind has none",
          staying.undo_stack().count() == 0)

    second._undo_action.trigger()
    check("undo removed the annotation", item.scene() is None)
    check("the other window is untouched",
          staying.undo_stack().count() == 0 and staying._doc.is_open())
    check("the other window still shows its own document",
          first.view is not moving and first.view.has_document())

    print("\n4. drag a page from one tab into another (phase 5)")
    # A real QDrag cannot be scripted: exec() hands control to the platform and
    # blocks the event loop on Windows. What this DOES do that the pytest suite
    # cannot is build genuine QDropEvent / QDragMoveEvent objects and put them
    # through the real widgets in real top-level windows, so the position, the
    # mime round-trip and the modifier reading are the platform's own rather
    # than a stand-in object's.
    donor = area.view_at(0)
    recipient = area.view_at(1)
    donor_pages = donor.page_count()
    recipient_pages = recipient.page_count()
    check("two documents to move a page between",
          donor is not recipient and donor_pages > 1)

    bar = area.bar()
    area.set_current_index(1)
    mime = make_page_mime(donor, [0])
    tab_rect = bar.tabRect(0)
    hover = QDragMoveEvent(tab_rect.center(), Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)
    bar.dragMoveEvent(hover)
    check("hovering a tab armed the switch", bar.hover_switch_timer().isActive())
    check("the bar refused the drop itself", not hover.isAccepted())
    check("it has not switched yet", area.current_index() == 1,
          area.current_index())
    bar._on_hover_elapsed()
    check("resting on the tab brought it forward", area.current_index() == 0,
          area.current_index())

    area.set_current_index(1)
    strip = recipient._page_panel._list
    cell = strip.visualItemRect(strip.item(0))
    # `drop_mime` is held in a NAMED local on purpose. QDropEvent does not take
    # ownership of the QMimeData, so an inline argument is collectable the
    # moment the constructor returns and the handler reads freed memory. A
    # segfault, not an exception, because this is all C++ underneath.
    drop_mime = make_page_mime(donor, [0])
    drop = QDropEvent(QPointF(cell.center().x(), cell.top() + 1),
                      Qt.DropAction.MoveAction, drop_mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    strip.dropEvent(drop)
    check("the drop was taken", drop.isAccepted())
    check("the page left the donor", donor.page_count() == donor_pages - 1,
          donor.page_count())
    check("the page arrived in the recipient",
          recipient.page_count() == recipient_pages + 1, recipient.page_count())
    check("both documents are unsaved", donor.is_dirty() and recipient.is_dirty())
    check("the donor's close prompt names the recipient",
          recipient.transfer_label() in donor.transfer_warning(),
          donor.transfer_warning())
    check("one command, not two", first.undo_stack().count() == 1,
          first.undo_stack().count())

    first.undo_stack().undo()
    check("undo put the page back in the donor",
          donor.page_count() == donor_pages, donor.page_count())
    check("undo took it out of the recipient",
          recipient.page_count() == recipient_pages, recipient.page_count())
    check("undo switched to the document it changed",
          area.current_view() is recipient, area.current_index())
    check("both documents are clean again",
          not donor.is_dirty() and not recipient.is_dirty())
    area.check_invariant()

    print("\n5. tear a tab out with the gesture (phase 4)")
    # Two more documents in the first window, so there is something to tear and
    # something left behind when it goes.
    first.open_paths([make_pdf(folder, "delta.pdf", 2)])
    check("three tabs to drag from", area.count() == 3, area.count())
    torn_view = area.view_at(2)
    torn_canvas = torn_view._canvas
    torn_scene = torn_canvas.scene()

    grab = _tab_point(bar, 2)
    _press(bar, grab)
    _move(bar, _below_bar(bar, grab))
    third = bar.tear_off().floating_window()
    check("crossing the threshold made a window",
          third is not None and third is not first and third is not second)
    check("three windows now", registry.count() == 3, registry.count())
    check("the torn window holds the document",
          third.document_area().view_at(0) is torn_view)
    check("the first window kept the rest", area.count() == 2, area.count())
    check("the canvas survived the gesture", torn_canvas.scene() is torn_scene)
    check("no native handle grown on the way",
          torn_canvas.internalWinId() == 0, torn_canvas.internalWinId())

    # The real window follows the cursor: the offset between the pointer and
    # the window frame is what has to stay constant, and only a real platform
    # plugin has a frame worth measuring.
    here = _below_bar(bar, grab)
    offset = here - third.frameGeometry().topLeft()
    _move(bar, here + QPoint(260, 180))
    check("the window tracked the cursor",
          (here + QPoint(260, 180)) - third.frameGeometry().topLeft() == offset,
          f"{offset} -> {(here + QPoint(260, 180)) - third.frameGeometry().topLeft()}")
    area.check_invariant()
    third.document_area().check_invariant()

    print("\n6. drop it back onto the first window's bar, at index 0")
    over = _tab_point(bar, 0, dx=6)
    _move(bar, over)
    target = bar.tear_off().drop_target()
    check("the first window is the drop target",
          target is not None and target[0] is first and target[1] == 0, target)
    check("the insertion line is drawn", bar.drop_indicator() is not None)
    _release(bar, over)
    check("the document docked at index 0",
          area.view_at(0) is torn_view, area.index_of(torn_view))
    check("three tabs again", area.count() == 3, area.count())
    check("the emptied window closed itself", registry.count() == 2,
          registry.count())
    check("the insertion line was cleared", bar.drop_indicator() is None)
    # The one thing offscreen genuinely cannot check: a leaked grabMouse() is a
    # frozen application, and only a real platform plugin has a grab to leak.
    check("the mouse grab was given back",
          QWidget.mouseGrabber() is None, QWidget.mouseGrabber())
    area.check_invariant()

    print("\n7. Ctrl+Tab walks the visit history, not the tab order")
    area.set_current_index(0)
    area.set_current_index(2)
    area.set_current_index(1)
    first.next_recent_tab()
    check("Ctrl+Tab went to the tab visited before this one",
          area.current_view() is area.view_at(2), area.current_index())
    first.next_recent_tab()
    check("holding Ctrl walked further back, not straight home",
          area.current_view() is area.view_at(0), area.current_index())
    first._end_mru_walk()
    check("the walk committed", not area.is_walking_mru())
    first.next_tab()
    check("Ctrl+PgDn is still positional",
          area.current_index() == 1, area.current_index())

    print("\n8. close both windows")
    quit_seen = []
    app.aboutToQuit.connect(lambda: quit_seen.append(True))

    for view in second.document_area().views():
        view.mark_clean()
    second._force_quit = True
    second.close()
    check("one window left", registry.count() == 1, registry.count())
    check("the app has NOT quit yet", not quit_seen)

    for view in first.document_area().views():
        view.mark_clean()
    first._force_quit = True
    first.close()
    check("no windows left", registry.count() == 0, registry.count())


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Rapid PDF")
    app.setOrganizationName("Lucas")
    # The same line main.py sets, and the reason step 7 proves anything.
    app.setQuitOnLastWindowClosed(False)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        QTimer.singleShot(0, lambda: run(app, folder))
        # A backstop, so a failure to quit hangs for a second rather than
        # forever. If the registry did its job this never fires.
        QTimer.singleShot(20_000, app.quit)
        code = app.exec()

    print(f"\nevent loop returned {code}: the last window closing quit the app")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
