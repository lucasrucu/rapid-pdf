"""Several open documents in one window, one tab each.

Phase 2 of docs/tabs-plan.md. Phase 1 made a DocumentView that owns exactly one
PDF; this is the thing that holds more than one of them and puts a tab bar over
the top. MainWindow holds a DocumentArea where it used to hold a DocumentView,
and `MainWindow.view` becomes "the front one".

WHY A QTabBar OVER A QStackedWidget AND NOT A QTabWidget. QTabWidget couples
removing a tab to removing its page, and its stack is a private child
(`qt_tabwidget_stackedwidget`) nobody else owns. Phase 4's tear-off needs the
mid-drag states that coupling forbids: the tab gone from the bar while the
widget is still parented where we put it, and the widget moved into a new
window before any tab has been committed. Owning both halves gives us those
directly. The price is that the two can drift, so the invariant
`bar.count() == stack.count()` is asserted in `check_invariant()` and the tests
call it after every operation.

PHASE 3 ADDED THE OTHER HALF OF THE MOVE. `adopt` takes a LIVE view out of
another window's stack and `detach` gives one up, and between them they are
what "Move to New Window" is made of. The rule that makes it work is that
neither ever calls `setParent(None)`: `insertWidget` on the destination
reparents the view straight across, and on Windows a trip through top-level
would create a real HWND and destroy it again on the way back, taking the
view's native resources with it.

Background tabs also stop holding what nobody is looking at, which is why
`_set_current_view` tells the leaving view it is no longer active. The
releasing itself is DocumentView's, where phase 1 put the clone lifecycle.

PHASE 4 ADDED THE GESTURE AND THE VISIT HISTORY. The tear-off itself lives in
`ui/tab_tear_off.py`; what is here is the three mouse events and the key press
forwarded into it, and the insertion line the bar paints while a tab is hovering
over it. It drives `detach`/`adopt` through MainWindow exactly as the menu item
does, so switching it off would cost the gesture and nothing else.

The MRU list is the other half. `_mru` is a visit history for TABS, which is a
different list from the registry's activation order for WINDOWS, and Ctrl+Tab
needs the first one. It is FROZEN while the walk is in flight (`_mru_walk`), so
holding Ctrl and tapping Tab walks a stable stack instead of swapping the top
two entries back and forth forever. Ctrl+PgDn stays strictly positional: the
two orders are deliberately different and `step_current` is untouched.
"""

from __future__ import annotations

import os

from PySide6.QtCore import (
    QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import QAction, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QHBoxLayout, QMenu, QSizePolicy, QStackedWidget, QStyle,
    QTabBar, QToolButton, QVBoxLayout, QWidget,
)

from ui.page_drag import read_page_mime
from ui.tab_tear_off import TabTearOff

# Tabs share the bar equally until they hit the ceiling, then shrink to the
# floor, and only once every tab is at the floor does the bar start scrolling.
# That order is the point: scrolling hides tabs, eliding only shortens them.
TAB_MIN_WIDTH = 92
TAB_MAX_WIDTH = 240

# The chevron button, which now sits to the LEFT of the tabs. Square, so it
# lines up with the new-tab button at the other end of the strip.
CHEVRON_WIDTH = 26
CHEVRON_HEIGHT = 26

# The arrowhead drawn inside it. Small: this is a disclosure mark, not an icon.
CHEVRON_GLYPH_WIDTH = 8.0
CHEVRON_GLYPH_HEIGHT = 3.0

# Everything in the title-bar row that is NOT tabs: the app icon, the chevron,
# the new-tab button, the drag gap and the three window controls. Subtracted
# from the WINDOW width to get the room the tabs may share out.
#
# WHY THE WINDOW AND NOT THE BAR. The share used to be a function of the bar's
# own width, which was fine while the bar was stretched across the whole strip.
# It is not stretched any more: it hugs its tabs so the new-tab button can sit
# against the last one, the way it does in a browser. A hugging widget takes its
# width from its own size hint, so a hint computed from that same width is a
# feedback loop that settles on whichever value it was seeded with. The window's
# width does not depend on this bar, which is what makes it a usable budget.
#
# Over-estimating is safe and under-estimating is not: too small a budget only
# means slightly narrower tabs, while too large lets the last tab slide under
# the new-tab button.
TAB_STRIP_RESERVE = 330

# Empty bar kept after the last tab. Two jobs, and it would be worth keeping for
# either one alone: it is the only surface the double-click-to-open-a-tab
# gesture has left now that the bar hugs its tabs, and it is the gap a browser
# leaves between the last tab and the plus button.
#
# IT WAS 28 AND THAT READ AS "FAR AWAY". Chrome and Edge leave about eight
# pixels, and at 28 the plus looked detached from the strip it belongs to,
# which was the complaint. Ten is enough to double-click into and close enough
# that the button reads as part of the tabs.
TRAILING_SLACK = 10

# The unsaved-changes dot, drawn in place of the close X.
DIRTY_DOT_RADIUS = 3.5

# The close control: the size of the whole button, which is also its hover pill,
# and the width of the X drawn inside it. The X was previously derived from
# `adjusted(5, 5, -5, -5)` on a 16px box, which is a 6px span; naming it keeps
# the glyph and the box independent, so the pill can grow without the mark
# growing with it.
CLOSE_BUTTON_SIZE = 16
CLOSE_GLYPH_SPAN = 6.0

# How far the close button's right edge sits inside the tab's right edge.
#
# THIS EXISTS BECAUSE QSS CANNOT SET IT. `QStyleSheetStyle` computes
# SE_TabBarTabRightButton itself, reads neither the `::tab` padding nor its
# margin, and never delegates to the widget's style, so a QProxyStyle override
# is not consulted either. Both were measured rather than assumed: raising
# `padding-right` from 6px to 20px moved the button by zero pixels, and a proxy
# style counted zero calls. Qt pins the button one pixel inside the tab rect,
# which against 10px of left padding is what "not centred at all" looks like.
# The only remaining lever is to move the widget, which is
# `DocumentTabBar._place_close_buttons`.
CLOSE_BUTTON_RIGHT_INSET = 10

# The line drawn where a torn-off tab would land. In the accent, and full
# height: it has to read as "between these two tabs" from the corner of the eye,
# while the thing actually being looked at is the window under the cursor.
#
# It was two pixels and that was not enough. Two pixels of accent, on a strip
# the floating window was sitting on top of, was a signal nobody reported
# seeing: the drop worked and looked like luck. So the line is wider, and it is
# no longer alone: `set_drop_active` washes the whole target strip in the accent
# so the answer to "which window is this going into" is readable without hunting
# for a hairline. See ui/tab_tear_off.py, where the floating window is also
# moved down out of the way of both.
DROP_LINE_WIDTH = 4

# How much of the accent the target strip is washed in while a tear-off is over
# it. Enough to read as "this one", not so much that the tab labels underneath
# stop being legible.
DROP_WASH_ALPHA = 46

# The accent outline around the target strip, under the wash.
DROP_OUTLINE_WIDTH = 2

# Matched to the tab's own corner radius in ui/theme.py. A square frame drawn
# around a row of rounded tabs reads as a second shape arguing with the first.
DROP_OUTLINE_RADIUS = 8

# Below this width the strip is not painted with drop feedback at all. A wash
# and an outline are a way of saying "this whole strip"; on a bar a few pixels
# wide they say "here is a small gold box", which is what a user actually
# reported seeing while dragging a tab. One tab at the floor width is the
# smallest thing that can carry the message, so that is the threshold.
DROP_FEEDBACK_MIN_WIDTH = TAB_MIN_WIDTH

