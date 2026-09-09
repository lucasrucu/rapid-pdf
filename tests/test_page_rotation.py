"""Turning pages: the document, the two panels, the undo stack, and the markup.

A commissioning engineer's day is scanned check sheets and drawings that come
in sideways, so rotation is the page edit that gets used most. These tests are
in four groups.

ARITHMETIC. core/page_ops.py turns a page by plain numbers rather than by
asking PyMuPDF for a matrix, so the first group pins that arithmetic against
PyMuPDF's own `rotation_matrix` and `derotation_matrix` for every base
rotation and every turn. If the two ever disagree, this is the test that says
so.

THE DOCUMENT. /Rotate changes, the render is really turned, four rights come
back to where they started, and a page that arrived at 270 turns from 270
rather than from zero.

ANNOTATIONS, which is the part that could have gone wrong quietly. Annotation
geometry is stored in the page's own UNROTATED user space and the viewer
composes /Rotate on top when it draws, so a rotation moves the markup and the
content together and neither needs repairing. That is proved here rather than
asserted: once at the PyMuPDF level, and once end to end through the app, a
save, and a plain reopen with no rapid-pdf code in the loop.

THE PANELS. The same turn, driven through the left thumbnail strip and through
the Organizer, has to leave the document and the undo stack in the same state.
That is the pairing test_undo_consistency.py established for delete and
reorder, applied to the third page edit.

Everything is the real widget, the real window undo stack and a real PDF on
disk. Runs offscreen (see conftest).
"""

import fitz
import pytest

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QShortcut
from PySide6.QtWidgets import QApplication

from core.page_ops import (
    ROTATE_180, ROTATE_CCW, ROTATE_CW, ROTATIONS, normalize_rotation,
    rotate_point, rotate_rect, rotate_rect_upright, rotated_size,
    rotation_after,
)
from ui.canvas import (
    AddItemsCommand, HighlightItem, LineAnnotationItem, TextAnnotationItem,
)
from ui.main_window import MainWindow
from ui.organizer import _PAGE_ID
from ui.page_commands import ROTATE_SHORTCUTS, RotatePagesCommand

EDITOR_TAB = 0
ORGANIZER_TAB = 1

# The page the fixture builds, and the black block printed on page 0. Both are
# in PDF user space, which is where they stay however the page is turned.
PAGE_W, PAGE_H = 200.0, 300.0
BLOCK = fitz.Rect(20, 30, 80, 60)


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


def _build_pdf(path, rotations=(0, 0, 0, 0)):
    """Four portrait pages. Page 0 carries a black block at a known spot."""
    raw = fitz.open()
    for index, rotation in enumerate(rotations):
        page = raw.new_page(width=PAGE_W, height=PAGE_H)
        if index == 0:
            page.draw_rect(BLOCK, color=(0, 0, 0), fill=(0, 0, 0))
        else:
            page.insert_text((20, 100), "ABCD"[index], fontsize=48)
        if rotation:
            page.set_rotation(rotation)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def pdf_path(tmp_path):
    return _build_pdf(tmp_path / "four.pdf")


@pytest.fixture
def tilted_pdf_path(tmp_path):
    """Page 1 arrives already at 270, the way a sideways scan does."""
    return _build_pdf(tmp_path / "tilted.pdf", rotations=(0, 270, 0, 0))


def _open_window(path):
    window = MainWindow()
    window.open_paths([path])
    return window


@pytest.fixture
def win(qt_app, pdf_path):
    window = _open_window(pdf_path)
    yield window
    window.view._doc.close()
    window.view._close_panel_render()
    window.view._close_org_render()
    window.deleteLater()


@pytest.fixture
def tilted_win(qt_app, tilted_pdf_path):
    window = _open_window(tilted_pdf_path)
    yield window
    window.view._doc.close()
    window.view._close_panel_render()
    window.view._close_org_render()
    window.deleteLater()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rotations(window) -> list:
    doc = window.view._doc
    return [doc.doc[i].rotation for i in range(doc.page_count())]


def _stack(window):
    return window.view._canvas.undo_stack


def _panel(window):
    return window.view._page_panel


def _organizer(window):
    return window.view._organizer


def _show_organizer(window):
    window.view._tabs.setCurrentIndex(ORGANIZER_TAB)
    QApplication.processEvents()


