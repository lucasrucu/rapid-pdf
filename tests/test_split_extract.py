"""Pulling pages OUT: extract, split, and what the new files cannot carry.

Combine existed and its inverse did not, so a document that arrived as one
scanned bundle could only be taken apart by hand. This is the other direction,
and it is also the mechanical half of the commissioning feature that cuts a
combined scan into one file per certificate, which is why the arithmetic
(`every_n_groups`, `groups_at_cuts`) and the writing (`plan_split`,
`run_split`) are plain functions and methods with no dialog anywhere near them.

THE COVERAGE GAP THIS DOES NOT REPEAT. `ui/combine_dialog.py` is 363 lines that
no test in the repo imports: every path that would reach it is monkeypatched
away, so the dialog itself is verified by nothing. `tests/test_split_dialog.py`
builds the real SplitDialog and runs a real split through it.

WHAT IS ASSERTED HERE beyond "it wrote some files":

- the SOURCE IS UNTOUCHED, by page count and by bytes on disk, because an
  extract that quietly edits the document it read from is the failure that
  would cost real work;
- ANNOTATIONS TRAVEL, because `insert_pdf` drops them unless asked and the
  markup on a certificate is the thing being extracted;
- A SIGNED source WARNS, because a signature covers the whole file and cannot
  come with pages copied out of it, and silently producing something that
  looks signed and is not is worse than refusing;
- NOTHING IS OVERWRITTEN by accident, on any of the three paths that write.
"""

import os

import fitz
import pytest

from core.pdf_document import (
    PDFDocument,
    every_n_groups,
    groups_at_cuts,
    page_range_label,
    page_range_suffix,
    sanitise_stem,
    unique_path,
)


def _build(path, pages=10, annotate=(), sign=False):
    """A PDF whose pages say which page they are, so an extract can be checked
    by reading the text back rather than by counting."""
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"PAGE-{index}")
        if index in annotate:
            page.add_highlight_annot(fitz.Rect(70, 90, 200, 110))
    if sign:
        page = doc[0]
        widget = fitz.Widget()
        widget.field_type = fitz.PDF_WIDGET_TYPE_SIGNATURE
        widget.field_name = "CommissioningEngineer"
        widget.rect = fitz.Rect(300, 700, 500, 740)
        annot = page.add_widget(widget)
        # A signature DICTIONARY in /V is what makes the field signed, the same
        # xref surgery tests/test_pdf_save_path.py uses and for the same reason:
        # nothing in this repo can produce a real cryptographic signature.
        sig = doc.get_new_xref()
        doc.update_object(sig, "<< /Type /Sig /Filter /Adobe.PPKLite "
                               "/SubFilter /adbe.pkcs7.detached >>")
        doc.xref_set_key(annot.xref, "V", f"{sig} 0 R")
    doc.save(str(path))
    doc.close()
    return str(path)


def _text_of(path):
    """The PAGE-n marker on every page of `path`, in order."""
    doc = fitz.open(path)
    out = [page.get_text().strip() for page in doc]
    doc.close()
    return out


@pytest.fixture
def source(tmp_path):
    pdf = PDFDocument()
    pdf.open(_build(tmp_path / "source.pdf", pages=10, annotate=(2, 5)))
    yield pdf
    pdf.close()


# ---------------------------------------------------------------------------
# The arithmetic, with no document and no disk
# ---------------------------------------------------------------------------

def test_every_n_groups_splits_evenly_and_keeps_the_remainder():
    assert every_n_groups(10, 4) == [(0, 1, 2, 3), (4, 5, 6, 7), (8, 9)]
    assert every_n_groups(8, 4) == [(0, 1, 2, 3), (4, 5, 6, 7)]
    assert every_n_groups(3, 1) == [(0,), (1,), (2,)]
    assert every_n_groups(3, 99) == [(0, 1, 2)], "N bigger than the document is one file"


def test_every_n_groups_refuses_nonsense_rather_than_looping():
    assert every_n_groups(10, 0) == []
    assert every_n_groups(10, -3) == []
    assert every_n_groups(0, 4) == []


