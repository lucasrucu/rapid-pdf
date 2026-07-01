import fitz as _fitz
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QLabel, QFileDialog, QMessageBox,
    QStyledItemDelegate, QStyle, QStyleOptionViewItem,
)
from PySide6.QtCore import Signal, Qt, QSize, QRect, QTimer, QPoint
from PySide6.QtGui import QIcon, QColor, QPixmap, QPainter, QDrag

THUMB_W = 160
THUMB_H = 210
ITEM_W = 192
ITEM_H = 262

# Render thumbnails this many pixels above/below the viewport so they're ready
# just before they scroll into view (mirrors the page-panel lazy strategy).
PREFETCH_PX = 240

_PAGE_ID = Qt.ItemDataRole.UserRole       # each item's source page index (retagged on edits)
# The page index in the RENDER doc (markup-baked clone) this item's thumbnail
# comes from. Set once at refresh and NEVER retagged — a drag reorders only the
# live doc, not the clone, so the lazy renderer must keep pulling each thumbnail
# from its original clone page or a scrolled-in placeholder would show the wrong
# page after a reorder.
_SRC_ID = Qt.ItemDataRole.UserRole + 1
_RENDERED = Qt.ItemDataRole.UserRole + 2  # bool: real thumbnail rendered (vs placeholder)


class _ThumbDelegate(QStyledItemDelegate):
    """Draw each page thumbnail with its 'Page N' label centred BELOW it.

    The list stays in ListMode (so reordering re-flows the grid cleanly); this
    delegate just owns the in-cell layout, which ListMode otherwise renders with
    the label beside the thumbnail where it gets clipped to nothing.

    Selection / hover / label colors come from the theme (set on the instance by
    PageOrganizer.apply_palette) so the grid follows light/dark.
    """
    _TEXT_H = 22
    # Even inset of the selection/hover backing (all four sides) so the accent
    # wraps the whole cell evenly, and a gap so the page-number label doesn't
    # touch the thumbnail above it.
    _PAD = 5
    _LABEL_GAP = 6
    # Purely cosmetic horizontal nudge applied to the cells flanking the
    # current drop line during a drag, so the insertion point reads as "pages
    # sliding apart to make room" instead of just a thin indicator line. Does
    # not touch layout/geometry — only where this delegate paints each cell.
    _NUDGE_PX = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sel_color = QColor("#F1AE04")
        self.hover_color = QColor("#F3EFE6")
        self.text_color = QColor("#2A2620")
        self.sel_text_color = QColor("#2A2010")

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        rect = option.rect
        drag_row = getattr(self.parent(), "drag_target_row", lambda: None)()
        if drag_row is not None:
            row = index.row()
            # Only nudge within the same visual row (a wrap boundary means the
            # "before" cell is the last item of the previous row, which reads
            # fine without a matching shift on the far side of the wrap).
            if row == drag_row - 1:
                rect = rect.translated(-self._NUDGE_PX, 0)
            elif row == drag_row:
                rect = rect.translated(self._NUDGE_PX, 0)
        backing = rect.adjusted(self._PAD, self._PAD, -self._PAD, -self._PAD)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.sel_color)
            painter.drawRoundedRect(backing, 8, 8)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.hover_color)
            painter.drawRoundedRect(backing, 8, 8)

        inner = backing.adjusted(5, 5, -5, -5)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None:
            area = QRect(inner.x(), inner.y(), inner.width(),
                         max(1, inner.height() - self._TEXT_H - self._LABEL_GAP))
            pm = icon.pixmap(area.size())
            painter.drawPixmap(
                area.x() + (area.width() - pm.width()) // 2,
                area.y() + (area.height() - pm.height()) // 2,
                pm,
            )
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text:
            painter.setPen(self.sel_text_color if selected else self.text_color)
            trect = QRect(inner.x(), inner.bottom() - self._TEXT_H + 2,
                          inner.width(), self._TEXT_H)
            painter.drawText(
                trect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                str(text),
            )
        painter.restore()