def _select_strip_rows(window, rows):
    strip = _panel(window)._list
    strip.clearSelection()
    for row in rows:
        item = strip.item(row)
        assert item is not None, f"no row {row} in the strip"
        item.setSelected(True)


def _select_grid_rows(window, rows):
    grid = _organizer(window)._list
    grid.clearSelection()
    for row in rows:
        for i in range(grid.count()):
            if grid.item(i).data(_PAGE_ID) == row:
                grid.item(i).setSelected(True)


def _rotate_through_strip(window, rows, delta):
    _select_strip_rows(window, rows)
    return _panel(window).rotate_selection(delta)


def _rotate_through_grid(window, rows, delta):
    _show_organizer(window)
    _select_grid_rows(window, rows)
    return _organizer(window).rotate_selected(delta)


def _render_bytes(doc, page_num, zoom=1.0):
    pix = doc[page_num].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.width, pix.height, pix.n, bytes(pix.samples), pix.stride


def _turned_clockwise(before, after) -> int:
    """How many pixels of `after` disagree with `before` turned 90 clockwise.

    Never exactly zero on real content: MuPDF antialiases the edges of a shape
    and the samples it lays down along a horizontal edge are not bit-identical
    to the ones along the vertical edge it becomes. So the caller compares
    against a small budget rather than against zero.
    """
    bw, bh, bn, bs, bstride = before
    aw, ah, an, asamples, astride = after
    assert (aw, ah) == (bh, bw), "a quarter turn has to swap the raster"
    wrong = 0
    for y in range(bh):
        for x in range(bw):
            nx, ny = bh - 1 - y, x
            b = bs[y * bstride + x * bn:y * bstride + x * bn + bn]
            a = asamples[ny * astride + nx * an:ny * astride + nx * an + an]
            if b != a:
                wrong += 1
    return wrong


# ---------------------------------------------------------------------------
# The arithmetic, against PyMuPDF's own matrices
# ---------------------------------------------------------------------------

def test_normalize_rotation_folds_anything_onto_a_quarter_turn():
    assert normalize_rotation(0) == 0
    assert normalize_rotation(90) == 90
    assert normalize_rotation(360) == 0
    assert normalize_rotation(450) == 90
    assert normalize_rotation(-90) == 270
    # PyMuPDF silently drops a value that is not a multiple of 90 (measured on
    # 1.27.2.3: set_rotation(91) leaves the page at 0), so anything off the
    # quarter is snapped here instead of reaching the document.
    assert normalize_rotation(91) == 90
    assert normalize_rotation("nonsense") == 0


def test_rotation_after_wraps_and_composes():
    assert rotation_after(0, ROTATE_CW) == 90
    assert rotation_after(270, ROTATE_CW) == 0
    assert rotation_after(270, ROTATE_CCW) == 180
    assert rotation_after(90, ROTATE_180) == 270


def test_rotated_size_swaps_only_on_a_quarter_turn():
    assert rotated_size(200, 300, ROTATE_CW) == (300, 200)
    assert rotated_size(200, 300, ROTATE_CCW) == (300, 200)
    assert rotated_size(200, 300, ROTATE_180) == (200, 300)
    assert rotated_size(200, 300, 0) == (200, 300)


@pytest.mark.parametrize("base", ROTATIONS)
@pytest.mark.parametrize("delta", (ROTATE_CW, ROTATE_180, ROTATE_CCW))
def test_the_arithmetic_is_pymupdfs_own_matrix(base, delta, subtests):
    """rotate_point == derotation_matrix(base) * rotation_matrix(base + delta).

    The claim core/page_ops.py makes in prose, checked against the library it
    is standing in for. Every base rotation, every turn, at the corners and in
    the middle of the page.
    """
    doc = fitz.open()
    doc.new_page(width=PAGE_W, height=PAGE_H)
    page = doc[0]
    try:
        page.set_rotation(base)
        box = page.bound()
        derot = page.derotation_matrix
        page.set_rotation(rotation_after(base, delta))
        rot = page.rotation_matrix
        for point in ((0, 0), (10, 10), (60, 40), (37.5, 211.25),
                      (box.width, box.height)):
            with subtests.test(point=point):
                expect = fitz.Point(point) * derot * rot
                got = rotate_point(point[0], point[1], delta,
                                   box.width, box.height)
                assert got[0] == pytest.approx(expect.x, abs=1e-6)
                assert got[1] == pytest.approx(expect.y, abs=1e-6)
    finally:
        doc.close()


