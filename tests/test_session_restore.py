"""Reopening last run's windows and tabs. Phase 6 of docs/tabs-plan.md.

WHAT IS ACTUALLY BEING PINNED HERE, because most of it is not the happy path.

1. THE ROUND TRIP through the settings store, including that a tab with no path
   never makes it into the record. That is the phase's one deliberate
   limitation and it is enforced in the coercer, so it holds however the record
   got there.

2. THE LAZY TAB, which is the whole reason this phase has bugs in it. A
   restored tab is named after its file and has not read it, and that is a
   genuinely different code path from an open tab. Every question the app asks
   a tab is asked here of one that has never been activated: the disambiguated
   label, the unsaved dot, the already-open check that routes an Explorer
   double-click, Ctrl+W, and the per-window undo stack.

3. THE FILES THAT HAVE GONE. A mapped drive that is offline takes every tab
   with it, so the count is one status line rather than N dialogs, and the tabs
   whose files are still there come back regardless.

4. THE TWO GATES. Off by default, and off for a launch that carried files.

WHAT IS NOT ASSERTED: geometry. Offscreen lies about it (see
tests/test_multi_window.py), so `_apply_geometry` is exercised through
`_geometry_is_reachable` directly and the placement itself is left to the real
platform, where tools/smoke_multi_window.py checks it.
"""

import os

import fitz
import pytest

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QMessageBox

from core.settings import Settings, set_settings
from ui.canvas import AddItemsCommand, HighlightItem
from ui.main_window import MainWindow
from ui.session import (
    SessionRecorder,
    capture_window,
    restore_on_launch,
    restore_session,
    set_recorder,
    should_restore,
)
from ui.window_registry import WindowRegistry


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    s = Settings(tmp_path / "settings.json", debounce_ms=0, migrate_legacy=False)
    # The "N documents are open" question is a real modal under offscreen and
    # there is nobody here to answer it. Phase 2 pins its behaviour.
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
def registry():
    """The registry every window in this test joins. conftest resets it."""
    return WindowRegistry.instance()


@pytest.fixture(autouse=True)
def recorder(registry):
    """A recorder bound to this test's registry, put back afterwards."""
    own = SessionRecorder(registry)
    previous = set_recorder(own)
    yield own
    set_recorder(previous)


def make_pdf(folder, name, pages=2):
    path = os.path.join(str(folder), name)
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 120), f"{name} page {i}", fontsize=28)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def window(qt_app, store, registry):
    w = MainWindow()
    yield w
    for view in w.document_area().views():
        view.mark_clean()
    w._force_quit = True
    w.close()


def restored_window(qt_app, registry):
    """A second window standing in for the one main.py builds at startup."""
    return registry.create_window(show=False)


# ---------------------------------------------------------------------------
# 1. The record itself
# ---------------------------------------------------------------------------

def test_a_session_round_trips_through_the_settings_store(window, store, tmp_path):
    """Every field a tab carries survives a write and a reload."""
    a = make_pdf(tmp_path, "alpha.pdf", 3)
    b = make_pdf(tmp_path, "beta.pdf", 2)
    window.open_paths([a, b])
    area = window.document_area()
    area.set_current_index(1)
    area.view_at(0).jump_to_page(2)
    area.view_at(0).set_fit_mode("fit_width")

    record = capture_window(window)
    store.session.windows = [record]
    store.flush()

    reloaded = Settings(store.path, debounce_ms=0, migrate_legacy=False)
    saved = reloaded.session.windows
    assert len(saved) == 1
    assert [t["path"] for t in saved[0]["tabs"]] == [a, b]
    assert saved[0]["current"] == 1
    assert saved[0]["tabs"][0]["page"] == 2
    assert saved[0]["tabs"][0]["fit_mode"] == "fit_width"
    assert saved[0]["tabs"][0]["zoom"] > 0
    assert saved[0]["geometry"] is not None


