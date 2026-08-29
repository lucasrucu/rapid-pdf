"""Carrying the three old QSettings keys into settings.json, once.

Rapid PDF up to 1.5.0 kept `theme/mode`, `ui/page_panel_visible` and
`ui/organizer_zoom_index` in `HKCU\\Software\\Lucas\\Rapid PDF`. Upgrading must
not silently reset somebody's dark mode and hidden page panel, so the first run
with no settings.json reads those three keys and folds them into the defaults.

Two things this has to get right beyond the obvious:

- **The registry key is left alone.** An older build installed back onto the
  same machine still reads it, and deleting user data as a tidy-up is never
  worth the one directory entry it saves.
- **It happens once.** A settings.json that exists, even an almost empty one,
  means the migration has already run; reading the registry again would undo
  every change made since.

The tests write into a throwaway QSettings scope rather than the real one, so
running them cannot disturb an actual install.
"""

import json

import pytest
from PySide6.QtCore import QSettings

from core.settings import DEFAULTS, Settings

# A scope of our own. Nothing shipped ever writes here.
TEST_ORG = "Lucas"
TEST_APP = "Rapid PDF Migration Tests"


@pytest.fixture
def legacy():
    """An empty legacy store, and a factory the Settings store can read it by."""
    store = QSettings(TEST_ORG, TEST_APP)
    store.clear()
    store.sync()
    yield store
    store.clear()
    store.sync()


@pytest.fixture
def legacy_factory():
    return lambda: QSettings(TEST_ORG, TEST_APP)


def _build(path, legacy_factory):
    return Settings(path, debounce_ms=0, legacy_settings=legacy_factory)


# ---------------------------------------------------------------------------

def test_all_three_legacy_keys_are_carried_over(tmp_path, legacy, legacy_factory):
    legacy.setValue("theme/mode", "dark")
    legacy.setValue("ui/page_panel_visible", False)
    legacy.setValue("ui/organizer_zoom_index", 5)
    legacy.sync()

    path = tmp_path / "settings.json"
    store = _build(path, legacy_factory)

    assert store.appearance.theme == "dark"
    assert store.view.page_panel_visible is False
    assert store.view.organizer_zoom_index == 5

    # And it is on disk straight away, not waiting on a debounce: the whole
    # point is that the NEXT run reads JSON, and that run may follow a crash.
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["appearance"]["theme"] == "dark"
    assert written["view"]["page_panel_visible"] is False
    assert written["view"]["organizer_zoom_index"] == 5
    assert written["schema_version"] == 1


def test_only_the_keys_that_were_set_move_the_defaults(tmp_path, legacy,
                                                       legacy_factory):
    """A partial legacy store leaves the rest of the defaults where they are."""
    legacy.setValue("theme/mode", "dark")
    legacy.sync()

    store = _build(tmp_path / "settings.json", legacy_factory)

    assert store.appearance.theme == "dark"
    assert store.view.page_panel_visible is True          # untouched default
    assert store.view.organizer_zoom_index == DEFAULTS["view"]["organizer_zoom_index"]
    assert store.close.x_closes == "window"


def test_string_booleans_from_the_registry_still_read_as_booleans(
        tmp_path, legacy, legacy_factory):
    """QSettings hands values back as strings on some backends, so
    "false" has to mean False rather than a non-empty string."""
    legacy.setValue("ui/page_panel_visible", "false")
    legacy.sync()

    store = _build(tmp_path / "settings.json", legacy_factory)
    assert store.view.page_panel_visible is False


def test_the_registry_key_is_left_exactly_as_it_was(tmp_path, legacy,
                                                    legacy_factory):
    legacy.setValue("theme/mode", "dark")
    legacy.setValue("ui/organizer_zoom_index", 5)
    legacy.sync()
    before = {key: legacy.value(key) for key in legacy.allKeys()}

    _build(tmp_path / "settings.json", legacy_factory)

    after = QSettings(TEST_ORG, TEST_APP)
    assert {key: after.value(key) for key in after.allKeys()} == before


def test_an_existing_settings_file_stops_the_migration_dead(tmp_path, legacy,
                                                            legacy_factory):
    """The migration is first-run only. A settings.json that is already there
    means the user has since made choices, and the registry is stale."""
    legacy.setValue("theme/mode", "dark")
    legacy.sync()

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "appearance": {"theme": "light"},
    }), encoding="utf-8")

    store = _build(path, legacy_factory)
    assert store.appearance.theme == "light"     # the file wins, not the registry


def test_an_empty_registry_writes_no_file_at_all(tmp_path, legacy, legacy_factory):
    """Nothing to carry over means a fresh install: run on defaults and leave
    the disk alone until something actually changes."""
    path = tmp_path / "settings.json"
    store = _build(path, legacy_factory)

    assert not path.exists()
    assert store.appearance.theme == "light"


def test_junk_in_the_registry_falls_back_to_the_defaults(tmp_path, legacy,
                                                         legacy_factory):
    legacy.setValue("theme/mode", "chartreuse")
    legacy.setValue("ui/organizer_zoom_index", "big")
    legacy.setValue("ui/page_panel_visible", "perhaps")
    legacy.sync()

    store = _build(tmp_path / "settings.json", legacy_factory)

    assert store.appearance.theme == "light"
    assert store.view.organizer_zoom_index == DEFAULTS["view"]["organizer_zoom_index"]
    assert store.view.page_panel_visible is True


def test_a_legacy_store_that_cannot_be_read_is_not_fatal(tmp_path):
    """Whatever goes wrong reaching the registry, the app still starts."""
    def explode():
        raise OSError("registry unavailable")

    store = Settings(tmp_path / "settings.json", debounce_ms=0,
                     legacy_settings=explode)
    assert store.appearance.theme == "light"


def test_the_migration_can_be_switched_off(tmp_path, legacy, legacy_factory):
    """What the test suite itself relies on: no run reads the real registry."""
    legacy.setValue("theme/mode", "dark")
    legacy.sync()

    store = Settings(tmp_path / "settings.json", debounce_ms=0,
                     legacy_settings=legacy_factory, migrate_legacy=False)
    assert store.appearance.theme == "light"
