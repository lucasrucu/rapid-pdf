"""Pure page-order and page-rotation arithmetic, shared by the page panels and
their undo commands.

Nothing here touches Qt or PyMuPDF. The point is that the awkward parts of a
drag-reorder (where does a multi-row selection land once the rows it is made of
have been taken out of the list?) and of undoing one are plain list maths that
can be tested on their own, without a widget or a PDF.

Everywhere in here an "order" is a permutation of range(page_count) read the
same way PDFDocument.reorder reads it: new page i is the page currently at
order[i].

The rotation half is the same idea for a different edit. A page's /Rotate is
one of 0, 90, 180, 270 and everything about turning one is arithmetic on that
number plus arithmetic on the box the page is drawn into. See ROTATE_CW below
for why the coordinate maths lives here rather than being read off a PyMuPDF
matrix at the call site.
"""

# The three turns the UI offers, as clockwise degrees. Anticlockwise is 270
# clockwise: there is one direction in the file format and pretending otherwise
# doubles every branch below for nothing.
ROTATE_CW = 90
ROTATE_CCW = 270
ROTATE_180 = 180

#: The only values /Rotate is allowed to hold.
ROTATIONS = (0, 90, 180, 270)


def normalize_rotation(degrees) -> int:
    """Fold any angle onto one of ROTATIONS.

    Worth doing OURSELVES rather than leaving to PyMuPDF, which is quietly
    lossy about it: measured on 1.27.2.3, `page.set_rotation(91)` leaves the
    page at 0 rather than raising or rounding. A caller that passed a value
    that is not a multiple of 90 would get silence and no rotation, so anything
    that is not a clean quarter turn is snapped to the nearest one here and the
    document only ever sees a legal value.
    """
    try:
        value = int(round(float(degrees) / 90.0)) * 90
    except (TypeError, ValueError):
        return 0
    return value % 360


def rotation_after(current, delta) -> int:
    """The /Rotate a page carries once `delta` clockwise degrees are added."""
    return (normalize_rotation(current) + normalize_rotation(delta)) % 360


def rotated_size(width: float, height: float, delta) -> tuple:
    """The visible page box after turning it by `delta`. A quarter turn swaps."""
    return (height, width) if normalize_rotation(delta) in (90, 270) else (width, height)


def rotate_point(x: float, y: float, delta, width: float, height: float) -> tuple:
    """Where visible point (x, y) lands when the page turns `delta` clockwise.

    `width` and `height` are the page's visible box BEFORE the turn, so a page
    that arrived already rotated (a 270-degree scan) is measured from where it
    is rather than from zero. Everything the canvas holds is in this space:
    markup is stored in rendered-pixel coordinates, which are visible page
    points times the document's frozen render scale, so the same three lines
    serve both once the caller has scaled its box to match.

    WHY ARITHMETIC AND NOT `page.derotation_matrix * page.rotation_matrix`.
    The two are the same number, and that was verified rather than assumed:
    for every base rotation in ROTATIONS and every delta, this agrees with
    PyMuPDF's own matrices to within 1e-6 (tests/test_page_rotation.py). What
    the arithmetic buys is that the transform can be applied to Qt items with
    no page in hand, in a module that has no PDF library in it, and tested on
    plain numbers.
    """
    delta = normalize_rotation(delta)
    if delta == 90:
        return (height - y, x)
    if delta == 180:
        return (width - x, height - y)
    if delta == 270:
        return (y, width - x)
    return (x, y)


def rotate_rect(rect, delta, width: float, height: float) -> tuple:
    """`rect` as (x0, y0, x1, y1) after a `delta` turn, normalised.

    An axis-aligned box stays axis-aligned through a quarter turn, so mapping
    the two opposite corners and re-normalising is exact. A quarter turn swaps
    the box's own width and height, which is the point: a highlight drawn along
    a line of text has to end up running down the page with it.
    """
    x0, y0, x1, y1 = rect
    ax, ay = rotate_point(x0, y0, delta, width, height)
    bx, by = rotate_point(x1, y1, delta, width, height)
    return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


