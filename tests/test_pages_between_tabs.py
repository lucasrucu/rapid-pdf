"""Dragging a page out of one tab and into another. Phase 5 of docs/tabs-plan.md.

WHAT EACH SECTION IS FOR.

1. THE ENGINE. `PDFDocument.transfer_pages_from` on its own, with no widgets:
   one insert_pdf per row so a non-contiguous selection lands as one block,
   annotations riding along, and a refusal to read a document into itself.

2. THE DROP. The mime guard that replaced `event.source() is not self`, the
   routing of a same-document drop back to the plain reorder, and the two
   refusals that have to happen at dragMove time rather than at the drop:
   the last page, and a second window.

3. ONE COMMAND, TWO DOCUMENTS. The reason the undo stack moved to the window.
   Undo has to put the page back in the source AND take it out of the
   destination, as one step, and it has to bring the tab it is changing to the
   front while it does.

4. WHAT TRAVELS. Unsaved markup, which insert_pdf cannot see, so it goes as
   JSON and has to arrive as live editable items on the right pages.

5. THE LEDGER. Save is per document and never atomic across two, so the close
   prompt has to say what an unsaved move means.

A REAL DRAG LOOP CANNOT RUN IN A TEST: QDrag.exec hands control to the
platform. So the drop is delivered the way Qt would deliver it, straight into
dropEvent, with a stand-in event carrying the position, the source, the mime
payload and the modifiers. That is the same trick test_page_panel_widget.py
uses, extended with the payload the guard now reads. Everything either side of
the fake event is the genuine widget and the genuine command.
"""

import fitz
import pytest

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from core.pdf_document import PDFDocument
from core.settings import Settings, set_settings
from ui.main_window import MainWindow
from ui.page_drag import PAGES_MIME, make_page_mime, read_page_mime


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


@pytest.fixture(autouse=True)
def never_opens_a_dialog(monkeypatch):
    """Offscreen still runs a real modal loop, so an unexpected message box
    hangs the suite instead of failing it."""
    for name in ("question", "warning", "critical", "information", "about"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, n=name, **k: pytest.fail(
                f"QMessageBox.{n} opened: {a[1:3]}")))


def _pdf(tmp_path, name, pages=3, width=400, height=500):
    path = tmp_path / name
    raw = fitz.open()
    for i in range(pages):
        page = raw.new_page(width=width, height=height)
        page.insert_text((20, 100), f"{name} p{i}", fontsize=24)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def alpha(tmp_path):
    return _pdf(tmp_path, "alpha.pdf", pages=3)


@pytest.fixture
def beta(tmp_path):
    return _pdf(tmp_path, "beta.pdf", pages=2)


def _dispose(window):
    for view in window.document_area().views():
        view.clear_document()
        view.teardown()
    window._force_quit = True
    window.close()
    window.deleteLater()


@pytest.fixture
def win(qt_app, store):
    window = MainWindow()
    window.view._canvas.resize(600, 700)
    window.view._canvas._flush_pending_render()
    yield window
    _dispose(window)


@pytest.fixture
def two_tabs(win, alpha, beta):
    """One window, two documents: alpha (3 pages) then beta (2 pages)."""
    win.open_paths([alpha, beta])
    area = win.document_area()
    first, second = area.view_at(0), area.view_at(1)
    for view in (first, second):
        view._canvas.resize(600, 700)
        view._canvas._flush_pending_render()
    return win, first, second


