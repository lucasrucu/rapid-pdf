"""The work that stopped running on the GUI thread, and the rules that let it.

WHAT THIS IS GUARDING. Before ui/worker.py there was no thread pool anywhere in
the app and OCR was the only background thread, so opening, saving and
searching all froze the window. Threading them introduces a class of bug that
does not raise: a PyMuPDF document driven from two threads is an access
violation, a stale result painted over a newer one is silently wrong, and a
worker that outlives its window aborts the process on the way out.

SO THE TESTS COME IN TWO KINDS, and the split is deliberate. Behaviour that can
be driven (a superseded query, a cancelled task, an exception, a window closing
under a running worker) is driven, with the event loop pumped by hand because
offscreen never runs one. The one thing that cannot be caught by running it,
that the search worker never reaches the live document, is asserted on the
STRUCTURE instead, exactly as tests/test_ocr_thread_safety.py does and for the
same reason: a data race usually interleaves harmlessly, so waiting to catch it
misbehaving is not a test that can pass every time.

NOTHING IN HERE SLEEPS WAITING FOR A THREAD. `pump_until` runs the event loop
until the thing being waited for has happened or a generous deadline passes,
and the assertions are on what arrived rather than on how long it took, because
a test that depends on timing is exactly what hides a threading bug.
"""

import ast
import pathlib
import threading
import time

import fitz
import pytest

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

import ui.document_view as document_view_module
from ui.document_view import DocumentView
from ui.main_window import MainWindow
from ui.worker import (
    BackgroundTask, DocumentSearcher, TaskCancelled, active_tasks,
    shutdown_tasks,
)

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def never_opens_a_dialog(monkeypatch):
    """A modal in an offscreen run hangs the suite instead of failing it.

    Every box this file could reach is a bug in the code under test, so they
    all become readable assertions. The tests that WANT to prove an error was
    reported patch `report_task_error` instead and look at the call.
    """
    for name in ("question", "warning", "critical", "information", "about"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, n=name, **k: pytest.fail(
                f"QMessageBox.{n} opened: {a[1:3]}")))


# ----------------------------------------------------------------------
# Running the loop by hand, because offscreen never runs one
# ----------------------------------------------------------------------

def pump(times: int = 3):
    app = QApplication.instance()
    for _ in range(times):
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)


def pump_until(predicate, timeout_s: float = 15.0) -> bool:
    """Run the event loop until `predicate` is true. False on the deadline."""
    app = QApplication.instance()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
    return predicate()


def a_pdf(path, pages=3, text="valve"):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=400, height=500)
        page.insert_text((20, 100), f"{text} {i}", fontsize=18)
    doc.save(str(path))
    doc.close()
    return str(path)


def _build(path=None):
    window = MainWindow()
    if path is not None:
        window.open_paths([path])
    window.view._canvas.resize(600, 700)
    window.view._canvas._flush_pending_render()
    return window


def _dispose(window):
    """The same order test_document_view_split.py uses, plus the threads.

    `teardown` is what stops the search thread and joins any worker, so the
    dispose path is also the assertion that it does: a window put away with a
    thread still in it aborts the process rather than failing a test.
    """
    for view in window._area.views():
        view.clear_document()
    window._teardown_for_quit()
    window.deleteLater()


@pytest.fixture
def win(qt_app, tmp_path):
    window = _build(a_pdf(tmp_path / "three.pdf"))
    yield window
    _dispose(window)


@pytest.fixture
def empty_win(qt_app):
    window = _build()
    yield window
    _dispose(window)


# ======================================================================
# 1. The worker abstraction
# ======================================================================

def test_the_callable_runs_off_the_gui_thread_and_the_result_lands_on_it(qt_app):
    """The whole point, in one test: the work is elsewhere, the answer is here."""
    seen = {}

    def work(ctx):
        return threading.get_ident()

    def landed(result):
        seen["worker"] = result
        seen["receiver"] = threading.get_ident()

    task = BackgroundTask(work, owner=None, name="ident")
    task.succeeded.connect(landed)
    task.start()
    assert pump_until(lambda: "worker" in seen), "the result never arrived"
    assert seen["receiver"] == threading.main_thread().ident
    assert seen["worker"] != threading.main_thread().ident
    task.shutdown()


