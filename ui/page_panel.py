from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem, QLabel,
    QStyledItemDelegate, QStyle,
)
from PySide6.QtCore import Signal, Qt, QSize, QTimer, QRect, QEvent
from PySide6.QtGui import QIcon, QPixmap, QColor

from ui.thumbnails import aspect_ratio_placeholder, draw_thumbnail, fit_size
from ui.theme import LIGHT


# Reference cell proportions. The ACTUAL cell width is derived from the list
# viewport at runtime (see _apply_layout) so thumbnails always fit the panel
# exactly: no horizontal scrollbar, no sideways jiggle.
THUMB_W = 100
THUMB_H = 130
ITEM_W = 122
ITEM_H = 170
_TEXT_H = 18
PANEL_W = 150   # fixed panel width (scrollbar gutter is always reserved)
# Render thumbnails this many pixels above/below the viewport so they're ready
# just before they scroll into view.
PREFETCH_PX = 300


class _PageDelegate(QStyledItemDelegate):
    """Draw thumbnail above label, selection highlight covering the whole cell.

    IconMode has a Qt quirk where the selection rect drifts away from the visual
    item position when icon sizes vary. ListMode + this delegate is pixel-perfect.
    """

    # Themed by PagePanel.apply_palette(); these LIGHT defaults only cover the
    # moment before the first call.
    sel_color = QColor(LIGHT.accent)
    hover_color = QColor(LIGHT.surface_hover)
    text_color = QColor(LIGHT.text_dim)
    sel_text_color = QColor(LIGHT.accent_text)

    # Even inset of the selection/hover backing inside the cell (all four sides),
    # so the rounded accent wraps the whole thumbnail evenly instead of bleeding
    # to the cell edges. Gap between the thumbnail and its page-number label.
    _PAD = 4
    _LABEL_GAP = 4

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        # Backing rect: evenly inset from the cell on all four sides → equal
        # padding around the thumbnail in every direction.
        backing = option.rect.adjusted(self._PAD, self._PAD, -self._PAD, -self._PAD)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.sel_color)
            painter.drawRoundedRect(backing, 8, 8)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.hover_color)
            painter.drawRoundedRect(backing, 8, 8)

        inner = backing.adjusted(4, 4, -4, -4)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None:
            thumb_area = QRect(inner.x(), inner.y(), inner.width(),
                               max(1, inner.height() - _TEXT_H - self._LABEL_GAP))
            draw_thumbnail(painter, icon, thumb_area)

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

    def __init__(self, parent=None):
        super().__init__(parent)
        # Set by PagePanel._apply_layout from the live viewport width.
        self.cell = QSize(ITEM_W, ITEM_H)

    def sizeHint(self, option, index):
        return QSize(self.cell)


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
        self._placeholder_cache: dict[tuple[int, int], QPixmap] = {}
        self._placeholder_color = QColor(LIGHT.surface_raised)  # themed via apply_palette()
        # Live thumbnail box, derived from the viewport (see _apply_layout). It is
        # the delegate's thumb_area, so a thumbnail rendered to it always fits
        # inside its own selection border.
        self._thumb_w = THUMB_W
        self._thumb_h = THUMB_H
        self._setup_ui()
        self.setFixedWidth(PANEL_W)

    def _render_source(self):
        """Doc to rasterise thumbnails from: the markup-baked clone if present,
        else the live document."""
        return self._render or self._doc

    def _placeholder_for(self, page_num: int) -> QPixmap:
        """A grey placeholder sized to the page's real aspect ratio, so a landscape
        drawing's thumbnail doesn't visibly change shape when it renders."""
        return aspect_ratio_placeholder(
            self._doc, page_num, self._thumb_w, self._thumb_h,
            self._placeholder_color, self._placeholder_cache,
        )

    def apply_palette(self, palette):
        """Theme the delegate (selection/hover/label) and placeholder fill, then
        repaint. Called once at start and on every light/dark toggle."""
        _PageDelegate.sel_color = QColor(palette.accent)
        _PageDelegate.hover_color = QColor(palette.surface_hover)
        _PageDelegate.text_color = QColor(palette.text_dim)
        _PageDelegate.sel_text_color = QColor(palette.accent_text)
        self._placeholder_color = QColor(palette.surface_raised)
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
        self._delegate = _PageDelegate(self._list)
        self._list.setItemDelegate(self._delegate)
        # No sideways movement, ever: the horizontal bar is off and cells are
        # sized to the viewport (see _apply_layout). The vertical bar is always
        # visible so its appearing/disappearing can't change the viewport width
        # (that width flip-flop was the old horizontal jiggle).
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        # Re-fit cells whenever the viewport geometry changes (debounced).
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(60)
        self._relayout_timer.timeout.connect(self._apply_layout)
        self._list.viewport().installEventFilter(self)
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

    def eventFilter(self, obj, event):
        if obj is self._list.viewport() and event.type() == QEvent.Type.Resize:
            self._relayout_timer.start()
        return super().eventFilter(obj, event)

    def _cell_size(self) -> QSize:
        """Cell sized so the item exactly fits the viewport width (list spacing
        on both sides), with the height scaled to keep the reference shape."""
        vw = self._list.viewport().width()
        cell_w = max(60, vw - 2 * self._list.spacing())
        return QSize(cell_w, int(cell_w * ITEM_H / ITEM_W))

    def _adopt_cell(self, cell: QSize):
        """Take `cell` as the cell size and derive the thumbnail box from it.

        The box is the delegate's thumb_area exactly, worked out from the same
        padding constants the delegate paints with, so the rendered thumbnail
        and the border drawn around it can never disagree. Single seam: both
        refresh() and _apply_layout() go through here.
        """
        self._delegate.cell = cell
        pad = 2 * (_PageDelegate._PAD + 4)   # backing inset + inner inset, both sides
        self._thumb_w = max(40, cell.width() - pad)
        self._thumb_h = max(40, cell.height() - pad - _TEXT_H
                            - _PageDelegate._LABEL_GAP)
        self._list.setIconSize(QSize(self._thumb_w, self._thumb_h))
        self._placeholder_cache.clear()

    def _apply_layout(self):
        """Fit cells to the current viewport width and re-render at that size."""
        cell = self._cell_size()
        if cell == self._delegate.cell:
            return
        self._adopt_cell(cell)
        self._rendered.clear()   # thumbnails must be re-rendered at the new width
        for i in range(self._list.count()):
            self._list.item(i).setSizeHint(cell)
        self._render_visible()

    def refresh(self):
        """Populate the panel with placeholder items immediately; the real page
        thumbnails are rendered lazily, only for rows that are actually visible."""
        self._list.blockSignals(True)
        self._list.clear()
        self._rendered.clear()
        cell = self._cell_size()
        self._adopt_cell(cell)
        if self._doc:
            for i in range(self._doc.page_count()):
                item = QListWidgetItem(QIcon(self._placeholder_for(i)), str(i + 1))
                item.setSizeHint(cell)
                item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
                self._list.addItem(item)
            if self._list.count() > 0:
                self._list.setCurrentRow(0)
        self._list.blockSignals(False)
        # Render the currently visible thumbnails now; once more after layout settles.
        self._render_visible()
        QTimer.singleShot(0, self._render_visible)

    def _render_width(self, page_num: int) -> int:
        """Width to rasterise page `page_num` at so it fits the box in BOTH axes.

        Rendering every page at the full box width overshoots for anything taller
        than the box (a portrait page comes out taller than the cell), and those
        extra pixels only get thrown away again when the delegate fits it into
        the cell. Ask for the fitted width up front instead.
        """
        if self._doc:
            w_pt, h_pt = self._doc.get_page_size(page_num)
            if w_pt > 0 and h_pt > 0:
                return fit_size(w_pt, h_pt, self._thumb_w, self._thumb_h)[0]
        return self._thumb_w

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
            thumb = self._render_source().render_thumbnail(
                i, max_width=self._render_width(i))
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
        return self._thumb_w

    def _on_row_changed(self, row: int):
        if row >= 0:
            self.page_selected.emit(row)
