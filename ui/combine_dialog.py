"""Adobe-style staged combine.

Each selected PDF first appears as ONE movable card (the whole file) in a
grid you can reorder freely. A card can be expanded into its individual
pages inline (double-click, or the Expand button); pages then move around
like any card, within and across documents. Nothing is merged and nothing
touches disk until Combine is clicked; closing the dialog changes nothing.

The grid deliberately reuses the Organizer's machinery (_DragList's manual
multi-item drop, fanned drag pixmap, edge autoscroll, and _ThumbDelegate's
card painting) so combining feels identical to organizing.
"""

import os

import fitz
from PySide6.QtCore import Qt, QSize, QRect
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton,
)

from core.pdf_document import PDFDocument
from ui.organizer import _DragList, _ThumbDelegate, THUMB_W, THUMB_H, ITEM_W, ITEM_H, _PAGE_ID

# Item payload: ("unit", src_idx) for a whole-file card,
#               ("page", src_idx, page_idx) for a single page card.
_PAYLOAD = Qt.ItemDataRole.UserRole + 10

_NAME_MAX = 20   # characters of the file name shown on a card label


class CombineDialog(QDialog):
    """Staging area for combining PDFs. Call merged_document() after exec()
    returns Accepted to take ownership of the merged fitz document."""

    def __init__(self, paths: list[str], palette=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Combine PDFs")
        self.setModal(True)
        self.resize(760, 560)

        self._merged: fitz.Document | None = None
        self._sources: list[dict] = []   # {path, name, short, pd (PDFDocument)}
        errors = []
        for path in paths:
            try:
                pd = PDFDocument()
                pd.doc = fitz.open(path)
                name = os.path.basename(path)
                base = os.path.splitext(name)[0]
                short = base if len(base) <= _NAME_MAX else base[:_NAME_MAX - 1] + "…"
                self._sources.append(
                    {"path": path, "name": name, "short": short, "pd": pd})
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")
        if errors:
            QMessageBox.critical(self, "Open Error", "\n".join(errors))

        self._setup_ui(palette)
        self._populate()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _setup_ui(self, palette):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._expand_btn = QPushButton("Expand into Pages")
        self._expand_btn.setToolTip(
            "Split the selected file card(s) into individual page cards "
            "(double-click a card does the same)")
        self._expand_btn.clicked.connect(self._expand_selected)
        bar.addWidget(self._expand_btn)
        bar.addStretch()
        hint = QLabel("Drag to reorder  ·  Double-click a file card to expand its pages")
        hint.setStyleSheet("font-size: 10px;")
        bar.addWidget(hint)
        layout.addLayout(bar)

        # Same grid configuration as the Organizer, same drag machinery.
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
        self._list.setSpacing(0)
        self._list.setUniformItemSizes(True)
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._delegate = _ThumbDelegate(self._list)
        if palette is not None:
            self._delegate.sel_color = QColor(palette.selection)
            self._delegate.hover_color = QColor(palette.surface)
            self._delegate.text_color = QColor(palette.text_dim)
            self._delegate.sel_text_color = QColor(palette.selection_text)
        self._list.setItemDelegate(self._delegate)
        self._list.reordered.connect(lambda order: self._retag())
        self._list.reorder_invalid.connect(self._retag)
        self._list.itemDoubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self._list)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self._combine_btn = QPushButton("Combine")
        self._combine_btn.setDefault(True)
        self._combine_btn.clicked.connect(self._do_combine)
        buttons.addWidget(self._combine_btn)
        layout.addLayout(buttons)

    def _populate(self):
        for src_idx in range(len(self._sources)):
            self._list.addItem(self._make_unit_item(src_idx))
        self._retag()

    # ------------------------------------------------------------------
    # Cards
    # ------------------------------------------------------------------

    def _make_unit_item(self, src_idx: int) -> QListWidgetItem:
        src = self._sources[src_idx]
        count = src["pd"].page_count()
        item = QListWidgetItem(QIcon(self._unit_pixmap(src_idx)),
                               f"{src['short']}  ({count} pg)")
        item.setSizeHint(QSize(ITEM_W, ITEM_H))
        item.setData(_PAYLOAD, ("unit", src_idx))
        item.setToolTip(f"{src['name']}: {count} page(s). "
                        f"Double-click to expand into pages.")
        return item

    def _make_page_item(self, src_idx: int, page_idx: int) -> QListWidgetItem:
        src = self._sources[src_idx]
        thumb = src["pd"].render_thumbnail(page_idx, max_width=THUMB_W)
        item = QListWidgetItem(QIcon(thumb), f"{src['short']}  p.{page_idx + 1}")
        item.setSizeHint(QSize(ITEM_W, ITEM_H))
        item.setData(_PAYLOAD, ("page", src_idx, page_idx))
        item.setToolTip(f"{src['name']}, page {page_idx + 1}")
        return item

    def _unit_pixmap(self, src_idx: int) -> QPixmap:
        """A 'whole document' card: the first page's thumbnail with a couple
        of offset sheets peeking out behind it, so a file unit reads
        differently from a single page."""
        src = self._sources[src_idx]
        front = src["pd"].render_thumbnail(0, max_width=THUMB_W - 14)
        canvas = QPixmap(THUMB_W, THUMB_H)
        canvas.fill(Qt.GlobalColor.transparent)
        p = QPainter(canvas)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fw, fh = front.width(), front.height()
        x = (THUMB_W - fw - 12) // 2
        y = max(0, (THUMB_H - fh - 12) // 2)
        # Back sheets (plain pages), then the front thumbnail on top.
        p.setPen(QColor(0, 0, 0, 60))
        for off in (12, 6):
            p.setBrush(QColor(255, 255, 255))
            p.drawRect(QRect(x + off, y + off, fw, fh))
        p.drawPixmap(x, y, front)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRect(x, y, fw, fh))
        p.end()
        return canvas

    def _retag(self):
        """Keep _PAGE_ID (the drag machinery's identity role) equal to the
        row, so _DragList's permutation check stays valid after any change."""
        for i in range(self._list.count()):
            self._list.item(i).setData(_PAGE_ID, i)

    # ------------------------------------------------------------------
    # Expand
    # ------------------------------------------------------------------

    def _on_double_clicked(self, item: QListWidgetItem):
        payload = item.data(_PAYLOAD)
        if payload and payload[0] == "unit":
            self._expand_item(self._list.row(item))

    def _expand_selected(self):
        rows = sorted((self._list.row(i) for i in self._list.selectedItems()
                       if (i.data(_PAYLOAD) or ("",))[0] == "unit"),
                      reverse=True)
        if not rows:
            QMessageBox.information(
                self, "Nothing to Expand",
                "Select one or more file cards first (page cards are already "
                "expanded).")
            return
        for row in rows:   # descending, so earlier rows stay valid
            self._expand_item(row)

    def _expand_item(self, row: int):
        item = self._list.item(row)
        payload = item.data(_PAYLOAD)
        if not payload or payload[0] != "unit":
            return
        src_idx = payload[1]
        count = self._sources[src_idx]["pd"].page_count()
        self._list.takeItem(row)
        for offset in range(count):
            self._list.insertItem(row + offset,
                                  self._make_page_item(src_idx, offset))
        self._retag()

    # ------------------------------------------------------------------
    # Combine
    # ------------------------------------------------------------------

    def _do_combine(self):
        if self._list.count() == 0:
            self.reject()
            return
        merged = fitz.open()
        try:
            for i in range(self._list.count()):
                payload = self._list.item(i).data(_PAYLOAD)
                src_doc = self._sources[payload[1]]["pd"].doc
                if payload[0] == "unit":
                    merged.insert_pdf(src_doc)
                else:
                    merged.insert_pdf(src_doc, from_page=payload[2],
                                      to_page=payload[2])
        except Exception as e:
            merged.close()
            QMessageBox.critical(self, "Combine Error", str(e))
            return
        self._merged = merged
        self.accept()

    def merged_document(self) -> fitz.Document | None:
        """Ownership passes to the caller; None if the dialog was cancelled."""
        doc = self._merged
        self._merged = None
        return doc

    # ------------------------------------------------------------------
    # Cleanup: sources are only read, so closing the dialog (either way)
    # leaves every input file untouched on disk.
    # ------------------------------------------------------------------

    def done(self, result: int):
        for src in self._sources:
            try:
                if src["pd"].doc is not None:
                    src["pd"].doc.close()
            except Exception:
                pass
            src["pd"].doc = None
        super().done(result)
