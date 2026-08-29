"""Several windows, and moving a live document between them. Phase 3.

WHAT EACH SECTION IS FOR.

1. THE REGISTRY. Who is open, who was touched last, and who decides the app is
   finished. Activation order is the part that has no other source of truth:
   `QApplication.activeWindow()` is None the moment a dialog or another
   application has focus, so the order is recorded as it happens and read back.

2. ADOPTION. The mechanism the tear-off will drive, exercised through the menu
   item instead of a gesture. This is the whole reason phase 3 exists before
   phase 4: a menu item can be driven by a test and a mouse drag across two
   top-level windows cannot.

3. THE REPARENT, RE-VERIFIED. Phase 1 measured that moving a live PDFCanvas
   between two QMainWindows keeps the same scene, the same undo stack and the
   same viewport, with `internalWinId()` never leaving 0. That measurement was
   of a bare canvas in a layout; these assert the same properties across
   `MainWindow.adopt`, which is the real path. It holds because the canvas is a
   raster QGraphicsView with no OpenGL viewport, so there is no native window
   handle to destroy on a reparent. See the standing constraint in
   docs/tabs-plan.md.

4. ROUTING. Where a file arriving from the shell lands with N windows open.

5. THE FILE DROP, which did not exist anywhere in the app before this phase.

Offscreen (see conftest) never runs the event loop and lies about geometry, so
nothing here waits on a signal round trip and nothing asserts on a position.
Window ACTIVATION is part of what offscreen will not do, which is exactly why
`note_activated` is a method the window calls rather than something read off
Qt: the test drives the same entry point `changeEvent` does.
"""

import fitz
import pytest

from PySide6.QtCore import QPointF, QRectF, QUrl
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QMessageBox

from core.settings import Settings, set_settings
from ui.canvas import AddItemsCommand, HighlightItem
from ui.main_window import MainWindow
from ui.window_registry import WindowRegistry


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    s = Settings(tmp_path / "settings.json", debounce_ms=0, migrate_legacy=False)
    # The "N documents are open" question belongs to test_document_tabs; here
    # it is only ever in the way of a teardown. Phase 2 pinned its behaviour.
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


@pytest.fixture
def registry():
    """The registry every window in this test joins.

    conftest already hands each test a fresh one with the quit disarmed; this
    just names it. A test that wants to watch the quit turns it back on.
    """
    return WindowRegistry.instance()


def _pdf(tmp_path, name, pages=1):
    path = tmp_path / name
    raw = fitz.open()
    for i in range(pages):
        page = raw.new_page(width=400, height=500)
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


@pytest.fixture
def gamma(tmp_path):
    return _pdf(tmp_path, "gamma.pdf", pages=4)


@pytest.fixture
def win(qt_app, store, registry):
    """One window, joined to the registry, torn down with everything it spawned."""
    window = MainWindow()
    window.resize(1200, 800)
    yield window
    # Every window this test made, not just this one. Marked clean first: a
    # dirty document puts up a real save prompt, and `never_opens_a_dialog`
    # turns that into a failure attributed to the teardown.
    for other in registry.windows():
        for view in other.document_area().views():
            view.mark_clean()
        other._force_quit = True
        other.close()


# ---------------------------------------------------------------------------
# 1. The registry
# ---------------------------------------------------------------------------

def test_a_window_joins_the_registry_when_it_is_built(win, registry):
    assert registry.windows() == [win]
    assert registry.active_window() is win


def test_windows_come_back_in_activation_order_most_recent_first(win, registry):
    """Not creation order. "The active window" is the one a file should land
    in, and that is whichever was touched last."""
    second = win.new_window()
    third = win.new_window()
    assert registry.windows() == [third, second, win]

    registry.note_activated(win)
    assert registry.windows() == [win, third, second]
    assert registry.active_window() is win

    registry.note_activated(second)
    assert registry.windows() == [second, win, third]


