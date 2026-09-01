"""Several tabs ticked, then moved to one new window in one command.

THE CHEAP ROUTE, ON PURPOSE. Shift-click range select was the other candidate
and it needs a real selection model on a QTabBar. QTabBar has one current tab
and nothing else; giving it a second selected tab means taking over its
painting, its keyboard handling and its drag reordering, which is a bigger
change than the feature is worth. So the tick is an entry on the tab's own
context menu, right next to the command that consumes it, and the state is a
list of VIEWS held by DocumentArea.

VIEWS, NOT INDICES, and that is what most of section 1 is about. An index goes
stale the moment anything renumbers the bar, and three things routinely do:
dragging a tab along the bar, closing a tab to the left of a ticked one, and
tearing one out into another window. Every one of those is checked below.

SECTION 2 IS THE MOVE ITSELF. One window, made once, then the singular move per
document into it. The singular move is where the reparent order lives (the
destination adopts before the source detaches, and the source closes last) and
there is deliberately no plural copy of that order; what the plural entry point
saves is the window per document and the raise per document, which is what the
flicker would have been.
"""

import fitz
import pytest

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QMessageBox

from core.settings import Settings, set_settings
from ui.window_registry import WindowRegistry


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


def _pdf(tmp_path, name):
    path = tmp_path / name
    raw = fitz.open()
    page = raw.new_page(width=400, height=500)
    page.insert_text((20, 100), name, fontsize=24)
    raw.save(str(path))
    raw.close()
    return str(path)


@pytest.fixture
def registry():
    return WindowRegistry.instance()


@pytest.fixture
def window(qt_app, store, registry, tmp_path):
    win = registry.create_window(show=False)
    win.resize(1200, 800)
    win.move(100, 100)
    win.show()
    win.open_paths([_pdf(tmp_path, n) for n in ("a.pdf", "b.pdf", "c.pdf",
                                                "d.pdf")])
    yield win


def _titles(area):
    return [area.bar().tabText(i) for i in range(area.count())]


# ======================================================================
# 1. Ticking, and surviving everything that renumbers the bar
# ======================================================================

def test_ticking_a_tab_records_the_view_and_marks_it(window):
    area = window.document_area()
    view = area.view_at(1)

    area.toggle_view_checked(view)
    assert area.checked_views() == [view]
    assert area.is_view_checked(view)
    assert area.bar().checked_indices() == (1,)

    area.toggle_view_checked(view)
    assert area.checked_views() == []
    assert area.bar().checked_indices() == ()


def test_the_ticked_list_comes_back_in_tab_order(window):
    area = window.document_area()
    for index in (3, 0, 2):
        area.set_view_checked(area.view_at(index), True)
    assert area.checked_views() == [area.view_at(0), area.view_at(2),
                                    area.view_at(3)]


def test_a_ticked_tab_stays_ticked_when_the_bar_is_reordered(window):
    """Dragging a tab along the bar renumbers every tab after it. Holding
    indices instead of views is exactly the bug this pins."""
    area = window.document_area()
    marked = area.view_at(0)
    area.set_view_checked(marked, True)

    # `tabMoved` is already wired to the area, which drags the stack along with
    # it, so this is the whole gesture and not half of it.
    area.bar().moveTab(0, 3)

    assert area.index_of(marked) == 3
    assert area.checked_views() == [marked]
    assert area.bar().checked_indices() == (3,)


def test_closing_a_tab_to_the_left_keeps_the_mark_on_the_right_tab(window):
    area = window.document_area()
    marked = area.view_at(2)
    area.set_view_checked(marked, True)

    area.remove_view(0)

    assert area.index_of(marked) == 1
    assert area.checked_views() == [marked]
    assert area.bar().checked_indices() == (1,)
    area.check_invariant()


def test_a_tab_that_leaves_the_window_leaves_the_selection(window):
    area = window.document_area()
    leaving = area.view_at(1)
    area.set_view_checked(leaving, True)
    area.set_view_checked(area.view_at(2), True)

    window.move_view_to_new_window(leaving)

    assert area.checked_views() == [area.view_at(1)]
    assert not any(v is leaving for v in area.checked_views())


def test_clearing_the_selection_clears_the_marks(window):
    area = window.document_area()
    area.set_view_checked(area.view_at(0), True)
    area.set_view_checked(area.view_at(1), True)
    area.clear_checked()
    assert area.checked_views() == []
    assert area.bar().checked_indices() == ()


# ======================================================================
# 2. Moving the ticked ones
# ======================================================================

