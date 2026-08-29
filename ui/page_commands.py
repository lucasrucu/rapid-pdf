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
    shift_map_after_insert, shift_map_after_reorder,
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

    def affected_views(self) -> tuple:
        """The documents this command dirties. One, unless it is a transfer.

        The window's shared stack reads this to know whose revision counter a
        command moves, and whose save marker a dropped redo branch invalidates.
        See ui/undo.py.
        """
        return (self._win,)

    def _focus(self):
        """Bring the affected documents to the front before changing them.

        The mitigation for the one real cost of a per-window undo stack: Ctrl+Z
        can now reach a tab you are not looking at, so the tab comes to you
        rather than changing behind your back. Last one wins, which for a
        transfer means the destination.
        """
        for view in self.affected_views():
            view.request_activation()

    def redo(self):
        self._focus()
        for view in self.affected_views():
            view.note_revision(1)
        self._apply()
        self._sync()

    def undo(self):
        self._focus()
        for view in self.affected_views():
            view.note_revision(-1)
        self._revert()
        self._sync()

    def _sync(self):
        for view in self.affected_views():
            view.after_page_structure_change()

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


class TransferPagesCommand(_PageCommand):
    """Move (or copy) pages out of one open document and into another.

    ONE command, touching TWO documents. That is the whole reason the undo
    stack moved to the window in phase 5: split across two stacks there is no
    ordering that undoes this without leaving a duplicate behind. See ui/undo.py.

    THREE THINGS TRAVEL, AND THEY TRAVEL BY DIFFERENT ROUTES.

      - The page itself, its annotations, its links to the outside world, its
        fonts, size and rotation: `PDFDocument.transfer_pages_from`, which is
        insert_pdf and needs no help.
      - Unsaved rapid-pdf markup: as JSON, through `export_page_markup` on the
        source canvas and `build_page_markup` on the destination. It is Qt
        scene items, not annotations in the file, so insert_pdf cannot see it
        and the page would arrive blank.
      - Lifted images: baked into the source page's CONTENT first
        (`bake_image_items`), because the JSON route deliberately skips them
        and they would otherwise be the one kind of markup that vanishes.

    What does NOT travel is reported, not fixed: internal GOTO links whose
    target is outside the moved pages, and layers. PyMuPDF drops both in
    silence and there is no generic repair, so the view says so once in the
    status bar. `warnings()` is where that line comes from.

    THE MARKUP IS REBUILT ONCE, at construction, and the same item objects are
    reused by every redo. That mirrors DeletePagesCommand's stash: an undo has
    to give back the objects the user had, carrying whatever style they had
    picked, not fresh copies of them.
    """

    def __init__(self, dest_view, src_view, rows: list, at: int,
                 copy: bool = False):
        self._src = src_view
        self._copy = bool(copy)
        self._rows = sorted({int(r) for r in rows
                             if 0 <= int(r) < src_view.page_count()})
        count = len(self._rows)
        verb = "Copy" if self._copy else "Move"
        noun = "page" if count == 1 else f"{count} pages"
        super().__init__(dest_view, f"{verb} {noun} between documents")
        self._dest = dest_view
        self._src_doc = src_view._doc
        self._src_canvas = src_view._canvas
        self._at = max(0, min(int(at), dest_view.page_count()))
        self._count = count
        # Read BEFORE anything moves: the report is about the source pages as
        # they stand now, and after a move they are not there to be asked.
        self._warnings = self._src_doc.transfer_report(self._rows)
        # Bake lifted images into the source page content, so they survive a
        # route that deliberately cannot carry them. Done once, and NOT undone:
        # baking is what a save would have done anyway, and the image is still
        # liftable on both sides afterwards. For a move it makes no difference
        # to the source (the stash is taken after the bake, so an undo gets the
        # baked page back); for a COPY it is a real change to a document this
        # command otherwise leaves alone, so the source is marked dirty for it.
        baked = self._src_canvas.bake_image_items(self._src_doc, self._rows)
        if baked and self._copy:
            src_view._mark_dirty()
        # Markup, out of the source and rebuilt as the destination's items.
        exported = self._src_canvas.export_page_markup(self._rows)
        self._carried = self._dest._canvas.build_page_markup(exported, self._at)
        # A move needs the pages back on undo, and after the delete the source
        # no longer has them. Same stash discipline as DeletePagesCommand.
        self._stash = None if self._copy else self._src_doc.extract_pages(self._rows)

        dest_canvas = self._dest._canvas
        self._dest_before = dest_canvas.snapshot_page_annotations()
        self._dest_before_page = dest_canvas.current_page()
        self._dest_after = shift_map_after_insert(self._dest_before, self._at,
                                                  self._count)
        self._dest_after.update(self._carried)
        self._dest_after_page = self._at

        self._src_before = self._src_canvas.snapshot_page_annotations()
        self._src_before_page = self._src_canvas.current_page()
        if self._copy:
            self._src_after = dict(self._src_before)
            self._src_after_page = self._src_before_page
        else:
            self._src_after = shift_map_after_delete(self._src_before, self._rows)
            self._src_after_page = page_after_delete(self._src_before_page,
                                                     self._rows)

    def affected_views(self) -> tuple:
        """Whose dirty state and whose tab this command moves.

        Source FIRST and destination LAST, so `_focus` ends on the destination
        and the user is looking at where the pages landed. A copy names only
        the destination: it takes nothing out of the source, so marking that
        document modified would be a lie the user then has to save.
        """
        return (self._dest,) if self._copy else (self._src, self._dest)

    def warnings(self) -> dict:
        """What this move loses or renames, for the status bar to say once."""
        return dict(self._warnings)

    def rows(self) -> list:
        return list(self._rows)

    def _apply(self):
        self._dest._doc.transfer_pages_from(self._src_doc, self._rows, self._at)
        if not self._copy:
            self._src_doc.delete_pages(self._rows)
            self._src_doc.note_pages_sent(self._count, self._dest.transfer_label())
            self._dest._doc.note_pages_taken(self._count, self._src.transfer_label())
            self._src_canvas.restore_page_annotations(self._src_after,
                                                      self._src_after_page)
        self._dest._canvas.restore_page_annotations(self._dest_after,
                                                    self._dest_after_page)
        self._dest._pending_page_selection = list(
            range(self._at, self._at + self._count))

    def _revert(self):
        # The destination gives the pages back first, then the source takes
        # them. Either order works on the documents, but this one leaves the
        # canvases in the state the maps below describe.
        self._dest._doc.delete_pages(
            list(range(self._at, self._at + self._count)))
        if not self._copy:
            self._src_doc.restore_pages(self._stash, self._rows)
            self._src_doc.forget_last_transfer(sent=True)
            self._dest._doc.forget_last_transfer(sent=False)
            self._src_canvas.restore_page_annotations(self._src_before,
                                                      self._src_before_page)
            self._src._pending_page_selection = list(self._rows)
        self._dest._canvas.restore_page_annotations(self._dest_before,
                                                    self._dest_before_page)

    def __del__(self):
        # Dropped from the stack: the stashed pages are unreachable now.
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
