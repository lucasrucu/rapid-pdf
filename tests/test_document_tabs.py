"""Several documents in one window, one tab each. Phase 2 of docs/tabs-plan.md.

WHAT EACH SECTION IS FOR.

1. THE INVARIANT. A QTabBar over a QStackedWidget is two objects that have to
   stay index-parallel, and nothing in Qt keeps them that way for us. That is
   the price of not using QTabWidget (phase 4's tear-off needs mid-drag states
   QTabWidget forbids), so `check_invariant()` is asserted after every single
   operation below.

2. THE APPEND IS GONE. The reason this phase exists. Opening a second PDF used
   to merge it into the one on screen. Every test here asserts on the FIRST
   document as well as the new one, because "the new tab has one page" would
   pass even if the first had been appended to.

3. TITLES. Basenames until two of them collide, then as much path as it takes.

4. CLOSING. Which tab, which prompt, and when the window goes with it.

5. REBINDING ON A SWITCH. Where the bugs in this phase were always going to be.
   The Edit menu's Undo is built from ONE undo stack and there is one stack per
   document, so it has to be rebuilt; the status bar, the page box and the title
   all follow the front view and have to stop following the one that left.

Offscreen (see conftest) never runs the event loop and lies about geometry, so
nothing here waits on a signal round trip and nothing asserts on a width.
"""

import fitz
import pytest

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from core.settings import Settings, set_settings
from ui.document_area import DocumentArea, tab_titles
from ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    s = Settings(tmp_path / "settings.json", debounce_ms=0, migrate_legacy=False)
    previous = set_settings(s)
    yield s
    set_settings(previous)


@pytest.fixture(autouse=True)
def never_opens_a_dialog(monkeypatch):
    """Offscreen still runs a real modal loop, so an unexpected message box
    hangs the suite instead of failing it. Each test that WANTS a prompt puts
    its own answer in with `_answer_with`."""
    for name in ("question", "warning", "critical", "information", "about"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, n=name, **k: pytest.fail(
                f"QMessageBox.{n} opened: {a[1:3]}")))


def _answer_with(monkeypatch, button):
    """Answer the next prompts with one button, and record the text of each.

    The text, not the title: what is asserted below is which QUESTION was put,
    and two different dialogs can share a title.
    """
    asked = []

    def fake_question(*args, **kwargs):
        asked.append(args[2] if len(args) > 2 else "")
        return button

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    return asked


def _pdf(tmp_path, name, pages=1, width=400, height=500):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = fitz.open()
    for i in range(pages):
        page = raw.new_page(width=width, height=height)
        page.insert_text((20, 100), f"{name} p{i}", fontsize=24)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def three_pages(tmp_path):
    return _pdf(tmp_path, "three.pdf", pages=3)


@pytest.fixture
def one_page(tmp_path):
    return _pdf(tmp_path, "one.pdf", pages=1)


def _build():
    window = MainWindow()
    window.view._canvas.resize(600, 700)
    window.view._canvas._flush_pending_render()
    return window


def _dispose(window):
    """Put a window away without leaving a trap for the next test.

    Same reasoning as test_document_view_split._dispose, applied to every tab:
    deleteLater needs an event loop nobody runs offscreen, so a view outlives
    the test with a render source and a queued render behind it, and the next
    QApplication.processEvents() would pump it against a closed document.
    """
    for view in window.document_area().views():
        view.clear_document()
        view.teardown()
    window._force_quit = True
    window.close()
    window.deleteLater()


@pytest.fixture
def win(qt_app, store):
    window = _build()
    yield window
    _dispose(window)


@pytest.fixture
def area(win) -> DocumentArea:
    return win.document_area()


def _edit_menu(window) -> QMenu:
    for menu in window.menuBar().findChildren(QMenu):
        if menu.title() == "Edit":
            return menu
    raise AssertionError("no Edit menu")


def _menu_action(window, menu_title, action_text):
    for menu in window.menuBar().findChildren(QMenu):
        if menu.title() == menu_title:
            for action in menu.actions():
                if action.text() == action_text:
                    return action
    return None


def _undo_action(window):
    for action in _edit_menu(window).actions():
        if action.text().startswith("Undo"):
            return action
    raise AssertionError("no Undo action on the Edit menu")


