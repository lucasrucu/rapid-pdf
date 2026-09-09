"""Selecting and copying the PAGE's own text, which is the thing a commissioning
engineer opens a P&ID to do: get "4100-PU-001" out of the drawing and into
something else without typing it again.

Three things these are really guarding.

THE TRANSFORM. PyMuPDF answers `get_text("words")` in the page's UNROTATED user
space, in points. The canvas draws a pixmap rendered WITH the rotation applied,
at a raster scale the document freezes for itself. Getting from one to the other
is `page.rotation_matrix * Matrix(zoom, zoom)`, and the failure mode if you skip
the first half is invisible on the ordinary page and total on a rotated one. So
the rotation tests do not compare the code against a restatement of its own
formula: they render the page, find the ink, and check the word boxes landed on
it. Drop `rotation_matrix` and rot=0 still passes while 90, 180 and 270 fail,
which is exactly the bug being guarded.

THE TOOL MODEL. Text selection is not a tool. It is what the select pointer does
when a press lands on a word and not on an annotation or a handle. That buys the
gesture for free and costs one thing: a marquee can no longer be STARTED on top
of a word. Everything else about the pointer has to be untouched, so the drawing,
dragging and marquee tests here are the real point of the file.

THE SCANS. A large share of what gets opened here has no text layer at all.
Selecting on one has to be a clean no-op, not an exception and not a dead click.
"""

import fitz
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from core.pdf_document import PDFDocument
from ui.canvas import HighlightItem, PDFCanvas


# The words page 0 of the fixture carries, and where PyMuPDF puts them in the
# page's own (unrotated, points) space. Written down rather than read back from
# the code under test, so a test point is derived independently of the mapping
# it is checking.
LINE_ONE = ("PUMP", "4100-PU-001", "SUCTION")
LINE_TWO = ("VALVE", "4100-HV-002", "OPEN")
WORD_BOXES_PT = {
    "PUMP":        fitz.Rect(50.0, 84.9, 90.4, 104.2),
    "4100-PU-001": fitz.Rect(94.3, 84.9, 177.6, 104.2),
    "SUCTION":     fitz.Rect(181.5, 84.9, 244.5, 104.2),
    "VALVE":       fitz.Rect(50.0, 124.9, 95.1, 144.2),
    "4100-HV-002": fitz.Rect(99.0, 124.9, 182.3, 144.2),
    "OPEN":        fitz.Rect(186.2, 124.9, 225.9, 144.2),
}
# Empty page, well clear of both lines of text.
BLANK_SPOT_PT = QPointF(300.0, 350.0)

#: Maps a grey byte to 0 (ink) or 255 (paper), so a rendered row can be searched
#: for ink with bytes.find instead of a Python loop over every pixel.
_INK = bytes(0 if v < 128 else 255 for v in range(256))


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


def _text_pdf(tmp_path, name="text.pdf", rotation=0):
    """Two pages: one carrying two lines of tag-shaped text, one blank.

    The blank second page is the stand-in for a scan. It is not an image of
    nothing, it is a page with no text layer, which is the state that matters
    here: `get_text("words")` returns [] for both.
    """
    raw = fitz.open()
    page = raw.new_page(width=400, height=500)
    page.insert_text((50, 100), " ".join(LINE_ONE), fontsize=14)
    page.insert_text((50, 140), " ".join(LINE_TWO), fontsize=14)
    if rotation:
        page.set_rotation(rotation)
    raw.new_page(width=400, height=500)
    path = tmp_path / name
    raw.save(str(path))
    raw.close()
    return str(path)


def _canvas_for(path, scale):
    """A canvas on that file, at a forced raster scale.

    The scale is normally chosen once from the page geometry and memoised
    forever (annotation coordinates are in units of it). Setting the memo before
    the canvas reads it is how a test picks the number, and it is the only way:
    there is deliberately no setter.
    """
    doc = PDFDocument()
    assert doc.open(path)
    doc._render_scale = scale
    canvas = PDFCanvas()
    canvas.resize(1400, 1700)
    canvas.set_document(doc)
    canvas._flush_pending_render()   # the debounced render never lands on its own
    return canvas, doc