class _FakeDrop:
    """What a drop handler reads off a real QDropEvent, and nothing else.

    Carries the mime payload and the modifiers as well as the position, because
    the guard is the mime FORMAT now and the copy/move choice is read off the
    modifiers at drop time.
    """

    def __init__(self, source, pos: QPoint, mime=None, modifiers=None):
        self._source = source
        self._pos = pos
        self._mime = mime
        self._modifiers = modifiers or Qt.KeyboardModifier.NoModifier
        self.accepted = False
        self.ignored = False

    def source(self):
        return self._source

    def pos(self):
        return self._pos

    def mimeData(self):
        return self._mime

    def modifiers(self):
        return self._modifiers

    def acceptProposedAction(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


def _row_top(view, row: int) -> QPoint:
    """A point in the strip that resolves to "insert above this row"."""
    lst = view._page_panel._list
    rect = lst.visualItemRect(lst.item(row))
    return QPoint(rect.center().x(), rect.top() + 1)


def _drop_into(dest_view, src_view, rows, row: int, copy: bool = False):
    """Deliver a page drag from `src_view`'s strip onto `dest_view`'s."""
    mods = (Qt.KeyboardModifier.ControlModifier if copy
            else Qt.KeyboardModifier.NoModifier)
    event = _FakeDrop(src_view._page_panel._list, _row_top(dest_view, row),
                      make_page_mime(src_view, rows), mods)
    dest_view._page_panel._list.dropEvent(event)
    return event


def _texts(view) -> list:
    """The text on each page, which is what says WHICH page ended up where."""
    doc = view._doc.doc
    return [doc[i].get_text().strip() for i in range(len(doc))]


def _mouse(canvas, kind, scene_pt, button=Qt.MouseButton.LeftButton):
    vp = canvas.mapFromScene(scene_pt)
    held = (Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease
            else button)
    return QMouseEvent(kind, QPointF(vp), QPointF(vp), button, held,
                       Qt.KeyboardModifier.NoModifier)


def _draw_a_rectangle(view, page: int = 0):
    """Pick the rect tool off the real toolbar and drag one out on the canvas."""
    canvas = view._canvas
    view.jump_to_page(page)
    canvas._flush_pending_render()
    view.trigger_tool("rect")
    start = QPointF(60 * canvas._zoom, 60 * canvas._zoom)
    end = QPointF(180 * canvas._zoom, 150 * canvas._zoom)
    canvas.mousePressEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonPress, start))
    canvas.mouseMoveEvent(_mouse(canvas, QMouseEvent.Type.MouseMove, end))
    canvas.mouseReleaseEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, end))


def _markup(view, page: int) -> list:
    return list(view._canvas._page_annotations.get(page, []))


# ---------------------------------------------------------------------------
# 1. The engine, with no widgets in the way
# ---------------------------------------------------------------------------

def test_transfer_moves_the_named_pages_and_lands_them_as_one_block(tmp_path):
    src, dest = PDFDocument(), PDFDocument()
    assert src.open(_pdf(tmp_path, "src.pdf", pages=4))
    assert dest.open(_pdf(tmp_path, "dest.pdf", pages=2))

    # Non-contiguous on purpose: one insert_pdf per row is what keeps this
    # landing as a single block with no arithmetic.
    moved = dest.transfer_pages_from(src, [0, 2], at=1)

    assert moved == 2
    assert dest.page_count() == 4
    assert [dest.doc[i].get_text().strip() for i in range(4)] == [
        "dest.pdf p0", "src.pdf p0", "src.pdf p2", "dest.pdf p1",
    ]
    assert src.page_count() == 4, "a transfer copies; the delete is the caller's"
    src.close()
    dest.close()


def test_transfer_carries_annotations_across(tmp_path):
    path = tmp_path / "annotated.pdf"
    raw = fitz.open()
    page = raw.new_page(width=400, height=500)
    page.add_rect_annot(fitz.Rect(50, 50, 150, 150))
    raw.new_page(width=400, height=500)
    raw.save(str(path))
    raw.close()

    src, dest = PDFDocument(), PDFDocument()
    assert src.open(str(path))
    assert dest.open(_pdf(tmp_path, "empty.pdf", pages=1))

    dest.transfer_pages_from(src, [0], at=1)

    assert len(list(dest.doc[1].annots())) == 1
    src.close()
    dest.close()


