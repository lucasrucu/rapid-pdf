from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QLabel,
    QStyledItemDelegate, QStyle, QStyleOptionViewItem, QToolButton, QMenu,
    QAbstractItemView,
)
from PySide6.QtCore import Signal, Qt, QSize, QTimer, QRect, QEvent, QPoint
from PySide6.QtGui import QIcon, QPixmap, QColor, QPainter, QPen, QDrag

from ui.thumbnails import aspect_ratio_placeholder, draw_thumbnail, fit_size
from ui.theme import LIGHT, themed_icon, qtawesome_available
from core.page_ops import move_rows


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
# Widest the drag pixmap is allowed to get. A full-size card stack under the
# cursor would blanket the strip it is being dragged through.
DRAG_PIXMAP_W = 108


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
    # Thickness of the line showing where a dragged page will land.
    _DROP_LINE_H = 3

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
        if not selected and self._is_current(index):
            # Ctrl-clicking the page you are viewing out of the selection would
            # otherwise leave nothing at all showing which page the editor is
            # on. An outline says "here" without competing with the fill.
            pen = QPen(self.sel_color)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(backing.adjusted(1, 1, -1, -1), 7, 7)

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
        self._paint_drop_line(painter, option, index)
        painter.restore()

    def _is_current(self, index) -> bool:
        view = self.parent()
        return bool(view is not None and hasattr(view, "currentRow")
                    and view.currentRow() == index.row())

    def _paint_drop_line(self, painter, option, index):
        """Draw the insertion indicator when a drag is hovering this gap.

        The strip is one column top to bottom, so the gap a page will land in is
        a horizontal line: above the row the drop resolves to, or below the last
        row when the drop is past the end.
        """
        target = getattr(self.parent(), "drag_target_row", lambda: None)()
        if target is None:
            return
        row = index.row()
        last = index.model().rowCount() - 1
        if row == target:
            y = option.rect.top()
        elif target > last and row == last:
            y = option.rect.bottom() - self._DROP_LINE_H
        else:
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.sel_color)
        painter.drawRoundedRect(
            QRect(option.rect.left() + self._PAD, y,
                  max(1, option.rect.width() - 2 * self._PAD), self._DROP_LINE_H),
            2, 2)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Set by PagePanel._apply_layout from the live viewport width.
        self.cell = QSize(ITEM_W, ITEM_H)

    def sizeHint(self, option, index):
        return QSize(self.cell)


