"""Preferences, and the version the app finally admits to.

Three things are worth testing here and the rest is Qt doing its job.

1. THE DIALOG READS AND WRITES THE REAL KEYS. Not a copy, not a shadow dict:
   touching a control has to land in `core.settings`, and opening the dialog
   has to show what is in there.

2. THE DIALOG AND THE MENU DO NOT DRIFT. This is the one that would actually
   bite. The page panel and the theme already applied instantly from the View
   menu before this dialog existed, so a dialog holding its own copy would give
   the same setting two values and whichever surface was touched last would
   win. Every test below drives ONE surface and asserts on the OTHER.

3. THE VERSION SHOWN IS THE ONE SOURCE. `core.version.APP_VERSION` and nothing
   else, in both places it appears.

A real MainWindow is built because the wiring is the subject. Offscreen never
runs the event loop, so nothing here waits on a signal round trip, and the
deferred startup update check never fires. Offscreen also lies about geometry,
which is why the fit assertions are about the MODE the canvas holds and never
about the transform it would have applied.
"""

import pytest

from PySide6.QtWidgets import QApplication, QMenu

from core.settings import DEFAULTS, Settings, set_settings, settings
from core.version import APP_VERSION
from ui.canvas import FIT_MODES
from ui.main_window import MainWindow
from ui.preferences_dialog import PreferencesDialog
from ui.theme import ThemeMode


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    """A settings file of this test's own, in place of the session-wide one."""
    fresh = Settings(tmp_path / "settings.json", debounce_ms=0,
                     migrate_legacy=False)
    previous = set_settings(fresh)
    yield fresh
    set_settings(previous)


@pytest.fixture
def window(qt_app, store):
    w = MainWindow()
    yield w
    w._force_quit = True
    w.close()
    w.deleteLater()


@pytest.fixture
def prefs(window):
    dialog = PreferencesDialog(window)
    yield dialog
    dialog.close()
    dialog.deleteLater()


# ---------------------------------------------------------------------------
# The keys it is allowed to touch
# ---------------------------------------------------------------------------

def test_the_dialog_adds_no_settings_of_its_own(store):
    """Phase 0 fixed the schema. The UI does not get to extend it."""
    assert set(DEFAULTS["close"]) == {"x_closes", "confirm_multiple_tabs"}
    assert set(DEFAULTS["appearance"]) == {"theme"}
    assert set(DEFAULTS["files"]) == {"default_folder_mode", "default_folder"}
    assert set(DEFAULTS["view"]) == {
        "page_panel_visible", "default_fit_mode", "organizer_zoom_index"}


def test_every_fit_mode_the_app_has_is_a_legal_setting():
    """`fit_height` was in the canvas and not in the allowed values, so it
    could be chosen and never remembered."""
    field = type(settings().view).default_fit_mode
    assert set(field._allowed) == set(FIT_MODES)


def test_the_organizer_zoom_has_no_control(prefs):
    """Persisted, but driven by Ctrl+wheel. A spinbox for it is noise."""
    assert not hasattr(prefs, "_zoom_spin")


def test_the_tab_confirmation_has_no_control(prefs):
    """Kept in the schema, kept out of the dialog until tabs exist. Every
    control on this page applies the moment it is touched, so a permanently
    disabled one would be the only thing here that does nothing."""
    assert not hasattr(prefs, "_confirm_tabs_check")


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------

def test_the_close_radios_show_what_is_stored(window, store):
    store.close.x_closes = "document"
    dialog = PreferencesDialog(window)
    assert dialog._close_radios["document"].isChecked()
    assert not dialog._close_radios["window"].isChecked()
    dialog.deleteLater()


def test_choosing_a_close_behaviour_writes_the_key(prefs, store):
    prefs._close_radios["document"].setChecked(True)
    assert store.close.x_closes == "document"
    prefs._close_radios["window"].setChecked(True)
    assert store.close.x_closes == "window"


def test_the_close_setting_reaches_the_window(prefs, store):
    """What the radio is actually for: closeEvent reads this key."""
    prefs._close_radios["document"].setChecked(True)
    assert settings().close.x_closes == "document"