def test_an_exception_inside_a_worker_surfaces_instead_of_vanishing(qt_app):
    """The failure this module exists to stop repeating.

    The house style was `print()` into a windowed build with no console, which
    is the same as saying nothing at all. A raise comes back as a message AND a
    traceback, so what reaches the user names the file and the line.
    """
    caught = {}

    def work(ctx):
        raise ValueError("the drawing is not a drawing")

    task = BackgroundTask(work, name="boom")
    task.failed.connect(lambda m, d: caught.update(message=m, details=d))
    task.start()
    assert pump_until(lambda: "message" in caught), "the exception vanished"
    assert caught["message"] == "the drawing is not a drawing"
    assert "ValueError" in caught["details"]
    assert "test_off_gui_thread" in caught["details"]
    task.shutdown()


def test_a_worker_that_is_cancelled_stops_and_says_so(qt_app):
    """Cancellation is cooperative: the callable stops when it next looks."""
    started = threading.Event()
    outcome = {}

    def work(ctx):
        started.set()
        for _ in range(10000):
            ctx.raise_if_cancelled()
            time.sleep(0.001)
        return "ran to the end"

    task = BackgroundTask(work, name="patient")
    task.cancelled.connect(lambda result: outcome.setdefault("cancelled", result))
    task.succeeded.connect(lambda result: outcome.setdefault("succeeded", result))
    task.start()
    assert started.wait(10), "the worker never started"
    task.cancel()
    assert pump_until(lambda: outcome), "the worker never stopped"
    assert "cancelled" in outcome
    assert "succeeded" not in outcome
    assert pump_until(lambda: not task.is_running())


def test_a_result_nobody_wanted_comes_back_to_be_disposed_of(qt_app):
    """A cancelled open can still have produced a fitz document, and something
    has to close it. It is handed back on `cancelled` rather than dropped on
    the worker thread, which is not the thread to close it on."""
    started = threading.Event()
    let_go = threading.Event()
    handed_back = {}

    def work(ctx):
        started.set()
        let_go.wait(10)
        return "a thing that has to be closed"

    task = BackgroundTask(work, name="late")
    task.cancelled.connect(lambda result: handed_back.setdefault("result", result))
    task.start()
    # Waited on rather than pumped for: the cancel has to land INSIDE the
    # callable, and a fixed number of loop turns is a race, not a wait.
    assert started.wait(10), "the worker never started"
    task.cancel()          # cancelled while the callable is still in flight
    let_go.set()
    assert pump_until(lambda: handed_back), "the orphaned result was dropped"
    assert handed_back["result"] == "a thing that has to be closed"


def test_the_gui_thread_keeps_processing_while_a_worker_runs(qt_app):
    """A frozen window with a spinner is still a frozen window.

    The same shape the printing tests use: prove the event loop is still
    delivering by giving it something to deliver and counting it. If the work
    were on the GUI thread the timer could not tick at all until it finished.
    """
    release = threading.Event()
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()

    task = BackgroundTask(lambda ctx: release.wait(10), name="slow")
    task.start()
    assert pump_until(lambda: len(ticks) >= 5), \
        "the event loop stopped while the worker ran"
    assert task.is_running()
    release.set()
    assert pump_until(lambda: not task.is_running())
    timer.stop()


def test_no_worker_outlives_its_window(win):
    """Closing the window is the end of the work it started.

    `_teardown_for_quit` cancels and JOINS. It never terminates a thread:
    killing one inside MuPDF is how a document gets corrupted, not how a window
    gets closed.
    """
    release = threading.Event()

    def work(ctx):
        while not ctx.is_cancelled():
            if release.wait(0.01):
                break
        return None

    task = BackgroundTask(work, owner=win, name="clinging")
    task.start()
    pump()
    assert active_tasks(win) == [task]

    win._teardown_for_quit()
    assert not task.is_running()
    assert active_tasks(win) == []


def test_a_task_owned_by_a_view_is_caught_by_the_window_it_sits_in(win):
    """Scoping is by ancestry, not by identity: a threaded open is owned by the
    window (its progress dialog is the window's) and a search by the view, and
    one sweep at the window has to catch both."""
    release = threading.Event()
    task = BackgroundTask(lambda ctx: release.wait(5), owner=win.view,
                          name="in-a-view")
    task.start()
    pump()
    assert task in active_tasks(win)
    release.set()
    assert shutdown_tasks(win) >= 1
    assert not task.is_running()


# ======================================================================
# 2. Search
# ======================================================================

def _open_search(view):
    view.open_search()
    return view._ensure_searcher()


def test_the_search_runs_off_the_gui_thread_and_still_finds_every_hit(win, tmp_path):
    view = win.view
    _open_search(view)
    view._start_search("valve")
    assert pump_until(lambda: view._search_term == "valve"), "no results arrived"
    assert len(view._search_hits) == 3          # one per page
    assert [pn for pn, _ in view._search_hits] == [0, 1, 2]


