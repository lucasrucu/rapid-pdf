"""Reopening a closed tab: Ctrl+Shift+T, the stack behind it, and its edges.

The feature is two halves and the tests are grouped that way.

RECORDING is the half with the traps in it, because "a tab went away" is not
the same question as "the user closed a document". `MainWindow.close_tab` is
the single chokepoint every closing route arrives at, so the entry is written
there, before `request_close` has a chance to clear the path off the view, and
kept only once that call has said the close actually happened. The two cases
that must NOT leave an entry behind are both here: a cancelled save prompt, and
a tab moved into another window, which is a document changing address rather
than closing.

REOPENING is the half with the ordering in it. Newest first, walking back
through several closures, and skipping anything whose file has gone since,
silently, because the user pressed a key expecting a tab and not a complaint.

WHAT THESE TESTS CANNOT COVER, and it is worth being explicit rather than
leaving a gap that looks like an oversight:

  - THE REAL KEY PRESS. Offscreen has no window procedure, so nothing delivers
    a WM_KEYDOWN and Qt's shortcut machinery has nothing to resolve. Every test
    here calls `reopen_closed_tab` directly, which is what the rest of this
    suite does with every other shortcut. What is therefore NOT proved is that
    "Ctrl+Shift+T" is the string Qt ends up matching, only that the action
    exists in the File menu carrying it.
  - WINDOW ACTIVATION AND Z ORDER. `raise_and_focus`, "the window the shortcut
    fired in", and anything about which window is in front are all no-ops
    offscreen. The multi-window test below asserts on where a view LIVES, never
    on which window is active.
"""

import os

import fitz
import pytest

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from core.settings import Settings, set_settings
from ui.document_area import DocumentArea
from ui.main_window import MainWindow
from ui.reopen_stack import MAX_ENTRIES, ReopenStack, set_reopen_stack


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    s = Settings(tmp_path / "settings.json", debounce_ms=0, migrate_legacy=False)
    previous = set_settings(s)
    yield s
    set_settings(previous)


@pytest.fixture(autouse=True)
def never_opens_a_dialog(monkeypatch):
    """Offscreen still runs a real modal loop, so an unexpected message box
    hangs the suite instead of failing it. Each test that WANTS a prompt puts
    its own answer in with `_answer_with`."""
    for name in ("question", "warning", "critical", "information", "about"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, n=name, **k: pytest.fail(
                f"QMessageBox.{n} opened: {a[1:3]}")))


@pytest.fixture(autouse=True)
def stack():
    """A fresh reopen stack per test, and the old one put back afterwards.

    The stack is deliberately application wide and therefore process wide, so
    without this every test would inherit whatever the one before it closed.
    Same door `ui.session.set_recorder` gives the session recorder.
    """
    own = ReopenStack()
    previous = set_reopen_stack(own)
    yield own
    set_reopen_stack(previous)


def _answer_with(monkeypatch, button):
    """Answer the next prompts with one button, and record the text of each."""
    asked = []

    def fake_question(*args, **kwargs):
        asked.append(args[2] if len(args) > 2 else "")
        return button

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    return asked


def _pdf(tmp_path, name, pages=1, width=400, height=500):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = fitz.open()
    for i in range(pages):
        page = raw.new_page(width=width, height=height)
        page.insert_text((20, 100), f"{name} p{i}", fontsize=24)
    raw.save(str(path))
    raw.close()
    return str(path)


def _build():
    window = MainWindow()
    window.view._canvas.resize(600, 700)
    window.view._canvas._flush_pending_render()
    return window


def _dispose(window):
    """Put a window away without leaving a trap for the next test.

    deleteLater needs an event loop nobody runs offscreen, so a view otherwise
    outlives the test with a render source and a queued render behind it, and
    the next processEvents would pump it against a closed document.
    """
    for view in window.document_area().views():
        view.clear_document()
        view.teardown()
    window._force_quit = True
    window.close()
    window.deleteLater()


@pytest.fixture
def win(qt_app, store):
    window = _build()
    yield window
    _dispose(window)


@pytest.fixture
def area(win) -> DocumentArea:
    return win.document_area()


def _paths(area) -> list:
    return [view.document_path() for view in area.views()]


def _close_event(window) -> bool:
    """Put the window through its own close handler. True when it accepted."""
    event = QCloseEvent()
    window.closeEvent(event)
    return event.isAccepted()


# ---------------------------------------------------------------------------
# The menu entry
# ---------------------------------------------------------------------------

def test_the_file_menu_carries_the_action_on_the_browser_key(win):
    """The literal string, not a StandardKey, for the reason Quit is spelled
    out: a platform-resolved key binds to something nobody can press."""
    for menu in win.menuBar().findChildren(QMenu):
        if menu.title() != "File":
            continue
        labels = [a.text() for a in menu.actions()]
        assert "Reopen Closed Tab" in labels
        # Directly under Close PDF, which is where a browser puts it.
        assert labels.index("Reopen Closed Tab") == labels.index("Close PDF") + 1
        action = menu.actions()[labels.index("Reopen Closed Tab")]
        assert action.shortcut().toString() == "Ctrl+Shift+T"
        return
    raise AssertionError("no File menu")


