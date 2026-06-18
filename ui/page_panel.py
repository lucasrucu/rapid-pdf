from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem, QLabel,
)
from PySide6.QtCore import Signal, Qt, QSize
from PySide6.QtGui import QIcon, QPixmap


THUMB_W = 100
THUMB_H = 130
ITEM_W = 118
ITEM_H = 155


class PagePanel(QWidget):
    page_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._doc = None
        self._setup_ui()
        self.setFixedWidth(130)

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
        # Smooth pixel-based scrolling instead of jumping a whole page per wheel tick.
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.verticalScrollBar().setSingleStep(16)
        self._list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self._list)

    def set_document(self, doc):
        self._doc = doc
        self.refresh()

    def refresh(self):
        self._list.blockSignals(True)
        self._list.clear()
        if self._doc:
            for i in range(self._doc.page_count()):
                thumb = self._doc.render_thumbnail(i, max_width=THUMB_W)
                item = QListWidgetItem(QIcon(thumb), f"  {i + 1}")
                item.setSizeHint(QSize(ITEM_W, ITEM_H))
                self._list.addItem(item)
            if self._list.count() > 0:
                self._list.setCurrentRow(0)
        self._list.blockSignals(False)

    def set_current_page(self, page_num: int):
        self._list.blockSignals(True)
        self._list.setCurrentRow(page_num)
        self._list.blockSignals(False)

    def update_page_thumbnail(self, page_num: int, pixmap: QPixmap):
        """Replace one page's thumbnail (e.g. to reflect a live edit on that page)."""
        if pixmap and not pixmap.isNull() and 0 <= page_num < self._list.count():
            self._list.item(page_num).setIcon(QIcon(pixmap))

    def thumb_width(self) -> int:
        return THUMB_W

    def _on_row_changed(self, row: int):
        if row >= 0:
            self.page_selected.emit(row)
