"""Where a search hit gets PAINTED, which is not where PyMuPDF reports it.

`page.search_for` answers in the page's UNROTATED user space, in points, at
every rotation: rotate a page 90 degrees and the rect for the same word comes
back byte for byte identical. The canvas draws a pixmap rendered WITH /Rotate
applied. So `rect * zoom` is only right by accident on an unrotated page, and
on a sideways scan (which is most of what gets opened here) it puts the
highlight nowhere near the text it matched. The mapping is
`page.rotation_matrix * Matrix(zoom, zoom)`, the same one page-text selection
and embedded-image hit-testing already use.

GROUND TRUTH IS PIXELS, NOT A SECOND COPY OF THE FORMULA. Each rotation case
renders the page twice, once with the searched word and once with that one word
left out, and diffs the two rasters. The pixels that differ ARE the word's ink,
located by the renderer rather than by any arithmetic in this repo. The
highlight has to cover them. Hardcoded expected rectangles would have agreed
with the bug, since the bug was a wrong docstring being believed.

Drop `rotation_matrix` from `set_search_hits` and rotation 0 still passes while
90, 180 and 270 fail. That is the bug, and it is why these are parametrized.
"""

import fitz
import pytest
from PySide6.QtWidgets import QApplication

from core.pdf_document import PDFDocument
from ui.canvas import PDFCanvas


# Each word is stamped at its own x, on a shared baseline, so that leaving one
# out does not shift the others. That is what makes the two-render diff below
# isolate exactly one word.
LINE_ONE_Y = 100.0
LINE_TWO_Y = 140.0
LINE_ONE = (("PUMP", 50.0), ("4100-PU-001", 94.3), ("SUCTION", 181.5))
LINE_TWO = (("VALVE", 50.0), ("4100-HV-002", 99.0), ("OPEN", 186.2))

TARGET = "4100-PU-001"

#: Maps a grey byte to 0 (ink) or 255 (paper), so a rendered row can be searched
#: with bytes.find instead of a Python loop over every pixel.
_INK = bytes(0 if v < 128 else 255 for v in range(256))


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


def _tag_pdf(tmp_path, name, rotation=0, omit=None):
    """A page of tag-shaped text, optionally with one word left out.

    `omit` is the word to skip. Everything else lands in exactly the same place
    either way, which is the whole point: the difference between the two renders
    is the omitted word and nothing else.
    """
    raw = fitz.open()
    page = raw.new_page(width=400, height=500)
    for word, x in LINE_ONE:
        if word != omit:
            page.insert_text((x, LINE_ONE_Y), word, fontsize=14)
    for word, x in LINE_TWO:
        if word != omit:
            page.insert_text((x, LINE_TWO_Y), word, fontsize=14)
    if rotation:
        page.set_rotation(rotation)
    path = tmp_path / name
    raw.save(str(path))
    raw.close()
    return str(path)


def _canvas_for(path, scale):
    """A canvas on that file at a forced raster scale.

    The scale is normally picked once from the page geometry and memoised
    forever; setting the memo before the canvas reads it is how a test chooses
    the number, and it is the only way (there is deliberately no setter).
    """
    doc = PDFDocument()
    assert doc.open(path)
    doc._render_scale = scale
    canvas = PDFCanvas()
    canvas.resize(1400, 1700)
    canvas.set_document(doc)
    canvas._flush_pending_render()   # the debounced render never lands on its own
    return canvas, doc


def _pixmap(path, rotation, scale):
    """Render page 0 of `path` at `scale`. Rotation comes from the file."""
    doc = fitz.open(path)
    try:
        page = doc[0]
        assert page.rotation == rotation
        return page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    finally:
        doc.close()


