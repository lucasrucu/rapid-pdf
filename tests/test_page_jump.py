"""The status-bar page box: type a number, press Enter, land on that page.

Two halves. The widget on its own covers the input handling, including every way
a number can be wrong. The window half proves the wiring: that the box is in the
status bar, that Ctrl+G reaches it, that a jump actually moves the editor and the
Organizer, and that scrolling moves the number back without fighting the typing.
"""

import fitz
import pytest

from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QApplication, QMenu

from ui.main_window import MainWindow
from ui.page_jump import PageJump


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def box(qt_app):
    """A page box on page 1 of a 12 page document."""
    w = PageJump()
    w.set_total(12)
    w.set_current_page(0)
    w.show()
    QApplication.processEvents()
    # Shown on its own the line edit is the only focusable thing in the window and
    # takes focus. In the app it sits in a status bar with the canvas in front of
    # it, so start from the state the user actually sees: not focused.
    w._edit.clearFocus()
    yield w
    w.close()


@pytest.fixture
def jumps(box):
    seen = []
    box.page_requested.connect(seen.append)
    return seen


def _type(box, text):
    """Put `text` in the box and press Enter, the way a user gets there."""
    box._edit.setText(text)
    box._edit.returnPressed.emit()


# ---------------------------------------------------------------------------
# What it shows
# ---------------------------------------------------------------------------

def test_shows_the_current_page_one_based(box):
    box.set_current_page(6)
    assert box.current_text() == "7"


def test_shows_the_total_because_a_page_number_alone_says_nothing(box):
    assert box.total_text() == "of 12"


def test_hides_itself_when_no_document_is_open(qt_app):
    w = PageJump()
    assert w.isHidden()
    assert w.current_text() == ""


def test_comes_back_when_a_document_opens(box):
    box.set_total(0)
    assert box.current_text() == ""
    box.set_total(887)
    assert box.total_text() == "of 887"
    assert box.current_text() == "1"


# ---------------------------------------------------------------------------
# Jumping
# ---------------------------------------------------------------------------

def test_a_number_and_enter_asks_for_that_page(box, jumps):
    _type(box, "7")
    assert jumps == [6]     # zero-based, like everything else that moves pages


def test_the_first_page_is_reachable(box, jumps):
    box.set_current_page(5)
    _type(box, "1")
    assert jumps == [0]


def test_the_last_page_is_reachable(box, jumps):
    _type(box, "12")
    assert jumps == [11]


def test_the_box_keeps_the_page_it_landed_on(box, jumps):
    _type(box, "9")
    assert box.current_text() == "9"


# ---------------------------------------------------------------------------
# Every way the input can be wrong
# ---------------------------------------------------------------------------

def test_past_the_end_clamps_to_the_last_page(box, jumps):
    _type(box, "9999")
    assert jumps == [11]
    assert box.current_text() == "12"   # shows where it actually went


def test_zero_clamps_to_the_first_page(box, jumps):
    box.set_current_page(4)
    _type(box, "0")
    assert jumps == [0]
    assert box.current_text() == "1"


def test_empty_does_nothing_but_puts_the_current_page_back(box, jumps):
    box.set_current_page(3)
    _type(box, "")
    assert jumps == []
    assert box.current_text() == "4"


def test_letters_never_reach_the_box(box):
    """The validator rejects them at the keystroke, so "abc" cannot be typed."""
    state, _, _ = box._edit.validator().validate("abc", 3)
    assert state == QValidator.State.Invalid


def test_a_negative_number_never_reaches_the_box_either(box):
    state, _, _ = box._edit.validator().validate("-4", 2)
    assert state == QValidator.State.Invalid


def test_garbage_pasted_in_anyway_is_survived(box, jumps):
    """Belt and braces: setText bypasses the validator, and this must not raise."""
    _type(box, "not a page")
    assert jumps == []
    assert box.current_text() == "1"


def test_nothing_happens_with_no_document(qt_app):
    w = PageJump()
    seen = []
    w.page_requested.connect(seen.append)
    w._edit.setText("5")
    w._edit.returnPressed.emit()
    assert seen == []


# ---------------------------------------------------------------------------
# Staying in sync without fighting the typing
# ---------------------------------------------------------------------------

def test_scrolling_moves_the_number(box):
    box.set_current_page(0)
    assert box.current_text() == "1"
    box.set_current_page(11)
    assert box.current_text() == "12"


def test_scrolling_does_not_overwrite_what_is_being_typed(box):
    box._edit.setFocus()
    box._edit.setText("88")          # halfway to 887
    box.set_current_page(4)          # the view scrolled underneath
    assert box.current_text() == "88"


def test_walking_away_from_a_half_typed_number_puts_the_page_back(box):
    box._edit.setFocus()
    box._edit.setText("88")
    box.set_current_page(4)
    box._edit.editingFinished.emit()
    assert box.current_text() == "5"


def test_ctrl_g_focus_selects_the_number_so_typing_replaces_it(box):
    box.set_current_page(9)
    box.focus_box()
    assert box._edit.hasFocus()
    assert box._edit.selectedText() == "10"


# ---------------------------------------------------------------------------
# Wired into the window
# ---------------------------------------------------------------------------

@pytest.fixture
def pdf_path(tmp_path):
    """A twenty page PDF, numbered, so a jump is checkable against the text."""
    path = tmp_path / "twenty.pdf"
    raw = fitz.open()
    for i in range(20):
        page = raw.new_page(width=200, height=200)
        page.insert_text((20, 100), f"p{i}", fontsize=48)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def win(qt_app, pdf_path):
    window = MainWindow()
    window.open_paths([pdf_path])
    yield window
    window.view._doc.close()
    window.view._close_panel_render()
    window.view._close_org_render()
    window.deleteLater()


def test_the_box_is_in_the_status_bar(win):
    assert win._page_jump in win.statusBar().findChildren(PageJump)
    assert win._page_jump.total_text() == "of 20"
    assert win._page_jump.current_text() == "1"


def test_a_jump_moves_the_editor_and_the_strip(win):
    win._page_jump._edit.setText("14")
    win._page_jump._edit.returnPressed.emit()
    assert win.view._current_page == 13
    assert win.view._canvas._current_page == 13
    assert win.view._page_panel._list.currentRow() == 13


def test_a_jump_moves_the_organizer_too(win):
    win.view._tabs.setCurrentIndex(1)      # rebuilds the grid from the document
    QApplication.processEvents()
    win._page_jump._edit.setText("17")
    win._page_jump._edit.returnPressed.emit()
    assert win.view._organizer._list.currentRow() == 16


def test_the_box_follows_the_page_the_editor_moves_to(win):
    win.view._on_page_selected(8)
    assert win._page_jump.current_text() == "9"


def test_out_of_range_in_the_window_clamps_instead_of_throwing(win):
    win._page_jump._edit.setText("500")
    win._page_jump._edit.returnPressed.emit()
    assert win.view._current_page == 19
    assert win._page_jump.current_text() == "20"


def test_ctrl_g_is_on_the_page_menu(win):
    shortcuts = {a.text(): a.shortcut().toString()
                 for m in win.menuBar().findChildren(QMenu) for a in m.actions()}
    assert shortcuts.get("Go to Page…") == "Ctrl+G"


def test_closing_the_document_empties_the_box(win):
    win.close_pdf()
    assert win._page_jump.total_text() == ""
    assert win._page_jump.current_text() == ""