def test_transfer_refuses_to_read_a_document_into_itself(tmp_path):
    """PyMuPDF cannot do it, and a silent no-op would look like a lost page.

    The drop handler never gets here: a same-document drop is routed to the
    plain reorder instead. This is the backstop that says so out loud.
    """
    doc = PDFDocument()
    assert doc.open(_pdf(tmp_path, "self.pdf", pages=2))
    with pytest.raises(ValueError):
        doc.transfer_pages_from(doc, [0], at=1)
    doc.close()


def test_a_delete_no_longer_leaves_a_bookmark_pointing_nowhere(tmp_path):
    """Known bug 5. delete_page renumbers the TOC but leaves the deleted page's
    own entry on -1, and it survives the save."""
    path = tmp_path / "toc.pdf"
    raw = fitz.open()
    for i in range(3):
        raw.new_page(width=400, height=500)
    raw.set_toc([[1, "One", 1], [1, "Two", 2], [1, "Three", 3]])
    raw.save(str(path))
    raw.close()

    doc = PDFDocument()
    assert doc.open(str(path))
    doc.delete_page(1)

    assert [entry[2] for entry in doc.doc.get_toc()] == [1, 2]
    doc.close()


def test_a_password_protected_pdf_is_refused_at_open(tmp_path):
    """Known bug 4. fitz.open SUCCEEDS on one of these and only sets
    needs_pass, so the app used to accept the file and throw on first render."""
    path = tmp_path / "locked.pdf"
    raw = fitz.open()
    raw.new_page(width=400, height=500)
    raw.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256,
             user_pw="secret", owner_pw="secret")
    raw.close()

    doc = PDFDocument()
    assert doc.open(str(path)) is False
    assert doc.doc is None
    assert "password" in doc.last_open_error.lower()


# ---------------------------------------------------------------------------
# 2. The drop: who is allowed to land, and where
# ---------------------------------------------------------------------------

def test_a_drop_from_another_tab_moves_the_page(two_tabs):
    _win, alpha_view, beta_view = two_tabs

    event = _drop_into(beta_view, alpha_view, [1], row=0)

    assert event.accepted
    assert _texts(beta_view) == ["alpha.pdf p1", "beta.pdf p0", "beta.pdf p1"]
    assert _texts(alpha_view) == ["alpha.pdf p0", "alpha.pdf p2"]


def test_ctrl_held_at_drop_time_copies_instead_of_moving(two_tabs):
    """Ctrl+drag is already Duplicate in this app, and it is what Windows does."""
    _win, alpha_view, beta_view = two_tabs

    _drop_into(beta_view, alpha_view, [1], row=0, copy=True)

    assert _texts(beta_view)[0] == "alpha.pdf p1"
    assert _texts(alpha_view) == ["alpha.pdf p0", "alpha.pdf p1", "alpha.pdf p2"]
    assert not alpha_view.is_dirty(), "a copy must not dirty the source"
    assert beta_view.is_dirty()


def test_a_drop_back_into_the_same_document_is_a_plain_reorder(two_tabs):
    """insert_pdf cannot read a document into itself, so this can NEVER take
    the transfer path. It has to route to the reorder that was already here."""
    _win, alpha_view, _beta = two_tabs
    asked = []
    alpha_view._page_panel.pages_reorder_requested.connect(
        lambda order, rows: asked.append((order, rows)))
    transfers = []
    alpha_view._page_panel.pages_transfer_requested.connect(
        lambda *a: transfers.append(a))

    _drop_into(alpha_view, alpha_view, [0], row=2)

    assert transfers == []
    assert asked == [([1, 0, 2], [0])]


def test_a_drag_that_is_not_ours_is_refused(two_tabs):
    """The guard is the mime FORMAT now, not `event.source() is not self`."""
    _win, _alpha, beta_view = two_tabs
    seen = []
    beta_view._page_panel.pages_transfer_requested.connect(lambda *a: seen.append(a))
    beta_view._page_panel.pages_reorder_requested.connect(lambda *a: seen.append(a))

    event = _FakeDrop(object(), _row_top(beta_view, 0), mime=None)
    beta_view._page_panel._list.dropEvent(event)

    assert event.ignored
    assert not event.accepted
    assert seen == []
    assert beta_view.page_count() == 2


