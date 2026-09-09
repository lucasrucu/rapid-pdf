"""The find bar's own behaviour, which nothing was checking.

SearchBar is deliberately dumb: it owns a line edit, three buttons and a
counter, and it says what happened. MainWindow runs the actual search. So the
contract worth guarding is exactly the signals and the two keys it handles by
hand, because those are what a caller wires to and what a refactor of the widget
would quietly drop.

Esc and Shift+Enter are handled in keyPressEvent rather than by a shortcut, so
they only work if the widget is the one holding focus. That is the reason
open_and_focus exists, and the reason it is tested.
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from ui.search_bar import SearchBar


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def bar(qt_app):
    b = SearchBar()
    yield b
    b.deleteLater()


def _key(key, mods=Qt.KeyboardModifier.NoModifier):
    return QKeyEvent(QKeyEvent.Type.KeyPress, key, mods)


def test_it_starts_hidden(bar):
    """Ctrl+F opens it; it is not part of the window's resting layout."""
    assert bar.isHidden()


def test_opening_shows_it_and_selects_what_is_already_typed(bar):
    bar._edit.setText("PU-001")
    bar.open_and_focus()
    assert not bar.isHidden()
    assert bar._edit.selectedText() == "PU-001"


def test_typing_announces_the_new_term(bar):
    seen = []
    bar.search_changed.connect(seen.append)
    bar._edit.setText("4100")
    assert seen == ["4100"]
    assert bar.term() == "4100"


def test_enter_asks_for_the_next_match(bar):
    seen = []
    bar.next_requested.connect(lambda: seen.append("next"))
    bar._edit.setText("valve")
    bar._edit.returnPressed.emit()
    assert seen == ["next"]


def test_shift_enter_asks_for_the_previous_match(bar):
    seen = []
    bar.prev_requested.connect(lambda: seen.append("prev"))
    bar.keyPressEvent(_key(Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier))
    assert seen == ["prev"]


def test_plain_enter_on_the_bar_itself_is_not_a_previous(bar):
    """Only the SHIFTED return goes backwards. The unshifted one belongs to the
    line edit's returnPressed, and taking it here would fire next twice."""
    seen = []
    bar.prev_requested.connect(lambda: seen.append("prev"))
    bar.keyPressEvent(_key(Qt.Key.Key_Return))
    assert seen == []


def test_escape_hides_it_and_says_so(bar):
    seen = []
    bar.closed.connect(lambda: seen.append("closed"))
    bar.open_and_focus()
    bar.keyPressEvent(_key(Qt.Key.Key_Escape))
    assert bar.isHidden()
    assert seen == ["closed"]


def test_the_close_button_does_the_same_thing_as_escape(bar):
    seen = []
    bar.closed.connect(lambda: seen.append("closed"))
    bar.open_and_focus()
    bar._on_close()
    assert bar.isHidden()
    assert seen == ["closed"]


def test_the_counter_shows_whatever_the_window_computed(bar):
    bar.set_count_text("3 of 12")
    assert bar._count.text() == "3 of 12"
