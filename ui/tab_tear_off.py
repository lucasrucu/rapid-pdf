"""Drag a tab out of the window and it becomes its own window.

Phase 4 of docs/tabs-plan.md, and deliberately the thinnest layer in the whole
feature. Phase 3 built every mechanism this needs and made all of it reachable
from a headless test: `MainWindow.move_view_to_window` for a drop onto an
existing window, `MainWindow.move_view_to_new_window` for a drop on empty
desktop, `DocumentArea.adopt` / `detach` under both. Nothing in here moves a
document itself. It decides WHEN and WHERE, and calls phase 3.

MANUAL MOUSE TRACKING, NOT QDrag. Three reasons, and the third is what settles
it:

  1. QDrag follows the cursor with a static pixmap. We want the real window
     following the cursor, because then the drop is a no-op and "the drop
     failed, where did my document go" stops being a class of bug.
  2. `QDrag.target()` is None on a rejected drop and carries no position, so
     "dropped on empty desktop, make a window" and "drop refused" are the same
     answer.
  3. On Windows `QDrag::exec()` BLOCKS THE EVENT LOOP for the length of the
     drag, which is exactly the wrong place to be creating a window and moving
     it every few milliseconds.

It also means the tab drag never enters Qt's drag-and-drop system at all, so it
cannot be confused with the page drag (`QDrag`, item-model mime) or an OS file
drop (`text/uri-list`). The three gestures in the plan's table stay separate by
construction rather than by a guard anyone has to remember to write.

THE SHAPE OF ONE GESTURE.

  press     record the position and the tab under it, then let QTabBar have the
            event so its own reorder and current-tab change still work.
  threshold DETACH_MARGIN px BEYOND the bar VERTICALLY, plus Qt's own
            `startDragDistance`. The vertical part is the whole point: a sloppy
            horizontal reorder must never become a tear, and horizontal
            overshoot past the last tab is not a tear either.
  crossing  grab the mouse (so moves keep arriving once the cursor has left the
            bar), create the real window, and from here on just `move()` it.
            The grab stays on the SOURCE bar for the whole gesture. Handing it
            to the new window is the obvious alternative and is far harder to
            reason about and to test.
  release   `releaseMouse()` FIRST, always. A target means adopt at the index
            under the cursor and let the emptied floating window close itself.
            No target means there is nothing to do.
  escape    re-dock into the source window at the index it came from.

HIT-TESTING IS DONE AGAINST THE REGISTRY, NOT AGAINST Qt. Neither
`QApplication.widgetAt()` nor `topLevelAt()` is usable here: both return the
floating window that is under the cursor by definition, and both round-trip to
the OS. So the windows are walked in activation order, the floating one and
anything minimised or hidden are skipped, and the global position is mapped
into each tab bar directly.

THE SINGLE-TAB CASE. A window with one tab drags ITSELF rather than spawning a
second window. Without that you tear the only document out of a window, close
the window behind it, and end up with the window you started with, having
thrown away its size and position on the way. The tab menu's Move to New Window
is disabled at one tab for the same reason.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtWidgets import QApplication, QTabBar

from ui.window_registry import WindowRegistry

# How far past the top or bottom edge of the bar the cursor has to go before a
# reorder becomes a tear. Sideways travel never counts, however far it goes.
DETACH_MARGIN = 28

# How far above and below a target bar still counts as "on" it. A tab bar is
# about 30 px tall and the cursor is holding a whole window, so the band people
# actually aim at is taller than the widget.
DOCK_MARGIN = 12

# A window holding one empty tab hides its tab bar (see
# DocumentArea._sync_header_visibility), so there is no bar to aim at. Its top
# strip is still a legitimate place to drop, and MainWindow.adopt already
# replaces the placeholder tab when a document lands there.
EMPTY_DOCK_HEIGHT = 34


def insertion_index(bar: QTabBar, local: QPoint) -> int:
    """Where a tab dropped at `local` should be inserted in `bar`.

    Past the last tab appends. On a tab, the half the cursor is in decides
    whether it goes before or after, which is what makes the insertion line
    land where the eye expects it.
    """
    index = bar.tabAt(local)
    if index < 0:
        return bar.count()
    rect = bar.tabRect(index)
    return index + 1 if local.x() > rect.center().x() else index


def _device_pixel_ratio(global_pos: QPoint, fallback) -> float:
    """The scale factor of the screen under a point, or the widget's own."""
    screen = QGuiApplication.screenAt(global_pos)
    if screen is not None:
        return float(screen.devicePixelRatio())
    if fallback is not None:
        return float(fallback.devicePixelRatioF())
    return 1.0