def _ink_bbox(pix):
    """Bounding box of the dark pixels in a rendered page, in pixmap pixels."""
    n, w, stride, s = pix.n, pix.width, pix.stride, pix.samples
    x0 = y0 = None
    x1 = y1 = -1
    for y in range(pix.height):
        row = s[y * stride: y * stride + w * n].translate(_INK)
        first = row.find(b"\x00")
        if first < 0:
            continue
        last = row.rfind(b"\x00")
        x0 = first // n if x0 is None else min(x0, first // n)
        x1 = max(x1, last // n)
        y0 = y if y0 is None else y0
        y1 = y
    assert x0 is not None, "the fixture page rendered blank"
    return x0, y0, x1 + 1, y1 + 1


def _diff_bbox(pix_a, pix_b):
    """Bounding box of the pixels where two same-sized renders disagree.

    With the two fixtures differing by one stamped word, this is that word's
    ink, found by the renderer and not by any coordinate maths under test.
    """
    assert (pix_a.width, pix_a.height, pix_a.n) == (pix_b.width, pix_b.height,
                                                    pix_b.n)
    n, w, stride = pix_a.n, pix_a.width, pix_a.stride
    a, b = pix_a.samples, pix_b.samples
    x0 = y0 = None
    x1 = y1 = -1
    for y in range(pix_a.height):
        lo = y * stride
        ra = a[lo: lo + w * n]
        rb = b[lo: lo + w * n]
        if ra == rb:
            continue
        first = next(i for i in range(len(ra)) if ra[i] != rb[i])
        last = next(i for i in range(len(ra) - 1, -1, -1) if ra[i] != rb[i])
        x0 = first // n if x0 is None else min(x0, first // n)
        x1 = max(x1, last // n)
        y0 = y if y0 is None else y0
        y1 = y
    assert x0 is not None, "the two fixtures rendered identically"
    return x0, y0, x1 + 1, y1 + 1


def _target_ink(tmp_path, rotation, scale, stem):
    """Where the searched word's ink actually is, in scene (pixmap) pixels."""
    full = _tag_pdf(tmp_path, f"{stem}-full.pdf", rotation=rotation)
    less = _tag_pdf(tmp_path, f"{stem}-less.pdf", rotation=rotation, omit=TARGET)
    return full, _diff_bbox(_pixmap(full, rotation, scale),
                            _pixmap(less, rotation, scale))


def _only_hit_rect(canvas, doc, term=TARGET):
    """Paint the document's hits for `term` and hand back the one scene rect."""
    hits = doc.search_text(term)
    assert len(hits) == 1, f"fixture should carry {term} exactly once"
    page, rect = hits[0]
    assert page == 0
    canvas.set_search_hits([rect], 0)
    assert len(canvas._search_items) == 1
    return canvas._search_items[0].rect()


def _assert_covers_ink(box, ink):
    """The highlight has to sit on the ink, and not by swallowing the page."""
    x0, y0, x1, y1 = ink
    pad = 1.0
    assert box.left() <= x0 + pad, f"highlight starts right of the ink: {box} vs {ink}"
    assert box.top() <= y0 + pad, f"highlight starts below the ink: {box} vs {ink}"
    assert box.right() >= x1 - pad, f"highlight ends left of the ink: {box} vs {ink}"
    assert box.bottom() >= y1 - pad, f"highlight ends above the ink: {box} vs {ink}"
    ink_area = (x1 - x0) * (y1 - y0)
    assert box.width() * box.height() < ink_area * 3.5, (
        f"highlight is far bigger than the text it marks: {box} vs {ink}")


# ---------------------------------------------------------------------------
# The contract search_text actually offers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_search_text_answers_in_unrotated_user_space(tmp_path, rotation):
    """The same word gives the same rect however the page is turned.

    This is the fact the old docstring got wrong, so it is written down as a
    test rather than left as a comment. If a future PyMuPDF starts answering in
    displayed space instead, this fails and the canvas transform has to change
    with it.
    """
    doc = PDFDocument()
    assert doc.open(_tag_pdf(tmp_path, f"space{rotation}.pdf", rotation=rotation))
    try:
        hits = doc.search_text(TARGET)
        assert len(hits) == 1
        rect = hits[0][1]
        # The unrotated page is 400x500; at 90 and 270 the DISPLAYED page is
        # 500x400, so a displayed-space answer could not equal this one.
        assert rect.x0 == pytest.approx(94.3, abs=0.5)
        assert rect.y0 == pytest.approx(84.9, abs=0.5)
        assert rect.x1 == pytest.approx(177.6, abs=0.5)
        assert rect.y1 == pytest.approx(104.2, abs=0.5)
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Where the highlight lands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("scale", [1.5, 3.0])
def test_a_search_hit_lands_on_its_text_at_every_rotation(qt_app, tmp_path,
                                                          rotation, scale):
    """A file that arrived from disk already rotated, searched and highlighted.

    Both halves of the transform are exercised: the scales are two rungs of the
    raster ladder, so a fix that folded the rotation in but dropped the zoom
    would fail at 3.0 while passing at 1.5.
    """
    path, ink = _target_ink(tmp_path, rotation, scale, f"disk{rotation}-{scale}")
    canvas, doc = _canvas_for(path, scale)
    try:
        _assert_covers_ink(_only_hit_rect(canvas, doc), ink)
    finally:
        canvas.deleteLater()
        doc.close()


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_a_page_rotated_in_the_app_moves_its_highlight_with_it(qt_app, tmp_path,
                                                               rotation):
    """Turning the page in the editor, not opening one already turned.

    Same write the rotate command makes (`set_rotation` plus a render-cache
    drop), so the highlight has to follow the text round without the file being
    saved and reopened.
    """
    scale = 2.0
    _, ink = _target_ink(tmp_path, rotation, scale, f"app{rotation}")
    path = _tag_pdf(tmp_path, f"upright{rotation}.pdf")
    canvas, doc = _canvas_for(path, scale)
    try:
        doc.doc[0].set_rotation(rotation)
        doc.invalidate_render_page(0)
        canvas.set_page(0, immediate=True)
        _assert_covers_ink(_only_hit_rect(canvas, doc), ink)
    finally:
        canvas.deleteLater()
        doc.close()


def test_the_highlight_marks_the_word_searched_and_not_its_neighbour(qt_app,
                                                                     tmp_path):
    """Covering ink is necessary, not sufficient: it has to be the RIGHT ink.

    On a 180 page the wrong transform still lands on the page, just mirrored,
    so this pins the hit to its own word and keeps it off the other line.
    """
    scale = 1.5
    path, ink = _target_ink(tmp_path, 180, scale, "which180")
    canvas, doc = _canvas_for(path, scale)
    try:
        box = _only_hit_rect(canvas, doc)
        _assert_covers_ink(box, ink)
        other = doc.search_text("4100-HV-002")
        assert len(other) == 1
        canvas.set_search_hits([other[0][1]], 0)
        neighbour = canvas._search_items[0].rect()
        assert not box.intersects(neighbour), (
            "the two lines' highlights overlap, so neither is on its own text")
    finally:
        canvas.deleteLater()
        doc.close()


def test_hits_on_a_rotated_page_are_cleared_and_replaced_not_stacked(qt_app,
                                                                     tmp_path):
    """Repainting is what a live search does on every keystroke."""
    path = _tag_pdf(tmp_path, "repaint.pdf", rotation=270)
    canvas, doc = _canvas_for(path, 1.5)
    try:
        rects = [r for _, r in doc.search_text("4100")]
        assert len(rects) == 2
        canvas.set_search_hits(rects, 0)
        assert len(canvas._search_items) == 2
        canvas.set_search_hits(rects[:1], 0)
        assert len(canvas._search_items) == 1
        canvas.set_search_hits([], -1)
        assert canvas._search_items == []
    finally:
        canvas.deleteLater()
        doc.close()