def test_activating_a_window_it_does_not_know_changes_nothing(win, registry):
    stranger = MainWindow()
    registry.unregister(stranger)
    order = registry.windows()
    registry.note_activated(stranger)
    assert registry.windows() == order
    stranger._force_quit = True
    stranger.close()


def test_closing_a_window_takes_it_out_of_the_registry(win, registry):
    second = win.new_window()
    assert len(registry.windows()) == 2
    second._force_quit = True
    second.close()
    assert registry.windows() == [win]


def test_the_last_window_closing_quits_the_application(win, registry, monkeypatch):
    """THE ONE PLACE APP LIFETIME IS DECIDED. `main.py` turns Qt's own
    quit-on-last-window off so that this runs instead, which is what makes a
    torn-off window closing while its parent lives need no special case."""
    quits = []
    monkeypatch.setattr(QApplication, "quit",
                        lambda *a: quits.append(True))
    registry.quit_on_last_window = True
    ended = []
    registry.last_window_closed.connect(lambda: ended.append(True))

    second = win.new_window()
    second._force_quit = True
    second.close()
    assert quits == [], "a window closing while another lives must not quit"
    assert ended == []

    win._force_quit = True
    win.close()
    assert ended == [True]
    assert quits == [True]


def test_the_registry_purges_a_window_that_died_without_unregistering(
        win, registry):
    """The backstop. A Python reference to a window whose C++ half has gone is
    the classic window-will-not-die bug, so `destroyed` purges as well."""
    from shiboken6 import delete

    second = win.new_window()
    assert len(registry.windows()) == 2
    delete(second)                  # straight to the C++ destructor
    assert registry.windows() == [win]


def test_views_walks_every_document_in_every_window(win, registry, alpha, beta):
    win.open_paths([alpha])
    second = win.new_window()
    second.open_paths([beta])
    registry.note_activated(win)
    pairs = list(registry.views())
    assert [w for w, _ in pairs] == [win, second]
    assert [v.document_path() for _, v in pairs] == [alpha, beta]


def test_dirty_views_reports_across_windows(win, registry, alpha, beta):
    win.open_paths([alpha])
    second = win.new_window()
    second.open_paths([beta])
    assert registry.dirty_views() == []
    second.view._mark_dirty()
    assert [v.document_path() for _, v in registry.dirty_views()] == [beta]


# ---------------------------------------------------------------------------
# 2. Adoption: moving a live document into another window
# ---------------------------------------------------------------------------

def test_move_to_new_window_takes_the_tab_with_it(win, registry, alpha, beta):
    win.open_paths([alpha, beta])
    moving = win.document_area().view_at(0)
    assert win.document_area().count() == 2

    target = win.move_view_to_new_window(moving)

    assert target is not None and target is not win
    assert registry.count() == 2
    assert win.document_area().count() == 1
    assert target.document_area().count() == 1
    assert target.document_area().view_at(0) is moving
    assert target.view is moving
    win.document_area().check_invariant()
    target.document_area().check_invariant()


def test_the_moved_document_is_the_same_object_not_a_reopened_copy(
        win, alpha, beta):
    win.open_paths([alpha, beta])
    moving = win.document_area().view_at(0)
    doc_before = moving._doc
    fitz_before = moving._doc.doc

    target = win.move_view_to_new_window(moving)

    assert target.view._doc is doc_before
    assert target.view._doc.doc is fitz_before
    assert target.view._doc.is_open(), "the document was closed by the move"
    assert target.view.page_count() == 3


def test_the_window_left_behind_keeps_its_other_document(win, alpha, beta):
    win.open_paths([alpha, beta])
    stays = win.document_area().view_at(1)
    win.move_view_to_new_window(win.document_area().view_at(0))
    assert win.document_area().views() == [stays]
    assert win.view is stays
    assert stays.document_path() == beta
    assert stays._doc.is_open()