def test_dragging_the_last_page_out_is_refused_while_it_is_still_a_drag(
        two_tabs, tmp_path):
    """Refused at dragMove time, so the user is never left holding a drag that
    was going to be turned away. Matches the existing delete guard."""
    win, _alpha, beta_view = two_tabs
    win.open_paths([_pdf(tmp_path, "lone.pdf", pages=1)])
    lone = win.document_area().view_at(2)

    payload = read_page_mime(make_page_mime(lone, [0]))
    refusal = beta_view._page_panel._list._transfer_refusal(payload, copy=False)

    assert refusal == "A document has to keep at least one page"

    event = _drop_into(beta_view, lone, [0], row=0)
    assert event.ignored
    assert lone.page_count() == 1
    assert beta_view.page_count() == 2


def test_a_drop_from_another_window_is_refused_with_a_reason(two_tabs):
    """Two windows means two undo stacks, and the duplicate-page problem the
    per-window stack exists to avoid comes straight back. Phase 5 says no."""
    win, alpha_view, beta_view = two_tabs
    torn = win.move_view_to_new_window(alpha_view)
    assert torn is not None and alpha_view.window() is torn

    payload = read_page_mime(make_page_mime(alpha_view, [0]))
    refusal = beta_view._page_panel._list._transfer_refusal(payload, copy=False)

    assert refusal == "Pages can only move between tabs in the same window"

    event = _drop_into(beta_view, alpha_view, [0], row=0)
    assert event.ignored
    assert beta_view.page_count() == 2
    assert alpha_view.page_count() == 3
    _dispose(torn)


def test_the_organizer_grid_takes_a_drop_from_another_document_too(two_tabs):
    """Both receivers had the same guard and both had to change. The grid is
    the one that also lost its native drop indicator to DragDrop mode, so it
    is worth asserting it still lands the pages."""
    _win, alpha_view, beta_view = two_tabs
    beta_view._tabs.setCurrentIndex(1)          # switch to the Organizer
    grid = beta_view._organizer._list
    assert grid.count() == 2, "the grid did not load"

    rect = grid.visualItemRect(grid.item(0))
    event = _FakeDrop(alpha_view._page_panel._list,
                      QPoint(rect.left() + 1, rect.center().y()),
                      make_page_mime(alpha_view, [1]))
    grid.dropEvent(event)

    assert event.accepted
    assert _texts(beta_view) == ["alpha.pdf p1", "beta.pdf p0", "beta.pdf p1"]
    assert _texts(alpha_view) == ["alpha.pdf p0", "alpha.pdf p2"]


def test_hovering_a_tab_under_a_page_drag_switches_to_it(two_tabs):
    """550 ms of rest brings a tab forward mid-drag, so a page can be dropped
    into a document that was not on screen when the drag started.

    The bar accepts the drag ENTER purely to keep receiving moves and refuses
    every drop, which is what stops the switch ending the drag. Offscreen never
    runs the countdown, so the timer is fired by hand.
    """
    win, alpha_view, _beta = two_tabs
    area = win.document_area()
    bar = area.bar()
    area.set_current_index(1)

    mime = make_page_mime(alpha_view, [0])

    class _FakeDragMove(_FakeDrop):
        def position(self):
            return QPointF(self._pos)

    rect = bar.tabRect(0)
    event = _FakeDragMove(None, rect.center(), mime)
    bar.dragMoveEvent(event)

    assert bar.hover_switch_timer().isActive()
    assert event.ignored, "the bar must never take the drop itself"
    assert area.current_index() == 1, "it switches on the timer, not on contact"

    bar._on_hover_elapsed()

    assert area.current_index() == 0


# ---------------------------------------------------------------------------
# 3. One command, two documents
# ---------------------------------------------------------------------------

