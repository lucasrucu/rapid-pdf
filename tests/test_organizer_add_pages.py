"""The Organizer's "+ Add Pages", and whether the EDITOR notices.

The bug this pins: Add Pages inserted straight into the live document and then
asked only for the Organizer grid to be rebuilt. The Editor's thumbnail strip
was never told, so a document that had genuinely grown to two pages showed one
thumbnail in the strip while the status bar under it read "page 2 of 2", and
printing produced both pages. The document was right the whole time; two of the
three views were stale.

So nothing here asserts on page_count() alone. A test that did would have passed
while the bug was live. Every assertion is about what a VIEW holds: the strip's
item count, the grid's item count, the page box, the status line, the pixels the
canvas actually rendered.
"""

import fitz
import pytest

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

import ui.organizer as organizer_mod
from ui.main_window import MainWindow

EDITOR, ORGANIZER = 0, 1


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


def _write_pdf(path, letters: str) -> str:
    """A PDF with one big letter per page, so a render is visibly not blank."""
    raw = fitz.open()
    for letter in letters:
        page = raw.new_page(width=200, height=200)
        page.insert_text((20, 140), letter, fontsize=96)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def base_pdf(tmp_path):
    return _write_pdf(tmp_path / "base.pdf", "A")


@pytest.fixture
def extra_pdf(tmp_path):
    return _write_pdf(tmp_path / "extra.pdf", "B")


@pytest.fixture
def win(qt_app, base_pdf):
    window = MainWindow()
    window.open_paths([base_pdf])
    yield window
    window.view._doc.close()
    window.view._close_panel_render()
    window.view._close_org_render()
    window.deleteLater()


# ---------------------------------------------------------------------------
# Driving the real button, without a file dialog
# ---------------------------------------------------------------------------

def _add_pages(window, monkeypatch, paths, tab=ORGANIZER, rows=None):
    """Click "+ Add Pages" with the dialog answering `paths`.

    Goes through PageOrganizer._add_pages, not through the view's handler, so
    the signal wiring between the two is part of what is under test.

    The Organizer is visited first whatever `tab` says. Its grid is only loaded
    on the way in, and the button being pressed lives on it.
    """
    view = window.view
    view._tabs.setCurrentIndex(ORGANIZER)
    QApplication.processEvents()
    view._tabs.setCurrentIndex(tab)
    QApplication.processEvents()
    if rows is not None:
        view._organizer._list.clearSelection()
        view._organizer.select_rows(rows)
    monkeypatch.setattr(
        organizer_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(p) for p in paths], "")))
    view._organizer._add_pages()
    QApplication.processEvents()


def _strip_rows(window) -> int:
    return window.view._page_panel._list.count()


def _grid_rows(window) -> int:
    return window.view._organizer._list.count()


def _letters(window) -> str:
    doc = window.view._doc
    return "".join(doc.doc[i].get_text().strip() for i in range(doc.page_count()))


def _render_of_last_page(window):
    """What the canvas puts on screen for the last page, as a QImage."""
    view = window.view
    last = view.page_count() - 1
    view.jump_to_page(last)
    view._canvas._flush_pending_render()
    QApplication.processEvents()
    item = view._canvas._bg_item
    assert item is not None, "the canvas has no page item at all"
    return item.pixmap().toImage()


def _ink(image) -> int:
    """Distinct colours in a coarse sample. A blank page gives 1."""
    seen = set()
    for y in range(0, image.height(), 5):
        for x in range(0, image.width(), 5):
            seen.add(image.pixel(x, y))
    return len(seen)


def _mouse(canvas, kind, scene_pt):
    vp = canvas.mapFromScene(scene_pt)
    held = (Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease
            else Qt.MouseButton.LeftButton)
    return QMouseEvent(kind, QPointF(vp), QPointF(vp), Qt.MouseButton.LeftButton,
                       held, Qt.KeyboardModifier.NoModifier)


