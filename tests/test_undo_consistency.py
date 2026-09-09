"""One Delete key, one outcome, whichever page panel had the focus.

THE BUG THESE START FROM. Delete in the left thumbnail strip pushed a
DeletePagesCommand and was fully undoable. Delete with the Organizer tab in
front called PDFDocument.delete_page directly and then cleared the window's
undo stack, so it was irreversible AND it took every annotation edit made
before it. Nothing on screen said which panel had the keyboard, so the same
keystroke sometimes cost an hour of markup. Organizer reorder did the same.

So these tests are mostly PAIRS: the same edit, driven through each panel, with
the same assertion on both sides. The strip's own behaviour is covered in
test_page_panel_edits.py; what is new here is that the grid now matches it, and
that an annotation edit made before a page delete is still undoable after one.

Everything is the real widget: the real organizer grid, the real window undo
stack, a real PDF on disk. Runs offscreen (see conftest).
"""

import fitz
import pytest

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QMessageBox

from core.page_ops import move_rows
from ui.canvas import AddItemsCommand, HighlightItem
from ui.main_window import MainWindow
from ui.organizer import _PAGE_ID
from ui.page_drag import make_page_mime
from ui.undo import WindowUndoStack

EDITOR_TAB = 0
ORGANIZER_TAB = 1


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def pdf_path(tmp_path):
    """A five page PDF, one letter per page, so page order is readable."""
    path = tmp_path / "five.pdf"
    raw = fitz.open()
    for letter in "ABCDE":
        page = raw.new_page(width=200, height=200)
        page.insert_text((20, 100), letter, fontsize=48)
    raw.save(str(path))
    raw.close()
    return str(path)


def _open_window(pdf_path):
    window = MainWindow()
    window.open_paths([pdf_path])
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
def second_win(qt_app, pdf_path):
    """A second window on the same file, for the two panels side by side."""
    window = _open_window(pdf_path)
    yield window
    window.view._doc.close()
    window.view._close_panel_render()
    window.view._close_org_render()
    window.deleteLater()


@pytest.fixture
def yes_to_delete(monkeypatch):
    """Answer the Organizer's confirmation with Yes, and count the asks."""
    asked = []

    def question(*args, **kwargs):
        asked.append(args)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _letters(window) -> str:
    doc = window.view._doc
    return "".join(doc.doc[i].get_text().strip() for i in range(doc.page_count()))


def _grid_rows(window) -> int:
    return window.view._organizer._list.count()


def _strip_rows(window) -> int:
    return window.view._page_panel._list.count()


def _stack(window) -> WindowUndoStack:
    return window.view._canvas.undo_stack


def _show_organizer(window):
    """Bring the Organizer tab forward, the way a user clicking it does."""
    window.view._tabs.setCurrentIndex(ORGANIZER_TAB)


def _select_grid_rows(window, rows):
    grid = window.view._organizer._list
    grid.clearSelection()
    for row in rows:
        grid.item(row).setSelected(True)


def _delete_through_the_organizer(window, rows):
    _show_organizer(window)
    _select_grid_rows(window, rows)
    window.view.delete_key()


def _highlight_page(window, page: int) -> HighlightItem:
    """One annotation edit, pushed the way the drawing tools push theirs.

    The item is attached live and the command is constructed after, which is
    the canvas's own convention (_Command skips the push's first redo).
    """
    canvas = window.view._canvas
    item = HighlightItem(QRectF(10, 10, 40, 20), QColor("yellow"), 0.4, page)
    canvas._attach_item(item)
    canvas.undo_stack.push(AddItemsCommand(canvas, [item], "Highlight"))
    return item


def _has_item(window, page: int, item) -> bool:
    return item in window.view._canvas._page_annotations.get(page, [])


# ---------------------------------------------------------------------------
# 1. The Organizer's delete is a command now
# ---------------------------------------------------------------------------

def test_the_organizer_delete_key_pushes_one_undoable_command(win, yes_to_delete):
    _delete_through_the_organizer(win, [1])
    assert _letters(win) == "ACDE"
    assert _stack(win).count() == 1
    assert _stack(win).undoText() == "Delete page"


def test_undoing_an_organizer_delete_brings_the_page_back_with_its_content(
        win, yes_to_delete):
    _delete_through_the_organizer(win, [1])
    _stack(win).undo()
    assert _letters(win) == "ABCDE", "the restored page is the one that was deleted"
    assert win.view._doc.page_count() == 5


def test_one_undo_brings_a_whole_multi_page_organizer_delete_back(win, yes_to_delete):
    _delete_through_the_organizer(win, [0, 2, 4])
    assert _letters(win) == "BD"
    assert _stack(win).count() == 1
    _stack(win).undo()
    assert _letters(win) == "ABCDE"


