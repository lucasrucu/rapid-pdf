"""Tabs that have been closed, and the way back to them. Ctrl+Shift+T.

Undo covers edits inside a document. Closing a tab is not an edit, so it has
never been on the undo stack and never should be: undoing a close would have to
mean re-opening a file, which is not a command with an inverse, and it would sit
on the WINDOW's shared stack (see ui/undo.py) where every other entry is a page
or a markup change. This is the separate history that closing gets instead, and
it is the one every browser has taught people to expect.

THE STACK IS GLOBAL, NOT PER WINDOW, AND THAT IS THE ONE REAL DECISION HERE.
A stack living on a MainWindow dies with it: `WindowRegistry.unregister` drops
the last reference to a window as it closes, and a window closing is exactly the
moment several tabs go at once. A per-window stack would therefore lose its
entries at the one time there is most to remember, and a tab closed in a window
that has since been closed would be unrecoverable, which is the opposite of what
this feature is for. Global also matches what browsers do: the reopen key works
in whatever window you happen to be in, not only the one the tab came from.

The window that fires the shortcut is where the tab comes back, because the
shortcut is window scoped (see MainWindow._add_action). So the STACK is app
wide and the DESTINATION is local, which is the same split the session recorder
uses.

WHAT AN ENTRY HOLDS. The same five keys ui/session.py writes per tab: `path`,
`page`, `zoom`, `raster_scale` and `fit_mode`. That is not a coincidence worth
removing, it is the point: session restore already proved those five are enough
to put a document back the way it was, and reopening a tab is the same job over
a shorter interval. `zoom` and `raster_scale` travel together because a view
scale is measured against the raster scale, so one without the other is the
wrong number (see DocumentView.raster_scale).

A view with no path is skipped, the way `capture_window` skips it. An untitled
or merged document exists only in memory, so there is no file to reopen and
recording it would promise something that cannot be delivered.

WHAT IS DELIBERATELY NOT HELD. Scroll position within a page, because nothing in
this codebase can answer for it: there is no scroll getter on DocumentView or on
the canvas, and the whole vocabulary the app has for "where you were" is the page
index plus the fit mode. Those are recorded. Also not held: unsaved markup, for
the reason ui/session.py gives at length. A reopened tab comes up as the file on
disk is, and the save prompt has already run by the time anything lands here.

BOUNDED AT TEN. A stack that grows without limit is a list of paths for every
tab closed in a session, which is memory nobody asked for and a history nobody
walks back that far. The oldest entry falls off the bottom.
"""

from __future__ import annotations

# How many closed tabs are remembered. Ten is what Chrome and Firefox keep, and
# the value is here rather than inline so a test can build a stack with its own.
MAX_ENTRIES = 10


def capture_view(view) -> dict | None:
    """One view as a reopen entry, or None when there is nothing to record.

    Read off the live widgets, so it has to run BEFORE the document is closed.
    `DocumentView.close_document` clears the path and resets the page, so a
    capture taken afterwards would be a row of defaults pointing at nothing.
    Every caller records first and pushes only once the close has actually
    happened, because a cancelled save prompt is not a close.

    Never raises. A view part way through its own teardown answers some of
    these with a dead C++ object, and a reopen entry is not worth taking a
    close path down with it.
    """
    try:
        path = view.document_path()
        if not path:
            return None               # untitled or merged: memory only
        return {
            "path": path,
            "page": view.current_page(),
            "zoom": view.view_scale(),
            # Measured against the zoom above, so the pair travels together.
            "raster_scale": view.raster_scale(),
            "fit_mode": view.fit_mode(),
        }
    except (AttributeError, RuntimeError):
        return None


class ReopenStack:
    """The closed tabs, newest first, capped at `limit`.

    Deliberately not a QObject and deliberately holding no widget references.
    An entry is five plain values, so it stays valid after the window it came
    from has been destroyed, which is the whole reason this is not per window.
    """

    def __init__(self, limit: int = MAX_ENTRIES):
        self._limit = max(1, int(limit))
        # Newest LAST, so `push` and `pop` are both list operations at the end
        # and the discard of the oldest is a slice off the front.
        self._entries: list = []

    def push(self, entry: dict | None) -> bool:
        """Remember a closed tab. False when there was nothing worth keeping.

        Takes None so callers can hand over the result of `capture_view`
        without testing it first: a view with no path is a normal case, not an
        error, and every call site would otherwise repeat the same guard.
        """
        if not entry or not entry.get("path"):
            return False
        self._entries.append(dict(entry))
        if len(self._entries) > self._limit:
            del self._entries[:len(self._entries) - self._limit]
        return True

    def pop(self) -> dict | None:
        """The most recently closed tab, removed. None when there are none.

        Popping is what makes repeated presses walk back through the closures
        rather than reopening the same tab over and over. An entry whose file
        has gone is popped and thrown away by the caller, which is why this
        does not touch the filesystem: whether a path is still openable is a
        question for the window that is about to try it.
        """
        if not self._entries:
            return None
        return self._entries.pop()

    def peek(self) -> dict | None:
        """The next entry without removing it. For tests and for a menu label."""
        return dict(self._entries[-1]) if self._entries else None

    def entries(self) -> list:
        """Everything held, oldest first. A copy: callers do not edit history."""
        return [dict(entry) for entry in self._entries]

    def clear(self) -> None:
        self._entries = []

    def __len__(self) -> int:
        return len(self._entries)


_STACK: ReopenStack | None = None


def reopen_stack() -> ReopenStack:
    """The one stack every window records into and reopens from."""
    global _STACK
    if _STACK is None:
        _STACK = ReopenStack()
    return _STACK


def set_reopen_stack(new: ReopenStack | None) -> ReopenStack | None:
    """Swap the app-wide stack (tests put in a fresh one, or a smaller one).

    Returns the one that was in place, so a caller can put it back. Same door
    `ui.session.set_recorder` opens, for the same reason: a module level
    singleton with no way to replace it makes every test that touches it
    depend on the order the suite runs in.
    """
    global _STACK
    previous = _STACK
    _STACK = new
    return previous


def record_closed_view(view) -> bool:
    """Capture a view and push it, in one call. What the close paths use."""
    return reopen_stack().push(capture_view(view))