# ---------------------------------------------------------------------------
# Coming back
# ---------------------------------------------------------------------------

def test_a_closed_tab_comes_back_with_its_path(win, area, tmp_path):
    alpha = _pdf(tmp_path, "alpha.pdf")
    beta = _pdf(tmp_path, "beta.pdf")
    win.open_paths([alpha, beta])
    assert win.close_tab(area.index_of_path(beta)) is True
    assert _paths(area) == [alpha]

    assert win.reopen_closed_tab() is True

    assert _paths(area) == [alpha, beta]
    assert area.current_view().document_path() == beta
    area.check_invariant()


def test_the_page_it_was_left_on_comes_back_with_it(win, area, tmp_path):
    """The page index IS the position: nothing in this codebase can read a
    scroll offset, so page plus fit mode is the whole vocabulary there is."""
    long_doc = _pdf(tmp_path, "long.pdf", pages=6)
    win.open_paths([long_doc])
    win.view.jump_to_page(4)
    assert win.view.current_page() == 4

    win.close_tab(area.index_of_path(long_doc))
    assert win.reopen_closed_tab() is True

    view = area.current_view()
    assert view.document_path() == long_doc
    assert view.current_page() == 4


def test_the_entry_carries_the_zoom_and_the_scale_it_was_measured_in(
        win, area, tmp_path, stack):
    """A view scale means nothing without the raster scale it was taken
    against, so the pair is recorded together or not at all."""
    one = _pdf(tmp_path, "one.pdf")
    win.open_paths([one])
    win.close_tab(0)

    entry = stack.peek()
    assert entry["path"] == one
    assert set(entry) == {"path", "page", "zoom", "raster_scale", "fit_mode"}
    assert entry["raster_scale"] > 0


def test_repeated_reopens_walk_back_through_the_closures(win, area, tmp_path):
    """Newest first, which is the whole reason it is a stack."""
    alpha = _pdf(tmp_path, "alpha.pdf")
    beta = _pdf(tmp_path, "beta.pdf")
    gamma = _pdf(tmp_path, "gamma.pdf")
    win.open_paths([alpha, beta, gamma])

    win.close_tab(area.index_of_path(gamma))
    win.close_tab(area.index_of_path(beta))
    assert _paths(area) == [alpha]

    assert win.reopen_closed_tab() is True
    assert area.current_view().document_path() == beta

    assert win.reopen_closed_tab() is True
    assert area.current_view().document_path() == gamma

    assert sorted(_paths(area)) == sorted([alpha, beta, gamma])
    area.check_invariant()


def test_reopening_with_nothing_closed_does_nothing(win, area, tmp_path):
    win.open_paths([_pdf(tmp_path, "alpha.pdf")])
    before = area.count()

    assert win.reopen_closed_tab() is False

    assert area.count() == before
    area.check_invariant()


def test_the_stack_runs_out_rather_than_repeating_itself(win, area, tmp_path):
    """One closure is one reopen. A second press must not open a second copy of
    the tab that just came back."""
    alpha = _pdf(tmp_path, "alpha.pdf")
    beta = _pdf(tmp_path, "beta.pdf")
    win.open_paths([alpha, beta])
    win.close_tab(area.index_of_path(beta))

    assert win.reopen_closed_tab() is True
    assert win.reopen_closed_tab() is False
    assert _paths(area) == [alpha, beta]


# ---------------------------------------------------------------------------
# Files that have gone, and the bound
# ---------------------------------------------------------------------------

def test_a_deleted_file_is_skipped_and_the_next_entry_opens(win, area, tmp_path):
    """Silently: no dialog, no exception, and it keeps going down the stack."""
    alpha = _pdf(tmp_path, "alpha.pdf")
    beta = _pdf(tmp_path, "beta.pdf")
    gamma = _pdf(tmp_path, "gamma.pdf")
    win.open_paths([alpha, beta, gamma])

    win.close_tab(area.index_of_path(gamma))
    win.close_tab(area.index_of_path(beta))
    os.remove(beta)                       # the newest entry, now unopenable

    assert win.reopen_closed_tab() is True

    assert _paths(area) == [alpha, gamma]
    assert area.current_view().document_path() == gamma
    area.check_invariant()


def test_a_stack_of_nothing_but_deleted_files_leaves_no_spare_tab(
        win, area, tmp_path):
    """The tab is made only once there is a live candidate for it, so a run of
    dead entries must not leave an empty tab behind."""
    alpha = _pdf(tmp_path, "alpha.pdf")
    beta = _pdf(tmp_path, "beta.pdf")
    win.open_paths([alpha, beta])
    win.close_tab(area.index_of_path(beta))
    os.remove(beta)

    assert win.reopen_closed_tab() is False

    assert _paths(area) == [alpha]
    area.check_invariant()


