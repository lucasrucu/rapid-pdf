"""The application shell: menus, status bar, theme, updates, window lifetime.

It holds exactly ONE DocumentView (ui/document_view.py), which owns the open
PDF and everything that belongs to it. Phase 1 of docs/tabs-plan.md: the window
used to BE the document, and nothing could hold a second one.

WHAT LIVES UP HERE, AND WHY. Chrome, and only chrome. The menu bar, the status
bar, the update strip, Preferences, the theme, `_force_quit` and `closeEvent`.
Three of those controls act on a document and still belong here:

  - the status bar's PAGE BOX. It is drawn in the status bar, so it is the
    frame's. It follows the front view (see `_on_view_page_changed`) and drives
    it (`_on_page_jump`), which is a rebind rather than a second copy.
  - the status bar's FIT GROUP, and `choose_fit_mode` / `fit_mode_chosen` with
    it. Same reason, plus one more: `view.default_fit_mode` is a single
    app-wide setting and the Preferences dropdown is the other view of it, so
    one owner is the point. Only the applying goes down to the canvas.
  - the single-key TOOL SHORTCUTS (v/h/r/l/t). Two live DocumentViews would
    each own a window-context QShortcut for "v", which Qt reports as ambiguous
    and then routes to neither.

The search bar went the other way and lives in the view: its hits are page
numbers in one particular document.

WHAT PHASE 2 HAS TO REBIND. Everything reached through `self._view`, plus the
Edit menu's Undo/Redo actions, which are built from the front view's undo stack
(`QUndoStack.createUndoAction`) and so are bound to one document's history.
"""

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QMessageBox, QStatusBar, QApplication,
    QToolButton, QButtonGroup,
)
from PySide6.QtCore import QTimer, Qt, QSize, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut, QIcon

# How long after the window is up before the update check goes out. Nothing
# about it blocks startup (it runs on its own thread), but the first render
# and the first PDF load should have the machine to themselves; an update is
# never urgent enough to compete with them.
UPDATE_CHECK_DELAY_MS = 1500

# The status bar's view-mode group: (mode, qtawesome id, tooltip, text fallback).
# The mode names match ui.canvas.FIT_MODES and core.settings' default_fit_mode.
_FIT_CONTROLS = [
    ("fit_page",   "mdi6.fit-to-page-outline",     "Fit page",           "Pg"),
    ("fit_width",  "mdi6.arrow-expand-horizontal", "Fit width",          "W"),
    ("fit_height", "mdi6.arrow-expand-vertical",   "Fit height",         "H"),
    ("actual",     "mdi6.percent-outline",         "100% (actual size)", "100"),
]

from core.settings import settings
from core.resources import app_icon_path
from core.version import APP_VERSION
from ui.update_notice import UpdateNotice
from ui.canvas import FIT_MODES
from ui.document_view import DocumentView
from ui.preferences_dialog import PreferencesDialog
from ui.page_jump import PageJump
from ui.theme import ThemeManager, apply_mica, themed_icon, qtawesome_available, LIGHT


