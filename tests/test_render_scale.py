"""The raster scale a document is drawn at: chosen by page size, then frozen.

Four things are being pinned down here, and they are separate claims:

1. THE RULE. A small page is sharpened, a large-format drawing is not. That is
   a decision about cost, not about taste, and core/render_scale.py carries the
   measurements behind it. The tests state the outcome for each real page size
   so a change to the budget shows up as a named page changing rung rather than
   as a float moving.

2. THE LADDER. The answer is always one of three values. A budget divided by an
   area is a continuous number and it would have been easy to return it, which
   would give every oddly-sized page its own coordinate space, its own render
   cache keys, and its own rounding behaviour in the annotation model.

3. THE OVERRIDE. An explicit setting beats the rule outright and is not
   re-checked against the budget, because the whole point of it is to force
   sharpness onto a drawing the rule would leave alone.

4. THE FREEZE. Once a document has answered, it answers the same forever, and
   a settings change does not reach back into an open document. Annotations are
   stored in scene coordinates, which are pixels at the raster scale, so a
   scale that moved under a live document would move the markup with it.
"""

import os
import sys

import fitz
import pytest
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pdf_document import PDFDocument
from core.render_scale import (
    AUTO, MEGAPIXEL_BUDGET, RENDER_SCALE_CHOICES, RENDER_SCALE_LADDER,
    choose_render_scale, resolve_setting, scale_for_page,
)
from core.settings import DEFAULTS, Settings, set_settings

# PDF points, portrait. The sizes the app actually opens: A4 is the scanned
# document or datasheet whose small text started all this, A1 is the
# engineering drawing that dominates the cost table.
A4 = (595, 842)
LETTER = (612, 792)
A3 = (842, 1191)
A2 = (1191, 1684)
A1 = (1684, 2384)
A0 = (2384, 3370)


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    """A settings store of its own, so a test cannot read the real one."""
    settings = Settings(os.path.join(str(tmp_path), "settings.json"),
                        debounce_ms=0, migrate_legacy=False)
    set_settings(settings)
    yield settings
    set_settings(None)


def make_doc(tmp_path, size, pages=2, name="doc.pdf"):
    path = os.path.join(str(tmp_path), name)
    raw = fitz.open()
    for i in range(pages):
        page = raw.new_page(width=size[0], height=size[1])
        page.insert_text((40, 120), f"page {i}", fontsize=9)
    raw.save(path)
    raw.close()
    doc = PDFDocument()
    assert doc.open(path)
    return doc


# ---------------------------------------------------------------------------
# 1. The rule
# ---------------------------------------------------------------------------

def test_an_a4_page_is_sharpened_to_the_top_of_the_ladder():
    """The page this whole change exists for. 4.5 Mpx, 15 ms, well inside budget."""
    assert scale_for_page(*A4) == 3.0


def test_letter_is_sharpened_too():
    """Not every small page is A4, and the rule is about area, not paper names."""
    assert scale_for_page(*LETTER) == 3.0


def test_an_a1_drawing_stays_exactly_where_it_is_today():
    """The load-bearing half of the decision.

    An A1 at 1.5 is already 9 Mpx and 34.5 MB, which is more than an A4 costs
    at the top of the ladder. Sharpening it costs 4x the memory and over 3x the
    time on the GUI thread, where that time is a frozen window.
    """
    assert scale_for_page(*A1) == 1.5


def test_a_page_bigger_than_a1_still_gets_the_floor_rather_than_less():
    """A0 is over budget at every rung, and there is nothing below 1.5.

    The budget is only ever allowed to move a document UP. Returning something
    smaller for a page too big to fit would make this change a regression for
    the largest drawings, which are the ones least able to afford one.
    """
    assert scale_for_page(*A0) == RENDER_SCALE_LADDER[0] == 1.5


def test_the_middle_sizes_land_on_the_middle_rung():
    """A3 takes the intermediate step; A2 is already drawing-sized."""
    assert scale_for_page(*A3) == 2.0
    assert scale_for_page(*A2) == 1.5


def test_a_landscape_page_is_judged_the_same_as_a_portrait_one():
    """The budget is on AREA. `page.bound()` reports the rotated size, so a
    landscape A1 arrives here with its sides swapped and must not change rung."""
    for size in (A4, A3, A1):
        assert scale_for_page(size[1], size[0]) == scale_for_page(*size)


