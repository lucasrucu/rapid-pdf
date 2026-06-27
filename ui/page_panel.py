from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem, QLabel,
    QStyledItemDelegate, QStyle,
)
from PySide6.QtCore import Signal, Qt, QSize, QTimer, QRect
from PySide6.QtGui import QIcon, QPixmap, QColor


THUMB_W = 100
THUMB_H = 130
ITEM_W = 118
ITEM_H = 155
_TEXT_H = 18
# Render thumbnails this many pixels above/below the viewport so they're ready
# just before they scroll into view.
PREFETCH_PX = 300


class _PageDelegate(QStyledItemDelegate):
    """Draw thumbnail above label, selection highlight covering the whole cell.

    IconMode has a Qt quirk where the selection rect drifts away from the visual
    item position when icon sizes vary. ListMode + this delegate is pixel-perfect.
    """

    sel_color = QColor("#F1AE04")
    hover_color = QColor("#FFFFFF")
    text_color = QColor("#7A7264")
    sel_text_color = QColor("#2A2010")

    def paint(self, painter, option, index):
        painter.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(option.rect, self.sel_color)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, self.hover_color)

        inner = option.rect.adjusted(6, 6, -6, -6)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None:
            thumb_area = QRect(inner.x(), inner.y(), inner.width(),
                               max(1, inner.height() - _TEXT_H))
            pm = icon.pixmap(thumb_area.size())
            painter.drawPixmap(
                thumb_area.x() + (thumb_area.width() - pm.width()) // 2,
                thumb_area.y() + (thumb_area.height() - pm.height()) // 2,
                pm,
            )

        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text:
            painter.setPen(self.sel_text_color if selected else self.text_color)
            trect = QRect(inner.x(), inner.bottom() - _TEXT_H + 2,
                          inner.width(), _TEXT_H)
            painter.drawText(
                trect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                str(text),
            )
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(ITEM_W, ITEM_H)


class PagePanel(QWidget):
    page_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._doc = None
        # Optional doc whose pages already have unsaved markup baked in. When set,
        # thumbnails render from it so they match the page + live overlays exactly
        # (the same trick the Organizer uses). Falls back to _doc when None.
        self._render = None
        # Rows whose real thumbnail has been rendered (others show a placeholder).
        self._rendered: set[int] = set()
        self._placeholder_cache: dict[int, QPixmap] = {}
        self._placeholder_color = QColor("#F3EFE6")  # themed via apply_palette()
        self._setup_ui()
        self.setFixedWidth(130)

    def _render_source(self):
        """Doc to rasterise thumbnails from: the markup-baked clone if present,
        else the live document."""
        return self._render or self._doc

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
            pm.fill(self._placeholder_color)
            self._placeholder_cache[h] = pm
        return pm

    def apply_palette(self, palette):
        """Theme the delegate (selection/hover/label) and placeholder fill, then
        repaint. Called once at start and on every light/dark toggle."""
        _PageDelegate.sel_color = QColor(palette.selection)
        _PageDelegate.hover_color = QColor(palette.surface)
        _PageDelegate.text_color = QColor(palette.text_dim)
        _PageDelegate.sel_text_color = QColor(palette.selection_text)
        self._placeholder_color = QColor(palette.surface_sunken)
        self._placeholder_cache.clear()
        self._list.viewport().update()

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
        # ListMode + custom delegate: avoids the IconMode Qt quirk where the
        # selection highlight rect drifts below the visual item position.
        self._list.setViewMode(QListWidget.ViewMode.ListMode)
        self._list.setFlow(QListWidget.Flow.TopToBottom)
        self._list.setWrapping(False)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setDragDropMode(QListWidget.DragDropMode.NoDragDrop)
        self._list.setUniformItemSizes(True)
        self._list.setItemDelegate(_PageDelegate(self._list))
        # Smooth pixel-based scrolling instead of jumping a whole page per wheel tick.
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.verticalScrollBar().setSingleStep(16)
        self._list.currentRowChanged.connect(self._on_row_changed)
        # Fill in thumbnails as rows scroll into view.
        self._list.verticalScrollBar().valueChanged.connect(self._render_visible)
        layout.addWidget(self._list)

    def set_document(self, doc, render=None):
        """doc = live document (drives page count + sizes). render = optional doc
        whose pages already have unsaved markup baked in, used only for thumbnails
        so they stay in sync with the page + live overlays."""
        self._doc = doc
        self._render = render
        self.refresh()

    def set_render_source(self, render):
        """Swap in a fresh markup-baked clone and re-render all thumbnails from it,
        so the panel reflects the current page content (e.g. after open/strip or a
        structural edit). Pass None to fall back to the live document."""
        self._render = render
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
            thumb = self._render_source().render_thumbnail(i, max_width=THUMB_W)
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
