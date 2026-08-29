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

WHAT IS STILL DELIBERATELY NOT HERE. MRU Ctrl+Tab ordering: the next/previous
shortcuts are positional and this class holds no visit history. The tear-off
gesture is phase 4, and it will drive `detach`/`adopt` rather than replace
them.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QHBoxLayout, QMenu, QSizePolicy, QStackedWidget, QStyle,
    QTabBar, QToolButton, QVBoxLayout, QWidget,
)

# Tabs share the bar equally until they hit the ceiling, then shrink to the
# floor, and only once every tab is at the floor does the bar start scrolling.
# That order is the point: scrolling hides tabs, eliding only shortens them.
TAB_MIN_WIDTH = 92
TAB_MAX_WIDTH = 240

# Room kept at the right of the bar for the chevron, so the last tab is not
# sitting underneath it.
CHEVRON_WIDTH = 26

# The unsaved-changes dot, drawn in place of the close X.
DIRTY_DOT_RADIUS = 3.5


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


class _TabCloseButton(QAbstractButton):
    """The control at the right of a tab: an X, or a dot while it is unsaved.

    The dot is not decoration next to the X, it REPLACES it, and hovering turns
    it back into an X. That is the arrangement every editor settled on because
    the two things want the same few pixels and the one you need is decided by
    what you are about to do: reading the bar, you want to know which documents
    are unsaved; reaching for this button, you want the close control.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFixedSize(16, 16)
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
        return QSize(16, 16)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        if self.shows_dot():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._glyph)
            centre = rect.center()
            painter.drawEllipse(centre, DIRTY_DOT_RADIUS, DIRTY_DOT_RADIUS)
            return
        if self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._hover_bg)
            painter.drawRoundedRect(rect, 3, 3)
        pen = QPen(self._glyph)
        pen.setWidthF(1.3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        inset = QRect(rect).adjusted(5, 5, -5, -5)
        painter.drawLine(inset.topLeft(), inset.bottomRight())
        painter.drawLine(inset.topRight(), inset.bottomLeft())


class DocumentTabBar(QTabBar):
    """The bar itself. Widths, the dirty dot, and the two gestures Qt does not
    give us: double-click on empty space, and a context menu per tab."""

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

    # -- geometry ------------------------------------------------------

    def _button_side(self) -> QTabBar.ButtonPosition:
        """Which end of the tab the style puts the close control on."""
        side = self.style().styleHint(
            QStyle.StyleHint.SH_TabBar_CloseButtonPosition, None, self)
        return QTabBar.ButtonPosition(side)

    def tabSizeHint(self, index: int) -> QSize:
        """Equal shares, capped and floored.

        Qt's own hint is the width of the text, which makes a long filename take
        half the bar. Sharing the room out instead is what makes tabs shrink
        before the bar starts scrolling, which is the behaviour asked for.
        """
        hint = super().tabSizeHint(index)
        count = max(1, self.count())
        available = max(0, self.width() - CHEVRON_WIDTH)
        share = available // count
        hint.setWidth(max(TAB_MIN_WIDTH, min(TAB_MAX_WIDTH, share)))
        return hint

    def resizeEvent(self, event):
        # The share above is a function of the bar's width, so a resize has to
        # re-ask for it.
        super().resizeEvent(event)
        self.updateGeometry()

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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_view = None
        self._syncing = False       # a bar/stack edit is mid-flight

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("documentTabHeader")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)

        self._bar = DocumentTabBar()
        self._bar.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Fixed)
        self._bar.currentChanged.connect(self._on_current_changed)
        self._bar.tabMoved.connect(self._on_tab_moved)
        self._bar.tabCloseRequested.connect(self.tab_close_requested)
        self._bar.new_tab_requested.connect(self.new_tab_requested)
        self._bar.tab_menu_requested.connect(self._on_tab_menu)
        header_row.addWidget(self._bar, stretch=1)

        # Every tab in one list, for when there are more than the bar can show
        # at a readable width. It is the answer to "which of these is scrolled
        # off", which scroll buttons alone never give you.
        self._chevron = QToolButton()
        self._chevron.setObjectName("documentTabChevron")
        self._chevron.setText("⌄")
        self._chevron.setToolTip("All open documents")
        self._chevron.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chevron.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._chevron.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._chevron.setFixedWidth(CHEVRON_WIDTH)
        self._chevron.clicked.connect(self._show_all_tabs_menu)
        header_row.addWidget(self._chevron)

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
        view.teardown()
        view.setParent(None)
        view.deleteLater()
        self._refresh_titles()
        self._sync_header_visibility()
        self._resync_current()
        self.check_invariant()

    def set_current_index(self, index: int):
        if 0 <= index < self._bar.count():
            self._bar.setCurrentIndex(index)
            # An index that is already current emits nothing, so the first tab
            # (added at index 0, already current) needs the sync run by hand.
            if self._current_view is not self.view_at(index):
                self._on_current_changed(index)

    def step_current(self, delta: int):
        """Positional next/previous, wrapping. Ctrl+PgDn and Ctrl+PgUp.

        Positional on purpose: the most-recently-used ordering behind Ctrl+Tab
        is phase 3, and it needs a visit history this class does not keep.
        """
        count = self._bar.count()
        if count < 2:
            return
        self.set_current_index((self._bar.currentIndex() + delta) % count)

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
        show = len(views) > 1 or (len(views) == 1 and views[0].has_document())
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
        self._sync_header_visibility()

    def _refresh_dirty(self, _=None):
        for i, view in enumerate(self.views()):
            self._bar.set_tab_dirty(i, view.has_document() and view.is_dirty())

    def apply_palette(self, palette):
        self._bar.apply_palette(palette)

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