# The mark on a tab that has been ticked for "Move Selected to New Window". A
# bar along the top edge of the tab, in the accent, because the tab's own
# selected state already owns its background and its underline.
CHECK_MARK_HEIGHT = 3


def tab_titles(paths: list) -> list:
    """A short, unique label for each open document, in tab order.

    `paths` carries one entry per tab: the file it came from, or None for an
    untitled document (a combine that has never been saved).

    The rule is the one every editor with tabs uses. Start at the basename
    without its extension, and where two tabs would read the same, walk both up
    their own path one folder at a time until they differ. Untitled documents
    are numbered by their position among the untitled ones in this window, so
    closing "Untitled 1" renumbers the rest; the whole bar is recomputed on
    every add and remove anyway, which is what keeps that honest.
    """
    titles: list = [None] * len(paths)
    untitled = 0
    real = []
    for i, path in enumerate(paths):
        if path:
            real.append(i)
        else:
            untitled += 1
            titles[i] = f"Untitled {untitled}"

    parts = {i: _path_parts(paths[i]) for i in real}
    depth = {i: 0 for i in real}

    # Bounded by the longest path: every round either deepens a colliding tab
    # or finds nothing left to deepen, and stops.
    for _ in range(max((len(p) for p in parts.values()), default=0) + 1):
        labels = {i: _label(parts[i], depth[i]) for i in real}
        clashes = {}
        for i in real:
            clashes.setdefault(labels[i], []).append(i)
        grew = False
        for group in clashes.values():
            if len(group) < 2:
                continue
            if len({tuple(parts[i]) for i in group}) == 1:
                # The same file open twice. No amount of walking up separates
                # two identical paths, and trying would grow both to the root.
                continue
            for i in group:
                if depth[i] < len(parts[i]) - 1:
                    depth[i] += 1
                    grew = True
        if not grew:
            break

    # Two tabs on the SAME file cannot be told apart by path (Duplicate Tab is
    # the only way to get there, since opening a file that is already open just
    # activates its tab). Number them instead of showing two identical labels.
    seen: dict = {}
    for i in real:
        label = _label(parts[i], depth[i])
        seen[label] = seen.get(label, 0) + 1
        titles[i] = label if seen[label] == 1 else f"{label} ({seen[label]})"
    return titles


def _path_parts(path: str) -> list:
    """A path split into folders plus the basename with its extension dropped."""
    parts = os.path.normpath(path).replace("\\", "/").split("/")
    parts[-1] = os.path.splitext(parts[-1])[0]
    return [p for p in parts if p] or [parts[-1]]


def _label(parts: list, depth: int) -> str:
    depth = min(depth, len(parts) - 1)
    return "/".join(parts[len(parts) - 1 - depth:])


