"""The Split dialog itself, built and run.

WHY THIS FILE IS HERE AT ALL. An audit of the suite found `ui/combine_dialog.py`
is never imported or instantiated by any test in the repo: every path that
reaches Combine is monkeypatched away, so 363 lines of real dialog are verified
by nothing at all. Split is not going the same way. The dialog below is
constructed for real, filled in through the same methods its widgets drive, and
its `run()` writes real files to a real tmp_path.

Nothing here needs a modal loop. `exec()` is never called: the two questions
that would block (which folder, and whether to replace an existing file) are a
line edit and a callback parameter, which is what makes the dialog drivable and
would have made Combine drivable too.
"""

import os

import fitz
import pytest

from PySide6.QtWidgets import QApplication

from core.pdf_document import PDFDocument
from ui.split_dialog import (
    MODE_AT,
    MODE_EVERY,
    MODE_EXTRACT,
    SplitDialog,
    format_page_spec,
    parse_page_spec,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QApplication.instance() or QApplication([])


def _build(path, pages=10):
    doc = fitz.open()
    for index in range(pages):
        doc.new_page().insert_text((72, 100), f"PAGE-{index}")
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture
def pdf(tmp_path):
    document = PDFDocument()
    document.open(_build(tmp_path / "bundle.pdf", pages=10))
    yield document
    document.close()


def _text_of(path):
    doc = fitz.open(path)
    out = [page.get_text().strip() for page in doc]
    doc.close()
    return out


# ---------------------------------------------------------------------------
# The page spec: one-based in, zero-based out
# ---------------------------------------------------------------------------

def test_page_spec_reads_the_way_a_person_writes_it():
    assert parse_page_spec("1-3", 10) == [0, 1, 2]
    assert parse_page_spec("1-3, 7", 10) == [0, 1, 2, 6]
    assert parse_page_spec("7", 10) == [6]
    assert parse_page_spec("8-", 10) == [7, 8, 9], "an open end means the rest"
    assert parse_page_spec("-3", 10) == [0, 1, 2], "an open start means from the top"


def test_page_spec_keeps_the_order_and_the_repeats():
    assert parse_page_spec("5, 1", 10) == [4, 0]
    assert parse_page_spec("2, 2", 10) == [1, 1]


def test_page_spec_drops_what_it_cannot_use_rather_than_guessing():
    assert parse_page_spec("1-99", 10) == list(range(10))
    assert parse_page_spec("40", 10) == []
    assert parse_page_spec("", 10) == []
    assert parse_page_spec("banana, 2", 10) == [1]
    assert parse_page_spec("1-3", 0) == []


def test_page_spec_tolerates_a_backwards_range():
    assert parse_page_spec("5-2", 10) == [1, 2, 3, 4]


def test_format_page_spec_is_the_way_back():
    assert format_page_spec([0, 1, 2, 6]) == "1-3, 7"
    assert format_page_spec([4]) == "5"
    assert format_page_spec([]) == ""


# ---------------------------------------------------------------------------
# The dialog, built for real
# ---------------------------------------------------------------------------

def test_it_opens_on_extract_with_the_whole_document(pdf, tmp_path):
    dialog = SplitDialog(pdf)
    assert dialog.mode() == MODE_EXTRACT
    plans = dialog.plans()
    assert len(plans) == 1 and plans[0].page_count == 10
    dialog.deleteLater()


def test_a_selection_prefills_the_extract_box(pdf):
    dialog = SplitDialog(pdf, selected_pages=[2, 3, 4, 8])
    plans = dialog.plans()
    assert plans[0].pages == (2, 3, 4, 8)
    assert os.path.basename(plans[0].path) == "bundle_p3-9-selection.pdf"
    dialog.deleteLater()


def test_extract_through_the_dialog_writes_one_file(pdf, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    dialog = SplitDialog(pdf, selected_pages=[1, 2])
    dialog.set_out_dir(str(out))
    report = dialog.run()
    assert len(report.written) == 1
    assert _text_of(report.written[0]) == ["PAGE-1", "PAGE-2"]
    assert pdf.page_count() == 10, "the dialog must not touch the source"
    assert "Wrote 1 file" in dialog.summary_line()
    dialog.deleteLater()


def test_split_every_n_through_the_dialog(pdf, tmp_path):
    out = tmp_path / "every"
    out.mkdir()
    dialog = SplitDialog(pdf)
    dialog.set_mode(MODE_EVERY)
    dialog.set_every(3)
    dialog.set_out_dir(str(out))
    plans = dialog.plans()
    assert [p.page_count for p in plans] == [3, 3, 3, 1]
    report = dialog.run()
    assert len(report.written) == 4
    assert _text_of(report.written[-1]) == ["PAGE-9"]
    assert sorted(os.listdir(out)) == [
        "bundle_part01.pdf", "bundle_part02.pdf",
        "bundle_part03.pdf", "bundle_part04.pdf"]
    dialog.deleteLater()


def test_split_at_cut_points_through_the_dialog(pdf, tmp_path):
    out = tmp_path / "at"
    out.mkdir()
    dialog = SplitDialog(pdf)
    dialog.set_mode(MODE_AT)
    dialog.set_cuts_text("4, 8")     # one-based: a new file starts at page 4
    dialog.set_out_dir(str(out))
    report = dialog.run()
    assert [len(_text_of(p)) for p in report.written] == [3, 4, 3]
    assert _text_of(report.written[1])[0] == "PAGE-3"
    dialog.deleteLater()


def test_the_preview_matches_what_would_be_written(pdf, tmp_path):
    dialog = SplitDialog(pdf)
    dialog.set_mode(MODE_EVERY)
    dialog.set_every(4)
    dialog.set_out_dir(str(tmp_path))
    dialog.refresh()
    rows = [dialog._preview.item(i).text()
            for i in range(dialog._preview.count())]
    assert len(rows) == 3
    assert "bundle_part01.pdf" in rows[0] and "pages 1 to 4" in rows[0]
    assert "pages 9 to 10" in rows[2]
    dialog.deleteLater()


def test_nothing_selected_disables_the_button(pdf, tmp_path):
    dialog = SplitDialog(pdf)
    dialog.set_pages_text("")
    assert dialog.plans() == []
    assert dialog._go.isEnabled() is False
    assert dialog.run() is None
    dialog.deleteLater()


# ---------------------------------------------------------------------------
# Overwriting is always a question
# ---------------------------------------------------------------------------

def test_an_existing_file_is_never_replaced_without_an_answer(pdf, tmp_path):
    out = tmp_path / "clash"
    out.mkdir()
    (out / "bundle_part01.pdf").write_bytes(b"already here")

    dialog = SplitDialog(pdf)
    dialog.set_mode(MODE_EVERY)
    dialog.set_every(5)
    dialog.set_out_dir(str(out))
    assert [p.exists for p in dialog.plans()] == [True, False]

    asked = []

    def say_no(paths):
        asked.append(list(paths))
        return False

    report = dialog.run(confirm_overwrite=say_no)
    assert asked and os.path.basename(asked[0][0]) == "bundle_part01.pdf"
    assert len(report.skipped) == 1 and len(report.written) == 1
    assert (out / "bundle_part01.pdf").read_bytes() == b"already here"
    assert "left alone" in dialog.summary_line()
    dialog.deleteLater()


def test_saying_yes_replaces_it(pdf, tmp_path):
    out = tmp_path / "clash2"
    out.mkdir()
    (out / "bundle_part01.pdf").write_bytes(b"already here")

    dialog = SplitDialog(pdf)
    dialog.set_mode(MODE_EVERY)
    dialog.set_every(5)
    dialog.set_out_dir(str(out))
    report = dialog.run(confirm_overwrite=lambda paths: True)
    assert len(report.written) == 2 and report.skipped == ()
    assert _text_of(str(out / "bundle_part01.pdf"))[0] == "PAGE-0"
    dialog.deleteLater()


def test_no_callback_means_never_overwrite(pdf, tmp_path):
    out = tmp_path / "clash3"
    out.mkdir()
    (out / "bundle_part01.pdf").write_bytes(b"already here")
    dialog = SplitDialog(pdf)
    dialog.set_mode(MODE_EVERY)
    dialog.set_every(5)
    dialog.set_out_dir(str(out))
    report = dialog.run()
    assert len(report.skipped) == 1
    assert (out / "bundle_part01.pdf").read_bytes() == b"already here"
    dialog.deleteLater()


def test_an_extract_steps_aside_rather_than_clashing(pdf, tmp_path):
    out = tmp_path / "extract"
    out.mkdir()
    dialog = SplitDialog(pdf, selected_pages=[0, 1])
    dialog.set_out_dir(str(out))
    first = dialog.run()
    assert os.path.basename(first.written[0]) == "bundle_p1-2.pdf"

    second_dialog = SplitDialog(pdf, selected_pages=[0, 1])
    second_dialog.set_out_dir(str(out))
    second = second_dialog.run()
    assert os.path.basename(second.written[0]) == "bundle_p1-2 (2).pdf"
    assert sorted(os.listdir(out)) == ["bundle_p1-2 (2).pdf", "bundle_p1-2.pdf"]
    dialog.deleteLater()
    second_dialog.deleteLater()


# ---------------------------------------------------------------------------
# The warning banner
# ---------------------------------------------------------------------------

def test_a_signed_document_shows_the_warning_in_the_dialog(tmp_path):
    path = str(tmp_path / "signed.pdf")
    doc = fitz.open()
    for index in range(4):
        doc.new_page().insert_text((72, 100), f"PAGE-{index}")
    widget = fitz.Widget()
    widget.field_type = fitz.PDF_WIDGET_TYPE_SIGNATURE
    widget.field_name = "Engineer"
    widget.rect = fitz.Rect(300, 700, 500, 740)
    annot = doc[0].add_widget(widget)
    sig = doc.get_new_xref()
    doc.update_object(sig, "<< /Type /Sig /Filter /Adobe.PPKLite "
                           "/SubFilter /adbe.pkcs7.detached >>")
    doc.xref_set_key(annot.xref, "V", f"{sig} 0 R")
    doc.save(path)
    doc.close()

    pdf = PDFDocument()
    pdf.open(path)
    dialog = SplitDialog(pdf)
    assert dialog._warning_label.isVisible() or dialog._warning_label.text()
    assert "NOT be signed" in dialog._warning_label.text()
    dialog.deleteLater()
    pdf.close()


def test_an_ordinary_document_shows_no_warning_banner(pdf):
    dialog = SplitDialog(pdf)
    assert dialog._warning_label.text() == ""
    assert dialog._warning_label.isVisible() is False
    dialog.deleteLater()
