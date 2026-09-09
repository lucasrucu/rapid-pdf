"""Resize handles on a text label, and the audit that says every painted handle works.

The bug these start from: a selected TextAnnotationItem painted four corner
handles, and PDFCanvas._handle_at only ever tested AnnotationItem and
LineAnnotationItem. A text label is neither, so the handles were decoration.
You could hover one, get a resize cursor, drag it, and nothing moved.

A label has no box of its own (it is exactly as big as its text), so the resize
that means something here is a font scale, which is also the one thing that
persists into the saved PDF. That is what a corner drag now does.

The wider gap this closes: before these, no test ever built a TextAnnotationItem,
a LineAnnotationItem or an ImageAnnotationItem, which is why the dead handles
survived. test_every_painted_handle_is_serviced is the guard against a fourth
item class arriving with the same hole.
"""

import pytest

import fitz
from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from core.pdf_document import PDFDocument
from ui.canvas import (
    MAX_FONT_SIZE, MIN_FONT_SIZE,
    HighlightItem, ImageAnnotationItem, LineAnnotationItem,
    PDFCanvas, RectAnnotationItem, TextAnnotationItem,
)


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


def _blank_pdf(tmp_path):
    raw = fitz.open()
    raw.new_page(width=400, height=500)
    path = tmp_path / "blank.pdf"
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def canvas(qt_app, tmp_path):
    doc = PDFDocument()
    doc.open(_blank_pdf(tmp_path))
    c = PDFCanvas()
    c.resize(600, 700)
    c.set_document(doc)
    c._flush_pending_render()   # the debounced render never lands on its own
    yield c
    c.deleteLater()
    doc.close()


@pytest.fixture
def label(canvas):
    """One selected text label near the top-left of the page."""
    item = TextAnnotationItem(QPointF(40, 40), "Resize me", QColor("black"), 12, 0)
    canvas._attach_item(item)
    canvas._scene.clearSelection()
    item.setSelected(True)
    return item


def _mouse(canvas, kind, scene_pt, button=Qt.MouseButton.LeftButton):
    vp = canvas.mapFromScene(scene_pt)
    held = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else button
    return QMouseEvent(kind, QPointF(vp), QPointF(vp), button, held,
                       Qt.KeyboardModifier.NoModifier)


def _drag(canvas, start, end):
    """Press, move, release, delivered the way Qt would deliver a real drag."""
    canvas.mousePressEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonPress, start))
    canvas.mouseMoveEvent(_mouse(canvas, QMouseEvent.Type.MouseMove, end))
    canvas.mouseReleaseEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, end))


def _corner(item, name):
    r = item.scene_text_rect()
    return {
        "tl": r.topLeft(), "tr": r.topRight(),
        "bl": r.bottomLeft(), "br": r.bottomRight(),
    }[name]


# ---------------------------------------------------------------------------
# The handles exist and are reachable
# ---------------------------------------------------------------------------

def test_a_selected_label_paints_four_corner_handles(label):
    assert TextAnnotationItem.HANDLE_CORNERS == ("tl", "tr", "bl", "br")


@pytest.mark.parametrize("name", ["tl", "tr", "bl", "br"])
def test_every_painted_corner_is_grabbable(canvas, label, name):
    """The regression. _handle_at used to return (None, None) for all four."""
    item, handle = canvas._handle_at(_corner(label, name))
    assert item is label
    assert handle == name


def test_a_point_away_from_the_label_grabs_nothing(canvas, label):
    item, handle = canvas._handle_at(QPointF(350, 400))
    assert (item, handle) == (None, None)


def test_the_handle_hit_boxes_sit_on_the_rect_the_label_paints(canvas, label):
    """What you see is what you can grab: the hit boxes are centred on the same
    scene_text_rect corners paint() draws from."""
    r = label.scene_text_rect()
    handles = canvas._get_text_handles(label)
    assert handles["tl"].center().toPoint() == r.topLeft().toPoint()
    assert handles["br"].center().toPoint() == r.bottomRight().toPoint()


# ---------------------------------------------------------------------------
# Dragging one actually resizes
# ---------------------------------------------------------------------------

def test_dragging_the_bottom_right_corner_out_grows_the_font(canvas, label):
    before = label._font_size
    start = _corner(label, "br")
    _drag(canvas, start, start + QPointF(60, 30))
    assert label._font_size > before


def test_dragging_the_bottom_right_corner_in_shrinks_the_font(canvas, label):
    label.set_font_size(40)
    before = label._font_size
    start = _corner(label, "br")
    _drag(canvas, start, start - QPointF(40, 20))
    assert label._font_size < before


def test_the_label_really_gets_bigger_on_the_page(canvas, label):
    before = label.scene_text_rect()
    start = _corner(label, "br")
    _drag(canvas, start, start + QPointF(60, 30))
    after = label.scene_text_rect()
    assert after.width() > before.width()
    assert after.height() > before.height()


def test_dragging_the_top_left_leaves_the_bottom_right_where_it_was(canvas, label):
    """The corner you are not holding stays nailed down, the way it does on a
    rectangle. Scaling the font grows the label down and right from its origin,
    so the item has to move to keep this true."""
    label.set_font_size(30)
    anchor_before = _corner(label, "br")
    start = _corner(label, "tl")
    _drag(canvas, start, start + QPointF(20, 10))
    assert label._font_size < 30
    anchor_after = _corner(label, "br")
    assert abs(anchor_after.x() - anchor_before.x()) < 1.0
    assert abs(anchor_after.y() - anchor_before.y()) < 1.0


