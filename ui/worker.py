"""Off-thread work, and the one place that is allowed to know how to do it.

WHY THIS EXISTS. Before it, the only background thread in the app was OCR's
(core/ocr_worker.py). Opening, saving, searching, combining, splitting and
printing all ran on the GUI thread, so a slow operation was a frozen window
rather than a busy one. On the A1 drawings and the 500-page packs this app is
built for, that is felt on every keystroke in the find bar.

THE RULE, AND IT IS THE WHOLE DESIGN: A WORKER MAY ONLY TOUCH A DOCUMENT NO
OTHER THREAD CAN REACH. PyMuPDF documents are not thread safe and MuPDF has no
lock covering the page tree, so two threads in one document is a segfault
waiting for the right interleaving, not an exception you can catch. The repo
already learned this twice: core/ocr_worker.py's module docstring is the long
version, and tests/test_ocr_thread_safety.py guards it.

The rule sorts the work by itself, which is why it is a rule and not a case by
case judgement:

  - SEARCH qualifies. `DocumentSearcher` below opens its own private copy of
    the document (from the file, or from bytes the UI thread serialised) and
    every `search_for` runs against that. The live document is never read from
    the search thread.
  - OPEN qualifies. The view is empty when it runs, so the handle the worker
    builds is not reachable from anywhere until the worker has finished and
    the UI thread adopts it.
  - SAVE DOES NOT. A rewrite reads the live document, and an incremental save
    of a signed or encrypted file writes through the very handle the canvas
    renders from. Serialising a private copy first would move the expensive
    half (`tobytes`) back onto the GUI thread and leave only the disk write
    off it, and the incremental path cannot be done on a copy at all. So save
    stays synchronous, with a wait cursor, and ui/document_view.py says so
    where it happens.

HANDING A DOCUMENT BETWEEN THREADS IS FINE; TOUCHING IT FROM TWO IS NOT. A
worker builds a fitz document, stops touching it, and returns it. The result
reaches the GUI thread through a queued signal delivered after the worker's
call has returned, which is the happens-before edge that makes the handover
safe. One thread at a time is the invariant, not thread affinity.

WHAT A FAILURE MUST NOT DO IS VANISH. The house style of `print()` into a
windowed build with no console is exactly the failure this module refuses to
repeat: a callable that raises hands its message and its traceback out through
`failed`, and `report_task_error` puts both in front of the user.

NO WORKER OUTLIVES ITS WINDOW. Tasks are not Qt children of the widget that
started them: destroying a running QThread aborts the process, so parenting one
to a widget would turn "close the window mid-open" into a crash. They are held
by the module registry below instead, scoped to an owner widget, and
`shutdown_tasks(owner)` cancels and joins them. If the owner is destroyed
without that (a path nobody should take, but a `deleteLater` in a test will),
`_on_owner_destroyed` cuts every outward connection so nothing lands on a dead
widget, and the task finishes into the void and unregisters itself.
"""

import threading
import traceback

from PySide6.QtCore import (
    QEventLoop, QMetaObject, QObject, QThread, Qt, Signal, Slot,
)
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

#: How long `shutdown_tasks` waits for a worker to notice it was cancelled
#: before giving up on it and abandoning it. Generous, because the thing being
#: waited for is usually one `fitz.open` of a file MuPDF decided to repair,
#: which has no cancellation hook to check.
SHUTDOWN_WAIT_MS = 5000

#: Emit search progress no more often than this many pages. A signal per page
#: across a 500-page pack is 500 queued events for a label nobody can read
#: that fast.
SEARCH_PROGRESS_EVERY = 8


class TaskCancelled(Exception):
    """Raised out of a worker callable by `TaskContext.raise_if_cancelled`.

    Caught by the job runner and reported as a cancellation rather than as a
    failure, so a cooperative early return never reaches the user as an error.
    """


class TaskContext:
    """The handle a worker callable is given. Read the cancel flag, say how
    far along it is, and nothing else.

    Deliberately not a QObject: it is used on the worker thread and its only
    job is to forward to the job that owns it.
    """

    def __init__(self, job):
        self._job = job

    def is_cancelled(self) -> bool:
        return self._job.is_cancelled()

    def raise_if_cancelled(self):
        if self._job.is_cancelled():
            raise TaskCancelled()

    def report(self, done: int, total: int = 0, label: str = ""):
        """Publish progress. Delivered on the GUI thread by Qt, queued."""
        self._job.progressed.emit(int(done), int(total), str(label))


