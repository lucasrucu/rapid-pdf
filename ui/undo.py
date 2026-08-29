"""One undo stack per WINDOW, and the per-document dirty state that hangs off it.

Phase 5 of docs/tabs-plan.md, and the part of it the plan called genuinely
uncertain. Read this before touching anything that pushes a command.

WHY THE STACK MOVED OFF THE CANVAS. Dragging a page from one tab into another
is ONE user action with TWO document-level effects: the page leaves A and
arrives in B. Split across two stacks there is no ordering that survives an
undo. Undo on B's stack re-inserts the page into A while A's stack still thinks
nothing happened, and the user is looking at a duplicate. Undo on A's first has
the mirror problem. QUndoGroup does not help: it only decides which of several
stacks is active, and the two halves still live in different histories.

One stack per window makes the move an ordinary single command with nothing to
pair up. The cost is that undo can now change a tab you are not looking at, and
the mitigation is that a command SWITCHES TO the document it is about to touch
before touching it (see `_PageCommand._focus`), so the user watches the thing
being undone rather than finding out later.

WHY DIRTY CANNOT COME OFF QUndoStack.isClean() ANY MORE. `setClean` marks one
index on one stack, and one stack now carries three documents' worth of
commands. Saving B would clear the modified marker on A. So each document keeps
a REVISION COUNTER instead: every command that touches it bumps the counter on
redo and drops it on undo, a save records the counter it was saved at, and the
document is dirty whenever the two differ. That is exactly QUndoStack's own
clean-index rule, scoped to one document rather than one stack.

The one case that rule gets wrong on its own is the branch: save at revision 3,
undo to 2, then make a new edit and the counter is 3 again while the content is
different. QUndoStack solves it by invalidating its clean index when the redo
branch is dropped; `push` below does the same thing, by telling every document
the stack has touched to give up a save marker that lived in the branch about
to disappear.
"""

import weakref

from PySide6.QtGui import QUndoStack


class WindowUndoStack(QUndoStack):
    """The window's single history, plus a note of which documents are in it.

    The note is what lets a view leaving the window (a tear-off, or a document
    being closed and the view reused) take its commands out of reach. There is
    no selective removal in QUndoStack, so the honest move is to drop the whole
    history, and `drop_history_for` only does that when the departing view is
    actually named in it. A freshly opened tab with no edits, which is the
    common case, costs nothing.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # Weak, because the stack outlives individual views and must never be
        # the reason one stays alive.
        self._touched: list = []

    # -- who is in here -----------------------------------------------------

    def note_touched(self, view):
        if view is None:
            return
        if not any(ref() is view for ref in self._touched):
            self._touched.append(weakref.ref(view))

    def tracked_views(self) -> list:
        """Live views this stack holds commands for, dead references dropped."""
        alive = []
        kept = []
        for ref in self._touched:
            view = ref()
            if view is not None:
                alive.append(view)
                kept.append(ref)
        self._touched = kept
        return alive

    def touches(self, view) -> bool:
        return any(v is view for v in self.tracked_views())

    def drop_history_for(self, view) -> bool:
        """Forget everything, but only if `view` is named in it. True if cleared.

        Called when a view leaves this window alive, and when a canvas is
        handed a different document. Either way the commands still sitting here
        would replay against something that is no longer in this window, or no
        longer exists at all.
        """
        if not self.touches(view):
            return False
        self.clear()
        return True

    # -- QUndoStack ---------------------------------------------------------

    def push(self, command):
        """Push, invalidating any save marker that lived in a dropped branch.

        `index() < count()` means the user has undone something and is now
        making a new edit, so QUndoStack is about to throw the redo branch
        away. Any document whose last save happened inside that branch can
        never get back to it, so its marker is retired here rather than
        silently reporting a document clean that is not.
        """
        if self.index() < self.count():
            for view in self.tracked_views():
                marker = getattr(view, "note_branch_dropped", None)
                if marker is not None:
                    marker()
        for view in _affected(command):
            self.note_touched(view)
        super().push(command)

    def clear(self):
        self._touched = []
        super().clear()


def _affected(command) -> tuple:
    """The DocumentViews a command says it touches, or none.

    Commands opt in by defining `affected_views()`. Anything that does not is
    still perfectly pushable; it just contributes nothing to the bookkeeping,
    which is what a command with no document behind it (a test double) wants.
    """
    getter = getattr(command, "affected_views", None)
    if getter is None:
        return ()
    try:
        return tuple(v for v in getter() if v is not None)
    except Exception:
        return ()