def test_groups_at_cuts_starts_a_new_file_at_each_cut():
    assert groups_at_cuts(10, [4, 7]) == [(0, 1, 2, 3), (4, 5, 6), (7, 8, 9)]


def test_groups_at_cuts_drops_the_cuts_that_mean_nothing():
    # 0 is where the first file starts anyway; 10 and 40 are past the end; the
    # repeat is the same cut twice. None of them may produce an empty file.
    assert groups_at_cuts(10, [0, 4, 4, 10, 40]) == [(0, 1, 2, 3), (4, 5, 6, 7, 8, 9)]
    assert groups_at_cuts(10, []) == [(0, 1, 2, 3, 4, 5, 6, 7, 8, 9)]


def test_page_labels_and_suffixes_count_from_one():
    assert page_range_label([0, 1, 2]) == "pages 1 to 3"
    assert page_range_label([4]) == "page 5"
    assert page_range_label([0, 4]) == "pages 1 and 5"
    assert page_range_suffix([0, 1, 2]) == "p1-3"
    assert page_range_suffix([4]) == "p5"
    assert page_range_suffix([0, 4, 8]) == "p1-9-selection"


def test_sanitise_stem_makes_a_name_windows_accepts():
    assert sanitise_stem('4100-01: RFCC/final?') == "4100-01_ RFCC_final_"
    assert sanitise_stem("") == "document"
    assert sanitise_stem("   ") == "document"


def test_unique_path_steps_aside_rather_than_replacing(tmp_path):
    target = tmp_path / "out.pdf"
    assert unique_path(str(target)) == str(target)
    target.write_bytes(b"x")
    stepped = unique_path(str(target))
    assert stepped == str(tmp_path / "out (2).pdf")
    (tmp_path / "out (2).pdf").write_bytes(b"x")
    assert unique_path(str(target)) == str(tmp_path / "out (3).pdf")


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

def test_extract_writes_exactly_the_pages_asked_for(source, tmp_path):
    out = str(tmp_path / "picked.pdf")
    assert source.write_extract([0, 3, 7], out) is True
    assert _text_of(out) == ["PAGE-0", "PAGE-3", "PAGE-7"]


def test_extract_keeps_the_order_it_was_given(source, tmp_path):
    out = str(tmp_path / "reordered.pdf")
    assert source.write_extract([7, 1, 4], out) is True
    assert _text_of(out) == ["PAGE-7", "PAGE-1", "PAGE-4"], (
        "the caller's order is the answer; sorting it is the undo stash's job")


def test_extract_leaves_the_source_alone(source, tmp_path):
    before_bytes = open(source.path, "rb").read()
    before_pages = source.page_count()
    assert source.write_extract([0, 1], str(tmp_path / "two.pdf")) is True
    assert source.page_count() == before_pages
    assert open(source.path, "rb").read() == before_bytes, (
        "the source file changed on disk during an extract")
    assert _text_of(source.path)[0] == "PAGE-0"


def test_extract_carries_annotations(source, tmp_path):
    out = str(tmp_path / "annotated.pdf")
    assert source.write_extract([2, 5], out) is True
    doc = fitz.open(out)
    assert [len(list(page.annots())) for page in doc] == [1, 1], (
        "insert_pdf drops annotations unless they are asked for by name")
    doc.close()


def test_extract_will_not_write_over_the_document_it_reads(source):
    assert source.write_extract([0], source.path) is False
    assert "taken from" in source.last_split_error


def test_extract_will_not_replace_a_file_without_being_told_to(source, tmp_path):
    out = tmp_path / "existing.pdf"
    out.write_bytes(b"not a pdf, and it must survive")
    assert source.write_extract([0], str(out)) is False
    assert "already exists" in source.last_split_error
    assert out.read_bytes() == b"not a pdf, and it must survive"
    assert source.write_extract([0], str(out), overwrite=True) is True
    assert _text_of(str(out)) == ["PAGE-0"]


def test_extract_refuses_an_empty_selection(source, tmp_path):
    assert source.write_extract([], str(tmp_path / "nothing.pdf")) is False
    assert source.write_extract([99], str(tmp_path / "nothing.pdf")) is False
    assert not (tmp_path / "nothing.pdf").exists()