class _Job(QObject):
    """Lives on the worker thread and runs exactly one callable.

    Kept separate from `BackgroundTask` because `moveToThread` needs an object
    with no parent, and because the thing that runs on the worker thread should
    be the thing that has nothing else in it.
    """

    progressed = Signal(int, int, str)
    succeeded = Signal(object)
    cancelled = Signal(object)     # carries the result, if there was one, to dispose of
    failed = Signal(str, str)      # message, traceback
    ended = Signal()               # always last, whatever happened

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self._cancel = threading.Event()

    def cancel(self):
        """Safe from any thread: threading.Event is what it is for."""
        self._cancel.set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    @Slot()
    def run(self):
        result = None
        try:
            if self.is_cancelled():
                self.cancelled.emit(None)
            else:
                result = self._fn(TaskContext(self))
                if self.is_cancelled():
                    # Finished, but nobody wants it any more. The result goes
                    # out on `cancelled` rather than being dropped here,
                    # because it may be a fitz document that has to be closed
                    # and this is not the thread to close it on.
                    self.cancelled.emit(result)
                else:
                    self.succeeded.emit(result)
        except TaskCancelled:
            self.cancelled.emit(None)
        except BaseException as e:            # noqa: BLE001 - nothing may vanish
            self.failed.emit(str(e) or e.__class__.__name__,
                             traceback.format_exc())
        finally:
            self.ended.emit()
            # THE THREAD ENDS ITSELF, FROM ITSELF, and the usual
            # `worker.finished.connect(thread.quit)` will not do. The QThread
            # object lives on the GUI thread, so that connection is QUEUED to
            # the GUI thread's event loop, and `shutdown()` joins by BLOCKING
            # the GUI thread. Nothing would ever deliver the quit, so every
            # shutdown would sit out its full timeout and then abandon a worker
            # that had already finished. Called from here it is direct, and Qt
            # handles a quit that lands before `exec()` by returning from it
            # immediately.
            thread = QThread.currentThread()
            if thread is not None:
                thread.quit()


class BackgroundTask(QObject):
    """One callable, one thread, one result, delivered on the GUI thread.

    Built and started on the GUI thread. `owner` is the widget the work belongs
    to; it decides what `shutdown_tasks` catches and when the outward signals
    are cut. It is NOT a Qt parent, on purpose: see the module docstring.
    """

    progressed = Signal(int, int, str)
    succeeded = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str, str)
    finished = Signal()            # exactly once, after one of the above

    def __init__(self, fn, owner=None, name: str = ""):
        super().__init__()
        self._owner = owner
        self.name = name or getattr(fn, "__name__", "task")
        self._thread = QThread()
        self._job = _Job(fn)
        self._job.moveToThread(self._thread)
        self._abandoned = False
        self._running = False

        self._thread.started.connect(self._job.run)
        self._job.progressed.connect(self._on_progress)
        self._job.succeeded.connect(self._on_succeeded)
        self._job.cancelled.connect(self._on_cancelled)
        self._job.failed.connect(self._on_failed)
        self._thread.finished.connect(self._on_thread_finished)

        if owner is not None and hasattr(owner, "destroyed"):
            owner.destroyed.connect(self._on_owner_destroyed)

    # -- lifecycle -----------------------------------------------------

    def owner(self):
        return self._owner

    def is_running(self) -> bool:
        """The THREAD is the authority, not a flag set by a queued slot.

        `_on_thread_finished` only runs when the event loop is pumped, so a
        flag would still say "running" for a worker that has already stopped,
        which is the difference between a test that waits and a test that
        fails.
        """
        return bool(self._thread.isRunning())

    def start(self) -> "BackgroundTask":
        if self._running:
            return self
        self._running = True
        _register(self)
        self._thread.start()
        return self

    def cancel(self):
        """Ask the callable to stop. It stops when it next looks."""
        self._job.cancel()

    def abandon(self):
        """Cancel, and cut every wire back to the owner.

        For the case where the owner is going away and the worker cannot be
        stopped in time. The thread runs to its natural end and unregisters
        itself; nothing it emits reaches a widget any more.
        """
        if self._abandoned:
            return
        self._abandoned = True
        self._job.cancel()
        # blockSignals rather than disconnect: it cuts every outward wire in
        # one call, it does not complain about the ones nobody had connected,
        # and it leaves the INWARD connection from the thread alone, so
        # `_on_thread_finished` still runs and still unregisters this task.
        self.blockSignals(True)

    def wait(self, timeout_ms: int = SHUTDOWN_WAIT_MS) -> bool:
        """Block until the worker is done. True if it finished in time."""
        if not self._thread.isRunning():
            return True
        return bool(self._thread.wait(timeout_ms))

    def shutdown(self, timeout_ms: int = SHUTDOWN_WAIT_MS) -> bool:
        """Cancel and join. Abandons the task if it will not stop in time.

        Never terminates the thread: killing a thread inside MuPDF is how you
        corrupt a document rather than how you close a window.
        """
        self.cancel()
        if self.wait(timeout_ms):
            # Unregistered HERE rather than left to `_on_thread_finished`,
            # which is queued and would not have run yet. A caller that has
            # just joined a thread is entitled to see it gone from the
            # registry, and unregistering twice is a no-op.
            self._running = False
            _unregister(self)
            return True
        self.abandon()
        return False

    # -- signal plumbing, all of this runs on the GUI thread -----------

    @Slot(int, int, str)
    def _on_progress(self, done, total, label):
        self.progressed.emit(done, total, label)

    @Slot(object)
    def _on_succeeded(self, result):
        self.succeeded.emit(result)

    @Slot(object)
    def _on_cancelled(self, result):
        self.cancelled.emit(result)

    @Slot(str, str)
    def _on_failed(self, message, details):
        self.failed.emit(message, details)

    @Slot()
    def _on_thread_finished(self):
        self._running = False
        _unregister(self)
        self.finished.emit()

    @Slot()
    def _on_owner_destroyed(self):
        self._owner = None
        self.abandon()