def test_an_untitled_tab_is_left_out_of_the_record(window, store, tmp_path):
    """A merged document lives only in memory, so there is nothing to write.

    This is the phase's one deliberate limitation. Serialising it would mean a
    cache directory with a disk-space policy, a cleanup policy and a restore
    path that can fail on a corrupt cache.
    """
    a = make_pdf(tmp_path, "alpha.pdf")
    window.open_paths([a])
    merged = window.new_tab()
    merged.open_path(make_pdf(tmp_path, "beta.pdf"))
    merged._mark_untitled()             # what an Organizer merge does

    record = capture_window(window)
    assert [t["path"] for t in record["tabs"]] == [a]


def test_a_window_holding_nothing_saveable_is_not_recorded(window):
    assert capture_window(window) is None


def test_a_hand_mangled_session_reads_back_as_no_session(store, tmp_path):
    """Same rule as every other setting: nonsense in the file is a default."""
    store.session.windows = [{"tabs": [{"path": str(tmp_path / "a.pdf")}]}]
    store.flush()
    raw = Settings(store.path, debounce_ms=0, migrate_legacy=False)
    raw._data["session"]["windows"] = {"not": "a list"}
    assert raw.session.windows == []


# ---------------------------------------------------------------------------
# 2. The lazy tab
# ---------------------------------------------------------------------------

@pytest.fixture
def two_tab_session(store, tmp_path):
    """A saved session of one window with two real files in it."""
    a = make_pdf(tmp_path, "alpha.pdf", 3)
    b = make_pdf(tmp_path, "beta.pdf", 2)
    store.session.windows = [{
        "geometry": None, "screen": None, "current": 0,
        "tabs": [{"path": a, "page": 0, "zoom": 0.0, "fit_mode": None},
                 {"path": b, "page": 1, "zoom": 0.0, "fit_mode": "fit_width"}],
    }]
    return a, b


def test_only_the_front_tab_reads_its_file(qt_app, registry, two_tab_session):
    """The point of the whole phase: eight drawings do not open at once."""
    a, b = two_tab_session
    window = restored_window(qt_app, registry)
    assert restore_session(window, registry) == 0

    area = window.document_area()
    assert area.count() == 2
    assert area.view_at(0).has_document()
    assert not area.view_at(1).has_document()
    assert area.view_at(1).is_pending()
    area.check_invariant()


def test_a_tab_that_was_never_activated_still_names_its_file(
        qt_app, registry, two_tab_session):
    """The label, the tooltip and the path all come off the pending path."""
    a, b = two_tab_session
    window = restored_window(qt_app, registry)
    restore_session(window, registry)
    area = window.document_area()
    lazy = area.view_at(1)

    assert lazy.document_path() == b
    assert area.bar().tabText(1) == "beta"
    assert area.bar().tabToolTip(1) == b
    assert not lazy.is_empty()


def test_disambiguation_walks_the_path_for_a_tab_nobody_opened(
        qt_app, registry, store, tmp_path):
    """Two files with the same name, neither of them read yet.

    `tab_titles` runs off `document_path()`, so this is the check that a
    pending tab answers that question the same way an open one does.
    """
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    a = make_pdf(left, "plan.pdf")
    b = make_pdf(right, "plan.pdf")
    store.session.windows = [{
        "geometry": None, "screen": None, "current": 0,
        "tabs": [{"path": a, "page": 0, "zoom": 0.0, "fit_mode": None},
                 {"path": b, "page": 0, "zoom": 0.0, "fit_mode": None}],
    }]

    window = restored_window(qt_app, registry)
    restore_session(window, registry)
    bar = window.document_area().bar()
    assert bar.tabText(0) == "left/plan"
    assert bar.tabText(1) == "right/plan"


