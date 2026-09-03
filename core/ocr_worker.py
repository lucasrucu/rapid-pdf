"""Background OCR worker for the "Enhance for Search…" feature.

Runs PDFDocument.ocr_page() for every page that lacks a text layer, on a
QThread so the UI never blocks. Only pages that page_has_text() reports as
empty are touched, so a document that's already fully searchable finishes
near instantly with nothing to do.

Threading model (the part that bit us in v1.1.0): signals from the worker
must land in slots of a QObject that LIVES ON THE UI THREAD. Connecting a
cross-thread signal to a plain Python closure does not queue to the main
thread, not even with an explicit QueuedConnection. PySide6 has no receiver
QObject to resolve a thread from, so the closure executes on the emitting
(worker) thread. That meant:
  - dialog.setValue()/setLabelText() ran on the worker thread (GUI calls off
    the GUI thread, which crashes or misbehaves depending on the machine),
  - QTimer.singleShot(0, dialog.close) scheduled its timer on the WORKER
    thread's event loop, and the very next line (thread.quit()) stopped that
    loop, so the timer never fired and the progress dialog never closed.
    That is the "stuck at page N of N" hang seen in the field.
_OcrUiController below is a real QObject parented to the UI; its slots are
therefore delivered on the UI thread via Qt's normal auto-queued mechanism.

THE SECOND THING THAT BIT US, AND IT WAS WORSE. Getting the signals right
still left the worker DRIVING THE LIVE fitz.Document. `ocr_page` does
`insert_pdf` and then `delete_page` on it, and `page_has_text`/`page_count`
read it, all on the worker thread, while the UI thread went on rendering the
very same document into the canvas and the panel thumbnails. PyMuPDF documents
are not thread safe: there is no lock inside MuPDF covering this, and two
threads in one document's page tree is a segfault waiting for the right
interleaving. It is a different failure from the signal bug above, it does not
announce itself, and no amount of correct signalling fixes it.

SO THE WORKER OWNS ITS OWN DOCUMENT. `run_ocr_enhance` serialises the live
document to bytes on the UI thread before the thread starts; the worker opens
those bytes as an INDEPENDENT fitz.Document, OCRs that, and hands the finished
file back as bytes. Nothing the worker touches is reachable from the UI
thread, so there is no sharing left to get wrong. The UI thread applies the
result in one step, `PDFDocument.replace_from_bytes`, after the thread is done.

The alternative was to keep one document and marshal every mutation onto the
UI thread with blocking queued signals. That was rejected: `ocr_page`
rasterises a page and runs Tesseract over it, so marshalling it would put the
slow part back on the UI thread and reintroduce exactly the freeze this
QThread exists to prevent. The price of the choice made instead is that the
document is briefly in memory twice, and that the bytes are produced up front
on the UI thread; both are small next to a per-page OCR pass.
"""

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot


class OCRWorker(QObject):
    """Runs on a QThread, over ITS OWN COPY of the document.

    Constructed from the source PDF as BYTES rather than from the live
    PDFDocument, which is the whole point: there is no object in here that the
    UI thread also holds. Talks back to the UI thread only via signals.
    """

    # (page_index_0_based, pages_total), emitted when a page STARTS being
    # checked/OCR'd, so the dialog shows the page currently in progress.
    progress = Signal(int, int)
    # Emitted once, at the end: the count of pages actually OCR'd, and the
    # finished PDF as bytes (None when nothing changed, so the UI thread can
    # skip replacing the document at all).
    finished = Signal(int, object)
    # Emitted if OCR failed on a specific page (non-fatal, the worker keeps
    # going with the remaining pages). Carries the error text so the UI can
    # surface a real reason (e.g. Tesseract language data missing).
    page_error = Signal(int, str)

    def __init__(self, source_bytes: bytes, language: str = "eng", dpi: int = 150):
        super().__init__()
        self._source_bytes = source_bytes
        self._language = language
        self._dpi = dpi
        self._cancelled = False

    def cancel(self):
        """Thread-safe-enough for a plain bool flag: worst case one extra
        page finishes OCR-ing after Cancel is clicked, which is fine."""
        self._cancelled = True

    def run(self):
        """OCR a PRIVATE copy of the document and return it as bytes.

        Every fitz call below is against `private`, which was opened on this
        thread from bytes and is closed on this thread before returning. The
        live document the user is looking at is never touched from here.

        The PDFDocument wrapper is reused rather than reimplemented so that
        `page_has_text` and `ocr_page` stay in one place; `adopt` is the
        documented way to hand it an in-memory fitz document.
        """
        import fitz

        from core.pdf_document import PDFDocument

        ocred = 0
        payload = None
        private = PDFDocument()
        try:
            private.adopt(fitz.open("pdf", self._source_bytes))
            total = private.page_count()
            for page_num in range(total):
                if self._cancelled:
                    break
                self.progress.emit(page_num, total)
                if not private.page_has_text(page_num):
                    try:
                        if private.ocr_page(page_num, language=self._language,
                                            dpi=self._dpi):
                            ocred += 1
                    except Exception as e:
                        self.page_error.emit(page_num, str(e))
            if ocred and private.doc is not None:
                # Cancelling still returns the pages that were finished, which
                # is the behaviour the old in-place worker had.
                payload = private.doc.tobytes()
        except Exception as e:                   # pragma: no cover - defensive
            # A failure opening or serialising the private copy must not take
            # the thread down without the UI ever hearing about it.
            self.page_error.emit(0, str(e))
        finally:
            private.close()
        self.finished.emit(ocred, payload)