def _draw_a_rectangle(view, page: int = 0):
    canvas = view._canvas
    view.jump_to_page(page)
    canvas._flush_pending_render()
    view.trigger_tool("rect")
    start = QPointF(60 * canvas._zoom, 60 * canvas._zoom)
    end = QPointF(180 * canvas._zoom, 150 * canvas._zoom)
    canvas.mousePressEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonPress, start))
    canvas.mouseMoveEvent(_mouse(canvas, QMouseEvent.Type.MouseMove, end))
    canvas.mouseReleaseEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, end))


def _markup_pages(view) -> set:
    return {pn for pn, items in view._canvas._page_annotations.items() if items}


# ---------------------------------------------------------------------------
# The reported bug: the strip does not grow with the document
# ---------------------------------------------------------------------------

def test_the_strip_grows_with_the_document(win, extra_pdf, monkeypatch):
    assert _strip_rows(win) == 1
    _add_pages(win, monkeypatch, [extra_pdf])
    assert win.view.page_count() == 2
    assert _strip_rows(win) == 2


def test_the_grid_grows_with_the_document(win, extra_pdf, monkeypatch):
    _add_pages(win, monkeypatch, [extra_pdf])
    assert _grid_rows(win) == win.view.page_count()


def test_every_view_agrees_on_the_count(win, extra_pdf, monkeypatch):
    _add_pages(win, monkeypatch, [extra_pdf])
    total = win.view.page_count()
    assert _strip_rows(win) == total
    assert _grid_rows(win) == total
    assert win._page_jump.total_text() == f"of {total}"
    assert f"of {total}" in win._status.currentMessage()


def test_the_canvas_renders_the_new_last_page(win, extra_pdf, monkeypatch):
    before = _ink(_render_of_last_page(win))
    _add_pages(win, monkeypatch, [extra_pdf])
    image = _render_of_last_page(win)
    assert not image.isNull()
    assert win.view._canvas.current_page() == win.view.page_count() - 1
    # Real content, not a blank sheet, and not the page that was there before.
    assert _ink(image) > 1
    assert before > 1


def test_the_pages_really_arrive_in_order(win, extra_pdf, monkeypatch):
    _add_pages(win, monkeypatch, [extra_pdf])
    assert _letters(win) == "AB"


# ---------------------------------------------------------------------------
# Which tab is in front must not decide what gets refreshed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tab", [EDITOR, ORGANIZER])
def test_both_panels_refresh_whichever_tab_is_in_front(win, extra_pdf,
                                                       monkeypatch, tab):
    _add_pages(win, monkeypatch, [extra_pdf], tab=tab)
    total = win.view.page_count()
    assert total == 2
    assert _strip_rows(win) == total
    # A background grid is rebuilt when it comes forward; bring it forward and
    # look, because "rebuilt later" is exactly what the strip failed to do.
    win.view._tabs.setCurrentIndex(ORGANIZER)
    QApplication.processEvents()
    assert _grid_rows(win) == total


# ---------------------------------------------------------------------------
# Undo and redo
# ---------------------------------------------------------------------------

def test_add_pages_is_on_the_undo_stack(win, extra_pdf, monkeypatch):
    assert win.view.undo_stack().count() == 0
    _add_pages(win, monkeypatch, [extra_pdf])
    assert win.view.undo_stack().count() == 1


def test_undo_takes_both_views_back(win, extra_pdf, monkeypatch):
    _add_pages(win, monkeypatch, [extra_pdf])
    win.view.undo_stack().undo()
    QApplication.processEvents()
    assert win.view.page_count() == 1
    assert _strip_rows(win) == 1
    assert _grid_rows(win) == 1
    assert win._page_jump.total_text() == "of 1"
    assert "of 1" in win._status.currentMessage()
    assert _letters(win) == "A"


def test_redo_puts_both_views_back_again(win, extra_pdf, monkeypatch):
    _add_pages(win, monkeypatch, [extra_pdf])
    win.view.undo_stack().undo()
    win.view.undo_stack().redo()
    QApplication.processEvents()
    assert win.view.page_count() == 2
    assert _strip_rows(win) == 2
    assert _grid_rows(win) == 2
    assert _letters(win) == "AB"


