"""The two memory-safety defects that were not the tear-off crash.

CRASH 2, THE SHARED DOCUMENT. `OCRWorker` used to be handed the LIVE
`PDFDocument` and drove it from a QThread: `page_count`, `page_has_text`, and
`ocr_page`, which does `insert_pdf` and then `delete_page`. The UI thread went
on rendering the same document into the canvas and the panel thumbnails at the
same time. PyMuPDF documents are not thread safe and MuPDF has no lock covering
this, so two threads in one page tree is a segfault waiting for the right
interleaving. The worker now opens its OWN document from bytes and hands the
finished file back as bytes.

CRASH 3, THE UNBOUND BUFFER. `_render_page_at_zoom` built a QImage over
`bytes(pix.samples)`, an unnamed temporary. QImage does not copy the buffer it
is given, so the image pointed at freed memory for the one line that mattered,
the `QPixmap.fromImage` that does the real copy.

WHAT THESE TESTS CAN DO. Both defects are reachable without a display, unlike
the tear-off crash, so these are ordinary tests rather than a source rule plus
a manual harness. What they cannot do is fail RELIABLY on the old code by
crashing: a use-after-free usually reads intact memory, and a data race usually
interleaves harmlessly. So they assert on the STRUCTURE that makes the bug
impossible rather than waiting to catch it misbehaving, which is the only
honest way to test for this class of thing in a suite that has to pass every
time.
"""

import ast
import pathlib

import fitz
import pytest

from PySide6.QtWidgets import QApplication

from core.ocr_worker import OCRWorker, run_ocr_enhance
from core.pdf_document import PDFDocument

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


def a_text_pdf(path, text="Already searchable"):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 144), text, fontsize=18)
    doc.save(str(path))
    doc.close()
    return str(path)


# ----------------------------------------------------------------------
# Crash 2: the worker never sees the live document
# ----------------------------------------------------------------------

def test_the_worker_is_built_from_bytes_not_from_a_document():
    """The constructor is the guarantee. If it cannot be handed a document,
    it cannot drive one from the wrong thread."""
    import inspect

    names = list(inspect.signature(OCRWorker.__init__).parameters)
    assert names[1] == "source_bytes", (
        "OCRWorker's first argument is what decides whether the worker thread "
        f"can reach the UI thread's document. Got {names}"
    )


def test_running_the_worker_leaves_the_live_document_alone(qt_app, tmp_path):
    """Run the worker body and check the source document was not touched.

    Deliberately a document that already HAS text, so no page needs OCR and
    the test costs nothing: what is being asserted is ownership, not OCR.
    """
    path = a_text_pdf(tmp_path / "text.pdf")
    live = PDFDocument()
    assert live.open(path)
    original = live.doc
    before = live.text_layer_report()

    seen = []
    worker = OCRWorker(live.doc.tobytes())
    worker.finished.connect(lambda count, payload: seen.append((count, payload)))
    worker.run()

    assert seen == [(0, None)], (
        "a fully searchable document needs no OCR, so nothing should come back"
    )
    assert live.doc is original, "the live document object was swapped underneath"
    assert live.text_layer_report() == before
    live.close()


def test_the_worker_mutates_only_its_own_copy(qt_app, tmp_path):
    """The private document is opened, changed and closed inside `run`.

    Uses a page with no text layer so `ocr_page` actually fires, and asserts
    the LIVE document still has no text afterwards: every mutation landed on
    the worker's copy, and applying it to the live one is the UI thread's job.
    """
    doc = fitz.open()
    page = doc.new_page(width=300, height=150)
    page.insert_text((30, 80), "RASTER ONLY", fontsize=24)
    raster = doc[0].get_pixmap(dpi=150)
    doc.close()

    flat = fitz.open()
    flat_page = flat.new_page(width=300, height=150)
    flat_page.insert_image(fitz.Rect(0, 0, 300, 150), pixmap=raster)
    path = tmp_path / "scanned.pdf"
    flat.save(str(path))
    flat.close()

    live = PDFDocument()
    assert live.open(str(path))
    assert live.text_layer_report() == [0], "precondition: no text layer"

    seen = []
    worker = OCRWorker(live.doc.tobytes())
    worker.finished.connect(lambda count, payload: seen.append((count, payload)))
    worker.run()

    count, payload = seen[0]
    if count == 0:
        pytest.skip("no tessdata on this machine, so OCR did nothing to check")

    assert payload is not None, "work was done but no document came back"
    assert live.text_layer_report() == [0], (
        "the worker changed the LIVE document, which is the data race this "
        "rewrite exists to remove"
    )
    live.close()