def test_rotate_rect_swaps_the_box_and_stays_normalised():
    # A wide box on a 200x300 page, turned right, comes out tall.
    x0, y0, x1, y1 = rotate_rect((10, 10, 60, 40), ROTATE_CW, 200, 300)
    assert x1 > x0 and y1 > y0
    assert (x1 - x0) == pytest.approx(30)
    assert (y1 - y0) == pytest.approx(50)


def test_rotate_rect_upright_keeps_the_box_and_moves_its_centre():
    box = (10, 10, 60, 40)
    x0, y0, x1, y1 = rotate_rect_upright(box, ROTATE_CW, 200, 300)
    assert (x1 - x0) == pytest.approx(50)
    assert (y1 - y0) == pytest.approx(30)
    centre = rotate_point(35, 25, ROTATE_CW, 200, 300)
    assert ((x0 + x1) / 2, (y0 + y1) / 2) == pytest.approx(centre)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

def test_rotating_one_page_changes_rotate_and_really_turns_the_render(win):
    doc = win.view._doc
    before = _render_bytes(doc.doc, 0)
    assert _rotate_through_strip(win, [0], ROTATE_CW)
    assert _rotations(win) == [90, 0, 0, 0]
    after = _render_bytes(doc.doc, 0)
    # Not a mere /Rotate flip in the dictionary: the rasteriser hands back the
    # turned page. Budget is antialiasing along the block's edges.
    assert _turned_clockwise(before, after) < 0.02 * PAGE_W * PAGE_H


def test_the_page_box_swaps_with_a_quarter_turn(win):
    doc = win.view._doc
    assert doc.get_page_size(0) == pytest.approx((PAGE_W, PAGE_H))
    _rotate_through_strip(win, [0], ROTATE_CW)
    assert doc.get_page_size(0) == pytest.approx((PAGE_H, PAGE_W))
    _rotate_through_strip(win, [0], ROTATE_180)
    assert doc.get_page_size(0) == pytest.approx((PAGE_H, PAGE_W))


def test_four_rights_come_back_to_the_original(win):
    doc = win.view._doc
    before = _render_bytes(doc.doc, 0)
    for _ in range(4):
        _rotate_through_strip(win, [0], ROTATE_CW)
    assert _rotations(win) == [0, 0, 0, 0]
    assert _render_bytes(doc.doc, 0) == before


def test_left_and_right_are_opposites(win):
    _rotate_through_strip(win, [0], ROTATE_CW)
    assert _rotations(win)[0] == 90
    _rotate_through_strip(win, [0], ROTATE_CCW)
    assert _rotations(win)[0] == 0


def test_a_page_that_arrived_rotated_turns_from_where_it_is(tilted_win):
    """The sideways scan case: /Rotate 270 on open, one right turn to upright."""
    assert _rotations(tilted_win) == [0, 270, 0, 0]
    doc = tilted_win.view._doc
    # Its visible box is already landscape before anything is asked of it.
    assert doc.get_page_size(1) == pytest.approx((PAGE_H, PAGE_W))
    assert _rotate_through_strip(tilted_win, [1], ROTATE_CW)
    # 270 + 90, not 0 + 90.
    assert _rotations(tilted_win) == [0, 0, 0, 0]
    assert doc.get_page_size(1) == pytest.approx((PAGE_W, PAGE_H))


def test_a_rotation_is_flagged_for_the_signature_warning(win):
    doc = win.view._doc
    doc._structure_changed = False
    _rotate_through_strip(win, [0], ROTATE_CW)
    # A signature covers the pages it was applied to, and a turned page is not
    # the page it signed, so save_plan has to warn before it writes.
    assert doc._structure_changed is True


def test_rotating_drops_only_that_pages_render_cache(win):
    doc = win.view._doc
    for page in range(4):
        doc.render_page_cached(page, 1.0)
    assert {key[0] for key in doc._render_cache} == {0, 1, 2, 3}
    _rotate_through_strip(win, [2], ROTATE_CW)
    assert 2 not in {key[0] for key in doc._render_cache}
    assert {0, 1, 3} <= {key[0] for key in doc._render_cache}