def rotate_rect_upright(rect, delta, width: float, height: float) -> tuple:
    """`rect` moved with the content but KEEPING its own size and orientation.

    For the two kinds of markup that carry their own upright content rather
    than a shape: a text label and a pasted image. Turning their box would
    stand the words on end or squash the picture into the wrong aspect, and
    neither item can draw itself rotated. So the box travels by its CENTRE and
    keeps the extents it had, which leaves the label or the stamp sitting on
    the same spot of the page, still readable.
    """
    x0, y0, x1, y1 = rect
    w, h = abs(x1 - x0), abs(y1 - y0)
    cx, cy = rotate_point((x0 + x1) / 2.0, (y0 + y1) / 2.0, delta, width, height)
    return (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def move_rows(count: int, rows, target: int) -> list:
    """Order after moving `rows` to insertion point `target`.

    `rows` keep their relative order and land as one block, even when the
    selection they came from was non-contiguous. `target` is an insertion index
    into the list AS SHOWN, before anything is taken out: 0 means above the
    first page, `count` means below the last. Because the moved rows come out
    first, a target that sat below some of them has to be pulled back by that
    many places, which is the one bit of this that is easy to get wrong.
    """
    picked = sorted({r for r in rows if 0 <= r < count})
    if not picked:
        return list(range(count))
    target = max(0, min(int(target), count))
    at = target - sum(1 for r in picked if r < target)
    taken = set(picked)
    rest = [i for i in range(count) if i not in taken]
    at = max(0, min(at, len(rest)))
    return rest[:at] + picked + rest[at:]


def invert_order(order: list) -> list:
    """The order that undoes `order`.

    After reorder(order), the page that used to be at old index `o` sits at new
    index `n` where order[n] == o. Sending each of those back where it came from
    is exactly inverse[o] = n.
    """
    inverse = [0] * len(order)
    for new_idx, old_idx in enumerate(order):
        inverse[old_idx] = new_idx
    return inverse


def is_permutation(order, count: int) -> bool:
    """True if `order` is a full permutation of range(count)."""
    try:
        return sorted(order) == list(range(count))
    except TypeError:
        return False


def page_after_delete(page: int, deleted) -> int:
    """Where page index `page` ends up once `deleted` pages are removed.

    A deleted page has no landing spot of its own, so it reports the index the
    page below it slides up into. That can be one past the end when the last
    page was the one deleted, so callers clamp to the new page count. Used to
    keep the editor on something sensible after a delete.
    """
    return page - sum(1 for d in set(deleted) if d < page)


def shift_map_after_delete(page_map: dict, deleted) -> dict:
    """Re-key a {page_index: value} map for a delete, dropping the deleted keys."""
    gone = set(deleted)
    out: dict = {}
    for page, value in page_map.items():
        if page in gone:
            continue
        out[page - sum(1 for d in gone if d < page)] = value
    return out


def shift_map_after_insert(page_map: dict, at: int, count: int) -> dict:
    """Re-key a {page_index: value} map for `count` pages inserted at `at`.

    The inverse of shift_map_after_delete for a contiguous block, which is what
    a page arriving from another document always is: transfer_pages_from lands
    a non-contiguous selection as one run starting at `at`. Keys at or after
    the insertion point move down by `count`; keys above nothing move at all.
    The inserted pages themselves are not given keys here. The caller fills
    those in with whatever markup came across with them.
    """
    if count <= 0:
        return dict(page_map)
    out: dict = {}
    for page, value in page_map.items():
        out[page + count if page >= at else page] = value
    return out


def shift_map_after_reorder(page_map: dict, order: list) -> dict:
    """Re-key a {page_index: value} map for a reorder."""
    out: dict = {}
    for new_idx, old_idx in enumerate(order):
        if old_idx in page_map:
            out[new_idx] = page_map[old_idx]
    return out