def test_a_pending_tab_shows_no_unsaved_dot(qt_app, registry, two_tab_session):
    """It has read nothing, so it cannot have unsaved anything."""
    window = restored_window(qt_app, registry)
    restore_session(window, registry)
    bar = window.document_area().bar()
    assert bar.close_button(1).is_dirty() is False
    assert window.document_area().view_at(1).is_dirty() is False


def test_opening_a_file_again_routes_to_its_pending_tab(
        qt_app, registry, two_tab_session):
    """An Explorer double-click on a restored-but-unopened file.

    `find_by_path` and `index_of_path` both read `document_path()`, so this is
    what stops a second copy of the document opening beside the tab that is
    already standing for it. Activating it is also what opens it.
    """
    a, b = two_tab_session
    window = restored_window(qt_app, registry)
    restore_session(window, registry)
    area = window.document_area()

    assert registry.find_by_path(b) == (window, area.view_at(1))
    registry.route_open([b])
    assert area.count() == 2
    assert area.current_index() == 1
    assert area.view_at(1).has_document()


def test_a_pending_tab_does_not_get_reused_by_an_open(
        qt_app, registry, two_tab_session, tmp_path):
    """`_target_view` looks for an EMPTY tab, and a pending one is spoken for."""
    a, b = two_tab_session
    window = restored_window(qt_app, registry)
    restore_session(window, registry)
    area = window.document_area()
    area.set_current_index(1)           # opens beta, so nothing is empty now
    area.set_current_index(0)

    window.open_paths([make_pdf(tmp_path, "gamma.pdf")])
    assert area.count() == 3
    assert area.view_at(1).document_path() == b


def test_ctrl_w_closes_a_tab_that_was_never_opened(qt_app, registry, two_tab_session):
    """It would have been a dead key on every tab of a restored window."""
    window = restored_window(qt_app, registry)
    restore_session(window, registry)
    area = window.document_area()
    assert window.close_tab(1) is True
    assert area.count() == 1
    area.check_invariant()


def test_the_last_tab_closing_closes_the_window_even_if_it_never_opened(
        qt_app, registry, two_tab_session):
    """Closing the front tab first leaves a window whose only tab is pending."""
    window = restored_window(qt_app, registry)
    restore_session(window, registry)
    area = window.document_area()

    assert window.close_tab(0) is True          # the one that was opened
    assert area.count() == 1
    assert area.view_at(0).is_pending() or area.view_at(0).has_document()
    assert registry.count() == 1

    # And the last one, which is what "close the last tab closes the window"
    # has to mean for a tab that has read nothing.
    area.view_at(0)._pending_path = area.view_at(0).document_path()
    area.view_at(0)._doc.close()
    assert area.view_at(0).is_pending()
    window.close_tab(0)
    assert registry.count() == 0


def test_activating_a_lazy_tab_opens_it_where_it_was_left(
        qt_app, registry, two_tab_session):
    """Page, fit mode and the window's chrome all land on the switch."""
    a, b = two_tab_session
    window = restored_window(qt_app, registry)
    restore_session(window, registry)
    area = window.document_area()

    area.set_current_index(1)
    lazy = area.view_at(1)
    assert lazy.has_document()
    assert not lazy.is_pending()
    assert lazy.current_page() == 1
    assert lazy.fit_mode() == "fit_width"
    assert window.windowTitle().startswith("Rapid PDF")
    assert "beta.pdf" in window.windowTitle()


def test_a_lazy_tab_restores_a_hand_set_zoom(qt_app, registry, store, tmp_path):
    """No fit mode was active, so the remembered scale is what comes back."""
    a = make_pdf(tmp_path, "alpha.pdf")
    store.session.windows = [{
        "geometry": None, "screen": None, "current": 0,
        "tabs": [{"path": a, "page": 0, "zoom": 0.42, "fit_mode": None}],
    }]
    window = restored_window(qt_app, registry)
    restore_session(window, registry)
    view = window.document_area().view_at(0)
    assert view.fit_mode() is None
    assert view.view_scale() == pytest.approx(0.42, rel=0.01)


