"""The settings store: schema, atomicity, and the ways a file can be wrong.

The store's job is to be the one place the app remembers anything, so most of
what is pinned down here is failure behaviour rather than happy-path reads. A
settings file is a file on a user's disk: it gets truncated by a power cut,
hand-edited, written by a build that is newer than the one reading it, and
copied between machines. None of that is allowed to stop the app starting.

No QApplication is needed for any of this. The store falls back to writing
straight through when there is no event loop for its debounce timer, which is
exactly the situation here, so a write lands before the next assertion.
"""

import json
import os

import pytest

from core.settings import (
    DEFAULTS,
    MIGRATIONS,
    SCHEMA_VERSION,
    Settings,
    default_settings_dir,
)


@pytest.fixture
def store(tmp_path):
    """A store on a throwaway file, with the registry migration switched off."""
    return Settings(tmp_path / "settings.json", debounce_ms=0,
                    migrate_legacy=False)


# ---------------------------------------------------------------------------
# Where it lives
# ---------------------------------------------------------------------------

def test_the_path_has_no_org_segment_in_it():
    """`%LOCALAPPDATA%\\Rapid PDF`, not `%LOCALAPPDATA%\\Lucas\\Rapid PDF`.

    QStandardPaths folds the organization name in as its own directory. Nobody
    wants a "Lucas" folder in their AppData, so it comes back out.
    """
    directory = default_settings_dir()
    assert directory.name == "Rapid PDF"
    assert directory.parent.name != "Lucas"


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------

def test_defaults_read_back_when_the_file_does_not_exist(store):
    assert not store.path.exists()
    assert store.close.x_closes == "window"
    assert store.close.confirm_multiple_tabs is True
    assert store.appearance.theme == "light"
    assert store.files.default_folder_mode == "last_used"
    assert store.files.default_folder == ""
    assert store.view.page_panel_visible is True
    assert store.view.default_fit_mode == "fit_page"
    assert store.view.organizer_zoom_index == DEFAULTS["view"]["organizer_zoom_index"]


def test_every_section_round_trips_through_the_file(tmp_path):
    """Set one value in each section, reload from disk, get it back."""
    path = tmp_path / "settings.json"
    store = Settings(path, debounce_ms=0, migrate_legacy=False)

    store.close.x_closes = "document"
    store.close.confirm_multiple_tabs = False
    store.appearance.theme = "dark"
    store.files.default_folder_mode = "fixed"
    store.files.default_folder = r"D:\Drawings"
    store.view.page_panel_visible = False
    store.view.default_fit_mode = "fit_width"
    store.view.organizer_zoom_index = 5
    assert store.flush() is False           # debounce_ms=0 already wrote it
    assert path.exists()

    reloaded = Settings(path, debounce_ms=0, migrate_legacy=False)
    assert reloaded.close.x_closes == "document"
    assert reloaded.close.confirm_multiple_tabs is False
    assert reloaded.appearance.theme == "dark"
    assert reloaded.files.default_folder_mode == "fixed"
    assert reloaded.files.default_folder == r"D:\Drawings"
    assert reloaded.view.page_panel_visible is False
    assert reloaded.view.default_fit_mode == "fit_width"
    assert reloaded.view.organizer_zoom_index == 5


def test_the_written_file_carries_the_schema_version(store):
    store.appearance.theme = "dark"
    written = json.loads(store.path.read_text(encoding="utf-8"))
    assert written["schema_version"] == SCHEMA_VERSION


def test_the_defaults_match_the_documented_schema():
    """The v1 shape, spelled out so a change to it is a deliberate edit here.

    `startup` and `session` arrived with phase 6 and did NOT bump the version:
    adding a key is not a migration, because a file from a build with fewer
    settings is simply a file with fewer keys.
    """
    assert set(DEFAULTS) == {"schema_version", "close", "appearance", "files",
                             "view", "startup", "session"}
    assert set(DEFAULTS["close"]) == {"x_closes", "confirm_multiple_tabs"}
    assert set(DEFAULTS["appearance"]) == {"theme"}
    assert set(DEFAULTS["files"]) == {"default_folder_mode", "default_folder"}
    assert set(DEFAULTS["view"]) == {
        "page_panel_visible", "default_fit_mode", "organizer_zoom_index",
        "render_scale"}
    assert set(DEFAULTS["startup"]) == {"restore_tabs"}
    assert set(DEFAULTS["session"]) == {"windows"}
    assert DEFAULTS["schema_version"] == SCHEMA_VERSION == 1


