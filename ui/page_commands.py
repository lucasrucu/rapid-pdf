"""Undoable page-structure edits (delete, insert, reorder, rotate), for BOTH panels.

The canvas already owns a QUndoStack for item-level edits (draw, move, resize,
restyle). Page delete and reorder used to CLEAR that stack, because the canvas
files its markup by page index and a structural edit renumbers every page out
from under the commands still sitting in it. Deleting pages was therefore a
one-way door.

The left thumbnail strip came through here first and the Organizer did not, so
for a while the SAME Delete key was reversible in one panel and, in the other,
took the whole window's history with it (annotation edits included) with nothing
on screen to say which panel had the keyboard. The Organizer used to apply its
own edit and tell the view afterwards, which is what made that unfixable at the
view: by the time it heard, the pages and the page order to put back were gone.
Both panels now ASK, and the ask lands on the same commands below. There is no
second way to delete or reorder a page.

"+ Add Pages" was the last edit still applied in the Organizer, and it had the
same shape of bug pointing the other way: it rebuilt the GRID and never told
the Editor's thumbnail strip, so a merged document showed two page counts at
once. It asks now too. See InsertPagesCommand.

These commands make both edits undoable by pairing the document change with a
snapshot of the whole page-to-markup map. Undo puts the document AND the map
back exactly as they were, so an item-level command underneath replays against
the numbering it was recorded with, and the stack stays coherent either way.

A delete keeps the removed pages alive in a stash document (see
PDFDocument.extract_pages) until the command itself is dropped, which is what
undo reinserts.
"""

import fitz

from PySide6.QtCore import QLineF, QPointF, QRectF
from PySide6.QtGui import QUndoCommand

from core.page_ops import (
    ROTATE_180, ROTATE_CCW, ROTATE_CW, invert_order, normalize_rotation,
    page_after_delete, rotate_point, rotate_rect, rotate_rect_upright,
    rotation_after, shift_map_after_delete, shift_map_after_insert,
    shift_map_after_reorder,
)
from ui.canvas import (
    ImageAnnotationItem, LineAnnotationItem, TextAnnotationItem,
    geometry_restore, geometry_snapshot,
)

# THE ROTATE MENU, DEFINED ONCE. Both page panels build their entries from
# this, so the strip's right-click menu and the Organizer's cannot drift apart
# in wording, order or direction. The tab in a label is Qt's separator for the
# right-hand shortcut column in a QMenu.
ROTATE_ACTIONS = (
    ("Rotate Right 90°\tCtrl+R", ROTATE_CW),
    ("Rotate Left 90°\tCtrl+Shift+R", ROTATE_CCW),
    ("Rotate 180°", ROTATE_180),
)

