"""The canvas view controls: the pan tool, and the fit modes.

Pan has to be genuinely read-only (nothing on the page selected, moved or
edited), the hand cursor has to mean panning and only panning, and a fit has to
survive a resize and give way to a manual zoom.

Qt's real drag loop can't run in a test (the platform owns it), so a gesture is
delivered the way Qt would deliver it, straight into the canvas handlers with
genuine QMouseEvents. Everything either side of that is the real widget.
"""

import pytest

import fitz
from PySide6.QtCore import QBuffer, QIODevice, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import QApplication, QGraphicsView

from core.pdf_document import PDFDocument
from ui.canvas import FIT_MODES, HighlightItem, PDFCanvas


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


def _key(kind, key):
    return QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier)


# ---------------------------------------------------------------------------
# Pan
# ---------------------------------------------------------------------------

def test_the_pan_tool_hands_the_drag_to_the_view(canvas):
    canvas.set_tool("pan")
    assert canvas.dragMode() == QGraphicsView.DragMode.ScrollHandDrag
    assert canvas.is_panning()


def test_leaving_pan_restores_the_hand_rolled_marquee(canvas):
    """The marquee is drawn by hand, so every other tool must be back on NoDrag."""
    canvas.set_tool("pan")
    canvas.set_tool("select")
    assert canvas.dragMode() == QGraphicsView.DragMode.NoDrag
    assert not canvas.is_panning()


def test_a_marquee_still_selects_after_a_trip_through_pan(canvas):
    item = HighlightItem(QRectF(20, 20, 50, 50), QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    canvas.set_tool("pan")
    canvas.set_tool("select")
    _drag(canvas, QPointF(5, 5), QPointF(120, 120))
    assert item.isSelected()


def test_dragging_in_pan_mode_moves_nothing_on_the_page(canvas):
    item = HighlightItem(QRectF(20, 20, 50, 50), QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    canvas.set_tool("pan")
    canvas.undo_stack.clear()
    _drag(canvas, item.mapToScene(item.rect().center()), QPointF(40, 40))
    assert item.pos() == QPointF(0, 0)
    assert not item.isSelected()
    assert canvas.undo_stack.count() == 0


def test_pan_mode_does_not_lift_an_embedded_image(canvas):
    canvas.set_tool("pan")
    _drag(canvas, _over_the_image(canvas), QPointF(60, 40))
    assert canvas._embedded_image_at(_over_the_image(canvas)) is not None
    assert canvas.undo_stack.count() == 0


def test_holding_space_pans_and_releasing_gives_the_tool_back(canvas):
    canvas.set_tool("rect")
    canvas.keyPressEvent(_key(QKeyEvent.Type.KeyPress, Qt.Key.Key_Space))
    assert canvas.is_panning()
    assert canvas.dragMode() == QGraphicsView.DragMode.ScrollHandDrag
    canvas.keyReleaseEvent(_key(QKeyEvent.Type.KeyRelease, Qt.Key.Key_Space))
    assert canvas._tool == "rect"
    assert canvas.dragMode() == QGraphicsView.DragMode.NoDrag


def test_space_autorepeat_does_not_lose_the_tool(canvas):
    """Holding the key streams repeats; only the first and last one may act."""
    canvas.set_tool("line")
    canvas.keyPressEvent(_key(QKeyEvent.Type.KeyPress, Qt.Key.Key_Space))
    repeat = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Space,
                       Qt.KeyboardModifier.NoModifier, autorep=True)
    canvas.keyPressEvent(repeat)
    canvas.keyReleaseEvent(_key(QKeyEvent.Type.KeyRelease, Qt.Key.Key_Space))
    assert canvas._tool == "line"


def test_middle_drag_pans_without_touching_the_page(canvas):
    item = HighlightItem(QRectF(20, 20, 50, 50), QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    canvas.set_tool("select")
    canvas.undo_stack.clear()
    _drag(canvas, item.mapToScene(item.rect().center()), QPointF(40, 40),
          button=Qt.MouseButton.MiddleButton)
    assert item.pos() == QPointF(0, 0)
    assert canvas.undo_stack.count() == 0
    assert not canvas.is_panning()   # the middle button was released


def test_middle_drag_scrolls_the_view(canvas):
    canvas.set_tool("select")
    bar = canvas.verticalScrollBar()
    bar.setValue(min(80, bar.maximum()))
    before = bar.value()
    canvas.mousePressEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonPress,
                                  QPointF(200, 200), Qt.MouseButton.MiddleButton))
    canvas.mouseMoveEvent(_mouse(canvas, QMouseEvent.Type.MouseMove,
                                 QPointF(200, 240), Qt.MouseButton.MiddleButton))
    canvas.mouseReleaseEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonRelease,
                                    QPointF(200, 240), Qt.MouseButton.MiddleButton))
    assert bar.value() != before


