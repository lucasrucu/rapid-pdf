"""Pure page-order arithmetic, shared by the page panel and its undo commands.

Nothing here touches Qt or PyMuPDF. The point is that the awkward parts of a
drag-reorder (where does a multi-row selection land once the rows it is made of
have been taken out of the list?) and of undoing one are plain list maths that
can be tested on their own, without a widget or a PDF.

Everywhere in here an "order" is a permutation of range(page_count) read the
same way PDFDocument.reorder reads it: new page i is the page currently at
order[i].
"""


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


def shift_map_after_reorder(page_map: dict, order: list) -> dict:
    """Re-key a {page_index: value} map for a reorder."""
    out: dict = {}
    for new_idx, old_idx in enumerate(order):
        if old_idx in page_map:
            out[new_idx] = page_map[old_idx]
    return out
