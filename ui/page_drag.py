"""The payload a page drag carries, and how a drop finds where it came from.

Phase 5 of docs/tabs-plan.md. Small on purpose: this module is the contract
between the two widgets that can start a page drag (the left strip in
ui/page_panel.py and the Organizer grid in ui/organizer.py) and the two that
can receive one, and nothing else should need to know its shape.

WHY A NAMED MIME TYPE AT ALL. Both drags used to build their payload from
`model().mimeData(...)`, which produces Qt's default
`application/x-qabstractitemmodeldatalist`, and both receivers then guarded on
`event.source() is not self`. That worked only because a page could never come
from anywhere else. The moment pages cross tabs, a drag from another document's
strip is a legitimate drop and the source check rejects it, while a stray
internal Qt drag from some future list widget would sail through. So the format
is named, the source identity travels INSIDE the payload, and the receivers
check the format first.

WHY THE PAYLOAD IS NOT THE PAGES. It carries `{window_id, doc_id, rows, count}`
and no PDF bytes at all. Serialising pages into the drag would mean building
them on every drag that might be dropped, copying megabytes to move one page,
and losing the identity of the document they came out of, which is the one
thing a MOVE needs. The drop looks the source up by `doc_id` in the live
registry instead, and if it cannot find it (the window closed mid-drag) it
simply refuses, which is the correct answer anyway.
"""

import json

from PySide6.QtCore import QByteArray, QMimeData

#: The one format a rapid-pdf page drag speaks.
PAGES_MIME = "application/x-rapidpdf-pages"


def make_page_mime(view, rows: list) -> QMimeData:
    """The drag payload for `rows` coming out of `view`.

    `window_id` is recorded for the record and for a cheap early reject; the
    binding check at drop time is a LIVE comparison of the two views' windows,
    because a tear-off can move a document to another window while the drag is
    still in flight.
    """
    window = view.window() if view is not None else None
    payload = {
        "window_id": getattr(window, "window_id", lambda: "")(),
        "doc_id": view.doc_id() if view is not None else "",
        "rows": [int(r) for r in rows],
        "count": len(rows),
    }
    mime = QMimeData()
    mime.setData(PAGES_MIME,
                 QByteArray(json.dumps(payload).encode("utf-8")))
    return mime


def read_page_mime(mime) -> dict | None:
    """The payload inside a drag, or None when this is not one of ours.

    Tolerant by design: anything malformed is "not ours" rather than an error,
    because the alternative is an exception raised inside a Qt drag handler,
    which on Windows happens while the event loop is blocked by the drag.
    """
    if mime is None:
        return None
    try:
        if not mime.hasFormat(PAGES_MIME):
            return None
        raw = bytes(mime.data(PAGES_MIME))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("doc_id"):
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    payload["rows"] = [int(r) for r in rows]
    return payload


def find_source_view(doc_id: str):
    """The live DocumentView a payload names, or None if it has gone.

    The registry is the lookup rather than a reference held in the payload,
    which is what lets the payload be plain JSON. None is a perfectly ordinary
    answer: the source tab can be closed mid-drag, and refusing the drop is
    what should happen when it is.
    """
    if not doc_id:
        return None
    # Imported here rather than at module scope: ui.window_registry imports the
    # window, which imports the document view, which imports the page panel,
    # which imports this. A local import keeps that circle from closing at
    # import time.
    from ui.window_registry import WindowRegistry

    for _window, view in WindowRegistry.instance().views():
        try:
            if view.doc_id() == doc_id:
                return view
        except (AttributeError, RuntimeError):
            continue
    return None