def test_the_zoom_default_agrees_with_the_organizers_ladder():
    """core/ cannot import ui/, so the organizer's default index is repeated in
    DEFAULTS. If the ladder ever changes, this is what catches the drift."""
    from ui.organizer import DEFAULT_ZOOM_INDEX

    assert DEFAULTS["view"]["organizer_zoom_index"] == DEFAULT_ZOOM_INDEX


def test_a_setting_the_file_does_not_carry_reads_as_its_default(tmp_path):
    """A file written by a build with fewer settings is just a shorter file."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "appearance": {"theme": "dark"},
    }), encoding="utf-8")

    store = Settings(path, debounce_ms=0, migrate_legacy=False)
    assert store.appearance.theme == "dark"        # what the file had
    assert store.close.x_closes == "window"        # everything else, default
    assert store.view.page_panel_visible is True
    assert store.schema_version == 1               # and no version bump for it


def test_missing_keys_are_not_written_back_just_for_being_missing(tmp_path):
    path = tmp_path / "settings.json"
    original = {"schema_version": 1, "appearance": {"theme": "dark"}}
    path.write_text(json.dumps(original), encoding="utf-8")

    store = Settings(path, debounce_ms=0, migrate_legacy=False)
    _ = store.close.x_closes, store.view.page_panel_visible
    assert store.flush() is False                  # reading changed nothing
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_unknown_keys_survive_a_write_cycle(tmp_path):
    """An older build must not strip settings a newer one added."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "appearance": {"theme": "light", "accent": "teal"},
        "tabs": {"restore_on_launch": True},
    }), encoding="utf-8")

    store = Settings(path, debounce_ms=0, migrate_legacy=False)
    store.close.x_closes = "document"

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["tabs"] == {"restore_on_launch": True}
    assert written["appearance"]["accent"] == "teal"
    assert written["close"]["x_closes"] == "document"


def test_a_junk_value_reads_as_the_default_rather_than_raising(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "close": {"x_closes": "banana", "confirm_multiple_tabs": "maybe"},
        "view": {"organizer_zoom_index": "big"},
    }), encoding="utf-8")

    store = Settings(path, debounce_ms=0, migrate_legacy=False)
    assert store.close.x_closes == "window"
    assert store.close.confirm_multiple_tabs is True
    assert store.view.organizer_zoom_index == DEFAULTS["view"]["organizer_zoom_index"]


def test_setting_a_value_outside_the_allowed_set_is_a_programming_error(store):
    """Reading junk is forgiving; writing it is not. A bad value from inside
    the app is a bug, and it should not reach the file."""
    with pytest.raises(ValueError):
        store.close.x_closes = "sideways"
    with pytest.raises(ValueError):
        store.appearance.theme = "puce"
    assert store.close.x_closes == "window"


def test_writing_the_same_value_again_does_not_dirty_the_store(store):
    store.appearance.theme = "dark"
    assert store.flush() is False        # already on disk
    store.appearance.theme = "dark"
    assert store.flush() is False        # and no second write for a no-op