def test_a_superseded_search_does_not_deliver_its_stale_results(win):
    """The one that matters most, and the reason the generation counter exists.

    Two queries asked for in the same turn, before the loop is pumped at all.
    The older one is already out of date by the time the worker looks at it, so
    it stops without emitting; and if it had got as far as emitting, the check
    at the landing point drops it. Either way the find bar only ever sees the
    newest term, rather than the older scan finishing last and painting over it.
    """
    view = win.view
    searcher = _open_search(view)
    delivered = []
    searcher.results_ready.connect(lambda term, hits: delivered.append(term))

    view._start_search("valve 0")
    view._start_search("valve 2")        # supersedes, does not queue behind

    assert pump_until(lambda: delivered), "no results at all"
    pump(20)                             # give the stale one every chance to land
    assert delivered == ["valve 2"], f"a stale result was delivered: {delivered}"
    assert view._search_term == "valve 2"
    assert [pn for pn, _ in view._search_hits] == [2]


def test_a_stale_result_is_dropped_at_the_landing_point_too(win):
    """Belt and braces, tested on its own because it is the half that catches a
    worker which had already emitted when the newer query arrived."""
    view = win.view
    searcher = _open_search(view)
    delivered = []
    searcher.results_ready.connect(lambda term, hits: delivered.append(term))

    generation = searcher.search("valve")
    searcher.cancel()                    # bumps the generation, as a new query does
    searcher._on_results(generation, "valve", [(0, (1.0, 2.0, 3.0, 4.0))])
    assert delivered == []


def test_typing_a_new_term_cancels_the_one_in_flight(win):
    view = win.view
    searcher = _open_search(view)
    view._start_search("valve")
    assert searcher.is_searching()
    view._on_search_term_changed("v")    # back under two characters
    assert not searcher.is_searching()
    pump(10)
    assert view._search_hits == []


def test_closing_the_find_bar_cancels_the_search(win):
    view = win.view
    searcher = _open_search(view)
    view._start_search("valve")
    view._on_search_closed()
    assert not searcher.is_searching()
    pump(10)
    assert view._search_hits == []
    assert view._search_term is None


def test_the_search_stays_usable_after_it_is_cancelled(win):
    """A cancelled operation leaves the document usable, which for a search
    means the next one still answers."""
    view = win.view
    _open_search(view)
    view._start_search("valve")
    view._cancel_search()
    pump(5)
    view._start_search("valve 1")
    assert pump_until(lambda: view._search_term == "valve 1")
    assert [pn for pn, _ in view._search_hits] == [1]
    assert view.page_count() == 3         # and the document itself is untouched


def test_a_search_failure_reaches_the_user_rather_than_the_console(win, monkeypatch):
    reported = {}
    monkeypatch.setattr(document_view_module, "report_task_error",
                        lambda parent, title, message, details="":
                        reported.update(title=title, message=message,
                                        details=details))
    view = win.view
    searcher = _open_search(view)
    # A source that cannot be opened: the worker raises where nothing else can
    # catch it, which is exactly the case that used to disappear.
    searcher.set_source("path", str(REPO / "no-such-file-anywhere.pdf"), "bogus")
    view._start_search("valve")
    assert pump_until(lambda: reported), "the search failure vanished"
    assert reported["title"] == "Search Error"
    assert reported["details"], "no traceback came with it"


def test_an_edit_makes_the_search_thread_rebuild_its_copy(win):
    """The private copy has to follow the document, or a search answers about a
    file that is no longer what is on screen."""
    view = win.view
    _open_search(view)
    before = view._search_source_sent
    view._mark_dirty()
    view._refresh_search_source()
    assert view._search_source_sent != before


def test_a_dirty_document_is_searched_from_bytes_not_from_the_file(win):
    """Clean and on disk, the file itself is the copy and costs nothing to
    describe. Dirty, it is not the document any more, so the bytes go across."""
    view = win.view
    searcher = _open_search(view)
    assert searcher._state.source()[1] == "path"
    view._mark_dirty()
    view._refresh_search_source()
    assert searcher._state.source()[1] == "bytes"


# ----------------------------------------------------------------------
# The structural rule, asserted the way test_ocr_thread_safety.py does
# ----------------------------------------------------------------------

