"""The application shell: menus, status bar, theme, updates, window lifetime.

It holds a DocumentArea (ui/document_area.py), which is a tab bar over a stack
of DocumentViews. Phase 2 of docs/tabs-plan.md. `MainWindow.view` is the FRONT
document, which is the only thing that changed for everything reaching through
it: phase 1 wrote every one of those call sites so this would be the only edit.

WHAT LIVES UP HERE, AND WHY. Chrome, and only chrome. The menu bar, the status
bar, the update strip, Preferences, the theme, `_force_quit` and `closeEvent`.
Three of those controls act on a document and still belong here:

  - the status bar's PAGE BOX. It is drawn in the status bar, so it is the
    frame's. It follows the front view (see `_on_view_page_changed`) and drives
    it (`_on_page_jump`), which is a rebind rather than a second copy.
  - the status bar's FIT GROUP, and `choose_fit_mode` / `fit_mode_chosen` with
    it. Same reason, plus one more: `view.default_fit_mode` is a single
    app-wide setting and the Preferences dropdown is the other view of it, so
    one owner is the point. Only the applying goes down to the canvas.
  - the single-key TOOL SHORTCUTS (v/h/r/l/t). Two live DocumentViews would
    each own a window-context QShortcut for "v", which Qt reports as ambiguous
    and then routes to neither. They stay here and reach the front view only,
    through the `view` property.

The search bar went the other way and lives in the view: its hits are page
numbers in one particular document.

WHAT IS REBOUND ON A TAB SWITCH, which is where the bugs in this phase live.
`_on_front_view_changed` is the single place it happens:

  - the five chrome signals, disconnected from the tab leaving and connected to
    the one arriving (`_connect_view` / `_disconnect_view`). Leave them all
    connected and a background document writes the status bar.
  - NOT the Edit menu's UNDO and REDO any more. Phase 5 gave the WINDOW one
    undo stack that every tab shares, because a page dragged from one tab into
    another is one action with two document-level effects and no pair of stacks
    can undo that without leaving a duplicate. The actions are built once
    (`_rebuild_undo_actions`) and never rebound. See ui/undo.py.
  - the status bar's page box, and the window title, both re-read off the
    arriving view (`DocumentView.refresh_chrome`).
  - the fit group, which follows the arriving CANVAS rather than the remembered
    setting, because a manual zoom breaks the fit on one canvas only
    (`_sync_fit_group`).

PHASE 3: THERE ARE SEVERAL OF THESE NOW. A window joins the WindowRegistry on
the way up and leaves it in `closeEvent`, and that registry owns three things
this class used to assume it was alone in deciding:

  - APP LIFETIME. `main.py` turns Qt's own quit-on-last-window off and the
    registry quits when its count reaches zero, so a torn-off window closing
    while its parent lives, and the parent closing while the child lives, are
    the same code path.
  - WHERE A FILE LANDS. `handle_cli_files` is still the "this window" verb, but
    the shell now goes through `WindowRegistry.route_open`, which can raise a
    tab in a different window entirely.
  - MOVING A DOCUMENT BETWEEN WINDOWS. `adopt` and `move_view_to_window` are
    the mechanism; the tab menu's "Move to New Window" and File > New Window
    are the only two things driving it. No gesture: phase 4.

PHASE 7: THE WINDOW HAS NO SYSTEM TITLE BAR. The document tabs are the top
row of the window now, which means the top row of the window is a widget and the
system title bar had to go. What is left up here is the same chrome as before in
a different order:

  - `_title_bar` (ui/title_bar.py) is the top row: the app icon, the tab strip
    HOSTED from the DocumentArea, the new-tab button, and the three window
    controls. It is not a second owner of the tabs; `DocumentArea.header()`
    hands the same widget over and every gesture on it is still that class's.
  - the MENU BAR sits directly under it, on its own row, which is where Explorer
    and Edge keep theirs. A hamburger in the title bar was the other candidate
    and it loses Alt+F, loses the screen reader's menu, turns five one-click
    menus into two clicks each, and spends title bar width on a button in order
    to save a row that costs 22 pixels. The complaint this phase answers was
    that the tabs were BELOW the menu; they are above it now, and that is the
    whole of what was asked for.
  - `_frameless` (ui/frameless.py) gives Windows back everything the system
    title bar used to do: dragging, double-click to maximise, Aero Snap, Snap
    Layouts, the system menu, and the eight resize edges.

Both of the first two are put in place with `setMenuWidget`, which is the one
slot QMainWindow keeps above the central widget. That is also why `menuBar()` is
overridden below: QMainWindow's own would build a second, empty one.

PER-WINDOW CHROME, WHICH IS THE THING THAT DOES NOT COME FREE. The QSS and the
QPalette are set on the QApplication, so those cover every window at once. Two
things do not, because they are properties of one native window: the Mica
backdrop (`apply_mica` works on the HWND, and a new top-level gets a fresh one,
so it has to be reapplied per window and AFTER show()), and the code-drawn
surfaces, which is why each window connects `theme_changed` for itself. The
update strip is the third case and it went the other way: it is one check per
APPLICATION, so only the first window runs it. See `_should_check_for_updates`.
"""

import os
import uuid

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QMenuBar, QMessageBox, QStatusBar, QApplication,
    QToolButton, QButtonGroup,
)
from PySide6.QtCore import QEvent, QPoint, QTimer, Qt, QSize, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut, QIcon

# How long after the window is up before the update check goes out. Nothing
# about it blocks startup (it runs on its own thread), but the first render
# and the first PDF load should have the machine to themselves; an update is
# never urgent enough to compete with them.
UPDATE_CHECK_DELAY_MS = 1500

# How far a new window is offset from the one that spawned it. Enough that the
# title bar and the tab bar of the window underneath stay clickable, which is
# what makes it read as a second window rather than a redraw of the first.
NEW_WINDOW_OFFSET = 32

# The status bar's view-mode group: (mode, qtawesome id, tooltip, text fallback).
# The mode names match ui.canvas.FIT_MODES and core.settings' default_fit_mode.
_FIT_CONTROLS = [
    ("fit_page",   "mdi6.fit-to-page-outline",     "Fit page",           "Pg"),
    ("fit_width",  "mdi6.arrow-expand-horizontal", "Fit width",          "W"),
    ("fit_height", "mdi6.arrow-expand-vertical",   "Fit height",         "H"),
    ("actual",     "mdi6.percent-outline",         "100% (actual size)", "100"),
]

from core.settings import settings
from core.resources import app_icon_path
from core.version import APP_VERSION
from ui.update_notice import UpdateNotice
from ui.canvas import FIT_MODES
from ui.document_area import DocumentArea
from ui.document_view import DocumentView
from ui.frameless import FramelessHelper
from ui.title_bar import TitleBar
from ui.preferences_dialog import PreferencesDialog
from ui.page_jump import PageJump
from ui.reopen_stack import capture_view, reopen_stack
from ui.session import recorder
from ui.theme import ThemeManager, apply_mica, themed_icon, qtawesome_available, LIGHT
from ui.undo import WindowUndoStack
from ui.window_registry import WindowRegistry, same_file


