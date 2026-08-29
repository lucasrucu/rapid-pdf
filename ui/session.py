"""What was open last time, and putting it back. Phase 6 of docs/tabs-plan.md.

The last phase, and the smallest, because phases 1 to 5 built everything it
needs: a view that owns one document, an area that holds several of them, a
registry that knows every window. All this adds is a record of that arrangement
and a way to rebuild it.

WHERE IT IS KEPT. In the settings store, under `session.windows`, alongside
`startup.restore_tabs` which decides whether any of it is read. A second file
would be a second thing to find, to quarantine when it is corrupt, and to keep
in step with the first. `core/settings.py` already does all three, and it
normalises the structure on the way out of the file so nothing here has to
check a type.

WHAT IS RECORDED. Per window: its geometry, the screen it was on, which tab was
in front, and its tabs as `{path, page, zoom, fit_mode}`.

THREE THINGS ARE DELIBERATELY NOT RECORDED, and each is a decision rather than
an omission.

1. **A TAB WITH NO PATH.** An untitled or merged document exists only in
   memory. Writing it down means serialising the document itself into a cache
   directory, which brings a disk-space policy, a cleanup policy and a restore
   path that can fail on a corrupt cache, all for a document the user has not
   named yet. It is skipped silently, on the way out and on the way back in.

2. **UNSAVED ANNOTATION STATE.** The markup model already round-trips through
   the saved PDF (`_flush_annotations` / `_load_saved_annotations` in
   ui/document_view.py). An autosave-to-cache scheme here would be a second
   copy of that machinery, aimed at one case: a crash with unsaved markup. That
   case is crash recovery, it wants its own design and its own prompt on the
   way back in ("recover the changes from the run that ended unexpectedly?"),
   and quietly folding it into session restore would mean a restored tab could
   come up dirty with no way to say where those changes came from. Restored
   tabs come up exactly as the file on disk is.

3. **THE ORDER WINDOWS WERE ACTIVATED IN.** The registry's order is a live
   thing about focus, not a property of the arrangement. Windows come back in
   the order they were recorded.

FILES THAT HAVE GONE ARE SKIPPED, SILENTLY, WITH ONE LINE IN THE STATUS BAR.
A mapped drive that is offline is the ordinary case on the machines this app
runs on, and it takes every tab with it at once. Eight modal dialogs before the
window is usable is the failure mode worth designing against, so the existence
check happens here, before any tab is made, and the whole answer is one line:
"3 files from the last session could not be found."

TABS COME BACK LAZY. `DocumentView.stage_path` makes a tab that is named after
its file and has not read it; `ensure_loaded`, driven by `set_active`, opens it
the first time it is looked at. Eight A1 drawings read at once before the window
paints would make startup feel broken, and opening big drawings fast is the
whole pitch. What that costs is that a lazy tab is a genuinely different code
path: see `is_empty()` in ui/document_view.py and its call sites.

WHEN IT IS WRITTEN. On every window close and on `aboutToQuit`. The rule that
decides whether a closing window stays in the record is whether the application
is going down with it:

  - closing one window of three while the app carries on REMOVES that window
    from the session, because it is not open any more and the user closed it on
    purpose;
  - closing a window as part of a shutdown (the Quit menu, a Windows session
    end, or simply being the last one) KEEPS it, because by `aboutToQuit` its
    views have been torn down and there is nothing left to read.

`SessionRecorder` holds the difference. Windows update their own entry in it,
keyed by `MainWindow.window_id()`, and the write is the whole map.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication

from core.settings import settings

# A restored window has to be reachable with the mouse. A saved geometry is
# accepted when at least this much of it lands on some screen's available area,
# which covers the monitor that has been unplugged and the laptop that came
# back from a dock at a different resolution.
MIN_VISIBLE_PX = 120


def _screen_name(window) -> str | None:
    try:
        screen = window.screen()
    except (AttributeError, RuntimeError):
        return None
    return screen.name() if screen is not None else None


def capture_window(window) -> dict | None:
    """One window as a record, or None when it holds nothing worth reopening.

    Read off the live widgets, so it has to run BEFORE the views are torn down.
    `MainWindow.closeEvent` calls it through `SessionRecorder.note_closing` for
    exactly that reason.
    """
    try:
        area = window.document_area()
    except (AttributeError, RuntimeError):
        return None

    tabs = []
    current = 0
    for index, view in enumerate(area.views()):
        path = view.document_path()
        if not path:
            continue                  # untitled or merged: memory only
        if index == area.current_index():
            current = len(tabs)
        tabs.append({
            "path": path,
            "page": view.current_page(),
            "zoom": view.view_scale(),
            "fit_mode": view.fit_mode(),
        })
    if not tabs:
        return None

    geometry = window.normalGeometry()
    if geometry.width() <= 0 or geometry.height() <= 0:
        geometry = window.geometry()
    return {
        "geometry": [geometry.x(), geometry.y(),
                     geometry.width(), geometry.height()],
        "screen": _screen_name(window),
        "current": current,
        "tabs": tabs,
    }


class SessionRecorder:
    """The session as it stands, and getting it onto disk.

    One per application. Windows write their own entry into it and the whole
    map is what lands in the settings store, in the order the entries were
    first made, so a window that has been open all along keeps its place.
    """

    def __init__(self, registry=None):
        self._registry = registry
        self._records: dict = {}

    def _windows(self) -> list:
        registry = self._registry
        if registry is None:
            from ui.window_registry import WindowRegistry
            registry = WindowRegistry.instance()
        return registry.windows()

    def refresh(self, window) -> None:
        """Re-read one window. A window holding nothing drops out of the record."""
        try:
            key = window.window_id()
        except (AttributeError, RuntimeError):
            return
        record = capture_window(window)
        if record is None:
            self._records.pop(key, None)
        else:
            self._records[key] = record

    def note_closing(self, window, shutting_down: bool) -> None:
        """A window is going. Called from `closeEvent`, before the teardown.

        `shutting_down` is what decides whether it stays in the session. See
        the module docstring: a window closed while the app carries on is a
        window the user finished with, and a window closed on the way out is
        still part of the arrangement being remembered.
        """
        if shutting_down:
            self.refresh(window)
        else:
            try:
                self._records.pop(window.window_id(), None)
            except (AttributeError, RuntimeError):
                pass

    def save(self) -> None:
        """Re-read every open window and write the session. Never raises.

        Runs on a close path and on `aboutToQuit`, so a failure here has to
        cost the session and nothing else. The store's own write is already
        silent on a full disk; this catches the rest.
        """
        try:
            for window in self._windows():
                self.refresh(window)
            settings().session.windows = list(self._records.values())
        except Exception as err:      # pragma: no cover - belt and braces
            print(f"Could not record the session: {err}")

    def forget(self) -> None:
        """Drop everything, in memory and on disk."""
        self._records = {}
        try:
            settings().session.windows = []
        except Exception:             # pragma: no cover
            pass


_RECORDER: SessionRecorder | None = None


def recorder() -> SessionRecorder:
    """The one recorder the app writes through."""
    global _RECORDER
    if _RECORDER is None:
        _RECORDER = SessionRecorder()
    return _RECORDER


def set_recorder(new: SessionRecorder | None) -> SessionRecorder | None:
    """Swap the app-wide recorder (tests point it at their own registry).

    Returns the one that was in place, so a caller can put it back.
    """
    global _RECORDER
    previous = _RECORDER
    _RECORDER = new
    return previous


def save_session() -> None:
    """Write the session now. Wired to `aboutToQuit` in main.py."""
    recorder().save()


# ---------------------------------------------------------------------------
# Coming back
# ---------------------------------------------------------------------------
def _geometry_is_reachable(rect: QRect) -> bool:
    """Whether enough of this rectangle lands on a screen to click on.

    A monitor that has been unplugged leaves a saved position out in space
    where the window cannot be dragged back from, which is worse than letting
    Qt place it.
    """
    for screen in QGuiApplication.screens():
        overlap = screen.availableGeometry().intersected(rect)
        if overlap.width() >= MIN_VISIBLE_PX and overlap.height() >= MIN_VISIBLE_PX:
            return True
    return False


def _apply_geometry(window, record: dict) -> bool:
    """Put a window back where it was, if that is still somewhere real."""
    geometry = record.get("geometry")
    if not geometry:
        return False
    rect = QRect(*geometry)
    if not _geometry_is_reachable(rect):
        return False
    name = record.get("screen")
    if name:
        for screen in QGuiApplication.screens():
            if screen.name() == name:
                try:
                    window.setScreen(screen)
                except (AttributeError, RuntimeError, TypeError):
                    pass          # not every platform plugin will place a window
                break
    window.setGeometry(rect)
    return True


def _restorable(record: dict) -> tuple:
    """Split one window's tabs into (found, missing count).

    The existence check is here rather than at open time on purpose: a mapped
    drive that is offline takes every tab with it at once, and the answer to
    that has to be one status line rather than one dialog per file.
    """
    found = []
    missing = 0
    for tab in record.get("tabs") or []:
        try:
            exists = os.path.isfile(tab["path"])
        except (OSError, ValueError):
            exists = False
        if exists:
            found.append(tab)
        else:
            missing += 1
    return found, missing


def _missing_line(count: int) -> str:
    if count == 1:
        return "1 file from the last session could not be found."
    return f"{count} files from the last session could not be found."


def _fill_window(window, record: dict, tabs: list) -> None:
    """Give a window the tabs from one record, all of them lazy.

    Exactly one file is read here, the one that was in front. Bringing a tab
    forward is what opens it (`set_active` -> `ensure_loaded`), and
    `ensure_loaded` is called by hand afterwards for the case Qt emits nothing
    for: the front tab is already index 0 and `setCurrentIndex(0)` on a bar
    that is already there is a no-op.
    """
    area = window.document_area()
    for tab in tabs:
        view = window.tab_for_restore()
        view.stage_path(tab["path"], page=tab.get("page") or 0,
                        zoom=tab.get("zoom") or 0.0,
                        fit_mode=tab.get("fit_mode"))

    current = record.get("current") or 0
    area.set_current_index(min(max(current, 0), area.count() - 1))
    front = area.current_view()
    if front is not None:
        front.ensure_loaded()


def restore_session(first_window, registry=None) -> int:
    """Rebuild last run's windows and tabs. Returns how many files had gone.

    `first_window` is the window main.py has already made; the first record
    fills it and every record after that gets a window of its own. Nothing here
    reads a PDF: every tab is staged and opens when it is first looked at.

    Returns 0 and touches nothing when there is no session, when every file in
    it has gone, or when `startup.restore_tabs` is off. The caller decides what
    to do with the count; main.py puts it in the status bar of the first window
    that came back.
    """
    if registry is None:
        from ui.window_registry import WindowRegistry
        registry = WindowRegistry.instance()

    records = settings().session.windows
    missing = 0
    usable = []
    for record in records:
        tabs, gone = _restorable(record)
        missing += gone
        if tabs:
            usable.append((record, tabs))
    if not usable:
        return missing

    first_record, first_tabs = usable[0]
    _apply_geometry(first_window, first_record)
    _fill_window(first_window, first_record, first_tabs)

    for record, tabs in usable[1:]:
        window = registry.create_window(show=False)
        if not _apply_geometry(window, record):
            window.resize(first_window.size())
        _fill_window(window, record, tabs)
        window.show()

    return missing


def should_restore(files: list | None = None, combine: bool = False) -> bool:
    """Whether THIS launch reopens last run's tabs. Two conditions, both hard.

    The setting has to be on, and the launch has to have carried nothing. A PDF
    double-clicked in Explorer is a request to read that file, and burying it
    under eight restored tabs is not what anybody meant by it; the same goes
    for a `--combine` verb, which is a request to merge a named set.

    Only ever asked in the primary process: `main.py` forwards a second launch
    and exits before it gets here.
    """
    if files or combine:
        return False
    return bool(settings().startup.restore_tabs)


def restore_on_launch(first_window, registry=None, files: list | None = None,
                      combine: bool = False) -> int:
    """Restore if this launch should, and say so in the status bar.

    The one entry point main.py calls. Separate from `restore_session` so the
    launch rule and the status line live in one place and the mechanism can be
    driven in a test without either.
    """
    if not should_restore(files, combine):
        return 0
    missing = restore_session(first_window, registry)
    if missing:
        first_window.show_status(_missing_line(missing))
    return missing
