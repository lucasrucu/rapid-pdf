"""What the X, Ctrl+W, Ctrl+Q and a Windows shutdown each do to the window.

Rapid PDF used to treat the X as "close the PDF": with a document open the
close event was ignored and an empty window stayed up, and the only way to
actually quit was the File menu. That is now a setting (`close.x_closes`)
defaulting to closing the app, which is what every other Windows app does.

Three rules the matrix below pins down, in order of precedence:

1. **A session end is never blocked.** `event.ignore()` during a Windows
   shutdown is what produces "this app is preventing shutdown", so nothing in
   this file is allowed to reach it on that path, setting or no setting.
2. **Unsaved changes prompt first, and the answer wins.** Cancel aborts the
   close whichever branch it was heading for.
3. **Then the setting decides** whether the window goes or just the document.

`QCloseEvent` is real; the tests read `isAccepted()` off it rather than
mocking, so what is asserted is exactly what Qt would act on.
"""

import fitz
import pytest

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from core.settings import Settings, set_settings
from ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    """A throwaway settings store, in place for the duration of one test."""
    s = Settings(tmp_path / "settings.json", debounce_ms=0, migrate_legacy=False)
    previous = set_settings(s)
    yield s
    set_settings(previous)


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "three.pdf"
    raw = fitz.open()
    for i in range(3):
        page = raw.new_page(width=200, height=200)
        page.insert_text((20, 100), f"p{i}", fontsize=36)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def win(qt_app, store, pdf_path):
    """A window with a document open, on a throwaway settings store."""
    window = MainWindow()
    window.open_paths([pdf_path])
    yield window
    window.view._doc.close()
    window.view._close_panel_render()
    window.view._close_org_render()
    window.deleteLater()


@pytest.fixture
def empty_win(qt_app, store):
    window = MainWindow()
    yield window
    window.view._doc.close()
    window.view._close_panel_render()
    window.view._close_org_render()
    window.deleteLater()


def _close(window):
    """Send a real close event the way Qt does, and report whether it stuck."""
    event = QCloseEvent()
    event.accept()          # Qt's own starting state for a close event
    window.closeEvent(event)
    return event.isAccepted()


@pytest.fixture
def never_prompts(monkeypatch):
    """Fail loudly if a test reaches the unsaved-changes dialog unexpectedly."""
    def boom(*args, **kwargs):
        raise AssertionError("the unsaved-changes prompt should not have opened")

    monkeypatch.setattr(QMessageBox, "question", staticmethod(boom))


def _answer_with(monkeypatch, button):
    """Make the next unsaved-changes prompt answer itself, and count the asks."""
    asked = []

    def fake_question(*args, **kwargs):
        asked.append(args)
        return button

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    return asked


# ---------------------------------------------------------------------------
# The close matrix, clean document
# ---------------------------------------------------------------------------

def test_x_closes_the_app_by_default(win, never_prompts):
    """The new default, and the reason this phase exists."""
    assert win.view._doc.doc is not None
    assert _close(win) is True


def test_x_closes_only_the_document_when_the_setting_says_so(win, store,
                                                             never_prompts):
    """The old behaviour, still available: the PDF goes, the window stays."""
    store.close.x_closes = "document"
    assert _close(win) is False          # ignored: the window survives
    assert win.view._doc.doc is None
    assert win.view._current_page == 0


def test_x_quits_when_nothing_is_open_whatever_the_setting(empty_win, store,
                                                           never_prompts):
    """There is no document to close, so "document" has nothing to fall back
    to and the window has to go."""
    store.close.x_closes = "document"
    assert empty_win.view._doc.doc is None
    assert _close(empty_win) is True


def test_quit_overrides_the_document_setting(win, store, never_prompts):
    """File > Quit / Ctrl+Q means quit, not "close the PDF"."""
    store.close.x_closes = "document"
    win._force_quit = True
    assert _close(win) is True


def test_closing_the_document_twice_ends_up_quitting(win, store, never_prompts):
    """With x_closes=document the first X empties the window and the second,
    now with nothing open, closes it."""
    store.close.x_closes = "document"
    assert _close(win) is False
    assert _close(win) is True


