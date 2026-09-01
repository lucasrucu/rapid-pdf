"""The seam between the window and the document it shows.

Phase 1 of docs/tabs-plan.md moved everything per-document out of MainWindow
into DocumentView. The plan calls it the riskiest phase of the whole project,
and the reason is the first section below: the thing most likely to break is
the thing that does not raise when it breaks.

WHAT EACH SECTION IS FOR.

1. THE ANNOTATION ROUND TRIP. `_flush_annotations` → save → `_after_successful_save`
   → `_strip_baked_annotations`, and `_load_saved_annotations` on the way back
   in. This is what makes markup reopen as editable objects instead of as flat
   pixels welded to the page. Get the order wrong and the file still saves and
   still opens; the markup just stops being editable, or arrives twice (once
   live, once baked underneath). Nothing raises, so only a real save-and-reopen
   catches it. There was no test for this before the split; there is now.

2. THE RENDER-CLONE PAIRING. Every markup-baked clone has to be closed by the
   thing that replaces it. A missed close leaks a whole fitz document per
   switch into the Organizer, which is invisible until it is a gigabyte.

3. OWNERSHIP. That the document state really is on the view, and that the
   window really did stop holding it. The second half is the one that keeps
   phase 2 honest: a window that still reaches for `self._doc` cannot hold two.

Offscreen (see conftest) never runs the event loop and lies about geometry, so
the canvas is given an explicit size and its debounced render is flushed by
hand, the way test_canvas_undo.py does it.
"""

import fitz
import pytest

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from core.pdf_document import RAPID_PDF_TAG
from ui.canvas import RectAnnotationItem
from ui.document_view import DocumentView
from ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def never_opens_a_dialog(monkeypatch):
    """Nothing in this file is supposed to reach a message box.

    Offscreen still runs a real modal loop, so a save that quietly failed would
    hang the suite instead of failing it. This turns any of them into a
    readable assertion.
    """
    for name in ("question", "warning", "critical", "information", "about"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, n=name, **k: pytest.fail(
                f"QMessageBox.{n} opened: {a[1:3]}")))


@pytest.fixture
def pdf_path(tmp_path):
    """A three page PDF, numbered, big enough to draw on."""
    path = tmp_path / "three.pdf"
    raw = fitz.open()
    for i in range(3):
        page = raw.new_page(width=400, height=500)
        page.insert_text((20, 100), f"p{i}", fontsize=36)
    raw.save(str(path))
    raw.close()
    return str(path)


def _build(path=None):
    """A real window, with its canvas given a size offscreen will not."""
    window = MainWindow()
    if path is not None:
        window.open_paths([path])
    window.view._canvas.resize(600, 700)
    window.view._canvas._flush_pending_render()
    return window


def _dispose(window):
    """Put a window away without leaving a trap for the next test.

    clear_document() first, deliberately. deleteLater() needs an event loop
    nobody runs offscreen, so the widgets outlive the test, and the Organizer
    keeps a render source and a queued render behind it. Any later
    QApplication.processEvents() (there is one inside _refresh_organizer) would
    then pump a dead test's render against a closed document. Clearing sets
    that source to None, which is what the Organizer checks.
    """
    window.view.clear_document()
    window.view.teardown()
    window.deleteLater()


@pytest.fixture
def win(qt_app, pdf_path):
    window = _build(pdf_path)
    yield window
    _dispose(window)


@pytest.fixture
def empty_win(qt_app):
    window = _build()
    yield window
    _dispose(window)


# ---------------------------------------------------------------------------
# Drawing, the way Qt would deliver it
# ---------------------------------------------------------------------------

def _mouse(canvas, kind, scene_pt, button=Qt.MouseButton.LeftButton):
    vp = canvas.mapFromScene(scene_pt)
    held = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else button
    return QMouseEvent(kind, QPointF(vp), QPointF(vp), button, held,
                       Qt.KeyboardModifier.NoModifier)