# ---------------------------------------------------------------------------
# Corrupt files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content", [
    '{"schema_version": 1, "close": {',      # truncated mid-write
    "",                                       # zero bytes
    "not json at all",
    "[1, 2, 3]",                              # valid JSON, wrong shape
    '\x00\x00\x00\x00',                       # what a bad power cut leaves
])
def test_a_corrupt_file_is_set_aside_and_defaults_take_over(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")

    store = Settings(path, debounce_ms=0, migrate_legacy=False)

    assert store.close.x_closes == "window"          # defaults, not a crash
    assert not path.exists()                          # moved out of the way
    bad = tmp_path / "settings.json.bad"
    assert bad.exists()
    assert bad.read_text(encoding="utf-8") == content  # kept for a post-mortem


def test_the_app_carries_on_writing_after_a_corrupt_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{{{", encoding="utf-8")

    store = Settings(path, debounce_ms=0, migrate_legacy=False)
    store.close.x_closes = "document"

    reloaded = Settings(path, debounce_ms=0, migrate_legacy=False)
    assert reloaded.close.x_closes == "document"


def test_a_second_corrupt_file_overwrites_the_first_quarantine(tmp_path):
    path = tmp_path / "settings.json"
    bad = tmp_path / "settings.json.bad"
    for content in ("first junk", "second junk"):
        path.write_text(content, encoding="utf-8")
        Settings(path, debounce_ms=0, migrate_legacy=False)
    assert bad.read_text(encoding="utf-8") == "second junk"


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------

def test_a_newer_schema_loads_read_only_against_defaults(tmp_path):
    """A file from a future build runs on defaults and is never written to.

    Writing it back from here would drop whatever that build understands, and
    silently damaging the user's settings is worse than ignoring them."""
    path = tmp_path / "settings.json"
    future = {
        "schema_version": SCHEMA_VERSION + 1,
        "close": {"x_closes": "document"},
        "something_from_the_future": 42,
    }
    path.write_text(json.dumps(future), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    store = Settings(path, debounce_ms=0, migrate_legacy=False)

    assert store.read_only is True
    assert store.close.x_closes == "window"      # defaults, not the file
    store.close.x_closes = "document"            # accepted, and goes nowhere
    assert store.flush() is False
    assert path.read_text(encoding="utf-8") == before


def test_the_migrations_table_is_ordered_and_contiguous():
    """Empty at schema 1. When it is not, every step from 1 up has to be there
    or a file two versions old has nowhere to go."""
    if not MIGRATIONS:
        assert SCHEMA_VERSION == 1
        return
    assert sorted(MIGRATIONS) == list(range(1, SCHEMA_VERSION))


def test_a_file_with_no_version_at_all_is_treated_as_current(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"appearance": {"theme": "dark"}}), encoding="utf-8")

    store = Settings(path, debounce_ms=0, migrate_legacy=False)
    assert store.read_only is False
    assert store.schema_version == SCHEMA_VERSION
    assert store.appearance.theme == "dark"


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------

def test_the_write_goes_through_a_tmp_file_and_leaves_none_behind(store):
    store.appearance.theme = "dark"
    assert store.path.exists()
    assert not store.path.with_name("settings.json.tmp").exists()


def test_a_failed_write_does_not_raise_and_does_not_damage_the_old_file(
        tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    store = Settings(path, debounce_ms=0, migrate_legacy=False)
    store.appearance.theme = "dark"
    good = path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    store.appearance.theme = "light"       # must not raise
    assert store.flush() is False
    assert path.read_text(encoding="utf-8") == good
    assert not path.with_name("settings.json.tmp").exists()


def test_the_directory_is_created_on_first_write(tmp_path):
    path = tmp_path / "Rapid PDF" / "settings.json"
    store = Settings(path, debounce_ms=0, migrate_legacy=False)
    assert not path.parent.exists()
    store.appearance.theme = "dark"
    assert path.exists()


# ---------------------------------------------------------------------------
# The debounce
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _pump_until(predicate, timeout_ms=3000):
    """Turn the event loop until `predicate` holds, or give up."""
    from PySide6.QtCore import QCoreApplication, QElapsedTimer

    clock = QElapsedTimer()
    clock.start()
    while clock.elapsed() < timeout_ms:
        QCoreApplication.processEvents()
        if predicate():
            return True
    return False


def test_a_burst_of_changes_is_one_write_not_one_per_change(qt_app, tmp_path):
    """The reason the debounce exists: the Organizer persists its zoom level on
    every Ctrl+wheel notch, and a spin is a dozen notches a second."""
    path = tmp_path / "settings.json"
    store = Settings(path, debounce_ms=60, migrate_legacy=False)

    for index in range(6):
        store.view.organizer_zoom_index = index
    assert not path.exists()        # nothing on disk while the notches land

    assert _pump_until(path.exists), "the debounced write never fired"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["view"]["organizer_zoom_index"] == 5   # the last one wins


def test_flush_writes_immediately_without_waiting_for_the_timer(qt_app, tmp_path):
    """What the close path relies on: the process may not live long enough for
    the timer, so quitting flushes by hand."""
    path = tmp_path / "settings.json"
    store = Settings(path, debounce_ms=10_000, migrate_legacy=False)
    store.appearance.theme = "dark"
    assert not path.exists()

    assert store.flush() is True
    assert json.loads(path.read_text(encoding="utf-8"))["appearance"]["theme"] == "dark"
