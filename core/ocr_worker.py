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
"""

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot


class OCRWorker(QObject):
    """Runs on a QThread. Talks back to the UI thread only via signals."""

    # (page_index_0_based, pages_total), emitted when a page STARTS being
    # checked/OCR'd, so the dialog shows the page currently in progress.
    progress = Signal(int, int)
    # Emitted once, at the end, with the count of pages actually OCR'd.
    finished = Signal(int)
    # Emitted if OCR failed on a specific page (non-fatal, the worker keeps
    # going with the remaining pages). Carries the error text so the UI can
    # surface a real reason (e.g. Tesseract language data missing).
    page_error = Signal(int, str)

    def __init__(self, doc, language: str = "eng", dpi: int = 150):
        super().__init__()
        self._doc = doc
        self._language = language
        self._dpi = dpi
        self._cancelled = False

    def cancel(self):
        """Thread-safe-enough for a plain bool flag: worst case one extra
        page finishes OCR-ing after Cancel is clicked, which is fine."""
        self._cancelled = True

    def run(self):
        total = self._doc.page_count()
        ocred = 0
        for page_num in range(total):
            if self._cancelled:
                break
            self.progress.emit(page_num, total)
            if not self._doc.page_has_text(page_num):
                try:
                    if self._doc.ocr_page(page_num, language=self._language, dpi=self._dpi):
                        ocred += 1
                except Exception as e:
                    self.page_error.emit(page_num, str(e))
        self.finished.emit(ocred)


class _OcrUiController(QObject):
    """Lives on the UI thread; every slot below is delivered there because
    cross-thread signal connections to a QObject's methods auto-queue."""

    def __init__(self, dialog, worker, thread, on_done, parent=None):
        super().__init__(parent)
        self._dialog = dialog
        self._worker = worker
        self._thread = thread
        self._on_done = on_done
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

    @Slot(int)
    def on_worker_finished(self, ocred_count: int):
        self._ocred = ocred_count
        self._finishing = True
        self._dialog.close()
        self._thread.quit()

    @Slot()
    def on_thread_finished(self):
        self._dialog.deleteLater()
        self._on_done(self._ocred, self._cancelled, list(self._errors))


def run_ocr_enhance(parent_widget, doc, on_done):
    """Kick off the OCR pass on a background thread with a modal progress
    dialog. `on_done(ocred_count, cancelled, errors)` is called on the UI
    thread once the worker finishes or is cancelled; `errors` is a list of
    (page_num, message) for pages whose OCR failed.

    Returns (thread, worker); the caller holds references so neither is
    garbage-collected mid-run.
    """
    from PySide6.QtWidgets import QProgressDialog

    thread = QThread(parent_widget)
    worker = OCRWorker(doc)
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

    controller = _OcrUiController(dialog, worker, thread, on_done, parent=parent_widget)

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