def test_no_page_is_ever_rendered_over_the_budget_unless_the_floor_forces_it():
    """The property behind the table above, stated once.

    Either the chosen scale fits the megapixel ceiling, or it is the floor and
    nothing smaller was available.
    """
    for size in (A4, LETTER, A3, A2, A1, A0):
        scale = scale_for_page(*size)
        megapixels = (size[0] * scale) * (size[1] * scale) / 1e6
        assert megapixels <= MEGAPIXEL_BUDGET or scale == RENDER_SCALE_LADDER[0]


def test_a_nonsense_page_size_falls_back_to_the_shipped_scale():
    """This runs on the open path. It degrades, it does not raise."""
    assert scale_for_page(0, 0) == 1.5
    assert scale_for_page(-10, 500) == 1.5


# ---------------------------------------------------------------------------
# 2. The ladder
# ---------------------------------------------------------------------------

def test_the_answer_is_always_a_rung_and_never_an_arbitrary_float():
    """Swept across two decades of page size, including the awkward middle.

    A budget divided by an area is continuous, and returning that number
    directly would give each odd page size its own raster scale, its own render
    cache keys and its own annotation coordinate space.
    """
    for width in range(50, 4001, 37):
        for height in (width, int(width * 1.414), int(width * 0.5) + 1):
            assert scale_for_page(width, height) in RENDER_SCALE_LADDER


def test_the_ladder_is_ordered_and_starts_at_the_scale_that_shipped():
    assert list(RENDER_SCALE_LADDER) == sorted(RENDER_SCALE_LADDER)
    assert RENDER_SCALE_LADDER[0] == 1.5


def test_every_rung_is_a_legal_setting_and_so_is_auto():
    """The dropdown and the rule cannot drift apart: the choices ARE the ladder."""
    assert RENDER_SCALE_CHOICES[0] == AUTO
    assert set(RENDER_SCALE_CHOICES[1:]) == {f"{s:g}" for s in RENDER_SCALE_LADDER}
    assert DEFAULTS["view"]["render_scale"] == AUTO


# ---------------------------------------------------------------------------
# 3. The override
# ---------------------------------------------------------------------------

def test_an_explicit_setting_beats_the_automatic_choice():
    """Forcing an A1 to Sharpest is the manual override, and it is not
    second-guessed against the budget. Somebody who chooses it has decided to
    pay the render time to read the drawing."""
    assert scale_for_page(*A1) == 1.5
    assert choose_render_scale(*A1, page_count=1, setting="3") == 3.0


def test_an_explicit_setting_can_also_hold_a_small_page_back():
    """The override runs both ways. Standard on an A4 is the old behaviour."""
    assert choose_render_scale(*A4, page_count=1, setting="1.5") == 1.5


def test_auto_and_nonsense_both_fall_through_to_the_rule():
    """A hand-edited or newer settings file must not stop a document opening."""
    assert resolve_setting(AUTO) is None
    assert resolve_setting("banana") is None
    assert resolve_setting(None) is None
    assert resolve_setting("2.5") is None      # a real float, but not a rung
    assert choose_render_scale(*A4, page_count=1, setting="banana") == 3.0


def test_page_count_is_only_ever_a_secondary_guard():
    """It pulls the answer down and never pushes it up.

    A long document is read by paging through it, and the render cache holds
    six entries however long it is, so nearly every turn pays a full render.
    What it must never do is override the page-size rule upward: a 500-page set
    of A1 drawings is still A1.
    """
    assert scale_for_page(*A4, page_count=500) == 2.0
    assert scale_for_page(*A1, page_count=500) == 1.5
    assert scale_for_page(*A4, page_count=10) == 3.0


# ---------------------------------------------------------------------------
# 4. Frozen for the life of the document
# ---------------------------------------------------------------------------

def test_a_document_picks_its_scale_from_its_own_pages(store, tmp_path):
    assert make_doc(tmp_path, A4, name="small.pdf").render_scale() == 3.0
    assert make_doc(tmp_path, A1, name="big.pdf").render_scale() == 1.5


def test_the_setting_is_read_when_the_document_opens(store, tmp_path):
    store.view.render_scale = "1.5"
    assert make_doc(tmp_path, A4, name="held.pdf").render_scale() == 1.5