def test_a_liftable_image_no_longer_promises_a_pan(canvas):
    """The cursor that misled: an open hand over something a drag MOVES."""
    canvas.set_tool("select")
    canvas.mouseMoveEvent(_mouse(canvas, QMouseEvent.Type.MouseMove,
                                 _over_the_image(canvas), Qt.MouseButton.NoButton))
    assert canvas.cursor().shape() == Qt.CursorShape.SizeAllCursor


def test_the_pan_tool_is_the_only_thing_showing_a_hand(canvas):
    canvas.set_tool("pan")
    assert canvas.cursor().shape() == Qt.CursorShape.OpenHandCursor


# ---------------------------------------------------------------------------
# Fit modes
# ---------------------------------------------------------------------------

def test_the_mode_names_match_the_settings_values():
    """core.settings' view.default_fit_mode uses these names; keep them in step."""
    assert set(FIT_MODES) == {"fit_page", "fit_width", "fit_height", "actual"}


def test_setting_a_mode_applies_it_and_remembers_it(canvas):
    canvas.set_fit_mode("fit_width")
    assert canvas.fit_mode() == "fit_width"


def test_an_unknown_mode_is_no_mode_at_all(canvas):
    canvas.set_fit_mode("fit_width")
    canvas.set_fit_mode("sideways")
    assert canvas.fit_mode() is None


def test_fit_width_puts_the_whole_page_width_on_screen(canvas):
    canvas.set_fit_mode("fit_width")
    scene_w = canvas._scene.sceneRect().width()
    shown_w = scene_w * canvas.transform().m11()
    assert shown_w <= canvas.viewport().width() + 1


def test_fit_height_puts_the_whole_page_height_on_screen(canvas):
    canvas.set_fit_mode("fit_height")
    scene_h = canvas._scene.sceneRect().height()
    shown_h = scene_h * canvas.transform().m22()
    assert shown_h <= canvas.viewport().height() + 1


def test_fit_page_fits_both_axes(canvas):
    canvas.set_fit_mode("fit_page")
    sr = canvas._scene.sceneRect()
    t = canvas.transform()
    assert sr.width() * t.m11() <= canvas.viewport().width() + 1
    assert sr.height() * t.m22() <= canvas.viewport().height() + 1


def test_actual_size_divides_the_render_scale_back_out(canvas):
    """100% means one PDF point per pixel, and the page is rasterised at _zoom."""
    canvas.set_fit_mode("actual")
    assert canvas.transform().m11() == pytest.approx(1.0 / canvas._zoom)


def test_a_fit_survives_a_resize(canvas):
    canvas.set_fit_mode("fit_page")
    canvas.resize(420, 380)
    sr = canvas._scene.sceneRect()
    assert sr.width() * canvas.transform().m11() <= canvas.viewport().width() + 1


def test_zooming_by_hand_clears_the_mode_and_says_so(canvas):
    """Ctrl+wheel breaks the fit, and the status bar hears about it so the
    group's active button can go out."""
    broken = []
    canvas.fit_mode_broken.connect(lambda: broken.append(True))
    canvas.set_fit_mode("fit_page")
    pos = QPointF(200, 200)
    canvas.wheelEvent(QWheelEvent(
        pos, canvas.mapToGlobal(pos.toPoint()), QPoint(0, 0), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase, False))
    assert canvas.fit_mode() is None
    assert broken == [True]
