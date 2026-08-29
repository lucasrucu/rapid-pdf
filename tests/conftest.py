"""Test setup.

Qt needs a platform plugin even to paint into a QImage, so force the offscreen
one before PySide6 is imported anywhere. That keeps the whole set runnable on a
headless box and, more usefully, on a laptop without popping windows.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(autouse=True, scope="session")
def _settings_off_the_real_file(tmp_path_factory):
    """Every test runs against a throwaway settings file.

    Anything that builds a MainWindow, a PageOrganizer or a ThemeManager reads
    the app-wide store, and without this a test run would read, migrate and
    rewrite the settings of whoever is running it. Legacy migration is off for
    the same reason: the suite has no business reading the registry.

    A per-test fixture can still swap in its own store (see
    test_organizer_zoom.py); this is the floor, not the only isolation.
    """
    from core.settings import Settings, set_settings

    path = tmp_path_factory.mktemp("settings") / "settings.json"
    store = Settings(path, debounce_ms=0, migrate_legacy=False)
    previous = set_settings(store)
    yield store
    set_settings(previous)