# ----------------------------------------------------------------------
# The registry. What makes "no worker outlives its window" checkable.
# ----------------------------------------------------------------------

_ACTIVE: list = []


def _register(task):
    if task not in _ACTIVE:
        _ACTIVE.append(task)


def _unregister(task):
    if task in _ACTIVE:
        _ACTIVE.remove(task)


def _owned_by(task, target) -> bool:
    """Whether `task` belongs to `target` or to something inside it."""
    owner = task.owner()
    if owner is None:
        return False
    if owner is target:
        return True
    is_ancestor = getattr(target, "isAncestorOf", None)
    if is_ancestor is None:
        return False
    try:
        return bool(is_ancestor(owner))
    except RuntimeError:
        return False          # the owner's C++ side has already gone


def active_tasks(owner=None) -> list:
    """Every running task, or only the ones under `owner`."""
    if owner is None:
        return list(_ACTIVE)
    return [t for t in _ACTIVE if _owned_by(t, owner)]


def shutdown_tasks(owner, timeout_ms: int = SHUTDOWN_WAIT_MS) -> int:
    """Cancel and join every task under `owner`. Returns how many were caught.

    Called from `DocumentView.teardown` and `MainWindow.closeEvent`, which are
    the two places a window's work stops being wanted.
    """
    caught = active_tasks(owner)
    for task in caught:
        task.shutdown(timeout_ms)
    return len(caught)


def report_task_error(parent, title: str, message: str, details: str = ""):
    """Put a worker's exception in front of the user, traceback and all.

    The traceback goes in the detailed text rather than the message: it is what
    makes a field report actionable and it is not what the user needs to read
    first. This function exists so that no background failure is ever a
    `print()` into a windowed build with no console.
    """
    box = QMessageBox(QMessageBox.Icon.Critical, title, message, parent=parent)
    if details:
        box.setDetailedText(details)
    box.exec()


def run_blocking_with_progress(parent, fn, title: str, label: str,
                               cancel_text: str = "Cancel", name: str = "",
                               dispose=None):
    """Run `fn` off-thread while the GUI keeps drawing, and return its result.

    For work whose CALLER cannot become asynchronous: `DocumentView.open_path`
    returns a bool that roughly two hundred places, tests included, read
    immediately. So the call stays synchronous and the FREEZE is what goes
    away: the parse runs on a worker, a window-modal progress dialog keeps the
    window repainting and the Cancel button readable, and a local event loop
    holds the caller until the worker lands.

    Returns (status, result, message, details) where status is one of
    "ok", "cancelled", "failed".

    THE MODAL DIALOG IS THE REENTRANCY GUARD. A local event loop means the user
    can act while a call is part way through, and window modality is what stops
    them acting on the window that is part way through. Other windows stay
    live, which is the point of using a modal dialog rather than blocking.

    Cancel does not abort the work; there is no hook inside `fitz.open` to
    check. It stops the caller waiting, and the task is left to finish. What it
    produced then arrives on `cancelled`, after this function has returned, and
    `dispose` is what closes it. Without that a cancelled open would leak one
    whole fitz document, which is the sort of thing that only shows up on the
    machine with eight A1 drawings open.
    """
    loop = QEventLoop()
    outcome = {"status": "cancelled", "result": None, "message": "", "details": ""}

    dialog = QProgressDialog(label, cancel_text, 0, 0, parent)
    dialog.setWindowTitle(title)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setValue(0)

    task = BackgroundTask(fn, owner=parent, name=name or title)

    def _progress(done, total, text):
        if total > 0:
            dialog.setMaximum(total)
            dialog.setValue(done)
        if text:
            dialog.setLabelText(text)

    def _ok(result):
        outcome["status"] = "ok"
        outcome["result"] = result

    def _fail(message, details):
        outcome["status"] = "failed"
        outcome["message"] = message
        outcome["details"] = details

    def _discard(result):
        if dispose is not None and result is not None:
            dispose(result)

    task.progressed.connect(_progress)
    task.succeeded.connect(_ok)
    task.failed.connect(_fail)
    task.cancelled.connect(_discard)
    task.finished.connect(loop.quit)
    dialog.canceled.connect(task.cancel)
    dialog.canceled.connect(loop.quit)

    task.start()
    dialog.show()
    loop.exec()
    dialog.close()
    dialog.deleteLater()
    return outcome["status"], outcome["result"], outcome["message"], outcome["details"]