# ---------------------------------------------------------------------------
# Unsaved changes override the setting, both ways
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("x_closes", ["window", "document"])
def test_cancel_at_the_prompt_aborts_the_close(win, store, monkeypatch, x_closes):
    store.close.x_closes = x_closes
    win.view._dirty = True
    asked = _answer_with(monkeypatch, QMessageBox.StandardButton.Cancel)

    assert _close(win) is False          # ignored
    assert asked, "the prompt never opened"
    assert win.view._doc.doc is not None      # and the document is still here


@pytest.mark.parametrize("x_closes", ["window", "document"])
def test_the_prompt_opens_before_either_branch_is_taken(win, store, monkeypatch,
                                                        x_closes):
    store.close.x_closes = x_closes
    win.view._dirty = True
    asked = _answer_with(monkeypatch, QMessageBox.StandardButton.Discard)

    accepted = _close(win)
    assert asked, "the prompt never opened"
    assert accepted is (x_closes == "window")


def test_cancelling_a_quit_leaves_the_window_ready_for_the_next_x(win, monkeypatch):
    """A cancelled Quit must clear _force_quit, or the next X would skip
    straight past the setting on a stale flag."""
    win.view._dirty = True
    win._force_quit = True
    _answer_with(monkeypatch, QMessageBox.StandardButton.Cancel)

    assert _close(win) is False
    assert win._force_quit is False


def test_a_clean_document_is_never_prompted_for(win, never_prompts):
    win.view._dirty = False
    assert _close(win) is True


# ---------------------------------------------------------------------------
# Ctrl+W and Ctrl+Q actually exist
# ---------------------------------------------------------------------------

def _file_menu_actions(window):
    for menu in window.menuBar().findChildren(QMenu):
        if menu.title() == "File":
            return {a.text(): a for a in menu.actions()}
    return {}


def test_close_pdf_is_on_ctrl_w(win):
    action = _file_menu_actions(win).get("Close PDF")
    assert action is not None
    assert action.shortcut().toString() == "Ctrl+W"


def test_quit_is_on_ctrl_q_not_the_exit_media_key(win):
    """QKeySequence.StandardKey.Quit resolves to the hardware Exit key on
    Windows, so the menu showed "Exit" and nothing on the keyboard reached it."""
    action = _file_menu_actions(win).get("Quit")
    assert action is not None
    assert action.shortcut().toString() == "Ctrl+Q"


def test_close_pdf_empties_the_window_without_closing_it(win, never_prompts):
    """What Ctrl+W is wired to. The document goes, the window and its
    Organizer grid are emptied, and nothing quits."""
    win.close_pdf()
    assert win.view._doc.doc is None
    assert win.view._current_page == 0
    assert win.view._organizer._list.count() == 0


# ---------------------------------------------------------------------------
# The shutdown bug
# ---------------------------------------------------------------------------

def test_a_session_end_is_never_ignored(win, store, never_prompts):
    """The bug: with a document open the close event was ignored no matter
    what, including during WM_QUERYENDSESSION, so Windows reported Rapid PDF
    as preventing shutdown."""
    store.close.x_closes = "document"
    win._session_ending = True

    assert _close(win) is True
    assert win.view._doc.doc is None


def test_a_session_end_does_not_stop_to_prompt(win, store, never_prompts):
    """A modal dialog during a session end blocks the shutdown just as surely
    as ignoring the event does."""
    store.close.x_closes = "document"
    win.view._dirty = True                    # would normally open the prompt
    win._session_ending = True

    assert _close(win) is True           # never_prompts would have fired


def test_commit_data_flags_the_session_and_gets_settings_onto_disk(win, store):
    store.appearance.theme = "dark"      # something worth persisting
    win._on_commit_data(None)

    assert win._session_ending is True
    assert store.path.exists()


def test_an_ordinary_close_is_not_treated_as_a_session_end(win, store,
                                                           never_prompts):
    store.close.x_closes = "document"
    assert win._session_is_ending() is False
    assert _close(win) is False          # still the document-only branch