def test_the_two_tabs_share_one_undo_stack(two_tabs):
    win, alpha_view, beta_view = two_tabs
    assert alpha_view.undo_stack() is beta_view.undo_stack()
    assert alpha_view.undo_stack() is win.undo_stack()


def test_undoing_a_cross_document_move_restores_both_documents(two_tabs):
    """THE REASON THE STACK MOVED TO THE WINDOW.

    Split across two stacks there is no ordering that gets back here: undo on
    the destination re-inserts the page into the source while the source's own
    stack still thinks nothing happened, and the user has a duplicate.
    """
    win, alpha_view, beta_view = two_tabs
    _drop_into(beta_view, alpha_view, [1], row=0)
    assert beta_view.page_count() == 3 and alpha_view.page_count() == 2

    win.undo_stack().undo()

    assert _texts(alpha_view) == ["alpha.pdf p0", "alpha.pdf p1", "alpha.pdf p2"]
    assert _texts(beta_view) == ["beta.pdf p0", "beta.pdf p1"]

    win.undo_stack().redo()

    assert _texts(alpha_view) == ["alpha.pdf p0", "alpha.pdf p2"]
    assert _texts(beta_view)[0] == "alpha.pdf p1"


def test_the_move_is_one_command_not_two(two_tabs):
    win, alpha_view, beta_view = two_tabs
    before = win.undo_stack().count()

    _drop_into(beta_view, alpha_view, [0, 1], row=0)

    assert win.undo_stack().count() == before + 1
    assert win.undo_stack().command(before).text() == "Move 2 pages between documents"


def test_undo_brings_the_affected_tab_to_the_front(two_tabs):
    """The cost of one stack per window is that undo can change a tab you are
    not looking at. The command asks for its tab first, so it is watched."""
    win, alpha_view, beta_view = two_tabs
    area = win.document_area()
    _drop_into(beta_view, alpha_view, [1], row=0)
    area.set_current_index(0)
    assert area.current_view() is alpha_view

    win.undo_stack().undo()

    assert area.current_view() is beta_view, "undo changed a tab off screen"


def test_both_documents_come_out_dirty_and_come_back_clean(two_tabs):
    """Dirty is a per-document revision counter now, because one shared stack's
    isClean() cannot answer the question for two documents at once."""
    win, alpha_view, beta_view = two_tabs
    assert not alpha_view.is_dirty() and not beta_view.is_dirty()

    _drop_into(beta_view, alpha_view, [1], row=0)

    assert alpha_view.is_dirty()
    assert beta_view.is_dirty()

    win.undo_stack().undo()

    assert not alpha_view.is_dirty()
    assert not beta_view.is_dirty()


def test_saving_one_document_leaves_the_other_dirty(two_tabs):
    """setClean() would have marked BOTH, which is the bug the counter fixes."""
    win, alpha_view, beta_view = two_tabs
    _drop_into(beta_view, alpha_view, [1], row=0)

    assert beta_view.save_pdf() is True

    assert not beta_view.is_dirty()
    assert alpha_view.is_dirty(), "saving the destination cleaned the source"


# ---------------------------------------------------------------------------
# 4. What travels with the page
# ---------------------------------------------------------------------------

def test_unsaved_markup_travels_as_json_and_arrives_editable(two_tabs):
    """insert_pdf cannot see it: it is Qt scene items, not annotations in the
    file. It goes through the _item_to_json seam, which is PDF-space and
    zoom-independent precisely so the far end can rebuild at its own zoom."""
    _win, alpha_view, beta_view = two_tabs
    _draw_a_rectangle(alpha_view, page=1)
    assert len(_markup(alpha_view, 1)) == 1

    _drop_into(beta_view, alpha_view, [1], row=0)

    arrived = _markup(beta_view, 0)
    assert len(arrived) == 1, "the markup did not come across"
    assert arrived[0].ann_type == "rect"
    assert arrived[0] is not None and arrived[0].scene() is not None
    assert _markup(alpha_view, 1) == [], "the markup was left behind as well"


