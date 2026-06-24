import fitz
from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsLineItem,
    QGraphicsPixmapItem, QGraphicsTextItem, QGraphicsItem,
    QInputDialog, QStyle, QApplication,
)
from PySide6.QtCore import Qt, QRectF, QPointF, QLineF, Signal, QBuffer, QIODevice, QTimer
from PySide6.QtGui import (
    QPen, QBrush, QColor, QPainter, QFont, QPixmap,
    QUndoStack, QUndoCommand,
)

HANDLE_SIZE = 8


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------

class AnnotationBase:
    """Common interface mixed into every annotation item class."""
    page_num: int = 0
    ann_type: str = ""
    text: str = ""

    def to_annotation_dict(self, zoom: float) -> dict:
        raise NotImplementedError

    def clone(self) -> "AnnotationBase":
        raise NotImplementedError

    def set_color(self, c: QColor): pass
    def set_stroke_color(self, c: QColor): self.set_color(c)
    def set_fill_color(self, c: QColor): pass
    def set_opacity(self, o: float): pass
    def set_fill(self, enabled: bool, color: QColor): pass
    def set_line_width(self, w: float): pass
    def set_font_size(self, size: int): pass
    def set_font_color(self, c: QColor): pass


# ---------------------------------------------------------------------------
# Rect-based items
# ---------------------------------------------------------------------------

class AnnotationItem(QGraphicsRectItem, AnnotationBase):
    """Base for rectangle-shaped annotation canvas items."""

    def __init__(self, rect: QRectF, page_num: int, ann_type: str):
        super().__init__(rect)
        self.page_num = page_num
        self.ann_type = ann_type
        self.text = ""
        self._font_size = 12
        self._font_color = QColor(0, 0, 0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)

    def set_font_size(self, size: int):
        self._font_size = size
        self.update()

    def set_font_color(self, c: QColor):
        self._font_color = QColor(c)
        self.update()

    def boundingRect(self) -> QRectF:
        extra = HANDLE_SIZE + 2
        return super().boundingRect().adjusted(-extra, -extra, extra, extra)

    def scene_rect(self) -> QRectF:
        r = self.rect()
        tl = self.mapToScene(r.topLeft())
        br = self.mapToScene(r.bottomRight())
        return QRectF(tl, br).normalized()

    def to_annotation_dict(self, zoom: float) -> dict:
        r = self.scene_rect()
        fitz_rect = fitz.Rect(
            r.x() / zoom, r.y() / zoom,
            r.right() / zoom, r.bottom() / zoom,
        )
        d = {"type": self.ann_type, "fitz_rect": fitz_rect}
        if self.text:
            d["text"] = self.text
            d["font_size"] = self._font_size
            fc = self._font_color
            d["font_color"] = (fc.redF(), fc.greenF(), fc.blueF())
        return d

    def clone(self) -> "AnnotationItem":
        raise NotImplementedError

    def paint(self, painter: QPainter, option, widget=None):
        saved = option.state
        option.state = option.state & ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)
        option.state = saved
        if self.isSelected():
            self._draw_handles(painter)
        if self.text:
            self._draw_text(painter)

    def _draw_handles(self, painter: QPainter):
        r = self.rect()
        cx, cy = r.center().x(), r.center().y()
        hs = HANDLE_SIZE
        corners = [
            (r.left(), r.top()), (cx, r.top()), (r.right(), r.top()),
            (r.right(), cy), (r.right(), r.bottom()), (cx, r.bottom()),
            (r.left(), r.bottom()), (r.left(), cy),
        ]
        painter.save()
        painter.setPen(QPen(QColor(0, 120, 215), 1))
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        for x, y in corners:
            painter.drawRect(QRectF(x - hs / 2, y - hs / 2, hs, hs))
        painter.restore()

    def _draw_text(self, painter: QPainter):
        r = self.rect()
        if r.width() < 16 or r.height() < 16:
            return
        inner = r.adjusted(6, 6, -6, -6)
        painter.save()
        painter.setFont(QFont("Arial", self._font_size))
        painter.setPen(QPen(self._font_color))
        painter.drawText(
            inner,
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            self.text,
        )
        painter.restore()


class HighlightItem(AnnotationItem):
    def __init__(self, rect: QRectF, color: QColor, opacity: float, page_num: int):
        super().__init__(rect, page_num, "highlight")
        self._color = QColor(color)
        self._opacity = opacity
        self._apply_style()

    def _apply_style(self):
        c = QColor(self._color)
        c.setAlphaF(self._opacity)
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setBrush(QBrush(c))

    def set_color(self, c: QColor):
        self._color = QColor(c)
        self._apply_style()
        self.update()

    def set_opacity(self, o: float):
        self._opacity = o
        self._apply_style()
        self.update()

    def to_annotation_dict(self, zoom: float) -> dict:
        d = super().to_annotation_dict(zoom)
        c = self._color
        d["color"] = (c.redF(), c.greenF(), c.blueF())
        d["opacity"] = self._opacity
        return d

    def clone(self) -> "HighlightItem":
        item = HighlightItem(self.rect(), self._color, self._opacity, self.page_num)
        item.setPos(self.pos())
        item.text = self.text
        item._font_size = self._font_size
        item._font_color = QColor(self._font_color)
        return item


class RectAnnotationItem(AnnotationItem):
    def __init__(self, rect: QRectF, stroke_color: QColor, fill_color: QColor | None,
                 opacity: float, page_num: int, line_width: float = 2.0):
        super().__init__(rect, page_num, "rect")
        self._stroke = QColor(stroke_color)
        self._fill = QColor(fill_color) if fill_color else None
        self._opacity = opacity
        self._line_width = line_width
        self._apply_style()

    def _apply_style(self):
        borderless = self._line_width <= 0
        if borderless:
            self.setPen(QPen(Qt.PenStyle.NoPen))
        else:
            pen_c = QColor(self._stroke)
            pen_c.setAlphaF(self._opacity)
            self.setPen(QPen(pen_c, self._line_width))
        if self._fill:
            fill_c = QColor(self._fill)
            # A borderless filled rect acts as a highlighter → honor opacity directly;
            # a bordered rect keeps a lighter fill so the outline stays readable.
            fill_c.setAlphaF(self._opacity if borderless else self._opacity * 0.4)
            self.setBrush(QBrush(fill_c))
        else:
            self.setBrush(QBrush(Qt.BrushStyle.NoBrush))

    def set_color(self, c: QColor):
        self._stroke = QColor(c)
        if self._fill:
            self._fill = QColor(c)
        self._apply_style()
        self.update()

    def set_stroke_color(self, c: QColor):
        """Recolor just the outline, leaving any fill untouched."""
        self._stroke = QColor(c)
        self._apply_style()
        self.update()

    def set_fill_color(self, c: QColor):
        """Set (and enable) the fill color, leaving the outline untouched."""
        self._fill = QColor(c)
        self._apply_style()
        self.update()

    def set_opacity(self, o: float):
        self._opacity = o
        self._apply_style()
        self.update()

    def set_fill(self, enabled: bool, color: QColor):
        self._fill = QColor(color) if enabled else None
        self._apply_style()
        self.update()

    def set_line_width(self, w: float):
        self._line_width = w
        self._apply_style()
        self.update()

    def to_annotation_dict(self, zoom: float) -> dict:
        d = super().to_annotation_dict(zoom)
        sc = self._stroke
        d["stroke_color"] = (sc.redF(), sc.greenF(), sc.blueF())
        if self._fill:
            fc = self._fill
            d["fill_color"] = (fc.redF(), fc.greenF(), fc.blueF())
        d["opacity"] = self._opacity
        d["line_width"] = self._line_width
        return d

    def clone(self) -> "RectAnnotationItem":
        item = RectAnnotationItem(
            self.rect(), self._stroke, self._fill,
            self._opacity, self.page_num, self._line_width,
        )
        item.setPos(self.pos())
        item.text = self.text
        item._font_size = self._font_size
        item._font_color = QColor(self._font_color)
        return item