class _PageList(QListWidget):
    """The thumbnail strip: multi-select, drag to reorder, Delete to remove.

    Reordering never moves rows in the widget. The drop works out the new page
    order arithmetically (core.page_ops.move_rows), hands it to the document,
    and the panel is rebuilt from the document afterwards. That keeps the model
    as the single source of truth (widget order and page order cannot drift),
    and it sidesteps the Qt bug the Organizer had to work around: Qt's native
    multi-row internal move resolves each moved row from a mime-encoded row list
    that goes stale as earlier rows are removed, which can hand a native
    takeItem an out-of-range index and take the process down C++-side.

    The drag itself still has to be ours rather than Qt's. After
    QAbstractItemView.startDrag's drag->exec() returns MoveAction and the view's
    own dropEvent did not perform the move, Qt "helpfully" removeRows() the
    current selection. Our dropEvent replaces the base implementation, so that
    cleanup would delete the very pages the user just moved. Owning the whole
    drag means nothing at all runs after exec().
    """

    # (new page order, the rows that were dragged) so the host can re-select the
    # moved block once it has rebuilt the strip from the document.
    reorder_requested = Signal(list, list)
    delete_requested = Signal()

    _STACK_OFFSET_PX = 5
    _STACK_MAX_CARDS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_row = None   # drop-target row while a drag is over the strip

    def drag_target_row(self):
        """Row the drop indicator points at, or None when nothing is dragging.
        Read by the delegate to draw the insertion line."""
        return self._drag_row

    def selected_rows(self) -> list:
        return sorted({self.row(i) for i in self.selectedItems()})

    # -- drag ---------------------------------------------------------------

    def startDrag(self, supportedActions):
        rows = self.selected_rows()
        if not rows:
            return
        mime = self.model().mimeData([self.model().index(r, 0) for r in rows])
        if mime is None:
            return
        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap, hotspot = self._stack_pixmap(rows)
        drag.setPixmap(pixmap)
        drag.setHotSpot(hotspot)
        drag.exec(supportedActions, Qt.DropAction.MoveAction)
        # Deliberately nothing after exec(): see the class docstring.

    def _stack_pixmap(self, rows):
        """A small fanned card stack of the dragged pages, with a count badge
        when there is more than one. Scaled down to DRAG_PIXMAP_W so it reads as
        "these pages" without covering the strip it is moving through."""
        shown = rows[:self._STACK_MAX_CARDS]
        off = self._STACK_OFFSET_PX
        extra = off * (len(shown) - 1)
        cell = self.itemDelegate().sizeHint(QStyleOptionViewItem(),
                                            self.model().index(rows[0], 0))
        cell_w, cell_h = max(1, cell.width()), max(1, cell.height())
        badge_d = 30
        margin = badge_d // 2 + 2
        canvas = QPixmap(cell_w + extra + margin, cell_h + extra + margin)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Back to front, so the first-selected card lands on top fully opaque.
        for depth, row in reversed(list(enumerate(shown))):
            item = self.item(row)
            if item is None:
                continue
            painter.setOpacity(1.0 if depth == 0 else 0.55)
            option = QStyleOptionViewItem()
            option.initFrom(self)
            option.rect = QRect(depth * off, depth * off + margin, cell_w, cell_h)
            option.state |= QStyle.StateFlag.State_Selected
            self.itemDelegate().paint(painter, option, self.indexFromItem(item))
        painter.setOpacity(1.0)
        if len(rows) > 1:
            bx, by = cell_w - badge_d // 2, 0
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._sel_color())
            painter.drawEllipse(bx, by, badge_d, badge_d)
            painter.setPen(QColor(_PageDelegate.sel_text_color))
            font = painter.font()
            font.setBold(True)
            font.setPointSize(10)
            painter.setFont(font)
            painter.drawText(QRect(bx, by, badge_d, badge_d),
                             Qt.AlignmentFlag.AlignCenter, str(len(rows)))
        painter.end()
        scaled = canvas.scaledToWidth(DRAG_PIXMAP_W,
                                      Qt.TransformationMode.SmoothTransformation)
        ratio = scaled.width() / max(1, canvas.width())
        hotspot = QPoint(int(cell_w * ratio / 2),
                         int((margin + cell_h / 2) * ratio))
        return scaled, hotspot

    def dragEnterEvent(self, event):
        if event.source() is not self:
            event.ignore()
            return
        # Qt's own handler is what arms the edge autoscroll, so a drag can reach
        # pages that are off the bottom of a long document. Left to ourselves we
        # would have to hand-roll a scroll timer for it.
        super().dragEnterEvent(event)
        event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.source() is not self:
            event.ignore()
            return
        super().dragMoveEvent(event)   # keeps the autoscroll fed
        event.acceptProposedAction()
        row = self._drop_row(self._event_pos(event))
        if row != self._drag_row:
            self._drag_row = row
            self.viewport().update()

    def dragLeaveEvent(self, event):
        self._clear_drag()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._clear_drag()
        # Internal reorder only. Ignoring a foreign drop means the source sees a
        # failed drop, so nothing anywhere gets removed.
        if event.source() is not self:
            event.ignore()
            return
        rows = self.selected_rows()
        if not rows:
            event.ignore()
            return
        order = move_rows(self.count(), rows, self._drop_row(self._event_pos(event)))
        event.acceptProposedAction()
        if order != list(range(self.count())):
            self.reorder_requested.emit(order, rows)

    @staticmethod
    def _event_pos(event):
        return event.position().toPoint() if hasattr(event, "position") else event.pos()

    def _drop_row(self, pos) -> int:
        """Insertion index the drop point resolves to, in the list as shown.

        One uniform column top to bottom (setUniformItemSizes), so the row is
        arithmetic off the pitch between two cells rather than a hit test. Two
        reasons not to use itemAt() here: it does not clip, so a point well past
        the last cell still comes back as a real row (which read as "drop after
        page 1" for a drop at the bottom of the strip), and it returns nothing at
        all in the few pixels of gap between cells, which is exactly where an
        insertion is being aimed. The split is the middle of the pitch: above it
        the pages land before that cell, below it after.
        """
        count = self.count()
        if count == 0:
            return 0
        first = self.visualItemRect(self.item(0))
        pitch = (self.visualItemRect(self.item(1)).top() - first.top()
                 if count > 1 else first.height())
        if pitch <= 0:
            return count
        offset = pos.y() - first.top()
        if offset < 0:
            return 0
        row = int(offset // pitch)
        if row >= count:
            return count
        return row + (1 if offset - row * pitch > pitch / 2 else 0)

    def _clear_drag(self):
        # Our dropEvent replaces the base one, which is where Qt would normally
        # stop the autoscroll it started; without this the strip keeps scrolling
        # after the drop.
        if hasattr(self, "stopAutoScroll"):
            self.stopAutoScroll()
        if self._drag_row is not None:
            self._drag_row = None
            self.viewport().update()

    def _sel_color(self):
        return QColor(_PageDelegate.sel_color)

    # -- keys ---------------------------------------------------------------

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class PagePanel(QWidget):
    page_selected = Signal(int)
    # Ascending rows the user asked to delete, and the full page order a drag
    # asked for. The host applies both to the document (as undoable commands)
    # and rebuilds this panel from it; the panel never edits pages itself.
    pages_delete_requested = Signal(list)
    pages_reorder_requested = Signal(list, list)

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
        if qtawesome_available():
            self._del_btn.setIcon(themed_icon("mdi6.trash-can-outline", palette.text))
        self._list.viewport().update()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 0, 0)
        header.setSpacing(2)
        self._title = QLabel("Pages")
        self._title.setStyleSheet("font-weight: bold; font-size: 10px;")
        header.addWidget(self._title)
        header.addStretch()

        # Always-visible delete affordance, so the feature is discoverable
        # without knowing about the Delete key or the right-click menu.
        self._del_btn = QToolButton()
        if qtawesome_available():
            self._del_btn.setIcon(themed_icon("mdi6.trash-can-outline", LIGHT.text))
            self._del_btn.setIconSize(QSize(14, 14))
        else:
            # No icon font on this machine: a labelled button beats an empty
            # square, and the strip is too narrow to carry both.
            self._del_btn.setText("Delete")
            self._del_btn.setStyleSheet("font-size: 10px;")
        self._del_btn.setAutoRaise(True)
        self._del_btn.setEnabled(False)
        self._del_btn.setToolTip("Delete the selected page(s)  (Del)")
        self._del_btn.clicked.connect(self._request_delete)
        header.addWidget(self._del_btn)
        layout.addLayout(header)

        self._list = _PageList()
        self._list.setIconSize(QSize(THUMB_W, THUMB_H))
        self._list.setSpacing(2)
        # ListMode + custom delegate: avoids the IconMode Qt quirk where the
        # selection highlight rect drifts below the visual item position.
        self._list.setViewMode(QListWidget.ViewMode.ListMode)
        self._list.setFlow(QListWidget.Flow.TopToBottom)
        self._list.setWrapping(False)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setUniformItemSizes(True)
        # Multi-select (shift for a run, ctrl to toggle one) plus drag to
        # reorder. The drop indicator is drawn by the delegate, not by Qt, so
        # it can be a full-width line in the strip's own accent colour.
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self._list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._list.setDragEnabled(True)
        self._list.viewport().setAcceptDrops(True)
        self._list.setDropIndicatorShown(False)
        # Wider than Qt's 16px default: the strip is tall and narrow, so the
        # band you have to hit to scroll a long document should be generous.
        self._list.setAutoScroll(True)
        self._list.setAutoScrollMargin(40)
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
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.delete_requested.connect(self._request_delete)
        self._list.reorder_requested.connect(self.pages_reorder_requested)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
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

    def set_render_source(self, render, current_page: int | None = None,
                          select: list | None = None):
        """Swap in a fresh markup-baked clone and re-render all thumbnails from it,
        so the panel reflects the current page content (e.g. after open/strip or a
        structural edit). Pass None to fall back to the live document.

        `current_page` / `select` put the user back where they were, which is
        what a page delete or reorder needs: one rebuild, not a rebuild plus a
        second pass to fix the highlight up afterwards."""
        self._render = render
        self.refresh(current_page=current_page, select=select)

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

    def refresh(self, current_page: int | None = None, select: list | None = None):
        """Populate the panel with placeholder items immediately; the real page
        thumbnails are rendered lazily, only for rows that are actually visible.

        Every structural edit ends here, rebuilt from the document, which is what
        keeps the strip's order and the document's order from ever drifting.
        `current_page` and `select` restore where the user was afterwards.
        """
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
                row = 0 if current_page is None else current_page
                self._list.setCurrentRow(max(0, min(row, self._list.count() - 1)))
                if select:
                    # The moved block is the selection; the current row only
                    # says which page the editor is on.
                    self._list.clearSelection()
                    for r in select:
                        if 0 <= r < self._list.count():
                            self._list.item(r).setSelected(True)
        self._list.blockSignals(False)
        self._on_selection_changed()
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
        # Already the current row: leave it alone, so a shift/ctrl selection
        # isn't collapsed by the page-change round trip it just caused.
        if page_num == self._list.currentRow():
            return
        self._list.blockSignals(True)
        self._list.setCurrentRow(page_num)
        self._list.blockSignals(False)
        self._on_selection_changed()
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

    def selected_rows(self) -> list:
        return self._list.selected_rows()

    def has_focus(self) -> bool:
        """True when the strip owns the keyboard, so the window can route Delete
        to pages instead of to the canvas."""
        return self._list.hasFocus() or self._list.viewport().hasFocus()

    def _on_row_changed(self, row: int):
        if row >= 0:
            self.page_selected.emit(row)

    def _on_selection_changed(self):
        rows = self._list.selected_rows()
        self._del_btn.setEnabled(bool(rows))
        self._title.setText(f"{len(rows)} selected" if len(rows) > 1 else "Pages")

    def _request_delete(self):
        rows = self._list.selected_rows()
        if rows:
            self.pages_delete_requested.emit(rows)

    def _show_context_menu(self, pos: QPoint):
        item = self._list.itemAt(pos)
        # Right-clicking outside the current selection moves the selection there
        # first, the way every file manager does, so the menu always acts on what
        # is highlighted.
        if item is not None and not item.isSelected():
            self._list.setCurrentItem(item)
        rows = self._list.selected_rows()
        if not rows:
            return
        menu = QMenu(self._list)
        label = "Delete Page" if len(rows) == 1 else f"Delete {len(rows)} Pages"
        menu.addAction(label, self._request_delete)
        menu.addSeparator()
        menu.addAction("Select All Pages", self._list.selectAll)
        menu.exec(self._list.viewport().mapToGlobal(pos))
