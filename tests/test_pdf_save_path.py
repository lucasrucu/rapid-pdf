"""The PDF write path: signatures, highlights, and the methods nobody tested.

WHY THIS FILE EXISTS. An audit found that `write_annotations`,
`clone_with_annotations`, `write_annotation_model`, `delete_tagged_annotations`,
`strip_dangling_toc` and `search_text` had no direct test reference anywhere in
the suite. The longest and most load-bearing method in the save path was the
least verified thing in the repo, and three real bugs were living in it:

1. Every save was a full rewrite (`garbage=4, deflate=True`), so opening a
   signed RFCC, nudging one annotation and pressing Ctrl+S silently voided the
   signature on the certificate that then got sent on. Nothing said a word.
2. A note typed into a HIGHLIGHT was written to the embedded JSON model and not
   to the PDF annotation, so it came back in rapid-pdf and did not exist in
   Acrobat. It looked saved. It was not.
3. Highlights were baked as filled Square annotations, which every other reader
   draws as an opaque box over the text rather than as a highlight.

The tests below pin all three, plus the round trip that has to keep working
either side of the change: a document written by an OLDER rapid-pdf carries
Square-based highlights, and it still has to open, edit and delete.

THE SIGNED FIXTURE IS SYNTHETIC, AND THAT IS SAID OUT LOUD. Nothing in this
repo can produce a real cryptographic signature (PyMuPDF cannot sign, and no
signing library is a dependency), so `signed_pdf` builds a signature WIDGET and
attaches a signature dictionary to its /V by xref surgery. That is exactly what
`is_signed()` looks at, so the detection path is covered honestly; what is not
covered is whether a real certificate chain still verifies afterwards, which
would need a signing library and a trust store. The incremental-write property
the verification depends on IS covered, by asserting the original bytes survive
the save untouched as a prefix of the new file.
"""

import os

import fitz
import pytest

from core.pdf_document import (
    MODEL_EMBED_NAME,
    RAPID_PDF_TAG,
    SAVE_MODE_INCREMENTAL,
    SAVE_MODE_REWRITE,
    PDFDocument,
)

HIGHLIGHT_SUBTYPE = "Highlight"
SQUARE_SUBTYPE = "Square"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _build(path, pages=2, sign=None, rotation=0):
    """A small PDF, optionally carrying a signature field.

    `sign` is None (no signature field at all), False (a field sitting there
    unsigned, which is what a blank RFCC form looks like) or True (a field with
    a signature dictionary in its /V, which is what a signed one looks like).
    """
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=400, height=500)
        if rotation:
            page.set_rotation(rotation)
        page.insert_text((20, 100), f"page {i} widget test", fontsize=18)
    if sign is not None:
        field = fitz.Widget()
        field.field_type = fitz.PDF_WIDGET_TYPE_SIGNATURE
        field.field_name = "CommissioningEngineer"
        field.rect = fitz.Rect(50, 300, 200, 350)
        doc[0].add_widget(field)
    doc.save(str(path))
    doc.close()

    if sign:
        # A signature dictionary, attached the only way this repo can attach
        # one. `is_signed()` reads the field's /V and nothing deeper.
        doc = fitz.open(str(path))
        widget = next(iter(doc[0].widgets()))
        sig_xref = doc.get_new_xref()
        doc.update_object(
            sig_xref,
            "<< /Type /Sig /Filter /Adobe.PPKLite /SubFilter /adbe.pkcs7.detached "
            "/M (D:20260101000000Z) /Name (Test Signer) >>",
        )
        doc.xref_set_key(widget.xref, "V", f"{sig_xref} 0 R")
        doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        doc.close()
    return str(path)


@pytest.fixture
def plain_pdf(tmp_path):
    return _build(tmp_path / "plain.pdf")


@pytest.fixture
def unsigned_form_pdf(tmp_path):
    """A signature FIELD with nothing in it. A blank certificate form."""
    return _build(tmp_path / "blank_form.pdf", sign=False)


@pytest.fixture
def signed_pdf(tmp_path):
    return _build(tmp_path / "rfcc.pdf", sign=True)