def test_the_source_window_closes_when_its_last_document_leaves(
        win, registry, alpha):
    """The general mechanism, which the menu item deliberately never reaches
    (it is disabled on a lone tab). Phase 4's tear-off will."""
    win.open_paths([alpha])
    target = registry.create_window(theme=win.theme_manager(), show=False)
    target.show()
    moved = win.move_view_to_window(win.document_area().view_at(0), target)
    assert moved
    assert target.document_area().count() == 1
    assert registry.windows() == [target]


def test_a_moved_view_answers_to_its_new_window(win, alpha, beta, gamma):
    """The two signals `_new_view` binds for the life of a view are decisions
    about TABS, so they have to follow the view to whichever window owns its
    tabs now. Leave them on the old one and opening a file from the moved
    document's toolbar puts a tab in a window it is not in."""
    win.open_paths([alpha, beta])
    moving = win.document_area().view_at(0)
    target = win.move_view_to_new_window(moving)

    moving.paths_requested.emit([gamma])

    assert target.document_area().count() == 2
    assert [v.document_path() for v in target.document_area().views()] == [
        alpha, gamma]
    assert win.document_area().count() == 1


def test_the_source_window_stops_driving_a_view_that_left(win, alpha, beta):
    """A background document writing another window's status bar is the whole
    class of bug the chrome rebinding exists to stop, and a MOVED document is
    the version of it that crosses windows."""
    win.open_paths([alpha, beta])
    moving = win.document_area().view_at(0)
    target = win.move_view_to_new_window(moving)

    win._status.showMessage("left alone")
    moving.status_message.emit("from the moved document")

    assert win._status.currentMessage() == "left alone"
    assert target._status.currentMessage() == "from the moved document"


def test_undo_in_the_new_window_touches_only_the_document_that_moved(
        win, alpha, beta):
    """`createUndoAction` binds an action to ONE stack for its life, so the new
    window has to build its own pair against the arriving stack."""
    win.open_paths([alpha, beta])
    moving = win.document_area().view_at(0)
    stays = win.document_area().view_at(1)
    target = win.move_view_to_new_window(moving)

    canvas = moving._canvas
    item = HighlightItem(QRectF(20, 20, 50, 50), QColor("yellow"), 0.5, 0)
    canvas._attach_item(item)
    canvas.undo_stack.push(AddItemsCommand(canvas, [item]))

    assert moving.undo_stack().count() == 1
    assert stays.undo_stack().count() == 0
    assert target._undo_action is not None
    assert target._undo_action.isEnabled()

    target._undo_action.trigger()

    assert moving.undo_stack().count() == 1
    assert moving.undo_stack().index() == 0
    assert item.scene() is None
    assert stays.undo_stack().count() == 0


# ---------------------------------------------------------------------------
# 3. The reparent, re-verified across adopt
# ---------------------------------------------------------------------------

def test_adopting_a_view_reparents_it_and_recreates_nothing(win, alpha, beta):
    """PHASE 1'S FINDING 2, RE-RUN ON THE REAL PATH.

    Phase 1 measured a bare PDFCanvas moved between two QMainWindow layouts.
    This is the same measurement across `MainWindow.adopt`, which is what the
    menu item and (later) the tear-off actually call. Every identity below has
    to survive, because each one is a thing that would be silently rebuilt if
    the view took a trip through top-level: `setParent(None)` promotes a widget
    to a window with a real HWND on Windows, and reparenting it back into a
    layout destroys that HWND and the widget's native resources with it.

    `internalWinId() == 0` is the assertion that says it never happened. It
    holds because PDFCanvas is a raster QGraphicsView whose viewport is a plain
    QWidget: no OpenGL, so no native window handle. Put an OpenGL viewport on
    the canvas and this test is the one that has to be re-run before believing
    anything above it (see the standing constraint in docs/tabs-plan.md).
    """
    win.open_paths([alpha, beta])
    moving = win.document_area().view_at(0)
    moving.jump_to_page(1)

    canvas = moving._canvas
    before = {
        "scene": canvas.scene(),
        "viewport": canvas.viewport(),
        "items": len(canvas.scene().items()),
        "page": moving.current_page(),
        "fitz": moving._doc.doc,
        "win_id": canvas.internalWinId(),
        "view_win_id": moving.internalWinId(),
    }
    assert before["win_id"] == 0, "already native before the move"

    target = win.move_view_to_new_window(moving)

    assert canvas.scene() is before["scene"]
    assert canvas.viewport() is before["viewport"]
    # NOT the undo stack, and phase 5 is why. It is the WINDOW's now, shared by
    # every tab in it, so a view arriving in another window necessarily joins
    # that window's history (ui/undo.py). Everything else finding 2 measured
    # still has to survive, which is what the rest of this list is.
    assert canvas.undo_stack is target.undo_stack()
    assert len(canvas.scene().items()) == before["items"]
    assert moving.current_page() == before["page"] == 1
    assert moving._doc.doc is before["fitz"]
    assert moving._doc.is_open()
    assert canvas.internalWinId() == 0, "the canvas grew a native handle"
    assert moving.internalWinId() == 0, "the view grew a native handle"
    assert moving.window() is target
    assert canvas.window() is target