class ImageAnnotationItem(AnnotationItem):
    """Clipboard image rendered inside a resizable rect."""

    def __init__(self, pixmap: QPixmap, image_bytes: bytes, rect: QRectF, page_num: int):
        super().__init__(rect, page_num, "image")
        self._pixmap = pixmap
        self._image_bytes = image_bytes

    def paint(self, painter: QPainter, option, widget=None):
        painter.drawPixmap(self.rect().toRect(), self._pixmap)
        if self.isSelected():
            painter.save()
            painter.setPen(QPen(QColor(0, 120, 215), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.rect())
            painter.restore()
            self._draw_handles(painter)

    def to_annotation_dict(self, zoom: float) -> dict:
        d = super().to_annotation_dict(zoom)
        d["image_bytes"] = self._image_bytes
        return d

    def clone(self) -> "ImageAnnotationItem":
        item = ImageAnnotationItem(self._pixmap, self._image_bytes, self.rect(), self.page_num)
        item.setPos(self.pos())
        return item


# ---------------------------------------------------------------------------
# Line item
# ---------------------------------------------------------------------------

class LineAnnotationItem(QGraphicsLineItem, AnnotationBase):
    """A straight line annotation."""

    def __init__(self, line: QLineF, color: QColor, opacity: float,
                 page_num: int, line_width: float = 2.0):
        super().__init__(line)
        self.page_num = page_num
        self.ann_type = "line"
        self.text = ""
        self._color = QColor(color)
        self._opacity = opacity
        self._line_width = line_width
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self._apply_style()

    def boundingRect(self) -> QRectF:
        extra = HANDLE_SIZE + 2
        return super().boundingRect().adjusted(-extra, -extra, extra, extra)

    def _apply_style(self):
        c = QColor(self._color)
        c.setAlphaF(self._opacity)
        self.setPen(QPen(c, self._line_width, Qt.PenStyle.SolidLine,
                         Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))

    def set_color(self, c: QColor):
        self._color = QColor(c)
        self._apply_style()
        self.update()

    def set_opacity(self, o: float):
        self._opacity = o
        self._apply_style()
        self.update()

    def set_line_width(self, w: float):
        self._line_width = w
        self._apply_style()
        self.update()

    def to_annotation_dict(self, zoom: float) -> dict:
        ln = self.line()
        p1 = self.mapToScene(ln.p1())
        p2 = self.mapToScene(ln.p2())
        return {
            "type": "line",
            "p1": fitz.Point(p1.x() / zoom, p1.y() / zoom),
            "p2": fitz.Point(p2.x() / zoom, p2.y() / zoom),
            "color": (self._color.redF(), self._color.greenF(), self._color.blueF()),
            "opacity": self._opacity,
            "line_width": self._line_width,
        }

    def clone(self) -> "LineAnnotationItem":
        item = LineAnnotationItem(
            self.line(), self._color, self._opacity, self.page_num, self._line_width
        )
        item.setPos(self.pos())
        return item

    def paint(self, painter: QPainter, option, widget=None):
        saved = option.state
        option.state = option.state & ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)
        option.state = saved
        if self.isSelected():
            ln = self.line()
            hs = HANDLE_SIZE
            painter.save()
            painter.setPen(QPen(QColor(0, 120, 215), 1))
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            for pt in [ln.p1(), ln.p2()]:
                painter.drawRect(QRectF(pt.x() - hs / 2, pt.y() - hs / 2, hs, hs))
            painter.restore()


# ---------------------------------------------------------------------------
# Text item
# ---------------------------------------------------------------------------