def _mouse(canvas, kind, scene_pt, button=Qt.MouseButton.LeftButton):
    vp = canvas.mapFromScene(scene_pt)
    held = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else button
    return QMouseEvent(kind, QPointF(vp), QPointF(vp), button, held,
                       Qt.KeyboardModifier.NoModifier)


def _draw_a_rectangle(view):
    """Pick the rect tool off the real toolbar and drag one out on the canvas."""
    canvas = view._canvas
    canvas.resize(600, 700)
    canvas._flush_pending_render()
    view.trigger_tool("rect")
    start = QPointF(60 * canvas._zoom, 60 * canvas._zoom)
    end = QPointF(180 * canvas._zoom, 150 * canvas._zoom)
    canvas.mousePressEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonPress, start))
    canvas.mouseMoveEvent(_mouse(canvas, QMouseEvent.Type.MouseMove, end))
    canvas.mouseReleaseEvent(_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, end))


def _items(view, page=0):
    return list(view._canvas._page_annotations.get(page, []))


# ---------------------------------------------------------------------------
# 1. The bar and the stack stay index-parallel
# ---------------------------------------------------------------------------

def test_a_fresh_window_has_one_empty_tab_and_no_bar(win, area):
    """One empty tab IS what "no document open" looks like. A bar carrying a
    single blank tab would be worse than no bar, so it stays hidden until
    there is something to name."""
    area.check_invariant()
    assert area.count() == 1
    assert not area.current_view().has_document()
    assert area._header.isHidden()


def test_every_operation_keeps_the_bar_and_the_stack_in_step(win, area, tmp_path):
    a = _pdf(tmp_path, "a.pdf")
    b = _pdf(tmp_path, "b.pdf")
    c = _pdf(tmp_path, "c.pdf")

    win.open_paths([a, b, c])
    area.check_invariant()
    assert area.count() == 3

    area.bar().moveTab(0, 2)
    area.check_invariant()

    win.close_tab(1)
    area.check_invariant()
    assert area.count() == 2

    win.new_tab()
    area.check_invariant()
    assert area.count() == 3


def test_dragging_a_tab_moves_its_document_with_it(win, area, tmp_path):
    """The bar reorders itself; the stack has to follow or every tab shows the
    wrong document."""
    a = _pdf(tmp_path, "a.pdf", pages=1)
    b = _pdf(tmp_path, "b.pdf", pages=2)
    win.open_paths([a, b])

    first = area.view_at(0)
    second = area.view_at(1)
    area.bar().moveTab(0, 1)

    assert area.view_at(0) is second
    assert area.view_at(1) is first
    assert area.bar().tabText(0) == "b"
    assert area.bar().tabText(1) == "a"
    area.check_invariant()