class MainWindow(QMainWindow):
    #: The page-fit mode was chosen, by whichever surface chose it. The status
    #: bar's icon group and the Preferences dropdown are two views of this one
    #: value, so both go through choose_fit_mode() and both listen here.
    fit_mode_chosen = Signal(str)

    def __init__(self, theme: ThemeManager | None = None):
        super().__init__()
        self._prefs_dialog = None  # the one Preferences window, while it is open
        self._connected_view = None  # the view currently driving this chrome
        # ONE UNDO STACK FOR THE WHOLE WINDOW, shared by every tab in it.
        # Phase 5 of docs/tabs-plan.md: a page dragged from one tab into
        # another is one action with two document-level effects, and split
        # across two stacks there is no ordering that undoes it without leaving
        # a duplicate. Every DocumentView that lands here is handed this stack;
        # see ui/undo.py for the dirty-state rule that goes with it.
        self._undo_stack = WindowUndoStack(self)
        # A stable name for this window, carried in a page drag's payload. The
        # binding check at drop time is still a LIVE comparison of the two
        # views' windows, because a tear-off can move a document to another
        # window while the drag is in flight; this is the record of where the
        # drag started. See ui/page_drag.py.
        self._window_id = uuid.uuid4().hex
        # Theme: use the passed-in manager, or stand one up (e.g. tests/smoke).
        self._theme = theme or ThemeManager(QApplication.instance())
        # Every open document in this window, one tab each. Empty for now: the
        # first view is added at the end of this method, once the chrome it
        # drives (status bar, page box, Edit menu) exists to be driven.
        self._area = DocumentArea()
        self._area.current_view_changed.connect(self._on_front_view_changed)
        self._area.view_close_requested.connect(self._on_view_close_requested)
        self._area.new_tab_requested.connect(self.new_tab)
        self._area.duplicate_requested.connect(self._duplicate_tab)
        self._area.tab_close_requested.connect(self.close_tab)
        self._area.move_to_new_window_requested.connect(self.move_view_to_new_window)
        self._area.move_selected_to_new_window_requested.connect(
            self.move_views_to_new_window)
        self._force_quit = False # Quit menu wants a real app quit, not "close PDF"
        self._session_ending = False  # Windows is ending the session; never block it
        self._mica_applied = False  # the backdrop needs an HWND, so it waits for show()
        self._screen_watched = False  # so does the QWindow behind screenChanged
        self._mru_filter_on = False   # a Ctrl+Tab walk is waiting on Ctrl coming up
        # Joined BEFORE anything that asks the registry a question. In
        # particular _should_check_for_updates counts the windows already in it,
        # so a window that has not joined would think it was the first.
        self._registry = WindowRegistry.instance()
        self.setAcceptDrops(True)   # PDFs dragged from Explorer: see dropEvent
        self.setMinimumSize(1100, 720)
        icon_path = app_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))
        # No system title bar. Built before the UI so the title bar it drives
        # can be handed to it as soon as that exists, and applied after, because
        # applying it reaches for the native handle.
        self._frameless = FramelessHelper(self)
        self._setup_ui()
        self._frameless.set_title_bar(self._title_bar)
        self._frameless.apply()
        # The Qt-side resize fallback for the platforms with no WM_NCHITTEST.
        # Inert on Windows, where the native path has already answered.
        self.setMouseTracking(True)
        self.installEventFilter(self._frameless)
        self._setup_menu()
        # The first tab. Last, because adding it makes it the front view, and
        # everything that fires on that reaches for chrome built above.
        self._area.add_view(self._new_view())
        self._setup_shortcuts()
        # Code-drawn surfaces (the canvas page backdrop) follow the theme too.
        self._apply_theme_surfaces(self._theme.palette)
        self._theme.theme_changed.connect(self._apply_theme_surfaces)
        # Optional Win11 Mica backdrop (silent no-op elsewhere). Repeated in
        # showEvent, which is the call that actually takes on Windows: this one
        # runs before the window has an HWND to apply anything to.
        apply_mica(self, self._theme.is_dark)
        self._connect_session_manager()
        # This window joins the app. Last, so a registry listener that reaches
        # back into it finds it finished.
        self._registry.register(self)
        if self._should_check_for_updates():
            # Ask GitHub whether there is a newer release. Off the GUI thread,
            # and after the window is up: see UPDATE_CHECK_DELAY_MS and
            # ui/update_notice.py. Offline this does nothing at all, silently.
            QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self._update_notice.start_check)

    def _should_check_for_updates(self) -> bool:
        """Only the first window checks. One update, one strip, one prompt.

        Every window builds its own UpdateNotice because the strip is drawn in
        the window, but the CHECK is about the application: three windows would
        make three GitHub requests, show three "update available" strips for
        the same release, and race each other to stage the same download. The
        one that opened first owns it.
        """
        return self._registry.count() <= 1

    @property
    def view(self) -> DocumentView:
        """The FRONT document of however many this window is holding.

        Phase 1 wrote every call site through this property so that phase 2
        only had to change what it returns. None only during construction,
        before the first tab is added.
        """
        return self._area.current_view()

    def document_area(self) -> DocumentArea:
        """The tab bar and the views under it. For tests and Preferences."""
        return self._area

    def _new_view(self) -> DocumentView:
        """Build a document view and wire what it needs for its whole life.

        The chrome signals are NOT wired here: those belong to whichever view
        is in front, and `_connect_view` binds them on the switch. What is
        wired here is the two the view raises no matter where it sits, both of
        which are decisions about tabs and therefore the window's.
        """
        view = DocumentView(self._theme)
        view.set_undo_stack(self._undo_stack)   # the window's, shared by every tab
        view.apply_palette(self._theme.palette)
        if hasattr(self, "_panel_action"):
            view.set_page_panel_visible(self._panel_action.isChecked())
        view.paths_requested.connect(self.open_paths)
        view.combine_requested.connect(self.combine_paths)
        return view

    def _apply_theme_surfaces(self, palette):
        """Re-tint the bits QSS can't reach: the canvas backdrop and its selection
        chrome, the thumbnail delegates, the toggle action's icon/label, and the
        Mica header on a switch."""
        for view in self._area.views():
            view.apply_palette(palette)
        self._area.apply_palette(palette)
        self._update_notice.apply_palette(palette)
        self._title_bar.apply_palette(palette)
        self._retint_fit_icons(palette)
        if hasattr(self, "_theme_action"):
            dark = self._theme.is_dark
            self._theme_action.setText("Light Mode" if dark else "Dark Mode")
            self._theme_action.setIcon(
                themed_icon("mdi6.weather-sunny" if dark else "mdi6.weather-night",
                            palette.text))
        apply_mica(self, self._theme.is_dark)

    # ------------------------------------------------------------------
    # Being one window of several
    # ------------------------------------------------------------------

    def showEvent(self, event):
        """Apply the Mica backdrop once the window actually exists.

        `apply_mica` reaches through to the HWND, and a top-level widget has no
        HWND until it is shown. The call in `__init__` is therefore a no-op on
        the first window and, more to the point, on every window opened after
        it: a new top-level gets a fresh handle and inherits nothing from the
        window it was torn off. So it is applied here, on the first show, which
        is the earliest moment there is anything to apply it to.
        """
        super().showEvent(event)
        if not self._mica_applied:
            self._mica_applied = True
            apply_mica(self, self._theme.is_dark)
        self._watch_screen_changes()

    def _watch_screen_changes(self):
        """Notice when this window is dragged onto a monitor at another scale.

        Same reason as the Mica line above: there is no `QWindow` to listen to
        until the widget has been shown. Phase 4 needs it because the tear-off
        can put a document on a different monitor in one gesture, and a cached
        page pixmap is DEVICE-dependent: rendered for a 1.0 screen and shown on
        a 1.5 one it is soft, and the panel thumbnails with it.
        """
        if self._screen_watched:
            return
        handle = self.windowHandle()
        if handle is None:
            return
        self._screen_watched = True
        handle.screenChanged.connect(self._on_screen_changed)

    def _on_screen_changed(self, _screen=None):
        """Every open document in this window re-renders. Nothing about the
        documents changed; their cached pixmaps are for the wrong device."""
        for view in self._area.views():
            view.rerender_for_screen_change()

    def changeEvent(self, event):
        """Tell the registry when this window comes to the front.

        The only thing that keeps `WindowRegistry.active_window` honest, and
        therefore the only thing that makes a file opened from Explorer land in
        the window being read rather than the oldest one.
        """
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_maximise_state()
            return
        if event.type() != QEvent.Type.ActivationChange:
            return
        if self.isActiveWindow():
            self._registry.note_activated(self)
        elif self._mru_filter_on:
            # Alt+Tab away with Ctrl still down and the release never arrives
            # here. Losing activation ends the walk, or the filter would sit
            # installed until some unrelated Ctrl came up.
            self._end_mru_walk()

    def raise_and_focus(self):
        """Un-minimize, raise and focus. What an Explorer launch or a drop
        deserves, and the one thing a bare relaunch does on its own."""
        self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized)
                            | Qt.WindowState.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()
        self._registry.note_activated(self)

    def activate_view(self, view) -> bool:
        """Bring one of this window's tabs to the front, and the window with it.

        The routing rule "a file already open activates that tab" ends here,
        which is why raising the window is part of it: the tab being current in
        a window behind three others is not what anybody meant by activating it.
        """
        index = self._area.index_of(view)
        if index < 0:
            return False
        self._area.set_current_index(index)
        self.raise_and_focus()
        return True

    # ------------------------------------------------------------------
    # Moving a document between windows
    # ------------------------------------------------------------------

    def adopt(self, view, at: int = -1) -> int:
        """Take a live DocumentView from another window into this one.

        Rewires the two signals `_new_view` binds for the life of a view, since
        both of them are decisions about TABS and the tabs are now this
        window's. The chrome signals are not touched here: those follow
        whichever view is in front, and `DocumentArea.adopt` makes this one
        current, which fires `_on_front_view_changed` and binds them.

        The view is reparented by `insertWidget` alone. Never `setParent(None)`
        anywhere on this path: see DocumentArea.adopt.
        """
        view.paths_requested.connect(self.open_paths)
        view.combine_requested.connect(self.combine_paths)
        # The arriving document joins THIS window's history. Its old window
        # already dropped whatever it held for it (see release_view), because a
        # command left behind there would replay against a document that is no
        # longer in that window.
        view.set_undo_stack(self._undo_stack)
        view.set_page_panel_visible(self._panel_action.isChecked())
        view.apply_palette(self._theme.palette)
        # A brand new window comes up holding one empty tab, and a document
        # arriving in it should REPLACE that placeholder rather than sit beside
        # it: a window with one document and one blank tab next to it is not
        # what "move this to a new window" means. Same rule `_target_view`
        # applies when a file is opened into an empty front tab, and it is
        # deliberately narrow: only a window whose ONLY tab is empty has a
        # placeholder to give up.
        placeholder = None
        if self._area.count() == 1 and self._area.view_at(0).is_empty():
            placeholder = self._area.view_at(0)
        index = self._area.adopt(view, at)
        if placeholder is not None:
            spare = self._area.index_of(placeholder)
            if spare >= 0:
                self._area.remove_view(spare)
                index = self._area.index_of(view)
        return index

    def release_view(self, view):
        """Cut this window's wiring to a view that is leaving, alive.

        Not `remove_view`, which destroys what it removes. Everything here is
        tolerant of a connection that was never made: a background tab has
        never had the chrome signals bound.
        """
        for signal, slot in ((view.paths_requested, self.open_paths),
                             (view.combine_requested, self.combine_paths)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        # This window's stack may hold commands that touch the departing
        # document, and there is no selective removal in QUndoStack. Dropping
        # the history is the honest answer, and `drop_history_for` only does it
        # when the view is actually named in there: a tab nothing has edited,
        # which is the ordinary tear-off, costs nothing.
        self._undo_stack.drop_history_for(view)
        self._disconnect_view(view)

    def move_view_to_window(self, view, target: "MainWindow", at: int = -1) -> bool:
        """Move one open document from this window into another one.

        `at` is where in the target's bar it lands, and -1 appends. The menu
        item never passes it; the phase 4 tear-off does, because a tab dropped
        between two others has to go between them.

        THE ORDER IS THE WHOLE THING, and it is the order phase 1's reparenting
        test was measured with:

        1. the destination window already exists and is shown, so the view has
           somewhere real to land;
        2. this window stops listening to the view;
        3. the destination ADOPTS it, which is the `insertWidget` that
           reparents it. Qt drops the view from this window's stacked layout on
           its own as a result, which is why the tab index is read before;
        4. only now does this window drop the tab that is left over;
        5. and only then, if nothing is left here, does this window close.

        Do 5 before 3 and the view is destroyed with its parent, which is the
        failure this sequence exists to avoid. There is no `setParent(None)`
        anywhere in it: on Windows that promotes the widget to a top-level with
        a real HWND, and reparenting it back destroys the HWND and the widget's
        native resources with it.

        STEP 5 IS DEFERRED, AND THAT IS THE 0xC000041D FIX. This method is
        called mid-drag, from `TabTearOff._attach_to_strip`, which runs inside
        `DocumentTabBar.mouseMoveEvent`, which Windows is running as a callback
        out of the window procedure with the mouse captured. `self.close()`
        there is a window destroying itself underneath the message dispatch it
        is still inside, and Windows answers by killing the process with
        STATUS_FATAL_USER_CALLBACK_EXCEPTION. Reproduced deterministically:
        drag a tab into a window whose only tab is an empty placeholder (the
        placeholder is retired on adopt, so that window is now holding exactly
        the dragged tab), then drag it on to a third window.

        `singleShot(0, ...)` puts the close on the next pass of the event loop
        instead, by which time the mouse-move callback has returned and the
        stack is the event loop's own. Nothing else in the sequence moves: the
        window is still empty, it still closes, and it closes before the user
        can see it empty.
        """
        if view is None or target is None or target is self:
            return False
        index = self._area.index_of(view)
        if index < 0:
            return False
        self.release_view(view)
        target.adopt(view, at)
        self._area.detach(view, index)
        if self._area.count() == 0:
            QTimer.singleShot(0, self.close)
        target.raise_and_focus()
        return True

    def move_view_to_new_window(self, view=None, geometry=None) -> "MainWindow | None":
        """Tab menu > Move to New Window. The user-facing half of phase 3.

        Offset from this window rather than placed exactly on top of it, so the
        document that just moved is visibly somewhere else.

        `geometry` overrides that placement, and is how phase 4's tear-off
        drops the new window straight under the cursor. It is an ARGUMENT and
        not a second method on purpose: create, size, position, show, and only
        then move the view across is the order the reparent depends on, and one
        copy of that order is the only safe number.
        """
        if view is None:
            view = self.view
        if view is None or self._area.index_of(view) < 0:
            return None
        target = self._registry.create_window(theme=self._theme, show=False)
        if geometry is not None:
            target.setGeometry(geometry)
        else:
            target.resize(self.size())
            target.move(self.pos() + QPoint(NEW_WINDOW_OFFSET, NEW_WINDOW_OFFSET))
        # Real, and on screen, BEFORE anything leaves. show() is also what
        # reapplies the Mica backdrop: a new top-level gets a fresh HWND and
        # inherits nothing from the window it was torn off. See showEvent.
        target.show()
        if not self.move_view_to_window(view, target):
            target.close()
            return None
        return target

    def move_views_to_new_window(self, views) -> "MainWindow | None":
        """Move SEVERAL open documents into one new window, in tab order.

        The consumer of the ticked tabs (see DocumentArea.checked_views). It is
        one entry point rather than a loop over `move_view_to_new_window`
        because that method makes a window per call: three ticked tabs would
        have made three windows, and looping over `move_view_to_window` alone
        would have raised and focused the destination once per document, which
        is three window activations and a visible flicker for one command.

        So the window is made once, up front and empty, and then the SINGULAR
        move runs per document into it. That is deliberate: `move_view_to_window`
        is the one place the reparent order lives (destination adopts, source
        detaches, source closes last) and a plural copy of that order is a
        second thing to keep right. Nothing about it flickers when the window it
        is moving into already exists.

        Tab order, not tick order: it is the order they are read in and the
        order they will be read in afterwards.
        """
        views = [v for v in self._area.views()
                 if any(v is w for w in (views or []))]
        if not views:
            return None
        target = self._registry.create_window(theme=self._theme, show=False)
        target.resize(self.size())
        target.move(self.pos() + QPoint(NEW_WINDOW_OFFSET, NEW_WINDOW_OFFSET))
        # Real, and on screen, before anything leaves. Same rule as
        # move_view_to_new_window: the reparent needs somewhere that exists.
        target.show()
        moved = 0
        for view in views:
            if self.move_view_to_window(view, target):
                moved += 1
        if not moved:
            target.close()
            return None
        self._area.clear_checked()
        target.raise_and_focus()
        return target

    def new_window(self) -> "MainWindow":
        """File > New Window (Ctrl+Shift+N). An empty second window."""
        window = self._registry.create_window(theme=self._theme, show=False)
        window.resize(self.size())
        window.move(self.pos() + QPoint(NEW_WINDOW_OFFSET, NEW_WINDOW_OFFSET))
        window.show()
        window.raise_and_focus()
        return window

    # ------------------------------------------------------------------
    # Files dropped on the window from the shell
    # ------------------------------------------------------------------

    @staticmethod
    def dropped_pdfs(mime) -> list:
        """The PDF paths in a drag, or [] if it is not carrying any.

        `hasUrls()` is what keeps this from colliding with the two internal
        page drags, and it is a stronger guarantee than it looks: a drag
        started inside the app carries item-model data and no urls at all, and
        `text/uri-list` only ever appears on a drag that came from the shell.
        So the three gestures in the plan's table stay separate without anybody
        having to check where the drag started.
        """
        if mime is None or not mime.hasUrls():
            return []
        paths = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            # normpath because QUrl hands back forward slashes even on Windows,
            # and every path in the app is compared against one that came from
            # a file dialog or a command line.
            path = os.path.normpath(url.toLocalFile())
            if os.path.splitext(path)[1].lower() == ".pdf" and os.path.isfile(path):
                paths.append(path)
        return paths

    def dragEnterEvent(self, event):
        if self.dropped_pdfs(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.dropped_pdfs(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """A PDF dropped from Explorer opens as a NEW TAB in this window.

        Never appended to the document on screen. That is the same rule
        `open_paths` enforces for every other way a file arrives, and it is the
        rule this gesture would break most easily: dropping a file onto a page
        looks like it should go into that page.
        """
        paths = self.dropped_pdfs(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.raise_and_focus()
        self.open_paths(sorted(paths))

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _setup_ui(self):
        self._build_chrome_rows()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Update strip, above everything, hidden until GitHub says there is a
        # newer release. Nothing in the layout moves while it stays hidden.
        self._update_notice = UpdateNotice()
        self._update_notice.staged_ready.connect(self._on_update_staged)
        root.addWidget(self._update_notice)

        # The documents. Its tab strip is no longer here: it has been hosted up
        # in the title bar, and what is left of the DocumentArea is the stack.
        root.addWidget(self._area)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Open a PDF to start  (Ctrl+O)")

        # Page box, immediately left of the fit group. Permanent widgets are laid
        # out left to right in the order they're added, so this one is added first.
        self._page_jump = PageJump()
        self._page_jump.page_requested.connect(self._on_page_jump)
        self._status.addPermanentWidget(self._page_jump)

        self._status.addPermanentWidget(self._build_fit_group())

    def _build_chrome_rows(self):
        """The two rows above the documents: the title bar, then the menu bar.

        `setMenuWidget` takes one widget, so both rows go into a column and the
        column goes in the slot. That slot is the only one QMainWindow keeps
        above the central widget, which is why the title bar is not simply the
        first thing in the central layout: the update strip and the documents
        live there, and the title bar has to be above the update strip too.
        """
        self._title_bar = TitleBar()
        self._title_bar.set_app_icon(self.windowIcon())
        # HOSTED, not owned. See DocumentArea.header().
        self._title_bar.host_tabs(self._area.header())
        self._title_bar.new_tab_requested.connect(self.new_tab)
        self._title_bar.system_menu_requested.connect(self.show_system_menu)
        controls = self._title_bar.controls()
        controls.minimise_requested.connect(self.showMinimized)
        controls.maximise_requested.connect(self.toggle_maximised)
        controls.close_requested.connect(self.close)

        # Built by hand rather than through `QMainWindow.menuBar()`, which would
        # put it in the slot this column is about to take. `menuBar()` below
        # returns this one, so every caller and every test still asks the same
        # question of the same object.
        self._menu_bar = QMenuBar()
        self._menu_bar.setObjectName("windowMenuBar")

        chrome = QWidget()
        chrome.setObjectName("windowChrome")
        column = QVBoxLayout(chrome)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._title_bar)
        column.addWidget(self._menu_bar)
        self.setMenuWidget(chrome)

    def menuBar(self) -> QMenuBar:
        """This window's menu bar, which QMainWindow did not put there.

        Shadowing the base method rather than leaving it alone is the whole
        point: `QMainWindow.menuBar()` builds and installs a menu bar the first
        time it is asked for one, and installing it would evict the column of
        chrome that holds the title bar. Six call sites and four tests ask this
        question; they should all keep getting the real answer.
        """
        return self._menu_bar

    def title_bar(self) -> TitleBar:
        """The top row. For the tests, and for the frameless helper."""
        return self._title_bar

    def frameless_helper(self) -> FramelessHelper:
        """The thing that gives Windows its window back. See ui/frameless.py."""
        return self._frameless

    # ------------------------------------------------------------------
    # Being a window without a system title bar
    # ------------------------------------------------------------------

    def nativeEvent(self, event_type, message):
        """Windows asks what every pixel of this window is; the helper answers.

        None from the helper means "not one of ours", and the base class gets it
        untouched. Never reached off Windows, and never under the offscreen
        platform the tests run on, which is why everything the helper decides is
        also reachable as a plain function (see ui/frameless.py).
        """
        handled = self._frameless.native_event(event_type, message)
        if handled is not None:
            return handled
        return super().nativeEvent(event_type, message)

    def toggle_maximised(self):
        """The maximise/restore button, and a double-click on the strip."""
        self._frameless.toggle_maximised()

    def show_system_menu(self, global_pos=None):
        """Move / Size / Minimize / Maximize / Close. Right-click and Alt+Space.

        The position defaults to the top-left of the window, which is where
        Windows puts it for the keyboard route and where the app icon is for the
        pointer one.
        """
        if global_pos is None:
            global_pos = self.mapToGlobal(QPoint(0, 0))
        self._frameless.show_system_menu(global_pos)

    def _sync_maximise_state(self):
        """Swap the maximise glyph for the restore one, and back.

        Driven from `changeEvent` rather than from the button, because the state
        changes without the button being touched: Win+Up, Aero Snap, a drag to
        the top of the screen, and a double-click on the strip all get there.
        """
        if hasattr(self, "_title_bar"):
            self._title_bar.set_maximised(
                self.isMaximized() or self.isFullScreen())

    # ------------------------------------------------------------------
    # Binding the chrome to whichever document is in front
    # ------------------------------------------------------------------

    #: The signals the window's chrome follows. Exactly one view is connected
    #: to them at a time: a background document that could still write the
    #: status bar or move the page box is the whole class of bug this phase
    #: had to avoid.
    _CHROME_SIGNALS = (
        ("title_changed", "_update_title"),
        ("status_message", "_status_message"),
        ("page_changed", "_on_view_page_changed"),
        ("fit_mode_broken", "_on_fit_mode_broken"),
        ("default_fit_requested", "_apply_default_fit"),
    )

    def _connect_view(self, view: DocumentView):
        """Wire the front document view to the window chrome it drives."""
        if view is self._connected_view:
            return
        for signal, slot in self._CHROME_SIGNALS:
            getattr(view, signal).connect(getattr(self, slot))
        self._connected_view = view

    def _disconnect_view(self, view: DocumentView):
        """Unwire a view that is no longer in front.

        Guarded on which view is actually wired, rather than just tolerating a
        failure. A move asks twice (`release_view` on the way out, then the
        front-view change the detach raises), and Qt answers a disconnect that
        was never connected with a RuntimeWarning on stderr rather than an
        exception, so "tolerant" was not the same as quiet.
        """
        if view is not self._connected_view:
            return
        for signal, slot in self._CHROME_SIGNALS:
            try:
                getattr(view, signal).disconnect(getattr(self, slot))
            except (RuntimeError, TypeError):
                pass
        self._connected_view = None

    def _on_front_view_changed(self, previous, current):
        """A different document came to the front. Rebind everything it drives."""
        if previous is not None:
            self._disconnect_view(previous)
        if current is None:
            self._page_jump.set_total(0)
            self._update_title()
            return
        self._connect_view(current)
        self._rebuild_undo_actions()
        self._sync_fit_group(current)
        # Re-announces the title, the status line and the page, so all three
        # stop showing the tab that just left.
        current.refresh_chrome()

    def _rebuild_undo_actions(self):
        """Point Edit > Undo / Redo at this WINDOW's history. Once, not per tab.

        PHASE 5 CHANGED WHAT THIS METHOD IS FOR. It used to run on every tab
        switch, because `QUndoStack.createUndoAction` binds an action to one
        stack for the life of that action and there was one stack per document.
        There is now one stack per WINDOW (see ui/undo.py: a cross-document
        page move cannot be undone as one action any other way), so the pair is
        built when the menu is and never rebound. The rebuild is kept, and made
        idempotent, because it is still the one place the pair is constructed
        and the anchor discipline below still matters: two actions carrying
        Ctrl+Z at once is an ambiguous shortcut to Qt.

        Undo can now reach a document in another tab. The command switches to
        that tab before it changes anything (see `_PageCommand._focus`), so the
        user watches the edit come back rather than finding out about it later.
        """
        if self._undo_action is not None:
            return
        self._undo_action = self._undo_stack.createUndoAction(self, "Undo")
        self._undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self._redo_action = self._undo_stack.createRedoAction(self, "Redo")
        self._redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self._edit_menu.insertAction(self._undo_anchor, self._undo_action)
        self._edit_menu.insertAction(self._undo_anchor, self._redo_action)

    def undo_stack(self) -> WindowUndoStack:
        """This window's single history. Every tab in it pushes here."""
        return self._undo_stack

    def window_id(self) -> str:
        """A stable name for this window, for a page drag's payload."""
        return self._window_id

    def _sync_fit_group(self, view: DocumentView):
        """Show the fit the ARRIVING canvas is actually in.

        Not the remembered setting. `view.default_fit_mode` is one app-wide
        value, but a manual zoom breaks the fit on one canvas and leaves the
        setting alone, so reading the setting here would light up a mode for a
        tab that is not in it.
        """
        mode = view.fit_mode()
        button = self._fit_btns.get(mode) if mode else None
        if button is not None:
            button.setChecked(True)
            self._retint_fit_icons(self._theme.palette)
            return
        self._clear_fit_group()

    def _status_message(self, text: str):
        self._status.showMessage(text)

    def show_status(self, text: str):
        """Put a line in the status bar from outside this window.

        Session restore is the caller: "3 files from the last session could not
        be found" is one line about the whole window, and eight modal dialogs
        for eight files on an offline mapped drive is the thing it exists to
        avoid.
        """
        self._status_message(text)

    def _on_view_page_changed(self, page_num: int):
        """Keep the status bar's page box on the view's page.

        The box is window chrome driven by document state, which is why it
        follows a signal instead of being poked from inside the view.
        """
        view = self.view
        if view is not None and view.has_document():
            self._page_jump.set_total(view.page_count())
            self._page_jump.set_current_page(page_num)
        else:
            self._page_jump.set_total(0)

    def _build_fit_group(self) -> QWidget:
        """The view-mode control: one icon per mode, exactly one of them active.

        Was a single text "Fit" button that only ever meant fit-page. The four
        modes are siblings, so they read as a group of icons with the mode named
        in the tooltip rather than a lone word that has to stand for all of them.
        """
        box = QWidget()
        box.setObjectName("fitGroup")
        row = QHBoxLayout(box)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(1)

        self._fit_group = QButtonGroup(self)
        self._fit_group.setExclusive(True)
        self._fit_btns: dict[str, QToolButton] = {}
        for mode, icon, tip, fallback in _FIT_CONTROLS:
            btn = QToolButton()
            btn.setObjectName("fitmode")
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            glyph = themed_icon(icon, LIGHT.text_dim) if qtawesome_available() else QIcon()
            if glyph.isNull():
                # No icon font on this machine, or a glyph id this version of the
                # font doesn't carry: a short label beats an invisible button.
                # Same fallback the page strip and the toolbar use.
                btn.setText(fallback)
                btn.setStyleSheet("font-size: 10px;")
            else:
                btn.setIcon(glyph)
                btn.setIconSize(QSize(15, 15))
            btn.clicked.connect(lambda _, m=mode: self._on_fit_clicked(m))
            self._fit_group.addButton(btn)
            self._fit_btns[mode] = btn
            row.addWidget(btn)
        return box

    def _setup_menu(self):
        mb = self.menuBar()

        fm = mb.addMenu("File")
        self._add_action(fm, "New Tab", self.new_tab, "Ctrl+T")
        # Ctrl+Shift+N, which is what every browser and every editor on this
        # machine uses for it. The tab menu's "Move to New Window" is the other
        # way to get a second window, and it is the one that carries a document.
        self._add_action(fm, "New Window", self.new_window, "Ctrl+Shift+N")
        # Was "Open / Combine PDFs…". Opening several files is N tabs now, not
        # a merge, so the one verb no longer covers both and Combine has to be
        # asked for by name.
        self._add_action(fm, "Open PDFs…", self.open_pdf, QKeySequence.StandardKey.Open)
        self._add_action(fm, "Combine PDFs…", self.combine_pdfs)
        self._add_action(fm, "Close PDF", self.close_pdf, "Ctrl+W")
        # Directly under Close, and on the key every browser uses for it. The
        # literal string for the same reason Quit is spelled out below: a
        # StandardKey is resolved by the platform and this one has no entry on
        # Windows worth trusting.
        self._add_action(fm, "Reopen Closed Tab", self.reopen_closed_tab,
                         "Ctrl+Shift+T")
        fm.addSeparator()
        self._add_action(fm, "Save", self.save_pdf, QKeySequence.StandardKey.Save)
        self._add_action(fm, "Save As…", self.save_pdf_as, "Ctrl+Shift+S")
        fm.addSeparator()
        self._add_action(fm, "Enhance for Search (OCR)…", self.enhance_for_search)
        fm.addSeparator()
        # Ctrl+Q spelled out, not QKeySequence.StandardKey.Quit. On Windows that
        # standard key resolves to the hardware Exit media key, so the menu
        # displayed "Exit" as its shortcut and Quit had no binding anybody could
        # actually press.
        self._add_action(fm, "Quit", self._quit_app, "Ctrl+Q")

        em = mb.addMenu("Edit")
        self._edit_menu = em
        # Undo/Redo are bound to the FRONT view's stack and rebuilt on every
        # tab switch (see _rebuild_undo_actions). They do not exist yet: the
        # first view is added after the menus are built. The separator below is
        # the anchor they get inserted in front of.
        self._undo_action = None
        self._redo_action = None
        self._undo_anchor = em.addSeparator()
        self._add_action(em, "Find…", self._open_search, QKeySequence.StandardKey.Find)
        em.addSeparator()
        self._add_action(em, "Copy", self.copy_selection, QKeySequence.StandardKey.Copy)
        self._add_action(em, "Paste", self.paste, QKeySequence.StandardKey.Paste)
        self._add_action(em, "Delete Selected", self._delete_key,
                         QKeySequence.StandardKey.Delete)
        em.addSeparator()
        self._add_action(em, "Bring to Front", self._bring_to_front, "Ctrl+]")
        self._add_action(em, "Send to Back", self._send_to_back, "Ctrl+[")
        em.addSeparator()
        # Ctrl+, is where every other app on this machine puts it.
        self._add_action(em, "Preferences…", self.open_preferences, "Ctrl+,")

        pm = mb.addMenu("Page")
        self._add_action(pm, "Go to Page…", self._focus_page_jump, "Ctrl+G")
        self._add_action(pm, "Delete Current Page", self.delete_current_page)

        vm = mb.addMenu("View")
        # Side page panel show/hide, remembered across runs.
        self._panel_action = QAction("Show Page Panel", self)
        self._panel_action.setCheckable(True)
        self._panel_action.setShortcut("Ctrl+B")
        self._panel_action.toggled.connect(self._on_panel_toggled)
        vm.addAction(self._panel_action)
        panel_visible = settings().view.page_panel_visible
        self._panel_action.setChecked(panel_visible)
        # Views are built after this runs and read the action for their initial
        # state (see _new_view), so there is nothing to set directly here.
        vm.addSeparator()
        self._add_action(vm, "Next Tab", self.next_tab, "Ctrl+PgDown")
        self._add_action(vm, "Previous Tab", self.previous_tab, "Ctrl+PgUp")
        # The MRU pair, and deliberately not the same order as the two above.
        self._add_action(vm, "Recent Tab", self.next_recent_tab, "Ctrl+Tab")
        self._add_action(vm, "Previous Recent Tab", self.previous_recent_tab,
                         "Ctrl+Shift+Tab")
        vm.addSeparator()
        self._theme_action = QAction("Dark Mode", self)
        self._theme_action.setShortcut("Ctrl+D")
        self._theme_action.triggered.connect(self._toggle_theme)
        vm.addAction(self._theme_action)
        # Set the initial label/icon to match the current mode.
        dark = self._theme.is_dark
        self._theme_action.setText("Light Mode" if dark else "Dark Mode")
        self._theme_action.setIcon(
            themed_icon("mdi6.weather-sunny" if dark else "mdi6.weather-night",
                        self._theme.palette.text))

        hm = mb.addMenu("Help")
        # The version, sitting directly above the control that asks whether it
        # is the newest one. Disabled because it is a statement, not a command:
        # "Check for Updates" is the only thing here to press, and the number it
        # is talking about should be readable without pressing anything.
        self._version_action = QAction(f"Rapid PDF v{APP_VERSION}", self)
        self._version_action.setEnabled(False)
        hm.addAction(self._version_action)
        self._add_action(hm, "Check for Updates…", self.check_for_updates)
        hm.addSeparator()
        self._add_action(hm, "About Rapid PDF", self._about)

    # ------------------------------------------------------------------
    # Preferences (see ui/preferences_dialog.py)
    # ------------------------------------------------------------------

    def open_preferences(self):
        """Edit > Preferences / Ctrl+, . One dialog, raised if already open.

        Not modal: the point of the dialog is that its controls and the View
        menu's are the same controls, and a modal window would eat the Ctrl+B
        and Ctrl+D that demonstrate it.
        """
        if self._prefs_dialog is None:
            # Built once and kept. Deleting it on close and rebuilding it would
            # mean tearing down and re-making the connections that are the
            # whole point of it, for no gain: it is six controls.
            self._prefs_dialog = PreferencesDialog(self)
        else:
            self._prefs_dialog.reload()
        self._prefs_dialog.show()
        self._prefs_dialog.raise_()
        self._prefs_dialog.activateWindow()
        return self._prefs_dialog

    # ------------------------------------------------------------------
    # Updates (see ui/update_notice.py and core/update/)
    # ------------------------------------------------------------------

    def check_for_updates(self):
        """Help menu, and the Preferences button. Same check as startup, but it
        says so when there is nothing to report, because somebody asked."""
        self._update_notice.start_check(manual=True)

    def _about(self):
        QMessageBox.about(
            self, "About Rapid PDF",
            f"<b>Rapid PDF {APP_VERSION}</b>"
            "<p>Fast PDF page management and markup.</p>"
            "<p>Copyright (c) 2026 Lucas Ruiz</p>")

    def _maybe_save_every_tab(self) -> bool:
        """Put the unsaved-changes question to every open document in turn.

        Cancel anywhere aborts the whole close, and the tabs already answered
        for stay answered: the ones that were saved are saved, the ones that
        were discarded are still dirty and will ask again next time. That is
        the same trade every tabbed editor makes, and the alternative (roll the
        saves back) is not a thing that can be done to a file on disk.
        """
        for view in self._area.views():
            if not view.maybe_save_before_close():
                return False
        return True

    def _on_update_staged(self, staged):
        """A verified update is on disk and the app has to close for the swap.

        The unsaved-changes prompt runs FIRST, and a cancelled prompt leaves
        everything as it was: the download stays staged and the strip keeps
        offering it. The helper is only started once closing is certain,
        because the moment it starts it is watching for this process to exit.
        """
        if not self._maybe_save_every_tab():
            self._update_notice.apply_cancelled()
            return
        if not self._update_notice.launch_swap(staged):
            return
        for view in self._area.views():
            view.mark_clean()    # the prompt above has already settled these
        self._force_quit = True
        self.close()

    def _toggle_theme(self):
        self._theme.toggle()

    def _on_panel_toggled(self, checked: bool):
        # One app-wide setting, so every tab moves, not just the front one.
        # A background tab left with a stale panel would jump on activation.
        for view in self._area.views():
            view.set_page_panel_visible(checked)
        settings().view.page_panel_visible = checked

    def _add_action(self, menu, label: str, slot, shortcut=None):
        action = QAction(label, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)

    def _setup_shortcuts(self):
        # Window level on purpose: two live views each owning a QShortcut for
        # "v" is ambiguous to Qt, which then routes it to neither. `view` is
        # the front one, so a single shortcut still reaches the right canvas.
        for key, tool in [("v", "select"), ("h", "pan"), ("r", "rect"),
                          ("l", "line"), ("t", "text")]:
            sc = QShortcut(key, self)
            sc.activated.connect(lambda t=tool: self.trigger_tool(t))
        # Alt+Space is the system menu on every Windows window and it stops
        # working the moment the caption stops being the system's. Windows would
        # still handle it through DefWindowProc, but Qt eats the key first, so
        # it is bound here and lands in the same place the right-click does.
        system_menu = QShortcut("Alt+Space", self)
        system_menu.activated.connect(self.show_system_menu)

    def trigger_tool(self, tool: str):
        view = self.view
        if view is not None:
            view.trigger_tool(tool)

    # ------------------------------------------------------------------
    # Tabs
    # ------------------------------------------------------------------

    def new_tab(self) -> DocumentView:
        """Ctrl+T, and a double-click on empty tab bar space."""
        view = self._new_view()
        self._area.add_view(view)
        return view

    def tab_for_restore(self) -> DocumentView:
        """An empty tab to stage a restored document into. Session restore only.

        Two differences from `new_tab`, and both are about not reading a file.
        It reuses the blank tab a new window comes up with, the way
        `_target_view` does for an Open, so a restored window does not carry a
        spare. And it adds every tab after that WITHOUT activating it: bringing
        a tab to the front is exactly what opens a lazy one, so `new_tab` here
        would read all eight documents on the way through. See ui/session.py.
        """
        view = self.view
        if view is not None and view.is_empty():
            return view
        view = self._new_view()
        self._area.add_view(view, activate=False)
        return view

    def next_tab(self):
        """Ctrl+PgDn. Positional, and wrapping.

        Positional, not most-recently-used: MRU is the Ctrl+Tab ordering and it
        is phase 3, because it needs a visit history nothing keeps yet.
        """
        self._area.step_current(1)

    def previous_tab(self):
        """Ctrl+PgUp."""
        self._area.step_current(-1)

    def next_recent_tab(self):
        """Ctrl+Tab. Most recently used, NOT positional.

        Deferred by phase 2, confirmed missing by phase 3, and here at last.
        Ctrl+PgDn stays the positional one: "the tab to the right" and "the tab
        I was just in" are different questions and the whole value of having
        both is that they are not the same key.
        """
        self._start_mru_walk(1)

    def previous_recent_tab(self):
        """Ctrl+Shift+Tab. Back up the same stack."""
        self._start_mru_walk(-1)

    def _start_mru_walk(self, delta: int):
        """Step the MRU, and start watching for Ctrl coming up if it stepped.

        The list is frozen for as long as Ctrl is held, so tapping Tab walks
        back through the stack rather than flipping between the top two.
        """
        if not self._area.step_mru(delta):
            return
        if self._mru_filter_on:
            return
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._mru_filter_on = True

    def eventFilter(self, obj, event):
        """Watch the whole application for Ctrl being released.

        An application filter and not `keyReleaseEvent`, because the release
        lands on whatever has keyboard focus (a canvas, a thumbnail list, the
        page box) and several of those consume it long before it would reach
        the window. Installed only while a walk is in flight and removed the
        moment it ends, so nothing is filtering in the general case.
        """
        if (self._mru_filter_on
                and event.type() == QEvent.Type.KeyRelease
                and event.key() == Qt.Key.Key_Control):
            self._end_mru_walk()
        return super().eventFilter(obj, event)

    def _end_mru_walk(self):
        """Commit the walk: the tab landed on becomes the most recent."""
        if self._mru_filter_on:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._mru_filter_on = False
        self._area.end_mru_walk()

    def close_tab(self, index: int) -> bool:
        """Close one tab: its own X, its middle-click, or Ctrl+W on the front one.

        A tab holding a document goes through `request_close`, which prompts
        for unsaved changes, closes the document and then announces itself; the
        tab-or-window decision lands in `_on_view_close_requested`. An empty
        tab has nothing to prompt about, and the last empty tab is what an
        empty window looks like, so closing that one does nothing at all.

        A RESTORED TAB THAT WAS NEVER OPENED counts as holding a document, and
        `close_document` answers for it without a prompt. Asking
        `has_document()` here would have made Ctrl+W dead on every tab of a
        window that had just come back, since none of them has read its file.

        THIS IS ALSO WHERE A CLOSED TAB IS REMEMBERED, for Ctrl+Shift+T. Every
        route that closes a tab arrives here: the tab's X, its middle click,
        Ctrl+W, and the context menu's Close, Close Others and Close to the
        Right. `DocumentArea.remove_view` is one level too low to record at,
        because it also catches a placeholder being retired and a cancelled
        combine taking its own empty tab away again, neither of which is a
        document the user closed. See ui/reopen_stack.py.
        """
        view = self._area.view_at(index)
        if view is None:
            return False
        if not view.is_empty():
            # Read BEFORE the close and kept only once the close has happened.
            # `request_close` reaches `PDFDocument.close`, which drops the path
            # and puts the page back to zero, so a capture taken afterwards
            # would describe nothing. And it answers False when the save prompt
            # is cancelled, which is not a close and must leave no entry.
            entry = capture_view(view)
            if not view.request_close():
                return False
            reopen_stack().push(entry)
            return True
        if self._area.count() <= 1:
            return False
        self._area.remove_view(index)
        return True

    @staticmethod
    def _still_on_disk(path: str) -> bool:
        """Whether a remembered path is still openable, cheaply and quietly.

        The whole existence check for a reopen. A file that has been deleted,
        renamed or unmounted since it was closed is skipped without a word: the
        user pressed a key expecting a tab, and a modal complaint about a file
        they may have deleted on purpose is not an answer to that.
        """
        try:
            return bool(path) and os.path.isfile(path)
        except (OSError, ValueError):
            return False

    def _index_of_open_file(self, path: str) -> int:
        """The tab in THIS window already holding this file, or -1.

        `same_file` rather than `os.path.samefile`, which hits the filesystem
        and raises for anything that has gone. See ui/window_registry.py.
        """
        for index, view in enumerate(self._area.views()):
            if same_file(view.document_path(), path):
                return index
        return -1

    def _drop_spare_tab(self, view, mine: bool):
        """Take away a tab this reopen made and then found no use for."""
        if view is None or not mine:
            return
        index = self._area.index_of(view)
        if index >= 0 and self._area.count() > 1:
            self._area.remove_view(index)

    def reopen_closed_tab(self) -> bool:
        """Ctrl+Shift+T. Bring the most recently closed tab back, here.

        HERE meaning this window, whichever window the key was pressed in. The
        history itself is application wide (see ui/reopen_stack.py: a per window
        stack dies with the window, at the exact moment several tabs closed at
        once), but the shortcut is window scoped, so where a tab comes back is
        the one part of it that is local. That is what browsers do too.

        THE ENTRY IS POPPED WHETHER OR NOT IT CAN BE USED. A file that has gone
        since it was closed is discarded silently and the one below it is tried,
        down to an empty stack, so holding the key does not stall on a path that
        will never open again. Nothing here raises and nothing here prompts.

        The restore path is reused rather than `open_path`, and that is not just
        tidiness: `stage_path` plus `ensure_loaded` is the only route that
        converts a remembered zoom out of the raster scale it was measured in
        (see DocumentView.ensure_loaded), so a document reopened after the
        render scale changed comes back the size it was rather than double it.

        A file already open in this window activates its tab instead of opening
        a second copy, which is the rule `open_paths` follows for every other
        way a path arrives.

        Returns True when a tab came back, False when the stack had nothing
        left that could be opened.
        """
        stack = reopen_stack()
        view = None
        mine = False
        while True:
            entry = stack.pop()
            if entry is None:
                break
            path = entry.get("path")
            if not self._still_on_disk(path):
                continue
            already = self._index_of_open_file(path)
            if already >= 0:
                self._area.set_current_index(already)
                self._drop_spare_tab(view, mine)
                return True
            if view is None:
                before = self._area.count()
                view = self.tab_for_restore()
                mine = self._area.count() > before
            if not view.stage_path(path, page=entry.get("page") or 0,
                                   zoom=entry.get("zoom") or 0.0,
                                   fit_mode=entry.get("fit_mode"),
                                   raster_scale=entry.get("raster_scale") or 0.0):
                continue
            index = self._area.index_of(view)
            if index >= 0:
                self._area.set_current_index(index)
            # By hand as well, for the case Qt emits nothing for: the tab being
            # reopened is already the current one, so setCurrentIndex is a no-op
            # and nothing would open the file. Same reason ui/session.py calls
            # it after filling a window.
            view.ensure_loaded()
            if view.has_document():
                return True
            # It is on disk and it would not open. `open_path` has already said
            # so in its own words, the view is an ordinary empty tab again, and
            # the entry below is the next thing to try.
        self._drop_spare_tab(view, mine)
        return False

    def _on_view_close_requested(self, view=None):
        """A view's document has gone and the tab is what is left.

        Closing the last tab closes the window, the way it does in every other
        tabbed app. The document is already closed by the time this runs, so
        `closeEvent` finds nothing left to prompt about.
        """
        if view is None:
            view = self.view
        if self._area.count() <= 1:
            self.close()
            return
        index = self._area.index_of(view)
        if index >= 0:
            self._area.remove_view(index)

    def _duplicate_tab(self, view: DocumentView):
        """Tab menu > Duplicate Tab: the same file, open a second time.

        Deliberately a second independent document rather than a second view of
        one: one canvas per document is forced by `PDFCanvas.set_document`, and
        two tabs sharing a document would share an undo stack neither of them
        owns. Unsaved markup does not come along, because it is not in the file.
        """
        path = view.document_path()
        if not path:
            return
        self.new_tab().open_path(path)

    def _target_view(self) -> DocumentView:
        """Somewhere empty to open into: the front tab if it is empty, else a
        new one. This is what makes the very first Open reuse the tab that is
        already there instead of leaving a blank one behind it."""
        view = self.view
        if view is not None and view.is_empty():
            return view
        return self.new_tab()

    # ------------------------------------------------------------------
    # File operations. The window owns the menu; the document owns the work,
    # so each of these is one hop into the front view.
    # ------------------------------------------------------------------

    def open_pdf(self):
        self.view.open_pdf()

    def open_paths(self, paths: list):
        """Open each PDF in its own tab.

        ONE FILE, ONE TAB. This used to hand the whole list to the open
        document, which appended every one of them onto the end of it: opening
        a second PDF silently merged it into the one being read, and the next
        Save wrote that merge over the file. Merging is now something you ask
        for by name (File > Combine PDFs, or the Organizer's Add Pages).

        A file that is already open activates its tab rather than opening a
        second copy of itself.
        """
        for path in paths:
            index = self._area.index_of_path(path)
            if index >= 0:
                self._area.set_current_index(index)
                continue
            self._target_view().open_path(path)

    def combine_pdfs(self):
        self.view.combine_pdfs()

    def combine_paths(self, paths: list):
        """Stage a combine and land the merge in a tab of its own.

        The open document is no longer closed to make room for it, which was
        the other way a merge could take a document you were reading away.
        """
        if not paths:
            return
        view = self.view
        borrowed = view is not None and view.is_empty()
        if not borrowed:
            view = self.new_tab()
        view.combine_paths(list(paths))
        if not borrowed and not view.has_document():
            # Cancelled at the dialog: take the empty tab away again.
            index = self._area.index_of(view)
            if index >= 0 and self._area.count() > 1:
                self._area.remove_view(index)

    def close_pdf(self):
        """File > Close PDF (Ctrl+W). Closes the front TAB now."""
        self.close_tab(self._area.current_index())

    def save_pdf(self) -> bool:
        return self.view.save_pdf()

    def save_pdf_as(self) -> bool:
        return self.view.save_pdf_as()

    def enhance_for_search(self):
        self.view.enhance_for_search()

    def copy_selection(self):
        self.view.copy_selection()

    def paste(self):
        self.view.paste()

    def paste_image(self):
        self.view.paste_image()

    def delete_current_page(self):
        self.view.delete_current_page()

    def _open_search(self):
        self.view.open_search()

    def _delete_key(self):
        self.view.delete_key()

    def _bring_to_front(self):
        self.view.bring_to_front()

    def _send_to_back(self):
        self.view.send_to_back()

    def handle_cli_files(self, files: list, combine: bool):
        """One aggregated shell/CLI launch batch, landing in THIS window.

        Decided by VERB, not by how many files arrived. `--combine` is the
        Explorer verb that means merge, and it opens the staged Combine dialog
        whether it carries two files or twenty. A plain open of N files opens N
        tabs, because that is what opening N files means now; it used to close
        whatever was open and stage a Combine as soon as the count passed one,
        which made "open these three drawings" destroy the document on screen.

        `main.py` no longer wires `batch_ready` here. It goes to
        `WindowRegistry.route_open`, which decides WHICH window first (a file
        already open anywhere wins, then the last window touched) and then does
        this. This stayed as the single-window verb underneath it, and as what
        every existing test drives.
        """
        # Un-minimize and raise: the user just acted in Explorer.
        self.raise_and_focus()
        if not files:
            return
        if combine:
            self.combine_paths(files)
        else:
            self.open_paths(files)

    # ------------------------------------------------------------------
    # The status bar's page box
    # ------------------------------------------------------------------

    def _focus_page_jump(self):
        """Ctrl+G / Page > Go to Page. Puts the cursor in the status-bar page box."""
        self._page_jump.focus_box()

    def _on_page_jump(self, page_num: int):
        """A page number was typed into the status-bar box."""
        view = self.view
        if view is not None:
            view.jump_to_page(page_num)

    # ------------------------------------------------------------------
    # What Preferences edits. Narrow on purpose: the dialog holds no copy of
    # any setting, it drives and follows the same objects the menus do.
    # ------------------------------------------------------------------

    def page_panel_action(self) -> QAction:
        """The View menu's Show Page Panel action, which owns that setting."""
        return self._panel_action

    def theme_manager(self) -> ThemeManager:
        """The ThemeManager, which owns the theme and persists it itself."""
        return self._theme

    def fit_mode_labels(self) -> dict:
        """mode -> the name it goes by, in the order the status bar shows it."""
        return {mode: tip for mode, _, tip, _ in _FIT_CONTROLS}

    def current_fit_mode(self) -> str:
        """The chosen page-fit mode.

        This is the remembered choice, not the live state of the canvas: a
        manual zoom breaks the fit that is applied without changing the mode
        the user picked, and it is the pick that both controls display.
        """
        return settings().view.default_fit_mode

    def choose_fit_mode(self, mode: str):
        """Apply a page-fit mode, show it as chosen, and remember it.

        The single entry point. The status bar's icons and the Preferences
        dropdown both call this and both listen to `fit_mode_chosen`, which is
        what keeps them from drifting into two separate values. It stayed on
        the window because the setting is one app-wide value and two of its
        three surfaces are window chrome; only the applying goes to the canvas.
        """
        if mode not in FIT_MODES:
            return
        view = self.view
        if view is not None:
            view.set_fit_mode(mode)
        button = self._fit_btns.get(mode)
        if button is not None:
            button.setChecked(True)
        self._retint_fit_icons(self._theme.palette)
        settings().view.default_fit_mode = mode
        self.fit_mode_chosen.emit(mode)

    def _apply_default_fit(self):
        """A freshly opened document starts in the remembered view mode."""
        self.choose_fit_mode(settings().view.default_fit_mode)

    def _on_fit_clicked(self, mode: str):
        self.choose_fit_mode(mode)

    def _retint_fit_icons(self, palette):
        """The active mode sits on an accent fill, so its glyph has to flip to the
        on-accent color. Qt won't swap a QIcon by QSS state, so it happens here."""
        if not qtawesome_available() or not hasattr(self, "_fit_btns"):
            return
        icons = {mode: icon for mode, icon, _, _ in _FIT_CONTROLS}
        for mode, btn in self._fit_btns.items():
            if btn.icon().isNull():
                continue          # this one fell back to a text label
            color = palette.accent_text if btn.isChecked() else palette.text_dim
            btn.setIcon(themed_icon(icons[mode], color))

    def _on_fit_mode_broken(self):
        # User zoomed manually, so no mode is active any more.
        self._clear_fit_group()

    def _clear_fit_group(self):
        # An exclusive QButtonGroup refuses to leave every button unchecked, so
        # exclusivity comes off for the one call that clears it.
        checked = self._fit_group.checkedButton()
        if checked is None:
            return
        self._fit_group.setExclusive(False)
        checked.setChecked(False)
        self._fit_group.setExclusive(True)
        self._retint_fit_icons(self._theme.palette)

    # ------------------------------------------------------------------
    # The title bar
    # ------------------------------------------------------------------

    def _update_title(self):
        """Reflect the open file and unsaved state in the window title.

        Qt renders the '[*]' placeholder as '*' only while windowModified is True.
        The view says WHEN (title_changed); the name and the dirty flag are read
        back off it here, because the title bar is the window's.

        The separator is a plain hyphen. It used to be an em dash, which reads
        badly at title-bar size and does not survive every place a window title
        gets copied into (taskbar previews, screenshots, bug reports).

        The name is one word. It was "Rapid PDF" in earlier releases, and at
        title-bar size the space read as two separate things. This string no longer
        reaches the caption at all, because the tab strip is the title bar now,
        so where it actually lands is the taskbar button and Alt+Tab. The
        filename stays appended: with several windows open, dropping it would
        give Alt+Tab a row of identical entries.

        NOT renamed with it: main.py's setApplicationName(), which is an
        identity string rather than a display one. QSettings and the
        %LOCALAPPDATA% settings folder are both named off it, so changing it
        would silently move every existing user's preferences and restored
        session and leave the old folder behind. See tests/test_product_name.py.
        """
        view = self.view
        name = view.document_name() if view is not None else None
        if name is None:
            self.setWindowModified(False)
            self.setWindowTitle("RapidPDF")
            return
        self.setWindowModified(view.is_dirty())
        self.setWindowTitle(f"RapidPDF - {name}[*]")

    def _quit_app(self):
        """Quit menu / Ctrl+Q. Quits the APPLICATION, not this window.

        Every window is asked, and the first one to refuse (a cancelled save
        prompt) stops the whole thing and leaves what is still open exactly as
        it was, including the `_force_quit` flags, which are put back. Closing
        the last one is what actually ends the process; the registry does that,
        not this method, which is why there is no `app.quit()` here.
        """
        for window in self._registry.windows():
            window._force_quit = True
            if not window.close():
                # Cancelled. The flag goes back on the one that refused; every
                # window after it was never told, and every one before it has
                # already gone.
                window._force_quit = False
                return

    # ------------------------------------------------------------------
    # Closing
    # ------------------------------------------------------------------

    def _connect_session_manager(self):
        """Listen for the session ending, if this platform reports one.

        Not every platform build emits `commitDataRequest`, and a headless test
        app has no session manager at all, so a missing signal is fine:
        `_session_is_ending` falls back to `isSavingSession()`.
        """
        app = QGuiApplication.instance()
        try:
            app.commitDataRequest.connect(self._on_commit_data)
        except (AttributeError, RuntimeError, TypeError):
            pass

    def _on_commit_data(self, manager=None):
        """Windows is ending the session (shutdown, restart, log off).

        Qt raises `commitDataRequest` from WM_QUERYENDSESSION. Everything that
        happens on this signal is on Windows's clock, so it does two things and
        nothing else: remember that the close about to arrive is a session end,
        and get the settings on disk while there is still a process to do it.
        """
        self._session_ending = True
        try:
            manager.setRestartHint(manager.RestartHint.RestartNever)
        except (AttributeError, TypeError):
            pass
        settings().flush()

    def _session_is_ending(self) -> bool:
        """Whether the close now in flight is Windows shutting the session down.

        Two independent reads because neither is reliable alone: the flag set by
        `commitDataRequest`, and Qt's own `isSavingSession()` for the case where
        the signal never reached us.
        """
        if self._session_ending:
            return True
        app = QGuiApplication.instance()
        try:
            return bool(app is not None and app.isSavingSession())
        except (AttributeError, RuntimeError):
            return False

    def _teardown_for_quit(self):
        """Release everything the process owns, on the way out for good."""
        # An application-wide event filter belonging to a window that is going
        # away outlives the window that installed it. Ctrl+Tab arms one.
        self._end_mru_walk()
        # Each view drops its render clones and closes its document.
        for view in self._area.views():
            view.teardown()
        # A check thread still running when the window goes is a crash on
        # exit. Bounded wait, see UpdateNotice.shutdown().
        self._update_notice.shutdown()
        settings().flush()   # writes are debounced; the timer may never fire

    def _confirm_closing_several_tabs(self, count: int) -> bool:
        """`close.confirm_multiple_tabs`, which finally has something to guard.

        The key has been in the schema since phase 0 with no reader and no
        control, because there were no tabs for it to be about. It is about
        losing your place, not about losing your work: an unsaved document gets
        a real save prompt of its own, and this question is skipped entirely
        when there is one, because two dialogs asking the same thing in a row
        is worse than one.
        """
        reply = QMessageBox.question(
            self, "Close Rapid PDF",
            f"{count} documents are open. Close them all?",
            QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Close,
        )
        return reply == QMessageBox.StandardButton.Close

    def closeEvent(self, event):
        """The X, Alt+F4, Ctrl+Q, and the session manager all land here.

        Precedence, highest first, and none of it changed with tabs:

        1. a session end is never blocked, prompted or delayed;
        2. `close.x_closes == "document"` empties the front tab and keeps the
           window, which is the pre-1.6 behaviour, still available;
        3. unsaved changes are prompted for, per dirty document, and Cancel
           anywhere aborts the whole close;
        4. `close.confirm_multiple_tabs` asks before several tabs go at once,
           and only when nothing is dirty (see _confirm_closing_several_tabs).

        THE SESSION IS RECORDED ON EVERY PATH THAT ACTUALLY CLOSES, and always
        before `_teardown_for_quit`, which closes the documents it would have
        been read from. See `_record_session` for the one decision it makes.
        """
        # Windows is shutting down. Anything that stalls here is what makes it
        # put up "this app is preventing shutdown", so there is no prompt and,
        # above all, no event.ignore(). An unsaved document is lost the same way
        # it would be in any app that does not implement the full session
        # protocol, which is the accepted trade for not blocking the machine.
        if self._session_is_ending():
            self._record_session()
            self._teardown_for_quit()
            self._registry.unregister(self)
            recorder().save()
            super().closeEvent(event)
            event.accept()
            return

        # The X closes the window and everything in it. Setting close.x_closes
        # to "document" restores the older behaviour: the front PDF closes and
        # the window stays up. The Quit menu (_force_quit) overrides either way.
        view = self.view
        if (view is not None and view.has_document() and not self._force_quit
                and settings().close.x_closes == "document"):
            if not view.maybe_save_before_close():
                self._force_quit = False
                event.ignore()
                return
            # REMEMBERED FOR Ctrl+Shift+T, and this is the one close path that
            # does not run through `close_tab`. The tab survives, but the
            # document the user was reading does not, and "bring back what I
            # just closed" is a question about the document rather than about
            # the widget holding it. Read after the prompt and before
            # `clear_document`, which is what drops the path.
            entry = capture_view(view)
            view.clear_document()
            reopen_stack().push(entry)
            recorder().save()   # the window stays, and it holds one tab less
            event.ignore()      # the window stays, empty
            return

        views = self._area.views()
        # A restored tab counts even though it has not read its file yet. This
        # warning is about losing your PLACE, and a window of eight tabs the
        # user has not clicked into is exactly the place it is about.
        open_docs = [v for v in views if not v.is_empty()]
        any_dirty = any(v.is_dirty() for v in open_docs)

        # The count warning, and only when nothing is dirty: a dirty tab is
        # about to be asked a better version of the same question.
        if (not any_dirty and len(open_docs) > 1
                and settings().close.confirm_multiple_tabs):
            if not self._confirm_closing_several_tabs(len(open_docs)):
                self._force_quit = False
                event.ignore()
                return

        # Unsaved changes are prompted for per document, and the answer
        # overrides the setting: Cancel aborts the close outright.
        if not self._maybe_save_every_tab():
            self._force_quit = False
            event.ignore()
            return

        self._record_session()
        self._teardown_for_quit()
        # Leaving the registry is what ends the application when this was the
        # last window. Nothing here decides that: `main.py` turns Qt's own
        # quit-on-last-window off precisely so one place does.
        self._registry.unregister(self)
        # After the unregister, so the windows still open are the ones read.
        recorder().save()
        super().closeEvent(event)

    def _record_session(self):
        """Fix this window's entry in the session, on the way out.

        The one decision: whether this close is part of the APPLICATION going
        down. If it is, the window stays in the session, because the whole
        arrangement is what gets remembered and by `aboutToQuit` its views have
        been torn down. If the app carries on without it, the user closed it on
        purpose and it drops out. See ui/session.py.
        """
        shutting_down = (self._force_quit or self._session_is_ending()
                         or self._registry.count() <= 1)
        recorder().note_closing(self, shutting_down)
