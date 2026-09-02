"""Drag a tab out of the window and it becomes its own window.

Phase 4 of docs/tabs-plan.md, and deliberately the thinnest layer in the whole
feature. Phase 3 built every mechanism this needs and made all of it reachable
from a headless test: `MainWindow.move_view_to_window` for a drop onto an
existing window, `MainWindow.move_view_to_new_window` for a drop on empty
desktop, `DocumentArea.adopt` / `detach` under both. Nothing in here moves a
document itself. It decides WHEN and WHERE, and calls phase 3.

A GHOST FOLLOWS THE CURSOR, AND THE WINDOW IS CREATED ON THE DROP.

That ordering is the whole design and it is a reversal of the first one, which
created the real window on the crossing and moved it under the cursor for the
length of the drag. Four separate complaints came out of that: the window had
to be held 46 px below the pointer so it would not cover its own drop feedback;
steering it meant steering a window rather than a cursor; it arrived with a DWM
shadow that popped in and out; and it had to be destroyed inside the release
handler, which is the likeliest source of a 0xc000041d process kill. See
`_DragGhost`.

STILL NOT QDrag, but for one reason rather than three. Two of the three
originally given here do not survive checking: `QDrag.exec()` does not block the
event loop, it runs a nested modal loop in which painting and timers still run,
and its static-pixmap behaviour is exactly what is wanted rather than a problem.
The reason that holds is separation: `DocumentView` already accepts a page drag
and an OS `text/uri-list` file drop, and keeping the tab gesture out of Qt's
drag system entirely means those three can never be confused for one another.

THE SHAPE OF ONE GESTURE.

  press     record the position and the tab under it, then let QTabBar have the
            event so its own reorder and current-tab change still work.
  threshold DETACH_MARGIN px BEYOND the bar VERTICALLY, plus Qt's own
            `startDragDistance`. Sideways travel never counts, which is also
            what Chromium does. Coming back costs only DOCK_MARGIN, and that
            asymmetry is the hysteresis that stops the state flapping.
  crossing  grab the mouse and show a picture of the tab. NOTHING MOVES.
  release   `releaseMouse()` FIRST, always. Then, and only then: adopt into the
            window under the cursor, or create a new one.
  escape    nothing to undo, because nothing left.

HIT-TESTING ASKS THE OS FIRST. `QApplication.topLevelAt` gives true z-order,
and the ghost is invisible to it because of `WindowTransparentForInput`. The
registry is still walked, in activation order, as the tie-break and the
fallback. The old objection to `topLevelAt`, that it always returned the window
being dragged, died with the window being dragged.

THE SINGLE-TAB CASE. A window with one tab drags ITSELF rather than spawning a
second window. Without that you tear the only document out of a window, close
the window behind it, and end up with the window you started with, having
thrown away its size and position on the way. The tab menu's Move to New Window
is disabled at one tab for the same reason.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication, QTabBar, QWidget

from ui.window_registry import WindowRegistry

# How far past the top or bottom edge of the bar the cursor has to go before a
# reorder becomes a tear. Sideways travel never counts, however far it goes.
#
# TWO NUMBERS, NOT ONE, AND THAT IS THE HYSTERESIS. Leaving costs 40 px and
# coming back costs 18 (DOCK_MARGIN below), so the gesture does not flutter
# between torn and docked while the cursor sits on the boundary. Chromium does
# the same thing with the same asymmetry; its own vertical figure is SLACK THAT
# KEEPS YOU ATTACHED rather than a distance that detaches you, and it has no
# horizontal detach at all, which is why sideways travel is ignored here too.
DETACH_MARGIN = 40

# How far above and below a target bar still counts as "on" it. A tab bar is
# about 30 px tall and the cursor is holding a whole window, so the band people
# actually aim at is taller than the widget.
#
# THIS IS NO LONGER THE DOCK ZONE. It is only the band inside which the drop
# gets a PRECISE index, the one the insertion line points at. The dock zone is
# the whole target window: see `_hit_test`. The band and the zone used to be the
# same thing and the result was a 46-pixel strip, on a window the floating one
# was sitting on top of, that you had to find before a drop would land. Everyone
# who missed it got a second window on the desktop instead of a docked tab, and
# nothing on screen had said why.
DOCK_MARGIN = 18

# How solid the ghost is. Enough to read the tab's own title through, little
# enough that the strip and the insertion line underneath stay legible, which
# is the job the old downward offset was doing badly.
GHOST_OPACITY = 0.78


class _DragGhost(QWidget):
    """A picture of the tab, following the cursor. Not a window in any sense
    the user or the window manager cares about.

    IT REPLACES A REAL WINDOW, and that is the whole change in this file.

    What used to happen: crossing the threshold created the actual top-level
    MainWindow and then move()d it under the cursor for the rest of the drag.
    Four separate complaints came out of that one decision.

      The window is enormous next to a cursor, so it had to be pushed 46 px
      DOWN to stop it covering the strip its own drop feedback was painted on.
      What you were dragging was therefore never under your pointer.

      Because it is window-sized, getting the CURSOR onto a target meant
      steering a whole window there. Every browser does the opposite: the
      cursor picks the target and the dragged thing follows it.

      A real top-level gets Mica and a DWM drop shadow the instant it appears,
      which is the shading that "pops" on and off mid-gesture.

      And creating a window on press meant DESTROYING one on release, inside
      the release handler, which is the most likely source of the 0xc000041d
      crash: Windows re-enters the app's native event hook while the widget is
      half-dead. No window is created or destroyed while the button is down
      any more, so that path is simply gone.

    WS_EX_TRANSPARENT IS THE LOAD-BEARING FLAG. Qt.WindowTransparentForInput
    maps to it, and it is what makes the OS hit test look straight THROUGH the
    ghost. Without it `topLevelAt(cursor)` answers "the ghost" every time and
    cursor-based targeting cannot work at all.
    """

    def __init__(self, pixmap):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowOpacity(GHOST_OPACITY)
        self._pixmap = pixmap
        self.resize(pixmap.size() / max(1.0, pixmap.devicePixelRatio()))

    def paintEvent(self, event):
        QPainter(self).drawPixmap(0, 0, self._pixmap)


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
        self._ghost = None            # the picture following the cursor
        self._source_window = None
        self._source_index = -1
        self._source_pos = QPoint()
        self._whole_window = False    # single-tab case: floating IS the source
        self._offset = QPoint()       # where in the tab the cursor took hold
        self._target = None           # (window, insertion index) under the cursor
        self._start_dpr = 1.0

    def is_dragging(self) -> bool:
        """Whether a tear is in flight. For the tests, and for the bar's
        paintEvent, which must not draw an insertion line into itself."""
        return self._dragging

    def ghost(self):
        """The picture currently following the cursor, or None. For the tests."""
        return self._ghost

    def floating_window(self):
        """Kept for callers that predate the ghost. There is no longer a window
        following the cursor, so this is always None while dragging."""
        return None

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
        """Take the mouse and put a GHOST on screen. Create nothing else.

        NOTHING IS MOVED HERE ANY MORE. The document stays in the window it
        came from for the whole gesture, and the only thing that appears is a
        picture of the tab under the cursor. Where it ends up is decided on
        release, by `_finish`, which is the one place a window is created.

        That ordering is what the four reported symptoms all came down to. See
        `_DragGhost` for the full account; the short version is that a real
        window is the wrong size to hold under a pointer, brings its own
        shadow, and has to be destroyed inside the release handler.

        The cost, and it is worth naming: the source strip does not close up
        while you drag, the way Edge's does. The tab stays in place and the
        ghost is the thing that moves. That is a cosmetic gap rather than a
        broken gesture, and closing it means detaching the view on the crossing
        and holding it parentless, which is the class of thing this rewrite
        exists to stop doing.
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
        self._whole_window = area.count() == 1
        self._dragging = True
        self._grab_input()
        try:
            self._ghost = _DragGhost(bar.grab(bar.tabRect(index)))
            # The hotspot is where in the tab the cursor took hold, so the
            # ghost sits under the pointer exactly where the real tab was.
            self._offset = QPoint(self._grab_in_tab)
            self._ghost.move(self.ghost_position(global_pos))
            self._ghost.show()
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

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------

    def _track(self, global_pos: QPoint):
        if self._ghost is None:
            return
        self._ghost.move(self.ghost_position(global_pos))
        self._set_target(self._hit_test(global_pos))

    def ghost_position(self, global_pos: QPoint) -> QPoint:
        """Where the ghost sits for a cursor at `global_pos`.

        The grab offset and nothing else. There is no downward clearance any
        more: the ghost is a tab-sized picture that the OS hit test passes
        straight through, so it cannot hide the strip underneath it and does
        not need to be pushed out of the way of its own feedback.
        """
        return global_pos - self._offset

    def _hit_test(self, global_pos: QPoint):
        """The (window, insertion index) under the cursor, or None.

        Walked off `WindowRegistry`, in activation order, and NOT off
        `QApplication.widgetAt()` or `topLevelAt()`: the window under the
        cursor is the one being dragged, by construction, and both of those
        round-trip to the OS on every mouse move for an answer we already have.

        THE WHOLE WINDOW IS THE ZONE, FOR EVERY WINDOW BUT THE ONE IT CAME FROM.
        Anywhere over another window docks into it. Only WHERE in its bar the
        tab lands still depends on aiming: inside the bar band you get the index
        under the cursor, and anywhere else in the window the tab goes on the
        end. That split is the point. Precision should be available to the
        people who want it and should never be the price of admission, and the
        old arrangement charged it: miss a 46-pixel strip and the document
        became a second window instead.

        THE SOURCE WINDOW IS THE EXCEPTION, and it has to be. The tear gesture
        is "drag the tab DOWN out of the bar", and down out of the bar is still
        inside the window it came from: give that window a body-sized dock zone
        and the tab re-docks into it the instant it leaves the bar, which is to
        say the tear-off stops existing. So the window a tab is being torn out
        of keeps the narrow band, and going back up to it is how you change your
        mind. That is the rule Chrome uses too, for the same reason.

        Activation order is what breaks the tie when two windows overlap under
        the cursor, which is the same order Windows would pick and the reason
        the registry is walked rather than the geometry being sorted.
        """
        # The cursor decides, and the OS is asked first. `topLevelAt` returns
        # true z-order, which the registry's activation order can only
        # approximate, and the ghost is invisible to it because of
        # WindowTransparentForInput. That was the objection to using it before:
        # the dragged thing was a real window sitting under the cursor, so the
        # answer was always itself. There is no such window now.
        under = QApplication.topLevelAt(global_pos)
        ordered = list(WindowRegistry.instance().windows())
        if under is not None and under in ordered:
            ordered.remove(under)
            ordered.insert(0, under)

        for window in ordered:
            if not window.isVisible() or window.isMinimized():
                continue
            if not hasattr(window, "document_area"):
                continue
            if not window.frameGeometry().contains(global_pos):
                continue
            area = window.document_area()
            bar = area.bar()
            if bar.isVisible():
                local = bar.mapFromGlobal(global_pos)
                band = bar.rect().adjusted(0, -DOCK_MARGIN, 0, DOCK_MARGIN)
                if band.contains(local):
                    return window, insertion_index(bar, local)
                if window is self._source_window:
                    continue
                # Over the window but not over its bar: append. The document
                # still lands, which is the whole change.
                return window, bar.count()
            if window is self._source_window:
                continue
            # An empty window hides its header, so there is no bar to aim at
            # and nowhere else for a document to go.
            return window, 0
        return None

    def _set_target(self, target):
        """Move the feedback onto the window under the cursor, and only repaint
        when it actually moved.

        Two things are set, not one. The insertion line says WHERE, and it is
        only ever meaningful when the cursor is near the bar; the highlight says
        WHICH WINDOW, and it is on for as long as this window is the target,
        which since the zone was widened is most of the time the cursor spends
        over it. The second one is the one that was missing.
        """
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
        bar.set_drop_active(True)
        bar.set_drop_indicator(bar.insertion_x(index))

    def _clear_indicator(self):
        if self._target is not None:
            self._clear_indicator_on(self._target[0])

    @staticmethod
    def _clear_indicator_on(window):
        try:
            bar = window.document_area().bar()
            bar.set_drop_indicator(None)
            bar.set_drop_active(False)
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
        view = self._view
        source = self._source_window
        try:
            self._clear_indicator()
            self._hide_ghost()
            if target is not None:
                window, index = target
                if window is source:
                    # Back where it started, or somewhere else in the same
                    # strip. Nothing to move between windows; let the bar put
                    # it at the index the cursor was over.
                    landing = min(max(0, index), self._bar.count() - 1)
                    if landing != self._source_index:
                        self._bar.moveTab(self._source_index, landing)
                else:
                    source.move_view_to_window(view, window, index)
                    window.activate_view(view)
            elif self._whole_window:
                # The only document in the window. There is no second window to
                # make: the gesture just repositions the one it came from.
                source.move(global_pos - self._offset)
            else:
                # THE ONLY PLACE A WINDOW IS CREATED, and it happens with the
                # button already up and the ghost already gone.
                source.move_view_to_new_window(
                    view, geometry=QRect(global_pos - self._offset,
                                         source.size()))
            self._settle_dpi(global_pos, view)
        finally:
            self._reset()

    def _cancel(self):
        """Escape. Nothing moved, so nothing has to be put back.

        This used to undo a window that had already been created and a view
        that had already been reparented. Now the document never left, so a
        cancel is the ghost disappearing and the grabs coming back.
        """
        self._release_input()
        try:
            self._clear_indicator()
            self._hide_ghost()
        finally:
            self._reset()

    def _hide_ghost(self):
        """Take the ghost off screen. Deleted on the next event loop pass
        rather than here: this runs inside a mouse handler, and destroying a
        top-level widget from inside one is the shape of the crash this
        rewrite removed."""
        ghost = self._ghost
        if ghost is None:
            return
        try:
            ghost.hide()
            ghost.deleteLater()
        except RuntimeError:                     # pragma: no cover - defensive
            pass

    def _abort(self):
        """A failure part way through starting. Give the input back and forget."""
        self._release_input()
        self._hide_ghost()
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
        if abs(_device_pixel_ratio(global_pos, self._source_window)
               - self._start_dpr) < 1e-3:
            return
        view.rerender_for_screen_change()