def test_dragging_the_bottom_right_leaves_the_top_left_where_it_was(canvas, label):
    before = label.pos()
    start = _corner(label, "br")
    _drag(canvas, start, start + QPointF(60, 30))
    assert label.pos() == before


def test_the_font_will_not_shrink_past_the_floor(canvas, label):
    start = _corner(label, "br")
    _drag(canvas, start, start - QPointF(500, 500))
    assert label._font_size == MIN_FONT_SIZE


def test_the_font_will_not_grow_past_the_ceiling(canvas, label):
    start = _corner(label, "br")
    _drag(canvas, start, start + QPointF(4000, 4000))
    assert label._font_size == MAX_FONT_SIZE


def test_a_bigger_label_writes_a_bigger_annotation(canvas, label):
    """The scale has to reach the saved file, not just the screen. The write path
    takes fontsize and the rect straight off this dict."""
    before = label.to_annotation_dict(canvas._zoom)
    start = _corner(label, "br")
    _drag(canvas, start, start + QPointF(60, 30))
    after = label.to_annotation_dict(canvas._zoom)
    assert after["font_size"] > before["font_size"]
    assert after["fitz_rect"].width > before["fitz_rect"].width


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

def test_resizing_a_label_is_undoable(canvas, label):
    start = _corner(label, "br")
    _drag(canvas, start, start + QPointF(60, 30))
    assert canvas.undo_stack.canUndo()
    assert canvas.undo_stack.command(canvas.undo_stack.count() - 1).text() == "Resize"


def test_undo_puts_the_font_and_the_position_back(canvas, label):
    label.set_font_size(30)
    canvas.undo_stack.clear()
    before_size, before_pos = label._font_size, QPointF(label.pos())
    start = _corner(label, "tl")
    _drag(canvas, start, start + QPointF(20, 10))
    assert label._font_size != before_size
    canvas.undo_stack.undo()
    assert label._font_size == before_size
    assert label.pos() == before_pos


def test_redo_replays_the_resize(canvas, label):
    start = _corner(label, "br")
    _drag(canvas, start, start + QPointF(60, 30))
    grown = label._font_size
    canvas.undo_stack.undo()
    canvas.undo_stack.redo()
    assert label._font_size == grown


def test_a_drag_that_changes_nothing_pushes_nothing(canvas, label):
    """Clicking a handle without moving is not an edit."""
    start = _corner(label, "br")
    _drag(canvas, start, start)
    assert canvas.undo_stack.count() == 0


# ---------------------------------------------------------------------------
# The audit itself
# ---------------------------------------------------------------------------

def _every_item_that_paints_handles(canvas):
    """One of each annotation class, selected, on the page.

    Add a class here when you add one to canvas.py. If it paints selection
    handles the test below will hold you to servicing them.
    """
    pm = QPixmap(20, 20)
    pm.fill(QColor("red"))
    items = [
        HighlightItem(QRectF(20, 200, 80, 30), QColor("yellow"), 0.4, 0),
        RectAnnotationItem(QRectF(20, 250, 80, 30), QColor("red"), None, 1.0, 0),
        ImageAnnotationItem(pm, b"", QRectF(20, 300, 80, 30), 0),
        LineAnnotationItem(QLineF(20, 350, 100, 380), QColor("blue"), 1.0, 0),
        TextAnnotationItem(QPointF(20, 400), "Label", QColor("black"), 12, 0),
    ]
    for it in items:
        canvas._attach_item(it)
        it.setSelected(True)
    return items


def test_every_painted_handle_is_serviced(canvas):
    """No item may paint a handle the canvas cannot resize from.

    This is the shape of the original bug, stated once for every class rather
    than for the one that happened to be caught. A new annotation type that
    paints handles and is not wired into _handle_at fails here.
    """
    unreachable = []
    for item in _every_item_that_paints_handles(canvas):
        if isinstance(item, TextAnnotationItem):
            corners = [_corner(item, n) for n in TextAnnotationItem.HANDLE_CORNERS]
        elif isinstance(item, LineAnnotationItem):
            corners = [r.center() for r in canvas._get_line_handles(item).values()]
        else:
            corners = [r.center() for r in canvas._get_handles_for_item(item).values()]
        for pt in corners:
            found, handle = canvas._handle_at(pt)
            if found is not item or handle is None:
                unreachable.append((type(item).__name__, pt))
    assert unreachable == []


def test_group_resize_is_still_one_item_at_a_time(canvas):
    """Documenting the shape, not endorsing it.

    Selecting several objects and dragging a handle scales ONLY the object that
    owns the handle. _handle_at returns a single item and mousePressEvent stores
    a single _resize_item, so there is nowhere for a group scale to live. If
    that ever changes, this test is the one to delete.
    """
    a = RectAnnotationItem(QRectF(20, 250, 80, 30), QColor("red"), None, 1.0, 0)
    b = RectAnnotationItem(QRectF(200, 250, 80, 30), QColor("red"), None, 1.0, 0)
    for it in (a, b):
        canvas._attach_item(it)
        it.setSelected(True)
    b_before = QRectF(b.rect())
    start = a.mapToScene(a.rect().bottomRight())
    _drag(canvas, start, start + QPointF(40, 20))
    assert a.rect() != QRectF(20, 250, 80, 30)   # the one you grabbed grew
    assert b.rect() == b_before                  # its neighbour did not