def opened(path) -> PDFDocument:
    doc = PDFDocument()
    assert doc.open(path), doc.last_open_error
    return doc


def highlight(rect, text=None, color=(1.0, 1.0, 0.0), opacity=0.4):
    ann = {"type": "highlight", "fitz_rect": fitz.Rect(rect),
           "color": color, "opacity": opacity}
    if text:
        ann["text"] = text
    return ann


def annots_of(path, page_num=0):
    doc = fitz.open(path)
    try:
        return [
            {"subtype": a.type[1], "title": a.info.get("title"),
             "content": a.info.get("content"), "rect": fitz.Rect(a.rect)}
            for a in doc[page_num].annots()
        ]
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Bug 1: signature detection
# ---------------------------------------------------------------------------

def test_a_plain_pdf_is_not_signed(plain_pdf):
    doc = opened(plain_pdf)
    assert doc.is_signed() is False
    assert doc.signature_names() == []
    doc.close()


def test_an_empty_signature_field_is_not_a_signature(unsigned_form_pdf):
    """A blank RFCC form is not a signed document, whatever sigflags says.

    PyMuPDF sets the AcroForm /SigFlags to 3 the moment a signature WIDGET is
    added, signed or not, so a detector built on get_sigflags() alone would
    call every blank certificate form signed, push it down the incremental
    path and warn about breaking a signature that does not exist.
    """
    raw = fitz.open(unsigned_form_pdf)
    assert raw.get_sigflags() == 3        # the trap, measured, not assumed
    raw.close()

    doc = opened(unsigned_form_pdf)
    assert doc.is_signed() is False
    doc.close()


def test_a_signed_field_is_detected_and_named(signed_pdf):
    doc = opened(signed_pdf)
    assert doc.is_signed() is True
    assert doc.signature_names() == ["CommissioningEngineer"]
    doc.close()


# ---------------------------------------------------------------------------
# Bug 1: the save plan
# ---------------------------------------------------------------------------

def test_unsigned_documents_still_plan_a_plain_rewrite(plain_pdf):
    doc = opened(plain_pdf)
    plan = doc.save_plan()
    assert plan.mode == SAVE_MODE_REWRITE
    assert plan.signed is False
    assert plan.breaks_signature is False
    assert plan.reason is None
    doc.close()


def test_a_signed_document_saved_in_place_plans_an_incremental_write(signed_pdf):
    doc = opened(signed_pdf)
    plan = doc.save_plan()
    assert plan.mode == SAVE_MODE_INCREMENTAL
    assert plan.signed is True
    assert plan.breaks_signature is False
    assert plan.needs_confirmation is False
    doc.close()


def test_save_as_on_a_signed_document_is_flagged_as_breaking_it(signed_pdf, tmp_path):
    doc = opened(signed_pdf)
    plan = doc.save_plan(str(tmp_path / "elsewhere.pdf"))
    assert plan.mode == SAVE_MODE_REWRITE
    assert plan.breaks_signature is True
    assert plan.needs_confirmation is True
    assert "CommissioningEngineer" in plan.reason
    assert "signed" in plan.reason.lower()
    doc.close()


def test_deleting_a_page_from_a_signed_document_is_flagged(signed_pdf):
    doc = opened(signed_pdf)
    doc.delete_page(1)
    plan = doc.save_plan()
    # Still an append, because the bytes can still be preserved, but no reader
    # accepts a signature over a page tree that moved underneath it.
    assert plan.mode == SAVE_MODE_INCREMENTAL
    assert plan.breaks_signature is True
    assert "reorder" in plan.reason or "removed" in plan.reason
    doc.close()


def test_a_saved_signed_document_stops_warning_about_pages(signed_pdf):
    doc = opened(signed_pdf)
    doc.delete_page(1)
    assert doc.save_plan().breaks_signature is True
    assert doc.save(allow_signature_break=True) is True
    # The file on disk now has the page tree the document has, so there is
    # nothing left for the NEXT save to warn about.
    assert doc.save_plan().breaks_signature is False
    doc.close()