# ---------------------------------------------------------------------------
# Undo and redo
# ---------------------------------------------------------------------------

def test_undo_and_redo_restore_the_exact_rotation(win):
    _rotate_through_strip(win, [0, 2], ROTATE_CW)
    assert _rotations(win) == [90, 0, 90, 0]
    _stack(win).undo()
    assert _rotations(win) == [0, 0, 0, 0]
    _stack(win).redo()
    assert _rotations(win) == [90, 0, 90, 0]


def test_undo_returns_a_page_to_the_rotation_it_was_opened_with(tilted_win):
    """The one an undo that assumed zero would get wrong."""
    assert _rotations(tilted_win)[1] == 270
    _rotate_through_strip(tilted_win, [1], ROTATE_CW)
    assert _rotations(tilted_win)[1] == 0
    _stack(tilted_win).undo()
    assert _rotations(tilted_win)[1] == 270
    _stack(tilted_win).redo()
    assert _rotations(tilted_win)[1] == 0
    _stack(tilted_win).undo()
    assert _rotations(tilted_win)[1] == 270


def test_a_mixed_selection_undoes_page_by_page(tilted_win):
    """Pages at different starting rotations turn together and come back apart."""
    _rotate_through_strip(tilted_win, [0, 1, 2], ROTATE_CCW)
    assert _rotations(tilted_win) == [270, 180, 270, 0]
    _stack(tilted_win).undo()
    assert _rotations(tilted_win) == [0, 270, 0, 0]


def test_a_rotation_is_one_undo_step_however_many_pages(win):
    stack = _stack(win)
    before = stack.count()
    _rotate_through_strip(win, [0, 1, 2, 3], ROTATE_180)
    assert stack.count() == before + 1
    assert _rotations(win) == [180, 180, 180, 180]
    stack.undo()
    assert _rotations(win) == [0, 0, 0, 0]


def test_rotating_marks_the_document_unsaved_and_undo_clears_it(win):
    view = win.view
    assert not view.is_dirty()
    _rotate_through_strip(win, [0], ROTATE_CW)
    assert view.is_dirty()
    _stack(win).undo()
    assert not view.is_dirty()


def test_an_annotation_edit_under_a_rotation_is_still_undoable(win):
    """The bug the undo work removed, checked for the new command too."""
    canvas = win.view._canvas
    item = HighlightItem(QRectF(10, 10, 40, 20), QColor("yellow"), 0.4, 0)
    canvas._attach_item(item)
    canvas.undo_stack.push(AddItemsCommand(canvas, [item], "Highlight"))
    _rotate_through_strip(win, [0], ROTATE_CW)
    stack = _stack(win)
    stack.undo()                       # the rotation
    assert _rotations(win)[0] == 0
    stack.undo()                       # the highlight, still there to undo
    assert item not in canvas._page_annotations.get(0, [])


def test_a_rotation_with_no_selection_and_no_document_is_refused(qt_app):
    window = MainWindow()
    try:
        assert _panel(window).rotate_selection(ROTATE_CW) is False
        assert _organizer(window).rotate_selected(ROTATE_CW) is False
    finally:
        window.deleteLater()


def test_a_zero_turn_pushes_nothing(win):
    stack = _stack(win)
    before = stack.count()
    assert _panel(win).rotate_selection(0) is False
    assert _panel(win).rotate_selection(360) is False
    assert stack.count() == before


# ---------------------------------------------------------------------------
# Annotations under a rotation
# ---------------------------------------------------------------------------

def test_pymupdf_keeps_annotation_geometry_in_unrotated_page_space(tmp_path):
    """The load-bearing claim, at the library level, with no app in the way.

    Annotation coordinates are stored in the page's own user space and /Rotate
    is composed on top at draw time, so turning the page moves the annotation
    and the content it sits on together, by exactly the same amount, with no
    stored geometry touched. That is why RotatePagesCommand does not transform
    anything that is already in the file.
    """
    path = tmp_path / "annotated.pdf"
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.draw_rect(BLOCK, color=(0, 0, 0), fill=(0, 0, 0))
    annot = page.add_highlight_annot(BLOCK.quad)
    annot.set_colors(stroke=(1, 0, 0))
    annot.update()
    doc.save(str(path))
    doc.close()

    doc = fitz.open(str(path))
    try:
        page = doc[0]
        first = next(page.annots())
        before_rect = fitz.Rect(first.rect)
        before_vertices = list(first.vertices)
        before = _render_bytes(doc, 0)

        page.set_rotation(90)

        after_annot = next(page.annots())
        assert fitz.Rect(after_annot.rect) == before_rect
        assert list(after_annot.vertices) == before_vertices
        # And the drawn result is the whole page turned, block and marker pen
        # together, not the block turning while the marker stayed put.
        assert _turned_clockwise(before, _render_bytes(doc, 0)) < 0.02 * PAGE_W * PAGE_H
    finally:
        doc.close()