def test_the_stack_keeps_the_newest_ten(stack):
    """Bounded, oldest discarded. A closed tab is a path and a page, but an
    unbounded list of them is a history nobody walks back that far."""
    for i in range(MAX_ENTRIES + 4):
        stack.push({"path": f"file{i:02d}.pdf"})

    assert len(stack) == MAX_ENTRIES
    kept = [entry["path"] for entry in stack.entries()]
    assert kept == [f"file{i:02d}.pdf" for i in range(4, MAX_ENTRIES + 4)]
    assert stack.pop()["path"] == f"file{MAX_ENTRIES + 3:02d}.pdf"


def test_a_view_with_no_path_is_not_worth_recording(win, area, tmp_path):
    """An untitled or merged document lives only in memory, so there is no file
    to reopen and pretending otherwise would promise what cannot be delivered."""
    win.open_paths([_pdf(tmp_path, "alpha.pdf")])
    win.new_tab()
    assert area.count() == 2

    win.close_tab(1)                      # the empty one

    assert win.reopen_closed_tab() is False


# ---------------------------------------------------------------------------
# What is NOT a close
# ---------------------------------------------------------------------------

def test_a_cancelled_save_prompt_records_nothing(win, area, monkeypatch,
                                                 tmp_path, stack):
    """The tab is still open, so there is nothing to reopen. An entry here
    would put a second copy of a document the user never closed on screen."""
    alpha = _pdf(tmp_path, "alpha.pdf")
    beta = _pdf(tmp_path, "beta.pdf")
    win.open_paths([alpha, beta])
    area.set_current_index(0)
    win.view._mark_dirty()
    asked = _answer_with(monkeypatch, QMessageBox.StandardButton.Cancel)

    assert win.close_tab(0) is False

    assert asked, "the unsaved-changes prompt never opened"
    assert len(stack) == 0
    assert _paths(area) == [alpha, beta]


def test_discarding_at_the_save_prompt_does_record_it(win, area, monkeypatch,
                                                      tmp_path, stack):
    """The other half of the same rule: Discard IS a close."""
    alpha = _pdf(tmp_path, "alpha.pdf")
    beta = _pdf(tmp_path, "beta.pdf")
    win.open_paths([alpha, beta])
    area.set_current_index(0)
    win.view._mark_dirty()
    _answer_with(monkeypatch, QMessageBox.StandardButton.Discard)

    assert win.close_tab(0) is True

    assert [entry["path"] for entry in stack.entries()] == [alpha]


def test_a_tab_moved_to_another_window_is_not_a_close(win, area, tmp_path,
                                                      stack):
    """A document changing address is not a document going away. The move runs
    through `detach`, not `close_tab`, and that is exactly why the recording
    lives in `close_tab` rather than in `DocumentArea.remove_view`."""
    alpha = _pdf(tmp_path, "alpha.pdf")
    beta = _pdf(tmp_path, "beta.pdf")
    win.open_paths([alpha, beta])
    moving = area.view_at(area.index_of_path(beta))

    second = win.move_view_to_new_window(moving)
    try:
        assert second is not None
        assert _paths(area) == [alpha]
        assert _paths(second.document_area()) == [beta]
        assert len(stack) == 0
        assert win.reopen_closed_tab() is False
    finally:
        _dispose(second)


def test_a_file_already_open_here_activates_its_tab(win, area, tmp_path):
    """The rule `open_paths` follows for every other way a path arrives: one
    file, one tab. Reopening cannot be the exception that makes a second copy."""
    alpha = _pdf(tmp_path, "alpha.pdf")
    beta = _pdf(tmp_path, "beta.pdf")
    win.open_paths([alpha, beta])
    win.close_tab(area.index_of_path(beta))
    win.open_paths([beta])                # back, by the ordinary route
    assert area.count() == 2

    assert win.reopen_closed_tab() is True

    assert _paths(area) == [alpha, beta]
    assert area.current_view().document_path() == beta
    area.check_invariant()


# ---------------------------------------------------------------------------
# The X that closes the document and keeps the window
# ---------------------------------------------------------------------------

def test_the_x_that_empties_the_front_tab_is_still_a_close(win, area, store,
                                                           tmp_path, stack):
    """`close.x_closes == "document"` keeps the window and clears the view. No
    tab is removed, but the document the user was reading has gone, and "bring
    back what I just closed" is a question about the document."""
    store.close.x_closes = "document"
    alpha = _pdf(tmp_path, "alpha.pdf")
    win.open_paths([alpha])

    assert _close_event(win) is False      # ignored: the window survives
    assert win.view.document_path() is None
    assert [entry["path"] for entry in stack.entries()] == [alpha]

    assert win.reopen_closed_tab() is True
    assert win.view.document_path() == alpha
    assert area.count() == 1               # reused the tab it emptied