def test_the_view_is_never_left_without_a_parent_during_a_move(win, alpha, beta):
    """The order the whole move hangs on: the destination adopts BEFORE the
    source lets go, so there is no instant at which the view is a top-level."""
    win.open_paths([alpha, beta])
    moving = win.document_area().view_at(0)
    parents = []
    original_adopt = MainWindow.adopt

    def watching_adopt(self, view, at=-1):
        parents.append(view.parent())
        result = original_adopt(self, view, at)
        parents.append(view.parent())
        return result

    MainWindow.adopt = watching_adopt
    try:
        win.move_view_to_new_window(moving)
    finally:
        MainWindow.adopt = original_adopt

    assert parents[0] is not None, "the view was parentless on the way in"
    assert parents[1] is not None
    assert parents[0] is not parents[1], "it never actually moved"
    assert moving.parent() is not None


# ---------------------------------------------------------------------------
# 4. Routing an arriving batch
# ---------------------------------------------------------------------------

def test_routing_with_no_windows_open_makes_one(qt_app, store, registry, alpha):
    """Not hypothetical. `setQuitOnLastWindowClosed(False)` means a running
    process with every window closed is a state that exists, and a launch
    forwarded into it has to produce a window rather than nothing."""
    assert registry.windows() == []
    landed = registry.route_open([alpha], combine=False)
    assert landed is not None
    assert registry.windows() == [landed]
    assert landed.view.document_path() == alpha
    landed._force_quit = True
    landed.close()


def test_a_file_already_open_activates_its_tab_instead_of_opening_it_twice(
        win, registry, alpha, beta):
    """`find_by_path`, which is the multi-window version of the duplicate check
    phase 2 put in `DocumentArea.index_of_path`."""
    win.open_paths([alpha, beta])
    second = win.new_window()           # empty, and the one that would take it
    registry.note_activated(second)

    landed = registry.route_open([alpha], combine=False)

    assert landed is win, "the file's own window should have been raised"
    assert win.document_area().count() == 2, "a duplicate tab was opened"
    assert win.view.document_path() == alpha
    assert second.document_area().count() == 1
    assert not second.view.has_document()


def test_a_new_file_lands_in_the_window_touched_last_not_the_oldest(
        win, registry, alpha, beta):
    win.open_paths([alpha])
    second = win.new_window()
    registry.note_activated(second)

    registry.route_open([beta], combine=False)

    assert second.view.document_path() == beta
    assert win.document_area().count() == 1
    assert win.view.document_path() == alpha


def test_a_bare_relaunch_raises_the_active_window_and_opens_nothing(
        win, registry, alpha):
    win.open_paths([alpha])
    second = win.new_window()
    registry.note_activated(second)
    landed = registry.route_open([], combine=False)
    assert landed is second
    assert second.document_area().count() == 1
    assert not second.view.has_document()