@pytest.fixture
def canvas(qt_app, tmp_path):
    c, doc = _canvas_for(_text_pdf(tmp_path), 1.5)
    yield c
    c.deleteLater()
    doc.close()


# ---------------------------------------------------------------------------
# Gestures, delivered the way Qt would deliver them
# ---------------------------------------------------------------------------

def _at(canvas, word):
    """The scene point at the centre of `word`, computed WITHOUT the canvas.

    Page 0 of the fixture is unrotated, so the mapping is the raster scale and
    nothing else. The rotated cases get their own, harder check further down.
    """
    r = WORD_BOXES_PT[word]
    z = canvas._zoom
    return QPointF((r.x0 + r.x1) / 2 * z, (r.y0 + r.y1) / 2 * z)


def _blank(canvas):
    return QPointF(BLANK_SPOT_PT.x() * canvas._zoom,
                   BLANK_SPOT_PT.y() * canvas._zoom)


def _rect(x, y, w, h):
    return QRectF(x, y, w, h)


def _mouse(canvas, kind, scene_pt, button=Qt.MouseButton.LeftButton,
           mods=Qt.KeyboardModifier.NoModifier):
    vp = canvas.mapFromScene(scene_pt)
    held = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else button
    return QMouseEvent(kind, QPointF(vp), QPointF(vp), button, held, mods)


def _press(canvas, pt, mods=Qt.KeyboardModifier.NoModifier):
    canvas.mousePressEvent(
        _mouse(canvas, QMouseEvent.Type.MouseButtonPress, pt, mods=mods))


def _move(canvas, pt, mods=Qt.KeyboardModifier.NoModifier):
    canvas.mouseMoveEvent(
        _mouse(canvas, QMouseEvent.Type.MouseMove, pt, mods=mods))


def _release(canvas, pt, mods=Qt.KeyboardModifier.NoModifier):
    canvas.mouseReleaseEvent(
        _mouse(canvas, QMouseEvent.Type.MouseButtonRelease, pt, mods=mods))


def _drag(canvas, start, end, mods=Qt.KeyboardModifier.NoModifier):
    _press(canvas, start, mods)
    _move(canvas, end, mods)
    _release(canvas, end, mods)


def _double_click(canvas, pt):
    _press(canvas, pt)
    _release(canvas, pt)
    canvas.mouseDoubleClickEvent(
        _mouse(canvas, QMouseEvent.Type.MouseButtonDblClick, pt))
    _release(canvas, pt)


def _key(canvas, key, mods=Qt.KeyboardModifier.NoModifier):
    canvas.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, key, mods))


def _ink_bbox(pix):
    """Bounding box of the dark pixels in a rendered page, in pixmap pixels."""
    n, w, stride, s = pix.n, pix.width, pix.stride, pix.samples
    x0 = y0 = None
    x1 = y1 = -1
    for y in range(pix.height):
        row = s[y * stride: y * stride + w * n].translate(_INK)
        first = row.find(b"\x00")
        if first < 0:
            continue
        last = row.rfind(b"\x00")
        cx0, cx1 = first // n, last // n
        x0 = cx0 if x0 is None else min(x0, cx0)
        x1 = max(x1, cx1)
        y0 = y if y0 is None else y0
        y1 = y
    assert x0 is not None, "the fixture page rendered blank"
    return x0, y0, x1 + 1, y1 + 1


# ---------------------------------------------------------------------------
# Dragging out a selection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scale", [1.5, 3.0])
def test_a_drag_across_a_line_selects_exactly_those_words(qt_app, tmp_path, scale):
    """The same gesture has to pick the same words whatever the page is rastered at.

    Two scales because the whole transform is a multiply by that number, and a
    single-scale test cannot tell a correct multiply from a missing one.
    """
    canvas, doc = _canvas_for(_text_pdf(tmp_path), scale)
    try:
        assert canvas._zoom == scale
        _drag(canvas, _at(canvas, "PUMP"), _at(canvas, "SUCTION"))
        assert canvas.selected_text() == "PUMP 4100-PU-001 SUCTION"
    finally:
        canvas.deleteLater()
        doc.close()


