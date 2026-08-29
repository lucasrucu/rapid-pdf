"""Known bug 6: a markup-baked clone closed while something is still reading it.

THE BUG. `_refresh_organizer` closed the previous clone as its FIRST statement
and only built the replacement several lines later, with a
`QApplication.processEvents()` in between. `PageOrganizer.refresh` queues
`_render_visible` on a zero timer, so that pump ran a render against a fitz
document that had just been closed and PyMuPDF raised "document closed". The
FIRST switch into the Organizer was clean, because there was no previous clone
to close; the second threw, every time a render was still queued. Measured on
`main` before phase 1, so it is nothing the tabs work introduced.

WHY IT IS INVISIBLE, and therefore why these tests are shaped the way they are.
The render runs from a queued Qt callback, so by the time PyMuPDF raises, the
exception is inside a slot and PySide has printed it to stderr and carried on.
Nothing fails, no dialog appears, the grid just silently keeps whatever it had.
So what is asserted here is THE STATE EVERY RENDER SAW WHEN IT STARTED, not
whether an exception arrived somewhere it could be caught.

The line it actually raised from is worth knowing, because it looks harmless:

    if not src or not src.doc:      # ui/organizer.py

PyMuPDF's Document defines `__len__`, so truth-testing a CLOSED document raises
rather than answering False. `core.pdf_document.source_is_readable` is the
question that can be asked safely, and every lazy renderer asks it now.

THE FIX IS THE ORDER: build the new clone, hand it over, and only then close the
old one, so nothing is ever pointing at a closed document whatever runs in
between. Phase 3 releases clones whenever a tab is backgrounded, which is the
same sequence run far more often, which is why this had to be fixed with it.
"""

import fitz
import pytest

from PySide6.QtWidgets import QApplication, QMessageBox

from core.pdf_document import source_is_readable
from core.settings import Settings, set_settings
from ui.main_window import MainWindow
from ui.organizer import PageOrganizer
from ui.page_panel import PagePanel


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
    for name in ("question", "warning", "critical", "information", "about"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, n=name, **k: pytest.fail(
                f"QMessageBox.{n} opened: {a[1:3]}")))


@pytest.fixture
def drawing(tmp_path):
    path = tmp_path / "drawing.pdf"
    raw = fitz.open()
    for i in range(8):
        page = raw.new_page(width=595, height=842)
        page.insert_text((40, 120), f"page {i}", fontsize=28)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def win(qt_app, store):
    window = MainWindow()
    window.resize(1200, 800)
    yield window
    for view in window.document_area().views():
        view.mark_clean()
    window._force_quit = True
    window.close()


@pytest.fixture
def render_watch(monkeypatch):
    """Record what every lazy thumbnail render saw when it started.

    A False in the list means a render ran against a document that had been
    closed out from under it, which is the defect itself rather than a symptom
    of it.
    """
    seen = []

    def watched(original, source_of):
        def call(self):
            source = source_of(self)
            if source is not None:
                seen.append(source_is_readable(source))
            try:
                return original(self)
            except Exception as exc:            # pragma: no cover - the bug
                seen.append(f"raised: {exc}")
                raise
        return call

    monkeypatch.setattr(
        PageOrganizer, "_render_visible",
        watched(PageOrganizer._render_visible, lambda s: s._render))
    monkeypatch.setattr(
        PagePanel, "_render_visible",
        watched(PagePanel._render_visible, lambda s: s._render))
    return seen


def test_the_second_switch_into_the_organizer_never_renders_a_closed_clone(
        win, drawing, render_watch):
    """THE BUG ITSELF. This fails against the code before the fix.

    Deliberately no `processEvents` between the switches: the queued render
    from the first switch has to still be pending when the second one runs,
    because that is the whole bug.
    """
    win.open_paths([drawing])
    tabs = win.view._tabs

    tabs.setCurrentIndex(1)         # into the Organizer: clone A, render queued
    tabs.setCurrentIndex(0)         # back to the Editor
    tabs.setCurrentIndex(1)         # and in again: clone A closed, B built

    assert render_watch, "no lazy render ran, so this proves nothing"
    assert all(state is True for state in render_watch), (
        f"a thumbnail rendered from a closed document: {render_watch}")
    assert win.view._org_render is not None
    assert win.view._org_render.is_open()


def test_switching_in_and_out_repeatedly_keeps_exactly_one_clone(
        win, drawing, render_watch):
    """The leak the pairing exists to stop, checked alongside the bug it hides
    behind: a missing close is a whole fitz document per switch."""
    win.open_paths([drawing])
    tabs = win.view._tabs
    clones = []
    for _ in range(5):
        tabs.setCurrentIndex(1)
        clones.append(win.view._org_render)
        tabs.setCurrentIndex(0)

    assert all(state is True for state in render_watch), render_watch
    assert len({id(c) for c in clones}) == 5, "the clone was not rebuilt"
    assert clones[-1].is_open()
    assert not any(c.is_open() for c in clones[:-1]), "an old clone was left open"


def test_a_released_clone_leaves_the_grid_on_the_live_document(win, drawing):
    """Releasing is not the same as replacing. `set_document(doc, None)` would
    rebuild every thumbnail off the live document; the pointer is dropped and
    nothing is redrawn, and a cell scrolled in afterwards falls back to the
    live document, which is still open."""
    win.open_paths([drawing])
    view = win.view
    view._tabs.setCurrentIndex(1)
    clone = view._org_render
    assert view._organizer._render is clone

    view._close_org_render()

    assert view._organizer._render is None
    assert view._org_render is None
    assert not clone.is_open()
    assert view._doc.is_open(), "the LIVE document must not have been closed"


def test_release_leaves_a_newer_clone_alone(win, drawing):
    """The guard on `release_render_source`. A refresh may already have swapped
    a newer clone in, and clearing that would blank thumbnails a render has
    just produced."""
    win.open_paths([drawing])
    view = win.view
    view._tabs.setCurrentIndex(1)
    current = view._organizer._render

    view._organizer.release_render_source(object())

    assert view._organizer._render is current


def test_a_closed_document_answers_is_readable_rather_than_raising(tmp_path):
    """THE LINE THE BUG RAISED FROM. `if not src.doc` looks like a None check
    and is not: PyMuPDF's Document defines __len__, so truth-testing a closed
    one raises "document closed"."""
    path = tmp_path / "one.pdf"
    raw = fitz.open()
    raw.new_page(width=200, height=200)
    raw.save(str(path))
    raw.close()

    from core.pdf_document import PDFDocument

    doc = PDFDocument()
    doc.open(str(path))
    assert source_is_readable(doc)

    # Closed underneath rather than through PDFDocument.close(), which also
    # drops the reference: this is the state a released clone is left in, and
    # the one the queued render used to walk into.
    doc.doc.close()
    with pytest.raises(ValueError):
        bool(doc.doc)               # the old guard, verbatim
    assert source_is_readable(doc) is False
    assert source_is_readable(None) is False