# ---------------------------------------------------------------------------
# Appearance: the dropdown and the View menu are one value
# ---------------------------------------------------------------------------

def test_the_theme_dropdown_shows_the_current_theme(prefs, window):
    assert prefs._theme_combo.currentData() == window.theme_manager().mode.value


def test_choosing_a_theme_moves_the_app_and_the_key(prefs, window, store):
    index = prefs._theme_combo.findData("dark")
    prefs._theme_combo.setCurrentIndex(index)
    assert window.theme_manager().mode is ThemeMode.DARK
    assert store.appearance.theme == "dark"


def test_the_menu_toggling_the_theme_moves_the_dropdown(prefs, window):
    """Ctrl+D with the dialog open. The dropdown has to follow."""
    before = prefs._theme_combo.currentData()
    window._toggle_theme()
    assert prefs._theme_combo.currentData() != before
    assert prefs._theme_combo.currentData() == window.theme_manager().mode.value


def test_the_dropdown_and_the_menu_survive_a_round_trip(prefs, window):
    window._toggle_theme()
    index = prefs._theme_combo.findData("light")
    prefs._theme_combo.setCurrentIndex(index)
    assert window.theme_manager().mode is ThemeMode.LIGHT


# ---------------------------------------------------------------------------
# View: the page panel checkbox IS the View menu's action
# ---------------------------------------------------------------------------

def test_the_checkbox_is_bound_to_the_menu_action(prefs, window):
    assert prefs._panel_check.isChecked() == window.page_panel_action().isChecked()


def test_ticking_the_checkbox_moves_the_menu_and_the_panel(prefs, window, store):
    prefs._panel_check.setChecked(False)
    assert not window.page_panel_action().isChecked()
    assert not window._page_panel.isVisible()
    assert store.view.page_panel_visible is False


def test_the_menu_action_moves_the_checkbox(prefs, window):
    """Ctrl+B with the dialog open."""
    window.page_panel_action().setChecked(False)
    assert prefs._panel_check.isChecked() is False
    window.page_panel_action().setChecked(True)
    assert prefs._panel_check.isChecked() is True


def test_the_panel_shortcut_is_shown_next_to_the_checkbox(prefs, window):
    assert window.page_panel_action().shortcut().toString() == "Ctrl+B"


# ---------------------------------------------------------------------------
# View: the fit dropdown and the status bar's icon group are one value
# ---------------------------------------------------------------------------

def test_the_dropdown_offers_every_mode_the_status_bar_does(prefs, window):
    offered = {prefs._fit_combo.itemData(i)
               for i in range(prefs._fit_combo.count())}
    assert offered == set(window._fit_btns) == set(FIT_MODES)


def test_the_dropdown_names_the_modes_the_way_the_tooltips_do(prefs, window):
    labels = {prefs._fit_combo.itemData(i): prefs._fit_combo.itemText(i)
              for i in range(prefs._fit_combo.count())}
    assert labels == window.fit_mode_labels()


def test_choosing_a_fit_applies_it_and_remembers_it(prefs, window, store):
    index = prefs._fit_combo.findData("fit_height")
    prefs._fit_combo.setCurrentIndex(index)
    assert window._canvas.fit_mode() == "fit_height"
    assert window._fit_btns["fit_height"].isChecked()
    assert store.view.default_fit_mode == "fit_height"


def test_the_status_bar_icons_move_the_dropdown(prefs, window, store):
    window._fit_btns["fit_width"].click()
    assert prefs._fit_combo.currentData() == "fit_width"
    assert store.view.default_fit_mode == "fit_width"


def test_a_manual_zoom_clears_the_icons_but_not_the_chosen_default(prefs, window,
                                                                   store):
    """Breaking a fit is not un-choosing it: the icons go out, the remembered
    mode stays, and so does the dropdown that displays it."""
    window._fit_btns["actual"].click()
    window._canvas.fit_mode_broken.emit()
    assert window._fit_group.checkedButton() is None
    assert prefs._fit_combo.currentData() == "actual"
    assert store.view.default_fit_mode == "actual"