def test_replace_from_bytes_keeps_the_path_and_swaps_the_content(tmp_path):
    """The UI thread's half of the handover.

    `adopt` would have been wrong here: it drops the path, which would push the
    next Ctrl+S through Save As on a document that has a perfectly good file.
    """
    path = a_text_pdf(tmp_path / "before.pdf", "BEFORE")
    live = PDFDocument()
    assert live.open(path)

    replacement = fitz.open()
    replacement.new_page().insert_text((72, 144), "AFTER", fontsize=18)
    replacement.new_page()
    payload = replacement.tobytes()
    replacement.close()

    assert live.replace_from_bytes(payload) is True
    assert live.path == path, "the document lost its path and would Save As"
    assert live.page_count() == 2
    assert "AFTER" in live.doc[0].get_text()
    live.close()


def test_replace_from_bytes_refuses_rubbish_without_losing_the_document(tmp_path):
    path = a_text_pdf(tmp_path / "keep.pdf", "KEEP")
    live = PDFDocument()
    assert live.open(path)

    assert live.replace_from_bytes(b"") is False
    assert live.replace_from_bytes(b"not a pdf at all") is False
    assert live.is_open(), "a bad payload closed the document it failed to replace"
    assert "KEEP" in live.doc[0].get_text()
    live.close()


def test_run_ocr_enhance_hands_the_worker_bytes(qt_app, tmp_path, monkeypatch):
    """The wiring, not the worker: whatever `run_ocr_enhance` builds the worker
    with is what crosses the thread boundary."""
    from PySide6.QtWidgets import QWidget

    captured = {}

    class Spy(OCRWorker):
        def __init__(self, source_bytes, *a, **kw):
            captured["arg"] = source_bytes
            super().__init__(source_bytes, *a, **kw)

    monkeypatch.setattr("core.ocr_worker.OCRWorker", Spy)

    live = PDFDocument()
    assert live.open(a_text_pdf(tmp_path / "wire.pdf"))
    parent = QWidget()
    thread, worker = run_ocr_enhance(parent, live, lambda *a: None)
    thread.quit()
    thread.wait(5000)

    assert isinstance(captured["arg"], bytes)
    assert captured["arg"].startswith(b"%PDF"), (
        "the worker was handed something other than a serialised document"
    )
    live.close()


# ----------------------------------------------------------------------
# Crash 3: QImage must never be built over an unnamed temporary
# ----------------------------------------------------------------------

def test_qimage_is_never_constructed_over_a_temporary_buffer():
    """QImage borrows its buffer, so the buffer needs a name that outlives it.

    A source rule, for the same reason as the winId one in
    tests/test_tear_off_crash.py: the defect is a lifetime, and a lifetime bug
    that reads intact freed memory nine times out of ten cannot be caught by
    asserting on behaviour. `QImage(bytes(pix.samples), ...)` and
    `QImage(pix.samples, ...)` are both temporaries; only a plain name is safe.
    """
    source = (REPO / "core" / "pdf_document.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    bad = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "QImage"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Name):
            bad.append((node.lineno, type(first).__name__))

    assert not bad, (
        "QImage does not copy the buffer it is handed. Bind it to a local name "
        "that outlives the QImage instead of passing an expression, or the "
        "image points at freed memory: "
        f"{bad}"
    )


def test_rendering_the_same_page_twice_agrees(tmp_path):
    """A cheap liveness check on the render path after the buffer change.

    Not proof of the fix (freed memory usually still reads correctly), but it
    would catch a bound-buffer change that got the stride or the format wrong.
    """
    path = a_text_pdf(tmp_path / "render.pdf", "RENDER ME")
    doc = PDFDocument()
    assert doc.open(path)

    first = doc.render_page(0, zoom=1.5)
    second = doc.render_page(0, zoom=1.5)

    assert not first.isNull() and not second.isNull()
    assert first.size() == second.size()
    assert first.toImage() == second.toImage()
    doc.close()