def _highlight_covers(rect: fitz.Rect, target: fitz.Rect) -> bool:
    """Is `rect` the marker-pen box MuPDF draws around `target`?

    add_highlight_annot pads the quad it is given (measured on 1.27.2.3: about
    h/16 top and bottom, about h*0.2357 left and right), so the annotation rect
    is bigger than the box it was asked for. It still has to CONTAIN the target
    and sit near it, which is what "attached to its content" means here.
    """
    pad = max(target.width, target.height)
    return (rect.contains(target)
            and abs(rect.x0 - target.x0) < pad
            and abs(rect.y0 - target.y0) < pad
            and abs(rect.x1 - target.x1) < pad
            and abs(rect.y1 - target.y1) < pad)


def test_markup_drawn_in_the_app_survives_a_rotation_still_on_its_content(win):
    """End to end: draw over the block, turn the page, save, reopen plainly.

    The check is done with no rapid-pdf code in the loop on the reading side:
    a plain fitz.open of the saved file has to show the highlight still on top
    of the black block, in the page's user space, where the block still is.
    """
    view = win.view
    canvas = view._canvas
    scale = view._doc.render_scale()
    # A highlight laid exactly over the block, in scene (rendered pixel) space.
    scene_box = QRectF(BLOCK.x0 * scale, BLOCK.y0 * scale,
                       BLOCK.width * scale, BLOCK.height * scale)
    item = HighlightItem(scene_box, QColor("yellow"), 0.4, 0)
    canvas._attach_item(item)
    canvas.undo_stack.push(AddItemsCommand(canvas, [item], "Highlight"))

    _rotate_through_strip(win, [0], ROTATE_CW)
    assert view.save_pdf() is True

    saved = fitz.open(view._doc.path)
    try:
        page = saved[0]
        assert page.rotation == 90
        marks = [a for a in page.annots() if a.type[0] == fitz.PDF_ANNOT_HIGHLIGHT]
        assert len(marks) == 1
        # Still bracketing the block, in the page's own unrotated user space.
        assert _highlight_covers(fitz.Rect(marks[0].rect), BLOCK)
    finally:
        saved.close()


def test_markup_moves_with_the_page_and_comes_back_on_undo(win):
    """The canvas half: unsaved items are in visible space, so they have to move.

    Left alone they would keep their old rendered-pixel coordinates while the
    page turned under them, which is a highlight sliding off the line it was
    drawn on. Undo has to put the exact geometry back, not an approximation of
    it.
    """
    view = win.view
    canvas = view._canvas
    scale = view._doc.render_scale()
    scene_w, scene_h = PAGE_W * scale, PAGE_H * scale
    box = QRectF(10.0, 20.0, 50.0, 30.0)
    item = HighlightItem(QRectF(box), QColor("yellow"), 0.4, 0)
    canvas._attach_item(item)
    canvas.undo_stack.push(AddItemsCommand(canvas, [item], "Highlight"))

    _rotate_through_strip(win, [0], ROTATE_CW)
    turned = item.scene_rect()
    x0, y0, x1, y1 = rotate_rect(
        (box.left(), box.top(), box.right(), box.bottom()),
        ROTATE_CW, scene_w, scene_h)
    assert (turned.left(), turned.top(), turned.right(), turned.bottom()) \
        == pytest.approx((x0, y0, x1, y1))
    # The box stood up: what was 50 wide by 30 tall is now 30 by 50.
    assert turned.width() == pytest.approx(box.height())
    assert turned.height() == pytest.approx(box.width())

    _stack(win).undo()
    back = item.scene_rect()
    assert (back.left(), back.top(), back.right(), back.bottom()) \
        == pytest.approx((box.left(), box.top(), box.right(), box.bottom()))