def _draw_a_rectangle(window):
    """Pick the rect tool off the real toolbar and drag one out on the canvas."""
    canvas = window.view._canvas
    window.view.trigger_tool("rect")
    start = QPointF(60 * canvas._zoom, 60 * canvas._zoom)
    end = QPointF(180 * canvas._zoom, 150 * canvas._zoom)
    canvas.mousePressEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonPress, start))
    canvas.mouseMoveEvent(_mouse(canvas, QMouseEvent.Type.MouseMove, end))
    canvas.mouseReleaseEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, end))


def _editable_items(window, page=0):
    return list(window.view._canvas._page_annotations.get(page, []))


def _baked_on_disk(path, page=0) -> int:
    """How many rapid-pdf annotations the FILE carries on a page."""
    doc = fitz.open(path)
    try:
        return sum(1 for a in doc[page].annots()
                   if a.info.get("title") == RAPID_PDF_TAG)
    finally:
        doc.close()


def _baked_live(window, page=0) -> int:
    """How many rapid-pdf annotations the LIVE in-memory document carries."""
    return sum(1 for a in window.view._doc.doc[page].annots()
               if a.info.get("title") == RAPID_PDF_TAG)


# ---------------------------------------------------------------------------
# 1. The annotation round trip (flush / save / strip / reload)
# ---------------------------------------------------------------------------

def test_the_gesture_really_makes_an_editable_object(win):
    """Guards every test below it: without a drawn item they prove nothing."""
    _draw_a_rectangle(win)
    items = _editable_items(win)
    assert len(items) == 1
    assert isinstance(items[0], RectAnnotationItem)
    assert win.view.is_dirty()


def test_saving_bakes_the_markup_into_the_file(win, pdf_path):
    """_flush_annotations, so other PDF viewers see the markup at all."""
    _draw_a_rectangle(win)
    assert win.save_pdf() is True
    assert _baked_on_disk(pdf_path) == 1


def test_saving_embeds_the_editable_model_too(win, pdf_path):
    """The baked copy is for other readers; the model is what WE reopen from."""
    _draw_a_rectangle(win)
    assert win.save_pdf() is True

    from core.pdf_document import PDFDocument
    check = PDFDocument()
    assert check.open(pdf_path)
    try:
        model = check.read_annotation_model()
    finally:
        check.close()
    assert model, "the save wrote no editable model"
    assert len(model["pages"]["0"]) == 1


def test_the_live_document_is_stripped_after_the_save(win):
    """_strip_baked_annotations. Skip it and the next page render draws the
    markup into the background pixmap while the live item is still on top, so
    every annotation shows twice and the ghost cannot be selected."""
    _draw_a_rectangle(win)
    assert _baked_live(win) == 0          # not baked yet, it is a Qt item
    assert win.save_pdf() is True
    assert _baked_live(win) == 0          # baked for the write, then stripped
    assert len(_editable_items(win)) == 1  # and the editable item survived


def test_a_saved_annotation_reopens_editable_and_not_as_pixels(qt_app, win, pdf_path):
    """THE ROUND TRIP. The headline feature, end to end, in a second window.

    Two halves, and both have to hold: the markup comes back as a live,
    editable object, AND the live document underneath it carries none of the
    baked copy. Either half alone would still "work" to look at.
    """
    _draw_a_rectangle(win)
    assert win.save_pdf() is True

    reopened = _build(pdf_path)
    try:
        items = _editable_items(reopened)
        assert len(items) == 1, "the markup did not come back as an editable item"
        assert isinstance(items[0], RectAnnotationItem)
        assert _baked_live(reopened) == 0, "the baked copy was not stripped on open"
        assert _baked_on_disk(pdf_path) == 1, "the file itself must keep its baked copy"
    finally:
        _dispose(reopened)


def test_the_round_trip_survives_a_second_save(qt_app, pdf_path):
    """Re-baking on every save is how the markup stops being editable in the
    end: one live item plus one un-stripped baked copy becomes two baked copies
    next time round.

    No `win` fixture here. This one saves over the file a second time, and a
    window still holding it open makes PyMuPDF fall back to a .bak beside it.
    """
    first = _build(pdf_path)
    try:
        _draw_a_rectangle(first)
        assert first.save_pdf() is True
    finally:
        _dispose(first)             # let go of the file before reopening it

    reopened = _build(pdf_path)
    try:
        assert reopened.save_pdf() is True
        assert _baked_on_disk(pdf_path) == 1, "the markup was baked twice"
        assert len(_editable_items(reopened)) == 1
    finally:
        _dispose(reopened)