# THE KEYS, AND WHY THESE TWO. Ctrl+R and Ctrl+Shift+R were both free: the
# whole bound set is Ctrl+O/S/W/T/Q/G/B/D/F/C/V/A/Z/Y, Ctrl+[ and Ctrl+],
# Ctrl+comma, Ctrl+PgUp/PgDn, Ctrl+Tab, the Organizer's Ctrl+plus/minus/0, the
# Shift pairs Ctrl+Shift+N/S/T/Tab, Alt+Space, and the five bare tool letters
# v h r l t. Ctrl+R is what Preview and every scan tool use for "turn it
# right", and the bare `r` that picks the Rectangle tool is a different
# sequence, so the two do not collide. There is deliberately no key for 180:
# it is Ctrl+R twice, and the free combinations left for it (Ctrl+Alt+R) are
# AltGr on the international layouts this app ships to.
ROTATE_SHORTCUTS = (
    ("Ctrl+R", ROTATE_CW),
    ("Ctrl+Shift+R", ROTATE_CCW),
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


class InsertPagesCommand(_PageCommand):
    """Merge pages from other PDF files into this document, reversibly.

    The Organizer's "+ Add Pages". It used to insert straight into the live
    document and then ask the host to rebuild, and the host rebuilt the
    ORGANIZER and nothing else: the Editor's thumbnail strip kept the item
    count it had before the merge, so a two page document showed one thumbnail
    while the status bar underneath it read "page 2 of 2". Coming through here
    fixes that by construction, because every page command re-syncs BOTH panels
    through DocumentView.after_page_structure_change.

    It fixes the quieter half too. Markup is filed by page index and an insert
    renumbers every page from the insertion point down; nothing shifted that
    map, so adding pages anywhere but the end left the existing markup pointing
    at the wrong pages.

    THE SOURCE PAGES ARE READ ONCE, at construction, into an in-memory stash,
    and every redo inserts from that. Same discipline as DeletePagesCommand's
    stash, for the same reason and one more: re-reading the files on each redo
    would quietly pick up whatever they say by then.

    A file that will not open is not a reason to lose the ones that will, so
    the stash takes what it can and the rest come back through `errors()` for
    the view to report once. If NOTHING opened, the count is zero and the view
    never pushes the command.
    """

    def __init__(self, window, paths: list, at: int):
        super().__init__(window, "Insert pages")
        self._at = max(0, min(int(at), window.page_count()))
        self._errors: list[str] = []
        self._stash = fitz.open()
        for path in paths:
            try:
                src = fitz.open(str(path))
            except Exception as e:
                self._errors.append(f"{path}: {e}")
                continue
            try:
                self._stash.insert_pdf(src)
            except Exception as e:
                self._errors.append(f"{path}: {e}")
            finally:
                src.close()
        self._count = len(self._stash)
        self.setText("Insert page" if self._count == 1
                     else f"Insert {self._count} pages")
        canvas = self._canvas
        self._before_map = canvas.snapshot_page_annotations()
        self._before_page = canvas.current_page()
        self._after_map = shift_map_after_insert(self._before_map, self._at,
                                                 self._count)
        self._after_page = self._at
        # A merge makes a document that no longer matches the file it came
        # from, so the path goes and the next save is a Save As. Undoing puts
        # the document back in step with that file, so the path comes back.
        self._path_before = self._doc.path

    def page_count(self) -> int:
        """How many pages actually opened. Zero means don't push this."""
        return self._count

    def errors(self) -> list:
        """One line per source file that could not be read, in words."""
        return list(self._errors)

    def rows(self) -> list:
        """Where the inserted pages sit once applied."""
        return list(range(self._at, self._at + self._count))

    def _apply(self):
        self._doc.insert_document(self._stash, self._at)
        self._canvas.restore_page_annotations(self._after_map, self._after_page)
        self._win._mark_untitled()
        self._win._pending_page_selection = self.rows()

    def _revert(self):
        self._doc.delete_pages(self.rows())
        self._canvas.restore_page_annotations(self._before_map, self._before_page)
        self._doc.path = self._path_before

    def __del__(self):
        # Dropped from the stack (cleared, or overwritten by a new edit), or
        # never pushed at all: the stash is unreachable now.
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


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def _rotate_item(item, delta: int, width: float, height: float):
    """Move one canvas item to where the page turning under it puts its content.

    `width` and `height` are the page's SCENE box before the turn, which is the
    visible page in points times the document's frozen render scale, because
    that is the space every annotation item is stored in (see
    PDFDocument.render_scale).

    Three behaviours, and the split is about what the item is, not what it is
    called. A highlight or a box or a line is a SHAPE over the content, so it
    turns with the content: a highlight along a line of text has to come out
    running down the page. A text label and a pasted image carry their own
    upright content and neither can draw itself rotated, so they travel by
    their centre and keep the size and orientation they had.
    """
    if isinstance(item, LineAnnotationItem):
        line = item.line()
        p1 = item.mapToScene(line.p1())
        p2 = item.mapToScene(line.p2())
        ax, ay = rotate_point(p1.x(), p1.y(), delta, width, height)
        bx, by = rotate_point(p2.x(), p2.y(), delta, width, height)
        item.setPos(QPointF(0.0, 0.0))
        item.setLine(QLineF(QPointF(ax, ay), QPointF(bx, by)))
    elif isinstance(item, TextAnnotationItem):
        box = item.scene_text_rect()
        x0, y0, _, _ = rotate_rect_upright(
            (box.left(), box.top(), box.right(), box.bottom()),
            delta, width, height)
        item.setPos(QPointF(x0, y0))
    else:
        box = item.scene_rect()
        turn = (rotate_rect_upright if isinstance(item, ImageAnnotationItem)
                else rotate_rect)
        x0, y0, x1, y1 = turn(
            (box.left(), box.top(), box.right(), box.bottom()),
            delta, width, height)
        # Pos back to the origin so the item's own rect IS its scene rect. The
        # two together are what geometry_snapshot captured, so an undo puts the
        # pair back and nothing is left leaning on the split.
        item.setPos(QPointF(0.0, 0.0))
        item.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))
    item.update()


