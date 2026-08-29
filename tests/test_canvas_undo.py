"""Undo for a moved object, images included.

The bug these start from: dragging an image that is embedded in the PDF lifted
it into a movable object AND moved it, and neither half went onto the undo
stack, so Ctrl+Z did nothing at all. Dragging a highlight was always undoable,
which is why it looked like undo worked "except on images".

Qt's real drag loop can't run in a test (the platform owns it), so the gesture
is delivered the way Qt would deliver it, straight into the canvas handlers
with genuine QMouseEvents. Everything either side of that is the real widget:
the undo stack, the lift, the document, the scene.
"""

import pytest

import fitz
from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from core.pdf_document import PDFDocument
from ui.canvas import HighlightItem, PDFCanvas


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


def _pdf_with_an_image(tmp_path, rect=fitz.Rect(100, 100, 160, 160)):
    """A one-page PDF carrying one small raster, placed the ordinary way (a `cm`
    matrix then `/Name Do`) so the lift takes its placement-removal path."""
    pm = QPixmap(60, 60)
    pm.fill(QColor("red"))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pm.save(buf, "PNG")
    buf.close()

    raw = fitz.open()
    page = raw.new_page(width=400, height=500)
    page.insert_image(rect, stream=bytes(buf.data()))
    path = tmp_path / "with_image.pdf"
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def canvas(qt_app, tmp_path):
    doc = PDFDocument()
    doc.open(_pdf_with_an_image(tmp_path))
    c = PDFCanvas()
    c.resize(600, 700)
    c.set_document(doc)
    c._flush_pending_render()   # the debounced render never lands on its own
    yield c
    c.deleteLater()
    doc.close()


def _mouse(canvas, kind, scene_pt, button=Qt.MouseButton.LeftButton):
    """One QMouseEvent aimed at a scene point, with the button state Qt would set."""
    vp = canvas.mapFromScene(scene_pt)
    held = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else button
    return QMouseEvent(kind, QPointF(vp), QPointF(vp), button, held,
                       Qt.KeyboardModifier.NoModifier)


def _drag(canvas, start, delta, button=Qt.MouseButton.LeftButton):
    """Press, move, release. Returns the item the drag ended up carrying."""
    canvas.mousePressEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonPress, start, button))
    canvas.mouseMoveEvent(_mouse(canvas, QMouseEvent.Type.MouseMove, start + delta, button))
    carried = list(canvas._drag_items)
    canvas.mouseReleaseEvent(
        _mouse(canvas, QMouseEvent.Type.MouseButtonRelease, start + delta, button))
    return carried[0] if carried else None


def _over_the_image(canvas):
    """A scene point inside the embedded image."""
    return QPointF(130 * canvas._zoom, 130 * canvas._zoom)


# ---------------------------------------------------------------------------
# Undo: moving an image
# ---------------------------------------------------------------------------

def test_the_page_really_carries_an_embedded_image(canvas):
    """Guards the fixture: without a liftable image the rest proves nothing."""
    assert canvas._embedded_image_at(_over_the_image(canvas)) is not None


def test_dragging_an_embedded_image_is_undoable(canvas):
    """The regression. This drag used to push nothing at all onto the stack."""
    _drag(canvas, _over_the_image(canvas), QPointF(60, 40))
    assert canvas.undo_stack.canUndo()


def test_the_lift_and_the_move_are_two_separate_steps(canvas):
    item = _drag(canvas, _over_the_image(canvas), QPointF(60, 40))
    assert item is not None
    texts = [canvas.undo_stack.command(i).text() for i in range(canvas.undo_stack.count())]
    assert texts == ["Lift image", "Move"]


def test_undoing_puts_the_image_back_where_it_was(canvas):
    item = _drag(canvas, _over_the_image(canvas), QPointF(60, 40))
    assert item.pos() == QPointF(60, 40)
    canvas.undo_stack.undo()
    assert item.pos() == QPointF(0, 0)
    assert item.scene() is not None    # still on the page, just back in place


def test_undoing_twice_drops_the_image_back_into_the_document(canvas):
    item = _drag(canvas, _over_the_image(canvas), QPointF(60, 40))
    # The lift took the image out of the page content, so the page no longer
    # reports one there.
    assert canvas._embedded_image_at(_over_the_image(canvas)) is None
    canvas.undo_stack.undo()
    canvas.undo_stack.undo()
    assert item.scene() is None
    assert canvas._embedded_image_at(_over_the_image(canvas)) is not None


def test_redo_replays_the_whole_gesture(canvas):
    item = _drag(canvas, _over_the_image(canvas), QPointF(60, 40))
    canvas.undo_stack.undo()
    canvas.undo_stack.undo()
    canvas.undo_stack.redo()
    canvas.undo_stack.redo()
    assert item.scene() is not None
    assert item.pos() == QPointF(60, 40)


def test_moving_an_already_lifted_image_is_undoable_too(canvas):
    item = _drag(canvas, _over_the_image(canvas), QPointF(60, 40))
    canvas.undo_stack.clear()
    _drag(canvas, item.mapToScene(item.rect().center()), QPointF(30, 30))
    assert canvas.undo_stack.count() == 1
    canvas.undo_stack.undo()
    assert item.pos() == QPointF(60, 40)


def test_moving_a_highlight_is_undoable(canvas):
    """The control: shape markup was never part of the bug and must stay working."""
    item = HighlightItem(QRectF(20, 20, 50, 50), QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    canvas.undo_stack.clear()
    _drag(canvas, item.mapToScene(item.rect().center()), QPointF(25, 25))
    assert canvas.undo_stack.count() == 1
    canvas.undo_stack.undo()
    assert item.pos() == QPointF(0, 0)