def test_the_stack_page_always_matches_the_current_tab(win, area, tmp_path):
    win.open_paths([_pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")])
    for index in (0, 1, 0):
        area.set_current_index(index)
        assert area.stack().currentIndex() == area.bar().currentIndex() == index
        assert area.current_view() is area.view_at(index)
        assert win.view is area.view_at(index)


# ---------------------------------------------------------------------------
# 2. A second file is a second tab, not an append
# ---------------------------------------------------------------------------

def test_a_second_file_opens_a_second_tab_and_leaves_the_first_alone(
        win, area, three_pages, one_page):
    """THE HEADLINE. Opening a second PDF used to append its pages onto the end
    of the first one, silently, and the next Save wrote that merge over the
    file. `first.page_count() == 3` is the assertion that used to fail."""
    win.open_paths([three_pages])
    first = win.view
    assert first.page_count() == 3

    win.open_paths([one_page])

    assert area.count() == 2
    assert first.page_count() == 3, "the open document was appended to"
    assert first.document_path() == three_pages
    assert win.view is not first
    assert win.view.page_count() == 1
    assert win.view.document_path() == one_page


def test_the_first_open_reuses_the_empty_tab_instead_of_leaving_a_blank_one(
        win, area, three_pages):
    win.open_paths([three_pages])
    assert area.count() == 1
    assert area.view_at(0).document_path() == three_pages


def test_opening_several_files_at_once_opens_several_tabs(win, area, tmp_path):
    paths = [_pdf(tmp_path, f"{n}.pdf") for n in ("a", "b", "c")]
    win.open_paths(paths)
    assert area.count() == 3
    assert [v.document_path() for v in area.views()] == paths
    assert all(v.page_count() == 1 for v in area.views())


def test_a_file_that_is_already_open_activates_its_tab(win, area, tmp_path):
    a = _pdf(tmp_path, "a.pdf")
    b = _pdf(tmp_path, "b.pdf")
    win.open_paths([a, b])
    assert area.current_index() == 1

    win.open_paths([a])

    assert area.count() == 2, "a second copy of an open file was opened"
    assert area.current_index() == 0
    assert win.view.document_path() == a


def test_the_two_views_hold_two_documents_and_two_undo_stacks(
        win, area, three_pages, one_page):
    win.open_paths([three_pages, one_page])
    first, second = area.views()
    assert first._doc is not second._doc
    assert first._canvas is not second._canvas
    assert first.undo_stack() is not second.undo_stack()


def test_each_tab_keeps_its_own_page(win, area, tmp_path):
    long_doc = _pdf(tmp_path, "long.pdf", pages=5)
    short_doc = _pdf(tmp_path, "short.pdf", pages=2)
    win.open_paths([long_doc, short_doc])

    area.set_current_index(0)
    win.view.jump_to_page(3)
    area.set_current_index(1)
    assert win.view.current_page() == 0
    area.set_current_index(0)
    assert win.view.current_page() == 3


# ---------------------------------------------------------------------------
# 3. Titles
# ---------------------------------------------------------------------------

def test_a_title_is_the_basename_without_the_extension():
    assert tab_titles([r"C:\jobs\4100\plan.pdf"]) == ["plan"]


def test_colliding_titles_walk_up_the_path_until_they_differ():
    got = tab_titles([r"C:\jobs\4100\plan.pdf", r"C:\jobs\4200\plan.pdf"])
    assert got == ["4100/plan", "4200/plan"]


def test_only_the_colliding_tabs_grow():
    got = tab_titles([r"C:\jobs\4100\plan.pdf", r"C:\jobs\4200\plan.pdf",
                      r"C:\jobs\4100\index.pdf"])
    assert got == ["4100/plan", "4200/plan", "index"]


def test_a_collision_walks_up_as_far_as_it_has_to():
    got = tab_titles([r"C:\a\sub\plan.pdf", r"C:\b\sub\plan.pdf"])
    assert got == ["a/sub/plan", "b/sub/plan"]


def test_untitled_documents_are_numbered_per_window():
    got = tab_titles([None, r"C:\jobs\plan.pdf", None])
    assert got == ["Untitled 1", "plan", "Untitled 2"]


def test_the_same_file_twice_is_numbered_because_no_path_can_separate_them():
    got = tab_titles([r"C:\jobs\plan.pdf", r"C:\jobs\plan.pdf"])
    assert got == ["plan", "plan (2)"]


def test_the_bar_shows_those_titles_and_the_full_path_in_the_tooltip(
        win, area, tmp_path):
    left = _pdf(tmp_path / "4100", "plan.pdf")
    right = _pdf(tmp_path / "4200", "plan.pdf")
    win.open_paths([left, right])

    assert area.bar().tabText(0) == "4100/plan"
    assert area.bar().tabText(1) == "4200/plan"
    assert area.bar().tabToolTip(0) == left


def test_closing_a_tab_shortens_the_label_the_collision_had_lengthened(
        win, area, tmp_path):
    """The whole bar is recomputed on every add and remove, which is what makes
    a label that grew for a collision shrink back when the collision goes."""
    left = _pdf(tmp_path / "4100", "plan.pdf")
    right = _pdf(tmp_path / "4200", "plan.pdf")
    win.open_paths([left, right])
    assert area.bar().tabText(0) == "4100/plan"

    win.close_tab(1)

    assert area.count() == 1
    assert area.bar().tabText(0) == "plan"


def test_the_bar_appears_once_there_is_a_document_to_name(win, area, three_pages):
    assert area._header.isHidden()
    win.open_paths([three_pages])
    assert not area._header.isHidden()


# ---------------------------------------------------------------------------
# 4. Closing
# ---------------------------------------------------------------------------

def test_ctrl_w_closes_one_tab_of_several_and_the_window_stays(
        win, area, three_pages, one_page):
    win.open_paths([three_pages, one_page])
    first = area.view_at(0)

    win.close_pdf()          # Ctrl+W, on the front tab (the second one)

    assert area.count() == 1
    assert area.view_at(0) is first
    assert first.document_path() == three_pages
    assert win.view is first
    area.check_invariant()


def test_closing_the_last_tab_closes_the_window(win, area, three_pages):
    win.open_paths([three_pages])
    win.close_pdf()
    assert not win.view.has_document()
    assert not win.isVisible()


def test_ctrl_w_on_the_last_empty_tab_does_nothing(win, area):
    """An empty single tab is what an empty window looks like. There is nothing
    to close, and closing the window out from under a Ctrl+W nobody aimed at
    anything would be a surprise."""
    assert win.close_pdf() is None or True
    assert area.count() == 1


def test_a_new_empty_tab_can_be_closed_again(win, area, three_pages):
    win.open_paths([three_pages])
    win.new_tab()
    assert area.count() == 2

    win.close_tab(1)

    assert area.count() == 1
    assert area.view_at(0).document_path() == three_pages
    area.check_invariant()


def test_closing_the_front_tab_unbinds_it_before_it_is_torn_down(
        win, area, three_pages, one_page):
    """Order, and it shows up as noise rather than as a failure.

    The window unbinds its chrome from a departing view by disconnecting five
    signals. Tear the view down first and every one of those disconnects fails,
    which PySide reports as a RuntimeWarning on stderr: harmless, and exactly
    the kind of thing that gets ignored until it is hiding a real one. So the
    departure is announced while the connections still exist.
    """
    import warnings

    win.open_paths([three_pages, one_page])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        win.close_tab(area.current_index())

    failed = [str(w.message) for w in caught
              if "Failed to disconnect" in str(w.message)]
    assert failed == []
    assert area.count() == 1


def test_closing_a_dirty_tab_prompts_and_cancel_keeps_it(
        win, area, monkeypatch, three_pages, one_page):
    win.open_paths([three_pages, one_page])
    area.set_current_index(0)
    win.view._mark_dirty()
    asked = _answer_with(monkeypatch, QMessageBox.StandardButton.Cancel)

    win.close_tab(0)

    assert asked, "the unsaved-changes prompt never opened"
    assert area.count() == 2
    assert area.view_at(0).has_document()


def test_closing_a_dirty_tab_after_discard_takes_the_tab(
        win, area, monkeypatch, three_pages, one_page):
    win.open_paths([three_pages, one_page])
    area.set_current_index(0)
    win.view._mark_dirty()
    _answer_with(monkeypatch, QMessageBox.StandardButton.Discard)

    win.close_tab(0)

    assert area.count() == 1
    assert area.view_at(0).document_path() == one_page
    area.check_invariant()


def test_the_x_prompts_once_per_dirty_tab(win, area, monkeypatch, tmp_path):
    """Per document, not once for the window: which files have unsaved work is
    the whole question, and one prompt cannot ask it."""
    win.open_paths([_pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf"),
                    _pdf(tmp_path, "c.pdf")])
    for view in area.views():
        view._mark_dirty()
    asked = _answer_with(monkeypatch, QMessageBox.StandardButton.Discard)

    win.close()

    assert len(asked) == 3


def test_a_cancelled_save_prompt_aborts_the_whole_window_close(
        win, area, monkeypatch, tmp_path):
    win.open_paths([_pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")])
    area.view_at(0)._mark_dirty()
    _answer_with(monkeypatch, QMessageBox.StandardButton.Cancel)

    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    event.accept()
    win.closeEvent(event)

    assert event.isAccepted() is False
    assert area.count() == 2


# ---------------------------------------------------------------------------
# 4b. close.confirm_multiple_tabs
# ---------------------------------------------------------------------------

def test_several_clean_tabs_ask_before_they_all_go(win, area, monkeypatch,
                                                   store, tmp_path):
    win.open_paths([_pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")])
    assert store.close.confirm_multiple_tabs is True
    asked = _answer_with(monkeypatch, QMessageBox.StandardButton.Cancel)

    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    event.accept()
    win.closeEvent(event)

    assert len(asked) == 1
    assert "2 documents" in asked[0]
    assert event.isAccepted() is False


def test_turning_the_setting_off_closes_several_tabs_without_asking(
        win, area, store, tmp_path):
    store.close.confirm_multiple_tabs = False
    win.open_paths([_pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")])

    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    event.accept()
    win.closeEvent(event)       # never_opens_a_dialog would have fired

    assert event.isAccepted() is True


def test_one_open_document_never_triggers_the_count_warning(win, area, three_pages):
    win.open_paths([three_pages])
    win.new_tab()               # a second TAB, but only one document

    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    event.accept()
    win.closeEvent(event)       # never_opens_a_dialog would have fired

    assert event.isAccepted() is True


def test_a_dirty_tab_skips_the_count_warning_and_goes_straight_to_the_save(
        win, area, monkeypatch, store, tmp_path):
    """Two dialogs asking the same thing in a row is worse than one, and the
    save prompt is the better of the two: it names a document and it offers to
    do something about it."""
    win.open_paths([_pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")])
    area.view_at(0)._mark_dirty()
    asked = _answer_with(monkeypatch, QMessageBox.StandardButton.Discard)

    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    event.accept()
    win.closeEvent(event)

    assert len(asked) == 1, "the count warning ran as well as the save prompt"
    assert "unsaved changes" in asked[0].lower()
    assert event.isAccepted() is True


# ---------------------------------------------------------------------------
# 4c. The tab bar's own close controls
# ---------------------------------------------------------------------------

def test_the_tab_close_button_closes_that_tab_not_the_front_one(
        win, area, three_pages, one_page):
    win.open_paths([three_pages, one_page])
    assert area.current_index() == 1

    area.bar().close_button(0).click()

    assert area.count() == 1
    assert area.view_at(0).document_path() == one_page
    area.check_invariant()


def test_a_dirty_tab_shows_a_dot_where_its_close_x_would_be(
        win, area, three_pages):
    win.open_paths([three_pages])
    button = area.bar().close_button(0)
    assert button.shows_dot() is False

    win.view._mark_dirty()

    assert button.is_dirty() is True
    assert button.shows_dot() is True, "a dirty tab still shows its X"


def test_saving_puts_the_x_back(win, area, three_pages):
    win.open_paths([three_pages])
    win.view._mark_dirty()
    assert area.bar().close_button(0).shows_dot() is True

    assert win.save_pdf() is True

    assert area.bar().close_button(0).shows_dot() is False


def test_close_others_leaves_exactly_the_one(win, area, tmp_path):
    paths = [_pdf(tmp_path, f"{n}.pdf") for n in ("a", "b", "c")]
    win.open_paths(paths)

    area.close_others(1)

    assert area.count() == 1
    assert area.view_at(0).document_path() == paths[1]
    area.check_invariant()


def test_close_to_the_right_leaves_everything_to_the_left(win, area, tmp_path):
    paths = [_pdf(tmp_path, f"{n}.pdf") for n in ("a", "b", "c", "d")]
    win.open_paths(paths)

    area.close_to_the_right(1)

    assert [v.document_path() for v in area.views()] == paths[:2]
    area.check_invariant()


def test_duplicate_tab_opens_a_second_independent_document(win, area, three_pages):
    win.open_paths([three_pages])
    original = area.view_at(0)

    area.duplicate_requested.emit(original)

    assert area.count() == 2
    copy = area.view_at(1)
    assert copy.document_path() == three_pages
    assert copy._doc is not original._doc
    assert copy.undo_stack() is not original.undo_stack()
    assert area.bar().tabText(0) == "three"
    assert area.bar().tabText(1) == "three (2)"


def test_the_tab_menu_offers_move_to_new_window(win, area, three_pages):
    """Phase 3 put it in. It was left out of phase 2 because there was no
    registry owning second windows to move a document into yet."""
    win.open_paths([three_pages])
    labels = [a.text() for a in area.build_tab_menu(0).actions() if a.text()]
    assert labels == ["Close", "Close Others", "Close to the Right",
                      "Move to New Window", "Duplicate Tab", "Copy Full Path",
                      "Open Containing Folder"]


def test_move_to_new_window_is_dead_on_the_only_tab(win, area, three_pages,
                                                    one_page):
    """Moving the last document out of a window and closing the window behind
    it lands you back where you started, minus the window's position and size.
    Every browser greys it out for the same reason."""
    win.open_paths([three_pages])

    def move_action(index):
        return next(a for a in area.build_tab_menu(index).actions()
                    if a.text() == "Move to New Window")

    assert not move_action(0).isEnabled()
    win.open_paths([one_page])
    assert move_action(0).isEnabled()
    assert move_action(1).isEnabled()


# ---------------------------------------------------------------------------
# 5. What gets rebound on a switch
# ---------------------------------------------------------------------------

def test_undo_is_rebuilt_on_a_switch_and_only_touches_the_front_document(
        win, area, three_pages, one_page):
    """THE ONE PHASE 1 CALLED OUT. `createUndoAction` binds an action to ONE
    stack for the life of that action, and there is one stack per document
    (PDFCanvas.set_document clears it), so the menu entry has to be rebuilt on
    every switch. Miss it and Ctrl+Z keeps undoing the document you left."""
    win.open_paths([three_pages, one_page])
    marked = area.view_at(0)
    other = area.view_at(1)

    area.set_current_index(0)
    _draw_a_rectangle(marked)
    assert len(_items(marked)) == 1
    assert _undo_action(win).isEnabled()

    area.set_current_index(1)
    # The other document has an empty history, so its Undo is dead. If the
    # action were still the first document's it would be live here.
    assert not _undo_action(win).isEnabled()

    _undo_action(win).trigger()
    assert len(_items(marked)) == 1, "undo reached the background document"

    area.set_current_index(0)
    assert _undo_action(win).isEnabled()
    _undo_action(win).trigger()
    assert len(_items(marked)) == 0
    assert len(_items(other)) == 0


def test_the_chrome_signals_follow_exactly_one_view(win, area, three_pages,
                                                    one_page):
    """Leave them all connected and a background document writes the status
    bar and moves the page box."""
    win.open_paths([three_pages, one_page])
    background = area.view_at(0)
    front = area.view_at(1)
    assert win.view is front

    background._update_status("from the background")

    assert "from the background" not in win.statusBar().currentMessage()
    assert win._page_jump.total_text() == "of 1"   # the FRONT document


def test_the_page_box_follows_the_tab_that_arrives(win, area, tmp_path):
    win.open_paths([_pdf(tmp_path, "long.pdf", pages=5),
                    _pdf(tmp_path, "short.pdf", pages=2)])
    assert win._page_jump.total_text() == "of 2"

    area.set_current_index(0)

    assert win._page_jump.total_text() == "of 5"
    assert win._page_jump.current_text() == "1"


def test_the_window_title_follows_the_tab_that_arrives(win, area, tmp_path):
    win.open_paths([_pdf(tmp_path, "alpha.pdf"), _pdf(tmp_path, "beta.pdf")])
    assert "beta.pdf" in win.windowTitle()

    area.set_current_index(0)

    assert "alpha.pdf" in win.windowTitle()


def test_the_title_star_follows_the_front_tabs_unsaved_state(win, area, tmp_path):
    win.open_paths([_pdf(tmp_path, "alpha.pdf"), _pdf(tmp_path, "beta.pdf")])
    area.view_at(0)._mark_dirty()
    assert win.isWindowModified() is False, "a background document starred the title"

    area.set_current_index(0)

    assert win.isWindowModified() is True


def test_the_status_line_follows_the_tab_that_arrives(win, area, tmp_path):
    win.open_paths([_pdf(tmp_path, "long.pdf", pages=5),
                    _pdf(tmp_path, "short.pdf", pages=2)])
    area.set_current_index(0)
    assert "long.pdf" in win.statusBar().currentMessage()
    assert "page 1 of 5" in win.statusBar().currentMessage()


def test_the_fit_group_follows_the_arriving_canvas_not_the_setting(
        win, area, tmp_path):
    """A manual zoom breaks the fit on ONE canvas and leaves the app-wide
    default alone, so the buttons cannot read the setting after a switch."""
    win.open_paths([_pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")])
    win.choose_fit_mode("fit_page")
    zoomed = area.view_at(1)
    zoomed._canvas.set_fit_mode(None)          # what a manual zoom leaves behind

    area.set_current_index(0)
    assert win._fit_group.checkedButton() is not None

    area.set_current_index(1)
    assert win._fit_group.checkedButton() is None


def test_the_tool_shortcuts_reach_the_front_view_only(win, area, three_pages,
                                                      one_page):
    """They stay on the window because two views each owning a QShortcut for
    "v" is ambiguous to Qt. `view` being the front one is what keeps a single
    shortcut correct."""
    win.open_paths([three_pages, one_page])
    front = area.view_at(1)
    background = area.view_at(0)

    win.trigger_tool("pan")

    assert front._canvas.is_panning()
    assert not background._canvas.is_panning()


def test_the_page_panel_toggle_moves_every_tab(win, area, three_pages, one_page):
    """One app-wide setting. A background tab left with a stale panel would
    jump the moment it was activated."""
    win.open_paths([three_pages, one_page])
    win.page_panel_action().setChecked(False)
    assert all(v._page_panel.isHidden() for v in area.views())
    win.page_panel_action().setChecked(True)
    assert all(not v._page_panel.isHidden() for v in area.views())


def test_a_new_tab_starts_with_the_panel_the_setting_asks_for(win, area,
                                                              three_pages):
    win.open_paths([three_pages])
    win.page_panel_action().setChecked(False)
    fresh = win.new_tab()
    assert fresh._page_panel.isHidden()


# ---------------------------------------------------------------------------
# 6. Navigation
# ---------------------------------------------------------------------------

def test_next_and_previous_tab_are_positional_and_wrap(win, area, tmp_path):
    win.open_paths([_pdf(tmp_path, f"{n}.pdf") for n in ("a", "b", "c")])
    area.set_current_index(0)

    win.next_tab()
    assert area.current_index() == 1
    win.next_tab()
    assert area.current_index() == 2
    win.next_tab()
    assert area.current_index() == 0, "next did not wrap"
    win.previous_tab()
    assert area.current_index() == 2, "previous did not wrap"


def test_the_navigation_shortcuts_are_the_positional_ones(win):
    assert _menu_action(win, "View", "Next Tab").shortcut().toString() == "Ctrl+PgDown"
    assert _menu_action(win, "View", "Previous Tab").shortcut().toString() == "Ctrl+PgUp"


def test_new_tab_is_on_ctrl_t(win):
    assert _menu_action(win, "File", "New Tab").shortcut().toString() == "Ctrl+T"


def test_stepping_a_single_tab_does_nothing(win, area):
    win.next_tab()
    assert area.current_index() == 0


def test_double_clicking_empty_bar_space_opens_a_new_tab(win, area, three_pages):
    win.open_paths([three_pages])
    area.bar().new_tab_requested.emit()
    assert area.count() == 2
    assert not area.view_at(1).has_document()


# ---------------------------------------------------------------------------
# 7. The shell / CLI launch path
# ---------------------------------------------------------------------------

def test_a_plain_launch_of_several_files_opens_several_tabs(win, area, tmp_path):
    """It used to close whatever was open and stage a Combine as soon as the
    count passed one, so "open these three drawings" destroyed the document on
    screen. The verb decides now, and this verb is open."""
    paths = [_pdf(tmp_path, f"{n}.pdf") for n in ("a", "b", "c")]
    win.handle_cli_files(paths, combine=False)

    assert area.count() == 3
    assert [v.document_path() for v in area.views()] == paths


def test_a_plain_launch_leaves_the_open_document_alone(win, area, three_pages,
                                                       one_page):
    win.open_paths([three_pages])
    first = win.view

    win.handle_cli_files([one_page], combine=False)

    assert first.page_count() == 3
    assert area.count() == 2


def test_the_combine_verb_goes_to_the_combine_dialog(win, area, monkeypatch,
                                                     tmp_path):
    """Decided by VERB, not by count: --combine means combine whether it
    carries two files or twenty."""
    staged = []
    monkeypatch.setattr("ui.document_view.DocumentView._combine_with_dialog",
                        lambda self, paths: staged.append(list(paths)))
    paths = [_pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")]

    win.handle_cli_files(paths, combine=True)

    assert staged == [paths]


def test_the_combine_verb_with_one_file_still_combines(win, monkeypatch, tmp_path):
    staged = []
    monkeypatch.setattr("ui.document_view.DocumentView._combine_with_dialog",
                        lambda self, paths: staged.append(list(paths)))
    path = _pdf(tmp_path, "a.pdf")

    win.handle_cli_files([path], combine=True)

    assert staged == [[path]]


def test_a_cancelled_combine_leaves_no_empty_tab_behind(win, area, monkeypatch,
                                                        three_pages, tmp_path):
    win.open_paths([three_pages])
    monkeypatch.setattr("ui.document_view.DocumentView._combine_with_dialog",
                        lambda self, paths: None)      # cancelled

    win.combine_paths([_pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")])

    assert area.count() == 1
    area.check_invariant()


def test_an_empty_launch_raises_the_window_and_opens_nothing(win, area):
    win.handle_cli_files([], combine=False)
    assert area.count() == 1
    assert not area.view_at(0).has_document()