def test_redo_reapplies_an_organizer_delete(win, yes_to_delete):
    _delete_through_the_organizer(win, [0, 2, 4])
    _stack(win).undo()
    _stack(win).redo()
    assert _letters(win) == "BD"


def test_both_panels_are_rebuilt_after_an_organizer_delete(win, yes_to_delete):
    """The grid used to take its own cells out and render the rest from a clone
    that still had the deleted pages in it."""
    _delete_through_the_organizer(win, [1])
    assert _grid_rows(win) == 4
    assert _strip_rows(win) == 4
    _stack(win).undo()
    assert _grid_rows(win) == 5
    assert _strip_rows(win) == 5


def test_declining_the_organizer_confirmation_deletes_nothing(win, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No)
    _delete_through_the_organizer(win, [1])
    assert _letters(win) == "ABCDE"
    assert _stack(win).count() == 0


def test_deleting_every_page_from_the_organizer_is_refused(win, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: warned.append(a))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    _delete_through_the_organizer(win, [0, 1, 2, 3, 4])
    assert warned, "a document has to keep a page, in either panel"
    assert _letters(win) == "ABCDE"
    assert _stack(win).count() == 0


# ---------------------------------------------------------------------------
# 2. The annotation history the old path threw away
# ---------------------------------------------------------------------------

def test_an_annotation_edit_is_still_undoable_after_an_organizer_delete(
        win, yes_to_delete):
    """THE BUG, in one test. The markup was made first, and deleting a page in
    the Organizer used to clear the stack it was sitting on."""
    item = _highlight_page(win, 0)
    assert _stack(win).count() == 1

    _delete_through_the_organizer(win, [3])
    assert _letters(win) == "ABCE"
    assert _stack(win).count() == 2, "the highlight's command is still on the stack"

    _stack(win).undo()                      # take back the page delete
    assert _letters(win) == "ABCDE"
    assert _has_item(win, 0, item)

    _stack(win).undo()                      # and then the highlight
    assert not _has_item(win, 0, item)
    assert _stack(win).index() == 0


def test_an_annotation_edit_survives_an_organizer_reorder(win):
    item = _highlight_page(win, 0)
    _show_organizer(win)
    win.view._organizer._list.reordered_rows.emit(move_rows(5, [0], 5), [0])
    assert _letters(win) == "BCDEA"
    assert _stack(win).count() == 2

    _stack(win).undo()
    assert _letters(win) == "ABCDE"
    assert _has_item(win, 0, item)
    _stack(win).undo()
    assert not _has_item(win, 0, item)


def test_markup_follows_its_page_through_an_organizer_delete(win, yes_to_delete):
    """Deleting page 0 moves page 3's markup to page 2, and undo puts it back."""
    item = _highlight_page(win, 3)
    _delete_through_the_organizer(win, [0])
    assert _has_item(win, 2, item)
    _stack(win).undo()
    assert _has_item(win, 3, item)


def test_markup_on_a_deleted_page_comes_back_with_it(win, yes_to_delete):
    item = _highlight_page(win, 2)
    _delete_through_the_organizer(win, [2])
    assert not _has_item(win, 2, item)
    _stack(win).undo()
    assert _has_item(win, 2, item), "the deleted page's own markup is restored too"


# ---------------------------------------------------------------------------
# 3. Reorder, the same story
# ---------------------------------------------------------------------------

def _reorder_through_the_organizer(window, rows, target):
    """A drop in the grid, from the point the widget reports it."""
    _show_organizer(window)
    count = window.view._doc.page_count()
    order = move_rows(count, rows, target)
    window.view._organizer._list.reordered_rows.emit(order, sorted(rows))
    return order


def test_an_organizer_reorder_is_undoable(win):
    _reorder_through_the_organizer(win, [0], 5)
    assert _letters(win) == "BCDEA"
    assert _stack(win).count() == 1
    assert _stack(win).undoText() == "Move page"
    _stack(win).undo()
    assert _letters(win) == "ABCDE"


def test_an_organizer_reorder_of_a_multi_page_selection_undoes_in_one_step(win):
    _reorder_through_the_organizer(win, [1, 3], 0)
    assert _letters(win) == "BDACE"
    assert _stack(win).count() == 1
    _stack(win).undo()
    assert _letters(win) == "ABCDE"


def test_the_grid_is_rebuilt_from_the_document_after_a_reorder(win):
    _reorder_through_the_organizer(win, [0], 5)
    assert _grid_rows(win) == 5
    assert _strip_rows(win) == 5
    _stack(win).undo()
    assert _letters(win) == "ABCDE"
    assert _grid_rows(win) == 5