def test_a_combined_document_has_no_file_to_append_to(signed_pdf, tmp_path):
    """Combine hands over an in-memory document. It can only be written whole."""
    merged = fitz.open()
    merged.insert_pdf(fitz.open(signed_pdf))
    doc = PDFDocument()
    doc.adopt(merged)
    plan = doc.save_plan(str(tmp_path / "combined.pdf"))
    assert plan.mode == SAVE_MODE_REWRITE
    assert plan.breaks_signature is True
    assert "Combine" in plan.reason
    doc.close()


# ---------------------------------------------------------------------------
# Bug 1: what save() actually does
# ---------------------------------------------------------------------------

def test_saving_a_signed_document_appends_and_leaves_the_original_bytes_alone(signed_pdf):
    """THE WHOLE POINT. The bytes that were signed must still be there.

    A full rewrite produces a different file and the signature over the old
    byte range is meaningless. An incremental write appends, so every byte the
    signature covered is still at the offset it was covered at. Asserting the
    old file is a literal PREFIX of the new one is the strongest statement of
    that available without a certificate chain to verify.
    """
    before = open(signed_pdf, "rb").read()

    doc = opened(signed_pdf)
    doc.write_annotations(0, [highlight((30, 60, 200, 90), text="checked")])
    assert doc.save() is True
    assert doc.last_save_error is None
    doc.close()

    after = open(signed_pdf, "rb").read()
    assert len(after) > len(before)
    assert after.startswith(before), "the save rewrote the file instead of appending"

    reopened = fitz.open(signed_pdf)
    assert reopened.get_sigflags() == 3
    widget = next(iter(reopened[0].widgets()))
    assert reopened.xref_get_key(widget.xref, "V")[0] == "xref"
    assert [a.type[1] for a in reopened[0].annots()] == [HIGHLIGHT_SUBTYPE]
    reopened.close()


def test_an_unsigned_save_is_still_a_full_rewrite(plain_pdf):
    """No behaviour change where there is nothing at stake: still compacted."""
    before = open(plain_pdf, "rb").read()
    doc = opened(plain_pdf)
    doc.write_annotations(0, [highlight((30, 60, 200, 90))])
    assert doc.save() is True
    doc.close()
    after = open(plain_pdf, "rb").read()
    assert not after.startswith(before), "an unsigned save should rebuild the file"


def test_save_as_on_a_signed_document_refuses_until_it_is_told_to_go_ahead(
        signed_pdf, tmp_path):
    """Neither silently proceeds nor silently refuses."""
    target = tmp_path / "copy.pdf"
    doc = opened(signed_pdf)
    doc.write_annotations(0, [highlight((30, 60, 200, 90))])

    assert doc.save(str(target)) is False
    assert doc.last_save_blocked_by_signature is True
    assert "CommissioningEngineer" in doc.last_save_error
    assert not target.exists(), "nothing may be written on a refusal"
    assert doc.path == signed_pdf, "and the document must not have moved"

    assert doc.save(str(target), allow_signature_break=True) is True
    assert doc.last_save_blocked_by_signature is False
    assert target.exists()
    assert doc.path == str(target)
    doc.close()


def test_a_real_save_failure_is_not_mistaken_for_a_signature_refusal(plain_pdf):
    doc = opened(plain_pdf)
    doc.close()
    assert doc.save() is False
    assert doc.last_save_blocked_by_signature is False
    assert doc.last_save_error == "There is no document to save."


def test_a_signed_document_can_be_saved_twice_in_a_row(signed_pdf):
    """The incremental path does not close and reopen, so it has to stay live."""
    doc = opened(signed_pdf)
    doc.write_annotations(0, [highlight((30, 60, 200, 90), text="one")])
    assert doc.save() is True
    # Still readable through the same handle: the incremental path deliberately
    # does not close and reopen the way the in-place rewrite has to.
    # (A fitz pixmap, not render_page, which needs a QGuiApplication.)
    assert doc.doc[0].get_pixmap(dpi=36).width > 0
    doc.delete_tagged_annotations(0)
    doc.write_annotations(0, [highlight((30, 60, 200, 90), text="two")])
    assert doc.save() is True
    doc.close()

    contents = [a["content"] for a in annots_of(signed_pdf)]
    assert contents == ["two"]