def test_a_line_turns_with_the_page(win):
    view = win.view
    canvas = view._canvas
    scale = view._doc.render_scale()
    scene_w, scene_h = PAGE_W * scale, PAGE_H * scale
    line = QLineF(10.0, 20.0, 90.0, 20.0)
    item = LineAnnotationItem(QLineF(line), QColor("red"), 1.0, 0, 2.0)
    canvas._attach_item(item)
    canvas.undo_stack.push(AddItemsCommand(canvas, [item], "Line"))

    _rotate_through_strip(win, [0], ROTATE_CW)
    turned = item.line()
    p1 = rotate_point(line.x1(), line.y1(), ROTATE_CW, scene_w, scene_h)
    p2 = rotate_point(line.x2(), line.y2(), ROTATE_CW, scene_w, scene_h)
    assert (turned.x1(), turned.y1()) == pytest.approx(p1)
    assert (turned.x2(), turned.y2()) == pytest.approx(p2)
    # A horizontal line came out vertical.
    assert turned.x1() == pytest.approx(turned.x2())

    _stack(win).undo()
    assert (item.line().x1(), item.line().y1()) == pytest.approx((line.x1(), line.y1()))
    assert (item.line().x2(), item.line().y2()) == pytest.approx((line.x2(), line.y2()))


def test_a_text_label_travels_but_stays_upright(win):
    """A label cannot draw itself sideways, so it keeps its size and moves by
    its centre. Standing the words on end would be worse than moving them."""
    view = win.view
    canvas = view._canvas
    scale = view._doc.render_scale()
    scene_w, scene_h = PAGE_W * scale, PAGE_H * scale
    item = TextAnnotationItem(QPointF(30.0, 40.0), "TAG 4100",
                              QColor("black"), 12, 0)
    canvas._attach_item(item)
    canvas.undo_stack.push(AddItemsCommand(canvas, [item], "Text"))
    before = item.scene_text_rect()

    _rotate_through_strip(win, [0], ROTATE_CW)
    after = item.scene_text_rect()
    assert after.width() == pytest.approx(before.width())
    assert after.height() == pytest.approx(before.height())
    centre = rotate_point(before.center().x(), before.center().y(),
                          ROTATE_CW, scene_w, scene_h)
    assert (after.center().x(), after.center().y()) == pytest.approx(centre)

    _stack(win).undo()
    assert item.pos().x() == pytest.approx(30.0)
    assert item.pos().y() == pytest.approx(40.0)


def test_markup_on_a_page_that_was_not_turned_does_not_move(win):
    canvas = win.view._canvas
    item = HighlightItem(QRectF(10, 20, 50, 30), QColor("yellow"), 0.4, 2)
    canvas._attach_item(item)
    canvas.undo_stack.push(AddItemsCommand(canvas, [item], "Highlight"))
    before = item.scene_rect()
    _rotate_through_strip(win, [0, 1], ROTATE_CW)
    after = item.scene_rect()
    assert (after.left(), after.top()) == pytest.approx((before.left(), before.top()))


# ---------------------------------------------------------------------------
# The two panels, side by side
# ---------------------------------------------------------------------------

def test_both_panels_produce_the_same_rotation(win, tilted_win):
    """Same turn, same multi-page selection, one through each panel."""
    assert _rotate_through_strip(win, [0, 2], ROTATE_CW)
    # tilted_win is a second window on a different file; give it the same
    # starting rotations first so the comparison is like for like.
    tilted_win.view._doc.doc[1].set_rotation(0)
    assert _rotate_through_grid(tilted_win, [0, 2], ROTATE_CW)
    assert _rotations(win) == _rotations(tilted_win)


def test_both_panels_leave_the_same_undo_state(win, tilted_win):
    tilted_win.view._doc.doc[1].set_rotation(0)
    _rotate_through_strip(win, [1, 3], ROTATE_CCW)
    _rotate_through_grid(tilted_win, [1, 3], ROTATE_CCW)

    for window in (win, tilted_win):
        assert _stack(window).count() == 1
        assert _stack(window).command(0).text() == "Rotate 2 pages"
    assert _rotations(win) == _rotations(tilted_win)

    _stack(win).undo()
    _stack(tilted_win).undo()
    assert _rotations(win) == _rotations(tilted_win) == [0, 0, 0, 0]

    _stack(win).redo()
    _stack(tilted_win).redo()
    assert _rotations(win) == _rotations(tilted_win)