def test_a_short_drag_stops_at_the_word_it_stopped_on(canvas):
    _drag(canvas, _at(canvas, "PUMP"), _at(canvas, "4100-PU-001"))
    assert canvas.selected_text() == "PUMP 4100-PU-001"


def test_a_drag_backwards_selects_the_same_range(canvas):
    _drag(canvas, _at(canvas, "SUCTION"), _at(canvas, "PUMP"))
    assert canvas.selected_text() == "PUMP 4100-PU-001 SUCTION"


def test_a_drag_across_both_lines_keeps_them_on_separate_lines(canvas):
    """The copied string is built from the words, not from a clip rectangle, so
    a two-line range comes back as two lines rather than one run-on."""
    _drag(canvas, _at(canvas, "4100-PU-001"), _at(canvas, "VALVE"))
    assert canvas.selected_text() == "4100-PU-001 SUCTION\nVALVE"


def test_a_drag_that_starts_on_empty_page_selects_no_text(canvas):
    """Empty page still belongs to the marquee. This is the one thing the
    pointer gives up by having text selection at all, and it gives up only the
    part of the page that has words on it."""
    _drag(canvas, _blank(canvas), _at(canvas, "PUMP"))
    assert canvas.selected_text() == ""
    assert not canvas.has_text_selection()


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

def test_copy_puts_the_selection_on_the_system_clipboard(canvas):
    QApplication.clipboard().clear()
    _drag(canvas, _at(canvas, "4100-PU-001"), _at(canvas, "4100-PU-001"))
    assert canvas.copy_selected() == 0     # no annotations were taken
    assert QApplication.clipboard().text() == "4100-PU-001"


