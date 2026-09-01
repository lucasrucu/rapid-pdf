"""Edit > Preferences (Ctrl+,): everything the app remembers, on one page.

WHY ONE PAGE. There are eight settings. A category sidebar for eight settings
is navigation for its own sake, and it hides half of them behind a click for no
gain. Five QGroupBoxes on one column is the whole design, and it stays the
design until the list is long enough that scrolling it is worse than clicking.

WHY THERE IS NO OK AND NO CANCEL. Every control here applies the moment it is
touched, and the only button is Close. That is not a shortcut, it is the only
option that is not a bug: the theme toggle and the page panel toggle already
apply instantly from the View menu, and a setting that commits immediately from
one surface and waits for OK on another is a setting the user cannot predict.
Committing everywhere is the version of that rule that needs no explaining.

WHY IT IS NOT MODAL. The dialog and the View menu are two views of the same
values, and the way to prove that to somebody is to let them press Ctrl+B while
the dialog is open and watch the checkbox move. A modal dialog would swallow
the shortcut and the demonstration.

TWO VIEWS, ONE VALUE, AND HOW THAT IS ENFORCED. Nothing here holds a copy of a
setting:

  - the page panel checkbox drives, and follows, the SAME QAction the View menu
    holds, so the setting is written in exactly one place (MainWindow's
    `_on_panel_toggled`);
  - the theme dropdown drives, and follows, the ThemeManager, which is what the
    View menu's Dark Mode action already toggles;
  - the page fit dropdown drives, and follows, `MainWindow.choose_fit_mode`,
    which is the one entry point the status bar's icon group also goes through.

Two-way `setChecked` / `setCurrentIndex` pairs do not loop: Qt emits nothing
when the value it is handed is the value already held, so each direction stops
at the first repeat. That is why there are no re-entrancy guards below.

WHAT IS DELIBERATELY NOT HERE. `view.organizer_zoom_index` is persisted but has
no control, because it is driven by Ctrl+wheel in the Organizer and a spinbox
for it would be a second, worse way to do something that already has a good
one.

`close.confirm_multiple_tabs` used to be in that list, kept out because tabs
did not exist and a permanently disabled checkbox would have been the one
control here that does nothing. Tabs exist now, so it has its checkbox, under
Closing with the other close behaviour. What it guards is losing your PLACE,
not your work: unsaved documents are prompted for one at a time whatever this
says, and the count warning is skipped entirely when any of them is dirty,
because two dialogs in a row asking the same question is worse than one.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
    QVBoxLayout,
)

from core.render_scale import AUTO as RENDER_SCALE_AUTO
from core.settings import dialog_start_dir, settings
from core.version import APP_VERSION
from ui.theme import ThemeMode

# (settings value, label). The order is the order they appear in the control.
_CLOSE_CHOICES = [
    ("window", "Close the window and everything in it"),
    ("document", "Keep Rapid PDF open with an empty window"),
]

_THEME_CHOICES = [
    ("light", "Light"),
    ("dark", "Dark"),
]


# The page-sharpness dropdown, in the order it is shown. Words, not numbers:
# "3.0" is meaningless to anybody who does not already know it is a raster
# scale, and the numbers are not even comparable between page sizes, since the
# same 3.0 is a cheap A4 and an unaffordable A1. Deliberately a fixed list and
# not a slider, for the same reason: a slider invites dragging to the end,
# which on a big drawing is a several-hundred-megabyte cache and a visibly
# frozen window, with nothing on the way to warn anybody.
RENDER_SCALE_LABELS = (
    (RENDER_SCALE_AUTO, "Automatic"),
    ("1.5", "Standard"),
    ("2", "Sharp"),
    ("3", "Sharpest"),
)


def _left(widget) -> QHBoxLayout:
    """A form field that keeps its natural width instead of filling the row.

    QFormLayout stretches the field column, which turns a two-item dropdown
    into a control the width of the dialog. The reader is meant to see how
    short the list is.
    """
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget)
    row.addStretch(1)
    return row


class PreferencesDialog(QDialog):
    """The settings page. Built against the window whose state it edits."""

    def __init__(self, window, parent=None):
        super().__init__(parent if parent is not None else window)
        self._window = window
        self.setWindowTitle("Preferences")
        self.setObjectName("PreferencesDialog")
        # Not a tool window: it is a real dialog, it just does not block.
        self.setModal(False)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(12)

        root.addWidget(self._build_startup_group())
        root.addWidget(self._build_closing_group())
        root.addWidget(self._build_appearance_group())
        root.addWidget(self._build_files_group())
        root.addWidget(self._build_view_group())
        root.addWidget(self._build_about_group())
        root.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        # Close is the only button, so it is also what Escape and the X do.
        buttons.button(QDialogButtonBox.StandardButton.Close).setDefault(True)
        root.addWidget(buttons)

        self._load_from_settings()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _build_startup_group(self) -> QGroupBox:
        """Session restore, and it is off until somebody asks for it.

        First in the column because it is about what happens before anything
        else does. One checkbox, because "reopen my tabs" is one question: how
        many tabs, which windows and where they were are all recorded either
        way and none of them is worth a control of its own.
        """
        box = QGroupBox("Startup")
        col = QVBoxLayout(box)
        self._restore_check = QCheckBox("Reopen the tabs I had open last time")
        self._restore_check.setToolTip(
            "Files that have moved or gone are skipped, and documents open "
            "when they are first looked at")
        self._restore_check.toggled.connect(self._on_restore_tabs)
        col.addWidget(self._restore_check)
        return box

    def _on_restore_tabs(self, checked: bool) -> None:
        settings().startup.restore_tabs = checked

    # ------------------------------------------------------------------
    # Closing
    # ------------------------------------------------------------------

    def _build_closing_group(self) -> QGroupBox:
        box = QGroupBox("Closing")
        col = QVBoxLayout(box)
        col.addWidget(QLabel("When I close a window"))

        self._close_radios: dict[str, QRadioButton] = {}
        for value, label in _CLOSE_CHOICES:
            radio = QRadioButton(label)
            radio.toggled.connect(
                lambda checked, v=value: self._on_close_choice(v, checked))
            col.addWidget(radio)
            self._close_radios[value] = radio

        self._confirm_tabs_check = QCheckBox(
            "Ask first when several documents are open")
        self._confirm_tabs_check.setToolTip(
            "Unsaved documents are always asked about, whatever this says")
        self._confirm_tabs_check.toggled.connect(self._on_confirm_tabs)
        col.addWidget(self._confirm_tabs_check)
        return box

    def _on_close_choice(self, value: str, checked: bool) -> None:
        # An exclusive radio group fires twice per change, once False and once
        # True. Only the True half is a decision.
        if checked:
            settings().close.x_closes = value

    def _on_confirm_tabs(self, checked: bool) -> None:
        settings().close.confirm_multiple_tabs = checked

    # ------------------------------------------------------------------
    # Appearance
    # ------------------------------------------------------------------

    def _build_appearance_group(self) -> QGroupBox:
        box = QGroupBox("Appearance")
        form = QFormLayout(box)

        self._theme_combo = QComboBox()
        for value, label in _THEME_CHOICES:
            self._theme_combo.addItem(label, value)
        self._theme_combo.currentIndexChanged.connect(self._on_theme_chosen)
        form.addRow("Theme", _left(self._theme_combo))

        theme = self._window.theme_manager()
        # The manager writes appearance.theme itself, so following it is the
        # same as following the setting, and it also catches Ctrl+D.
        theme.theme_changed.connect(self._sync_theme_combo)
        return box

    def _on_theme_chosen(self, index: int) -> None:
        value = self._theme_combo.itemData(index)
        try:
            mode = ThemeMode(value)
        except ValueError:
            return
        self._window.theme_manager().set_mode(mode)

    def _sync_theme_combo(self, _palette=None) -> None:
        mode = self._window.theme_manager().mode
        index = self._theme_combo.findData(mode.value)
        if index >= 0:
            self._theme_combo.setCurrentIndex(index)

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def _build_files_group(self) -> QGroupBox:
        box = QGroupBox("Files")
        col = QVBoxLayout(box)
        col.addWidget(QLabel("Open dialogs start in"))

        self._folder_radios: dict[str, QRadioButton] = {}

        last_used = QRadioButton("The last folder I used")
        last_used.toggled.connect(
            lambda checked: self._on_folder_mode("last_used", checked))
        col.addWidget(last_used)
        self._folder_radios["last_used"] = last_used

        fixed_row = QHBoxLayout()
        # No text: the path box beside it is the label, which is the only way
        # to say "this folder" without saying it twice. The accessible name is
        # what a screen reader reads instead.
        fixed = QRadioButton("")
        fixed.setAccessibleName("A folder I choose")
        fixed.setToolTip("Always start in one folder")
        fixed.toggled.connect(
            lambda checked: self._on_folder_mode("fixed", checked))
        fixed_row.addWidget(fixed)
        self._folder_radios["fixed"] = fixed

        self._folder_edit = QLineEdit()
        self._folder_edit.setPlaceholderText("Choose a folder")
        # editingFinished, not textChanged: a half-typed path is not a choice,
        # and writing one per keystroke would churn the settings file.
        self._folder_edit.editingFinished.connect(self._on_folder_typed)
        fixed_row.addWidget(self._folder_edit, stretch=1)

        self._browse_button = QPushButton("Browse…")
        self._browse_button.clicked.connect(self._on_browse)
        fixed_row.addWidget(self._browse_button)

        col.addLayout(fixed_row)
        return box

    def _on_folder_mode(self, value: str, checked: bool) -> None:
        if not checked:
            return
        settings().files.default_folder_mode = value
        self._sync_folder_enabled()

    def _sync_folder_enabled(self) -> None:
        """The path box only means anything under the fixed-folder choice."""
        fixed = settings().files.default_folder_mode == "fixed"
        self._folder_edit.setEnabled(fixed)
        self._browse_button.setEnabled(fixed)

    def _on_folder_typed(self) -> None:
        text = self._folder_edit.text().strip()
        if text and not Path(text).is_dir():
            # Typed a folder that is not there. Say so in the box rather than
            # in a message box, and leave the stored value alone.
            self._folder_edit.setText(settings().files.default_folder)
            return
        settings().files.default_folder = text

    def _on_browse(self) -> None:
        start = self._folder_edit.text().strip() or dialog_start_dir()
        chosen = QFileDialog.getExistingDirectory(
            self, "Folder to open file dialogs in", start)
        if not chosen:
            return
        settings().files.default_folder = chosen
        self._folder_edit.setText(chosen)

    # ------------------------------------------------------------------
    # View
    # ------------------------------------------------------------------

    def _build_view_group(self) -> QGroupBox:
        box = QGroupBox("View")
        col = QVBoxLayout(box)

        panel_row = QHBoxLayout()
        self._panel_check = QCheckBox("Show the page panel")
        panel_action = self._window.page_panel_action()
        self._panel_check.toggled.connect(panel_action.setChecked)
        panel_action.toggled.connect(self._panel_check.setChecked)
        panel_row.addWidget(self._panel_check)
        panel_row.addStretch(1)

        shortcut = QLabel(panel_action.shortcut().toString())
        shortcut.setObjectName("PreferencesHint")
        shortcut.setEnabled(False)      # reads as a hint, not as a control
        panel_row.addWidget(shortcut)
        col.addLayout(panel_row)

        form = QFormLayout()
        self._fit_combo = QComboBox()
        for mode, label in self._window.fit_mode_labels().items():
            self._fit_combo.addItem(label, mode)
        self._fit_combo.currentIndexChanged.connect(self._on_fit_chosen)
        form.addRow("Default page fit", _left(self._fit_combo))

        self._scale_combo = QComboBox()
        for value, label in RENDER_SCALE_LABELS:
            self._scale_combo.addItem(label, value)
        index = self._scale_combo.findData(settings().view.render_scale)
        self._scale_combo.setCurrentIndex(max(index, 0))
        self._scale_combo.currentIndexChanged.connect(self._on_render_scale_chosen)
        form.addRow("Page sharpness", _left(self._scale_combo))
        col.addLayout(form)

        # The one control on this page that does NOT apply instantly, so it is
        # the one control that has to say so. It cannot: a page's rendered
        # pixels are the coordinate space its annotations are stored in, so
        # re-rendering an open document at a new scale would mean moving every
        # mark on it in step. Rather than leave the user to discover that
        # nothing happened, the hint sits under the dropdown and tells them
        # where the change will show up.
        hint = QLabel("Applies to documents opened from now on. "
                      "Automatic sharpens ordinary pages and leaves large "
                      "drawings at their current speed.")
        hint.setObjectName("PreferencesHint")
        hint.setWordWrap(True)
        hint.setEnabled(False)          # reads as a hint, not as a control
        col.addWidget(hint)

        self._window.fit_mode_chosen.connect(self._sync_fit_combo)
        return box

    def _on_render_scale_chosen(self, index: int) -> None:
        value = self._scale_combo.itemData(index)
        if value:
            settings().view.render_scale = value

    def _on_fit_chosen(self, index: int) -> None:
        mode = self._fit_combo.itemData(index)
        if mode:
            self._window.choose_fit_mode(mode)

    def _sync_fit_combo(self, mode: str) -> None:
        index = self._fit_combo.findData(mode)
        if index >= 0:
            self._fit_combo.setCurrentIndex(index)

    # ------------------------------------------------------------------
    # About
    # ------------------------------------------------------------------

    def _build_about_group(self) -> QGroupBox:
        """The version, and the button that asks whether it is the newest one.

        The app knew its own version and never said so anywhere a user would
        look. It says so here, and next to the control that is about versions,
        which is the only place the answer is any use.
        """
        box = QGroupBox("About")
        row = QHBoxLayout(box)

        self._version_label = QLabel(f"Rapid PDF v{APP_VERSION}")
        self._version_label.setObjectName("PreferencesVersion")
        self._version_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self._version_label)
        row.addStretch(1)

        self._update_button = QPushButton("Check for updates")
        self._update_button.clicked.connect(self._window.check_for_updates)
        row.addWidget(self._update_button)
        return box

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Re-read the state. The window calls this when it re-shows a dialog
        it kept from last time."""
        self._load_from_settings()

    def _load_from_settings(self) -> None:
        """Put every control where the current state says it should be.

        Between loads the controls follow the app rather than the file, because
        everything that writes the file goes through the same action, manager
        or method these are bound to.
        """
        store = settings()

        self._restore_check.setChecked(store.startup.restore_tabs)

        radio = self._close_radios.get(store.close.x_closes)
        if radio is not None:
            radio.setChecked(True)
        self._confirm_tabs_check.setChecked(store.close.confirm_multiple_tabs)

        self._sync_theme_combo()

        mode = store.files.default_folder_mode
        folder_radio = self._folder_radios.get(mode)
        if folder_radio is not None:
            folder_radio.setChecked(True)
        self._folder_edit.setText(store.files.default_folder)
        self._sync_folder_enabled()

        self._panel_check.setChecked(self._window.page_panel_action().isChecked())
        self._sync_fit_combo(self._window.current_fit_mode())

    # ------------------------------------------------------------------
    # Closing the dialog
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        """Get the file on disk on the way out.

        Writes are debounced by 250 ms and somebody who closes Preferences and
        then closes the app has probably done both inside that window.
        """
        settings().flush()
        super().closeEvent(event)