def test_extract_makes_the_folder_it_was_pointed_at(source, tmp_path):
    out = str(tmp_path / "new" / "deeper" / "one.pdf")
    assert source.write_extract([0], out) is True
    assert os.path.isfile(out)


def test_suggested_name_says_which_pages_and_never_collides(source, tmp_path):
    first = source.suggest_extract_path([0, 1, 2], str(tmp_path))
    assert os.path.basename(first) == "source_p1-3.pdf"
    open(first, "wb").close()
    second = source.suggest_extract_path([0, 1, 2], str(tmp_path))
    assert os.path.basename(second) == "source_p1-3 (2).pdf"


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------

def test_split_every_n_produces_the_right_files_and_distribution(source, tmp_path):
    plans = source.plan_split(every_n_groups(source.page_count(), 4),
                              out_dir=str(tmp_path))
    assert [os.path.basename(p.path) for p in plans] == [
        "source_part01.pdf", "source_part02.pdf", "source_part03.pdf"]
    report = source.run_split(plans)
    assert len(report.written) == 3
    assert report.failed == () and report.skipped == ()
    assert [_text_of(p) for p in report.written] == [
        ["PAGE-0", "PAGE-1", "PAGE-2", "PAGE-3"],
        ["PAGE-4", "PAGE-5", "PAGE-6", "PAGE-7"],
        ["PAGE-8", "PAGE-9"],
    ]
    assert source.page_count() == 10, "splitting must not touch the source"


def test_split_at_cut_points_writes_the_runs_between_them(source, tmp_path):
    plans = source.plan_split(groups_at_cuts(source.page_count(), [3, 8]),
                              out_dir=str(tmp_path))
    report = source.run_split(plans)
    assert [len(_text_of(p)) for p in report.written] == [3, 5, 2]
    assert _text_of(report.written[1])[0] == "PAGE-3"


def test_part_numbers_are_padded_so_they_sort(source, tmp_path):
    plans = source.plan_split(every_n_groups(10, 1), out_dir=str(tmp_path))
    names = [os.path.basename(p.path) for p in plans]
    assert names[0] == "source_part01.pdf" and names[-1] == "source_part10.pdf"
    assert names == sorted(names), "zero padding is what makes Explorer agree"


def test_plan_split_defaults_to_the_documents_own_folder_and_name(source):
    plans = source.plan_split(every_n_groups(source.page_count(), 5))
    assert os.path.dirname(plans[0].path) == os.path.dirname(source.path)
    assert os.path.basename(plans[0].path).startswith("source_part")


def test_plan_split_flags_a_collision_and_run_split_leaves_it_alone(source, tmp_path):
    plans = source.plan_split(every_n_groups(10, 5), out_dir=str(tmp_path))
    sitting_there = plans[0].path
    open(sitting_there, "wb").write(b"already here")
    replanned = source.plan_split(every_n_groups(10, 5), out_dir=str(tmp_path))
    assert replanned[0].exists is True and replanned[1].exists is False

    report = source.run_split(replanned)
    assert report.skipped == (sitting_there,)
    assert len(report.written) == 1
    assert open(sitting_there, "rb").read() == b"already here", (
        "run_split overwrote a file without being told it could")

    report = source.run_split(replanned, overwrite=True)
    assert len(report.written) == 2 and report.skipped == ()
    assert _text_of(sitting_there) == ["PAGE-0", "PAGE-1", "PAGE-2", "PAGE-3", "PAGE-4"]


def test_run_split_carries_on_past_one_bad_target(source, tmp_path):
    plans = source.plan_split(every_n_groups(10, 5), out_dir=str(tmp_path))
    # A DIRECTORY where a file should go, and permission to replace it, so the
    # write is genuinely attempted and genuinely fails. The other file still
    # has to land: a forty certificate split must not stop dead on one.
    os.makedirs(plans[0].path)
    report = source.run_split(plans, overwrite=True)
    assert len(report.written) == 1
    assert len(report.failed) == 1 and report.failed[0][0] == plans[0].path
    assert report.failed[0][1], "a failure has to come with a reason to show"
    assert report.ok is False