def test_an_ocr_rebuilt_document_cannot_be_appended_to(signed_pdf):
    """replace_from_bytes keeps the path but drops the file behind it.

    Everything that looks like an in-place save still says yes here (`path` is
    unchanged, and PyMuPDF's own can_save_incrementally() returns true for an
    in-memory document), and PyMuPDF would then raise "incremental needs
    original file". The plan has to catch that before the write.
    """
    doc = opened(signed_pdf)
    rebuilt = fitz.open(signed_pdf)
    payload = rebuilt.tobytes()
    rebuilt.close()
    assert doc.replace_from_bytes(payload) is True
    assert doc.path == signed_pdf

    plan = doc.save_plan()
    assert plan.mode == SAVE_MODE_REWRITE
    assert plan.breaks_signature is True
    assert doc.save() is False
    assert doc.last_save_blocked_by_signature is True
    assert doc.save(allow_signature_break=True) is True
    doc.close()


# ---------------------------------------------------------------------------
# Bugs 2 and 3: highlights
# ---------------------------------------------------------------------------

def test_a_highlight_is_written_as_real_text_markup(plain_pdf):
    doc = opened(plain_pdf)
    doc.write_annotations(0, [highlight((50, 50, 150, 100))])
    assert doc.save() is True
    doc.close()

    written = annots_of(plain_pdf)
    assert [a["subtype"] for a in written] == [HIGHLIGHT_SUBTYPE]
    assert written[0]["title"] == RAPID_PDF_TAG


def test_highlight_text_reaches_the_pdf_and_not_only_the_sidecar(plain_pdf):
    """Bug 2. Read back by a plain PyMuPDF that knows nothing about the model.

    The typed note used to live only in the embedded JSON, so it survived a
    reopen here and vanished in every other reader. This asserts on the
    annotation's own /Contents, with the sidecar deliberately not consulted.
    """
    doc = opened(plain_pdf)
    doc.write_annotations(0, [highlight((50, 50, 150, 100), text="valve tagged 4100")])
    assert doc.save() is True
    doc.close()

    written = annots_of(plain_pdf)
    assert written[0]["content"] == "valve tagged 4100"


def test_a_rect_annotation_still_carries_its_text(plain_pdf):
    """The branch that was already right, pinned so it stays right."""
    doc = opened(plain_pdf)
    doc.write_annotations(0, [{
        "type": "rect", "fitz_rect": fitz.Rect(50, 50, 150, 100),
        "color": (1.0, 0.0, 0.0), "text": "rect note",
    }])
    assert doc.save() is True
    doc.close()
    written = annots_of(plain_pdf)
    assert written[0]["subtype"] == SQUARE_SUBTYPE
    assert written[0]["content"] == "rect note"


def test_a_highlight_lands_centred_on_the_rect_it_was_drawn_at(plain_pdf):
    """Geometry has to stay put, and MuPDF pads a highlight's /Rect.

    add_highlight_annot gives the annotation a marker-pen appearance whose
    overhang is scaled off the quad height (about h/16 vertically and h*0.2357
    horizontally on 1.27.2.3), so /Rect comes back BIGGER than the quad asked
    for. What must not move is the centre, because that is where the user drew
    it. The overhang is checked to be symmetric and small rather than pinned to
    a magic number, since it is a MuPDF appearance detail and not a contract.
    """
    want = fitz.Rect(50, 50, 150, 100)
    doc = opened(plain_pdf)
    doc.write_annotations(0, [highlight(want)])
    # Hold the Page. An Annot read after its page object has been released is
    # an access violation, not an exception; `doc[0].annots()` in a throwaway
    # expression frees the page out from under the annotation.
    page = doc.doc[0]
    got = fitz.Rect(next(iter(page.annots())).rect)
    doc.close()

    assert got.x0 + got.x1 == pytest.approx(want.x0 + want.x1, abs=0.01)
    assert got.y0 + got.y1 == pytest.approx(want.y0 + want.y1, abs=0.01)
    assert got.contains(want)
    assert got.width - want.width < want.height        # overhang stays modest
    assert got.height - want.height < want.height / 4


