"""How sharp to rasterise a document, decided once from its page geometry.

WHY THIS EXISTS

Every page in the app was rasterised at a hardcoded 1.5, assigned once in
`PDFCanvas.__init__` and never changed for the life of the process. 1.5 is
about 108 DPI. On a clean vector page that is fine. On a scanned page of small
text it is not, and the reason it cannot be fixed by zooming is structural:
zooming does not re-rasterise anything, it only changes the QGraphicsView's
transform, so zooming in magnifies a 108 DPI bitmap and can never resolve a
single pixel more detail than that bitmap already holds. The canvas says so
itself, in `view_scale`'s docstring. The user zooms, the text gets bigger and
stays just as unreadable, and there is nothing in the UI that helps.

The obvious fix, raise it to 3.0 for everything, is worse than it looks. Raster
cost is quadratic in scale and linear in page area, and rendering happens on
the GUI thread: there is no QThreadPool anywhere in this codebase, so a slower
render is not a spinner, it is a frozen window. Measured with
`tools/measure_render_time.py`:

    page   scale   megapixels   ms     MB
    A4     1.5     1.1          4.5    4.3
    A4     3.0     4.5         15.4   17.2
    A1     1.5     9.0         29.4   34.5
    A1     3.0    36.1         96.2  137.8

An A1 engineering drawing is already eight times an A4's area, so it starts at
the cost an A4 only reaches at the top of the ladder, and taking A1 to 3.0
costs 3.3x more time and 4x more memory on the single most expensive page the
app ever draws. Those synthetic figures are a FLOOR, incidentally: a real
drawing carries thousands of vector strokes that this measurement's text-only
page does not, and docs/performance.md puts a real A1 at 1.5 nearer 120-135 ms.
Scale that by the same 3.3x and a real A1 at 3.0 is most of half a second of
dead window on every page turn.

So the rule is BY PAGE SIZE, not by page count and not by a global switch.
Small pages, which are cheap and are the ones whose text is unreadable, get
sharpened. Large-format drawings, which are expensive and are usually already
legible because their content is vector line work rather than a scan, stay
exactly where they are today. A page-count rule would have got this backwards:
drawings are typically short documents, so "sharpen short documents" would
sharpen precisely the pages that cannot afford it.

THE BUDGET IS IN PIXELS

Pixels are what both time and memory actually track, so the budget is a
megapixel ceiling on the rendered output and the scale is the highest rung of
the ladder that fits under it. 6.0 Mpx is the chosen ceiling and the measured
table is what picks it: an A4 at 3.0 is 4.5 Mpx and clears it comfortably,
while an A3 at 3.0 is 9.0 Mpx and misses it comfortably. Nothing common sits on
the edge, so a page a hair over or under a standard size cannot flip rungs.
Where that lands the sizes the app actually opens:

    A4, Letter   3.0    sharp, and cheap enough that nobody notices
    A3           2.0
    A2, A1, A0   1.5    unchanged from today

THE FLOOR MATTERS AS MUCH AS THE CEILING

`_LADDER[0]` is 1.5, the shipped scale, and the automatic choice never returns
less than it. An A0 at 1.5 is already over budget and there is no rung below to
demote it to, so the budget can only ever move a document UP. This change
cannot make any document slower or blurrier than the build before it.

FIXED FOR THE LIFE OF THE DOCUMENT

The chosen scale is memoised on the PDFDocument and never recomputed, and a
settings change applies only to tabs opened afterwards. That is not laziness,
it is the only cheap option. Annotation items live in SCENE space, which is
rendered-pixel space, and they serialise through `to_annotation_dict(self._zoom)`;
the image-lift crop in `ui/canvas.py` multiplies PDF coordinates by the same
`_zoom` to find its source rectangle. Changing the scale on a live document
means rescaling the geometry of every annotation on every page, plus the undo
stack that holds their old geometry, plus anything mid-drag. That is a real
refactor with a real risk of silently moving somebody's markup, and it buys
nothing a reopened tab does not.
"""

from __future__ import annotations