def test_undoing_the_move_puts_the_markup_back_too(two_tabs):
    _win, alpha_view, beta_view = two_tabs
    _draw_a_rectangle(alpha_view, page=1)
    original = _markup(alpha_view, 1)[0]

    _drop_into(beta_view, alpha_view, [1], row=0)
    _win.undo_stack().undo()

    assert _markup(beta_view, 0) == []
    assert _markup(alpha_view, 1) == [original], "not the same object back"


def test_a_moved_page_reports_what_it_quietly_lost(tmp_path):
    """PyMuPDF drops an internal GOTO link whose target did not come along,
    and says nothing at all about it. transfer_report is what says it."""
    path = tmp_path / "linked.pdf"
    raw = fitz.open()
    raw.new_page(width=400, height=500)
    raw.new_page(width=400, height=500)
    # Re-fetched: new_page() invalidates the Page objects handed out before it.
    raw[0].insert_link({"kind": fitz.LINK_GOTO, "page": 1,
                        "from": fitz.Rect(10, 10, 90, 40)})
    raw.save(str(path))
    raw.close()

    doc = PDFDocument()
    assert doc.open(str(path))

    assert doc.transfer_report([0])["links"] == 1
    assert doc.transfer_report([0, 1])["links"] == 0, "the target came too"
    doc.close()


# ---------------------------------------------------------------------------
# 5. The ledger, and what the close prompt says
# ---------------------------------------------------------------------------

def test_the_donor_close_prompt_says_the_page_lives_in_both_files(two_tabs):
    _win, alpha_view, beta_view = two_tabs
    _drop_into(beta_view, alpha_view, [1], row=0)

    warning = alpha_view.transfer_warning()

    assert "beta.pdf" in warning
    assert "AND in beta.pdf" in warning


def test_the_recipient_close_prompt_says_the_page_would_be_lost(two_tabs):
    _win, alpha_view, beta_view = two_tabs
    _drop_into(beta_view, alpha_view, [1], row=0)

    warning = beta_view.transfer_warning()

    assert "alpha.pdf" in warning
    assert "loses it" in warning


def test_saving_settles_the_ledger(two_tabs):
    _win, alpha_view, beta_view = two_tabs
    _drop_into(beta_view, alpha_view, [1], row=0)
    assert beta_view.transfer_warning()

    assert beta_view.save_pdf() is True

    assert beta_view.transfer_warning() == ""
    assert alpha_view.transfer_warning(), "the donor still owes a save"


def test_undoing_the_move_takes_the_ledger_entry_back(two_tabs):
    win, alpha_view, beta_view = two_tabs
    _drop_into(beta_view, alpha_view, [1], row=0)

    win.undo_stack().undo()

    assert alpha_view.transfer_warning() == ""
    assert beta_view.transfer_warning() == ""


# ---------------------------------------------------------------------------
# 6. The payload itself
# ---------------------------------------------------------------------------

def test_the_payload_names_the_document_and_not_the_widget(two_tabs):
    _win, alpha_view, _beta = two_tabs
    mime = make_page_mime(alpha_view, [0, 2])

    assert mime.hasFormat(PAGES_MIME)
    payload = read_page_mime(mime)
    assert payload["doc_id"] == alpha_view.doc_id()
    assert payload["rows"] == [0, 2]
    assert payload["count"] == 2
    assert payload["window_id"] == alpha_view.window().window_id()


def test_a_malformed_payload_is_simply_not_ours(qt_app):
    """Tolerant on purpose: an exception raised inside a Qt drag handler fires
    while the event loop is blocked by the drag."""
    from PySide6.QtCore import QByteArray, QMimeData

    assert read_page_mime(None) is None
    assert read_page_mime(QMimeData()) is None

    junk = QMimeData()
    junk.setData(PAGES_MIME, QByteArray(b"not json"))
    assert read_page_mime(junk) is None

    empty = QMimeData()
    empty.setData(PAGES_MIME, QByteArray(b'{"doc_id": "x", "rows": []}'))
    assert read_page_mime(empty) is None