def test_a_highlight_paints_where_the_old_square_painted(plain_pdf):
    """The rendered result, not just the coordinates.

    Renders the same box both ways and compares the painted region. The centre
    has to be identical; the highlight's extent differs by the marker-pen
    overhang, and the ways it differs are asserted rather than waved at.
    """
    def painted(build):
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        build(page)
        pix = page.get_pixmap(dpi=72)
        xs, ys = [], []
        for y in range(pix.height):
            for x in range(pix.width):
                if pix.pixel(x, y) != (255, 255, 255):
                    xs.append(x)
                    ys.append(y)
        doc.close()
        return min(xs), min(ys), max(xs), max(ys)

    box = fitz.Rect(50, 50, 150, 100)

    def old_square(page):
        a = page.add_rect_annot(box)
        a.set_colors(fill=(1, 1, 0), stroke=(1, 1, 0))
        a.set_border(width=0)
        a.update()

    def new_highlight(page):
        writer = PDFDocument()
        writer.doc = page.parent
        try:
            writer.write_annotations(0, [highlight(box, opacity=1.0)])
        finally:
            writer.doc = None

    sx0, sy0, sx1, sy1 = painted(old_square)
    hx0, hy0, hx1, hy1 = painted(new_highlight)

    assert (sx0 + sx1) / 2 == pytest.approx((hx0 + hx1) / 2, abs=0.5)
    assert (sy0 + sy1) / 2 == pytest.approx((hy0 + hy1) / 2, abs=0.5)
    assert abs((hy1 - hy0) - (sy1 - sy0)) <= 2          # same band of the page
    assert 0 < (hx1 - hx0) - (sx1 - sx0) <= 20         # rounded caps, both ends


def test_a_highlight_on_a_rotated_page_does_not_balloon(tmp_path):
    """Why the quad is built from corners rather than from the derotated rect.

    Hand add_highlight_annot the derotated BOUNDING BOX of a wide, short box on
    a 90-degree page and MuPDF reads the long side as the text height, padding
    sideways by ~0.24 of it: a 190x30 box comes back four times too wide.
    Mapping the four corners individually keeps the quad's own orientation, so
    the overhang follows the box the user drew.
    """
    path = _build(tmp_path / "rotated.pdf", pages=1, rotation=90)
    want = fitz.Rect(30, 80, 220, 110)          # visible space, wide and short

    doc = opened(path)
    doc.write_annotations(0, [highlight(want)])
    page = doc.doc[0]                       # held; see the centring test above
    got = fitz.Rect(next(iter(page.annots())).rect)

    # Where it should be in native space, for comparison.
    native = (fitz.Rect(want) * page.derotation_matrix).normalize()
    doc.close()

    assert got.x0 + got.x1 == pytest.approx(native.x0 + native.x1, abs=0.01)
    assert got.y0 + got.y1 == pytest.approx(native.y0 + native.y1, abs=0.01)
    # native is 30 wide. The old bounding-box quad gave ~120 here.
    assert got.width < native.width * 1.5


def test_highlight_colour_and_opacity_survive_the_write(plain_pdf):
    doc = opened(plain_pdf)
    doc.write_annotations(0, [highlight((50, 50, 150, 100),
                                        color=(0.0, 1.0, 0.0), opacity=0.3)])
    assert doc.save() is True
    doc.close()

    raw = fitz.open(plain_pdf)
    page = raw[0]                            # held; see the centring test above
    seen = 0
    for a in page.annots():
        # A text-markup annotation's colour is /C, reported by PyMuPDF as stroke.
        assert a.colors["stroke"] == pytest.approx([0.0, 1.0, 0.0])
        assert a.opacity == pytest.approx(0.3, abs=0.001)
        seen += 1
    raw.close()
    assert seen == 1