class _OcrUiController(QObject):
    """Lives on the UI thread; every slot below is delivered there because
    cross-thread signal connections to a QObject's methods auto-queue."""

    def __init__(self, dialog, worker, thread, on_done, doc, parent=None):
        super().__init__(parent)
        self._dialog = dialog
        self._worker = worker
        self._thread = thread
        self._on_done = on_done
        self._doc = doc           # the LIVE document, only ever touched here
        self._payload = None      # the worker's finished PDF, as bytes
        self._ocred = 0
        self._cancelled = False
        self._finishing = False   # guards the canceled-on-close feedback loop
        self._errors: list[tuple[int, str]] = []

    @Slot(int, int)
    def on_progress(self, page_num: int, total: int):
        self._dialog.setMaximum(max(total, 1))
        self._dialog.setValue(page_num)
        self._dialog.setLabelText(f"Checking page {page_num + 1} of {total}…")

    @Slot(int, str)
    def on_page_error(self, page_num: int, message: str):
        self._errors.append((page_num, message))
        print(f"OCR error on page {page_num + 1}: {message}")

    @Slot()
    def on_cancel(self):
        # QProgressDialog.closeEvent() emits canceled() as part of any close,
        # including the programmatic close below. Without this guard a normal
        # completion would be misreported as user-cancelled.
        if self._finishing:
            return
        self._cancelled = True
        self._worker.cancel()
        self._dialog.setLabelText("Cancelling…")

    @Slot(int, object)
    def on_worker_finished(self, ocred_count: int, payload):
        self._ocred = ocred_count
        self._payload = payload
        self._finishing = True
        self._dialog.close()
        self._thread.quit()

    @Slot()
    def on_thread_finished(self):
        """The one place the OCR result lands on the live document.

        This runs on the UI thread, AFTER the worker thread has finished, so
        the private document the bytes came from is closed and there is no
        moment when two threads are in one fitz document. `replace_from_bytes`
        keeps the path, so the next save still writes where it always would.
        """
        self._dialog.deleteLater()
        applied = self._ocred
        if self._payload is not None:
            if not self._doc.replace_from_bytes(self._payload):
                self._errors.append(
                    (0, self._doc.last_open_error or
                     "The OCR result could not be applied to the document."))
                applied = 0
        self._payload = None
        self._on_done(applied, self._cancelled, list(self._errors))


def run_ocr_enhance(parent_widget, doc, on_done):
    """Kick off the OCR pass on a background thread with a modal progress
    dialog. `on_done(ocred_count, cancelled, errors)` is called on the UI
    thread once the worker finishes or is cancelled; `errors` is a list of
    (page_num, message) for pages whose OCR failed.

    Returns (thread, worker); the caller holds references so neither is
    garbage-collected mid-run.

    The document is serialised HERE, on the UI thread, and only the bytes
    cross into the worker. See the module docstring: the worker never sees the
    live fitz document, because PyMuPDF documents are not thread safe and the
    UI thread carries on rendering this one while OCR runs.
    """
    from PySide6.QtWidgets import QProgressDialog

    source_bytes = doc.doc.tobytes() if doc.doc is not None else b""

    thread = QThread(parent_widget)
    worker = OCRWorker(source_bytes)
    worker.moveToThread(thread)

    dialog = QProgressDialog(
        "Scanning pages for OCR…", "Cancel", 0, max(doc.page_count(), 1), parent_widget
    )
    dialog.setWindowTitle("Enhance for Search (OCR)")
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setValue(0)

    controller = _OcrUiController(dialog, worker, thread, on_done, doc,
                                  parent=parent_widget)

    # worker signals fire on the worker thread; the controller is a QObject on
    # the UI thread, so Qt's AutoConnection queues these to the UI thread.
    thread.started.connect(worker.run)
    worker.progress.connect(controller.on_progress)
    worker.page_error.connect(controller.on_page_error)
    worker.finished.connect(controller.on_worker_finished)
    thread.finished.connect(controller.on_thread_finished)
    thread.finished.connect(worker.deleteLater)
    dialog.canceled.connect(controller.on_cancel)

    thread.start()
    dialog.show()
    return thread, worker
