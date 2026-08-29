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
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

import fitz
from PySide6.QtCore import QRectF, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from core.settings import Settings, set_settings
from ui.canvas import AddItemsCommand, HighlightItem
from ui.theme import apply_theme
from ui.window_registry import WindowRegistry

FAILURES = []


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
        "undo": canvas.undo_stack,
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
    check("same undo stack object", canvas.undo_stack is before["undo"])
    check("same viewport object", canvas.viewport() is before["viewport"])
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

    print("\n4. close both windows")
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
    # The same line main.py sets, and the reason step 4 proves anything.
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