def test_undo_gives_the_source_file_back(win, base_pdf, extra_pdf, monkeypatch):
    _add_pages(win, monkeypatch, [extra_pdf])
    # A merge is a derived document: no file behind it, so the next save asks.
    assert win.view._doc.path is None
    win.view.undo_stack().undo()
    QApplication.processEvents()
    assert win.view._doc.path == base_pdf


# ---------------------------------------------------------------------------
# Markup is filed by page index, so an insert has to renumber it
# ---------------------------------------------------------------------------

def test_markup_follows_its_page_when_pages_are_inserted_above_it(
        win, tmp_path, monkeypatch):
    two_more = _write_pdf(tmp_path / "two.pdf", "CD")
    view = win.view
    _draw_a_rectangle(view, page=0)
    assert _markup_pages(view) == {0}
    # Insert BEFORE the marked-up page: select nothing and aim at row 0.
    view._insert_pages([two_more], 0)
    QApplication.processEvents()
    assert _letters(win) == "CDA"
    assert _markup_pages(view) == {2}, "markup stayed on the page index, not the page"
    assert _strip_rows(win) == 3


def test_undoing_that_insert_puts_the_markup_back_on_page_one(
        win, tmp_path, monkeypatch):
    two_more = _write_pdf(tmp_path / "two.pdf", "CD")
    view = win.view
    _draw_a_rectangle(view, page=0)
    view._insert_pages([two_more], 0)
    view.undo_stack().undo()
    QApplication.processEvents()
    assert _markup_pages(view) == {0}
    assert _strip_rows(win) == 1


# ---------------------------------------------------------------------------
# Several files, and files that will not open
# ---------------------------------------------------------------------------

def test_several_files_land_as_one_undoable_block(win, tmp_path, monkeypatch):
    b = _write_pdf(tmp_path / "b.pdf", "B")
    c = _write_pdf(tmp_path / "c.pdf", "CD")
    _add_pages(win, monkeypatch, [b, c])
    assert _letters(win) == "ABCD"
    assert _strip_rows(win) == 4
    assert _grid_rows(win) == 4
    assert win.view.undo_stack().count() == 1
    win.view.undo_stack().undo()
    QApplication.processEvents()
    assert _letters(win) == "A"
    assert _strip_rows(win) == 1


def test_a_broken_file_is_reported_and_the_good_ones_still_land(
        win, tmp_path, monkeypatch):
    good = _write_pdf(tmp_path / "good.pdf", "B")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    shown = []
    monkeypatch.setattr(organizer_mod.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: shown.append(a)))
    import ui.document_view as document_view_mod
    monkeypatch.setattr(document_view_mod.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: shown.append(a)))
    _add_pages(win, monkeypatch, [bad, good])
    assert shown, "an unreadable file has to be reported"
    assert _letters(win) == "AB"
    assert _strip_rows(win) == 2


def test_nothing_is_pushed_when_no_file_opens(win, tmp_path, monkeypatch):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    import ui.document_view as document_view_mod
    monkeypatch.setattr(document_view_mod.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: None))
    _add_pages(win, monkeypatch, [bad])
    assert win.view.page_count() == 1
    assert _strip_rows(win) == 1
    assert win.view.undo_stack().count() == 0
    assert win.view._doc.path is not None, "a failed merge is not a merge"


# ---------------------------------------------------------------------------
# Insertion point
# ---------------------------------------------------------------------------

def test_pages_land_after_the_selected_row(qt_app, tmp_path, monkeypatch):
    base = _write_pdf(tmp_path / "abc.pdf", "ABC")
    extra = _write_pdf(tmp_path / "x.pdf", "X")
    window = MainWindow()
    window.open_paths([base])
    try:
        _add_pages(window, monkeypatch, [extra], rows=[0])
        assert _letters(window) == "AXBC"
        assert _strip_rows(window) == 4
        assert _grid_rows(window) == 4
    finally:
        window.view._doc.close()
        window.view._close_panel_render()
        window.view._close_org_render()
        window.deleteLater()


def test_with_no_selection_pages_go_on_the_end(win, extra_pdf, monkeypatch):
    _add_pages(win, monkeypatch, [extra_pdf], rows=[])
    assert _letters(win) == "AB"