class TextAnnotationItem(QGraphicsTextItem, AnnotationBase):
    """Free-floating text label with configurable font size."""

    def __init__(self, pos: QPointF, text: str, color: QColor, font_size: int, page_num: int):
        super().__init__(text)
        self.page_num = page_num
        self.ann_type = "text"
        self.text = text
        self._color = QColor(color)
        self._font_size = font_size
        self._apply_style()
        self.setPos(pos)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setAcceptHoverEvents(True)

    def _apply_style(self):
        self.setDefaultTextColor(self._color)
        self.setFont(QFont("Arial", self._font_size))

    def set_color(self, c: QColor):
        self._color = QColor(c)
        self._apply_style()
        self.update()

    # For a free-floating text label the text color *is* its color.
    def set_font_color(self, c: QColor):
        self.set_color(c)

    def set_font_size(self, size: int):
        self._font_size = size
        self._apply_style()
        self.update()

    def boundingRect(self) -> QRectF:
        extra = HANDLE_SIZE + 2
        return QGraphicsTextItem.boundingRect(self).adjusted(-extra, -extra, extra, extra)

    def paint(self, painter: QPainter, option, widget=None):
        saved = option.state
        option.state = option.state & ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)
        option.state = saved
        if self.isSelected():
            br = QGraphicsTextItem.boundingRect(self)
            hs = HANDLE_SIZE
            painter.save()
            painter.setPen(QPen(QColor(0, 120, 215), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(br.adjusted(1, 1, -1, -1))
            painter.setPen(QPen(QColor(0, 120, 215), 1))
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            for x, y in [
                (br.left(), br.top()), (br.right(), br.top()),
                (br.left(), br.bottom()), (br.right(), br.bottom()),
            ]:
                painter.drawRect(QRectF(x - hs / 2, y - hs / 2, hs, hs))
            painter.restore()

    def to_annotation_dict(self, zoom: float) -> dict:
        pos = self.pos()
        br = QGraphicsTextItem.boundingRect(self)
        fitz_rect = fitz.Rect(
            pos.x() / zoom, pos.y() / zoom,
            (pos.x() + br.width()) / zoom, (pos.y() + br.height()) / zoom,
        )
        return {
            "type": "text",
            "fitz_rect": fitz_rect,
            "text": self.toPlainText(),
            "color": (self._color.redF(), self._color.greenF(), self._color.blueF()),
            "font_size": self._font_size,
        }

    def clone(self) -> "TextAnnotationItem":
        return TextAnnotationItem(
            self.pos(), self.toPlainText(), self._color, self._font_size, self.page_num
        )


# ---------------------------------------------------------------------------
# Undo/redo — snapshots + commands
# ---------------------------------------------------------------------------

def style_snapshot(item) -> dict:
    """Capture every style/text attribute an annotation item might carry."""
    snap = {}
    for attr in ("_color", "_stroke", "_fill", "_opacity", "_line_width", "_font_size", "_font_color"):
        if hasattr(item, attr):
            v = getattr(item, attr)
            snap[attr] = QColor(v) if isinstance(v, QColor) else v
    snap["text"] = getattr(item, "text", "")
    if isinstance(item, TextAnnotationItem):
        snap["_plain"] = item.toPlainText()
    return snap


def style_restore(item, snap: dict):
    for attr, v in snap.items():
        if attr in ("text", "_plain"):
            continue
        setattr(item, attr, QColor(v) if isinstance(v, QColor) else v)
    item.text = snap.get("text", "")
    if isinstance(item, TextAnnotationItem):
        item.setPlainText(snap.get("_plain", ""))
        item.text = snap.get("_plain", "")
    if hasattr(item, "_apply_style"):
        item._apply_style()
    item.update()


def geometry_snapshot(item) -> dict:
    if isinstance(item, LineAnnotationItem):
        return {"line": QLineF(item.line()), "pos": QPointF(item.pos())}
    return {"rect": QRectF(item.rect()), "pos": QPointF(item.pos())}


def geometry_restore(item, snap: dict):
    if "line" in snap:
        item.setLine(snap["line"])
    else:
        item.setRect(snap["rect"])
    item.setPos(snap["pos"])
    item.update()


class _Command(QUndoCommand):
    """Base for canvas edits. The edit is already applied live when the command is
    constructed, so the first redo() (fired by QUndoStack.push) is a no-op."""

    def __init__(self, canvas, text: str):
        super().__init__(text)
        self._canvas = canvas
        self._skip_redo = True

    def redo(self):
        if self._skip_redo:
            self._skip_redo = False
            return
        self._do_redo()
        self._canvas.annotation_changed.emit()

    def undo(self):
        self._do_undo()
        self._canvas.annotation_changed.emit()

    def _do_redo(self): ...
    def _do_undo(self): ...


class AddItemsCommand(_Command):
    def __init__(self, canvas, items, text="Add"):
        super().__init__(canvas, text)
        self._items = list(items)

    def _do_redo(self):
        self._canvas._scene.clearSelection()
        for it in self._items:
            self._canvas._attach_item(it)
            it.setSelected(True)

    def _do_undo(self):
        for it in self._items:
            self._canvas._detach_item(it)


class RemoveItemsCommand(_Command):
    def __init__(self, canvas, items, text="Delete"):
        super().__init__(canvas, text)
        self._items = list(items)

    def _do_redo(self):
        for it in self._items:
            self._canvas._detach_item(it)

    def _do_undo(self):
        self._canvas._scene.clearSelection()
        for it in self._items:
            self._canvas._attach_item(it)
            it.setSelected(True)


class MoveCommand(_Command):
    def __init__(self, canvas, items, dx, dy, text="Move"):
        super().__init__(canvas, text)
        self._items = list(items)
        self._dx, self._dy = dx, dy

    def _do_redo(self):
        for it in self._items:
            it.moveBy(self._dx, self._dy)

    def _do_undo(self):
        for it in self._items:
            it.moveBy(-self._dx, -self._dy)


class NudgeCommand(MoveCommand):
    """Like MoveCommand but consecutive nudges of the same item set collapse into one."""

    def __init__(self, canvas, items, dx, dy):
        super().__init__(canvas, items, dx, dy, text="Nudge")

    def id(self) -> int:
        return 0x4E5544  # "NUD"

    def mergeWith(self, other) -> bool:
        if set(map(id, self._items)) != set(map(id, other._items)):
            return False
        self._dx += other._dx
        self._dy += other._dy
        return True


class ResizeCommand(_Command):
    def __init__(self, canvas, item, before, after, text="Resize"):
        super().__init__(canvas, text)
        self._item = item
        self._before, self._after = before, after

    def _do_redo(self):
        geometry_restore(self._item, self._after)

    def _do_undo(self):
        geometry_restore(self._item, self._before)


class StyleCommand(_Command):
    def __init__(self, canvas, before, after, text="Change style"):
        super().__init__(canvas, text)
        self._before = before  # list of (item, snapshot)
        self._after = after

    def _do_redo(self):
        for it, snap in self._after:
            style_restore(it, snap)

    def _do_undo(self):
        for it, snap in self._before:
            style_restore(it, snap)


# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------

class PDFCanvas(QGraphicsView):
    annotation_changed = Signal()
    selection_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._scene.selectionChanged.connect(self._on_selection_changed)

        self._doc = None
        self._current_page = 0
        self._zoom = 1.5
        self._bg_item: QGraphicsPixmapItem | None = None

        self._page_annotations: dict[int, list] = {}

        # Embedded images on the CURRENT page, as (xref, fitz.Rect in PDF coords).
        # Recomputed on every page load; clicking one "lifts" it into a movable object.
        self._embedded_images: list = []

        # Tool state — stroke (outline/line) and fill are now independent colors.
        self._tool = "select"
        self._stroke_color = QColor(255, 255, 0)
        self._fill_color = QColor(255, 255, 0)
        self._opacity = 0.5
        self._fill_enabled = False
        self._line_width = 2.0
        self._font_size = 12
        self._font_color = QColor(0, 0, 0)

        # Drawing state
        self._drawing = False
        self._draw_start: QPointF | None = None
        self._temp_item = None

        # Move/drag state (a drag moves every item in _drag_items together)
        self._drag_items: list = []
        self._drag_start: QPointF | None = None
        self._drag_total = QPointF(0, 0)
        self._drag_moved = False
        self._drag_is_lift = False        # dragging a freshly lifted image (not undoable)
        self._duplicating = False
        self._dup_clones: list = []
        # Net displacement is computed from a fixed anchor so Shift can axis-lock it.
        self._drag_anchor: QPointF | None = None
        self._drag_applied = QPointF(0, 0)

        # Marquee (rubber-band) selection state
        self._press_empty_pos: QPointF | None = None
        self._press_additive = False
        self._rubber_item: QGraphicsRectItem | None = None

        # Resize state
        self._resize_item: AnnotationBase | None = None
        self._resize_handle: str | None = None
        self._resize_orig_pos: QPointF | None = None
        self._resize_orig_rect: QRectF | None = None
        self._resize_before: dict | None = None

        # Undo/redo
        self._undo_stack = QUndoStack(self)

        # In-app annotation clipboard (clones of copied items)
        self._clipboard_items: list = []

        # Copy-confirmation flash: a brief pulsing outline over the copied items
        self._flash_items: list = []
        self._flash_step = 0
        self._flash_total = 8
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(40)
        self._flash_timer.timeout.connect(self._on_flash_tick)

        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        # NoAnchor: wheelEvent re-anchors the zoom under the cursor manually so that
        # Ctrl+scroll keeps the point under the mouse fixed instead of drifting.
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(QColor(55, 55, 55)))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def undo_stack(self) -> QUndoStack:
        return self._undo_stack

    def _push(self, command):
        self._undo_stack.push(command)

    def _attach_item(self, item):
        """Add an item to the scene and its page's annotation list (idempotent)."""
        if item.scene() is None:
            self._scene.addItem(item)
        lst = self._page_annotations.setdefault(item.page_num, [])
        if item not in lst:
            lst.append(item)
        item.setVisible(item.page_num == self._current_page)

    def _detach_item(self, item):
        """Remove an item from the scene and its page's annotation list."""
        if item.scene() is not None:
            self._scene.removeItem(item)
        lst = self._page_annotations.get(item.page_num, [])
        if item in lst:
            lst.remove(item)

    def _cancel_interaction(self):
        """Drop any in-progress marquee/drag so a page or tool switch starts clean."""
        if self._rubber_item is not None and self._rubber_item.scene() is not None:
            self._scene.removeItem(self._rubber_item)
        self._rubber_item = None
        self._press_empty_pos = None
        self._drag_items = []
        self._drag_start = None
        self._drag_moved = False
        self._duplicating = False
        self._dup_clones = []
        self._drag_anchor = None
        self._drag_applied = QPointF(0, 0)

    def set_document(self, doc):
        self._doc = doc
        self._page_annotations.clear()
        self._embedded_images = []
        self._current_page = 0
        self._scene.clear()
        self._bg_item = None
        self._undo_stack.clear()
        self._cancel_interaction()
        if doc and doc.page_count() > 0:
            self._load_page(0)

    def set_page(self, page_num: int):
        if not self._doc or page_num == self._current_page:
            return
        if not (0 <= page_num < self._doc.page_count()):
            return
        self._cancel_interaction()
        for item in self._page_annotations.get(self._current_page, []):
            item.setVisible(False)
        self._current_page = page_num
        self._load_page(page_num)

    def set_tool(self, tool: str):
        self._cancel_interaction()
        self._tool = tool
        _cursors = {
            "select": Qt.CursorShape.ArrowCursor,
            "text":   Qt.CursorShape.IBeamCursor,
        }
        self.setCursor(_cursors.get(tool, Qt.CursorShape.CrossCursor))

    def _apply_style_change(self, items, fn, text: str):
        """Apply fn to each item and record one undoable StyleCommand."""
        if not items:
            return
        before = [(it, style_snapshot(it)) for it in items]
        for it in items:
            fn(it)
        after = [(it, style_snapshot(it)) for it in items]
        self._push(StyleCommand(self, before, after, text))
        self.annotation_changed.emit()

    def set_stroke_color(self, color: QColor):
        """Outline color for rectangles / color for lines (the 'Line' control)."""
        self._stroke_color = QColor(color)
        items = [i for i in self._scene.selectedItems()
                 if isinstance(i, (RectAnnotationItem, LineAnnotationItem, HighlightItem))]
        self._apply_style_change(items, lambda it: it.set_stroke_color(color), "Change line color")

    def set_fill_color(self, color: QColor):
        """Fill color for rectangles (the 'Fill' control). Picking a color enables fill."""
        self._fill_color = QColor(color)
        self._fill_enabled = True
        items = [i for i in self._scene.selectedItems() if isinstance(i, RectAnnotationItem)]
        self._apply_style_change(items, lambda it: it.set_fill_color(color), "Change fill color")

    def set_opacity(self, opacity: float):
        self._opacity = opacity
        items = [i for i in self._scene.selectedItems() if isinstance(i, AnnotationBase)]
        self._apply_style_change(items, lambda it: it.set_opacity(opacity), "Change opacity")

    def set_fill_enabled(self, enabled: bool):
        self._fill_enabled = enabled
        items = [i for i in self._scene.selectedItems() if isinstance(i, RectAnnotationItem)]
        self._apply_style_change(items, lambda it: it.set_fill(enabled, self._fill_color), "Toggle fill")

    def set_line_width(self, width: float):
        self._line_width = width
        items = [i for i in self._scene.selectedItems()
                 if isinstance(i, (RectAnnotationItem, LineAnnotationItem))]
        self._apply_style_change(items, lambda it: it.set_line_width(width), "Change line width")

    def set_font_size(self, size: int):
        self._font_size = size
        items = [i for i in self._scene.selectedItems()
                 if isinstance(i, TextAnnotationItem)
                 or (isinstance(i, AnnotationItem) and not isinstance(i, ImageAnnotationItem))]
        self._apply_style_change(items, lambda it: it.set_font_size(size), "Change font size")

    def set_font_color(self, color: QColor):
        self._font_color = QColor(color)
        items = [i for i in self._scene.selectedItems()
                 if isinstance(i, TextAnnotationItem)
                 or (isinstance(i, AnnotationItem) and not isinstance(i, ImageAnnotationItem))]
        self._apply_style_change(items, lambda it: it.set_font_color(color), "Change font color")

    def delete_selected(self):
        items = [i for i in self._scene.selectedItems() if isinstance(i, AnnotationBase)]
        if not items:
            return
        for item in items:
            self._detach_item(item)
        self._push(RemoveItemsCommand(self, items))
        self.annotation_changed.emit()

    def copy_selected(self):
        """Snapshot the current selection into the in-app annotation clipboard."""
        selected = [i for i in self._scene.selectedItems() if isinstance(i, AnnotationBase)]
        self._clipboard_items = [i.clone() for i in selected]
        if selected:
            self._flash(selected)
        return len(self._clipboard_items)

    def _flash(self, items):
        """Briefly pulse an outline around items to confirm a copy (no text popup)."""
        self._flash_items = list(items)
        self._flash_step = 0
        self._flash_timer.start()
        self.viewport().update()

    def _on_flash_tick(self):
        self._flash_step += 1
        if self._flash_step >= self._flash_total:
            self._flash_timer.stop()
            self._flash_items = []
        self.viewport().update()

    def drawForeground(self, painter: QPainter, rect: QRectF):
        super().drawForeground(painter, rect)
        if not self._flash_items:
            return
        frac = 1.0 - (self._flash_step / self._flash_total)  # 1 → 0 as it fades
        margin = 3 + self._flash_step * 2                     # expands outward
        col = QColor(0, 120, 215)
        col.setAlphaF(max(0.0, frac))
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(col, 2.5))
        for it in self._flash_items:
            if it.scene() is None or not it.isVisible():
                continue
            r = it.sceneBoundingRect().adjusted(-margin, -margin, margin, margin)
            painter.drawRoundedRect(r, 4, 4)
        painter.restore()

    def has_clipboard_items(self) -> bool:
        return bool(self._clipboard_items)

    def paste_clipboard_items(self):
        """Paste copied annotations onto the current page, offset slightly, and select them."""
        if not self._clipboard_items or not self._doc:
            return
        offset = 12.0
        pasted = []
        for src in self._clipboard_items:
            clone = src.clone()
            clone.page_num = self._current_page
            clone.setPos(clone.pos() + QPointF(offset, offset))
            self._attach_item(clone)
            pasted.append(clone)
        self._scene.clearSelection()
        for it in pasted:
            it.setSelected(True)
        self._push(AddItemsCommand(self, pasted, "Paste"))
        self.annotation_changed.emit()

    def bring_to_front(self):
        for item in self._scene.selectedItems():
            if isinstance(item, AnnotationBase):
                others = [i for i in self._page_annotations.get(self._current_page, [])
                          if i is not item]
                max_z = max((i.zValue() for i in others), default=0)
                item.setZValue(max_z + 1)

    def send_to_back(self):
        for item in self._scene.selectedItems():
            if isinstance(item, AnnotationBase):
                others = [i for i in self._page_annotations.get(self._current_page, [])
                          if i is not item]
                min_z = min((i.zValue() for i in others), default=0)
                # Stay above the background (z=-1)
                item.setZValue(max(min_z - 1, -0.5))

    def get_all_annotation_dicts(self, page_num: int) -> list:
        return [item.to_annotation_dict(self._zoom)
                for item in self._page_annotations.get(page_num, [])]

    # ------------------------------------------------------------------
    # Selection → properties panel sync
    # ------------------------------------------------------------------

    def _on_selection_changed(self):
        items = [i for i in self._scene.selectedItems() if isinstance(i, AnnotationBase)]
        self.selection_changed.emit(self._selection_summary(items))

    @staticmethod
    def _item_stroke_color(it):
        if isinstance(it, RectAnnotationItem):
            return it._stroke
        if isinstance(it, (LineAnnotationItem, HighlightItem)):
            return it._color
        return None

    @staticmethod
    def _item_font_color(it):
        # A text label's own color IS its text color.
        if isinstance(it, TextAnnotationItem):
            return it._color
        return getattr(it, "_font_color", None)

    def _selection_summary(self, items) -> dict:
        """Fold each style property across the selection (value if all agree, else None)."""
        if not items:
            return {}

        def fold(getter):
            vals = [v for v in (getter(i) for i in items) if v is not None]
            if not vals:
                return None
            first = vals[0]
            return first if all(v == first for v in vals[1:]) else None

        # Fill on/off is folded over rectangles only (None = mixed or no rects).
        rects = [i for i in items if isinstance(i, RectAnnotationItem)]
        fill_flags = [i._fill is not None for i in rects]
        fill = None
        if fill_flags:
            fill = fill_flags[0] if all(f == fill_flags[0] for f in fill_flags) else None

        return {
            "stroke_color": fold(self._item_stroke_color),
            "fill_color": fold(lambda i: i._fill if isinstance(i, RectAnnotationItem) else None),
            "fill": fill,
            "opacity": fold(lambda i: getattr(i, "_opacity", None)),
            "line_width": fold(lambda i: getattr(i, "_line_width", None)),
            "font_size": fold(lambda i: getattr(i, "_font_size", None)),
            "font_color": fold(self._item_font_color),
            "types": {i.ann_type for i in items},
        }

    # ------------------------------------------------------------------
    # Editable annotation model — JSON round-trip (save → reopen → still editable)
    # ------------------------------------------------------------------

    def _item_to_json(self, item) -> dict | None:
        """Convert one annotation to a JSON-safe dict, or None to skip it.

        Images are skipped: they are baked into page content on save and stay
        editable on reopen via the existing embedded-image "lift" feature.
        """
        d = item.to_annotation_dict(self._zoom)
        t = d.get("type")
        if t == "image":
            return None
        j = {"type": t}
        if "fitz_rect" in d:
            r = d["fitz_rect"]
            j["rect"] = [r.x0, r.y0, r.x1, r.y1]
        if "p1" in d and "p2" in d:
            j["p1"] = [d["p1"].x, d["p1"].y]
            j["p2"] = [d["p2"].x, d["p2"].y]
        for k in ("color", "stroke_color", "fill_color", "font_color"):
            if k in d:
                j[k] = list(d[k])
        for k in ("opacity", "line_width", "font_size", "text"):
            if k in d:
                j[k] = d[k]
        return j

    def export_annotation_model(self) -> dict:
        """Serialize every page's editable annotations for embedding in the PDF."""
        pages: dict[str, list] = {}
        for page_num, items in self._page_annotations.items():
            out = [j for j in (self._item_to_json(it) for it in items) if j is not None]
            if out:
                pages[str(page_num)] = out
        return {"version": 1, "pages": pages}

    def _item_from_dict(self, j: dict, page_num: int):
        """Rebuild a canvas item from its JSON form (inverse of _item_to_json)."""
        z = self._zoom
        t = j.get("type")

        def col(key, default=(0.0, 0.0, 0.0)):
            v = j.get(key, default)
            return QColor.fromRgbF(float(v[0]), float(v[1]), float(v[2]))

        try:
            if t in ("rect", "highlight"):
                r = j["rect"]
                rect = QRectF(r[0] * z, r[1] * z, (r[2] - r[0]) * z, (r[3] - r[1]) * z)
                opacity = j.get("opacity", 1.0)
                if t == "highlight":
                    item = HighlightItem(rect, col("color"), opacity, page_num)
                else:
                    fill = col("fill_color") if "fill_color" in j else None
                    item = RectAnnotationItem(
                        rect, col("stroke_color"), fill, opacity, page_num,
                        j.get("line_width", 2.0),
                    )
                item.text = j.get("text", "")
                item._font_size = j.get("font_size", 12)
                if "font_color" in j:
                    item._font_color = col("font_color")
                return item
            if t == "line":
                p1, p2 = j["p1"], j["p2"]
                line = QLineF(p1[0] * z, p1[1] * z, p2[0] * z, p2[1] * z)
                return LineAnnotationItem(
                    line, col("color"), j.get("opacity", 1.0), page_num,
                    j.get("line_width", 2.0),
                )
            if t == "text":
                r = j["rect"]
                pos = QPointF(r[0] * z, r[1] * z)
                return TextAnnotationItem(
                    pos, j.get("text", ""), col("color"),
                    j.get("font_size", 12), page_num,
                )
        except Exception as e:
            print(f"Reconstruct error ({t}): {e}")
        return None

    def load_annotation_model(self, model: dict):
        """Reconstruct editable items from an embedded model after opening a PDF."""
        if not model:
            return
        for page_str, items_json in model.get("pages", {}).items():
            try:
                page_num = int(page_str)
            except (ValueError, TypeError):
                continue
            for j in items_json:
                item = self._item_from_dict(j, page_num)
                if item is None:
                    continue
                self._scene.addItem(item)
                self._page_annotations.setdefault(page_num, []).append(item)
                item.setVisible(page_num == self._current_page)

    def reload_current_page(self):
        """Re-render the current page (e.g. after stripping baked markup on open)."""
        if self._doc and self._doc.page_count() > 0:
            self._load_page(self._current_page)

    def get_render_zoom(self) -> float:
        return self._zoom

    def remap_page_annotations(self, from_page: int, to_page: int):
        if from_page == to_page:
            return
        new_map: dict[int, list] = {}
        for pnum, items in self._page_annotations.items():
            if pnum == from_page:
                new_pnum = to_page
            elif from_page < to_page and from_page < pnum <= to_page:
                new_pnum = pnum - 1
            elif from_page > to_page and to_page <= pnum < from_page:
                new_pnum = pnum + 1
            else:
                new_pnum = pnum
            for item in items:
                item.page_num = new_pnum
            if new_pnum in new_map:
                new_map[new_pnum].extend(items)
            else:
                new_map[new_pnum] = items
        self._page_annotations = new_map
        if self._current_page == from_page:
            self._current_page = to_page
        elif from_page < to_page and from_page < self._current_page <= to_page:
            self._current_page -= 1
        elif from_page > to_page and to_page <= self._current_page < from_page:
            self._current_page += 1

    def reorder_pages(self, new_order: list):
        """Apply a full page permutation: new page i holds old page new_order[i]."""
        new_map: dict[int, list] = {}
        for new_idx, old_idx in enumerate(new_order):
            items = self._page_annotations.get(old_idx, [])
            for item in items:
                item.page_num = new_idx
            if items:
                new_map[new_idx] = items
        self._page_annotations = new_map
        if self._current_page in new_order:
            self._current_page = new_order.index(self._current_page)
        else:
            self._current_page = 0
        if self._doc and self._doc.page_count() > 0:
            self._load_page(self._current_page)

    def grab_current_thumbnail(self, max_width: int) -> QPixmap:
        """Render the current page AS SHOWN (background + live overlays) to a thumbnail.

        Used to keep the left page panel in sync with unsaved edits without flushing
        annotations into the PDF.
        """
        if not self._bg_item:
            return QPixmap()
        src = self._scene.sceneRect()
        if src.width() <= 0:
            return QPixmap()
        scale = max_width / src.width()
        target = QPixmap(max(1, round(src.width() * scale)),
                         max(1, round(src.height() * scale)))
        target.fill(Qt.GlobalColor.white)
        painter = QPainter(target)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        had_selection = list(self._scene.selectedItems())
        self._scene.clearSelection()       # keep selection handles out of the thumbnail
        self._scene.render(painter, QRectF(target.rect()), src)
        for it in had_selection:
            it.setSelected(True)
        painter.end()
        return target

    def remove_page_annotations(self, page_num: int):
        for item in self._page_annotations.get(page_num, []):
            if item.scene():
                self._scene.removeItem(item)
        new_map: dict[int, list] = {}
        for pnum, items in self._page_annotations.items():
            if pnum == page_num:
                continue
            new_pnum = pnum - 1 if pnum > page_num else pnum
            for item in items:
                item.page_num = new_pnum
            new_map[new_pnum] = items
        self._page_annotations = new_map
        if self._current_page > page_num:
            self._current_page -= 1
        elif self._current_page == page_num:
            self._current_page = max(0, page_num - 1)

    # ------------------------------------------------------------------
    # Handle helpers
    # ------------------------------------------------------------------

    def _rect_corners_in_scene(self, item: AnnotationItem):
        r = item.rect()
        tl = item.mapToScene(r.topLeft())
        tr = item.mapToScene(r.topRight())
        bl = item.mapToScene(r.bottomLeft())
        left = min(tl.x(), bl.x())
        right = max(tr.x(), item.mapToScene(r.bottomRight()).x())
        top = min(tl.y(), tr.y())
        bottom = max(bl.y(), item.mapToScene(r.bottomRight()).y())
        return left, top, right, bottom, (left + right) / 2, (top + bottom) / 2

    def _get_handles_for_item(self, item: AnnotationItem) -> dict[str, QRectF]:
        l, t, r, b, cx, cy = self._rect_corners_in_scene(item)
        hs = HANDLE_SIZE

        def h(x, y): return QRectF(x - hs / 2, y - hs / 2, hs, hs)

        return {
            "tl": h(l, t), "t": h(cx, t), "tr": h(r, t),
            "r":  h(r, cy),
            "br": h(r, b), "b": h(cx, b), "bl": h(l, b),
            "l":  h(l, cy),
        }

    def _get_line_handles(self, item: LineAnnotationItem) -> dict[str, QRectF]:
        hs = HANDLE_SIZE
        ln = item.line()
        p1s = item.mapToScene(ln.p1())
        p2s = item.mapToScene(ln.p2())

        def h(p): return QRectF(p.x() - hs / 2, p.y() - hs / 2, hs, hs)

        return {"p1": h(p1s), "p2": h(p2s)}

    def _handle_at(self, scene_pos: QPointF):
        for item in self._scene.selectedItems():
            if isinstance(item, AnnotationItem):
                for name, rect in self._get_handles_for_item(item).items():
                    if rect.contains(scene_pos):
                        return item, name
            elif isinstance(item, LineAnnotationItem):
                for name, rect in self._get_line_handles(item).items():
                    if rect.contains(scene_pos):
                        return item, name
        return None, None

    def _handle_cursor(self, handle: str) -> Qt.CursorShape:
        if handle in ("tl", "br"):
            return Qt.CursorShape.SizeFDiagCursor
        if handle in ("tr", "bl"):
            return Qt.CursorShape.SizeBDiagCursor
        if handle in ("t", "b"):
            return Qt.CursorShape.SizeVerCursor
        if handle in ("l", "r"):
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeAllCursor  # p1, p2

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_page(self, page_num: int):
        pixmap = self._doc.render_page(page_num, self._zoom)
        if self._bg_item is None:
            self._bg_item = self._scene.addPixmap(pixmap)
            self._bg_item.setZValue(-1)
        else:
            self._bg_item.setPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))

        self._embedded_images = self._compute_embedded_images(page_num)

        for item in self._page_annotations.get(page_num, []):
            if item.scene() is None:
                self._scene.addItem(item)
            item.setVisible(True)

    def _compute_embedded_images(self, page_num: int) -> list:
        """List (xref, PDF-coord Rect) for every raster image currently drawn on the page.

        Uses get_images(), which stops reporting an image once it has been redacted
        out (i.e. once lifted) — so lifted images never reappear here.
        """
        out = []
        if not self._doc or not self._doc.doc or page_num >= self._doc.page_count():
            return out
        try:
            page = self._doc.doc[page_num]
            for img in page.get_images(full=True):
                xref = img[0]
                for r in page.get_image_rects(xref):
                    if r.width > 3 and r.height > 3:
                        out.append((xref, fitz.Rect(r)))
        except Exception as e:
            print(f"Embedded-image scan error: {e}")
        return out

    def _annotation_at(self, scene_pos: QPointF):
        for item in self._scene.items(scene_pos):
            if isinstance(item, AnnotationBase):
                return item
        return None

    def _embedded_image_at(self, scene_pos: QPointF):
        """Return (xref, fitz.Rect) of the smallest embedded image under scene_pos, or None.

        Smallest-area wins so a small image on top of a full-page background is the one
        you grab (a scanned page that is one big image still lifts as a whole — by design).
        """
        if self._zoom <= 0:
            return None
        px, py = scene_pos.x() / self._zoom, scene_pos.y() / self._zoom
        best, best_area = None, None
        for xref, r in self._embedded_images:
            if r.x0 <= px <= r.x1 and r.y0 <= py <= r.y1:
                area = r.width * r.height
                if best_area is None or area < best_area:
                    best, best_area = (xref, r), area
        return best

    def _lift_embedded_image(self, xref: int, fitz_rect):
        """Convert an embedded image into a movable/resizable object.

        Removes only this instance from the page (redaction over its rect, images only),
        re-renders the background, and drops an ImageAnnotationItem in its place.
        Returns the new item, or None on failure.
        """
        page_num = self._current_page
        doc = self._doc.doc
        try:
            ext = doc.extract_image(xref)
            image_bytes = ext["image"]
        except Exception as e:
            print(f"Lift extract failed: {e}")
            return None
        pixmap = QPixmap()
        if not pixmap.loadFromData(image_bytes):
            return None
        try:
            page = doc[page_num]
            page.add_redact_annot(fitz_rect, fill=None)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_REMOVE,
                text=fitz.PDF_REDACT_TEXT_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            )
        except Exception as e:
            print(f"Lift redaction failed: {e}")
            return None

        # Background now renders without the lifted image.
        self._bg_item.setPixmap(self._doc.render_page(page_num, self._zoom))

        z = self._zoom
        rect = QRectF(fitz_rect.x0 * z, fitz_rect.y0 * z,
                      fitz_rect.width * z, fitz_rect.height * z)
        item = ImageAnnotationItem(pixmap, image_bytes, rect, page_num)
        self._scene.addItem(item)
        self._page_annotations.setdefault(page_num, []).append(item)
        self._embedded_images = self._compute_embedded_images(page_num)
        self._scene.clearSelection()
        item.setSelected(True)
        self.annotation_changed.emit()
        return item

    def _paste_from_clipboard(self):
        if not self._doc or self._doc.page_count() == 0:
            return
        qimage = QApplication.clipboard().image()
        if qimage.isNull():
            return
        pixmap = QPixmap.fromImage(qimage)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buf, "PNG")
        buf.close()
        image_bytes = bytes(buf.data())
        center = self.mapToScene(self.viewport().rect().center())
        w, h = float(pixmap.width()), float(pixmap.height())
        max_dim = 600.0
        if w > max_dim or h > max_dim:
            scale = max_dim / max(w, h)
            w, h = w * scale, h * scale
        rect = QRectF(center.x() - w / 2, center.y() - h / 2, w, h)
        item = ImageAnnotationItem(pixmap, image_bytes, rect, self._current_page)
        self._attach_item(item)
        self._scene.clearSelection()
        item.setSelected(True)
        self._push(AddItemsCommand(self, [item], "Paste image"))
        self.annotation_changed.emit()

    def _item_intersects(self, item, rect: QRectF) -> bool:
        """True if an annotation item overlaps rect (touch/intersect semantics)."""
        if isinstance(item, AnnotationItem):
            l, t, r, b, _, _ = self._rect_corners_in_scene(item)
            return rect.intersects(QRectF(l, t, r - l, b - t))
        if isinstance(item, LineAnnotationItem):
            ln = item.line()
            p1 = item.mapToScene(ln.p1())
            p2 = item.mapToScene(ln.p2())
            return rect.intersects(QRectF(p1, p2).normalized().adjusted(-1, -1, 1, 1))
        return rect.intersects(item.sceneBoundingRect())

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        scene_pos = self.mapToScene(event.pos())

        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_moved = False
            self._drag_total = QPointF(0, 0)
            self._drag_is_lift = False
            if self._tool == "select":
                res_item, res_handle = self._handle_at(scene_pos)
                if res_item and res_handle:
                    self._resize_item = res_item
                    self._resize_handle = res_handle
                    self._resize_orig_pos = scene_pos
                    self._resize_before = geometry_snapshot(res_item)
                    if isinstance(res_item, AnnotationItem):
                        l, t, r, b, _, _ = self._rect_corners_in_scene(res_item)
                        self._resize_orig_rect = QRectF(l, t, r - l, b - t)
                    event.accept()
                    return

                additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                duplicate = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                item = self._annotation_at(scene_pos)
                if item:
                    if duplicate:
                        # Clone the whole selection (or just this item if it isn't selected).
                        base = [i for i in self._scene.selectedItems()
                                if isinstance(i, AnnotationBase)]
                        if item not in base:
                            base = [item]
                        self._dup_clones = [i.clone() for i in base]
                        self._scene.clearSelection()
                        for c in self._dup_clones:
                            self._scene.addItem(c)
                            c.setSelected(True)
                        self._duplicating = True
                        self._drag_items = list(self._dup_clones)
                    else:
                        if additive:
                            item.setSelected(not item.isSelected())
                        elif not item.isSelected():
                            self._scene.clearSelection()
                            item.setSelected(True)
                        # else: clicked an already-selected item with no modifier → keep the group
                        self._drag_items = [i for i in self._scene.selectedItems()
                                            if isinstance(i, AnnotationBase)]
                    self._drag_start = scene_pos
                    self._drag_anchor = scene_pos
                    self._drag_applied = QPointF(0, 0)
                else:
                    # No annotation here. An embedded PDF image lifts+drags on press;
                    # truly empty space begins a marquee (decided on first move).
                    emb = self._embedded_image_at(scene_pos)
                    lifted = self._lift_embedded_image(*emb) if emb else None
                    if lifted:
                        self._drag_items = [lifted]
                        self._drag_start = scene_pos
                        self._drag_anchor = scene_pos
                        self._drag_applied = QPointF(0, 0)
                        self._drag_is_lift = True
                    else:
                        self._press_empty_pos = scene_pos
                        self._press_additive = additive
                        self._drag_items = []
                        self._drag_start = None

            elif self._tool == "text":
                existing = self._annotation_at(scene_pos)
                if isinstance(existing, TextAnnotationItem):
                    text, ok = QInputDialog.getText(
                        self, "Edit Text", "Text:", text=existing.toPlainText()
                    )
                    if ok:
                        self._apply_style_change(
                            [existing],
                            lambda it: (it.setPlainText(text), setattr(it, "text", text)),
                            "Edit text",
                        )
                else:
                    text, ok = QInputDialog.getText(self, "Add Text", "Enter text:")
                    if ok and text.strip():
                        new_item = TextAnnotationItem(
                            scene_pos, text, self._font_color, self._font_size, self._current_page
                        )
                        self._attach_item(new_item)
                        self._scene.clearSelection()
                        new_item.setSelected(True)
                        self._push(AddItemsCommand(self, [new_item], "Add text"))
                        self.annotation_changed.emit()

            elif self._tool in ("rect", "line"):
                # Starting a new shape drops any current selection, so you can draw
                # straight over an existing (selected) object without deselecting first.
                self._scene.clearSelection()
                self._drawing = True
                self._draw_start = scene_pos

                if self._tool == "rect":
                    fill = self._fill_color if self._fill_enabled else None
                    self._temp_item = RectAnnotationItem(
                        QRectF(scene_pos, scene_pos),
                        self._stroke_color, fill, self._opacity, self._current_page,
                        self._line_width,
                    )
                    self._temp_item._font_size = self._font_size
                    self._temp_item._font_color = QColor(self._font_color)
                else:  # line
                    # A line tool always draws a visible stroke, even if the rect
                    # border width is currently set to 0 ("No line").
                    lw = self._line_width if self._line_width > 0 else 2.0
                    self._temp_item = LineAnnotationItem(
                        QLineF(scene_pos, scene_pos),
                        self._stroke_color, self._opacity, self._current_page,
                        lw,
                    )

                self._scene.addItem(self._temp_item)

        event.accept()

    def mouseMoveEvent(self, event):
        scene_pos = self.mapToScene(event.pos())

        if self._resize_item and self._resize_handle:
            if isinstance(self._resize_item, LineAnnotationItem):
                local_pos = self._resize_item.mapFromScene(scene_pos)
                ln = self._resize_item.line()
                if self._resize_handle == "p1":
                    self._resize_item.setLine(QLineF(local_pos, ln.p2()))
                else:
                    self._resize_item.setLine(QLineF(ln.p1(), local_pos))
                self._resize_item.update()
            else:
                total = scene_pos - self._resize_orig_pos
                orig = self._resize_orig_rect
                l, t, r, b = orig.left(), orig.top(), orig.right(), orig.bottom()
                h = self._resize_handle

                if "l" in h: l += total.x()
                if "r" in h: r += total.x()
                if "t" in h: t += total.y()
                if "b" in h: b += total.y()

                if r - l < 8:
                    if "l" in h: l = r - 8
                    else: r = l + 8
                if b - t < 8:
                    if "t" in h: t = b - 8
                    else: b = t + 8

                p = self._resize_item.pos()
                self._resize_item.setRect(QRectF(l - p.x(), t - p.y(), r - l, b - t))
                self._resize_item.update()

        elif self._drag_items and self._drag_start is not None:
            anchor = self._drag_anchor if self._drag_anchor is not None else self._drag_start
            total = scene_pos - anchor
            # Shift locks the move/duplicate to a straight horizontal or vertical path.
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                if abs(total.x()) >= abs(total.y()):
                    total = QPointF(total.x(), 0.0)
                else:
                    total = QPointF(0.0, total.y())
            delta = total - self._drag_applied
            if delta.x() or delta.y():
                for it in self._drag_items:
                    it.moveBy(delta.x(), delta.y())
                self._drag_applied = total
                self._drag_total = total
                self._drag_moved = True

        elif self._press_empty_pos is not None:
            # Begin/extend a marquee once the press has dragged past a small threshold.
            if self._rubber_item is None:
                if (scene_pos - self._press_empty_pos).manhattanLength() > 3:
                    self._rubber_item = QGraphicsRectItem()
                    self._rubber_item.setPen(
                        QPen(QColor(0, 120, 215), 0, Qt.PenStyle.DashLine))
                    self._rubber_item.setBrush(QBrush(QColor(0, 120, 215, 40)))
                    self._rubber_item.setZValue(1000)
                    self._scene.addItem(self._rubber_item)
            if self._rubber_item is not None:
                self._rubber_item.setRect(
                    QRectF(self._press_empty_pos, scene_pos).normalized())

        elif self._drawing and self._temp_item and self._draw_start is not None:
            if isinstance(self._temp_item, LineAnnotationItem):
                end = scene_pos
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    dx = scene_pos.x() - self._draw_start.x()
                    dy = scene_pos.y() - self._draw_start.y()
                    if abs(dx) >= abs(dy):
                        end = QPointF(scene_pos.x(), self._draw_start.y())
                    else:
                        end = QPointF(self._draw_start.x(), scene_pos.y())
                self._temp_item.setLine(QLineF(self._draw_start, end))
            else:
                rect = QRectF(self._draw_start, scene_pos).normalized()
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    side = min(rect.width(), rect.height())
                    rect = QRectF(
                        self._draw_start,
                        QPointF(self._draw_start.x() + side, self._draw_start.y() + side),
                    )
                self._temp_item.setRect(rect)

        # Update cursor for hover feedback
        if (self._tool == "select" and not self._drawing and not self._drag_items
                and not self._resize_item and self._rubber_item is None):
            res_item, res_handle = self._handle_at(scene_pos)
            if res_item and res_handle:
                self.setCursor(self._handle_cursor(res_handle))
            elif self._annotation_at(scene_pos):
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)

        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._resize_item:
                after = geometry_snapshot(self._resize_item)
                if self._resize_before is not None and after != self._resize_before:
                    self._push(ResizeCommand(
                        self, self._resize_item, self._resize_before, after))
                self._resize_item = None
                self._resize_handle = None
                self._resize_orig_pos = None
                self._resize_orig_rect = None
                self._resize_before = None
                self.annotation_changed.emit()

            elif self._drag_items:
                if self._duplicating:
                    for c in self._dup_clones:
                        self._page_annotations.setdefault(c.page_num, []).append(c)
                    self._push(AddItemsCommand(self, list(self._dup_clones), "Duplicate"))
                    self.annotation_changed.emit()
                    self._dup_clones = []
                    self._duplicating = False
                elif self._drag_moved and not self._drag_is_lift:
                    # An existing selection was moved — record one undoable step.
                    self._push(MoveCommand(
                        self, list(self._drag_items),
                        self._drag_total.x(), self._drag_total.y()))
                    self.annotation_changed.emit()
                elif self._drag_moved:
                    self.annotation_changed.emit()  # lifted image moved (not undoable)
                self._drag_items = []
                self._drag_start = None
                self._drag_moved = False
                self._drag_is_lift = False
                self._drag_anchor = None
                self._drag_applied = QPointF(0, 0)

            elif self._rubber_item is not None:
                rubber_rect = self._rubber_item.rect()
                self._scene.removeItem(self._rubber_item)
                self._rubber_item = None
                if not self._press_additive:
                    self._scene.clearSelection()
                for it in self._page_annotations.get(self._current_page, []):
                    if self._item_intersects(it, rubber_rect):
                        it.setSelected(True)
                self._press_empty_pos = None

            elif self._press_empty_pos is not None:
                # A plain click on empty space — clear selection (unless additive).
                if not self._press_additive:
                    self._scene.clearSelection()
                self._press_empty_pos = None

            elif self._drawing and self._temp_item:
                keep = False
                if isinstance(self._temp_item, LineAnnotationItem):
                    keep = self._temp_item.line().length() > 4
                else:
                    r = self._temp_item.rect()
                    keep = r.width() > 4 and r.height() > 4

                if keep:
                    self._page_annotations.setdefault(self._current_page, []).append(self._temp_item)
                    # Leave the new object selected so its color/style can be tweaked
                    # immediately, while the drawing tool stays active for the next shape.
                    self._scene.clearSelection()
                    self._temp_item.setSelected(True)
                    self._push(AddItemsCommand(self, [self._temp_item], "Draw"))
                    self.annotation_changed.emit()
                else:
                    self._scene.removeItem(self._temp_item)

                self._temp_item = None
                self._drawing = False
                self._draw_start = None

        event.accept()

    def mouseDoubleClickEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        if self._tool == "select":
            item = self._annotation_at(scene_pos)
            if isinstance(item, TextAnnotationItem):
                text, ok = QInputDialog.getText(
                    self, "Edit Text", "Text:", text=item.toPlainText()
                )
                if ok:
                    self._apply_style_change(
                        [item],
                        lambda it: (it.setPlainText(text), setattr(it, "text", text)),
                        "Edit text",
                    )
            elif isinstance(item, AnnotationItem):
                text, ok = QInputDialog.getText(
                    self, "Text", "Text inside shape:", text=item.text
                )
                if ok:
                    self._apply_style_change(
                        [item], lambda it: setattr(it, "text", text), "Edit text",
                    )
        event.accept()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1.0 / 1.15
            # Re-anchor the zoom under the cursor: scale, then translate the view so
            # the scene point that was under the mouse stays under the mouse.
            cursor_pos = event.position().toPoint()
            before = self.mapToScene(cursor_pos)
            self.scale(factor, factor)
            after = self.mapToScene(cursor_pos)
            delta = after - before
            self.translate(delta.x(), delta.y())
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        arrows = {
            Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1),
        }

        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
        elif key == Qt.Key.Key_A and mods & Qt.KeyboardModifier.ControlModifier:
            for item in self._page_annotations.get(self._current_page, []):
                item.setSelected(True)
        elif key in arrows:
            items = [i for i in self._scene.selectedItems() if isinstance(i, AnnotationBase)]
            if items:
                step = 10 if mods & Qt.KeyboardModifier.ShiftModifier else 1
                dx, dy = arrows[key]
                dx, dy = dx * step, dy * step
                for it in items:
                    it.moveBy(dx, dy)
                self._push(NudgeCommand(self, items, dx, dy))
                self.annotation_changed.emit()
            else:
                super().keyPressEvent(event)
        elif key == Qt.Key.Key_BracketRight and mods & Qt.KeyboardModifier.ControlModifier:
            self.bring_to_front()
        elif key == Qt.Key.Key_BracketLeft and mods & Qt.KeyboardModifier.ControlModifier:
            self.send_to_back()
        else:
            super().keyPressEvent(event)