def test_a_saved_document_stops_being_dirty(win):
    _draw_a_rectangle(win)
    assert win.view.is_dirty()
    assert win.save_pdf() is True
    assert not win.view.is_dirty()
    # NOT isClean(). Phase 5 put one undo stack on the WINDOW, so its clean
    # index cannot say "this document is saved" without claiming it for every
    # other tab too. Each document remembers the revision it was saved at
    # instead; see ui/undo.py.
    assert win.view._saved_rev == win.view._rev


# ---------------------------------------------------------------------------
# 2. The markup-baked clone pairing
# ---------------------------------------------------------------------------

def test_switching_to_the_organizer_builds_a_render_clone(win):
    win.view._tabs.setCurrentIndex(1)
    assert win.view._org_render is not None
    assert win.view._org_render.doc is not None


def test_the_organizer_clone_has_a_closer_that_really_closes_it(win):
    """Every clone is paired with a closer, and the closer both closes the
    fitz document and drops the reference. _refresh_organizer calls this as its
    first statement, before it makes another one.

    Deliberately NOT tested by switching into the Organizer twice: that path
    trips a pre-existing bug (see the note added to docs/tabs-plan.md), which
    phase 1 is not allowed to fix.
    """
    win.view._tabs.setCurrentIndex(1)
    clone = win.view._org_render.doc
    assert not clone.is_closed

    win.view._close_org_render()
    assert clone.is_closed
    assert win.view._org_render is None


def test_rebuilding_the_panel_closes_the_previous_clone(win):
    first = win.view._panel_render.doc
    win.view._refresh_panel_thumbnails()
    second = win.view._panel_render.doc

    assert first.is_closed, "the previous panel clone was never closed"
    assert second is not first


def test_teardown_closes_both_clones_and_the_document(win):
    win.view._tabs.setCurrentIndex(1)
    org = win.view._org_render.doc
    panel = win.view._panel_render.doc
    live = win.view._doc.doc

    win.view.teardown()

    assert org.is_closed
    assert panel.is_closed
    assert live.is_closed
    assert win.view._org_render is None
    assert win.view._panel_render is None


def test_closing_the_document_closes_both_clones(win):
    win.view._tabs.setCurrentIndex(1)
    org = win.view._org_render.doc
    panel = win.view._panel_render.doc

    win.close_pdf()

    assert org.is_closed
    assert panel.is_closed
    assert not win.view.has_document()


# ---------------------------------------------------------------------------
# 3a. The view owns the document
# ---------------------------------------------------------------------------

#: Everything phase 1 moved off the window. Named here rather than inline
#: because two tests need the same list from opposite directions.
_PER_DOCUMENT = [
    "_doc", "_current_page", "_org_render", "_panel_render", "_dirty",
    "_ocr_thread", "_ocr_worker", "_pending_page_selection",
    "_search_hits", "_search_index", "_search_term", "_search_timer",
    "_canvas", "_page_panel", "_organizer", "_toolbar", "_search_bar", "_tabs",
]


def test_the_view_owns_every_piece_of_per_document_state(win):
    missing = [name for name in _PER_DOCUMENT if not hasattr(win.view, name)]
    assert missing == []


def test_the_editor_organizer_switcher_is_inside_the_view(win):
    """It is a view switcher for ONE document, not document tabs. Phase 2's
    tab bar goes around the view; this one stays in it."""
    tabs = win.view._tabs
    assert tabs.count() == 2
    assert [tabs.tabText(i) for i in range(2)] == ["Editor", "Organizer"]
    assert tabs.parent() is not None
    assert tabs.window() is win


def test_the_view_holds_its_own_canvas_and_never_shares_it(win, empty_win):
    """PDFCanvas.set_document clears the undo stack, so one canvas can only
    ever serve one document. Two views, two canvases.

    The STACK is no longer per canvas: phase 5 moved it to the window, so these
    two share one only when they are in one window. These are two windows, so
    they do not."""
    assert win.view._canvas is not empty_win.view._canvas
    assert win.view.undo_stack() is not empty_win.view.undo_stack()
    assert win.view.undo_stack() is win.view._canvas.undo_stack
    assert win.view.undo_stack() is win.undo_stack()