def test_a_lazy_tab_joins_the_windows_undo_stack_when_it_opens(
        qt_app, registry, two_tab_session):
    """One stack per window, and a restored tab is handed it before it loads."""
    window = restored_window(qt_app, registry)
    restore_session(window, registry)
    area = window.document_area()
    lazy = area.view_at(1)
    assert lazy.undo_stack() is window.undo_stack()

    area.set_current_index(1)
    canvas = lazy._canvas
    item = HighlightItem(canvas.sceneRect().adjusted(10, 10, -10, -10).normalized(),
                         canvas.palette().text().color(), 0.5, 0)
    canvas._attach_item(item)
    canvas.undo_stack.push(AddItemsCommand(canvas, [item]))
    assert window.undo_stack().count() == 1
    assert lazy.is_dirty()
    window.undo_stack().undo()
    assert not lazy.is_dirty()


# ---------------------------------------------------------------------------
# 3. Files that have gone
# ---------------------------------------------------------------------------

def test_a_missing_file_is_skipped_and_the_rest_still_come_back(
        qt_app, registry, store, tmp_path):
    """No dialog, and the count is what the status line is built from."""
    a = make_pdf(tmp_path, "alpha.pdf")
    b = make_pdf(tmp_path, "beta.pdf")
    gone = str(tmp_path / "offline.pdf")
    store.session.windows = [{
        "geometry": None, "screen": None, "current": 2,
        "tabs": [{"path": a, "page": 0, "zoom": 0.0, "fit_mode": None},
                 {"path": gone, "page": 0, "zoom": 0.0, "fit_mode": None},
                 {"path": b, "page": 0, "zoom": 0.0, "fit_mode": None}],
    }]

    window = restored_window(qt_app, registry)
    missing = restore_session(window, registry)
    area = window.document_area()

    assert missing == 1
    assert area.count() == 2
    assert [v.document_path() for v in area.views()] == [a, b]
    area.check_invariant()


def test_the_missing_count_lands_in_the_status_bar(
        qt_app, registry, store, tmp_path):
    a = make_pdf(tmp_path, "alpha.pdf")
    store.startup.restore_tabs = True
    store.session.windows = [{
        "geometry": None, "screen": None, "current": 0,
        "tabs": [{"path": a, "page": 0, "zoom": 0.0, "fit_mode": None}]
                + [{"path": str(tmp_path / f"gone{i}.pdf"), "page": 0,
                    "zoom": 0.0, "fit_mode": None} for i in range(3)],
    }]

    window = restored_window(qt_app, registry)
    assert restore_on_launch(window, registry) == 3
    assert (window.statusBar().currentMessage()
            == "3 files from the last session could not be found.")


def test_a_window_whose_files_have_all_gone_is_not_reopened(
        qt_app, registry, store, tmp_path):
    a = make_pdf(tmp_path, "alpha.pdf")
    store.session.windows = [
        {"geometry": None, "screen": None, "current": 0,
         "tabs": [{"path": str(tmp_path / "gone.pdf"), "page": 0,
                   "zoom": 0.0, "fit_mode": None}]},
        {"geometry": None, "screen": None, "current": 0,
         "tabs": [{"path": a, "page": 0, "zoom": 0.0, "fit_mode": None}]},
    ]
    window = restored_window(qt_app, registry)
    assert restore_session(window, registry) == 1
    assert registry.count() == 1
    assert window.document_area().view_at(0).document_path() == a


# ---------------------------------------------------------------------------
# 4. The two gates
# ---------------------------------------------------------------------------

def test_restore_is_off_by_default(store):
    assert store.startup.restore_tabs is False
    assert should_restore([], False) is False


def test_nothing_is_restored_with_the_setting_off(
        qt_app, registry, store, two_tab_session):
    window = restored_window(qt_app, registry)
    assert restore_on_launch(window, registry) == 0
    assert window.document_area().count() == 1
    assert window.document_area().view_at(0).is_empty()


