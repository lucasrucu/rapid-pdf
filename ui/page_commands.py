"""Undoable page-structure edits (delete, reorder) for the left page panel.

The canvas already owns a QUndoStack for item-level edits (draw, move, resize,
restyle). Page delete and reorder used to CLEAR that stack, because the canvas
files its markup by page index and a structural edit renumbers every page out
from under the commands still sitting in it. Deleting pages was therefore a
one-way door, which is fine for a deliberate trip through the Organizer and not
fine at all for a Delete key in the thumbnail strip.

These commands make both edits undoable by pairing the document change with a
snapshot of the whole page-to-markup map. Undo puts the document AND the map
back exactly as they were, so an item-level command underneath replays against
the numbering it was recorded with, and the stack stays coherent either way.

A delete keeps the removed pages alive in a stash document (see
PDFDocument.extract_pages) until the command itself is dropped, which is what
undo reinserts.
"""

from PySide6.QtGui import QUndoCommand

from core.page_ops import (
    invert_order, page_after_delete, shift_map_after_delete,
    shift_map_after_reorder,
)


class _PageCommand(QUndoCommand):
    """Shared plumbing: apply, then let the window re-sync everything around it.

    Unlike the canvas's _Command, the edit is NOT applied at construction time,
    so the first redo() that QUndoStack.push() fires is the one that does the
    work. Everything a page edit has to touch (thumbnails, status bar, dirty
    flag) hangs off one host callback rather than being repeated per command.
    """

    def __init__(self, window, text: str):
        super().__init__(text)
        self._win = window
        self._canvas = window._canvas
        self._doc = window._doc

    def redo(self):
        self._apply()
        self._win.after_page_structure_change()

    def undo(self):
        self._revert()
        self._win.after_page_structure_change()

    def _apply(self): ...
    def _revert(self): ...


class DeletePagesCommand(_PageCommand):
    """Remove a selection of pages, reversibly."""

    def __init__(self, window, rows: list):
        self._rows = sorted({int(r) for r in rows})
        label = "Delete page" if len(self._rows) == 1 else f"Delete {len(self._rows)} pages"
        super().__init__(window, label)
        canvas = self._canvas
        self._before_map = canvas.snapshot_page_annotations()
        self._before_page = canvas.current_page()
        self._after_map = shift_map_after_delete(self._before_map, self._rows)
        self._after_page = page_after_delete(self._before_page, self._rows)
        # Copy the pages out BEFORE anything deletes them. The stash lives as
        # long as this command does, which is as long as the undo is offered.
        self._stash = self._doc.extract_pages(self._rows)

    def rows(self) -> list:
        return list(self._rows)

    def _apply(self):
        self._doc.delete_pages(self._rows)
        self._canvas.restore_page_annotations(self._after_map, self._after_page)

    def _revert(self):
        self._doc.restore_pages(self._stash, self._rows)
        self._canvas.restore_page_annotations(self._before_map, self._before_page)

    def __del__(self):
        # Dropped from the stack (cleared, or overwritten by a new edit): the
        # stashed pages are no longer reachable, so let PyMuPDF have the memory.
        try:
            if self._stash is not None:
                self._stash.close()
        except Exception:
            pass


class ReorderPagesCommand(_PageCommand):
    """Apply a page permutation, reversibly."""

    def __init__(self, window, order: list, moved: int = 0):
        label = "Move page" if moved == 1 else "Move pages"
        super().__init__(window, label)
        self._order = list(order)
        self._inverse = invert_order(self._order)
        canvas = self._canvas
        self._before_map = canvas.snapshot_page_annotations()
        self._before_page = canvas.current_page()
        self._after_map = shift_map_after_reorder(self._before_map, self._order)
        self._after_page = (self._order.index(self._before_page)
                            if self._before_page in self._order else 0)

    def order(self) -> list:
        return list(self._order)

    def _apply(self):
        self._doc.reorder(self._order)
        self._canvas.restore_page_annotations(self._after_map, self._after_page)

    def _revert(self):
        self._doc.reorder(self._inverse)
        self._canvas.restore_page_annotations(self._before_map, self._before_page)