def test_plan_split_says_no_to_nothing(source):
    assert source.plan_split([]) == []
    assert source.plan_split([()]) == []


def test_a_plan_can_be_built_by_hand_for_the_commissioning_case(source, tmp_path):
    """The reason the writer takes plans rather than a mode: a caller that
    knows which pages are which certificate names its own files."""
    from core.pdf_document import SplitPlan

    plans = [
        SplitPlan(str(tmp_path / "RFCC-4100-01.pdf"), (0, 1), "pages 1 to 2"),
        SplitPlan(str(tmp_path / "RFCC-4100-02.pdf"), (2, 3, 4), "pages 3 to 5"),
    ]
    report = source.run_split(plans)
    assert len(report.written) == 2
    assert _text_of(str(tmp_path / "RFCC-4100-02.pdf")) == ["PAGE-2", "PAGE-3", "PAGE-4"]


# ---------------------------------------------------------------------------
# What the new files cannot carry
# ---------------------------------------------------------------------------

def test_extracting_from_a_signed_document_warns(tmp_path):
    pdf = PDFDocument()
    pdf.open(_build(tmp_path / "signed.pdf", pages=4, sign=True))
    assert pdf.is_signed() is True
    warnings = pdf.split_warnings()
    assert warnings, "a signed source must say the new files will not be signed"
    assert "CommissioningEngineer" in warnings[0]
    assert "NOT be signed" in warnings[0]

    plans = pdf.plan_split(every_n_groups(4, 2), out_dir=str(tmp_path))
    report = pdf.run_split(plans)
    assert report.warnings == tuple(warnings), (
        "the warning has to reach the caller of the write, not only a dialog")
    assert len(report.written) == 2
    # AND THE FILES REALLY ARE UNSIGNED. Without the /V strip in build_extract
    # this assertion fails: insert_pdf(widgets=True) carries the signature
    # dictionary across, so the extracted page lands in a new file claiming a
    # signature over bytes that no longer exist. Warning about it and then
    # shipping the claim anyway is the silently invalid output this is for.
    check = PDFDocument()
    check.open(report.written[0])
    assert check.is_signed() is False
    assert check.signature_names() == []
    check.close()
    pdf.close()


def test_the_signature_field_survives_the_strip_but_empty(tmp_path):
    """The field is part of the page and belongs on it. Only the claim goes."""
    pdf = PDFDocument()
    pdf.open(_build(tmp_path / "signed.pdf", pages=2, sign=True))
    out = pdf.build_extract([0])
    fields = [w.field_name for page in out for w in page.widgets()]
    assert "CommissioningEngineer" in fields
    kind, _ = out.xref_get_key(
        next(w.xref for page in out for w in page.widgets()), "V")
    assert kind in (None, "null"), f"the signature value came across as {kind}"
    out.close()
    pdf.close()


def test_extracting_from_an_encrypted_document_warns_that_the_copy_is_not(tmp_path):
    path = str(tmp_path / "locked.pdf")
    doc = fitz.open()
    for index in range(4):
        doc.new_page().insert_text((72, 100), f"PAGE-{index}")
    doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw",
             owner_pw="own", permissions=int(fitz.PDF_PERM_PRINT))
    doc.close()

    pdf = PDFDocument()
    pdf.open(path)
    pdf.unlock("pw")
    warnings = pdf.split_warnings()
    assert any("NOT be password protected" in w for w in warnings)

    out = str(tmp_path / "out.pdf")
    assert pdf.write_extract([0, 1], out) is True
    check = fitz.open(out)
    assert not check.needs_pass, "the fixture is wrong if this one is protected"
    check.close()
    pdf.close()


def test_an_ordinary_document_warns_about_nothing(source):
    assert source.split_warnings() == []


def test_build_extract_hands_back_a_document_the_caller_owns(source):
    out = source.build_extract([1, 1, 2])
    assert len(out) == 3, "a page asked for twice is written twice"
    assert out[0].get_text().strip() == "PAGE-1"
    out.close()
    assert source.page_count() == 10