def test_changing_the_setting_does_not_move_an_open_document(store, tmp_path):
    """THE POINT OF THE MEMO, not an artefact of it.

    Annotations live in scene space, which is pixels at the raster scale, and
    serialise through `to_annotation_dict(self._zoom)`. A scale that changed
    under a live document would silently move every mark on it.
    """
    doc = make_doc(tmp_path, A4)
    assert doc.render_scale() == 3.0
    store.view.render_scale = "1.5"
    assert doc.render_scale() == 3.0
    assert doc.render_scale() == 3.0      # and not on the second ask either


def test_reopening_is_what_applies_a_changed_setting(store, tmp_path):
    """The promise the Preferences hint makes: new tabs, not open ones."""
    doc = make_doc(tmp_path, A4)
    assert doc.render_scale() == 3.0
    store.view.render_scale = "1.5"
    assert doc.open(doc.path)
    assert doc.render_scale() == 1.5


def test_a_save_does_not_move_the_scale(store, tmp_path):
    """A save reopens the file in place. The markup on it was drawn against
    the scale that was chosen when it was opened, so that scale has to survive
    the round trip even though the document object is holding a new fitz
    handle afterwards."""
    doc = make_doc(tmp_path, A4)
    assert doc.render_scale() == 3.0
    store.view.render_scale = "1.5"
    assert doc.save()
    assert doc.render_scale() == 3.0


def test_a_merged_document_decides_again(store, tmp_path):
    """`adopt` hands the object a genuinely different document, of a size it
    has no reason to share, so the old answer must not be kept."""
    doc = make_doc(tmp_path, A4)
    assert doc.render_scale() == 3.0
    merged = fitz.open()
    merged.new_page(width=A1[0], height=A1[1])
    doc.adopt(merged)
    assert doc.render_scale() == 1.5


# ---------------------------------------------------------------------------
# What the canvas does with it, and what it leaves alone
# ---------------------------------------------------------------------------

def test_the_canvas_rasterises_at_the_documents_chosen_scale(qt_app, store,
                                                             tmp_path):
    from ui.canvas import PDFCanvas

    canvas = PDFCanvas()
    canvas.set_document(make_doc(tmp_path, A4, name="sharp.pdf"))
    assert canvas.raster_scale() == 3.0

    canvas.set_document(make_doc(tmp_path, A1, name="wide.pdf"))
    assert canvas.raster_scale() == 1.5


def test_the_scene_is_the_page_at_that_scale(qt_app, store, tmp_path):
    """What sharper actually means: more pixels, for the same page.

    This is the assertion that would have caught the original bug. Before this
    change the scene was 1.5x the page whatever the page was, so there were no
    more pixels to zoom into and the view transform was all that ever moved.
    """
    from ui.canvas import PDFCanvas

    canvas = PDFCanvas()
    canvas.set_document(make_doc(tmp_path, A4, name="sharp.pdf"))
    scene = canvas.scene().sceneRect()
    assert scene.width() == pytest.approx(A4[0] * 3.0, abs=2)
    assert scene.height() == pytest.approx(A4[1] * 3.0, abs=2)


def test_fit_page_puts_the_same_picture_on_screen_at_either_scale(qt_app, store,
                                                                  tmp_path):
    """Existing fit behaviour is UNCHANGED, which is not automatic.

    Every fit mode computes its transform from the scene rect, and the scene
    rect grows with the raster scale, so the view scale a fit lands on is
    different at 3.0 than at 1.5. What has to match is the product, which is
    how much of the page ends up across the viewport. If these two disagreed,
    sharpening a document would have silently changed how big it looks.
    """
    from ui.canvas import PDFCanvas

    def on_screen(setting):
        store.view.render_scale = setting
        canvas = PDFCanvas()
        canvas.resize(800, 600)
        canvas.set_document(make_doc(tmp_path, A4, name=f"fit{setting}.pdf"))
        canvas.fit_page()
        return canvas.view_scale() * canvas.raster_scale()

    assert on_screen("3") == pytest.approx(on_screen("1.5"), rel=0.01)


def test_actual_size_still_means_one_point_to_one_pixel(qt_app, store, tmp_path):
    """`actual_size` divides the raster scale back out, so it has to track it."""
    from ui.canvas import PDFCanvas

    canvas = PDFCanvas()
    canvas.resize(800, 600)
    canvas.set_document(make_doc(tmp_path, A4, name="actual.pdf"))
    canvas.actual_size()
    assert canvas.view_scale() * canvas.raster_scale() == pytest.approx(1.0,
                                                                       rel=0.01)
