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

PHASE 6 ADDED STEP 8, session restore. Offscreen has no real geometry and no
real screen name, so the suite can only pin the mechanism; here the session is
recorded off windows the OS actually placed and the restored windows are put
back at those coordinates. It also checks the thing the whole phase is for:
that reopening N tabs reads exactly one file per window.

PHASE 4 ADDED THE TEAR-OFF GESTURE STEPS, AND THE GHOST REWRITE CHANGED WHAT
THEY WATCH. Those steps are the reason to prefer the second command line above.
The pytest suite drives the gesture by handing synthesised QMouseEvents to the
tab bar, which is enough to pin the decisions it makes; what it cannot do is
grab the mouse, put a real top-level on screen under a cursor, or say whether
the geometry the gesture computes lands anywhere sensible on a real screen.

TWO DIFFERENT THINGS FOLLOW THE POINTER, and there is a step for each.

  step 5  A TAB is carried as a GHOST, a tab-sized picture. Nothing moves and
          no window is created while the button is down; the real window is
          made on the RELEASE. That reversal is the whole of the rewrite in
          ui/tab_tear_off.py, so this step pins both halves of it: a ghost that
          tracks the cursor mid-drag, and no new window until the drop.
  step 6  A WINDOW HOLDING ONE TAB drags ITSELF, live, because there is no
          second window to make. No ghost, the real window follows the cursor,
          the merge into another strip is deferred to the release, and the
          feedback on the target strip is the insertion LINE rather than a
          ghost slot.

Run natively and watch: the ghost should sit under the pointer at the point on
the tab it was grabbed by, not offset by a title bar, and the window should
appear where the button came up.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

import fitz
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QDragMoveEvent, QDropEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from core.settings import Settings, set_settings, settings
from ui.canvas import AddItemsCommand, HighlightItem
from ui.page_drag import make_page_mime
from ui.session import restore_on_launch
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


def _vacant_point(registry, start):
    """A point no window of ours covers, walking down from `start`.

    A DROP HAS TO LAND ON EMPTY DESKTOP TO MAKE A WINDOW, and "empty" is a
    question about the registry rather than about the screen: `_hit_test` walks
    exactly the windows the registry knows and returns a target for any point
    inside one of their frames. Picking a corner and hoping is what makes a
    tool fail for a reason that has nothing to do with the code under it, so
    the point is checked against the frames the OS actually gave the windows.

    The FRAME and not the geometry, because a frameless window still carries a
    resize border and the gap the layout leaves has to clear it.
    """
    point = QPoint(start)
    for _ in range(60):
        covered = [w.frameGeometry() for w in registry.windows()
                   if w.isVisible()]
        if not any(rect.contains(point) for rect in covered):
            return point
        point += QPoint(0, 40)
    return point