# The rungs the raster scale is allowed to take, ascending. 1.5 is what every
# build so far has used and is the floor; 3.0 is about 216 DPI, which is where
# small scanned text becomes comfortably readable. Intermediate values are
# deliberately absent: each distinct scale is a separate key in the render
# cache and a separate coordinate space for annotations, so a handful of
# well-understood values is worth more than a continuum.
RENDER_SCALE_LADDER = (1.5, 2.0, 3.0)

# The value the settings field carries when the app should decide for itself.
AUTO = "auto"

# Everything view.render_scale accepts. Strings, because "auto" has to live in
# the same field as the numbers and a JSON file with mixed types in one key is
# a worse thing to validate than a string that is parsed in one place.
RENDER_SCALE_CHOICES = (AUTO,) + tuple(_fmt for _fmt in
                                       (f"{s:g}" for s in RENDER_SCALE_LADDER))

# Megapixel ceiling on a rendered page. See the module docstring for the
# measurements that pick it: it sits between an A4 at 3.0 (4.5 Mpx, wanted) and
# an A3 at 3.0 (9.0 Mpx, not wanted), with margin on both sides.
MEGAPIXEL_BUDGET = 6.0

# SECONDARY guard, and secondary is the whole point of where it sits in
# `scale_for_page`: page size decides the scale, and this only ever pulls the
# answer back down. A document of many hundreds of small pages is a scanned
# bundle or a report, and it is read by paging through it rather than by
# sitting on one page. The render cache holds six entries no matter how long
# the document is, so at that length nearly every page turn is a cache miss
# paying the full render cost, and 15 ms per turn on a document somebody is
# flicking through is worth more than the extra sharpness. Below this, page
# count is not consulted at all.
LONG_DOCUMENT_PAGES = 200
LONG_DOCUMENT_MAX_SCALE = 2.0


def scale_for_page(width_pt: float, height_pt: float, page_count: int = 1) -> float:
    """The sharpest ladder rung this page can be rendered at within budget.

    `width_pt` and `height_pt` are PDF points as `page.bound()` reports them,
    which is the post-rotation size and is free to ask for: it needs no raster,
    so the scale can be settled before a single pixel is drawn.

    Always returns a member of RENDER_SCALE_LADDER, never an arbitrary float.
    A page too large for even the lowest rung still gets that lowest rung,
    because 1.5 is what it would have been rendered at anyway and there is
    nothing to be gained by going below the behaviour that shipped.

    A page with a nonsense size (zero, negative, or a value fitz could not
    produce) also gets the floor. This is called on the open path, so it has to
    degrade to the old behaviour rather than raise.
    """
    floor = RENDER_SCALE_LADDER[0]
    if not (width_pt > 0 and height_pt > 0):
        return floor

    ceiling = MEGAPIXEL_BUDGET * 1e6
    chosen = floor
    for scale in RENDER_SCALE_LADDER:
        if (width_pt * scale) * (height_pt * scale) <= ceiling:
            chosen = scale

    if page_count >= LONG_DOCUMENT_PAGES:
        chosen = min(chosen, LONG_DOCUMENT_MAX_SCALE)
    return max(chosen, floor)


def resolve_setting(value) -> float | None:
    """Turn a stored `view.render_scale` into a fixed scale, or None for auto.

    None means "no opinion, work it out from the page", which is what both
    `auto` and any unrecognised value produce. Unrecognised has to behave like
    auto rather than raise for the same reason every other setting falls back
    to its default on nonsense: a hand-edited or newer settings file must not
    stop a document opening.
    """
    if value is None or value == AUTO:
        return None
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return None
    return scale if scale in RENDER_SCALE_LADDER else None


def choose_render_scale(width_pt: float, height_pt: float, page_count: int = 1,
                        setting=AUTO) -> float:
    """The scale a document should be rasterised at, setting first.

    An explicit choice in Preferences wins outright and is not second-guessed
    against the budget. Somebody who picks Sharpest for a drawing has decided
    to trade the render time for the detail, and this is the manual override
    that makes that possible. Auto, and anything unreadable, falls to the
    measured rule in `scale_for_page`.
    """
    forced = resolve_setting(setting)
    if forced is not None:
        return forced
    return scale_for_page(width_pt, height_pt, page_count)