def test_the_grid_rotates_from_its_own_selection(win):
    _rotate_through_grid(win, [3], ROTATE_180)
    assert _rotations(win) == [0, 0, 0, 180]


def test_the_strip_falls_back_to_the_page_on_screen(win):
    strip = _panel(win)._list
    win.view.jump_to_page(2)
    strip.clearSelection()
    assert _panel(win).rotate_rows() == [2]
    assert _panel(win).rotate_selection(ROTATE_CW)
    assert _rotations(win) == [0, 0, 90, 0]


def test_the_grid_falls_back_to_the_current_cell(win):
    _show_organizer(win)
    grid = _organizer(win)._list
    grid.clearSelection()
    grid.setCurrentRow(1)
    grid.clearSelection()
    assert _organizer(win).rotate_rows() == [1]


def test_both_panels_keep_the_turned_pages_selected(win):
    _rotate_through_strip(win, [1, 2], ROTATE_CW)
    assert _panel(win).selected_rows() == [1, 2]


def test_the_rotate_buttons_follow_the_document(win):
    """Tied to the document the same way Delete Selected is."""
    org = _organizer(win)
    assert len(org._rotate_btns) == 2
    assert all(btn.isEnabled() for btn in org._rotate_btns.values())
    org._doc = None
    org._update_buttons()
    assert not any(btn.isEnabled() for btn in org._rotate_btns.values())


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def _our_keys() -> set:
    return {sequence for sequence, _ in ROTATE_SHORTCUTS}


def _installed(widget, keys=None) -> list:
    wanted = _our_keys() if keys is None else keys
    return [s for s in widget.findChildren(QShortcut)
            if s.key().toString() in wanted]


def test_the_rotate_keys_collide_with_nothing_already_bound(win):
    """Ctrl+R and Ctrl+Shift+R against every other binding in the window.

    The tool letters, the menu actions and the Organizer's zoom keys are all
    real QKeySequences hanging off the window, so this is a set comparison
    against what is actually bound rather than a reading of docs/shortcuts.md.
    """
    ours = _our_keys()
    taken = set()
    for action in win.findChildren(QAction):
        if "Rotate" in action.text():
            # A Page-menu rotate action bound to the same key is this feature,
            # not a collision. Skipping it here is what lets ui/main_window.py
            # grow one later without this guard turning into a false alarm.
            continue
        for sequence in action.shortcuts():
            taken.add(sequence.toString())
    for shortcut in win.findChildren(QShortcut):
        text = shortcut.key().toString()
        if text not in ours:      # our own two are in here; skip them
            taken.add(text)
    # Sanity: the set really did pick the existing bindings up.
    assert {"Ctrl+S", "Ctrl+O", "Ctrl+B", "Ctrl+G", "Ctrl+0"} <= taken
    assert ours.isdisjoint(taken), sorted(ours & taken)


def test_the_rotate_keys_are_scoped_to_the_panel_they_are_installed_on(win):
    """WidgetWithChildrenShortcut, so several open documents cannot make one
    key ambiguous and therefore dead. See PagePanel._install_rotate_shortcuts."""
    for widget in (_panel(win), _organizer(win)):
        installed = _installed(widget)
        assert len(installed) == len(ROTATE_SHORTCUTS)
        for shortcut in installed:
            assert shortcut.context() == Qt.ShortcutContext.WidgetWithChildrenShortcut


@pytest.mark.parametrize("sequence,delta", ROTATE_SHORTCUTS)
def test_each_shortcut_turns_the_selection_the_way_the_menu_would(win, sequence,
                                                                  delta):
    _select_strip_rows(win, [0])
    for shortcut in _installed(_panel(win), {sequence}):
        shortcut.activated.emit()
    assert _rotations(win) == [rotation_after(0, delta), 0, 0, 0]


def test_the_command_reports_what_it_did(win):
    command = RotatePagesCommand(win.view, [0, 1], ROTATE_CCW)
    assert command.rows() == [0, 1]
    assert command.delta() == 270
    assert command.direction() == "left"
    assert command.text() == "Rotate 2 pages"
    assert RotatePagesCommand(win.view, [0], ROTATE_CW).text() == "Rotate page"