class TabTearOff:
    """The gesture, for one tab bar.

    Owned by `DocumentArea`, which forwards the bar's three mouse events and
    its key presses in here. Every method that can start or end a drag leaves
    the input grabs in a defined state, because a leaked `grabMouse()` makes
    the whole application stop responding to the mouse.
    """

    def __init__(self, bar, area):
        self._bar = bar
        self._area = area
        self._reset()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _reset(self):
        self._armed = False           # pressed on a tab, may still become a tear
        self._dragging = False        # past the threshold, we own the mouse
        self._press_local = QPoint()
        self._press_global = QPoint()
        self._press_index = -1
        self._grab_in_tab = QPoint()  # where in the tab the cursor took hold
        self._view = None
        self._floating = None         # the window following the cursor
        self._source_window = None
        self._source_index = -1
        self._source_pos = QPoint()
        self._whole_window = False    # single-tab case: floating IS the source
        self._offset = QPoint()       # cursor position inside the floating window
        self._target = None           # (window, insertion index) under the cursor
        self._start_dpr = 1.0

    def is_dragging(self) -> bool:
        """Whether a tear is in flight. For the tests, and for the bar's
        paintEvent, which must not draw an insertion line into itself."""
        return self._dragging

    def floating_window(self):
        """The window currently following the cursor, or None."""
        return self._floating

    def drop_target(self):
        """(window, index) under the cursor, or None. For the tests."""
        return self._target

    # ------------------------------------------------------------------
    # The three mouse events, forwarded by DocumentTabBar
    # ------------------------------------------------------------------

    def press(self, event):
        """Record what was grabbed. Never consumes the event.

        The caller runs `QTabBar.mousePressEvent` straight after this, which is
        what keeps Qt's own tab reordering and its current-tab change working.
        Everything below is a decision made later, on the first move that goes
        far enough.
        """
        self._reset()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        local = event.position().toPoint()
        index = self._bar.tabAt(local)
        if index < 0:
            return
        self._armed = True
        self._press_local = local
        self._press_global = event.globalPosition().toPoint()
        self._press_index = index
        self._grab_in_tab = local - self._bar.tabRect(index).topLeft()

    def move(self, event) -> bool:
        """Returns True when this gesture has taken the event.

        Until the threshold is crossed it takes nothing, so a plain drag along
        the bar is still QTabBar's reorder and behaves exactly as it did.
        """
        if self._dragging:
            self._track(event.globalPosition().toPoint())
            return True
        if not self._armed:
            return False
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            self._armed = False
            return False
        global_pos = event.globalPosition().toPoint()
        if not self._crossed(event.position().toPoint(), global_pos):
            return False
        if not self._begin(global_pos):
            self._armed = False
            return False
        self._track(global_pos)
        return True

    def release(self, event) -> bool:
        if not self._dragging:
            self._armed = False
            return False
        self._finish(event.globalPosition().toPoint())
        return True

    def key_press(self, event) -> bool:
        """Escape mid-drag puts the document back where it came from."""
        if not self._dragging or event.key() != Qt.Key.Key_Escape:
            return False
        self._cancel()
        return True

    # ------------------------------------------------------------------
    # The threshold
    # ------------------------------------------------------------------

    def _crossed(self, local: QPoint, global_pos: QPoint) -> bool:
        """Whether this move is a tear rather than a reorder.

        Vertical overshoot only. Dragging the last tab off the right-hand end
        of the bar is something people do by accident every time they reorder,
        and turning that into a second window would be unforgivable.
        """
        rect = self._bar.rect()
        if local.y() < rect.top():
            beyond = rect.top() - local.y()
        elif local.y() > rect.bottom():
            beyond = local.y() - rect.bottom()
        else:
            return False
        if beyond < DETACH_MARGIN:
            return False
        travelled = (global_pos - self._press_global).manhattanLength()
        return travelled >= QApplication.startDragDistance()

    # ------------------------------------------------------------------
    # Starting
    # ------------------------------------------------------------------

    def _begin(self, global_pos: QPoint) -> bool:
        """Take the mouse and put the real window on screen.

        The window is created HERE, on the crossing, not on the drop. What the
        user sees for the rest of the gesture is the result rather than a
        preview of it, and `move_view_to_new_window` already builds it in the
        order the reparent depends on (create, size, position, show, and only
        then move the view across), so this passes it a geometry instead of
        writing a second copy of that sequence.
        """
        bar = self._bar
        area = self._area
        source = bar.window()
        if source is None or not hasattr(source, "move_view_to_new_window"):
            return False
        view = area.view_at(self._press_index)
        if view is None:
            return False

        # Let QTabBar finish whatever reorder it had going before we take over,
        # so it is not left believing a drag is still in flight. The index is
        # re-read afterwards, because that reorder may have moved this tab.
        self._settle_tab_bar(global_pos)
        index = area.index_of(view)
        if index < 0:
            return False

        self._view = view
        self._source_window = source
        self._source_index = index
        self._source_pos = source.pos()
        self._start_dpr = _device_pixel_ratio(global_pos, source)
        self._dragging = True
        self._grab_input()
        try:
            if area.count() == 1:
                # One tab: drag the window itself. Tearing the only document
                # out and closing the window behind it hands back the window
                # you started with, minus its size and position.
                self._whole_window = True
                self._floating = source
            else:
                self._floating = source.move_view_to_new_window(
                    view, geometry=QRect(source.pos(), source.size()))
                if self._floating is None:
                    raise RuntimeError("the new window was not created")
            self._offset = self._offset_in_floating()
        except Exception:
            self._abort()
            raise
        return True

    def _settle_tab_bar(self, global_pos: QPoint):
        """Hand QTabBar a release so its own drag state ends cleanly.

        From here on this gesture eats the moves, and a QTabBar left mid-drag
        keeps a pressed index and an offset that the next press would inherit.
        """
        local = QPointF(self._bar.mapFromGlobal(global_pos))
        event = QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease, local, QPointF(global_pos),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier)
        QTabBar.mouseReleaseEvent(self._bar, event)

    def _offset_in_floating(self) -> QPoint:
        """Where inside the floating window the cursor should sit.

        The same offset it took hold of the tab at, so the tab stays under the
        pointer rather than jumping to a corner the moment the window appears.
        Computed once: the tab can be re-laid-out during the drag and a
        recomputed offset would make the window twitch.

        Measured against `frameGeometry()`, NOT against the widget origin,
        because `move()` positions the frame. Use the client origin and every
        step of the drag is out by the title bar and the border.
        """
        area = self._floating.document_area()
        bar = area.bar()
        index = area.index_of(self._view)
        rect = bar.tabRect(max(0, index))
        local = rect.topLeft() + self._grab_in_tab
        return bar.mapToGlobal(local) - self._floating.frameGeometry().topLeft()

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------

    def _track(self, global_pos: QPoint):
        if self._floating is None:
            return
        self._floating.move(global_pos - self._offset)
        self._set_target(self._hit_test(global_pos))

    def _hit_test(self, global_pos: QPoint):
        """The (window, insertion index) under the cursor, or None.

        Walked off `WindowRegistry`, in activation order, and NOT off
        `QApplication.widgetAt()` or `topLevelAt()`: the window under the
        cursor is the one being dragged, by construction, and both of those
        round-trip to the OS on every mouse move for an answer we already have.
        """
        floating = self._floating
        for window in WindowRegistry.instance().windows():
            if window is floating:
                continue
            if not window.isVisible() or window.isMinimized():
                continue
            if not hasattr(window, "document_area"):
                continue
            area = window.document_area()
            bar = area.bar()
            if bar.isVisible():
                local = bar.mapFromGlobal(global_pos)
                band = bar.rect().adjusted(0, -DOCK_MARGIN, 0, DOCK_MARGIN)
                if band.contains(local):
                    return window, insertion_index(bar, local)
                continue
            # An empty window hides its header, so aim at the strip where the
            # bar would be. Dropping there is what fills that window up.
            local = area.mapFromGlobal(global_pos)
            strip = QRect(0, 0, area.width(), EMPTY_DOCK_HEIGHT + DOCK_MARGIN)
            if strip.contains(local):
                return window, 0
        return None

    def _set_target(self, target):
        """Move the insertion line, and only repaint when it actually moved."""
        previous = self._target
        if previous is not None:
            same_window = target is not None and target[0] is previous[0]
            if not same_window:
                self._clear_indicator_on(previous[0])
        self._target = target
        if target is None:
            return
        window, index = target
        bar = window.document_area().bar()
        bar.set_drop_indicator(bar.insertion_x(index))

    def _clear_indicator(self):
        if self._target is not None:
            self._clear_indicator_on(self._target[0])

    @staticmethod
    def _clear_indicator_on(window):
        try:
            window.document_area().bar().set_drop_indicator(None)
        except (AttributeError, RuntimeError):   # pragma: no cover - defensive
            pass

    # ------------------------------------------------------------------
    # Ending
    # ------------------------------------------------------------------

    def _finish(self, global_pos: QPoint):
        """The button came up.

        `releaseMouse()` first and unconditionally: everything after it can
        fail, and a leaked grab is a frozen application rather than a lost
        document.
        """
        self._release_input()
        target = self._target
        floating = self._floating
        view = self._view
        try:
            self._clear_indicator()
            if target is not None and target[0] is not floating:
                window, index = target
                # Phase 3's move, index and all. The floating window is left
                # holding nothing and closes itself from inside it.
                floating.move_view_to_window(view, window, index)
                window.activate_view(view)
            # No target is not a failure. The window is already exactly where
            # they let go of it, which is the whole payoff of creating it on
            # the crossing rather than on the drop.
            self._settle_dpi(global_pos, view)
        finally:
            self._reset()

    def _cancel(self):
        """Escape. Put it back where it came from, at the index it came from."""
        self._release_input()
        floating = self._floating
        try:
            self._clear_indicator()
            if self._whole_window:
                if floating is not None:
                    floating.move(self._source_pos)
            elif floating is not None and self._source_window is not None:
                floating.move_view_to_window(
                    self._view, self._source_window, self._source_index)
                self._source_window.activate_view(self._view)
        finally:
            self._reset()

    def _abort(self):
        """A failure part way through starting. Give the input back and forget."""
        self._release_input()
        self._reset()

    def _grab_input(self):
        """Keep the events coming once the cursor has left the bar.

        The keyboard too, because Escape has to reach us and the bar has
        NoFocus: without the grab the key lands on whatever had focus when the
        drag started, which is usually a canvas that eats it.
        """
        self._bar.grabMouse()
        self._bar.grabKeyboard()

    def _release_input(self):
        """Called on EVERY exit path. Qt tolerates a release without a grab."""
        try:
            self._bar.releaseMouse()
        except RuntimeError:                     # pragma: no cover - defensive
            pass
        try:
            self._bar.releaseKeyboard()
        except RuntimeError:                     # pragma: no cover - defensive
            pass

    def _settle_dpi(self, global_pos: QPoint, view):
        """Re-render if the document landed on a monitor at a different scale.

        Cached page pixmaps are DEVICE-dependent: rendered for a 1.0 screen and
        shown on a 1.5 one they are soft, and the panel thumbnails with them.
        Nothing about the document changed, so this is a cache drop and a
        redraw. The window's own `screenChanged` covers every later move; this
        covers the one move that happens while nobody is watching for it.
        """
        if view is None or not hasattr(view, "rerender_for_screen_change"):
            return
        if abs(_device_pixel_ratio(global_pos, self._floating)
               - self._start_dpr) < 1e-3:
            return
        view.rerender_for_screen_change()