def test_a_launch_carrying_files_does_not_restore(
        qt_app, registry, store, two_tab_session, tmp_path):
    """Opening a PDF from Explorer must not bury it under last run's tabs."""
    store.startup.restore_tabs = True
    assert should_restore([str(tmp_path / "dropped.pdf")], False) is False
    assert should_restore([], True) is False
    assert should_restore([], False) is True

    window = restored_window(qt_app, registry)
    assert restore_on_launch(window, registry,
                             [str(tmp_path / "dropped.pdf")], False) == 0
    assert window.document_area().count() == 1
    assert window.document_area().view_at(0).is_empty()


# ---------------------------------------------------------------------------
# 5. Several windows, and when a closing window stays in the record
# ---------------------------------------------------------------------------

def test_two_windows_come_back_as_two_windows(
        qt_app, registry, store, tmp_path):
    a = make_pdf(tmp_path, "alpha.pdf")
    b = make_pdf(tmp_path, "beta.pdf")
    store.session.windows = [
        {"geometry": None, "screen": None, "current": 0,
         "tabs": [{"path": a, "page": 0, "zoom": 0.0, "fit_mode": None}]},
        {"geometry": None, "screen": None, "current": 0,
         "tabs": [{"path": b, "page": 0, "zoom": 0.0, "fit_mode": None}]},
    ]
    window = restored_window(qt_app, registry)
    restore_session(window, registry)

    assert registry.count() == 2
    assert sorted(v.document_path() for _, v in registry.views()) == sorted([a, b])


def test_closing_one_window_of_two_drops_it_from_the_session(
        qt_app, registry, store, recorder, tmp_path):
    """The app carries on, so that window is not part of the arrangement."""
    a = make_pdf(tmp_path, "alpha.pdf")
    b = make_pdf(tmp_path, "beta.pdf")
    first = registry.create_window(show=False)
    first.open_paths([a])
    second = registry.create_window(show=False)
    second.open_paths([b])

    second._force_quit = False
    second.close()
    assert [t["path"] for w in store.session.windows for t in w["tabs"]] == [a]

    first._force_quit = True
    first.close()
    assert [t["path"] for w in store.session.windows for t in w["tabs"]] == [a]


def test_quitting_keeps_every_window_that_went_down_with_the_app(
        qt_app, registry, store, recorder, tmp_path):
    """The Quit menu closes them one at a time, and all of them are the session."""
    a = make_pdf(tmp_path, "alpha.pdf")
    b = make_pdf(tmp_path, "beta.pdf")
    first = registry.create_window(show=False)
    first.open_paths([a])
    second = registry.create_window(show=False)
    second.open_paths([b])

    first._quit_app()               # sets _force_quit on both and closes both
    assert registry.count() == 0
    saved = [t["path"] for w in store.session.windows for t in w["tabs"]]
    assert sorted(saved) == sorted([a, b])


def test_a_restored_window_records_itself_again_on_the_way_out(
        qt_app, registry, store, recorder, two_tab_session):
    """A session survives a run that never opened most of what it restored."""
    a, b = two_tab_session
    window = restored_window(qt_app, registry)
    restore_session(window, registry)

    window._force_quit = True
    window.close()
    saved = store.session.windows
    assert len(saved) == 1
    assert [t["path"] for t in saved[0]["tabs"]] == [a, b]


# ---------------------------------------------------------------------------
# 6. Placement
# ---------------------------------------------------------------------------

def test_a_geometry_out_in_space_is_refused(qt_app):
    """An unplugged monitor leaves a position nothing can be dragged back from."""
    from ui.session import _geometry_is_reachable

    screen = QApplication.primaryScreen().availableGeometry()
    assert _geometry_is_reachable(QRect(screen.x() + 10, screen.y() + 10, 800, 600))
    assert not _geometry_is_reachable(QRect(-30000, -30000, 800, 600))
