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
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent, QPainter
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
        self._pixmap = None           # what the ghost draws, grabbed once
        self._source_window = None
        self._source_index = -1
        self._source_pos = QPoint()
        self._whole_window = False    # single-tab case: floating IS the source
        self._offset = QPoint()       # where in the tab the cursor took hold
        self._target = None           # (window, insertion index) under the cursor
        self._attached_to = None      # the window whose strip is holding it now
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
            if self._whole_window:
                # ONE TAB: DRAG THE WINDOW ITSELF, LIVE. There is no second
                # window to make and nothing to preview, so a ghost would be a
                # picture of a tab floating over the window it never left. His
                # words: "if one tab only exists in one window, it shouldnt
                # move, me dragging the tab should move the window".
                #
                # This is the one case the create-on-drop rule does not apply
                # to, and it is safe for the same reason: no window is created
                # or destroyed. An existing one is moved.
                self._offset = global_pos - source.frameGeometry().topLeft()
            else:
                # The hotspot is where in the tab the cursor took hold, so the
                # ghost sits under the pointer exactly where the real tab was.
                self._offset = QPoint(self._grab_in_tab)
                self._pixmap = bar.grab(bar.tabRect(index))
                self._attached_to = source
                # It starts life attached to the window it came from, so the
                # first move out of the strip detaches it exactly as a move out
                # of any other strip does. One rule, not two.
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
        target = self._hit_test(global_pos)
        self._set_target(target)

        if self._whole_window:
            # A LONE TAB KEEPS THE DEFERRED DROP, and it is not an oversight.
            # Live re-attach earns its keep by letting you watch a strip reflow
            # around the tab you are carrying; with one tab there is no strip
            # to reflow, and the thing following the cursor is the window
            # itself. Merging it into another window mid-drag would mean
            # emptying and closing the very window under the pointer.
            if self._source_window is not None:
                self._source_window.move(self.ghost_position(global_pos))
            return

        if target is not None:
            self._attach_to_strip(*target)

        # The ghost is shown only while the cursor is off the strip that holds
        # the tab. On the strip, the tab itself is the feedback and a second
        # picture of it would be one too many.
        holder = self._attached_to
        if holder is not None and self._over_strip(holder, global_pos):
            self._hide_ghost()
        else:
            self._show_ghost(global_pos)
            if self._ghost is not None:
                self._ghost.move(self.ghost_position(global_pos))

    def _over_strip(self, window, global_pos: QPoint) -> bool:
        """Whether the cursor is on `window`'s strip, give or take DOCK_MARGIN.

        Asymmetric on purpose, and this is the hysteresis: arriving costs
        DOCK_MARGIN and leaving costs DETACH_MARGIN, so the tab does not
        flicker between two strips while the cursor sits on a boundary.
        """
        try:
            bar = window.document_area().bar()
            local = bar.mapFromGlobal(global_pos)
        except (AttributeError, RuntimeError):   # pragma: no cover - defensive
            return False
        return bar.rect().adjusted(0, -DOCK_MARGIN, 0, DOCK_MARGIN).contains(local)

    def _attach_to_strip(self, window, index: int):
        """Put the tab INTO that strip, now, rather than promising to.

        THIS IS THE POINT OF THE CHANGE. What used to happen on approach was a
        wash over the target strip and an insertion line showing where the tab
        WOULD go, with the real move deferred until the button came up. Edge
        does not do that: get close and the tab is simply there, and the strip
        reflows around it. His words: "instead of the highlight showing where
        the tab will be displayed, if it get close just add the tab".

        THE VIEW IS NEVER HOMELESS, and that constraint shapes the whole
        gesture. `DocumentArea.adopt` and `detach` are a matched pair: adopt
        reparents the live widget into the destination stack and detach only
        then tidies the source, because the other order closes the source
        window while it is still the widget's parent and the view dies with it.
        So there is no "in mid-air" state for the DOCUMENT. It sits in whichever
        window last claimed it, and the ghost is only a picture of the thing
        being carried.

        Safe for the same reason create-on-drop is safe: this moves a live view
        between two windows that BOTH ALREADY EXIST, which is exactly what
        `move_view_to_window` was built for. Nothing is constructed or destroyed
        while the button is down, so the crash path stays closed.
        """
        holder = self._attached_to
        if holder is None or self._view is None:
            return
        try:
            if window is holder:
                area = window.document_area()
                at = area.index_of(self._view)
                if at < 0:
                    return
                landing = min(max(0, index), area.count() - 1)
                if landing != at:
                    area.bar().moveTab(at, landing)
            else:
                if not holder.move_view_to_window(self._view, window, index):
                    return
                window.activate_view(self._view)
                self._attached_to = window
        except (AttributeError, RuntimeError):   # pragma: no cover - defensive
            return

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
        """Kept as the tests' window onto where a drop would land.

        IT NO LONGER PAINTS ANYTHING. The accent wash, the outline and the
        insertion line all said "this is where the tab would go if you let go
        now", and they were answering a question the tab itself now answers by
        being there. Removing them also removes the gold box that was appearing
        mid-drag, at the source rather than by suppressing the symptom.
        """
        self._target = target

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
        view = self._view
        source = self._source_window
        attached = self._attached_to
        target = self._target
        try:
            self._hide_ghost()
            if self._whole_window:
                if target is not None and target[0] is not source:
                    # The lone-tab merge, deferred to here for the reason in
                    # `_track`: the source empties and closes behind it.
                    window, index = target
                    source.move_view_to_window(view, window, index)
                    window.activate_view(view)
                # Otherwise the window has been following the cursor all along
                # and is already exactly where they let go of it.
            elif target is not None:
                # OVER A STRIP, WHICH MEANS IT IS ALREADY IN ONE. The approach
                # attached it; the release only has to bring it forward. This
                # covers going back up to the source bar as well, and that case
                # is why the test for it exists: falling through to the branch
                # below would have made a second window out of a tab that never
                # actually left the first one.
                (attached or source).activate_view(view)
            else:
                # THE ONLY PLACE A WINDOW IS CREATED, and it happens with the
                # button already up, the ghost already gone, and the cursor
                # over no strip at all.
                (attached or source).move_view_to_new_window(
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
            self._hide_ghost()
            holder = self._attached_to
            if (holder is not None and self._source_window is not None
                    and holder is not self._source_window):
                # It has already joined another window's strip, so this cancel
                # has a real move to reverse.
                holder.move_view_to_window(
                    self._view, self._source_window, self._source_index)
                self._source_window.activate_view(self._view)
            if self._whole_window and self._source_window is not None:
                # The one thing a cancel still has to undo, because it is the
                # one thing that moved.
                self._source_window.move(self._source_pos)
        finally:
            self._reset()

    def _show_ghost(self, global_pos: QPoint):
        """Bring the picture back for the part of the drag that is in mid-air."""
        if self._ghost is not None or self._pixmap is None:
            return
        try:
            self._ghost = _DragGhost(self._pixmap)
            self._ghost.move(self.ghost_position(global_pos))
            self._ghost.show()
        except RuntimeError:                     # pragma: no cover - defensive
            self._ghost = None

    def _hide_ghost(self):
        """Take the ghost off screen. Deleted on the next event loop pass
        rather than here: this runs inside a mouse handler, and destroying a
        top-level widget from inside one is the shape of the crash this
        rewrite removed."""
        ghost = self._ghost
        if ghost is None:
            return
        self._ghost = None
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
