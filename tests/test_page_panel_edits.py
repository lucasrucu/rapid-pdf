"""Page delete and reorder driven through the real window, with real undo.

test_page_ops.py proves the arithmetic and the PDF primitives. This proves the
wiring on top of them: that the panel's signals reach the undo stack, that one
Ctrl+Z puts a whole multi-page delete back, and that the strip and the document
still agree afterwards. Runs offscreen (see conftest), so it needs no display.
"""

import fitz
import pytest

from PySide6.QtWidgets import QApplication

from core.page_ops import move_rows
from ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def pdf_path(tmp_path):
    """A five page PDF, one letter per page, so order is readable."""
    path = tmp_path / "five.pdf"
    raw = fitz.open()
    for letter in "ABCDE":
        page = raw.new_page(width=200, height=200)
        page.insert_text((20, 100), letter, fontsize=48)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def win(qt_app, pdf_path):
    window = MainWindow()
    window.open_paths([pdf_path])
    yield window
    window._doc.close()
    window._close_panel_render()
    window._close_org_render()
    window.deleteLater()


def _letters(window) -> str:
    doc = window._doc
    return "".join(doc.doc[i].get_text().strip() for i in range(doc.page_count()))


def _rows(window) -> int:
    return window._page_panel._list.count()


def test_opens_with_the_strip_matching_the_document(win):
    assert _letters(win) == "ABCDE"
    assert _rows(win) == 5


def test_delete_one_page_and_undo(win):
    win._delete_pages([1])
    assert _letters(win) == "ACDE"
    assert _rows(win) == 4
    win._canvas.undo_stack.undo()
    assert _letters(win) == "ABCDE"
    assert _rows(win) == 5


def test_delete_a_multi_page_selection_in_one_action(win):
    win._delete_pages([0, 2, 4])
    assert _letters(win) == "BD"
    assert _rows(win) == 2


def test_one_undo_brings_a_whole_multi_page_delete_back(win):
    win._delete_pages([0, 2, 4])
    win._canvas.undo_stack.undo()
    assert _letters(win) == "ABCDE"
    assert _rows(win) == 5


def test_redo_reapplies_the_delete(win):
    win._delete_pages([0, 2, 4])
    win._canvas.undo_stack.undo()
    win._canvas.undo_stack.redo()
    assert _letters(win) == "BD"
    assert _rows(win) == 2


def test_deleting_every_page_is_refused(win, monkeypatch):
    import ui.main_window as mw
    warned = []
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        lambda *a, **k: warned.append(a))
    win._delete_pages([0, 1, 2, 3, 4])
    assert warned, "deleting the whole document should be refused, not done"
    assert _letters(win) == "ABCDE"


def test_reorder_a_single_page_and_undo(win):
    order = move_rows(5, [0], 5)
    win._reorder_pages(order, [0])
    assert _letters(win) == "BCDEA"
    win._canvas.undo_stack.undo()
    assert _letters(win) == "ABCDE"


def test_reorder_a_multi_page_selection_keeps_relative_order(win):
    order = move_rows(5, [1, 3], 0)
    win._reorder_pages(order, [1, 3])
    assert _letters(win) == "BDACE"
    assert _rows(win) == 5
    win._canvas.undo_stack.undo()
    assert _letters(win) == "ABCDE"


def test_moved_pages_stay_selected_after_the_drag(win):
    order = move_rows(5, [0, 1], 5)
    win._reorder_pages(order, [0, 1])
    assert _letters(win) == "CDEAB"
    assert win._page_panel.selected_rows() == [3, 4]


def test_a_bad_permutation_is_dropped_and_the_strip_rebuilt(win):
    win._reorder_pages([0, 1, 2], [0])          # too short for five pages
    assert _letters(win) == "ABCDE"
    assert _rows(win) == 5


def test_delete_then_reorder_undo_one_step_at_a_time(win):
    win._delete_pages([1])
    assert _letters(win) == "ACDE"
    win._reorder_pages(move_rows(4, [3], 0), [3])
    assert _letters(win) == "EACD"
    win._canvas.undo_stack.undo()
    assert _letters(win) == "ACDE"
    win._canvas.undo_stack.undo()
    assert _letters(win) == "ABCDE"


def test_the_editor_stays_on_a_real_page_after_a_delete(win):
    win._on_page_selected(4)
    win._delete_pages([4])
    assert win._current_page == win._doc.page_count() - 1
    assert 0 <= win._current_page < win._doc.page_count()


def test_page_edits_mark_the_document_unsaved(win):
    assert not win._dirty
    win._delete_pages([1])
    assert win._dirty


def test_undoing_back_to_the_opened_state_clears_the_modified_marker(win):
    win._delete_pages([1])
    win._canvas.undo_stack.undo()
    assert not win._dirty


def test_saving_writes_what_the_strip_shows(win, tmp_path):
    win._delete_pages([0])
    win._reorder_pages(move_rows(4, [0], 4), [0])
    shown = _letters(win)
    assert shown == "CDEB"
    out = tmp_path / "out.pdf"
    assert win._doc.save(str(out))
    reopened = fitz.open(str(out))
    try:
        written = "".join(reopened[i].get_text().strip()
                          for i in range(len(reopened)))
    finally:
        reopened.close()
    assert written == shown