class RotatePagesCommand(_PageCommand):
    """Turn a selection of pages by a quarter or a half, reversibly.

    THE EASY HALF is the document. A page's orientation is one number, /Rotate,
    and PyMuPDF's `set_rotation` writes it. Nothing else in the file moves:
    measured on 1.27.2.3, an annotation's `rect` and `vertices` come back
    IDENTICAL after a rotation, because annotation geometry is stored in the
    page's own unrotated user space and the viewer composes /Rotate on top when
    it draws. Render the page before and after and the second image is the
    first one turned, markup and all. So annotations already IN the file need
    no repair, and that is proved rather than asserted: see
    tests/test_page_rotation.py, which round-trips a highlight through a save
    and a plain PyMuPDF reopen and checks it still covers the same content.

    THE HARD HALF is the markup that is not in the file yet. Canvas items live
    in rendered-pixel space (visible page points times the document's frozen
    render scale), and "visible" is exactly the thing /Rotate changes. Left
    alone they would keep their old coordinates while the page turned under
    them, so a highlight would slide off the line it was drawn on. They are
    moved here, from the page's box as it stood BEFORE the turn, so a page that
    arrived already rotated turns from where it is rather than from zero.

    The stored coordinates are not converted, only re-expressed: the save path
    derotates visible space back to user space through the page's own matrix
    (PDFDocument.write_annotations), so a mark that is moved by the turn and
    then derotated by the new rotation lands on exactly the user-space rect it
    would have had if the page had never been turned.
    """

    _NAMES = {90: "right", 180: "180°", 270: "left"}

    def __init__(self, window, rows: list, delta):
        self._delta = normalize_rotation(delta)
        self._rows = sorted({int(r) for r in rows
                             if 0 <= int(r) < window._doc.page_count()})
        count = len(self._rows)
        noun = "page" if count == 1 else f"{count} pages"
        super().__init__(window, f"Rotate {noun}")
        doc = self._doc
        canvas = self._canvas
        self._before = {row: normalize_rotation(doc.doc[row].rotation)
                        for row in self._rows}
        self._after = {row: rotation_after(self._before[row], self._delta)
                       for row in self._rows}
        # The page-to-markup map does NOT change: rotation renumbers nothing.
        # It is snapshotted anyway because restore_page_annotations is the one
        # call that re-seats every item and re-renders the page, which is what
        # makes the turn show up in the editor.
        self._map = canvas.snapshot_page_annotations()
        self._page = canvas.current_page()
        # Scene box per page, BEFORE the turn, and the geometry to put back.
        scale = doc.render_scale()
        self._scene_box = {}
        self._geometry = []
        for row in self._rows:
            w_pt, h_pt = doc.get_page_size(row)
            self._scene_box[row] = (w_pt * scale, h_pt * scale)
            for item in self._map.get(row, []):
                self._geometry.append((row, item, geometry_snapshot(item)))

    def rows(self) -> list:
        return list(self._rows)

    def delta(self) -> int:
        return self._delta

    def direction(self) -> str:
        """"right", "left" or "180", for the status line."""
        return self._NAMES.get(self._delta, "")

    def _set_rotations(self, rotations: dict):
        doc = self._doc
        if not doc.doc:
            return
        for row, degrees in rotations.items():
            if 0 <= row < doc.page_count():
                doc.doc[row].set_rotation(degrees)
                # The page is drawn differently now, so every cached zoom level
                # of it is stale. Rotation is the one page edit that changes
                # what a render looks like without touching page indices, so
                # the whole-cache drop the other commands use would be waste.
                doc.invalidate_render_page(row)
        # A signed document's signature covers the pages as they were, and a
        # turned page is not the page it signed. Same flag the delete and the
        # reorder raise, so save_plan warns before it writes.
        doc._note_structure_change()

    def _apply(self):
        self._set_rotations(self._after)
        for row, item, _ in self._geometry:
            width, height = self._scene_box[row]
            _rotate_item(item, self._delta, width, height)
        self._canvas.restore_page_annotations(self._map, self._page)

    def _revert(self):
        self._set_rotations(self._before)
        for _, item, snapshot in self._geometry:
            geometry_restore(item, snapshot)
        self._canvas.restore_page_annotations(self._map, self._page)


def request_rotation(view, rows, delta) -> bool:
    """Turn `rows` of `view`'s document, as one undoable step. Both panels call it.

    THE SHARED ASK, the way `DocumentView._delete_pages` is the shared ask for
    a delete. It lives here rather than on the view for one reason: there must
    be exactly ONE way to rotate a page, and putting it beside the command it
    pushes is what stops a second one growing in a panel. Neither panel touches
    the document; they collect a selection and a direction and hand them over.
    """
    if view is None:
        return False
    doc = getattr(view, "_doc", None)
    if doc is None or not doc.doc:
        return False
    delta = normalize_rotation(delta)
    rows = sorted({int(r) for r in rows if 0 <= int(r) < doc.page_count()})
    if not rows or delta == 0:
        return False
    command = RotatePagesCommand(view, rows, delta)
    # Keep the pages that were turned selected once both panels rebuild, the
    # same way a drag keeps the block it moved.
    view._pending_page_selection = list(rows)
    view.undo_stack().push(command)
    count = len(rows)
    pages = f"{count} page{'s' if count > 1 else ''}"
    view._update_status(
        f"Rotated {pages} {command.direction()}  (Ctrl+Z to undo)")
    return True
