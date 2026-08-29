"""A save that cannot overwrite the original, and what the user is told about it.

THE FAILURE. `PDFDocument.save` writes an in-place save to a temp file next to
the target and swaps it over with `os.replace`. When that swap cannot happen,
the finished content is salvaged as `<name>.pdf.bak` so no work is lost.

WHAT WAS WRONG WITH IT. The salvage was silent. `save()` returned False, the
window said "Could not save the PDF", and `self.path` was left naming the
original. So four things disagreed: the live document was the .bak, the title
bar and the tab named the original, the original on disk still held the old
content, and the next Save would write to whichever of them the path said. The
user had every reason to believe their edits were in the file they opened.

Two windows can now hold the same file, which turns a losing swap from exotic
into ordinary, so it is closed from both ends: the .bak is ADOPTED as the
document's path, and the reason is put in `last_save_error` for the window to
show. The tests below pin both halves plus the wording, because the wording IS
the fix: a message that does not name the .bak leaves the user exactly as lost.
"""

import os

import fitz
import pytest

from PySide6.QtWidgets import QApplication, QMessageBox

from core.pdf_document import PDFDocument
from core.settings import Settings, set_settings
from ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    s = Settings(tmp_path / "settings.json", debounce_ms=0, migrate_legacy=False)
    s.close.confirm_multiple_tabs = False
    previous = set_settings(s)
    yield s
    set_settings(previous)


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "drawing.pdf"
    raw = fitz.open()
    for i in range(2):
        page = raw.new_page(width=400, height=500)
        page.insert_text((20, 100), f"page {i}", fontsize=24)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def swap_always_fails(monkeypatch):
    """Make the in-place swap fail the way a locked file makes it fail.

    Only the swap onto the TARGET: the salvage rename onto the .bak has to keep
    working, because the whole point of the .bak is that it is the copy that
    survives. That is what a file open in Acrobat, held by a sync client, or
    open in a second Rapid PDF window actually looks like on Windows.
    """
    real_replace = os.replace
    blocked = []

    def replace(src, dst, *args, **kwargs):
        if str(dst).lower().endswith(".pdf"):
            blocked.append(str(dst))
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    return blocked


def test_a_failed_swap_still_salvages_the_work(pdf_path, swap_always_fails):
    doc = PDFDocument()
    assert doc.open(pdf_path)
    assert doc.save() is False
    assert swap_always_fails, "the swap was never attempted"
    assert os.path.exists(pdf_path + ".bak")
    doc.close()


def test_a_failed_swap_says_why_instead_of_failing_silently(
        pdf_path, swap_always_fails):
    doc = PDFDocument()
    doc.open(pdf_path)
    doc.save()
    message = doc.last_save_error
    assert message, "save() returned False with nothing to tell the user"
    assert pdf_path in message, "the file that could not be written is not named"
    assert pdf_path + ".bak" in message, "the salvaged file is not named"
    doc.close()


def test_the_document_adopts_the_file_that_actually_holds_the_work(
        pdf_path, swap_always_fails):
    """THE DIVERGENCE THIS CLOSES. The live document is the .bak after the
    salvage, so leaving `path` on the original is what makes the app describe a
    file it is not holding."""
    doc = PDFDocument()
    doc.open(pdf_path)
    doc.save()
    assert doc.path == pdf_path + ".bak"
    assert doc.is_open()
    doc.close()


def test_the_original_on_disk_is_left_exactly_as_it_was(
        pdf_path, swap_always_fails):
    before = open(pdf_path, "rb").read()
    doc = PDFDocument()
    doc.open(pdf_path)
    doc.save()
    doc.close()
    assert open(pdf_path, "rb").read() == before


def test_a_successful_save_leaves_no_error_behind(pdf_path):
    doc = PDFDocument()
    doc.open(pdf_path)
    assert doc.save() is True
    assert doc.last_save_error is None
    assert doc.path == pdf_path
    doc.close()


def test_saving_with_nothing_open_says_so(monkeypatch):
    doc = PDFDocument()
    assert doc.save("anywhere.pdf") is False
    assert doc.last_save_error


def test_the_window_shows_the_reason_and_the_tab_follows_the_new_path(
        qt_app, store, pdf_path, swap_always_fails, monkeypatch):
    """End to end. The dialog carries the real reason, and the tab stops
    claiming to be a file it is no longer holding."""
    shown = []
    monkeypatch.setattr(
        QMessageBox, "critical",
        staticmethod(lambda *a, **k: shown.append(a[2] if len(a) > 2 else "")))

    window = MainWindow()
    try:
        window.open_paths([pdf_path])
        assert window.document_area().bar().tabText(0) == "drawing"

        assert window.save_pdf() is False

        assert len(shown) == 1
        assert pdf_path + ".bak" in shown[0]
        assert window.view.document_path() == pdf_path + ".bak"
        assert window.document_area().bar().tabText(0) == "drawing.pdf"
        assert window.document_area().bar().tabToolTip(0) == pdf_path + ".bak"
    finally:
        for view in window.document_area().views():
            view.mark_clean()
        window._force_quit = True
        window.close()
