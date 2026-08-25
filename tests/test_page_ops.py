"""Page selection, delete and reorder, at the model level.

The widget side of the page panel is Qt plumbing that a human can see is right.
What a human cannot see is whether a multi-page drag landed the pages in the
order they were shown in, or whether an undo put back the same document. That is
what these cover: the pure order arithmetic in core.page_ops, and the PDFDocument
primitives that make a delete reversible.

Documents are built in memory with PyMuPDF, one recognisable letter per page, so
"what order are the pages in now" is a string comparison rather than a guess.
"""

import fitz
import pytest

from core.page_ops import (
    invert_order, is_permutation, move_rows, page_after_delete,
    shift_map_after_delete, shift_map_after_reorder,
)
from core.pdf_document import PDFDocument


# ---------------------------------------------------------------------------
# move_rows: where a dragged selection lands. Pure arithmetic, no Qt, no PDF.
# ---------------------------------------------------------------------------

def test_single_page_moves_down():
    # Drag page 0 to sit below page 2 (insertion point 3).
    assert move_rows(5, [0], 3) == [1, 2, 0, 3, 4]


def test_single_page_moves_up():
    assert move_rows(5, [3], 1) == [0, 3, 1, 2, 4]


def test_drop_onto_own_position_changes_nothing():
    assert move_rows(5, [2], 2) == [0, 1, 2, 3, 4]
    assert move_rows(5, [2], 3) == [0, 1, 2, 3, 4]


def test_contiguous_block_keeps_its_order():
    assert move_rows(6, [1, 2, 3], 6) == [0, 4, 5, 1, 2, 3]


def test_non_contiguous_selection_lands_as_one_block_in_page_order():
    # Pages 0, 2 and 4 dropped at the very end: they merge into one run and
    # keep their relative order, which is the whole point of a multi-drag.
    assert move_rows(5, [0, 2, 4], 5) == [1, 3, 0, 2, 4]


def test_selection_can_move_to_the_top():
    assert move_rows(4, [2, 3], 0) == [2, 3, 0, 1]


def test_target_is_pulled_back_past_rows_taken_out_above_it():
    # Rows 0 and 1 come out first, so an insertion point of 3 in the list AS
    # SHOWN is really position 1 in what is left.
    assert move_rows(5, [0, 1], 3) == [2, 0, 1, 3, 4]


def test_moving_everything_is_a_no_op():
    assert move_rows(3, [0, 1, 2], 0) == [0, 1, 2]


def test_out_of_range_and_duplicate_rows_are_ignored():
    assert move_rows(3, [1, 1, 9, -1], 0) == [1, 0, 2]


def test_empty_selection_leaves_the_order_alone():
    assert move_rows(3, [], 2) == [0, 1, 2]


def test_result_is_always_a_permutation():
    for target in range(7):
        order = move_rows(6, [1, 4], target)
        assert is_permutation(order, 6), (target, order)


# ---------------------------------------------------------------------------
# invert_order: the undo of a reorder.
# ---------------------------------------------------------------------------

def test_inverse_round_trips():
    order = move_rows(6, [0, 3], 6)
    inverse = invert_order(order)
    # Applying order then inverse gets every page back where it started.
    assert [order[i] for i in inverse] == list(range(6))


def test_inverse_of_identity_is_identity():
    assert invert_order([0, 1, 2]) == [0, 1, 2]


# ---------------------------------------------------------------------------
# Map re-keying: the canvas files markup by page index, so a structural edit
# has to move it with the pages.
# ---------------------------------------------------------------------------

def test_markup_map_follows_a_delete():
    page_map = {0: "a", 1: "b", 2: "c", 3: "d"}
    assert shift_map_after_delete(page_map, [1]) == {0: "a", 1: "c", 2: "d"}


def test_markup_map_drops_deleted_pages_and_closes_the_gap():
    page_map = {0: "a", 2: "c", 4: "e"}
    assert shift_map_after_delete(page_map, [0, 1]) == {0: "c", 2: "e"}


def test_markup_map_follows_a_reorder():
    page_map = {0: "a", 2: "c"}
    assert shift_map_after_reorder(page_map, [2, 0, 1]) == {0: "c", 1: "a"}


def test_page_after_delete_slides_up():
    assert page_after_delete(4, [1, 2]) == 2
    assert page_after_delete(0, [3]) == 0
    # A deleted page reports the index its successor slides into.
    assert page_after_delete(2, [2]) == 2


# ---------------------------------------------------------------------------
# PDFDocument: delete a selection, then put it back.
# ---------------------------------------------------------------------------