class _DragList(QListWidget):
    """Thumbnail grid that reorders via a MANUAL internal move.

    Qt's native InternalMove drop handling only reliably supports moving a
    single contiguous block of rows: for a non-contiguous multi-selection it
    resolves each moved row's new position from a mime-encoded row list that
    goes stale mid-drop (rows shift as earlier ones are removed), which can
    hand a native takeItem/removeRow an out-of-range index and crash the
    process outright (a C++-side assert/segfault, not a catchable Python
    exception — this is a long-standing Qt issue, not specific to this app).
    So the drop is handled entirely by hand instead of delegating to
    super().dropEvent(): take every selected item out (descending row order,
    so each takeItem() never invalidates a later index), figure out where the
    drop point landed among what's left, and reinsert the whole taken batch
    together at that spot. This sidesteps Qt's native multi-row math and, as a
    side effect, gives the desired UX: non-contiguous selections always land
    as one merged block wherever the user drops them.
    """
    reordered = Signal(list)       # new order as a list of original page indices
    reorder_invalid = Signal()     # drop left the list in an unexpected state

    # Fanned-stack drag pixmap tuning: how far each card behind the top one
    # peeks out (purely cosmetic, mirrors a hand of playing cards).
    _STACK_OFFSET_PX = 6
    _STACK_MAX_CARDS = 5   # cap the visible fan so a 40-page selection doesn't
                           # draw 40 layered thumbnails under the cursor

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._drag_row = None  # current drop-target row while a drag is over the grid, else None

    def drag_target_row(self):
        """Row the drop indicator currently points at, or None when not dragging.
        Read by the delegate to nudge the cells on either side of that row."""
        return self._drag_row

    def startDrag(self, supportedActions):
        """Replace Qt's default multi-item drag rendering (every selected item
        painted near its own original position, which reads as the whole grid
        smearing across the screen) with a single fanned card-stack pixmap that
        follows the cursor as one unit.

        This only changes what's painted under the cursor during the drag —
        the actual reorder on drop is unaffected (still handled by dropEvent's
        manual take/reinsert, which already merges the selection into one block
        wherever it lands)."""
        rows = sorted({self.row(i) for i in self.selectedItems()})
        if len(rows) <= 1:
            super().startDrag(supportedActions)
            return

        mime = self.model().mimeData([self.model().index(r, 0) for r in rows])
        if mime is None:
            super().startDrag(supportedActions)
            return

        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap, hotspot = self._stack_pixmap(rows)
        drag.setPixmap(pixmap)
        drag.setHotSpot(hotspot)
        drag.exec(supportedActions, Qt.DropAction.MoveAction)

    def _stack_pixmap(self, rows):
        """Build a fanned card-stack pixmap from the selected rows' thumbnails:
        a handful of cards peeking out behind a full-opacity top card, plus a
        badge showing the total selection count. Returns (pixmap, hotspot) where
        hotspot keeps the top card under the cursor at the same spot it was
        grabbed, so the drag doesn't visually "jump"."""
        shown = rows[:self._STACK_MAX_CARDS]
        off = self._STACK_OFFSET_PX
        extra = off * (len(shown) - 1)
        cell_w, cell_h = self.gridSize().width(), self.gridSize().height()
        badge_d = 26
        # Extra margin at the top-right so the count badge (anchored to the
        # fanned-out corner) has room without being clipped by the pixmap edge.
        margin = badge_d // 2 + 2
        canvas = QPixmap(cell_w + extra + margin, cell_h + extra + margin)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Back-to-front so the top (first-selected) card paints last, fully
        # opaque, on top of the fanned-out ones behind it. Cards are offset by
        # `margin` on both axes so the top-right badge has clearance without
        # needing negative coordinates.
        for depth, row in reversed(list(enumerate(shown))):
            item = self.item(row)
            if item is None:
                continue
            opacity = 1.0 if depth == 0 else 0.55
            painter.setOpacity(opacity)
            option = QStyleOptionViewItem()
            option.initFrom(self)
            option.rect = QRect(depth * off, depth * off + margin, cell_w, cell_h)
            option.state |= QStyle.StateFlag.State_Selected
            self.itemDelegate().paint(painter, option, self.indexFromItem(item))
        painter.setOpacity(1.0)
        # Badge with the total selection count (not just the fanned subset),
        # so a 20-page drag still reads as "20", not "5".
        if len(rows) > 1:
            bx = cell_w - badge_d // 2
            by = 0
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._delegate_sel_color())
            painter.drawEllipse(bx, by, badge_d, badge_d)
            painter.setPen(Qt.GlobalColor.white)
            font = painter.font()
            font.setBold(True)
            font.setPointSize(10)
            painter.setFont(font)
            painter.drawText(QRect(bx, by, badge_d, badge_d),
                              Qt.AlignmentFlag.AlignCenter, str(len(rows)))
        painter.end()
        # Hotspot: the top card sits at (0, margin) in canvas space, so its
        # centre is a stable, cursor-following anchor point.
        return canvas, QPoint(cell_w // 2, margin + cell_h // 2)

    def _delegate_sel_color(self):
        delegate = self.itemDelegate()
        color = getattr(delegate, "sel_color", None)
        return color if color is not None else QColor("#F1AE04")

    def dragMoveEvent(self, event):
        super().dragMoveEvent(event)
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        row = self._drop_row(pos)
        if row != self._drag_row:
            self._drag_row = row
            self.viewport().update()

    def dragLeaveEvent(self, event):
        self._drag_row = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._drag_row = None
        n = self.count()
        rows = sorted({self.row(i) for i in self.selectedItems()})
        if not rows:
            event.ignore()
            return

        # Where the user is dropping, expressed as an index into the list
        # AFTER the dragged rows are removed from it (so it's directly usable
        # as an insertion index with no further adjustment).
        target = self._drop_row(event.position().toPoint() if hasattr(event, "position")
                                 else event.pos())
        removed_before_target = sum(1 for r in rows if r < target)
        target -= removed_before_target

        items = [self.takeItem(r) for r in reversed(rows)]
        items.reverse()  # back to original relative order
        target = max(0, min(target, self.count()))
        for offset, item in enumerate(items):
            self.insertItem(target + offset, item)

        self.clearSelection()
        for offset in range(len(items)):
            self.item(target + offset).setSelected(True)

        event.acceptProposedAction()
        self._emit_reorder(n)

    def _drop_row(self, pos) -> int:
        """Row index (pre-removal) the drop point resolves to: the row of the
        item the point landed in/after, or count() if it's past the last item."""
        item = self.itemAt(pos)
        if item is None:
            return self.count()
        row = self.row(item)
        rect = self.visualItemRect(item)
        # Grid flows left-to-right with wrapping: "insert after" means the
        # point is past the item's horizontal midpoint (mirrors the native
        # drop-indicator's before/after split for a wrapped grid).
        if pos.x() > rect.center().x():
            row += 1
        return row

    def _emit_reorder(self, expected_n: int):
        """Read the post-drop order from item ids and report it (or flag a bad drop)."""
        if self.count() != expected_n:
            self.reorder_invalid.emit()
            return
        order = [self.item(i).data(_PAGE_ID) for i in range(self.count())]
        if any(v is None for v in order) or sorted(order) != list(range(expected_n)):
            self.reorder_invalid.emit()
            return
        if order != list(range(expected_n)):
            self.reordered.emit(order)


class PageOrganizer(QWidget):
    """Grid view for reviewing and reordering PDF pages."""

    page_activated = Signal(int)        # double-click → switch editor to this page
    pages_reordered_perm = Signal(list)  # new page order (permutation of old indices)
    pages_deleted = Signal(list)        # list of deleted page indices (descending)
    needs_rebuild = Signal()            # ask the host to rebuild the markup thumbnails
    pages_added = Signal(int)           # pages inserted via "+ Add Pages" (count) → host marks unsaved

    def __init__(self, parent=None):
        super().__init__(parent)
        self._doc = None       # real document — all structural edits happen here
        self._render = None    # optional PDFDocument whose pages have markup baked in
        self._placeholder_color = QColor("#F3EFE6")  # themed via apply_palette()
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # --- Top bar ---
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._add_btn = QPushButton("+ Add Pages from PDF…")
        self._add_btn.setToolTip("Insert pages from another PDF after the last selected page")
        self._add_btn.clicked.connect(self._add_pages)
        bar.addWidget(self._add_btn)

        self._del_btn = QPushButton("Delete Selected")
        self._del_btn.setToolTip("Permanently remove selected pages from the document (Del)")
        self._del_btn.clicked.connect(self.delete_selected)
        bar.addWidget(self._del_btn)

        bar.addStretch()

        hint = QLabel("Drag to reorder  ·  Double-click to edit in canvas")
        hint.setObjectName("section")
        hint.setStyleSheet("font-size: 10px;")
        bar.addWidget(hint)

        layout.addLayout(bar)

        # --- Grid ---
        # ListMode + LeftToRight wrapping gives a grid whose layout the VIEW
        # manages (so reordering re-flows with no gaps), while native InternalMove
        # provides drop acceptance + the between-items insertion indicator.
        self._list = _DragList()
        self._list.setViewMode(QListWidget.ViewMode.ListMode)
        self._list.setFlow(QListWidget.Flow.LeftToRight)
        self._list.setWrapping(True)
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setIconSize(QSize(THUMB_W, THUMB_H))
        self._list.setGridSize(QSize(ITEM_W, ITEM_H))
        self._list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self._list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._list.setDropIndicatorShown(True)
        # NOTE: don't combine setSpacing() with an explicit setGridSize() here —
        # Qt's grid-mode layout adds spacing into the row/column pitch AND into
        # the per-item rect it hands the delegate, and the two don't line up:
        # the whole grid visibly shifts down/right by ~spacing, which clips the
        # very first row against the viewport top (nothing above it to absorb
        # the shift). All the visual gutter here comes from the delegate's own
        # _PAD inset instead, so spacing stays 0.
        self._list.setSpacing(0)
        self._list.setUniformItemSizes(True)
        self._delegate = _ThumbDelegate(self._list)
        self._list.setItemDelegate(self._delegate)
        self._list.reordered.connect(self._on_reordered)
        self._list.reorder_invalid.connect(self.needs_rebuild)
        self._list.itemDoubleClicked.connect(self._on_item_activated)
        # Smooth pixel scrolling + render thumbnails lazily as cells scroll in.
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.verticalScrollBar().valueChanged.connect(self._render_visible)
        layout.addWidget(self._list)
        self._placeholder_cache: dict[int, QPixmap] = {}

    def set_document(self, doc, render=None):
        """doc = live document (edited in place). render = optional doc whose pages
        already have unsaved markup baked in, used only for thumbnails."""
        self._doc = doc
        self._render = render
        self.refresh()

    def _render_source(self):
        """Doc to rasterise thumbnails from: the markup-baked clone if present,
        else the live document (same source the page panel uses)."""
        return self._render or self._doc

    def _placeholder_for(self, page_num: int) -> QPixmap:
        """A grey placeholder sized to the page's real aspect ratio, so a landscape
        drawing's cell doesn't visibly change shape when its thumbnail renders.
        Page size is read without rasterising, so this stays cheap on big docs."""
        h = THUMB_H
        src = self._render_source()
        if src:
            w_pt, h_pt = src.get_page_size(page_num)
            if w_pt > 0 and h_pt > 0:
                h = max(1, min(THUMB_H, round(THUMB_W * h_pt / w_pt)))
        pm = self._placeholder_cache.get(h)
        if pm is None:
            pm = QPixmap(THUMB_W, h)
            pm.fill(self._placeholder_color)
            self._placeholder_cache[h] = pm
        return pm

    def apply_palette(self, palette):
        """Theme the delegate (selection/hover/label) and placeholder fill, then
        repaint. Called once at start and on every light/dark toggle."""
        self._delegate.sel_color = QColor(palette.selection)
        self._delegate.hover_color = QColor(palette.surface)
        self._delegate.text_color = QColor(palette.text_dim)
        self._delegate.sel_text_color = QColor(palette.selection_text)
        self._placeholder_color = QColor(palette.surface_sunken)
        self._placeholder_cache.clear()
        self._list.viewport().update()

    def refresh(self):
        """Populate cells with placeholders immediately; real page thumbnails are
        rendered lazily, only for cells actually in (or near) the viewport.

        Eager all-page rendering blocked the Organizer open for ~3ms/page (an
        ~300ms freeze on a 100-page doc); lazy rendering makes the open near-
        instant and only pays for what's on screen."""
        src = self._render_source()
        self._list.blockSignals(True)
        self._list.clear()
        if src and src.doc:
            for i in range(src.page_count()):
                item = QListWidgetItem(QIcon(self._placeholder_for(i)), f"Page {i + 1}")
                item.setSizeHint(QSize(ITEM_W, ITEM_H))
                item.setData(_PAGE_ID, i)
                item.setData(_SRC_ID, i)        # clone page this thumbnail comes from
                item.setData(_RENDERED, False)
                item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
                self._list.addItem(item)
        self._list.blockSignals(False)
        # Render what's visible now, and again once the grid has laid out.
        self._render_visible()
        QTimer.singleShot(0, self._render_visible)
        self._update_buttons()

    def _render_visible(self):
        """Rasterise real thumbnails for any placeholder cells in (or near) view.

        Each thumbnail is pulled from its _SRC_ID page in the render doc, NOT its
        current row: a drag reorders only the live doc, so a cell scrolled into
        view after a reorder must still render its original clone page."""
        src = self._render_source()
        if not src or not src.doc:
            return
        vp = self._list.viewport().rect().adjusted(0, -PREFETCH_PX, 0, PREFETCH_PX)
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is None or item.data(_RENDERED):
                continue
            if not self._list.visualItemRect(item).intersects(vp):
                continue
            src_page = item.data(_SRC_ID)
            if src_page is None or src_page >= src.page_count():
                continue
            item.setIcon(QIcon(src.render_thumbnail(src_page, max_width=THUMB_W)))
            item.setData(_RENDERED, True)

    def thumb_width(self) -> int:
        return THUMB_W

    def update_page_thumbnail(self, page_num: int, pixmap: QPixmap):
        """Patch one page's thumbnail in place (e.g. after a live canvas edit),
        instead of rebuilding the whole grid from a fresh markup-baked clone.

        Mirrors PagePanel.update_page_thumbnail — same cheap "grab what the
        canvas already rendered" pixmap, no PyMuPDF re-render and no throwaway
        fitz clone. This is what keeps the Organizer's thumbnails from lagging
        behind the Editor tab, which was patching its own panel this way already.

        Looks the row up by _PAGE_ID rather than assuming row == page_num, since
        a completed drag reorders the live doc while a partial edit that arrives
        mid-drag must still land on the item representing that logical page."""
        if not pixmap or pixmap.isNull():
            return
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is not None and item.data(_PAGE_ID) == page_num:
                item.setIcon(QIcon(pixmap))
                item.setData(_RENDERED, True)
                return

    def _update_buttons(self):
        has_doc = bool(self._doc and self._doc.doc)
        self._add_btn.setEnabled(has_doc)
        self._del_btn.setEnabled(has_doc)

    def _retag_identity(self):
        """After a structural edit the widget order == document order again, so
        reset each item's stored page id to its row and relabel."""
        for i in range(self._list.count()):
            it = self._list.item(i)
            it.setData(_PAGE_ID, i)
            it.setText(f"Page {i + 1}")
            it.setSizeHint(QSize(ITEM_W, ITEM_H))

    def _on_reordered(self, new_order: list):
        if not self._doc:
            return
        self._doc.reorder(new_order)
        self.pages_reordered_perm.emit(new_order)
        self._retag_identity()

    def _on_item_activated(self, item: QListWidgetItem):
        self.page_activated.emit(self._list.row(item))

    def _add_pages(self):
        if not self._doc or not self._doc.doc:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select PDFs to Insert", "", "PDF Files (*.pdf)"
        )
        if not paths:
            return
        paths = sorted(paths)
        selected = self._list.selectedItems()
        at = self._list.row(selected[-1]) + 1 if selected else self._doc.page_count()
        total = 0
        errors = []
        for path in paths:
            try:
                src = _fitz.open(path)
                count = len(src)
                src.close()
                self._doc.insert_pdf(path, start_at=at)
                at += count
                total += count
            except Exception as e:
                errors.append(f"{path}: {e}")
        self.needs_rebuild.emit()  # host rebuilds markup thumbnails + calls set_document
        if total:
            self.pages_added.emit(total)  # merge → host marks the doc untitled + unsaved
        if errors:
            QMessageBox.critical(self, "Insert Error", "\n".join(errors))

    def delete_selected(self):
        if not self._doc or not self._doc.doc:
            return
        rows = sorted(
            {self._list.row(i) for i in self._list.selectedItems()},
            reverse=True,
        )
        if not rows:
            return
        remaining = self._doc.page_count() - len(rows)
        if remaining < 1:
            QMessageBox.warning(self, "Cannot Delete", "Cannot delete all pages.")
            return
        answer = QMessageBox.question(
            self, "Delete Pages",
            f"Permanently delete {len(rows)} page(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for row in rows:               # descending → indices stay valid
            self._doc.delete_page(row)
            self._list.takeItem(row)
        self.pages_deleted.emit(rows)
        self._retag_identity()