class _TabListButton(QToolButton):
    """The "all open documents" control, at the left of the strip.

    IT PAINTS ITS OWN ARROWHEAD instead of setting one as text, and that is the
    whole reason the class exists. It carried a text glyph before, U+2304 DOWN
    ARROWHEAD, which is a thinly covered codepoint: where a font does not have
    it the button renders as an empty box or as nothing at all, and where a
    fallback font supplies it the size and baseline are whatever that font
    happens to use, which is the other half of "it is not centred". Two lines
    from a QPainter are the same shape in every font on every machine.

    The close button next to every tab already draws its X this way, so this is
    the established answer here rather than a new idea.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._glyph = QColor("#666666")

    def apply_palette(self, palette):
        self._glyph = QColor(palette.text_dim)
        self.update()

    def paintEvent(self, event):
        # The stylesheet still owns the background, the radius and the hover and
        # pressed fills; this only adds the mark on top.
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self._glyph)
        pen.setWidthF(1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        centre = self.rect().center()
        half = CHEVRON_GLYPH_WIDTH / 2
        drop = CHEVRON_GLYPH_HEIGHT / 2
        # A v, drawn as one polyline so the corner joins cleanly.
        painter.drawPolyline([
            QPointF(centre.x() - half, centre.y() - drop + 0.5),
            QPointF(centre.x(), centre.y() + drop + 0.5),
            QPointF(centre.x() + half, centre.y() - drop + 0.5),
        ])


class _TabCloseButton(QAbstractButton):
    """The control at the right of a tab: an X, or a dot while it is unsaved.

    The dot is not decoration next to the X, it REPLACES it, and hovering turns
    it back into an X. That is the arrangement every editor settled on because
    the two things want the same few pixels and the one you need is decided by
    what you are about to do: reading the bar, you want to know which documents
    are unsaved; reaching for this button, you want the close control.

    EVERYTHING HERE IS DRAWN IN FLOATING POINT, AND THAT IS THE FIX FOR HALF OF
    "the x is not centered at all". `QRect(0, 0, 16, 16).center()` is QPoint(7,
    7), not 8: QRect::center rounds down because a QRect's right edge is its
    last pixel rather than its bound. The X was built from that point and from
    `adjusted(5, 5, -5, -5)`, which put its midpoint at 7.5 while the true
    centre of the box is 8.0. Half a pixel, on an antialiased 1.3px pen, is a
    visibly soft, lopsided glyph rather than a crisp one. QRectF has no such
    rounding, so the centre is exact and both strokes land on the same point.

    The other half of the problem is not in this class and cannot be fixed from
    here: see `DocumentTabBar._place_close_buttons`.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFixedSize(CLOSE_BUTTON_SIZE, CLOSE_BUTTON_SIZE)
        self._dirty = False
        self._glyph = QColor("#666666")
        self._hover_bg = QColor(0, 0, 0, 28)

    def apply_palette(self, palette):
        self._glyph = QColor(palette.text_dim)
        hover = QColor(palette.surface_hover)
        hover.setAlpha(200)
        self._hover_bg = hover
        self.update()

    def set_dirty(self, dirty: bool):
        if dirty == self._dirty:
            return
        self._dirty = dirty
        self.update()

    def is_dirty(self) -> bool:
        return self._dirty

    def shows_dot(self) -> bool:
        """What the button is painting right now. Named for the tests, which
        cannot see pixels but can ask the question the pixels answer."""
        return self._dirty and not self.underMouse()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(CLOSE_BUTTON_SIZE, CLOSE_BUTTON_SIZE)

    def glyph_centre(self) -> QPointF:
        """The exact middle of the button, in float. Named for the tests, which
        cannot see pixels but can ask where the mark was put."""
        return QRectF(self.rect()).center()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        centre = rect.center()
        if self.shows_dot():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._glyph)
            painter.drawEllipse(centre, DIRTY_DOT_RADIUS, DIRTY_DOT_RADIUS)
            return
        if self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._hover_bg)
            # Half a pixel in on every side so the rounded edge sits inside the
            # button rather than straddling its boundary.
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        pen = QPen(self._glyph)
        pen.setWidthF(1.3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        half = CLOSE_GLYPH_SPAN / 2
        painter.drawLine(QPointF(centre.x() - half, centre.y() - half),
                         QPointF(centre.x() + half, centre.y() + half))
        painter.drawLine(QPointF(centre.x() + half, centre.y() - half),
                         QPointF(centre.x() - half, centre.y() + half))


#: How long a page drag has to rest over a tab before that tab comes forward.
#: Long enough that dragging ACROSS the bar on the way somewhere else never
#: fires, short enough that deliberately parking on a tab does not feel stuck.
HOVER_SWITCH_MS = 550

#: How far the cursor may wander and still count as resting on the same tab.
#: A hand holding a drag still is not still, and restarting the countdown on
#: every pixel of tremor means it never completes.
HOVER_JITTER_PX = 4


class DocumentTabBar(QTabBar):
    """The bar itself. Widths, the dirty dot, and the two gestures Qt does not
    give us: double-click on empty space, and a context menu per tab.

    IT IS ALSO A DRAG TARGET, and a strange one: it accepts a page drag so it
    can HOVER-SWITCH, and then refuses every drop. Dragging a page onto a tab
    and holding still brings that tab forward mid-drag, so the page can be
    dropped into a document that was not on screen when the drag started. The
    drop itself belongs to the strip or the grid underneath, so `dragMoveEvent`
    ignores the event: Qt keeps delivering moves to a widget that accepted the
    ENTER, which is exactly the shape this needs.

    Switching does not end the drag. Nothing here touches the QDrag; the stack
    simply shows a different widget under a cursor that is still holding pages.
    """

    #: Double-clicked past the last tab.
    new_tab_requested = Signal()
    #: (tab index, global position) - right-clicked on a tab.
    tab_menu_requested = Signal(int, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("documentTabBar")
        self.setTabsClosable(True)      # middle-click close comes with it
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.setUsesScrollButtons(True)
        self.setMovable(True)
        self.setDrawBase(False)
        self.setExpanding(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self._palette = None
        # Phase 4. Set by DocumentArea, which owns both halves of the move.
        self._tear_off = None
        # x of the insertion line while a torn-off tab is over this bar, or None.
        self._drop_x = None
        # Whether this window is the drop target at all. Separate from the line
        # above because the dock zone is now the WHOLE window: the cursor can be
        # down in the page view with this strip still being the thing that will
        # receive the document, and the strip has to say so.
        self._drop_active = False
        # Tabs ticked for "Move Selected to New Window", by index. Pushed down
        # from DocumentArea, which holds the real answer as VIEWS: an index goes
        # stale the moment a tab is dragged along the bar.
        self._checked = ()
        # Phase 5. Hover-switch while a PAGE drag is over the bar: which tab
        # the cursor is resting on, where it was when the countdown started,
        # and the countdown itself.
        self.setAcceptDrops(True)
        self._hover_index = -1
        self._hover_pos = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(HOVER_SWITCH_MS)
        self._hover_timer.timeout.connect(self._on_hover_elapsed)

    # -- hover-switch under a page drag --------------------------------

    def dragEnterEvent(self, event):
        """Accept a PAGE drag only, and only so move events keep arriving.

        Nothing else is welcome: a file dropped from Explorer belongs to the
        window, and the tear-off never enters Qt's drag system at all (phase 4),
        so there is no third case to worry about.
        """
        if read_page_mime(event.mimeData()) is None:
            self._cancel_hover()
            event.ignore()
            return
        event.accept()

    def dragMoveEvent(self, event):
        """Count down to a tab switch, and never accept the drop itself."""
        if read_page_mime(event.mimeData()) is None:
            self._cancel_hover()
            event.ignore()
            return
        pos = (event.position().toPoint() if hasattr(event, "position")
               else event.pos())
        index = self.tabAt(pos)
        wandered = (self._hover_pos is None
                    or (pos - self._hover_pos).manhattanLength() > HOVER_JITTER_PX)
        if index != self._hover_index or wandered:
            self._hover_index = index
            self._hover_pos = pos
            self._hover_timer.stop()
            if index >= 0 and index != self.currentIndex():
                self._hover_timer.start()
        # Deliberately ignored: hovering here switches tabs, it does not land
        # pages. The drop belongs to the strip or the grid under the bar.
        event.ignore()

    def dragLeaveEvent(self, event):
        self._cancel_hover()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._cancel_hover()
        event.ignore()

    def _cancel_hover(self):
        self._hover_timer.stop()
        self._hover_index = -1
        self._hover_pos = None

    def _on_hover_elapsed(self):
        """The cursor rested long enough. Bring that tab forward."""
        index = self._hover_index
        if 0 <= index < self.count() and index != self.currentIndex():
            self.setCurrentIndex(index)

    def hover_switch_timer(self) -> QTimer:
        """The countdown, for the tests. A real drag loop cannot run in one."""
        return self._hover_timer

    # -- the tear-off gesture ------------------------------------------

    def set_tear_off(self, controller):
        """The phase 4 gesture controller, installed by DocumentArea."""
        self._tear_off = controller

    def tear_off(self):
        return self._tear_off

    def set_drop_indicator(self, x):
        """Draw (or clear) the insertion line at `x`. None clears it."""
        if x == self._drop_x:
            return
        self._drop_x = x
        self.update()

    def drop_indicator(self):
        """Where the insertion line is, or None. Named for the tests, which
        cannot see pixels but can ask the question the pixels answer."""
        return self._drop_x

    def set_drop_active(self, active: bool):
        """Wash this strip in the accent, or stop.

        The louder half of the drag feedback, and the half that answers the
        question people actually ask mid-drag: not "between which two tabs" but
        "is it going into THIS window". It is set whenever the cursor is
        anywhere over this window, which since the dock zone was widened is most
        of the screen area a drag crosses.
        """
        if active == self._drop_active:
            return
        self._drop_active = active
        self.update()

    def drop_active(self) -> bool:
        return self._drop_active

    def _can_paint_drop_feedback(self) -> bool:
        """Whether this bar is a real strip rather than a few stray pixels.

        THIS IS THE BACKSTOP, NOT THE FIX. The reported artifact was "a small
        gold box floating in the caption": a wash and a 2px outline are a way
        of saying "this whole strip", and around a bar a few pixels wide they
        say something else entirely. The width threshold was the first answer
        and it is kept, with an empty bar refused outright, because a bar with
        nothing in it has no gap for a tab to land between and so can never
        legitimately be lit.

        The condition that actually produced the box is upstream of here and is
        now handled there: a window holding one empty document hides its whole
        header (`_sync_header_visibility`), and the tear-off used to name that
        window as a target and light its bar anyway. `_show_drop_feedback` now
        declines to light a strip that is not on screen, so the state is never
        set rather than being set and then not drawn.
        """
        return self.count() > 0 and self.width() >= DROP_FEEDBACK_MIN_WIDTH

    def set_checked_indices(self, indices):
        """Which tabs are ticked, for painting. DocumentArea owns the truth."""
        indices = tuple(sorted(set(indices)))
        if indices == self._checked:
            return
        self._checked = indices
        self.update()

    def checked_indices(self) -> tuple:
        return self._checked

    def insertion_x(self, index: int) -> int:
        """The x a tab inserted at `index` would start at.

        Past the last tab is its right edge, so dropping on empty bar space
        draws the line after everything rather than at the origin.
        """
        if self.count() == 0:
            return 0
        if index >= self.count():
            return self.tabRect(self.count() - 1).right()
        return self.tabRect(max(0, index)).left()

    def mousePressEvent(self, event):
        """Record the grab, then let QTabBar have the event.

        `super()` is not optional: it is what still gives us Qt's own reorder
        and the current-tab change. The tear-off decides nothing here.
        """
        if self._tear_off is not None:
            self._tear_off.press(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Once the threshold is crossed the gesture owns the mouse, and feeding
        # QTabBar any more moves would have it reordering a tab that is no
        # longer in this bar.
        if self._tear_off is not None and self._tear_off.move(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._tear_off is not None and self._tear_off.release(event):
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        # Only ever reached mid-drag: the bar has NoFocus, and the gesture
        # grabs the keyboard precisely so Escape lands here.
        if self._tear_off is not None and self._tear_off.key_press(event):
            return
        super().keyPressEvent(event)

    # -- geometry ------------------------------------------------------

    def _button_side(self) -> QTabBar.ButtonPosition:
        """Which end of the tab the style puts the close control on."""
        side = self.style().styleHint(
            QStyle.StyleHint.SH_TabBar_CloseButtonPosition, None, self)
        return QTabBar.ButtonPosition(side)

    def tabSizeHint(self, index: int) -> QSize:
        """Equal shares of the strip's budget, capped and floored.

        Qt's own hint is the width of the text, which makes a long filename take
        half the bar. Sharing the room out instead is what makes tabs shrink
        before the bar starts scrolling, which is the behaviour asked for.

        The budget is the WINDOW's width less TAB_STRIP_RESERVE, not this bar's
        width. See that constant for why: the bar hugs its tabs now, so its own
        width is downstream of this hint and cannot also be its input.
        """
        hint = super().tabSizeHint(index)
        count = max(1, self.count())
        share = self._share_budget() // count
        hint.setWidth(max(TAB_MIN_WIDTH, min(TAB_MAX_WIDTH, share)))
        return hint

    def _share_budget(self) -> int:
        """The width the tabs may divide between them."""
        window = self.window()
        reference = window.width() if window is not None else self.width()
        return max(TAB_MIN_WIDTH, reference - TAB_STRIP_RESERVE)

    def _place_close_buttons(self):
        """Move every close button to where the stylesheet implies it should be.

        THE STYLESHEET CANNOT DO THIS, which is the whole reason the method
        exists. `QStyleSheetStyle` works out SE_TabBarTabRightButton on its own,
        consults neither the `::tab` padding nor its margin, and does not fall
        through to the widget's style, so overriding `subElementRect` in a
        QProxyStyle is not consulted either. Both facts were measured, not
        inferred: taking `padding-right` from 6px to 20px moved the button by
        exactly zero pixels, and a proxy style installed on this bar counted
        exactly zero calls. What Qt actually does is pin the button one pixel
        inside the tab rect, which next to 10px of left padding is a tab with
        breathing room down one side and none down the other.

        Vertically Qt centres on the tab RECT, which is right only while the
        stylesheet's top and bottom margins match. They now do, deliberately;
        see the tab block in ui/theme.py. So this only has to correct x, and it
        deliberately leaves y alone rather than duplicating arithmetic that is
        already correct and would then have two owners.

        CALLED FROM THREE PLACES AND IT NEEDS ALL THREE. `tabLayoutChange` is
        the documented hook and covers adding, removing, moving and resizing.
        It is not enough on its own: `QTabBar::setTabButton` re-lays out that
        one tab directly without ever raising `tabLayoutChange`, so a button
        installed on a new tab would sit at Qt's position until something else
        happened to the bar. `resizeEvent` is the third because the share
        budget, and therefore every tab rect, moves with the window.
        """
        side = self._button_side()
        for i in range(self.count()):
            button = self.tabButton(i, side)
            if not isinstance(button, _TabCloseButton):
                continue
            rect = self.tabRect(i)
            if rect.isEmpty():
                continue
            geometry = button.geometry()
            x = rect.right() - geometry.width() - CLOSE_BUTTON_RIGHT_INSET
            if x != geometry.x():
                button.move(x, geometry.y())

    def tabLayoutChange(self):
        super().tabLayoutChange()
        self._place_close_buttons()

    def sizeHint(self) -> QSize:
        """The tabs, plus a deliberate strip of nothing after the last one.

        WHY THE SLACK EXISTS. Double-clicking empty bar space opens a new tab
        (`mouseDoubleClickEvent`), and a bar that hugs its tabs exactly has no
        empty space for that gesture to land on: the feature would still be in
        the code and unreachable with a mouse. Hugging is what puts the new-tab
        button against the last tab, so the answer is to hug with a margin
        rather than to give the stretch back.

        It is also what a browser looks like. Chrome and Edge both leave a gap
        between the last tab and the plus, and neither butts them together.
        """
        hint = super().sizeHint()
        hint.setWidth(hint.width() + TRAILING_SLACK)
        return hint

    def resizeEvent(self, event):
        # The share is a function of the WINDOW's width, and a window resize
        # reaches this bar as a resize of its own, so this is still the right
        # place to re-ask for the hint.
        super().resizeEvent(event)
        self.updateGeometry()
        self._place_close_buttons()

    # -- the dirty dot -------------------------------------------------

    def install_close_button(self, index: int) -> _TabCloseButton:
        """Put our own close control on a tab, replacing Qt's plain X.

        `setTabsClosable(True)` stays on: it is what gives us middle-click close
        for free (finding 3 in the plan), and it is independent of which widget
        is actually sitting in the button slot.
        """
        button = _TabCloseButton(self)
        if self._palette is not None:
            button.apply_palette(self._palette)
        button.clicked.connect(lambda: self._on_close_clicked(button))
        self.setTabButton(index, self._button_side(), button)
        # setTabButton lays this one tab out again WITHOUT raising
        # tabLayoutChange, so without this the button would keep Qt's placement
        # until the next unrelated relayout. See _place_close_buttons.
        self._place_close_buttons()
        return button

    def _on_close_clicked(self, button: _TabCloseButton):
        # The button's index moves under it whenever a tab is added, removed or
        # dragged, so it is looked up at the moment of the click rather than
        # captured when the button was made.
        side = self._button_side()
        for i in range(self.count()):
            if self.tabButton(i, side) is button:
                self.tabCloseRequested.emit(i)
                return

    def close_button(self, index: int) -> _TabCloseButton | None:
        button = self.tabButton(index, self._button_side())
        return button if isinstance(button, _TabCloseButton) else None

    def set_tab_dirty(self, index: int, dirty: bool):
        button = self.close_button(index)
        if button is not None:
            button.set_dirty(dirty)

    def apply_palette(self, palette):
        self._palette = palette
        side = self._button_side()
        for i in range(self.count()):
            button = self.tabButton(i, side)
            if isinstance(button, _TabCloseButton):
                button.apply_palette(palette)

    # -- gestures ------------------------------------------------------

    def _accent(self) -> QColor:
        return QColor(self._palette.accent) if self._palette is not None \
            else QColor("#3b82f6")

    def _accent_press(self) -> QColor:
        """The accent's pressed value, used for marks drawn ON the accent."""
        return QColor(self._palette.accent_press) if self._palette is not None \
            else QColor("#1d4ed8")

    def paintEvent(self, event):
        """The tabs, then the tick marks, then the drop feedback over the lot.

        All three are drawn on top rather than as part of a tab, and for the
        same reason in each case: they belong to somewhere a tab is not. The
        insertion line belongs to the gap BETWEEN two tabs and there is no tab
        at the end of the bar for it to belong to; the drop wash belongs to the
        strip; and the tick marks are the only thing here that is per tab, kept
        with the other two so one QPainter covers all of it.
        """
        super().paintEvent(event)
        if not self._checked and not self._drop_active and self._drop_x is None:
            return
        painter = QPainter(self)
        accent = self._accent()

        for index in self._checked:
            if 0 <= index < self.count():
                rect = self.tabRect(index)
                painter.fillRect(
                    QRect(rect.left(), rect.top(), rect.width(),
                          CHECK_MARK_HEIGHT), accent)

        # THE LITTLE YELLOW SQUARE, and why the drop feedback is now gated on a
        # width. The wash and the outline are meant to light up a whole tab
        # strip so it reads as "this window". They were painted on self.rect()
        # unconditionally, and a bar with no tabs in it has a rect a few pixels
        # wide: an alpha wash plus a 2px accent outline around that is not a
        # highlighted strip, it is a small gold box floating in the caption,
        # which is exactly what got reported. A window holding one empty
        # document hides its header (see _sync_header_visibility), and the
        # tear-off still names that window as the target and still calls
        # set_drop_active on its bar, so the case is reached routinely rather
        # than rarely. Since the bar now hugs its tabs it is narrower than it
        # used to be, which would have made this more common, not less.
        if self._drop_active and self._can_paint_drop_feedback():
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            wash = QColor(accent)
            wash.setAlpha(DROP_WASH_ALPHA)
            inset = DROP_OUTLINE_WIDTH / 2
            frame = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(wash)
            # Rounded to the tab's own radius: a square frame around a row of
            # rounded tabs reads as a second, competing shape.
            painter.drawRoundedRect(frame, DROP_OUTLINE_RADIUS,
                                    DROP_OUTLINE_RADIUS)
            pen = QPen(accent)
            pen.setWidth(DROP_OUTLINE_WIDTH)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(frame, DROP_OUTLINE_RADIUS,
                                    DROP_OUTLINE_RADIUS)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # Same gate, same reason: a 4px line on a bar barely wider than 4px is
        # the same artifact wearing a different shape.
        #
        # NOT IN THE ACCENT, AND THAT IS THE POINT. The line used to be the
        # same colour as the wash and the outline it is drawn on top of, which
        # is invisible by construction: at index 0 it lands exactly under the
        # frame's left edge and the two merge into one thick gold border that
        # says nothing about position. A pressed-accent line reads as a mark ON
        # the highlighted strip in both themes, darker than the wash in light
        # and brighter than it in dark. It is also held inside the frame rather
        # than clamped to the bar, so the first and last positions are as
        # legible as the ones in the middle.
        if self._drop_x is not None and self._can_paint_drop_feedback():
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            edge = DROP_OUTLINE_WIDTH + 1
            left = max(edge, min(int(self._drop_x) - DROP_LINE_WIDTH // 2,
                                 self.width() - DROP_LINE_WIDTH - edge))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._accent_press())
            painter.drawRoundedRect(
                QRectF(left, edge, DROP_LINE_WIDTH,
                       max(1, self.height() - 2 * edge)),
                DROP_LINE_WIDTH / 2, DROP_LINE_WIDTH / 2)

    def mouseDoubleClickEvent(self, event):
        """Empty space opens a new tab. A double-click ON a tab does nothing.

        Deliberately nothing: the obvious candidates (rename, close) are both
        destructive-by-accident on a control people double-click to bring a
        window forward.
        """
        if event.button() == Qt.MouseButton.LeftButton \
                and self.tabAt(event.position().toPoint()) < 0:
            self.new_tab_requested.emit()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        index = self.tabAt(event.pos())
        if index < 0:
            return
        self.tab_menu_requested.emit(index, event.globalPos())


class DocumentArea(QWidget):
    """The tab bar and the stack of DocumentViews under it, kept in step."""

    #: (previous view or None, new front view or None). The window rebinds its
    #: chrome on this: see MainWindow._on_front_view_changed.
    current_view_changed = Signal(object, object)

    #: A view asked to be closed (File > Close PDF reached it, or its own X).
    #: Carries the view, not an index, because the index moves.
    view_close_requested = Signal(object)

    #: The bar wants a new empty tab (double-click on empty space).
    new_tab_requested = Signal()

    #: (view,) - the context menu's Duplicate Tab.
    duplicate_requested = Signal(object)

    #: (index,) - the tab's own close control, its middle-click, or the
    #: context menu. The window decides what closing means; see close_tab.
    tab_close_requested = Signal(int)

    #: (view,) - the context menu's Move to New Window. The window owns the
    #: move because it needs the registry to make the destination.
    move_to_new_window_requested = Signal(object)

    #: (list of views,) - the context menu's Move Selected to New Window. The
    #: cheap half of multi-tab move: several tabs ticked one at a time, then one
    #: command that takes all of them at once. Carries views rather than
    #: indices, because the indices move as the first one leaves.
    move_selected_to_new_window_requested = Signal(list)

    #: () - which tabs are ticked has changed. The window watches it so nothing
    #: else has to poll `checked_views()`.
    checked_views_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_view = None
        self._syncing = False       # a bar/stack edit is mid-flight
        # Tabs ticked for "Move Selected to New Window", in tab order, held as
        # VIEWS. Indices were the obvious choice and are wrong: dragging a tab
        # along the bar renumbers every one of them, and so does closing a tab
        # to the left of a ticked one.
        #
        # This is deliberately NOT a selection model on the QTabBar. QTabBar has
        # exactly one current tab and no concept of a second selected one, and
        # giving it one means reimplementing its painting, its keyboard handling
        # and its drag reordering. The pages grid has a real selection model
        # (ui/page_panel.py, ui/organizer.py) because a grid of pages is a
        # QListWidget and gets one for free. A bar of tabs is not, so this is a
        # ticked LIST plus a mark painted over the top, and the whole feature
        # lives in the context menu where nobody has to discover a modifier.
        self._checked: list = []
        # Most recently looked at FIRST. A visit history for TABS, which is not
        # the same list as the registry's activation order for WINDOWS: that
        # one answers "where does a new file go", this one answers Ctrl+Tab.
        self._mru: list = []
        # A frozen copy of the above while Ctrl is held down, plus where in it
        # the walk has got to. None means no walk is in flight.
        self._mru_walk = None
        self._mru_pos = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("documentTabHeader")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)

        # Every tab in one list, for when there are more than the bar can show
        # at a readable width. It is the answer to "which of these is scrolled
        # off", which scroll buttons alone never give you.
        #
        # IT IS FIRST IN THE ROW, to the left of the tabs, which is where Edge
        # puts the same control. It used to be last, and because the bar was
        # stretched across the whole strip "last" meant flung to the far right
        # with a lane of empty caption between it and the tabs it belonged to.
        #
        # setFixedSize, not setFixedWidth. With only a width set, a QToolButton
        # keeps its default Preferred height and grew to the full height of the
        # row, while the tab bar beside it is vertically Fixed and sat at its own
        # hint. Two neighbours on different heights is what "not centred" was.
        self._chevron = _TabListButton()
        self._chevron.setObjectName("documentTabChevron")
        self._chevron.setToolTip("All open documents")
        self._chevron.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chevron.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._chevron.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._chevron.setFixedSize(CHEVRON_WIDTH, CHEVRON_HEIGHT)
        self._chevron.clicked.connect(self._show_all_tabs_menu)
        header_row.addWidget(self._chevron, 0, Qt.AlignmentFlag.AlignVCenter)

        self._bar = DocumentTabBar()
        # Preferred, not Expanding. The bar now hugs its tabs so that the
        # new-tab button in the title bar lands against the last one instead of
        # against the window controls. See TAB_STRIP_RESERVE.
        self._bar.setSizePolicy(QSizePolicy.Policy.Preferred,
                                QSizePolicy.Policy.Fixed)
        self._bar.currentChanged.connect(self._on_current_changed)
        self._bar.tabMoved.connect(self._on_tab_moved)
        self._bar.tabCloseRequested.connect(self.tab_close_requested)
        self._bar.new_tab_requested.connect(self.new_tab_requested)
        self._bar.tab_menu_requested.connect(self._on_tab_menu)
        # Phase 4. The bar forwards its mouse events in here; everything the
        # gesture then does goes back out through MainWindow's move methods.
        self._tear_off = TabTearOff(self._bar, self)
        self._bar.set_tear_off(self._tear_off)
        # No trailing stretch here on purpose. This header hugs its two widgets
        # so the title-bar row can put the new-tab button immediately after the
        # last tab; the stretch that fills the rest of the caption lives there,
        # after that button. See ui/title_bar.py.
        header_row.addWidget(self._bar, stretch=0)

        self._header = header
        root.addWidget(header)

        self._stack = QStackedWidget()
        root.addWidget(self._stack, stretch=1)
        self._sync_header_visibility()

    # ------------------------------------------------------------------
    # What the window asks
    # ------------------------------------------------------------------

    def bar(self) -> DocumentTabBar:
        return self._bar

    def header(self) -> QWidget:
        """The strip holding the bar and the chevron.

        Handed out so the window's title bar can HOST it: the tabs are the top
        row of the window now (see ui/title_bar.py). Reparenting it changes
        nothing about who owns it; every signal, every gesture and the invariant
        below are still this widget's.
        """
        return self._header

    def stack(self) -> QStackedWidget:
        return self._stack

    def count(self) -> int:
        return self._bar.count()

    def current_index(self) -> int:
        return self._bar.currentIndex()

    def current_view(self):
        return self._current_view

    def view_at(self, index: int):
        if 0 <= index < self._stack.count():
            return self._stack.widget(index)
        return None

    def index_of(self, view) -> int:
        return self._stack.indexOf(view)

    def views(self) -> list:
        return [self._stack.widget(i) for i in range(self._stack.count())]

    def index_of_path(self, path: str) -> int:
        """The tab already showing this file, or -1.

        Opening a file that is already open activates its tab instead of
        making a second copy of it, so this is what that check runs on.
        """
        target = os.path.normcase(os.path.abspath(path))
        for i, view in enumerate(self.views()):
            open_path = view.document_path()
            if open_path and os.path.normcase(os.path.abspath(open_path)) == target:
                return i
        return -1

    # ------------------------------------------------------------------
    # Ticked tabs, for "Move Selected to New Window"
    # ------------------------------------------------------------------

    def checked_views(self) -> list:
        """The ticked tabs, in TAB ORDER rather than the order they were ticked.

        Tab order is the order they will land in the new window, and it is the
        order the eye reads them in, so it is the only one worth reporting.
        """
        live = self.views()
        return [v for v in live if any(v is c for c in self._checked)]

    def is_view_checked(self, view) -> bool:
        return any(v is view for v in self._checked)

    def set_view_checked(self, view, checked: bool):
        if view is None or self.index_of(view) < 0:
            return
        already = self.is_view_checked(view)
        if already == checked:
            return
        if checked:
            self._checked.append(view)
        else:
            self._checked = [v for v in self._checked if v is not view]
        self._sync_checked_marks()
        self.checked_views_changed.emit()

    def toggle_view_checked(self, view):
        self.set_view_checked(view, not self.is_view_checked(view))

    def clear_checked(self):
        if not self._checked:
            return
        self._checked = []
        self._sync_checked_marks()
        self.checked_views_changed.emit()

    def _forget_checked(self, view):
        """A view left this window. Quietly, without announcing a change the
        caller is in the middle of making anyway."""
        if self.is_view_checked(view):
            self._checked = [v for v in self._checked if v is not view]
            self._sync_checked_marks()

    def _sync_checked_marks(self):
        """Push the ticked VIEWS down to the bar as indices, which is what it
        paints with. Re-run after anything that renumbers the tabs."""
        self._checked = [v for v in self._checked if self.index_of(v) >= 0]
        self._bar.set_checked_indices(
            [self.index_of(v) for v in self._checked])

    def check_invariant(self):
        """The bar and the stack are index-parallel. Nothing else keeps them
        that way, so the tests assert it after every operation."""
        assert self._bar.count() == self._stack.count(), (
            f"tab bar has {self._bar.count()} tabs, "
            f"stack has {self._stack.count()} pages")

    # ------------------------------------------------------------------
    # Adding and removing
    # ------------------------------------------------------------------

    #: The per-view wiring this area owns, for the whole life of the view
    #: rather than only while it is in front: the tab label and its unsaved dot
    #: have to keep up with a document nobody is looking at.
    #:
    #: Every one of them is a BOUND METHOD, not a lambda, and that is
    #: load-bearing for phase 3. A view being moved is connected to its
    #: destination area BEFORE it is disconnected from its source, so the
    #: source has to be able to drop exactly its own connections and leave the
    #: destination's alone. `signal.disconnect(bound_method)` does that;
    #: `signal.disconnect()` with no argument would cut both.
    def _view_wiring(self, view) -> tuple:
        return (
            (view.title_changed, self._refresh_titles),
            (view.dirty_changed, self._refresh_dirty),
            (view.close_requested, self._on_view_closed),
            (view.activation_requested, self._on_activation_requested),
        )

    def _connect_view(self, view):
        for signal, slot in self._view_wiring(view):
            signal.connect(slot)

    def _disconnect_view(self, view):
        for signal, slot in self._view_wiring(view):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _on_view_closed(self):
        """A view announced that it has been asked to go.

        `sender()` rather than a captured view, so the slot is one object this
        area can disconnect by reference. See `_view_wiring`.
        """
        view = self.sender()
        if view is not None:
            self.view_close_requested.emit(view)

    def _on_activation_requested(self):
        """A view is about to be changed and wants to be watched while it is.

        Phase 5: one undo stack per window, so Ctrl+Z can reach a document in
        another tab. The command asks for its tab first, and this is what
        brings it up. `sender()` rather than a captured view, for the same
        reason as `_on_view_closed`: one object this area can disconnect by
        reference. Ignored while an MRU walk is in flight, which is the one
        time the front tab is deliberately not the one the user is settling on.
        """
        view = self.sender()
        if view is None or view is self._current_view:
            return
        index = self.index_of(view)
        if index >= 0 and not self.is_walking_mru():
            self.set_current_index(index)

    def add_view(self, view, activate: bool = True) -> int:
        """Take ownership of a DocumentView and give it a tab.

        The stack takes the widget FIRST. Adding the first tab makes it current
        immediately, and the handler for that reaches into the stack for the
        widget it is switching to.
        """
        return self._insert_view(view, self._stack.count(), activate)

    def adopt(self, view, at: int = -1) -> int:
        """Take a LIVE view out of another window and give it a tab here.

        The move that "Move to New Window" is made of, and the one phase 4's
        tear-off will drive. Two things about it are not negotiable:

        NEVER `setParent(None)`. `insertWidget` reparents the view straight
        from the source stack into this one. On Windows, promoting a widget to
        a top-level creates a real HWND and reparenting it back destroys that
        HWND along with the widget's native resources; phase 1's measurement
        (the scene, the undo stack and the viewport all surviving, with
        `internalWinId()` never leaving 0) was made with exactly this order and
        does not hold without it.

        DESTINATION FIRST. This runs while the view is still in the source's
        tab bar, and the source only tidies up afterwards (see `detach`). The
        other order closes the source window while it is still the view's
        parent, and the view dies with it.
        """
        index = self._stack.count() if at < 0 else max(0, min(at, self._stack.count()))
        return self._insert_view(view, index, activate=True)

    def _insert_view(self, view, index: int, activate: bool) -> int:
        self._stack.insertWidget(index, view)
        self._connect_view(view)
        self._bar.insertTab(index, "")
        self._bar.install_close_button(index)
        self._refresh_titles()
        if activate:
            self.set_current_index(index)
        self._sync_header_visibility()
        self.check_invariant()
        return index

    def detach(self, view, index: int | None = None):
        """Give up a view's tab WITHOUT tearing the view down.

        The source half of a move. `remove_view` is the other thing that takes
        a tab away and it destroys what was in it; this one hands the view over
        alive, so it does no `teardown()`, no `deleteLater()` and above all no
        `setParent(None)`.

        `index` is passed in by a caller that already adopted the view
        somewhere else, because by then the widget has been reparented and this
        stack no longer knows where its tab was. Qt removes a reparented widget
        from the old layout on its own, so the stack may already be one short
        of the bar; that is the one moment the invariant is allowed to be
        false, and it is re-asserted at the end.
        """
        if index is None:
            index = self.index_of(view)
        if not (0 <= index < self._bar.count()):
            return None
        if view is self._current_view:
            # Same reason as remove_view: announce the departure while the
            # connections still exist, so the window unbinds cleanly.
            self._current_view = None
            self.current_view_changed.emit(view, None)
        self._syncing = True
        try:
            self._bar.removeTab(index)
            if self._stack.indexOf(view) >= 0:
                self._stack.removeWidget(view)
        finally:
            self._syncing = False
        self._disconnect_view(view)
        self._forget_visit(view)
        self._forget_checked(view)
        self._refresh_titles()
        self._sync_header_visibility()
        self._resync_current()
        self.check_invariant()
        return view

    def remove_view(self, index: int):
        """Drop a tab and the view under it, and release what the view held.

        No prompting here: whether the document may go is the window's
        decision and has already been made by the time this runs.
        """
        view = self.view_at(index)
        if view is None:
            return
        if view is self._current_view:
            # Announce the departure while this view's connections still exist,
            # so the window can unbind its chrome from it cleanly. Do it after
            # the teardown below and the disconnect fails with a Qt warning on
            # stderr, because the signals have already been cut.
            self._current_view = None
            self.current_view_changed.emit(view, None)
        self._syncing = True
        try:
            self._bar.removeTab(index)
            self._stack.removeWidget(view)
        finally:
            self._syncing = False
        self._disconnect_view(view)
        self._forget_visit(view)
        self._forget_checked(view)
        self._retire(view)
        self._refresh_titles()
        self._sync_header_visibility()
        self._resync_current()
        self.check_invariant()

    def _retire(self, view):
        """Close a removed view's document now, destroy the widget later.

        THE `setParent(None)` THAT USED TO BE HERE IS GONE, and it is the same
        0xC000041D as `MainWindow.move_view_to_window`. On Windows,
        `setParent(None)` promotes a widget to a top-level with a real HWND,
        and the `deleteLater` behind it destroys that native window again. This
        method runs inside a mouse-move dispatch on the ordinary tear-off path:
        dropping a tab into a window whose only tab is an empty placeholder
        retires the placeholder through `remove_view` (see
        `MainWindow.adopt`), with the mouse still captured and the window
        procedure still on the stack. Creating and destroying a native window
        there is what the OS kills the process for.

        `remove_view` has already taken the widget out of the bar and the
        stack, so nothing is displaying it and the parent it keeps until
        deletion costs nothing. `teardown()` stays synchronous, because that is
        plain Python releasing the fitz document and the callers that close a
        tab and immediately reopen the file depend on the handle being gone.
        """
        view.teardown()
        view.deleteLater()

    def set_current_index(self, index: int):
        if 0 <= index < self._bar.count():
            self._bar.setCurrentIndex(index)
            # An index that is already current emits nothing, so the first tab
            # (added at index 0, already current) needs the sync run by hand.
            if self._current_view is not self.view_at(index):
                self._on_current_changed(index)

    def step_current(self, delta: int):
        """Positional next/previous, wrapping. Ctrl+PgDn and Ctrl+PgUp.

        Positional on purpose, and it stayed that way when phase 4 added the
        MRU walk below. They are two different orders and conflating them is
        the mistake: this one is "the tab to the right", `step_mru` is "the tab
        I was just in", and a user reaches for each of them for its own reason.
        """
        count = self._bar.count()
        if count < 2:
            return
        self.set_current_index((self._bar.currentIndex() + delta) % count)

    # ------------------------------------------------------------------
    # Most recently used, for Ctrl+Tab
    # ------------------------------------------------------------------

    def _note_visit(self, view):
        """This tab became the front one. Ignored while a walk is in flight.

        The freeze is the whole design. Recording every step of a walk would
        put the tab just landed on at the top of the list, so the next tap
        would come straight back, and holding Ctrl would flip between two tabs
        forever instead of walking back through the stack.
        """
        if view is None or self._mru_walk is not None:
            return
        self._mru = [v for v in self._mru if v is not view]
        self._mru.insert(0, view)

    def _forget_visit(self, view):
        self._mru = [v for v in self._mru if v is not view]
        if self._mru_walk is not None:
            self._mru_walk = [v for v in self._mru_walk if v is not view]

    def mru_order(self) -> list:
        """Every open view, most recently looked at first.

        Anything never visited (a background tab opened alongside others) comes
        last, in tab order, so the list always names every tab exactly once
        however the history got there.
        """
        live = self.views()
        ordered = [v for v in self._mru if any(v is w for w in live)]
        ordered += [v for v in live if not any(v is o for o in ordered)]
        return ordered

    def is_walking_mru(self) -> bool:
        return self._mru_walk is not None

    def step_mru(self, delta: int) -> bool:
        """One tap of Ctrl+Tab. True if a walk is now in flight.

        The caller is what notices Ctrl coming up: see
        MainWindow._start_mru_walk, which arms an application event filter on a
        True and disarms it on the release.
        """
        if self.count() < 2:
            return False
        if self._mru_walk is None:
            self._mru_walk = self.mru_order()
            self._mru_pos = 0
        order = self._mru_walk
        if len(order) < 2:
            self._mru_walk = None
            return False
        self._mru_pos = (self._mru_pos + delta) % len(order)
        index = self.index_of(order[self._mru_pos])
        if index >= 0:
            self.set_current_index(index)
        return True

    def end_mru_walk(self):
        """Ctrl came up. Whatever tab it landed on is now the most recent."""
        if self._mru_walk is None:
            return
        self._mru_walk = None
        self._mru_pos = 0
        self._note_visit(self._current_view)

    # ------------------------------------------------------------------
    # Staying in step
    # ------------------------------------------------------------------

    def _on_current_changed(self, index: int):
        if self._syncing:
            return
        self._stack.setCurrentIndex(index)
        self._set_current_view(self.view_at(index))

    def _resync_current(self):
        """Put the stack and the remembered front view back on the bar's tab.

        Run after a removal: the bar emits currentChanged from inside
        removeTab, while the stack still holds the page being dropped, so the
        emit is suppressed and the truth is re-read here.
        """
        index = self._bar.currentIndex()
        self._stack.setCurrentIndex(index)
        self._set_current_view(self.view_at(index))

    def _set_current_view(self, new):
        """One place a different document becomes the front one.

        Two things happen here and the order matters. The view LEAVING is told
        first, because backgrounding is what releases its render cache and its
        markup clones, and doing that before the arriving view rebuilds its own
        keeps the peak at one document's worth rather than two. Phase 2
        measured the numbers that make this the headline: one document's
        six-entry pixmap cache is 207 MB, and ten live documents plus twenty
        markup clones are 2 MB between them.
        """
        if new is self._current_view:
            return False
        previous = self._current_view
        self._current_view = new
        if previous is not None:
            previous.set_active(False)
        if new is not None:
            new.set_active(True)
        # The one place a tab is looked at, so the one place the visit history
        # is written. Frozen mid-walk; see _note_visit.
        self._note_visit(new)
        self.current_view_changed.emit(previous, new)
        return True

    def _on_tab_moved(self, frm: int, to: int):
        """A tab was dragged along the bar; the stack follows it.

        `removeWidget` takes the page out of the stacked layout without
        reparenting it, so the view never becomes a top-level and never grows
        (then loses) a native window handle. That matters here and it is the
        same rule phase 4 has to keep: see "Never call setParent(None)" in the
        plan.
        """
        widget = self._stack.widget(frm)
        if widget is None:
            return
        self._syncing = True
        try:
            self._stack.removeWidget(widget)
            self._stack.insertWidget(to, widget)
        finally:
            self._syncing = False
        self._stack.setCurrentIndex(self._bar.currentIndex())
        self._refresh_titles()
        self.check_invariant()

    def _sync_header_visibility(self):
        """One empty tab is what "no document open" looks like, and a bar with
        a single blank tab on it is worse than no bar at all."""
        views = self.views()
        show = len(views) > 1 or (len(views) == 1 and not views[0].is_empty())
        self._header.setVisible(show)
        self._chevron.setVisible(len(views) > 1)

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    def _refresh_titles(self):
        """Recompute EVERY label, on every add and every remove.

        Disambiguation is a property of the whole set, not of one tab: opening
        a second `drawings/plan.pdf` has to lengthen the first one's label too,
        and closing it has to shorten it back.
        """
        views = self.views()
        titles = tab_titles([v.document_path() for v in views])
        for i, (view, title) in enumerate(zip(views, titles)):
            self._bar.setTabText(i, title)
            path = view.document_path()
            self._bar.setTabToolTip(i, path or title)
        self._refresh_dirty()
        self._sync_checked_marks()
        self._sync_header_visibility()

    def _refresh_dirty(self, _=None):
        for i, view in enumerate(self.views()):
            self._bar.set_tab_dirty(i, view.has_document() and view.is_dirty())

    def apply_palette(self, palette):
        self._bar.apply_palette(palette)
        self._chevron.apply_palette(palette)

    # ------------------------------------------------------------------
    # Menus
    # ------------------------------------------------------------------

    def _show_all_tabs_menu(self):
        menu = QMenu(self)
        current = self._bar.currentIndex()
        for i in range(self._bar.count()):
            action = QAction(self._bar.tabText(i), menu)
            action.setCheckable(True)
            action.setChecked(i == current)
            action.setToolTip(self._bar.tabToolTip(i))
            action.triggered.connect(lambda _=False, k=i: self.set_current_index(k))
            menu.addAction(action)
        menu.exec(self._chevron.mapToGlobal(
            QPoint(0, self._chevron.height())))

    def _on_tab_menu(self, index: int, position: QPoint):
        view = self.view_at(index)
        if view is None:
            return
        menu = self.build_tab_menu(index)
        menu.exec(position)

    def build_tab_menu(self, index: int) -> QMenu:
        """The per-tab context menu.

        "Move to New Window" is here now, and it is the whole user-facing half
        of phase 3. No pinning; a pinned tab is a promise about ordering that
        the drag reordering above already breaks.
        """
        view = self.view_at(index)
        menu = QMenu(self)
        path = view.document_path() if view is not None else None

        close = menu.addAction("Close")
        close.triggered.connect(lambda: self.tab_close_requested.emit(index))

        others = menu.addAction("Close Others")
        others.setEnabled(self.count() > 1)
        others.triggered.connect(lambda: self.close_others(index))

        right = menu.addAction("Close to the Right")
        right.setEnabled(index < self.count() - 1)
        right.triggered.connect(lambda: self.close_to_the_right(index))

        menu.addSeparator()

        # Disabled on a lone tab: moving the only document out of a window and
        # closing the window behind it produces the window you started with,
        # having thrown away its position and size on the way. Every browser
        # greys it out for the same reason. File > New Window is what to reach
        # for when an empty second window is actually what is wanted.
        move_out = menu.addAction("Move to New Window")
        move_out.setEnabled(self.count() > 1)
        move_out.triggered.connect(
            lambda: self.move_to_new_window_requested.emit(view))

        # THE CHEAP HALF OF MULTI-TAB MOVE. Tick as many tabs as you want, one
        # right-click each, then move the lot in one go. Shift-click range
        # select was the other candidate and was ruled out: it needs a real
        # selection model on a QTabBar, which does not have one and cannot be
        # given one without taking over its painting, its keyboard handling and
        # its drag reordering.
        #
        # Ticking lives on the same menu as the command that consumes it, so
        # there is nothing to discover: right-click a tab and both are there.
        select = menu.addAction("Select Tab")
        select.setCheckable(True)
        select.setChecked(self.is_view_checked(view))
        select.triggered.connect(lambda: self.toggle_view_checked(view))

        checked = self.checked_views()
        move_selected = menu.addAction(
            f"Move Selected to New Window ({len(checked)})" if checked
            else "Move Selected to New Window")
        # Disabled on "all of them" for the same reason the singular one is
        # disabled on a lone tab: moving every document out of a window and
        # closing the window behind it hands back the window you started with,
        # minus its size and position.
        move_selected.setEnabled(bool(checked) and len(checked) < self.count())
        move_selected.triggered.connect(
            lambda: self.move_selected_to_new_window_requested.emit(
                self.checked_views()))

        clear = menu.addAction("Clear Selection")
        clear.setEnabled(bool(checked))
        clear.triggered.connect(self.clear_checked)

        menu.addSeparator()

        duplicate = menu.addAction("Duplicate Tab")
        duplicate.setEnabled(bool(path))
        duplicate.triggered.connect(
            lambda: self.duplicate_requested.emit(view))

        menu.addSeparator()

        copy_path = menu.addAction("Copy Full Path")
        copy_path.setEnabled(bool(path))
        copy_path.triggered.connect(lambda: self._copy_path(path))

        reveal = menu.addAction("Open Containing Folder")
        reveal.setEnabled(bool(path))
        reveal.triggered.connect(lambda: self._reveal(path))
        return menu

    def close_others(self, index: int):
        """Close every tab but this one, right to left.

        Right to left because closing shifts every index above it, and the
        tab being kept is identified by its view rather than its number for
        the same reason: a cancelled save prompt leaves a tab in place and
        moves everything after it.
        """
        keep = self.view_at(index)
        for i in range(self.count() - 1, -1, -1):
            if self.view_at(i) is not keep:
                self.tab_close_requested.emit(i)

    def close_to_the_right(self, index: int):
        anchor = self.view_at(index)
        for i in range(self.count() - 1, -1, -1):
            if self.view_at(i) is anchor:
                break
            self.tab_close_requested.emit(i)

    def _copy_path(self, path: str):
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.clipboard().setText(path)

    def _reveal(self, path: str):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