def test_copy_prefers_annotations_when_both_are_selected(canvas):
    """Ctrl+V pastes from the annotation clipboard, so an annotation selection
    has to keep winning or copy/paste of markup silently breaks."""
    QApplication.clipboard().setText("untouched")
    item = HighlightItem(_rect(10, 10, 40, 20), QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    _drag(canvas, _at(canvas, "PUMP"), _at(canvas, "PUMP"))
    item.setSelected(True)
    assert canvas.copy_selected() == 1
    assert QApplication.clipboard().text() == "untouched"


def test_copy_with_nothing_selected_touches_nothing(canvas):
    QApplication.clipboard().setText("untouched")
    assert canvas.copy_selected() == 0
    assert QApplication.clipboard().text() == "untouched"


# ---------------------------------------------------------------------------
# Double and triple click
# ---------------------------------------------------------------------------

def test_double_click_selects_one_word(canvas):
    _double_click(canvas, _at(canvas, "4100-PU-001"))
    assert canvas.selected_text() == "4100-PU-001"


def test_triple_click_takes_the_whole_line(canvas):
    """Qt has no triple-click event, so the third press is counted by hand:
    soon after the double-click and on the same spot."""
    _double_click(canvas, _at(canvas, "4100-PU-001"))
    _press(canvas, _at(canvas, "4100-PU-001"))
    assert canvas.selected_text() == "PUMP 4100-PU-001 SUCTION"


def test_a_press_somewhere_else_is_not_a_triple_click(canvas):
    _double_click(canvas, _at(canvas, "4100-PU-001"))
    _press(canvas, _at(canvas, "VALVE"))
    assert canvas.selected_text() == "VALVE"


# ---------------------------------------------------------------------------
# Select all
# ---------------------------------------------------------------------------

def test_ctrl_a_selects_the_page_text_when_nothing_is_drawn_on_it(canvas):
    _key(canvas, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert canvas.selected_text() == ("PUMP 4100-PU-001 SUCTION\n"
                                      "VALVE 4100-HV-002 OPEN")


def test_ctrl_a_still_selects_the_annotations_when_there_are_any(canvas):
    """The old meaning has to survive. Text is what Ctrl+A falls back to on a
    page carrying no markup, where it used to do nothing at all."""
    item = HighlightItem(_rect(10, 10, 40, 20), QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    _key(canvas, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert item.isSelected()
    assert not canvas.has_text_selection()


# ---------------------------------------------------------------------------
# Pages with no text layer
# ---------------------------------------------------------------------------

def test_a_page_with_no_text_layer_has_nothing_to_select(canvas):
    canvas.set_page(1, immediate=True)
    assert canvas.page_words() == []
    assert not canvas.page_has_selectable_text()


def test_dragging_on_a_page_with_no_text_layer_is_a_clean_no_op(canvas):
    canvas.set_page(1, immediate=True)
    _drag(canvas, _at(canvas, "PUMP"), _at(canvas, "SUCTION"))
    assert canvas.selected_text() == ""
    assert not canvas.has_text_selection()


def test_double_clicking_a_page_with_no_text_layer_does_not_raise(canvas):
    canvas.set_page(1, immediate=True)
    _double_click(canvas, _at(canvas, "PUMP"))
    assert not canvas.has_text_selection()


def test_select_all_on_a_page_with_no_text_layer_reports_it_found_none(canvas):
    canvas.set_page(1, immediate=True)
    assert canvas.select_all_text() is False
    assert not canvas.has_text_selection()


# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------

def test_the_selection_clears_on_a_page_change(canvas):
    _double_click(canvas, _at(canvas, "PUMP"))
    assert canvas.has_text_selection()
    canvas.set_page(1, immediate=True)
    assert not canvas.has_text_selection()
    assert canvas._text_sel_items == []


def test_the_selection_clears_on_a_tool_change(canvas):
    _double_click(canvas, _at(canvas, "PUMP"))
    assert canvas.has_text_selection()
    canvas.set_tool("rect")
    assert not canvas.has_text_selection()
    assert canvas._text_sel_items == []


def test_the_selection_clears_when_the_next_click_lands_on_empty_page(canvas):
    _double_click(canvas, _at(canvas, "PUMP"))
    _press(canvas, _blank(canvas))
    _release(canvas, _blank(canvas))
    assert not canvas.has_text_selection()


def test_the_selection_clears_when_the_next_click_lands_on_an_annotation(canvas):
    item = HighlightItem(_rect(10, 10, 40, 20), QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    _double_click(canvas, _at(canvas, "PUMP"))
    _press(canvas, QPointF(30, 20))
    _release(canvas, QPointF(30, 20))
    assert not canvas.has_text_selection()
    assert item.isSelected()


def test_escape_drops_the_selection(canvas):
    _double_click(canvas, _at(canvas, "PUMP"))
    _key(canvas, Qt.Key.Key_Escape)
    assert not canvas.has_text_selection()


def test_a_new_document_leaves_no_selection_behind(qt_app, tmp_path, canvas):
    _double_click(canvas, _at(canvas, "PUMP"))
    other = PDFDocument()
    assert other.open(_text_pdf(tmp_path, "other.pdf"))
    try:
        canvas.set_document(other)
        canvas._flush_pending_render()
        assert not canvas.has_text_selection()
        assert canvas._text_sel_items == []
    finally:
        other.close()


# ---------------------------------------------------------------------------
# The regression risk: the rest of the pointer
# ---------------------------------------------------------------------------

def test_drawing_a_rectangle_over_text_still_makes_one(canvas):
    canvas.set_tool("rect")
    # Corner to corner across both lines: a zero-height drag is thrown away as a
    # stray click, which has nothing to do with text selection.
    _drag(canvas, _at(canvas, "PUMP"), _at(canvas, "OPEN"))
    made = canvas._page_annotations.get(0, [])
    assert len(made) == 1
    assert made[0].ann_type == "rect"


def test_drawing_a_highlight_from_the_toolbar_is_untouched(canvas):
    canvas.set_tool("line")
    _drag(canvas, _at(canvas, "PUMP"), _at(canvas, "OPEN"))
    assert len(canvas._page_annotations.get(0, [])) == 1


def test_an_annotation_sitting_on_text_still_drags(canvas):
    """The press lands on both a word and an annotation. The annotation wins,
    and the drag has to be a real move with a real undo step behind it."""
    box = WORD_BOXES_PT["4100-PU-001"]
    z = canvas._zoom
    item = HighlightItem(_rect(box.x0 * z, box.y0 * z,
                               (box.x1 - box.x0) * z, (box.y1 - box.y0) * z),
                         QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    before = item.pos()
    start = _at(canvas, "4100-PU-001")
    _drag(canvas, start, start + QPointF(60, 40))
    assert item.pos() != before
    assert not canvas.has_text_selection()
    assert canvas.undo_stack.count() == 1
    canvas.undo_stack.undo()
    assert item.pos() == before


def test_a_resize_handle_over_text_still_wins_the_press(canvas):
    box = WORD_BOXES_PT["4100-PU-001"]
    z = canvas._zoom
    item = HighlightItem(_rect(box.x0 * z, box.y0 * z,
                               (box.x1 - box.x0) * z, (box.y1 - box.y0) * z),
                         QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    item.setSelected(True)
    corner = item.scene_rect().bottomRight()
    _press(canvas, corner)
    assert canvas._resize_item is item
    assert not canvas.has_text_selection()
    _release(canvas, corner)


def test_a_marquee_from_empty_page_still_picks_up_annotations(canvas):
    item = HighlightItem(_rect(280 * canvas._zoom, 300 * canvas._zoom, 40, 20),
                         QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    _drag(canvas, QPointF(250 * canvas._zoom, 280 * canvas._zoom),
          QPointF(360 * canvas._zoom, 360 * canvas._zoom))
    assert item.isSelected()


def test_ctrl_press_on_a_word_still_duplicates_the_annotation_under_it(canvas):
    box = WORD_BOXES_PT["PUMP"]
    z = canvas._zoom
    item = HighlightItem(_rect(box.x0 * z, box.y0 * z,
                               (box.x1 - box.x0) * z, (box.y1 - box.y0) * z),
                         QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    item.setSelected(True)
    start = _at(canvas, "PUMP")
    _drag(canvas, start, start + QPointF(50, 50),
          mods=Qt.KeyboardModifier.ControlModifier)
    assert len(canvas._page_annotations.get(0, [])) == 2


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_word_boxes_land_on_the_ink_however_the_page_is_rotated(qt_app, tmp_path,
                                                                rotation):
    """Ground truth is the rendered pixels, not a second copy of the formula.

    The union of the word rectangles must cover every dark pixel on the page and
    must not be wildly bigger than them. With `rotation_matrix` dropped from the
    transform this passes at 0 and fails at 90, 180 and 270, which is the whole
    reason it is parametrized.
    """
    path = _text_pdf(tmp_path, f"rot{rotation}.pdf", rotation=rotation)
    canvas, doc = _canvas_for(path, 1.5)
    try:
        z = canvas._zoom
        page = doc.doc[0]
        ink = _ink_bbox(page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False))

        words = canvas.page_words()
        assert len(words) == 6
        union = words[0].rect
        for w in words[1:]:
            union = union.united(w.rect)

        pad = 2.0
        assert union.left() - pad <= ink[0]
        assert union.top() - pad <= ink[1]
        assert union.right() + pad >= ink[2]
        assert union.bottom() + pad >= ink[3]
        # Tight, not "the whole page happens to contain the ink".
        ink_area = (ink[2] - ink[0]) * (ink[3] - ink[1])
        assert union.width() * union.height() < ink_area * 2.0
    finally:
        canvas.deleteLater()
        doc.close()


@pytest.mark.parametrize("rotation", [90, 270])
def test_a_drag_on_a_rotated_page_selects_the_words_it_crossed(qt_app, tmp_path,
                                                               rotation):
    path = _text_pdf(tmp_path, f"pick{rotation}.pdf", rotation=rotation)
    canvas, doc = _canvas_for(path, 2.0)
    try:
        by_text = {w.text: w for w in canvas.page_words()}
        _drag(canvas, by_text["PUMP"].rect.center(),
              by_text["SUCTION"].rect.center())
        assert canvas.selected_text() == "PUMP 4100-PU-001 SUCTION"
    finally:
        canvas.deleteLater()
        doc.close()


# ---------------------------------------------------------------------------
# The signal, for whoever wires a status line to it
# ---------------------------------------------------------------------------

def test_the_selection_signal_reports_the_length_and_then_zero(canvas):
    seen = []
    canvas.text_selection_changed.connect(seen.append)
    _double_click(canvas, _at(canvas, "4100-PU-001"))
    assert seen[-1] == len("4100-PU-001")
    canvas.clear_text_selection()
    assert seen[-1] == 0
