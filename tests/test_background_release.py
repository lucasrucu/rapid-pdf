"""What a backgrounded tab gives back. Phase 3 of docs/tabs-plan.md.

PHASE 2 MEASURED THE COST and it decided the shape of this. One document's
six-entry pixmap cache is 207 MB at A1 and zoom 1.5; ten live fitz documents
plus twenty markup-baked clones came to 2 MB between them. So the cache is the
entire cost and the clones are rounding, which is why `invalidate_render_cache`
is the first thing backgrounding does and the clones are tidiness.

PHASE 3 MEASURED THE SAVING (tools/measure_tab_memory.py, six A1 tabs, every
page turned to so every cache is full):

    holding everything, as before this phase          +1249 MB
    releasing on background                            +387 MB
    saved                                              +863 MB, 173 MB a tab

THE 173 RATHER THAN 207 IS THE INTERESTING PART, and it is why the measurement
was worth running rather than quoting the arithmetic. The canvas scene holds
the page currently on screen as its background item, and QPixmap is implicitly
shared, so the cache entry for that page and the scene item are the same
memory. Dropping the cache frees the five entries nobody is holding and leaves
the sixth. The corollary is the case the first measurement accidentally ran: a
tab where only ONE page was ever rendered saves nothing at all, because its one
cache entry is the page the scene is holding. The saving is real for someone
reading through a drawing set, which is the case that produced the number.

What deliberately stays: the live fitz document and the canvas scene. Those are
what make a switch back instant and what phase 1's finding 2 proved survive a
move between windows.

Releasing clones is also what made known bug 6 fire on every switch instead of
intermittently. That bug and its fix are in tests/test_organizer_clone_lifecycle.py.
"""

import fitz
import pytest

from PySide6.QtWidgets import QApplication, QMessageBox

from core.settings import Settings, set_settings
from ui.main_window import MainWindow


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


def _pdf(tmp_path, name, pages=8):
    path = tmp_path / name
    raw = fitz.open()
    for i in range(pages):
        page = raw.new_page(width=595, height=842)
        page.insert_text((40, 120), f"{name} page {i}", fontsize=28)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def first(tmp_path):
    return _pdf(tmp_path, "first.pdf")


@pytest.fixture
def second(tmp_path):
    return _pdf(tmp_path, "second.pdf")


@pytest.fixture
def win(qt_app, store):
    window = MainWindow()
    window.resize(1200, 800)
    yield window
    for view in window.document_area().views():
        view.mark_clean()
    window._force_quit = True
    window.close()


def _render_now(view):
    """The canvas render is debounced and offscreen never runs the loop."""
    view._canvas._flush_pending_render()


# ---------------------------------------------------------------------------
# The pixmap cache, which is the whole saving
# ---------------------------------------------------------------------------

def test_backgrounding_a_tab_drops_its_pixmap_cache(win, first, second):
    """THE HEADLINE. Measured at 173 MB a tab, by one call that cannot fail."""
    win.open_paths([first])
    background = win.view
    _render_now(background)
    assert background._doc._render_cache, "nothing was cached to drop"

    win.open_paths([second])          # the first tab is now behind this one

    assert background.is_active() is False
    assert background._doc._render_cache == {}
    assert win.view is not background
    assert win.view.is_active() is True


def test_the_document_and_the_scene_survive_backgrounding(win, first, second):
    """What deliberately STAYS. These are what make a switch back instant and
    what finding 2 proved survive a move between windows; the cache is not."""
    win.open_paths([first])
    background = win.view
    _render_now(background)
    fitz_doc = background._doc.doc
    scene = background._canvas.scene()
    items = len(scene.items())

    win.open_paths([second])

    assert background._doc.doc is fitz_doc
    assert background._doc.is_open()
    assert background._canvas.scene() is scene
    assert len(scene.items()) == items
    assert background.page_count() == 8


def test_backgrounding_releases_both_markup_clones(win, first, second):
    win.open_paths([first])
    background = win.view
    background._tabs.setCurrentIndex(1)      # build the Organizer's clone
    assert background._org_render is not None
    assert background._panel_render is not None

    win.open_paths([second])

    assert background._org_render is None
    assert background._panel_render is None


def test_coming_back_to_the_front_rebuilds_what_was_released(
        win, first, second):
    win.open_paths([first, second])
    area = win.document_area()
    background = area.view_at(0)
    assert background._panel_render is None

    area.set_current_index(0)

    assert background.is_active() is True
    assert background._panel_render is not None
    assert background._panel_render.is_open()


def test_a_backgrounded_tab_is_not_asked_to_rebuild_twice(win, first, second):
    """`set_active` is a no-op when the answer is already the one being set,
    which is what keeps a stack of switches from cloning the document once per
    signal that happens to pass through."""
    win.open_paths([first, second])
    front = win.view
    built = []
    original = type(front)._make_markup_baked_render
    type(front)._make_markup_baked_render = lambda self: built.append(1) or original(self)
    try:
        front.set_active(True)
        front.set_active(True)
        assert built == []
    finally:
        type(front)._make_markup_baked_render = original


def test_ten_tabs_keep_one_render_cache_between_them(win, tmp_path):
    """The shape of the saving, asserted rather than measured: whatever the
    cache costs, exactly one document is paying it."""
    paths = [_pdf(tmp_path, f"drawing{i}.pdf", pages=2) for i in range(10)]
    win.open_paths(paths)
    for view in win.document_area().views():
        _render_now(view)

    holding = [v for v in win.document_area().views() if v._doc._render_cache]

    assert win.document_area().count() == 10
    assert holding == [win.view]