def test_the_search_worker_never_names_the_live_document_type():
    """A worker may only touch a document no other thread can reach.

    `ui/worker.py` opens its own fitz document from a path or from bytes and
    never imports or mentions PDFDocument, so there is no way to hand it the
    live one. That is the guarantee; a running test cannot make it, because a
    data race usually interleaves harmlessly.
    """
    source = (REPO / "ui" / "worker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "PDFDocument" not in names
    assert "PDFDocument" not in [
        alias.name
        for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]


def test_the_hits_cross_the_thread_as_numbers(win):
    """Four floats, not a Rect born of a document that is about to be closed.

    Almost certainly a Rect would be fine; almost is the wrong standard for the
    class of bug this module exists to avoid.
    """
    view = win.view
    searcher = _open_search(view)
    raw = []
    searcher._worker.results.connect(lambda g, t, hits: raw.append(hits))
    view._start_search("valve")
    assert pump_until(lambda: raw)
    assert all(isinstance(box, tuple) and len(box) == 4 for _, box in raw[0])
    # And the view gets Rects back, because that is what the canvas draws with.
    assert pump_until(lambda: view._search_hits)
    assert isinstance(view._search_hits[0][1], fitz.Rect)


def test_the_search_thread_is_not_started_until_somebody_searches(win):
    """Eight restored tabs must not mean eight idle threads."""
    assert win.view._searcher is None


# ======================================================================
# 3. Open
# ======================================================================

def test_a_file_over_the_threshold_is_read_on_a_worker(empty_win, tmp_path,
                                                       monkeypatch):
    monkeypatch.setattr(document_view_module, "OPEN_ASYNC_MIN_BYTES", 1)
    path = a_pdf(tmp_path / "big.pdf", pages=4)
    view = empty_win.view
    assert view._should_thread_open(path)
    assert view.open_path(path)
    assert view.page_count() == 4
    assert view.document_path() == path
    assert not view.is_dirty()
    assert view.document().last_open_error is None


def test_a_small_file_is_read_inline_and_never_starts_a_thread(empty_win, tmp_path):
    """Below the line a thread and a modal dialog cost more than the parse, so
    the threaded path would make opening slower and flash a dialog at every
    double click."""
    path = a_pdf(tmp_path / "small.pdf")
    view = empty_win.view
    assert not view._should_thread_open(path)
    assert view.open_path(path)
    assert active_tasks(empty_win) == []


def test_the_window_keeps_drawing_while_a_big_file_is_read(empty_win, tmp_path,
                                                           monkeypatch):
    """The local event loop is what makes the synchronous contract survivable.

    `open_path` still returns a bool that two hundred callers read immediately.
    What went away is the freeze, so the same assertion the printing tests make
    applies: the loop is still delivering while the read is in flight.
    """
    monkeypatch.setattr(document_view_module, "OPEN_ASYNC_MIN_BYTES", 1)
    path = a_pdf(tmp_path / "slow.pdf")
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()

    real = document_view_module._read_pdf

    def slow(target):
        inner = real(target)

        def run(ctx):
            time.sleep(0.25)
            return inner(ctx)
        return run

    monkeypatch.setattr(document_view_module, "_read_pdf", slow)
    assert empty_win.view.open_path(path)
    timer.stop()
    assert len(ticks) >= 3, "the window was frozen for the whole read"


def test_a_cancelled_open_says_nothing_and_leaves_a_tab_that_still_works(
        empty_win, tmp_path, monkeypatch):
    """Cancel is not an error. The tab is empty, which is where it started, and
    the same file opens normally straight afterwards.

    `never_opens_a_dialog` is doing half the assertion: a message box saying
    "you cancelled" would fail this test rather than being tolerated.
    """
    monkeypatch.setattr(document_view_module, "OPEN_ASYNC_MIN_BYTES", 1)
    path = a_pdf(tmp_path / "cancel-me.pdf")
    view = empty_win.view
    monkeypatch.setattr(document_view_module, "run_blocking_with_progress",
                        lambda *a, **k: ("cancelled", None, "", ""))
    assert not view.open_path(path)
    assert view._open_cancelled
    assert not view.has_document()
    assert view.is_empty()

    monkeypatch.undo()
    assert view.open_path(path)
    assert view.page_count() == 3


def test_a_worker_that_blows_up_opening_reaches_the_user(empty_win, tmp_path,
                                                         monkeypatch):
    monkeypatch.setattr(document_view_module, "OPEN_ASYNC_MIN_BYTES", 1)
    reported = {}
    monkeypatch.setattr(document_view_module, "report_task_error",
                        lambda parent, title, message, details="":
                        reported.update(title=title, details=details))

    def explode(path):
        def run(ctx):
            raise OSError("the share went away")
        return run

    monkeypatch.setattr(document_view_module, "_read_pdf", explode)
    path = a_pdf(tmp_path / "doomed.pdf")
    assert not empty_win.view.open_path(path)
    assert reported["title"] == "Open Error"
    assert "OSError" in reported["details"]


def test_a_file_the_worker_cannot_read_falls_back_to_the_core(empty_win, tmp_path,
                                                              monkeypatch):
    """Everything that is not a clean open is the core's to answer: it owns the
    error text, the locked-handle bookkeeping and the retry counters."""
    monkeypatch.setattr(document_view_module, "OPEN_ASYNC_MIN_BYTES", 1)
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7 this is not a pdf" + b"\0" * 4096)
    view = empty_win.view
    assert not view._load_document(str(broken))
    assert not view._open_cancelled
    assert view.document().last_open_error


# ======================================================================
# 4. Save keeps the cursor honest
# ======================================================================

def test_a_save_puts_the_override_cursor_back(win, tmp_path):
    """The busy state is a wait cursor, and a wait cursor that is not taken off
    again is a window that looks broken for the rest of the session."""
    assert QApplication.overrideCursor() is None
    assert win.view.save_pdf()
    assert QApplication.overrideCursor() is None


def test_a_failed_save_puts_the_override_cursor_back_too(win, monkeypatch):
    monkeypatch.setattr(win.view._doc, "save", lambda *a, **k: False)
    monkeypatch.setattr(document_view_module.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: None))
    assert not win.view.save_pdf()
    assert QApplication.overrideCursor() is None


