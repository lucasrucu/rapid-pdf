from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem, QLabel,
)
from PySide6.QtCore import Signal, Qt, QSize, QTimer
from PySide6.QtGui import QIcon, QPixmap, QColor


THUMB_W = 100
THUMB_H = 130
ITEM_W = 118
ITEM_H = 155
# Render thumbnails this many pixels above/below the viewport so they're ready
# just before they scroll into view.
PREFETCH_PX = 300


class PagePanel(QWidget):
    page_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._doc = None
        # Rows whose real thumbnail has been rendered (others show a placeholder).
        self._rendered: set[int] = set()
        self._placeholder_cache: dict[int, QPixmap] = {}
        self._setup_ui()
        self.setFixedWidth(130)

    def _placeholder_for(self, page_num: int) -> QPixmap:
        """A grey placeholder sized to the page's real aspect ratio, so a landscape
        drawing's thumbnail doesn't visibly change shape when it renders. Page size
        is read without rasterising, so this stays cheap even for big documents."""
        h = THUMB_H
        if self._doc:
            w_pt, h_pt = self._doc.get_page_size(page_num)
            if w_pt > 0 and h_pt > 0:
                h = max(1, min(THUMB_H, round(THUMB_W * h_pt / w_pt)))
        pm = self._placeholder_cache.get(h)
        if pm is None:
            pm = QPixmap(THUMB_W, h)
            pm.fill(QColor(60, 60, 60))
            self._placeholder_cache[h] = pm
        return pm

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(4)

        lbl = QLabel("Pages")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("font-weight: bold; font-size: 10px;")
        layout.addWidget(lbl)

        self._list = QListWidget()
        self._list.setIconSize(QSize(THUMB_W, THUMB_H))
        self._list.setSpacing(2)
        # IconMode draws the page number centred *below* each thumbnail (like the
        # Organizer). TopToBottom + no wrapping keeps it a single vertical column.
        self._list.setViewMode(QListWidget.ViewMode.IconMode)
        self._list.setFlow(QListWidget.Flow.TopToBottom)
        self._list.setWrapping(False)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setDragDropMode(QListWidget.DragDropMode.NoDragDrop)
        self._list.setUniformItemSizes(True)
        # Smooth pixel-based scrolling instead of jumping a whole page per wheel tick.
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.verticalScrollBar().setSingleStep(16)
        self._list.currentRowChanged.connect(self._on_row_changed)
        # Fill in thumbnails as rows scroll into view.
        self._list.verticalScrollBar().valueChanged.connect(self._render_visible)
        layout.addWidget(self._list)

    def set_document(self, doc):
        self._doc = doc
        self.refresh()

    def refresh(self):
        """Populate the panel with placeholder items immediately; the real page
        thumbnails are rendered lazily, only for rows that are actually visible."""
        self._list.blockSignals(True)
        self._list.clear()
        self._rendered.clear()
        if self._doc:
            for i in range(self._doc.page_count()):
                item = QListWidgetItem(QIcon(self._placeholder_for(i)), str(i + 1))
                item.setSizeHint(QSize(ITEM_W, ITEM_H))
                item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
                self._list.addItem(item)
            if self._list.count() > 0:
                self._list.setCurrentRow(0)
        self._list.blockSignals(False)
        # Render the currently visible thumbnails now; once more after layout settles.
        self._render_visible()
        QTimer.singleShot(0, self._render_visible)

    def _render_visible(self):
        """Render real thumbnails for any not-yet-rendered rows in (or near) view."""
        if not self._doc:
            return
        vp = self._list.viewport().rect().adjusted(0, -PREFETCH_PX, 0, PREFETCH_PX)
        for i in range(self._list.count()):
            if i in self._rendered:
                continue
            item = self._list.item(i)
            if item is None or not self._list.visualItemRect(item).intersects(vp):
                continue
            thumb = self._doc.render_thumbnail(i, max_width=THUMB_W)
            item.setIcon(QIcon(thumb))
            self._rendered.add(i)

    def set_current_page(self, page_num: int):
        self._list.blockSignals(True)
        self._list.setCurrentRow(page_num)
        self._list.blockSignals(False)
        # Scrolling to the row may reveal new thumbnails to render.
        self._render_visible()

    def update_page_thumbnail(self, page_num: int, pixmap: QPixmap):
        """Replace one page's thumbnail (e.g. to reflect a live edit on that page)."""
        if pixmap and not pixmap.isNull() and 0 <= page_num < self._list.count():
            self._list.item(page_num).setIcon(QIcon(pixmap))
            # A live-rendered thumbnail counts as rendered so a later scroll pass
            # doesn't clobber it with a stale re-render.
            self._rendered.add(page_num)

    def thumb_width(self) -> int:
        return THUMB_W

    def _on_row_changed(self, row: int):
        if row >= 0:
            self.page_selected.emit(row)