# ----------------------------------------------------------------------
# Search: a worker thread that outlives one query, because the private
# document is the expensive part and re-opening it per keystroke would
# cost more than the search it is there to speed up.
# ----------------------------------------------------------------------

class _SearchState:
    """The generation counter and the source, shared between the two threads.

    A plain lock rather than a Qt one: the only things crossing are an int and
    a small tuple, and this is read once per page.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._source = None        # (token, kind, payload)

    def bump(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def generation(self) -> int:
        with self._lock:
            return self._generation

    def is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def set_source(self, source):
        with self._lock:
            self._source = source

    def source(self):
        with self._lock:
            return self._source


class _SearchWorker(QObject):
    """Lives on the search thread and owns a PRIVATE copy of the document.

    Never given the live PDFDocument, and there is no attribute on it that the
    GUI thread also holds. It opens its own fitz document from the source the
    GUI thread described (a path for a document that is clean on disk, bytes
    for one that is not), searches that, and hands back plain tuples.

    THE HITS COME BACK AS TUPLES, NOT AS fitz.Rect. A Rect is four floats and
    carries no reference to the document it came from, so passing one would
    almost certainly be fine. Almost is the wrong standard for the class of bug
    this module exists to avoid, and rebuilding four floats on the other side
    costs nothing.
    """

    progressed = Signal(int, int, int)        # generation, pages done, pages total
    results = Signal(int, str, object)        # generation, term, [(page, (x0,y0,x1,y1))]
    failed = Signal(int, str, str)            # generation, message, traceback

    def __init__(self, state: _SearchState):
        super().__init__()
        self._state = state
        self._doc = None
        self._token = None

    @Slot()
    def release(self):
        """Close the private document. Runs on the search thread, before quit."""
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
        self._doc = None
        self._token = None

    def _ensure_document(self):
        """Open (or re-open) the private copy for the current source."""
        import fitz

        source = self._state.source()
        if source is None:
            self.release()
            return None
        token, kind, payload = source
        if self._doc is not None and token == self._token:
            return self._doc
        self.release()
        if kind == "path":
            self._doc = fitz.open(payload)
        else:
            self._doc = fitz.open("pdf", payload)
        self._token = token
        return self._doc

    @Slot(int, str)
    def run_query(self, generation: int, term: str):
        """One search of the whole private document, abandoned the moment a
        newer query has been asked for.

        Queries arrive through this thread's event loop, so they are already
        serialised. Supersession is the generation check: a query that is no
        longer the current one stops between pages and emits nothing, so a
        stale result cannot land on the find bar and the newer query is not
        left queueing behind a scan of five hundred pages.
        """
        if not self._state.is_current(generation):
            return
        try:
            doc = self._ensure_document()
            if doc is None:
                self.results.emit(generation, term, [])
                return
            total = doc.page_count
            hits = []
            for page_num in range(total):
                if not self._state.is_current(generation):
                    return
                if page_num % SEARCH_PROGRESS_EVERY == 0:
                    self.progressed.emit(generation, page_num, total)
                try:
                    for rect in doc[page_num].search_for(term):
                        hits.append((page_num,
                                     (rect.x0, rect.y0, rect.x1, rect.y1)))
                except Exception:
                    # One unreadable page is not a failed search. The old
                    # synchronous code printed here, which in a windowed build
                    # is the same as saying nothing, so it says nothing on
                    # purpose and the other pages still answer.
                    continue
            if not self._state.is_current(generation):
                return
            self.results.emit(generation, term, hits)
        except BaseException as e:          # noqa: BLE001 - nothing may vanish
            self.failed.emit(generation, str(e) or e.__class__.__name__,
                             traceback.format_exc())


class DocumentSearcher(QObject):
    """Search one document off the GUI thread, newest query wins.

    One of these per DocumentView, built the first time the find bar is opened
    and shut down with the view. It holds a thread that outlives an individual
    query because the private copy of the document is the expensive part:
    re-opening a 500-page pack per keystroke would cost more than the search.

    `set_source` describes where the private copy comes from and is called
    again whenever the live document changes; the token is what tells the
    worker its copy is stale.
    """

    progressed = Signal(int, int)             # pages done, pages total
    results_ready = Signal(str, object)       # term, [(page_num, fitz.Rect)]
    failed = Signal(str, str)                 # message, traceback

    #: GUI thread to search thread. Queued by Qt because the receiver lives on
    #: another thread, which is exactly the delivery wanted.
    _request = Signal(int, str)

    def __init__(self, owner=None):
        super().__init__()
        self._owner = owner
        self._state = _SearchState()
        self._thread = QThread()
        self._worker = _SearchWorker(self._state)
        self._worker.moveToThread(self._thread)
        self._worker.progressed.connect(self._on_progress)
        self._worker.results.connect(self._on_results)
        self._worker.failed.connect(self._on_failed)
        self._request.connect(self._worker.run_query)
        self._pending_term = None
        self._thread.start()
        if owner is not None and hasattr(owner, "destroyed"):
            owner.destroyed.connect(self._on_owner_destroyed)

    @Slot()
    def _on_owner_destroyed(self):
        """A separate slot because `destroyed` carries the QObject, and
        `shutdown`'s only parameter is a timeout."""
        self.shutdown()

    # -- the source of the private copy --------------------------------

    def set_source(self, kind: str, payload, token):
        """Describe the private copy: ("path", filename) or ("bytes", data).

        `token` is compared by the worker to decide whether the copy it is
        holding is still the right one, so it has to change whenever the
        document does.
        """
        self._state.set_source((token, kind, payload))

    def clear_source(self):
        self._state.set_source(None)

    def has_source(self) -> bool:
        return self._state.source() is not None

    # -- queries -------------------------------------------------------

    def search(self, term: str) -> int:
        """Ask for `term`. Supersedes anything already in flight."""
        generation = self._state.bump()
        self._pending_term = term
        self._request.emit(generation, term)
        return generation

    def cancel(self):
        """Abandon whatever is in flight. Nothing more is delivered."""
        self._state.bump()
        self._pending_term = None

    def is_searching(self) -> bool:
        return self._pending_term is not None

    def pending_term(self):
        return self._pending_term

    def shutdown(self, timeout_ms: int = SHUTDOWN_WAIT_MS):
        """Stop the thread and close the private document, in that order.

        `release` is invoked on the search thread through its event loop so the
        private document is closed by the thread that opened it, which keeps
        the one-thread-at-a-time rule true right to the end.
        """
        self._owner = None
        if not self._thread.isRunning():
            return
        self._state.bump()
        self._pending_term = None
        # A queued invocation rather than another signal: there is one call and
        # one receiver, and it has to run on the search thread before quit().
        QMetaObject.invokeMethod(self._worker, "release",
                                 Qt.ConnectionType.QueuedConnection)
        self._thread.quit()
        self._thread.wait(timeout_ms)

    # -- results, on the GUI thread ------------------------------------

    @Slot(int, int, int)
    def _on_progress(self, generation, done, total):
        if not self._state.is_current(generation):
            return
        self.progressed.emit(done, total)

    @Slot(int, str, object)
    def _on_results(self, generation, term, hits):
        """The second half of supersession, and the reason it is belt and braces.

        The worker stops between pages, so a stale query usually emits nothing
        at all. It can still have got as far as emitting when the newer query
        arrived, and that signal is already queued by then. This is where such
        a result is dropped rather than painted over a newer one.
        """
        if not self._state.is_current(generation):
            return
        import fitz

        self._pending_term = None
        self.results_ready.emit(term,
                                [(pn, fitz.Rect(*box)) for pn, box in hits])

    @Slot(int, str, str)
    def _on_failed(self, generation, message, details):
        if not self._state.is_current(generation):
            return
        self._pending_term = None
        self.failed.emit(message, details)


def process_events():
    """Let the GUI draw once. Used by tests to prove it still can."""
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
