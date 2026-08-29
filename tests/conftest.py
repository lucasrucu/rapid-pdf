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


@pytest.fixture(autouse=True)
def _fresh_window_registry():
    """Every test gets its own WindowRegistry, and none of them ends the run.

    Two separate problems, one fixture. From phase 3 a MainWindow joins the
    registry as it is built, so without this every window any test ever made
    would pile up in one process-wide list and a later test's routing would
    reach into a widget the test that made it has finished with.

    And the registry QUITS THE APPLICATION when its last window leaves, which
    is exactly right in the app and would be a shot at the test runner here.
    It is disarmed by default; the one test that asserts on it turns it back on
    and watches `QApplication.quit` rather than letting it fire.

    Windows built before the reset keep a reference to the OLD registry object
    and unregister from that, which is what makes the swap safe mid-suite.
    """
    from ui.window_registry import WindowRegistry

    WindowRegistry.reset_instance()
    WindowRegistry.instance().quit_on_last_window = False
    yield
    WindowRegistry.reset_instance()