def test_moving_several_selected_tabs_lands_all_of_them(window, registry):
    area = window.document_area()
    moving = [area.view_at(1), area.view_at(3)]
    labels = [area.bar().tabText(1), area.bar().tabText(3)]
    for view in moving:
        area.set_view_checked(view, True)

    target = window.move_views_to_new_window(area.checked_views())

    assert target is not None and target is not window
    assert registry.count() == 2
    assert target.document_area().count() == 2
    assert [target.document_area().view_at(i) for i in range(2)] == moving
    assert _titles(target.document_area()) == labels
    assert area.count() == 2
    area.check_invariant()
    target.document_area().check_invariant()


def test_the_moved_documents_are_still_alive(window):
    """The same reparent rule as the singular move: never `setParent(None)`, so
    the scene, the pages and the native-resource-free view all survive."""
    area = window.document_area()
    moving = [area.view_at(0), area.view_at(2)]
    scenes = [v._canvas.scene() for v in moving]
    pages = [v.page_count() for v in moving]
    for view in moving:
        area.set_view_checked(view, True)

    target = window.move_views_to_new_window(area.checked_views())

    for view, scene, count in zip(moving, scenes, pages):
        assert view.window() is target
        assert view._canvas.scene() is scene
        assert view.page_count() == count
        assert view._canvas.internalWinId() == 0


def test_the_move_makes_exactly_one_window(window, registry):
    """The reason there is a plural entry point at all. Looping over
    `move_view_to_new_window` would have made one window per document."""
    area = window.document_area()
    for index in (0, 1, 2):
        area.set_view_checked(area.view_at(index), True)

    window.move_views_to_new_window(area.checked_views())

    assert registry.count() == 2


def test_the_move_clears_the_selection_behind_it(window):
    area = window.document_area()
    area.set_view_checked(area.view_at(0), True)
    window.move_views_to_new_window(area.checked_views())
    assert area.checked_views() == []


def test_moving_nothing_makes_no_window(window, registry):
    assert window.move_views_to_new_window([]) is None
    assert window.move_views_to_new_window(None) is None
    assert registry.count() == 1


def test_a_view_from_another_window_is_ignored(window, registry, tmp_path):
    """The list arrives from a menu, and a menu can be stale. Anything not in
    THIS window's bar is dropped rather than reparented out of somebody else's."""
    other = registry.create_window(show=False)
    other.show()
    other.open_paths([_pdf(tmp_path, "z.pdf")])
    stranger = other.document_area().view_at(0)

    area = window.document_area()
    mine = area.view_at(1)
    target = window.move_views_to_new_window([mine, stranger])

    assert target.document_area().count() == 1
    assert target.document_area().view_at(0) is mine
    assert stranger.window() is other


# ======================================================================
# 3. The menu that drives it
# ======================================================================

def test_the_menu_reports_how_many_are_ticked(window):
    area = window.document_area()
    labels = [a.text() for a in area.build_tab_menu(0).actions()]
    assert "Move Selected to New Window" in labels

    area.set_view_checked(area.view_at(0), True)
    area.set_view_checked(area.view_at(1), True)
    labels = [a.text() for a in area.build_tab_menu(0).actions()]
    assert "Move Selected to New Window (2)" in labels


def _action(menu, prefix):
    return next(a for a in menu.actions() if a.text().startswith(prefix))


def test_move_selected_is_dead_with_nothing_ticked_and_with_all_of_them(window):
    """Dead on nothing for the obvious reason, and dead on everything for the
    same reason the singular one is dead on a lone tab: moving every document
    out of a window and closing the window behind it hands back the window you
    started with, minus its size and position."""
    area = window.document_area()
    menu = area.build_tab_menu(0)
    assert not _action(menu, "Move Selected").isEnabled()

    area.set_view_checked(area.view_at(0), True)
    assert _action(area.build_tab_menu(0), "Move Selected").isEnabled()

    for index in range(area.count()):
        area.set_view_checked(area.view_at(index), True)
    assert not _action(area.build_tab_menu(0), "Move Selected").isEnabled()


def test_the_menu_ticks_and_the_command_moves(window, registry):
    """The whole feature through its own menu, which is the only way a user can
    reach it: tick two tabs one right-click each, then run the command."""
    area = window.document_area()
    wanted = [area.view_at(1), area.view_at(2)]

    for index in (1, 2):
        _action(area.build_tab_menu(index), "Select Tab").trigger()
    assert area.checked_views() == wanted

    _action(area.build_tab_menu(1), "Move Selected").trigger()

    assert registry.count() == 2
    target = next(w for w in registry.windows() if w is not window)
    assert [target.document_area().view_at(i) for i in range(2)] == wanted
    assert area.count() == 2


def test_clear_selection_is_on_the_menu_and_works(window):
    area = window.document_area()
    area.set_view_checked(area.view_at(0), True)
    _action(area.build_tab_menu(0), "Clear Selection").trigger()
    assert area.checked_views() == []
