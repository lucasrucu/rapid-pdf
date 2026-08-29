"""Every open window, who was touched last, and where an incoming file lands.

Phase 3 of docs/tabs-plan.md. Phase 2 put several documents in one window; this
is the thing that knows about several windows. It is deliberately the whole
mechanism the tear-off needs, driven by a MENU ITEM rather than a gesture, so
all of it is reachable from a headless test. Phase 4 then adds mouse tracking
that calls in here and nothing else, and can be switched off without taking
multi-window with it.

WHAT IT OWNS, AND WHY EACH ONE IS HERE RATHER THAN ON A WINDOW.

  - ACTIVATION ORDER. "The active window" is not a property any single window
    can answer, and Qt's own `QApplication.activeWindow()` is None the moment
    focus is on a dialog, a menu, or another application. A file arriving from
    Explorer still has to land somewhere sensible, so the order is recorded as
    it happens (`note_activated`, driven by MainWindow.changeEvent) and read
    back later.
  - APP LIFETIME. `QApplication.setQuitOnLastWindowClosed(False)` is set in
    main.py and the decision moves here, to `unregister`. One place decides, so
    a torn-off window closing while its parent lives needs no special case, and
    neither does the parent closing while the torn-off child lives.
  - ROUTING. `route_open` is the single answer to "these paths arrived, what
    now", whether they came from the shell, a second launch, or a file drop.

OWNERSHIP IS WEAK, ON PURPOSE. `_windows` is a plain list and a window is
dropped from it in `unregister`, which `MainWindow.closeEvent` calls. Belt and
braces, `destroyed` is connected to a purge, because a Python reference to a
window whose C++ half has already gone is the classic "the window will not die"
bug: Qt deletes the widget, the registry still names it, and every later sweep
touches a dangling wrapper.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

try:                                    # PySide6 ships it; a stub keeps import safe
    from shiboken6 import isValid as _cpp_alive
except ImportError:                     # pragma: no cover - PySide6 always has it
    def _cpp_alive(obj) -> bool:
        return True


def same_file(a: str, b: str) -> bool:
    """Whether two paths name the same file, for the purposes of tabs.

    Case-folded and absolute, which is what Windows means by "the same file".
    Deliberately not `os.path.samefile`: that hits the filesystem and raises
    for anything that has been deleted or unmounted since it was opened, and a
    tab holding a file that has gone is still that tab.
    """
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class WindowRegistry(QObject):
    """The open windows, in the order they were last touched."""

    #: A window joined. Carries the window.
    window_added = Signal(object)

    #: A window left. Carries the window, which may already be part way
    #: through its own destruction: read nothing off it but its identity.
    window_removed = Signal(object)

    #: The last window went. `unregister` quits the application right after
    #: emitting this, unless `quit_on_last_window` has been turned off.
    last_window_closed = Signal()

    _instance: "WindowRegistry | None" = None

    def __init__(self, parent=None):
        super().__init__(parent)
        # Most recently activated FIRST. See note_activated.
        self._windows: list = []
        # The manager new windows are built with, so a second window opens in
        # the theme the first one is in rather than re-reading the setting.
        self._theme = None
        # Off in tests that want to watch the last close without ending the
        # process. The real app leaves it on: see main.py.
        self.quit_on_last_window = True

    # ------------------------------------------------------------------
    # The one registry
    # ------------------------------------------------------------------

    @classmethod
    def instance(cls) -> "WindowRegistry":
        if cls._instance is None:
            cls._instance = WindowRegistry()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Throw the registry away and start again. Tests only.

        Windows outlive a single test far more easily than they should
        (offscreen never runs an event loop, so nothing is ever really torn
        down), and a registry carrying the previous test's windows makes the
        next one route into a widget that is half deleted.
        """
        cls._instance = None

    def set_theme(self, theme):
        """The ThemeManager every later window is built with."""
        self._theme = theme

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def register(self, window):
        """Take a window into the registry. Called from MainWindow.__init__.

        A brand new window goes to the FRONT of the activation order: it is
        about to be shown and it is what the user just asked for, and on
        Windows the activation event that would say so arrives later (or, in a
        headless run, never).
        """
        if any(w is window for w in self._windows):
            return
        self._windows.insert(0, window)
        try:
            window.destroyed.connect(self._purge_dead)
        except (AttributeError, RuntimeError, TypeError):
            pass
        self.window_added.emit(window)

    def unregister(self, window):
        """Drop a window, and quit the app if it was the last one.

        This is THE place app lifetime is decided. `main.py` turns Qt's own
        quit-on-last-window off precisely so that this runs instead: Qt counts
        every top-level widget, so a Preferences dialog or a modal message box
        left over from a closing window makes its count wrong, and it has no
        opinion at all about a window that is registered but not yet shown.
        """
        before = len(self._windows)
        self._windows = [w for w in self._windows if w is not window]
        if len(self._windows) == before:
            return
        self.window_removed.emit(window)
        if self._windows:
            return
        self.last_window_closed.emit()
        if self.quit_on_last_window:
            app = QApplication.instance()
            if app is not None:
                app.quit()

    def _purge_dead(self, *_args):
        """Drop any window whose C++ half has already been deleted.

        `destroyed` runs while the widget is being torn down, so nothing here
        may touch the object: `isValid` is a pure question about the C++
        pointer. This is the backstop for a window that went without its
        `closeEvent` running (a `deleteLater` on a parent, a test that drops
        its last reference), not the normal path, which is `unregister`.
        """
        alive = [w for w in self._windows if _cpp_alive(w)]
        if len(alive) == len(self._windows):
            return
        self._windows = alive
        if not alive:
            self.last_window_closed.emit()
            if self.quit_on_last_window:
                app = QApplication.instance()
                if app is not None:
                    app.quit()

    # ------------------------------------------------------------------
    # Reading it
    # ------------------------------------------------------------------

    def windows(self) -> list:
        """Every open window, most recently activated first."""
        self._windows = [w for w in self._windows if _cpp_alive(w)]
        return list(self._windows)

    def count(self) -> int:
        return len(self.windows())

    def note_activated(self, window):
        """Record that this window came to the front.

        Driven by `MainWindow.changeEvent` on `ActivationChange`. It is the
        only thing that keeps `active_window` honest, which is why the routing
        rules below are all phrased in terms of "last touched" rather than
        anything Qt can be asked directly.
        """
        if not any(w is window for w in self._windows):
            return
        if self._windows[0] is window:
            return
        self._windows = [w for w in self._windows if w is not window]
        self._windows.insert(0, window)

    def active_window(self):
        """The window a new document should land in, or None with none open.

        The last one touched, NOT the oldest. Opening a file from Explorer
        while reading in a second window puts it in the window being read,
        which is the only answer that is ever right.
        """
        windows = self.windows()
        return windows[0] if windows else None

    def views(self):
        """Every open document in the app, as (window, view) pairs.

        In window activation order, and within a window in tab order.
        """
        for window in self.windows():
            for view in window.document_area().views():
                yield window, view

    def find_by_path(self, path: str):
        """The (window, view) already showing this file, or None.

        This is what stops a second copy of a document opening in a second
        window, which is the multi-window version of the duplicate-tab check
        phase 2 put in `DocumentArea.index_of_path`.
        """
        if not path:
            return None
        for window, view in self.views():
            if same_file(view.document_path(), path):
                return window, view
        return None

    def dirty_views(self) -> list:
        """Every (window, view) with unsaved changes, anywhere in the app."""
        return [(w, v) for w, v in self.views() if v.has_document() and v.is_dirty()]

    # ------------------------------------------------------------------
    # Making windows
    # ------------------------------------------------------------------

    def create_window(self, theme=None, show: bool = True):
        """Build a window, which registers itself on the way up.

        The import is late because `ui.main_window` imports this module: the
        window has to know the registry to join it, and the registry only has
        to know the window to make one.
        """
        from ui.main_window import MainWindow

        if theme is None:
            active = self.active_window()
            theme = self._theme
            if theme is None and active is not None:
                theme = active.theme_manager()
        window = MainWindow(theme=theme)
        if show:
            window.show()
        return window

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def route_open(self, paths: list, combine: bool = False):
        """Where an incoming batch of files goes. Returns the window raised.

        Wired to `InstanceServer.batch_ready` in main.py, and the same rules
        serve a shell file drop. In order:

        1. a path already open ANYWHERE activates that window and that tab and
           raises it, instead of opening a second copy of the file;
        2. anything else opens as a new tab in the active window, which is the
           last one touched;
        3. no windows at all means make one first. That is not hypothetical:
           `setQuitOnLastWindowClosed(False)` means a running process with
           every window closed is a state that exists, and a launch forwarded
           into it has to produce a window rather than nothing;
        4. `--combine` still opens the staged Combine dialog, parented to the
           active window, and its merge lands as one new tab there. Decided by
           the VERB, not the file count, exactly as phase 2 left it.
        """
        target = self.active_window()
        if target is None:
            target = self.create_window()
        target.raise_and_focus()

        paths = [p for p in (paths or []) if p]
        if not paths:
            return target                 # a bare relaunch: raising IS the job

        if combine:
            target.combine_paths(paths)
            target.raise_and_focus()
            return target

        raised = target
        for path in paths:
            hit = self.find_by_path(path)
            if hit is not None:
                window, view = hit
                window.activate_view(view)
                raised = window
                continue
            target.open_paths([path])
            raised = target
        raised.raise_and_focus()
        return raised