def test_a_degenerate_highlight_is_skipped_not_crashed(plain_pdf):
    doc = opened(plain_pdf)
    doc.write_annotations(0, [
        highlight((50, 50, 50, 50)),                       # empty
        {"type": "highlight", "fitz_rect": None},          # no rect at all
        highlight((50, 50, 150, 100)),                     # the good one
    ])
    assert len(list(doc.doc[0].annots())) == 1
    doc.close()


# ---------------------------------------------------------------------------
# Backward compatibility with files older rapid-pdf versions wrote
# ---------------------------------------------------------------------------

def legacy_square_highlight_pdf(path, note="legacy note"):
    """A file exactly as rapid-pdf 1.9.0 and earlier wrote it.

    A filled Square annotation tagged rapid-pdf, plus the embedded JSON model
    that the app rebuilds its editable items from. Note the model has never
    recorded an annotation SUBTYPE, only `"type": "highlight"`, which is the
    reason changing the subtype cannot break an old file.
    """
    doc = fitz.open()
    page = doc.new_page(width=400, height=500)
    page.insert_text((20, 100), "old document", fontsize=18)
    a = page.add_rect_annot(fitz.Rect(50, 50, 150, 100))
    a.set_colors(fill=(1, 1, 0), stroke=(1, 1, 0))
    a.set_opacity(0.4)
    a.set_border(width=0)
    info = a.info
    info["title"] = RAPID_PDF_TAG
    a.set_info(info)
    a.update()
    doc.embfile_add(MODEL_EMBED_NAME, (
        '{"version": 1, "pages": {"0": [{"type": "highlight", '
        '"rect": [50.0, 50.0, 150.0, 100.0], "color": [1.0, 1.0, 0.0], '
        '"opacity": 0.4, "text": "' + note + '"}]}}'
    ).encode("utf-8"))
    doc.save(str(path))
    doc.close()
    return str(path)


def test_an_old_square_highlight_still_opens_and_reads_its_model(tmp_path):
    path = legacy_square_highlight_pdf(tmp_path / "old.pdf")
    doc = opened(path)
    assert [a["subtype"] for a in annots_of(path)] == [SQUARE_SUBTYPE]
    model = doc.read_annotation_model()
    assert model["pages"]["0"][0]["type"] == "highlight"
    assert model["pages"]["0"][0]["text"] == "legacy note"
    doc.close()


def test_an_old_square_highlight_is_still_stripped_on_open(tmp_path):
    """delete_tagged_annotations matches on the TAG, never on the subtype.

    This is the whole backward-compatibility argument. If it had matched on
    Square, the change would have left every old highlight baked into the page
    AND rebuilt as an editable item, so every one of them would double up.
    """
    path = legacy_square_highlight_pdf(tmp_path / "old.pdf")
    doc = opened(path)
    doc.delete_tagged_annotations(0)
    assert list(doc.doc[0].annots()) == []
    doc.close()


def test_an_old_square_highlight_is_re_baked_as_real_markup(tmp_path):
    """Open, strip, re-bake: the same highlight comes back as text markup.

    The full editing round trip an old file goes through the first time it is
    opened and saved in the new version, with the canvas step done by hand.
    """
    path = legacy_square_highlight_pdf(tmp_path / "old.pdf")
    doc = opened(path)
    model = doc.read_annotation_model()
    doc.delete_tagged_annotations(0)

    item = model["pages"]["0"][0]
    doc.write_annotations(0, [highlight(item["rect"], text=item["text"],
                                        color=tuple(item["color"]),
                                        opacity=item["opacity"])])
    doc.write_annotation_model(model)
    assert doc.save() is True
    doc.close()

    written = annots_of(path)
    assert [a["subtype"] for a in written] == [HIGHLIGHT_SUBTYPE]
    assert written[0]["content"] == "legacy note"
    assert written[0]["title"] == RAPID_PDF_TAG

    again = opened(path)
    assert again.read_annotation_model()["pages"]["0"][0]["text"] == "legacy note"
    again.close()


# ---------------------------------------------------------------------------
# The rest of the untested write path
# ---------------------------------------------------------------------------