def test_a_view_can_be_built_without_a_window_at_all(win):
    """The point of the split: a DocumentView is a widget, not a window part."""
    loose = DocumentView(win.theme_manager())
    try:
        assert not loose.has_document()
        assert loose.document_name() is None
        assert loose._canvas is not win.view._canvas
    finally:
        loose.teardown()
        loose.deleteLater()


# ---------------------------------------------------------------------------
# 3b. The window stopped holding it
# ---------------------------------------------------------------------------

def test_the_window_holds_none_of_the_document_state(win):
    """A window that still owns `_doc` cannot hold two of them, which is the
    whole reason phase 1 exists."""
    still_there = [name for name in _PER_DOCUMENT if hasattr(win, name)]
    assert still_there == []


def test_the_window_source_never_reaches_for_document_internals(win):
    """The attribute check above only sees what __init__ happened to set. This
    reads the file, so a method that pokes at `self._canvas` on some branch no
    test covers is caught too."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "ui" / "main_window.py"
              ).read_text(encoding="utf-8")
    found = [f"self.{name}" for name in _PER_DOCUMENT
             if f"self.{name}" in source]
    assert found == []


def test_the_window_keeps_the_chrome_that_is_genuinely_its_own(win):
    """The other side of the same line. These stayed, deliberately: the page
    box and the fit group are status-bar chrome that rebinds on the front
    view, and the rest is window lifetime."""
    for name in ("_page_jump", "_fit_btns", "_fit_group", "_status",
                 "_update_notice", "_panel_action", "_theme_action",
                 "_force_quit", "_session_ending", "_theme"):
        assert hasattr(win, name), name


def test_the_page_box_is_window_chrome_driven_by_the_view(win):
    """It lives in the status bar and it follows whatever the view is showing."""
    assert win._page_jump in win.statusBar().findChildren(type(win._page_jump))
    win.view._on_page_selected(2)
    assert win._page_jump.current_text() == "3"
    assert win._page_jump.total_text() == "of 3"


def test_the_fit_group_is_window_chrome_that_applies_to_the_view(win):
    """Choosing, remembering and showing the mode are the window's; applying
    it is the canvas's."""
    win.choose_fit_mode("fit_height")
    assert win._fit_btns["fit_height"].isChecked()
    assert win.view._canvas.fit_mode() == "fit_height"


# ---------------------------------------------------------------------------
# 4. The signals the view publishes across the seam
# ---------------------------------------------------------------------------

def test_the_view_publishes_status_lines_instead_of_writing_the_status_bar(win):
    seen = []
    win.view.status_message.connect(seen.append)
    win.view._on_page_selected(1)
    assert seen and "page 2 of 3" in seen[-1]
    assert "page 2 of 3" in win.statusBar().currentMessage()


def test_the_view_publishes_the_page_it_moved_to(win):
    seen = []
    win.view.page_changed.connect(seen.append)
    win.view._on_page_selected(2)
    assert seen[-1] == 2


def test_the_view_publishes_the_title_and_the_window_paints_it(win, pdf_path):
    import os

    seen = []
    win.view.title_changed.connect(lambda: seen.append(win.view.document_name()))
    win.view._mark_dirty()
    assert seen[-1] == os.path.basename(pdf_path)
    assert win.windowTitle().startswith("Rapid PDF - ")
    assert win.isWindowModified()


def test_the_view_publishes_dirty_only_when_it_changes(win):
    seen = []
    win.view.dirty_changed.connect(seen.append)
    win.view._mark_dirty()
    win.view._mark_dirty()          # already dirty, nothing new to say
    assert seen == [True]


def test_the_view_publishes_a_broken_fit_and_the_group_follows(win):
    """The canvas breaks the fit; the button that shows it is on the window,
    so the view republishes the canvas's signal across the seam."""
    win.choose_fit_mode("fit_page")
    assert win._fit_group.checkedButton() is not None
    win.view._canvas.fit_mode_broken.emit()
    assert win._fit_group.checkedButton() is None