def test_a_save_that_raises_still_puts_the_cursor_back(win, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(win.view._doc, "save", boom)
    with pytest.raises(RuntimeError):
        win.view.save_pdf()
    assert QApplication.overrideCursor() is None


# ======================================================================
# 5. Session restore, which the threaded open must not have broken
# ======================================================================

def test_session_restore_still_opens_a_pending_tab_when_it_comes_to_the_front(
        empty_win, tmp_path):
    """A restored tab stands for a file it has not read. `set_active` is what
    reads it, and that is still true with the read on a worker."""
    path = a_pdf(tmp_path / "restored.pdf", pages=2)
    view = empty_win.view
    assert view.stage_path(path, page=1)
    assert view.is_pending()
    assert not view.has_document()

    view.set_active(True)
    assert view.has_document()
    assert view.page_count() == 2
    assert view.document_path() == path
    assert not view.is_pending()


def test_a_pending_tab_over_the_threshold_opens_through_the_worker(
        empty_win, tmp_path, monkeypatch):
    monkeypatch.setattr(document_view_module, "OPEN_ASYNC_MIN_BYTES", 1)
    path = a_pdf(tmp_path / "restored-big.pdf", pages=4)
    view = empty_win.view
    assert view.stage_path(path, page=2)
    assert view.ensure_loaded()
    assert view.page_count() == 4
    assert view.current_page() == 2
    assert not view.is_pending()


def test_a_pending_tab_is_only_read_once(empty_win, tmp_path):
    """`ensure_loaded` is driven by every activation, so the second one must
    find nothing to do rather than re-reading the file."""
    path = a_pdf(tmp_path / "once.pdf")
    view = empty_win.view
    view.stage_path(path)
    assert view.ensure_loaded()
    assert not view.ensure_loaded()


# ======================================================================
# 6. The searcher's own lifecycle
# ======================================================================

def test_the_search_thread_is_stopped_and_joined_by_teardown(qt_app, tmp_path):
    window = _build(a_pdf(tmp_path / "bye.pdf"))
    view = window.view
    searcher = _open_search(view)
    view._start_search("valve")
    thread = searcher._thread
    assert thread.isRunning()
    _dispose(window)
    assert not thread.isRunning()


def test_shutting_a_searcher_down_twice_is_not_a_crash(qt_app, tmp_path):
    searcher = DocumentSearcher(owner=None)
    searcher.shutdown()
    searcher.shutdown()
    assert not searcher._thread.isRunning()


def test_task_cancelled_is_reported_as_a_cancellation_not_a_failure(qt_app):
    """A cooperative early return is not an error, and must never reach the
    user as one."""
    outcome = []

    def work(ctx):
        raise TaskCancelled()

    task = BackgroundTask(work, name="polite")
    task.cancelled.connect(lambda r: outcome.append("cancelled"))
    task.failed.connect(lambda m, d: outcome.append("failed"))
    task.start()
    assert pump_until(lambda: outcome)
    assert outcome == ["cancelled"]