def test_write_annotations_replaces_only_our_own_tagged_markup(plain_pdf):
    doc = opened(plain_pdf)
    foreign = doc.doc[0].add_rect_annot(fitz.Rect(200, 200, 300, 300))
    info = foreign.info
    info["title"] = "Some Other Reviewer"
    foreign.set_info(info)
    foreign.update()

    doc.write_annotations(0, [highlight((50, 50, 150, 100))])
    doc.write_annotations(0, [highlight((60, 60, 160, 110))])
    titles = sorted(a.info.get("title") for a in doc.doc[0].annots())
    assert titles == ["Some Other Reviewer", RAPID_PDF_TAG]
    doc.close()


def test_write_annotations_bakes_every_supported_type(plain_pdf):
    doc = opened(plain_pdf)
    doc.write_annotations(0, [
        highlight((30, 30, 120, 60)),
        {"type": "rect", "fitz_rect": fitz.Rect(30, 70, 120, 100),
         "stroke_color": (1, 0, 0), "line_width": 2},
        {"type": "line", "p1": fitz.Point(30, 120), "p2": fitz.Point(120, 120),
         "color": (0, 0, 1), "line_width": 3},
        {"type": "text", "fitz_rect": fitz.Rect(30, 140, 200, 180),
         "text": "note", "font_size": 11},
    ])
    subtypes = sorted(a.type[1] for a in doc.doc[0].annots())
    assert subtypes == ["FreeText", "Highlight", "Line", "Square"]
    doc.close()


def test_the_annotation_model_round_trips_through_the_file(plain_pdf):
    doc = opened(plain_pdf)
    model = {"version": 1, "pages": {"0": [{"type": "highlight",
                                            "rect": [10.0, 10.0, 50.0, 30.0]}]}}
    doc.write_annotation_model(model)
    assert doc.save() is True
    doc.close()

    reopened = opened(plain_pdf)
    assert reopened.read_annotation_model() == model
    reopened.close()


def test_rewriting_the_model_leaves_exactly_one_copy(plain_pdf):
    """A stale second copy would let an OLD set of annotations win on reopen."""
    doc = opened(plain_pdf)
    for n in range(3):
        doc.write_annotation_model({"version": 1, "pages": {"0": [{"n": n}]}})
    assert doc.doc.embfile_names().count(MODEL_EMBED_NAME) == 1
    assert doc.read_annotation_model()["pages"]["0"][0]["n"] == 2
    doc.close()


def test_clone_with_annotations_leaves_the_live_document_alone(plain_pdf):
    doc = opened(plain_pdf)
    clone = doc.clone_with_annotations({0: [highlight((50, 50, 150, 100))]})
    try:
        assert len(clone) == doc.page_count()
        assert [a.type[1] for a in clone[0].annots()] == [HIGHLIGHT_SUBTYPE]
        assert list(doc.doc[0].annots()) == [], "the live document must not be marked"
    finally:
        clone.close()
    assert doc.is_open(), "closing the clone must not close the document"
    doc.close()


def test_strip_dangling_toc_drops_the_bookmark_a_deleted_page_left_behind(plain_pdf):
    doc = opened(plain_pdf)
    doc.doc.set_toc([[1, "One", 1], [1, "Two", 2]])
    doc.doc.delete_page(1)          # not doc.delete_page: that strips already
    assert any(e[2] <= 0 for e in doc.doc.get_toc(simple=True))
    assert doc.strip_dangling_toc() == 1
    assert doc.doc.get_toc(simple=True) == [[1, "One", 1]]
    assert doc.strip_dangling_toc() == 0
    doc.close()


def test_search_text_finds_hits_on_every_page(plain_pdf):
    doc = opened(plain_pdf)
    hits = doc.search_text("widget test")
    assert [pn for pn, _ in hits] == [0, 1]
    assert all(isinstance(r, fitz.Rect) and not r.is_empty for _, r in hits)
    assert doc.search_text("") == []
    assert doc.search_text("nothing here at all") == []
    doc.close()