class MainWindow(QMainWindow):
    #: The page-fit mode was chosen, by whichever surface chose it. The status
    #: bar's icon group and the Preferences dropdown are two views of this one
    #: value, so both go through choose_fit_mode() and both listen here.
    fit_mode_chosen = Signal(str)

    def __init__(self, theme: ThemeManager | None = None):
        super().__init__()
        self._prefs_dialog = None  # the one Preferences window, while it is open
        # Theme: use the passed-in manager, or stand one up (e.g. tests/smoke).
        self._theme = theme or ThemeManager(QApplication.instance())
        # The one open document, and everything that belongs to it. Built here
        # rather than in _setup_ui so the title below has something to read.
        self._view = DocumentView(self._theme)
        self._force_quit = False # Quit menu wants a real app quit, not "close PDF"
        self._session_ending = False  # Windows is ending the session; never block it
        self._update_title()
        self.setMinimumSize(1100, 720)
        icon_path = app_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))
        self._setup_ui()
        self._setup_menu()
        self._setup_shortcuts()
        # Code-drawn surfaces (the canvas page backdrop) follow the theme too.
        self._apply_theme_surfaces(self._theme.palette)
        self._theme.theme_changed.connect(self._apply_theme_surfaces)
        # Optional Win11 Mica backdrop (silent no-op elsewhere).
        apply_mica(self, self._theme.is_dark)
        self._connect_session_manager()
        # Ask GitHub whether there is a newer release. Off the GUI thread, and
        # after the window is up: see UPDATE_CHECK_DELAY_MS and
        # ui/update_notice.py. Offline this does nothing at all, silently.
        QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self._update_notice.start_check)

    @property
    def view(self) -> DocumentView:
        """The document this window is showing.

        Phase 2 makes this the FRONT one of several. Everything that reaches
        through it is written so that stays the only change.
        """
        return self._view

    def _apply_theme_surfaces(self, palette):
        """Re-tint the bits QSS can't reach: the canvas backdrop and its selection
        chrome, the thumbnail delegates, the toggle action's icon/label, and the
        Mica header on a switch."""
        self._view.apply_palette(palette)
        self._update_notice.apply_palette(palette)
        self._retint_fit_icons(palette)
        if hasattr(self, "_theme_action"):
            dark = self._theme.is_dark
            self._theme_action.setText("Light Mode" if dark else "Dark Mode")
            self._theme_action.setIcon(
                themed_icon("mdi6.weather-sunny" if dark else "mdi6.weather-night",
                            palette.text))
        apply_mica(self, self._theme.is_dark)

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Update strip, above everything, hidden until GitHub says there is a
        # newer release. Nothing in the layout moves while it stays hidden.
        self._update_notice = UpdateNotice()
        self._update_notice.staged_ready.connect(self._on_update_staged)
        root.addWidget(self._update_notice)

        # The document, in the same place in the layout the Editor/Organizer
        # switcher used to sit (that switcher is now inside it).
        root.addWidget(self._view)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Open a PDF to start  (Ctrl+O)")

        # Page box, immediately left of the fit group. Permanent widgets are laid
        # out left to right in the order they're added, so this one is added first.
        self._page_jump = PageJump()
        self._page_jump.page_requested.connect(self._on_page_jump)
        self._status.addPermanentWidget(self._page_jump)

        self._status.addPermanentWidget(self._build_fit_group())

        # Last, so every piece of chrome a view drives already exists.
        self._connect_view(self._view)

    def _connect_view(self, view: DocumentView):
        """Wire one document view to the window chrome it drives.

        A method rather than five lines inline because phase 2 runs it once per
        tab, and the disconnect that pairs with it goes next to it.
        """
        view.title_changed.connect(self._update_title)
        view.status_message.connect(self._status_message)
        view.page_changed.connect(self._on_view_page_changed)
        view.fit_mode_broken.connect(self._on_fit_mode_broken)
        view.default_fit_requested.connect(self._apply_default_fit)
        view.close_requested.connect(self._on_view_close_requested)

    def _status_message(self, text: str):
        self._status.showMessage(text)

    def _on_view_page_changed(self, page_num: int):
        """Keep the status bar's page box on the view's page.

        The box is window chrome driven by document state, which is why it
        follows a signal instead of being poked from inside the view.
        """
        if self._view.has_document():
            self._page_jump.set_total(self._view.page_count())
            self._page_jump.set_current_page(page_num)
        else:
            self._page_jump.set_total(0)

    def _on_view_close_requested(self):
        """The view was asked to go (File > Close PDF).

        Nothing to do while the window holds exactly one: it stays, emptied,
        which is what Close PDF has always done. Phase 2 is where the tab goes
        with it, and this is the seam it hangs off.
        """

    def _build_fit_group(self) -> QWidget:
        """The view-mode control: one icon per mode, exactly one of them active.

        Was a single text "Fit" button that only ever meant fit-page. The four
        modes are siblings, so they read as a group of icons with the mode named
        in the tooltip rather than a lone word that has to stand for all of them.
        """
        box = QWidget()
        box.setObjectName("fitGroup")
        row = QHBoxLayout(box)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(1)

        self._fit_group = QButtonGroup(self)
        self._fit_group.setExclusive(True)
        self._fit_btns: dict[str, QToolButton] = {}
        for mode, icon, tip, fallback in _FIT_CONTROLS:
            btn = QToolButton()
            btn.setObjectName("fitmode")
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            glyph = themed_icon(icon, LIGHT.text_dim) if qtawesome_available() else QIcon()
            if glyph.isNull():
                # No icon font on this machine, or a glyph id this version of the
                # font doesn't carry: a short label beats an invisible button.
                # Same fallback the page strip and the toolbar use.
                btn.setText(fallback)
                btn.setStyleSheet("font-size: 10px;")
            else:
                btn.setIcon(glyph)
                btn.setIconSize(QSize(15, 15))
            btn.clicked.connect(lambda _, m=mode: self._on_fit_clicked(m))
            self._fit_group.addButton(btn)
            self._fit_btns[mode] = btn
            row.addWidget(btn)
        return box

    def _setup_menu(self):
        mb = self.menuBar()

        fm = mb.addMenu("File")
        self._add_action(fm, "Open / Combine PDFs…", self.open_pdf, QKeySequence.StandardKey.Open)
        self._add_action(fm, "Combine PDFs…", self.combine_pdfs)
        self._add_action(fm, "Close PDF", self.close_pdf, "Ctrl+W")
        fm.addSeparator()
        self._add_action(fm, "Save", self.save_pdf, QKeySequence.StandardKey.Save)
        self._add_action(fm, "Save As…", self.save_pdf_as, "Ctrl+Shift+S")
        fm.addSeparator()
        self._add_action(fm, "Enhance for Search (OCR)…", self.enhance_for_search)
        fm.addSeparator()
        # Ctrl+Q spelled out, not QKeySequence.StandardKey.Quit. On Windows that
        # standard key resolves to the hardware Exit media key, so the menu
        # displayed "Exit" as its shortcut and Quit had no binding anybody could
        # actually press.
        self._add_action(fm, "Quit", self._quit_app, "Ctrl+Q")

        em = mb.addMenu("Edit")
        # Bound to the front view's stack. Phase 2 rebuilds these on a tab
        # switch; there is one history per document and no way around that
        # (PDFCanvas.set_document clears the stack).
        undo_act = self._view.undo_stack().createUndoAction(self, "Undo")
        undo_act.setShortcut(QKeySequence.StandardKey.Undo)
        redo_act = self._view.undo_stack().createRedoAction(self, "Redo")
        redo_act.setShortcut(QKeySequence.StandardKey.Redo)
        em.addAction(undo_act)
        em.addAction(redo_act)
        em.addSeparator()
        self._add_action(em, "Find…", self._open_search, QKeySequence.StandardKey.Find)
        em.addSeparator()
        self._add_action(em, "Copy", self.copy_selection, QKeySequence.StandardKey.Copy)
        self._add_action(em, "Paste", self.paste, QKeySequence.StandardKey.Paste)
        self._add_action(em, "Delete Selected", self._delete_key,
                         QKeySequence.StandardKey.Delete)
        em.addSeparator()
        self._add_action(em, "Bring to Front", self._bring_to_front, "Ctrl+]")
        self._add_action(em, "Send to Back", self._send_to_back, "Ctrl+[")
        em.addSeparator()
        # Ctrl+, is where every other app on this machine puts it.
        self._add_action(em, "Preferences…", self.open_preferences, "Ctrl+,")

        pm = mb.addMenu("Page")
        self._add_action(pm, "Go to Page…", self._focus_page_jump, "Ctrl+G")
        self._add_action(pm, "Delete Current Page", self.delete_current_page)

        vm = mb.addMenu("View")
        # Side page panel show/hide, remembered across runs.
        self._panel_action = QAction("Show Page Panel", self)
        self._panel_action.setCheckable(True)
        self._panel_action.setShortcut("Ctrl+B")
        self._panel_action.toggled.connect(self._on_panel_toggled)
        vm.addAction(self._panel_action)
        panel_visible = settings().view.page_panel_visible
        self._panel_action.setChecked(panel_visible)
        # setChecked(False) fires no toggle, so the panel is set directly too.
        self._view.set_page_panel_visible(panel_visible)
        vm.addSeparator()
        self._theme_action = QAction("Dark Mode", self)
        self._theme_action.setShortcut("Ctrl+D")
        self._theme_action.triggered.connect(self._toggle_theme)
        vm.addAction(self._theme_action)
        # Set the initial label/icon to match the current mode.
        dark = self._theme.is_dark
        self._theme_action.setText("Light Mode" if dark else "Dark Mode")
        self._theme_action.setIcon(
            themed_icon("mdi6.weather-sunny" if dark else "mdi6.weather-night",
                        self._theme.palette.text))

        hm = mb.addMenu("Help")
        # The version, sitting directly above the control that asks whether it
        # is the newest one. Disabled because it is a statement, not a command:
        # "Check for Updates" is the only thing here to press, and the number it
        # is talking about should be readable without pressing anything.
        self._version_action = QAction(f"Rapid PDF v{APP_VERSION}", self)
        self._version_action.setEnabled(False)
        hm.addAction(self._version_action)
        self._add_action(hm, "Check for Updates…", self.check_for_updates)
        hm.addSeparator()
        self._add_action(hm, "About Rapid PDF", self._about)

    # ------------------------------------------------------------------
    # Preferences (see ui/preferences_dialog.py)
    # ------------------------------------------------------------------

    def open_preferences(self):
        """Edit > Preferences / Ctrl+, . One dialog, raised if already open.

        Not modal: the point of the dialog is that its controls and the View
        menu's are the same controls, and a modal window would eat the Ctrl+B
        and Ctrl+D that demonstrate it.
        """
        if self._prefs_dialog is None:
            # Built once and kept. Deleting it on close and rebuilding it would
            # mean tearing down and re-making the connections that are the
            # whole point of it, for no gain: it is six controls.
            self._prefs_dialog = PreferencesDialog(self)
        else:
            self._prefs_dialog.reload()
        self._prefs_dialog.show()
        self._prefs_dialog.raise_()
        self._prefs_dialog.activateWindow()
        return self._prefs_dialog

    # ------------------------------------------------------------------
    # Updates (see ui/update_notice.py and core/update/)
    # ------------------------------------------------------------------

    def check_for_updates(self):
        """Help menu, and the Preferences button. Same check as startup, but it
        says so when there is nothing to report, because somebody asked."""
        self._update_notice.start_check(manual=True)

    def _about(self):
        QMessageBox.about(
            self, "About Rapid PDF",
            f"<b>Rapid PDF {APP_VERSION}</b>"
            "<p>Fast PDF annotation and page organization.</p>"
            "<p>Copyright (c) 2026 Lucas Ruiz</p>")

    def _on_update_staged(self, staged):
        """A verified update is on disk and the app has to close for the swap.

        The unsaved-changes prompt runs FIRST, and a cancelled prompt leaves
        everything as it was: the download stays staged and the strip keeps
        offering it. The helper is only started once closing is certain,
        because the moment it starts it is watching for this process to exit.
        """
        if not self._view.maybe_save_before_close():
            self._update_notice.apply_cancelled()
            return
        if not self._update_notice.launch_swap(staged):
            return
        self._view.mark_clean()      # the prompt above has already settled this
        self._force_quit = True
        self.close()

    def _toggle_theme(self):
        self._theme.toggle()

    def _on_panel_toggled(self, checked: bool):
        self._view.set_page_panel_visible(checked)
        settings().view.page_panel_visible = checked

    def _add_action(self, menu, label: str, slot, shortcut=None):
        action = QAction(label, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)

    def _setup_shortcuts(self):
        for key, tool in [("v", "select"), ("h", "pan"), ("r", "rect"),
                          ("l", "line"), ("t", "text")]:
            sc = QShortcut(key, self)
            sc.activated.connect(lambda t=tool: self._view.trigger_tool(t))

    # ------------------------------------------------------------------
    # File operations. The window owns the menu; the document owns the work,
    # so each of these is one hop into the front view.
    # ------------------------------------------------------------------

    def open_pdf(self):
        self._view.open_pdf()

    def open_paths(self, paths: list):
        """Open/append the given PDF paths (shared by the Open dialog and the
        shell/CLI launch path)."""
        self._view.open_paths(paths)

    def combine_pdfs(self):
        self._view.combine_pdfs()

    def close_pdf(self):
        """File > Close PDF (Ctrl+W)."""
        self._view.request_close()

    def save_pdf(self) -> bool:
        return self._view.save_pdf()

    def save_pdf_as(self) -> bool:
        return self._view.save_pdf_as()

    def enhance_for_search(self):
        self._view.enhance_for_search()

    def copy_selection(self):
        self._view.copy_selection()

    def paste(self):
        self._view.paste()

    def paste_image(self):
        self._view.paste_image()

    def delete_current_page(self):
        self._view.delete_current_page()

    def _open_search(self):
        self._view.open_search()

    def _delete_key(self):
        self._view.delete_key()

    def _bring_to_front(self):
        self._view.bring_to_front()

    def _send_to_back(self):
        self._view.send_to_back()

    def handle_cli_files(self, files: list, combine: bool):
        """One aggregated shell/CLI launch batch (see core.single_instance).

        Pulls the window to the front first, since the launch came from
        Explorer and not from the app, then hands the batch to the document.
        """
        # Un-minimize and raise: the user just acted in Explorer.
        self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized)
                            | Qt.WindowState.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()
        if not files:
            return
        self._view.handle_cli_files(files, combine)

    # ------------------------------------------------------------------
    # The status bar's page box
    # ------------------------------------------------------------------

    def _focus_page_jump(self):
        """Ctrl+G / Page > Go to Page. Puts the cursor in the status-bar page box."""
        self._page_jump.focus_box()

    def _on_page_jump(self, page_num: int):
        """A page number was typed into the status-bar box."""
        self._view.jump_to_page(page_num)

    # ------------------------------------------------------------------
    # What Preferences edits. Narrow on purpose: the dialog holds no copy of
    # any setting, it drives and follows the same objects the menus do.
    # ------------------------------------------------------------------

    def page_panel_action(self) -> QAction:
        """The View menu's Show Page Panel action, which owns that setting."""
        return self._panel_action

    def theme_manager(self) -> ThemeManager:
        """The ThemeManager, which owns the theme and persists it itself."""
        return self._theme

    def fit_mode_labels(self) -> dict:
        """mode -> the name it goes by, in the order the status bar shows it."""
        return {mode: tip for mode, _, tip, _ in _FIT_CONTROLS}

    def current_fit_mode(self) -> str:
        """The chosen page-fit mode.

        This is the remembered choice, not the live state of the canvas: a
        manual zoom breaks the fit that is applied without changing the mode
        the user picked, and it is the pick that both controls display.
        """
        return settings().view.default_fit_mode

    def choose_fit_mode(self, mode: str):
        """Apply a page-fit mode, show it as chosen, and remember it.

        The single entry point. The status bar's icons and the Preferences
        dropdown both call this and both listen to `fit_mode_chosen`, which is
        what keeps them from drifting into two separate values. It stayed on
        the window because the setting is one app-wide value and two of its
        three surfaces are window chrome; only the applying goes to the canvas.
        """
        if mode not in FIT_MODES:
            return
        self._view.set_fit_mode(mode)
        button = self._fit_btns.get(mode)
        if button is not None:
            button.setChecked(True)
        self._retint_fit_icons(self._theme.palette)
        settings().view.default_fit_mode = mode
        self.fit_mode_chosen.emit(mode)

    def _apply_default_fit(self):
        """A freshly opened document starts in the remembered view mode."""
        self.choose_fit_mode(settings().view.default_fit_mode)

    def _on_fit_clicked(self, mode: str):
        self.choose_fit_mode(mode)

    def _retint_fit_icons(self, palette):
        """The active mode sits on an accent fill, so its glyph has to flip to the
        on-accent color. Qt won't swap a QIcon by QSS state, so it happens here."""
        if not qtawesome_available() or not hasattr(self, "_fit_btns"):
            return
        icons = {mode: icon for mode, icon, _, _ in _FIT_CONTROLS}
        for mode, btn in self._fit_btns.items():
            if btn.icon().isNull():
                continue          # this one fell back to a text label
            color = palette.accent_text if btn.isChecked() else palette.text_dim
            btn.setIcon(themed_icon(icons[mode], color))

    def _on_fit_mode_broken(self):
        # User zoomed manually, so no mode is active any more. An exclusive
        # QButtonGroup refuses to leave every button unchecked, so exclusivity
        # comes off for the one call that clears it.
        checked = self._fit_group.checkedButton()
        if checked is None:
            return
        self._fit_group.setExclusive(False)
        checked.setChecked(False)
        self._fit_group.setExclusive(True)
        self._retint_fit_icons(self._theme.palette)

    # ------------------------------------------------------------------
    # The title bar
    # ------------------------------------------------------------------

    def _update_title(self):
        """Reflect the open file and unsaved state in the window title.

        Qt renders the '[*]' placeholder as '*' only while windowModified is True.
        The view says WHEN (title_changed); the name and the dirty flag are read
        back off it here, because the title bar is the window's.
        """
        name = self._view.document_name()
        if name is None:
            self.setWindowModified(False)
            self.setWindowTitle("Rapid PDF")
            return
        self.setWindowModified(self._view.is_dirty())
        self.setWindowTitle(f"Rapid PDF — {name}[*]")

    def _quit_app(self):
        """Quit menu / Ctrl+Q → actually quit, even with a PDF open."""
        self._force_quit = True
        self.close()

    # ------------------------------------------------------------------
    # Closing
    # ------------------------------------------------------------------

    def _connect_session_manager(self):
        """Listen for the session ending, if this platform reports one.

        Not every platform build emits `commitDataRequest`, and a headless test
        app has no session manager at all, so a missing signal is fine:
        `_session_is_ending` falls back to `isSavingSession()`.
        """
        app = QGuiApplication.instance()
        try:
            app.commitDataRequest.connect(self._on_commit_data)
        except (AttributeError, RuntimeError, TypeError):
            pass

    def _on_commit_data(self, manager=None):
        """Windows is ending the session (shutdown, restart, log off).

        Qt raises `commitDataRequest` from WM_QUERYENDSESSION. Everything that
        happens on this signal is on Windows's clock, so it does two things and
        nothing else: remember that the close about to arrive is a session end,
        and get the settings on disk while there is still a process to do it.
        """
        self._session_ending = True
        try:
            manager.setRestartHint(manager.RestartHint.RestartNever)
        except (AttributeError, TypeError):
            pass
        settings().flush()

    def _session_is_ending(self) -> bool:
        """Whether the close now in flight is Windows shutting the session down.

        Two independent reads because neither is reliable alone: the flag set by
        `commitDataRequest`, and Qt's own `isSavingSession()` for the case where
        the signal never reached us.
        """
        if self._session_ending:
            return True
        app = QGuiApplication.instance()
        try:
            return bool(app is not None and app.isSavingSession())
        except (AttributeError, RuntimeError):
            return False

    def _teardown_for_quit(self):
        """Release everything the process owns, on the way out for good."""
        # The view drops its render clones and closes its document.
        self._view.teardown()
        # A check thread still running when the window goes is a crash on
        # exit. Bounded wait, see UpdateNotice.shutdown().
        self._update_notice.shutdown()
        settings().flush()   # writes are debounced; the timer may never fire

    def closeEvent(self, event):
        """The X, Alt+F4, Ctrl+Q, and the session manager all land here.

        Written so the tab work can extend it: the only decision that changes
        with tabs is what `clear_document` has to clear and whether more than
        one document is open, and both sit behind the two branches below.
        """
        # Windows is shutting down. Anything that stalls here is what makes it
        # put up "this app is preventing shutdown", so there is no prompt and,
        # above all, no event.ignore(). An unsaved document is lost the same way
        # it would be in any app that does not implement the full session
        # protocol, which is the accepted trade for not blocking the machine.
        if self._session_is_ending():
            self._teardown_for_quit()
            super().closeEvent(event)
            event.accept()
            return

        # The X closes the window and the app. Setting close.x_closes to
        # "document" restores the older behaviour: the PDF closes and an empty
        # window stays up. The Quit menu (_force_quit) overrides either way.
        close_document_only = (
            self._view.has_document()
            and not self._force_quit
            and settings().close.x_closes == "document"
        )

        # Unsaved changes are prompted for first and the answer overrides the
        # setting: Cancel aborts the close outright, whichever branch it was
        # heading for.
        if not self._view.maybe_save_before_close():
            self._force_quit = False
            event.ignore()
            return

        if close_document_only:
            self._view.clear_document()
            event.ignore()      # the window stays, empty
            return

        self._teardown_for_quit()
        super().closeEvent(event)