def test_the_moved_pages_stay_selected_in_the_grid(win):
    """The strip does this already. The grid is rebuilt from the document now,
    so its selection has to be put back rather than surviving on its own."""
    _reorder_through_the_organizer(win, [0, 1], 5)
    assert _letters(win) == "CDEAB"
    grid = win.view._organizer._list
    assert sorted(grid.row(i) for i in grid.selectedItems()) == [3, 4]


def test_a_bad_permutation_from_the_grid_changes_nothing(win):
    _show_organizer(win)
    win.view._organizer._list.reordered_rows.emit([0, 1, 2], [0])  # too short
    assert _letters(win) == "ABCDE"
    assert _stack(win).count() == 0
    assert _grid_rows(win) == 5


def test_a_second_drag_after_a_reorder_still_reads_the_right_order(win):
    """The grid used to re-tag its own cells after applying its own edit. It is
    rebuilt from the document now, and the ids have to come back right or the
    next drop reports a permutation of the wrong thing."""
    _reorder_through_the_organizer(win, [0], 5)
    grid = win.view._organizer._list
    assert [grid.item(i).data(_PAGE_ID) for i in range(grid.count())] == list(range(5))
    _reorder_through_the_organizer(win, [4], 0)
    assert _letters(win) == "ABCDE"


class _FakeDrop:
    """What the grid's dropEvent reads off a real QDropEvent, and nothing else.

    Same stand-in as test_pages_between_tabs.py uses. Qt's drag loop cannot run
    in a test, but everything after the drop lands is the real widget.
    """

    def __init__(self, source, pos, mime):
        self._source = source
        self._pos = pos
        self._mime = mime
        self.accepted = False
        self.ignored = False

    def source(self):
        return self._source

    def pos(self):
        return self._pos

    def mimeData(self):
        return self._mime

    def modifiers(self):
        return Qt.KeyboardModifier.NoModifier

    def acceptProposedAction(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


def test_a_real_drop_in_the_grid_goes_through_the_undo_stack(win):
    """The whole path, from the widget's own dropEvent.

    Worth doing properly for one case: the host now CLEARS AND REBUILDS the
    grid from inside the drop handler, where the old code only moved cells
    about, so the rebuild has to survive being run there.
    """
    _show_organizer(win)
    view = win.view
    grid = view._organizer._list
    grid.resize(800, 600)
    grid.clearSelection()
    grid.item(0).setSelected(True)

    last = grid.visualItemRect(grid.item(4))
    event = _FakeDrop(grid, QPoint(last.right() - 1, last.center().y()),
                      make_page_mime(view, [0]))
    grid.dropEvent(event)

    assert event.accepted
    assert _letters(win) == "BCDEA"
    assert _stack(win).count() == 1
    assert _grid_rows(win) == 5
    _stack(win).undo()
    assert _letters(win) == "ABCDE"
    assert _grid_rows(win) == 5


# ---------------------------------------------------------------------------
# 4. The two panels, side by side
# ---------------------------------------------------------------------------

def test_the_strip_and_the_organizer_leave_the_same_undo_state(
        win, second_win, yes_to_delete):
    """The actual requirement: same operation, same outcome, either panel."""
    win.view._delete_pages([1])                       # the strip's path
    _delete_through_the_organizer(second_win, [1])    # the grid's path

    assert _letters(win) == _letters(second_win) == "ACDE"
    assert _stack(win).count() == _stack(second_win).count() == 1
    assert _stack(win).undoText() == _stack(second_win).undoText()
    assert win.view._dirty == second_win.view._dirty is True

    _stack(win).undo()
    _stack(second_win).undo()
    assert _letters(win) == _letters(second_win) == "ABCDE"
    assert win.view._dirty == second_win.view._dirty is False


def test_neither_panel_forces_a_document_dirty_that_undo_can_clean(
        win, second_win, yes_to_delete):
    """_forced_dirty is the flag a save clears and an undo cannot. A page delete
    used to set it in the Organizer, so undoing back to the opened state left
    the document modified with nothing to point at."""
    win.view._delete_pages([0])
    _delete_through_the_organizer(second_win, [0])
    assert not win.view._forced_dirty
    assert not second_win.view._forced_dirty


def test_reorder_leaves_the_same_undo_state_in_both_panels(win, second_win):
    order = move_rows(5, [0], 5)
    win.view._reorder_pages(order, [0])
    _reorder_through_the_organizer(second_win, [0], 5)

    assert _letters(win) == _letters(second_win) == "BCDEA"
    assert _stack(win).count() == _stack(second_win).count() == 1
    assert _stack(win).undoText() == _stack(second_win).undoText()

    _stack(win).undo()
    _stack(second_win).undo()
    assert _letters(win) == _letters(second_win) == "ABCDE"


def test_the_delete_key_still_routes_by_which_panel_is_in_front(win, yes_to_delete,
                                                               monkeypatch):
    """Routing is unchanged. What changed is that one of the destinations, the
    Organizer, is undoable now.

    The strip's own branch cannot be reached here: the window is never shown
    offscreen, so the panel reports itself invisible and Delete falls through to
    the canvas. That fall-through is the third branch, and it is asserted below.
    """
    _show_organizer(win)
    _select_grid_rows(win, [0])
    win.view.delete_key()
    assert _letters(win) == "BCDE"
    assert _stack(win).count() == 1

    win.view._tabs.setCurrentIndex(EDITOR_TAB)
    reached = []
    monkeypatch.setattr(win.view._canvas, "delete_selected",
                        lambda: reached.append("canvas"))
    win.view.delete_key()
    assert reached == ["canvas"], "with no page panel focused, Delete is the canvas's"
    assert _letters(win) == "BCDE", "and it takes no pages with it"


# ---------------------------------------------------------------------------
# 5. The menu's "Delete Current Page", the third way in
# ---------------------------------------------------------------------------

def test_delete_current_page_is_undoable(win, yes_to_delete):
    win.view._on_page_selected(2)
    win.view.delete_current_page()
    assert _letters(win) == "ABDE"
    assert _stack(win).count() == 1
    _stack(win).undo()
    assert _letters(win) == "ABCDE"


def test_delete_current_page_keeps_an_earlier_annotation_edit(win, yes_to_delete):
    item = _highlight_page(win, 0)
    win.view._on_page_selected(3)
    win.view.delete_current_page()
    assert _stack(win).count() == 2
    _stack(win).undo()
    _stack(win).undo()
    assert not _has_item(win, 0, item)


def test_delete_current_page_still_refuses_the_last_page(qt_app, tmp_path,
                                                         monkeypatch):
    one = tmp_path / "one.pdf"
    raw = fitz.open()
    raw.new_page(width=200, height=200)
    raw.save(str(one))
    raw.close()
    window = _open_window(str(one))
    warned = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: warned.append(a))
    try:
        window.view.delete_current_page()
        assert warned
        assert window.view._doc.page_count() == 1
    finally:
        window.view._doc.close()
        window.view._close_panel_render()
        window.view._close_org_render()
        window.deleteLater()