def _make_doc(letters: str) -> PDFDocument:
    """An in-memory PDF with one page per letter, that letter written on it."""
    raw = fitz.open()
    for letter in letters:
        page = raw.new_page(width=200, height=200)
        page.insert_text((20, 100), letter, fontsize=48)
    doc = PDFDocument()
    doc.adopt(raw)
    return doc


def _letters(doc: PDFDocument) -> str:
    return "".join(doc.doc[i].get_text().strip() for i in range(doc.page_count()))


@pytest.fixture
def doc():
    d = _make_doc("ABCDE")
    yield d
    d.close()


def test_the_fixture_reads_back_in_order(doc):
    assert _letters(doc) == "ABCDE"


def test_delete_pages_removes_a_whole_selection_at_once(doc):
    assert doc.delete_pages([3, 1]) == [1, 3]
    assert _letters(doc) == "ACE"


def test_delete_pages_ignores_duplicates_and_out_of_range(doc):
    assert doc.delete_pages([1, 1, 99, -4]) == [1]
    assert _letters(doc) == "ACDE"


def test_delete_of_a_contiguous_run(doc):
    doc.delete_pages([1, 2, 3])
    assert _letters(doc) == "AE"


def test_undo_of_a_multi_page_delete_restores_order_and_content(doc):
    rows = [0, 2, 4]
    stash = doc.extract_pages(rows)
    doc.delete_pages(rows)
    assert _letters(doc) == "BD"
    doc.restore_pages(stash, rows)
    stash.close()
    assert _letters(doc) == "ABCDE"


def test_undo_of_a_contiguous_delete_restores_order(doc):
    rows = [1, 2]
    stash = doc.extract_pages(rows)
    doc.delete_pages(rows)
    assert _letters(doc) == "ADE"
    doc.restore_pages(stash, rows)
    stash.close()
    assert _letters(doc) == "ABCDE"


def test_undo_of_deleting_the_last_page(doc):
    rows = [4]
    stash = doc.extract_pages(rows)
    doc.delete_pages(rows)
    assert _letters(doc) == "ABCD"
    doc.restore_pages(stash, rows)
    stash.close()
    assert _letters(doc) == "ABCDE"


def test_extract_pages_does_not_touch_the_document(doc):
    stash = doc.extract_pages([0, 1])
    assert len(stash) == 2
    stash.close()
    assert _letters(doc) == "ABCDE"


# ---------------------------------------------------------------------------
# PDFDocument.reorder driven by a real drag's arithmetic, and undone.
# ---------------------------------------------------------------------------

def test_dragging_one_page_to_the_end_and_undoing_it(doc):
    order = move_rows(5, [0], 5)
    doc.reorder(order)
    assert _letters(doc) == "BCDEA"
    doc.reorder(invert_order(order))
    assert _letters(doc) == "ABCDE"


def test_dragging_a_multi_page_selection_keeps_relative_order(doc):
    # Pages A and C dragged to the top: they arrive together, A still above C.
    order = move_rows(5, [0, 2], 0)
    doc.reorder(order)
    assert _letters(doc) == "ACBDE"


def test_dragging_a_non_contiguous_selection_and_undoing_it(doc):
    order = move_rows(5, [1, 3], 5)
    doc.reorder(order)
    assert _letters(doc) == "ACEBD"
    doc.reorder(invert_order(order))
    assert _letters(doc) == "ABCDE"


def test_reorder_refuses_anything_that_is_not_a_permutation(doc):
    doc.reorder([0, 1, 2])          # too short for a 5 page document
    assert _letters(doc) == "ABCDE"


def test_delete_then_reorder_then_undo_both(doc):
    rows = [1]
    stash = doc.extract_pages(rows)
    doc.delete_pages(rows)          # ACDE
    order = move_rows(4, [3], 0)
    doc.reorder(order)              # EACD
    assert _letters(doc) == "EACD"
    doc.reorder(invert_order(order))
    assert _letters(doc) == "ACDE"
    doc.restore_pages(stash, rows)
    stash.close()
    assert _letters(doc) == "ABCDE"


def test_saved_file_matches_the_edited_order(tmp_path, doc):
    """The pane shows the model; a save has to write the model, not the widget."""
    doc.delete_pages([0])
    doc.reorder(move_rows(4, [0], 4))
    assert _letters(doc) == "CDEB"
    out = tmp_path / "edited.pdf"
    assert doc.save(str(out))
    reopened = PDFDocument()
    assert reopened.open(str(out))
    assert _letters(reopened) == "CDEB"
    reopened.close()