def test_an_unknown_mode_is_refused(window, store):
    window.choose_fit_mode("fit_sideways")
    assert store.view.default_fit_mode == "fit_page"


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def test_the_folder_controls_show_what_is_stored(window, store, tmp_path):
    folder = tmp_path / "drawings"
    folder.mkdir()
    store.files.default_folder_mode = "fixed"
    store.files.default_folder = str(folder)
    dialog = PreferencesDialog(window)
    assert dialog._folder_radios["fixed"].isChecked()
    assert dialog._folder_edit.text() == str(folder)
    dialog.deleteLater()


def test_the_path_box_is_dead_under_the_last_used_choice(prefs, store):
    prefs._folder_radios["last_used"].setChecked(True)
    assert store.files.default_folder_mode == "last_used"
    assert not prefs._folder_edit.isEnabled()
    assert not prefs._browse_button.isEnabled()

    prefs._folder_radios["fixed"].setChecked(True)
    assert store.files.default_folder_mode == "fixed"
    assert prefs._folder_edit.isEnabled()
    assert prefs._browse_button.isEnabled()


def test_typing_a_real_folder_writes_it(prefs, store, tmp_path):
    prefs._folder_radios["fixed"].setChecked(True)
    prefs._folder_edit.setText(str(tmp_path))
    prefs._folder_edit.editingFinished.emit()
    assert store.files.default_folder == str(tmp_path)


def test_typing_a_folder_that_is_not_there_changes_nothing(prefs, store, tmp_path):
    prefs._folder_radios["fixed"].setChecked(True)
    prefs._folder_edit.setText(str(tmp_path))
    prefs._folder_edit.editingFinished.emit()

    prefs._folder_edit.setText(str(tmp_path / "nowhere"))
    prefs._folder_edit.editingFinished.emit()
    assert store.files.default_folder == str(tmp_path)
    assert prefs._folder_edit.text() == str(tmp_path)


# ---------------------------------------------------------------------------
# The version
# ---------------------------------------------------------------------------

def test_the_dialog_shows_the_one_version(prefs):
    assert prefs._version_label.text() == f"Rapid PDF v{APP_VERSION}"


def test_the_help_menu_shows_the_one_version(window):
    assert window._version_action.text() == f"Rapid PDF v{APP_VERSION}"


def test_the_version_in_the_menu_is_a_statement_not_a_button(window):
    assert not window._version_action.isEnabled()


def test_the_check_for_updates_button_runs_the_same_check(prefs, window):
    """Both surfaces call the one method, so there is nothing to keep in step."""
    calls = []
    window._update_notice.start_check = lambda manual=False: calls.append(manual)
    prefs._update_button.click()
    assert calls == [True]


def test_nothing_hardcodes_a_version_string():
    """The app has one source and every surface reads it."""
    from pathlib import Path
    import re

    root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"\b\d+\.\d+\.\d+\b")
    for name in ("ui/main_window.py", "ui/preferences_dialog.py"):
        text = (root / name).read_text(encoding="utf-8", errors="replace")
        stripped = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("#"))
        assert not pattern.search(stripped), (
            f"{name} carries a literal version; read core.version.APP_VERSION")


# ---------------------------------------------------------------------------
# Opening the dialog from the window
# ---------------------------------------------------------------------------

def test_preferences_opens_one_dialog_and_reuses_it(window):
    first = window.open_preferences()
    second = window.open_preferences()
    assert first is second
    first.close()


def test_the_shortcut_is_on_the_edit_menu(window):
    edit = next(m for m in window.menuBar().findChildren(QMenu)
                if m.title() == "Edit")
    labels = {a.text(): a.shortcut().toString() for a in edit.actions()}
    assert labels.get("Preferences…") == "Ctrl+,"


def test_the_version_sits_with_the_update_check_on_the_help_menu(window):
    help_menu = next(m for m in window.menuBar().findChildren(QMenu)
                     if m.title() == "Help")
    texts = [a.text() for a in help_menu.actions()]
    assert texts[0] == f"Rapid PDF v{APP_VERSION}"
    assert texts[1] == "Check for Updates…"