def test_combine_is_still_decided_by_the_verb_and_parented_to_the_active_window(
        win, registry, alpha, beta, monkeypatch):
    staged = []
    monkeypatch.setattr(MainWindow, "combine_paths",
                        lambda self, paths: staged.append((self, list(paths))))
    second = win.new_window()
    registry.note_activated(second)

    registry.route_open([alpha, beta], combine=True)

    assert staged == [(second, [alpha, beta])]


def test_routing_a_mix_puts_the_new_ones_together_and_raises_the_open_one(
        win, registry, alpha, beta, gamma):
    win.open_paths([alpha])
    second = win.new_window()
    registry.note_activated(second)

    registry.route_open([beta, alpha, gamma], combine=False)

    assert [v.document_path() for v in second.document_area().views()] == [
        beta, gamma]
    assert [v.document_path() for v in win.document_area().views()] == [alpha]


# ---------------------------------------------------------------------------
# 5. PDFs dropped on the window from the shell
# ---------------------------------------------------------------------------

class _Urls:
    """The one thing dropped_pdfs reads. A real QMimeData would do, but this
    says what the discrimination is actually made on."""

    def __init__(self, urls):
        self._urls = urls

    def hasUrls(self):
        return bool(self._urls)

    def urls(self):
        return self._urls


def test_a_dropped_pdf_is_recognised(win, alpha):
    assert win.dropped_pdfs(_Urls([QUrl.fromLocalFile(alpha)])) == [alpha]


def test_a_drag_carrying_no_urls_is_not_a_file_drop(win):
    """THE WHOLE COLLISION GUARD. The two internal page drags carry item-model
    data and no urls at all, and `text/uri-list` only ever appears on a drag
    that came from the shell. So the three gestures in the plan's table stay
    apart without anybody checking where the drag started."""
    assert win.dropped_pdfs(_Urls([])) == []
    assert win.dropped_pdfs(None) == []


def test_dropping_something_that_is_not_a_pdf_is_ignored(win, tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("hello")
    assert win.dropped_pdfs(_Urls([QUrl.fromLocalFile(str(other))])) == []


def test_dropping_a_pdf_opens_it_as_a_new_tab_and_never_appends(
        win, alpha, beta):
    """The rule this gesture would break most easily: dropping a file onto a
    page looks like it should go into that page. It does not."""
    win.open_paths([alpha])
    pages_before = win.view.page_count()

    win.dropEvent(_Drop(_Urls([QUrl.fromLocalFile(beta)])))

    assert win.document_area().count() == 2
    assert [v.document_path() for v in win.document_area().views()] == [
        alpha, beta]
    assert win.document_area().view_at(0).page_count() == pages_before
    win.document_area().check_invariant()


def test_dropping_a_file_that_is_already_open_activates_its_tab(
        win, alpha, beta):
    win.open_paths([alpha, beta])
    win.document_area().set_current_index(1)

    win.dropEvent(_Drop(_Urls([QUrl.fromLocalFile(alpha)])))

    assert win.document_area().count() == 2
    assert win.view.document_path() == alpha


def test_a_drop_lands_in_the_window_it_was_dropped_on(win, alpha, beta):
    """Not the active one. A drop names its target by where the cursor was."""
    second = win.new_window()
    second.open_paths([alpha])
    win.raise_and_focus()           # win is now the active window

    second.dropEvent(_Drop(_Urls([QUrl.fromLocalFile(beta)])))

    assert second.document_area().count() == 2
    assert win.document_area().count() == 1
    assert not win.view.has_document()


class _Drop:
    """Just enough QDropEvent for dropEvent: the payload, and whether it was
    taken. Building a real one needs a live drag manager the platform owns."""

    def __init__(self, mime):
        self._mime = mime
        self.accepted = False

    def mimeData(self):
        return self._mime

    def acceptProposedAction(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False