def test_opening_a_document_asks_the_window_for_the_default_fit(qt_app, pdf_path):
    """Applying the remembered mode means checking a status-bar button, which
    only the window can do, so the view asks rather than does."""
    window = _build()
    asked = []
    window.view.default_fit_requested.connect(lambda: asked.append(True))
    try:
        window.open_paths([pdf_path])
        assert asked == [True]
        assert window._fit_group.checkedButton() is not None
    finally:
        _dispose(window)


def test_closing_the_pdf_announces_that_the_view_was_asked_to_go(win):
    """Nothing acts on it yet: one view per window and it stays, emptied.
    Phase 2 is where the tab goes with it."""
    seen = []
    win.view.close_requested.connect(lambda: seen.append(True))
    win.close_pdf()
    assert seen == [True]
    assert not win.view.has_document()


def test_nothing_is_announced_when_there_was_nothing_to_close(empty_win):
    seen = []
    empty_win.view.close_requested.connect(lambda: seen.append(True))
    empty_win.close_pdf()
    assert seen == []


# ---------------------------------------------------------------------------
# 5. The window's menu actions still reach the document
# ---------------------------------------------------------------------------

def test_every_file_action_the_menu_calls_lands_on_the_view(win, monkeypatch):
    """The menus stayed on the window, so each one is a hop into the front
    view. Phase 2 changes which view that is and nothing else."""
    calls = []
    for name in ("open_pdf", "combine_pdfs", "save_pdf", "save_pdf_as",
                 "enhance_for_search", "copy_selection", "paste", "paste_image",
                 "delete_current_page", "open_search", "delete_key",
                 "bring_to_front", "send_to_back"):
        monkeypatch.setattr(win.view, name,
                            lambda n=name: calls.append(n) or True)

    win.open_pdf()
    win.combine_pdfs()
    win.save_pdf()
    win.save_pdf_as()
    win.enhance_for_search()
    win.copy_selection()
    win.paste()
    win.paste_image()
    win.delete_current_page()
    win._open_search()
    win._delete_key()
    win._bring_to_front()
    win._send_to_back()

    assert calls == [
        "open_pdf", "combine_pdfs", "save_pdf", "save_pdf_as",
        "enhance_for_search", "copy_selection", "paste", "paste_image",
        "delete_current_page", "open_search", "delete_key",
        "bring_to_front", "send_to_back",
    ]


def test_the_tool_shortcuts_stay_on_the_window_and_reach_the_view(win):
    """Two live views would each own a window-context QShortcut for "v", which
    Qt reports as ambiguous. They stay up here and forward."""
    win.view.trigger_tool("pan")
    assert win.view._canvas.is_panning()
    win.view.trigger_tool("select")
    assert not win.view._canvas.is_panning()


def test_the_page_panel_toggle_stays_on_the_menu_and_moves_the_view(win):
    """isHidden(), not isVisible(): the window is never shown offscreen, so
    every child reports invisible whatever the toggle did."""
    win.page_panel_action().setChecked(False)
    assert win.view._page_panel.isHidden()
    win.page_panel_action().setChecked(True)
    assert not win.view._page_panel.isHidden()


def test_open_paths_no_longer_appends_into_the_open_document(win, tmp_path):
    """CHANGED ON PURPOSE IN PHASE 2, and the point of the phase.

    This test used to assert the opposite: opening a second file appended its
    pages onto the end of the first, taking a three-page document to four. That
    was not a weak test, it was an accurate one, and the behaviour it described
    is the single worst thing the app did. Opening a second PDF silently merged
    it into the one on screen and the next Save wrote the merge over the file.

    A second file is a second tab. The first document is untouched, which is
    the half that matters: `page_count() == 3` on the original view is the
    assertion that would have failed under the old behaviour.
    """
    extra = tmp_path / "one_more.pdf"
    raw = fitz.open()
    raw.new_page(width=400, height=500)
    raw.save(str(extra))
    raw.close()

    first = win.view
    win.open_paths([str(extra)])

    assert win.document_area().count() == 2
    assert first.page_count() == 3, "the open document was appended to"
    assert win.view is not first
    assert win.view.page_count() == 1
