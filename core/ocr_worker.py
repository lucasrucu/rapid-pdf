"""Background OCR worker for the "Enhance for Search…" feature.

Runs PDFDocument.ocr_page() for every page that lacks a text layer, on a
QThread so the UI never blocks. Only pages that page_has_text() reports as
empty are touched — a document that's already fully searchable finishes near
instantly with nothing to do.
"""

from PySide6.QtCore import QObject, QThread, Signal


class OCRWorker(QObject):
    """Runs on a QThread. Talks back to the UI thread only via signals."""

    # (pages_done, pages_total) — emitted after each page is checked/OCR'd.
    progress = Signal(int, int)
    # Emitted once, at the end, with the count of pages actually OCR'd.
    finished = Signal(int)
    # Emitted if something raised while OCR-ing a specific page (non-fatal —
    # the worker keeps going with the remaining pages).
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
            if not self._doc.page_has_text(page_num):
                try:
                    if self._doc.ocr_page(page_num, language=self._language, dpi=self._dpi):
                        ocred += 1
                except Exception as e:
                    self.page_error.emit(page_num, str(e))
            self.progress.emit(page_num + 1, total)
        self.finished.emit(ocred)


def run_ocr_enhance(parent_widget, doc, on_done):
    """Kick off the OCR pass on a background thread with a modal progress
    dialog. `on_done(ocred_count, cancelled)` is called on the UI thread once
    the worker finishes or is cancelled.

    Returns the QThread (kept alive by the caller holding a reference) so the
    caller can decide what to do afterward (e.g. re-save the document).
    """
    from PySide6.QtWidgets import QProgressDialog
    from PySide6.QtCore import Qt, QTimer

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

    state = {"cancelled": False}

    def on_progress(done, total):
        dialog.setMaximum(max(total, 1))
        dialog.setValue(done)
        dialog.setLabelText(f"Checking page {done} of {total}…")

    def on_page_error(page_num, message):
        print(f"OCR error on page {page_num + 1}: {message}")

    def on_cancel():
        state["cancelled"] = True
        worker.cancel()
        dialog.setLabelText("Cancelling…")

    def on_worker_finished(ocred_count):
        # Defer dialog.close() to the next event-loop iteration rather than
        # calling it synchronously from inside this slot. Closing a
        # WindowModal QProgressDialog while still nested inside delivery of
        # the cross-thread `finished` signal was observed (empirically, via
        # manual testing) to hang indefinitely — QTimer.singleShot(0, ...)
        # lets the current signal delivery unwind first.
        QTimer.singleShot(0, dialog.close)
        # Ask the thread's event loop to stop. Do NOT block here with
        # thread.wait() — calling it synchronously from inside a slot that is
        # itself being delivered through the event loop can stall well past
        # the thread actually finishing (observed empirically: wait() timed
        # out even though the worker's run() had already returned). Instead,
        # defer on_done() to QThread.finished, which Qt fires once the
        # thread's loop has genuinely stopped.
        state["ocred"] = ocred_count
        thread.quit()

    def on_thread_finished():
        on_done(state.get("ocred", 0), state["cancelled"])

    # worker.progress/finished/page_error fire from the worker's own thread.
    # on_progress/on_worker_finished/on_page_error touch GUI widgets (the
    # dialog), which is only legal on the main thread. worker is a QObject
    # moved to `thread`, so connections FROM its signals auto-queue correctly
    # — but the receivers here are plain closures with no thread affinity of
    # their own, so force QueuedConnection explicitly rather than relying on
    # Qt's auto-detection (which caused these to run in-thread and freeze the
    # dialog's paint/event handling during manual testing).
    thread.started.connect(worker.run)
    worker.progress.connect(on_progress, Qt.ConnectionType.QueuedConnection)
    worker.page_error.connect(on_page_error, Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(on_worker_finished, Qt.ConnectionType.QueuedConnection)
    thread.finished.connect(on_thread_finished)
    dialog.canceled.connect(on_cancel)

    thread.start()
    dialog.show()
    return thread, worker