# ---------------------------------------------------------------------------
# 6. ui/undo.py's bookkeeping, which had no tests of its own
# ---------------------------------------------------------------------------

class _Bare:
    """A stand-in for a DocumentView, as far as the stack is concerned."""

    def __init__(self):
        self.dropped = 0

    def note_branch_dropped(self):
        self.dropped += 1


class _NoisyCommand:
    """Not a QUndoCommand: _affected only ever calls affected_views()."""

    def affected_views(self):
        raise RuntimeError("this command cannot say what it touches")


def test_a_command_that_cannot_say_what_it_touches_is_reported_not_swallowed(capsys):
    """ui/undo.py used to catch this with no output at all, so a command that
    never dirtied its document looked like a document that was never edited."""
    from ui.undo import _affected

    assert _affected(_NoisyCommand()) == ()
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "cannot say what it touches" in err


def test_a_command_with_no_affected_views_is_still_silent():
    from ui.undo import _affected

    assert _affected(object()) == ()


def test_the_stack_tracks_the_views_a_command_names(qt_app):
    from PySide6.QtGui import QUndoCommand

    class Cmd(QUndoCommand):
        def __init__(self, views):
            super().__init__("edit")
            self._views = views

        def affected_views(self):
            return tuple(self._views)

    stack = WindowUndoStack()
    view = _Bare()
    stack.push(Cmd([view]))
    assert stack.touches(view)
    assert stack.drop_history_for(view) is True
    assert stack.count() == 0
    assert stack.drop_history_for(view) is False


def test_a_dropped_redo_branch_retires_the_save_marker(qt_app):
    from PySide6.QtGui import QUndoCommand

    class Cmd(QUndoCommand):
        def __init__(self, view):
            super().__init__("edit")
            self._view = view

        def affected_views(self):
            return (self._view,)

    stack = WindowUndoStack()
    view = _Bare()
    stack.push(Cmd(view))
    stack.undo()
    stack.push(Cmd(view))          # the redo branch above is thrown away here
    assert view.dropped == 1
