import fitz as _fitz
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QLabel, QFileDialog, QMessageBox,
    QStyledItemDelegate, QStyle,
)
from PySide6.QtCore import Signal, Qt, QSize, QRect, QTimer
from PySide6.QtGui import QIcon, QColor, QPixmap

THUMB_W = 160
THUMB_H = 210
ITEM_W = 184
ITEM_H = 244

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
    """
    _TEXT_H = 22

    def paint(self, painter, option, index):
        painter.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(option.rect, QColor(0, 120, 212))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, QColor(46, 46, 46))

        inner = option.rect.adjusted(6, 6, -6, -6)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None:
            area = QRect(inner.x(), inner.y(), inner.width(),
                         max(1, inner.height() - self._TEXT_H))
            pm = icon.pixmap(area.size())
            painter.drawPixmap(
                area.x() + (area.width() - pm.width()) // 2,
                area.y() + (area.height() - pm.height()) // 2,
                pm,
            )
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text:
            painter.setPen(QColor("#ffffff") if selected else QColor("#cccccc"))
            trect = QRect(inner.x(), inner.bottom() - self._TEXT_H + 2,
                          inner.width(), self._TEXT_H)
            painter.drawText(
                trect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                str(text),
            )
        painter.restore()


class _DragList(QListWidget):
    """Thumbnail grid that reorders via Qt's NATIVE InternalMove.

    Native handling means Qt owns drag acceptance and the between-items drop
    indicator (so there is no "can't place here" cursor), and ListMode layout
    means the view re-flows items with no leftover gaps. After each drop we read
    the new order back from each item's _PAGE_ID role and report it.
    """
    reordered = Signal(list)       # new order as a list of original page indices
    reorder_invalid = Signal()     # drop left the list in an unexpected state

    def dropEvent(self, event):
        n = self.count()
        super().dropEvent(event)   # native reorder: removes source, inserts at indicator
        self._emit_reorder(n)

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


_ORG_STYLE = """
QWidget {
    background-color: #1a1a1a;
    color: #d4d4d4;
}
QPushButton {
    background-color: #2d2d2d;
    border: 1px solid #444;
    border-radius: 3px;
    color: #d4d4d4;
    padding: 4px 10px;
    min-height: 26px;
}
QPushButton:hover {
    background-color: #3a3a3a;
    border-color: #666;
    color: #fff;
}
QPushButton:pressed {
    background-color: #0078d4;
}
QListWidget {
    background-color: #242424;
    border: none;
    outline: none;
}
QListWidget::item {
    border-radius: 4px;
    color: #ccc;
    padding: 4px;
}
QListWidget::item:selected {
    background-color: #0078d4;
    color: #fff;
}
QListWidget::item:hover:!selected {
    background-color: #2e2e2e;
}
"""


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
        self._setup_ui()
        self.setStyleSheet(_ORG_STYLE)

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
        hint.setStyleSheet("color: #555; font-size: 10px;")
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
        self._list.setSpacing(4)
        self._list.setUniformItemSizes(True)
        self._list.setItemDelegate(_ThumbDelegate(self._list))
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
            pm.fill(QColor(60, 60, 60))
            self._placeholder_cache[h] = pm
        return pm

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