def settle(app, passes=8):
    """Let deferred work run before asking what happened.

    `MainWindow.move_view_to_window` puts the close of an emptied window on the
    NEXT pass of the event loop rather than closing it where it stands, and
    that `singleShot(0, ...)` is the 0xC000041D fix rather than a detail. The
    ghost is retired the same way, with `deleteLater`. `run()` is itself a
    timer callback, so nothing deferred inside it happens until it is pumped
    and a window that has already lost its last tab is still in the registry.
    """
    for _ in range(passes):
        app.processEvents()


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

    # THE WINDOWS ARE PLACED BY HAND HERE, AND IT IS NOT TIDINESS.
    # `move_view_to_new_window` offsets a new window from its parent by
    # NEW_WINDOW_OFFSET, which is 32, and a strip accepts a carried window
    # anywhere in a band DOCK_MARGIN (18) px above and below it. So the window
    # step 2 made sits 32 px down and across from the one it came from, with
    # its own tabs inside that window's dock band and almost on top of the tabs
    # these steps have to aim at. `TabTearOff._hit_test` breaks a tie between
    # two windows under the cursor on ACTIVATION order, so that overlap turns
    # every target check below into a question about which window was touched
    # last rather than about where the cursor is.
    #
    # A MainWindow will not go below 1100x720 (setMinimumSize in
    # ui/main_window.py), so on an ordinary screen the two cannot be pulled
    # fully apart, and they do not have to be. Every point these steps aim at
    # is in the LEFT half of the LEFT window, so the only thing that has to be
    # true is that the other one starts to the right of all of them.
    first.setGeometry(40, 60, 1100, 720)
    second.setGeometry(760, 60, 1100, 720)
    settle(app)

    torn_view = area.view_at(2)
    torn_canvas = torn_view._canvas
    torn_scene = torn_canvas.scene()
    windows_before = registry.count()

    grab = _tab_point(bar, 2)
    _press(bar, grab)
    mid_air = _below_bar(bar, grab)
    _move(bar, mid_air)
    tear = bar.tear_off()

    # A GHOST, AND NOT ONE THING ELSE. The window is made on the DROP now, so
    # while the button is down the document has not moved, the source strip
    # still holds all three tabs, and the only new object on screen is a
    # picture of the tab. Deferring the creation is the whole point of the
    # rewrite: nothing is built while the mouse is captured, so nothing has to
    # be torn down inside the release handler, which is where the 0xC000041D
    # came from. Both halves are worth pinning, so both are checked.
    check("the gesture engaged", tear.is_dragging())
    ghost = tear.ghost()
    check("a ghost is carrying the tab", ghost is not None)
    check("no window was made on the crossing",
          registry.count() == windows_before, registry.count())
    check("the document has not moved", torn_view.window() is first)
    check("the source strip still holds all three", area.count() == 3,
          area.count())
    check("floating_window() answers None, as it says it does",
          tear.floating_window() is None, tear.floating_window())
    check("the bar has the mouse", QWidget.mouseGrabber() is bar,
          QWidget.mouseGrabber())
    area.check_invariant()

    # THE GHOST IS WHAT FOLLOWS THE CURSOR NOW, so the offset between the
    # pointer and the thing being carried is what has to stay constant, and it
    # is measured off the ghost rather than off a window. Only a real platform
    # plugin puts the ghost on screen at coordinates worth measuring, which is
    # the reason to run this script the second way.
    if ghost is not None:
        offset = mid_air - ghost.frameGeometry().topLeft()
        further = mid_air + QPoint(150, 110)
        _move(bar, further)
        carried = tear.ghost()
        check("the same ghost is still in flight", carried is ghost)
        moved_to = (carried.frameGeometry().topLeft()
                    if carried is not None else None)
        check("the ghost tracked the cursor",
              moved_to is not None and further - moved_to == offset,
              f"{offset} -> {None if moved_to is None else further - moved_to}")
        check("and the OS put it where the gesture computed",
              moved_to == tear.ghost_position(further),
              f"{tear.ghost_position(further)} -> {moved_to}")
        # No downward clearance any more. The old floating window was held 46 px
        # below the pointer so it would not cover its own drop feedback; a ghost
        # is tab-sized and the OS hit test passes straight through it.
        check("no clearance below the pointer",
              tear.ghost_position(further).y() <= further.y(),
              f"{tear.ghost_position(further).y()} vs {further.y()}")

    # Out over empty desktop and let go. This is the only place a window is
    # created, and it happens with the button already up.
    drop_at = _vacant_point(registry, QPoint(300, 620))
    _move(bar, drop_at)
    check("over no window at all", tear.drop_target() is None, tear.drop_target())
    expected_at = tear.ghost_position(drop_at)
    _release(bar, drop_at)
    settle(app)

    check("the drag is over", not bar.tear_off().is_dragging())
    check("the ghost is gone", bar.tear_off().ghost() is None)
    third = torn_view.window()
    check("releasing made a window",
          third is not None and third is not first and third is not second)
    check("one more window than before",
          registry.count() == windows_before + 1, registry.count())
    check("the new window holds the document",
          third.document_area().count() == 1
          and third.document_area().view_at(0) is torn_view)
    check("the first window kept the rest", area.count() == 2, area.count())
    check("the canvas survived the gesture", torn_canvas.scene() is torn_scene)
    check("no native handle grown on the way",
          torn_canvas.internalWinId() == 0, torn_canvas.internalWinId())
    # Where the button came up, not at a corner. A few pixels of slack because
    # a real window manager may nudge a window it is placing; the failure this
    # is watching for is a whole title bar of offset, not a rounding.
    landed = third.geometry().topLeft()
    check("the window appeared where the button came up",
          abs(landed.x() - expected_at.x()) <= 8
          and abs(landed.y() - expected_at.y()) <= 8,
          f"{expected_at} -> {landed}")
    # The one thing offscreen genuinely cannot check: a leaked grabMouse() is a
    # frozen application, and only a real platform plugin has a grab to leak.
    check("the mouse grab was given back",
          QWidget.mouseGrabber() is None, QWidget.mouseGrabber())
    area.check_invariant()
    third.document_area().check_invariant()

    print("\n6. drag the torn window back onto the first window's strip")
    # A WINDOW WITH ONE TAB DRAGS ITSELF, so this is the other half of the
    # gesture rather than step 5 run backwards. There is no second window to
    # make and nothing to preview, so there is no ghost: the real window
    # follows the cursor, the merge is deferred to the release, and the
    # feedback on the target strip is the insertion LINE, because nothing has
    # joined that strip for it to ghost. See TabTearOff._show_drop_feedback.
    torn_bar = third.document_area().bar()
    torn_tear = torn_bar.tear_off()
    back = _tab_point(torn_bar, 0)
    _press(torn_bar, back)
    here = _below_bar(torn_bar, back)
    _move(torn_bar, here)
    check("the lone tab took its window with it", torn_tear.is_dragging())
    check("no ghost for a whole window", torn_tear.ghost() is None,
          torn_tear.ghost())
    check("still three windows", registry.count() == windows_before + 1,
          registry.count())

    # The offset between the pointer and the window frame is what has to stay
    # constant here, and only a real platform plugin has a frame worth
    # measuring. This is the check step 5 used to make, in the one case it is
    # still true of.
    offset = here - third.frameGeometry().topLeft()
    there = here + QPoint(120, 80)
    _move(torn_bar, there)
    check("the window tracked the cursor",
          there - third.frameGeometry().topLeft() == offset,
          f"{offset} -> {there - third.frameGeometry().topLeft()}")

    over = _tab_point(bar, 0, dx=6)
    _move(torn_bar, over)
    target = torn_tear.drop_target()
    check("the first window's strip is the drop target",
          target is not None and target[0] is first and target[1] == 0, target)
    # A carried WINDOW now parts the strip too, the same as a carried tab.
    # It used to get the insertion line instead, on the reasoning that nothing
    # had joined the target yet so there was no tab to ghost. Lucas could not
    # see the difference between the two gestures and there is no longer one.
    check("the strip parted for it", not bar.ghost_slot_rect().isEmpty(),
          bar.ghost_slot_rect())
    check("no carried-tab ghost, the tab has not joined", bar.ghost_index() is None,
          bar.ghost_index())
    check("and the merge has not happened yet", area.count() == 2, area.count())

    _release(torn_bar, over)
    settle(app)
    check("the document docked at index 0",
          area.view_at(0) is torn_view, area.index_of(torn_view))
    check("three tabs again", area.count() == 3, area.count())
    check("the emptied window closed itself", registry.count() == windows_before,
          registry.count())
    check("the strip closed up again", bar.ghost_slot_rect().isEmpty(),
          bar.ghost_slot_rect())
    check("the mouse grab was given back",
          QWidget.mouseGrabber() is None, QWidget.mouseGrabber())
    area.check_invariant()

    print("\n6b. a carried TAB gets the ghost slot, not the line")
    # The other feedback, and the one a person sees most: the strip parts, the
    # arriving tab drops into the gap, and it is painted as a ghost until the
    # button comes up. Driven back onto the source's own strip so it needs no
    # second window and changes no tab counts, only the order.
    slot_view = area.view_at(2)
    slot_grab = _tab_point(bar, 2)
    _press(bar, slot_grab)
    _move(bar, _below_bar(bar, slot_grab))
    tear = bar.tear_off()
    check("the tear engaged", tear.is_dragging())
    check("a ghost in mid-air", tear.ghost() is not None)

    onto = _tab_point(bar, 0, dx=6)
    _move(bar, onto)
    target = tear.drop_target()
    check("the strip it came from is a target again",
          target is not None and target[0] is first and target[1] == 0, target)
    check("the tab has already joined the strip at index 0",
          area.index_of(slot_view) == 0, area.index_of(slot_view))
    check("the ghost slot is held open", bar.ghost_index() == 0,
          bar.ghost_index())
    check("no insertion line for a carried tab", bar.drop_indicator() is None,
          bar.drop_indicator())
    # The picture gets out of the way over a strip: the gap and the ghosted tab
    # in it are the feedback, and a second copy of the tab hanging over them is
    # what hid the thing it was meant to be showing.
    check("the carried picture is hidden over the strip", tear.ghost() is None,
          tear.ghost())

    _release(bar, onto)
    settle(app)
    check("the document stayed at index 0", area.view_at(0) is slot_view,
          area.index_of(slot_view))
    check("still three tabs and still one window each",
          area.count() == 3 and registry.count() == windows_before,
          f"{area.count()} tabs, {registry.count()} windows")
    check("the ghost became a real tab", bar.ghost_index() is None,
          bar.ghost_index())
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

    print("\n8. save the session, close everything, and bring it back (phase 6)")
    # The pytest suite drives this too, but only offscreen, where there is no
    # real geometry and no real screen name to remember. What this step adds is
    # a session recorded off windows the OS actually placed, and windows put
    # back where those coordinates say.
    store.startup.restore_tabs = True
    expected = sorted(os.path.basename(v.document_path())
                      for _, v in registry.views() if v.document_path())
    expected_windows = registry.count()

    # Closing the last window quits the application, and there would be nothing
    # left to restore into. Disarmed for the length of this step and armed
    # again for step 9, which is the one that proves the quit still happens.
    registry.quit_on_last_window = False
    for window in list(registry.windows()):
        for view in window.document_area().views():
            view.mark_clean()
        window._force_quit = True
        window.close()
    check("everything closed", registry.count() == 0, registry.count())

    saved = settings().session.windows
    check("the session kept every window that went down with the app",
          len(saved) == expected_windows, f"{len(saved)} of {expected_windows}")
    check("it names every document that was open",
          sorted(os.path.basename(t["path"])
                 for w in saved for t in w["tabs"]) == expected,
          sorted(os.path.basename(t["path"]) for w in saved for t in w["tabs"]))
    check("it remembers where each window was",
          all(w["geometry"] for w in saved), [w["geometry"] for w in saved])
    # Offscreen's one screen has no name at all, so this is only a real check
    # on a real platform. It is the reason to run this script the second way.
    if QApplication.platformName() == "offscreen":
        check("the screen was asked for (offscreen has no name to give)",
              all("screen" in w for w in saved))
    else:
        check("and which screen it was on",
              all(w["screen"] for w in saved), [w["screen"] for w in saved])

    registry.quit_on_last_window = True
    revived = registry.create_window(theme=theme, show=False)
    missing = restore_on_launch(revived, registry)
    revived.show()
    check("no files went missing", missing == 0, missing)
    check("the windows came back", registry.count() == len(saved), registry.count())

    tabs_back = list(registry.views())
    loaded = [v for _, v in tabs_back if v.has_document()]
    pending = [v for _, v in tabs_back if v.is_pending()]
    check("every tab came back",
          len(tabs_back) == sum(len(w["tabs"]) for w in saved), len(tabs_back))
    check("exactly one document per window was read",
          len(loaded) == len(saved), len(loaded))
    check("the rest are waiting to be looked at",
          len(pending) == len(tabs_back) - len(loaded), len(pending))
    check("a tab that has read nothing still names its file",
          all(v.document_path() for v in pending))

    want = saved[0]["geometry"]
    got = revived.geometry()
    check("the first window came back at its saved size",
          [got.width(), got.height()] == want[2:],
          f"{want[2:]} -> {[got.width(), got.height()]}")
    check("and within a few pixels of its saved position",
          abs(got.x() - want[0]) <= 8 and abs(got.y() - want[1]) <= 8,
          f"{want[:2]} -> {[got.x(), got.y()]}")

    if pending:
        waiting = pending[0]
        holder = waiting.window()
        holder.document_area().set_current_index(
            holder.document_area().index_of(waiting))
        check("bringing a lazy tab forward opened it", waiting.has_document())
        check("and it is no longer pending", not waiting.is_pending())

    print("\n9. close the restored windows")
    quit_seen = []
    app.aboutToQuit.connect(lambda: quit_seen.append(True))

    windows = registry.windows()
    for window in windows[:-1]:
        for view in window.document_area().views():
            view.mark_clean()
        window._force_quit = True
        window.close()
    check("one window left", registry.count() == 1, registry.count())
    check("the app has NOT quit yet", not quit_seen)

    last = registry.windows()[0]
    for view in last.document_area().views():
        view.mark_clean()
    last._force_quit = True
    last.close()
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
