import os
import fitz
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QTabWidget,
    QFileDialog, QMessageBox, QStatusBar, QApplication,
    QToolButton, QButtonGroup,
)
from PySide6.QtCore import QTimer, Qt, QSize, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut, QIcon

# Debounce for search-as-you-type: long enough that fast typing doesn't
# re-scan the document per keystroke, short enough to feel live.
SEARCH_DEBOUNCE_MS = 220

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

from core.pdf_document import PDFDocument
from core.page_ops import is_permutation
from core.settings import dialog_start_dir, remember_dialog_dir, settings
from core.resources import app_icon_path
from core.ocr_worker import run_ocr_enhance
from core.version import APP_VERSION
from ui.update_notice import UpdateNotice
from ui.canvas import PDFCanvas, FIT_MODES
from ui.preferences_dialog import PreferencesDialog
from ui.toolbar import ToolBar
from ui.page_panel import PagePanel
from ui.page_commands import DeletePagesCommand, ReorderPagesCommand
from ui.organizer import PageOrganizer
from ui.page_jump import PageJump
from ui.search_bar import SearchBar
from ui.combine_dialog import CombineDialog
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
        self._doc = PDFDocument()
        self._current_page = 0
        self._org_render = None  # throwaway clone backing the Organizer's markup thumbnails
        self._panel_render = None  # throwaway clone backing the left page panel's thumbnails
        self._dirty = False      # unsaved changes exist (annotations, page edits, merges)
        self._force_quit = False # Quit menu wants a real app quit, not "close PDF"
        self._session_ending = False  # Windows is ending the session; never block it
        self._ocr_thread = None  # active OCR QThread, or None when idle
        self._ocr_worker = None  # keep a ref alive alongside the thread
        # Rows the page panel should end up with highlighted after the next
        # structural edit (the pages that were just moved). Consumed once.
        self._pending_page_selection = None
        # Text-search state (driven by the Ctrl+F bar)
        self._search_hits: list = []   # [(page_num, fitz.Rect), ...]
        self._search_index = -1
        self._search_term = None       # term the hits were computed for
        self._search_timer = QTimer(self)   # search-as-you-type debounce
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._run_live_search)
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

    def _apply_theme_surfaces(self, palette):
        """Re-tint the bits QSS can't reach: the canvas backdrop and its selection
        chrome, the thumbnail delegates, the toggle action's icon/label, and the
        Mica header on a switch."""
        self._canvas.apply_palette(palette)
        self._page_panel.apply_palette(palette)
        self._organizer.apply_palette(palette)
        self._toolbar.apply_palette(palette)
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

        self._tabs = QTabWidget()
        self._tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self._tabs)

        # ---- Tab 0: Editor ----
        editor_widget = QWidget()
        editor_layout = QHBoxLayout(editor_widget)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)

        self._page_panel = PagePanel()
        self._page_panel.page_selected.connect(self._on_page_selected)
        self._page_panel.pages_delete_requested.connect(self._delete_pages)
        self._page_panel.pages_reorder_requested.connect(self._reorder_pages)
        editor_layout.addWidget(self._page_panel)

        self._canvas = PDFCanvas()
        self._canvas.annotation_changed.connect(self._on_annotation_changed)
        self._canvas.page_changed.connect(self._on_canvas_page_changed)
        # Keep the title/modified indicator in sync whenever the undo stack crosses
        # the clean boundary (e.g. Ctrl+Z back to saved state clears the * marker).
        self._canvas.undo_stack.cleanChanged.connect(self._on_clean_changed)

        # Search bar sits above the canvas, hidden until Ctrl+F.
        self._search_bar = SearchBar()
        self._search_bar.search_changed.connect(self._on_search_term_changed)
        self._search_bar.next_requested.connect(lambda: self._search_step(1))
        self._search_bar.prev_requested.connect(lambda: self._search_step(-1))
        self._search_bar.closed.connect(self._on_search_closed)
        # Re-apply highlights once a (debounced) page render lands, since
        # _load_page clears any overlays that belonged to the previous page.
        self._canvas.page_loaded.connect(self._on_canvas_page_loaded)

        canvas_col = QVBoxLayout()
        canvas_col.setContentsMargins(0, 0, 0, 0)
        canvas_col.setSpacing(0)
        canvas_col.addWidget(self._search_bar)
        canvas_col.addWidget(self._canvas, stretch=1)
        editor_layout.addLayout(canvas_col, stretch=1)

        self._toolbar = ToolBar()
        self._toolbar.tool_changed.connect(self._canvas.set_tool)
        self._toolbar.line_color_changed.connect(self._canvas.set_stroke_color)
        self._toolbar.fill_color_changed.connect(self._canvas.set_fill_color)
        self._toolbar.fill_cleared.connect(lambda: self._canvas.set_fill_enabled(False))
        self._toolbar.line_width_changed.connect(self._canvas.set_line_width)
        self._toolbar.opacity_changed.connect(self._canvas.set_opacity)
        self._toolbar.font_size_changed.connect(self._canvas.set_font_size)
        self._toolbar.font_color_changed.connect(self._canvas.set_font_color)
        self._canvas.selection_changed.connect(self._toolbar.show_selection)
        editor_layout.addWidget(self._toolbar)

        self._tabs.addTab(editor_widget, "Editor")

        # ---- Tab 1: Organizer ----
        self._organizer = PageOrganizer()
        self._organizer.page_activated.connect(self._on_organizer_page_activated)
        self._organizer.pages_reordered_perm.connect(self._on_pages_reordered_perm)
        self._organizer.pages_deleted.connect(self._on_pages_deleted)
        self._organizer.pages_added.connect(self._on_pages_added)
        self._organizer.needs_rebuild.connect(self._refresh_organizer)
        self._tabs.addTab(self._organizer, "Organizer")

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Open a PDF to start  (Ctrl+O)")

        # Page box, immediately left of the fit group. Permanent widgets are laid
        # out left to right in the order they're added, so this one is added first.
        self._page_jump = PageJump()
        self._page_jump.page_requested.connect(self._on_page_jump)
        self._status.addPermanentWidget(self._page_jump)

        self._status.addPermanentWidget(self._build_fit_group())
        self._canvas.fit_mode_broken.connect(self._on_fit_mode_broken)

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
        undo_act = self._canvas.undo_stack.createUndoAction(self, "Undo")
        undo_act.setShortcut(QKeySequence.StandardKey.Undo)
        redo_act = self._canvas.undo_stack.createRedoAction(self, "Redo")
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
        self._add_action(em, "Bring to Front", self._canvas.bring_to_front, "Ctrl+]")
        self._add_action(em, "Send to Back", self._canvas.send_to_back, "Ctrl+[")
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
        self._page_panel.setVisible(panel_visible)   # setChecked(False) fires no toggle
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
        if not self._maybe_save_before_close():
            self._update_notice.apply_cancelled()
            return
        if not self._update_notice.launch_swap(staged):
            return
        self._dirty = False          # the prompt above has already settled this
        self._force_quit = True
        self.close()

    def _toggle_theme(self):
        self._theme.toggle()

    def _on_panel_toggled(self, checked: bool):
        self._page_panel.setVisible(checked)
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
            sc.activated.connect(lambda t=tool: self._toolbar.trigger_tool(t))

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def open_pdf(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open / Combine PDFs", dialog_start_dir(self._doc.path),
            "PDF Files (*.pdf)"
        )
        if not paths:
            return
        remember_dialog_dir(paths[0])
        self.open_paths(sorted(paths))  # combine in filename order

    def open_paths(self, paths: list):
        """Open/append the given PDF paths (shared by the Open dialog and the
        shell/CLI launch path)."""
        if not paths:
            return
        # A document is already open → append the chosen PDFs to the end.
        if self._doc.doc:
            added = self._append_pdfs(paths)
            self._refresh_panel_thumbnails()
            self._update_status(
                f"Appended {added} page(s) from {len(paths)} file(s)"
            )
            return

        # Multiple files with nothing open: stage the combine (Adobe-style)
        # instead of merging blindly in filename order.
        if len(paths) > 1:
            self._combine_with_dialog(paths)
            return

        # No document yet → open the first file, then append any others.
        if not self._doc.open(paths[0]):
            QMessageBox.critical(self, "Error", f"Could not open:\n{paths[0]}")
            return
        self._dirty = False  # freshly opened, in sync with disk (combine below re-dirties)
        if len(paths) > 1:
            self._append_pdfs(paths[1:])
        self._canvas.set_document(self._doc)
        self._page_panel.set_document(self._doc)
        self._current_page = 0
        # Stale search hits reference the previous document's pages.
        self._search_hits = []
        self._search_index = -1
        self._search_term = None
        if len(paths) == 1:
            # A single freshly opened file may carry an editable model to restore.
            self._load_saved_annotations()
        # Always rebuild the panel thumbnails from a markup-baked clone after open.
        # _load_saved_annotations already does this when it restores a model, but a
        # file with baked markup and no model (or none to restore) still needs the
        # panel re-rendered so it matches the page rather than the pre-strip doc.
        self._refresh_panel_thumbnails()
        self._apply_default_fit()
        self._update_status(
            f"Combined {len(paths)} files" if len(paths) > 1 else ""
        )

    def combine_pdfs(self):
        """File > Combine PDFs: pick files, stage them as movable cards, merge
        only when Combine is clicked. Any open document is closed first (with
        the usual save prompt)."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Combine PDFs", dialog_start_dir(self._doc.path),
            "PDF Files (*.pdf)"
        )
        if not paths:
            return
        remember_dialog_dir(paths[0])
        if self._doc.doc:
            self.close_pdf()          # prompts to save; may be cancelled
            if self._doc.doc:
                return                # user backed out, keep the open doc
        self._combine_with_dialog(sorted(paths))

    def handle_cli_files(self, files: list, combine: bool):
        """One aggregated shell/CLI launch batch (see core.single_instance).

        Several files together, or an explicit --combine verb, go to the
        staged Combine dialog as whole-file cards; a lone file opens (or
        appends, matching the Open menu's behavior). Also pulls the window to
        the front, since the launch came from Explorer, not from the app.
        """
        # Un-minimize and raise: the user just acted in Explorer.
        self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized)
                            | Qt.WindowState.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()
        if not files:
            return
        if combine or len(files) > 1:
            if self._doc.doc:
                self.close_pdf()      # prompts to save; may be cancelled
                if self._doc.doc:
                    return
            self._combine_with_dialog(files)
        else:
            self.open_paths(files)

    def _combine_with_dialog(self, paths: list):
        """Run the staged-combine dialog and adopt its merged output.

        The dialog holds everything in memory: cancelling (or closing it)
        leaves the app and every input file exactly as they were."""
        dlg = CombineDialog(paths, palette=self._theme.palette, parent=self)
        if dlg.exec() != CombineDialog.DialogCode.Accepted:
            return
        merged = dlg.merged_document()
        if merged is None or len(merged) == 0:
            if merged is not None:
                merged.close()
            return
        self._doc.adopt(merged)       # untitled: first save goes to Save As
        self._canvas.set_document(self._doc)
        self._page_panel.set_document(self._doc)
        self._current_page = 0
        self._search_hits = []
        self._search_index = -1
        self._search_term = None
        self._mark_dirty()
        self._refresh_panel_thumbnails()
        self._apply_default_fit()
        self._update_status(f"Combined {len(paths)} files (not saved yet)")

    def _append_pdfs(self, paths) -> int:
        """Insert each PDF's pages at the end of the current doc. Returns pages added."""
        total = 0
        errors = []
        for path in paths:
            try:
                src = fitz.open(path)
                count = len(src)
                src.close()
                self._doc.insert_pdf(path, start_at=self._doc.page_count())
                total += count
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")
        if total:
            # A merge produces a derived document → untitled + unsaved.
            self._mark_untitled()
            self._mark_dirty()
        if errors:
            QMessageBox.critical(self, "Insert Error", "\n".join(errors))
        return total

    def copy_selection(self):
        """Copy the selected annotations into the in-app clipboard."""
        if not self._doc.doc:
            return
        n = self._canvas.copy_selected()
        if n:
            self._update_status(f"Copied {n} object(s) — Ctrl+V to paste")

    def paste(self):
        """Paste in-app copied annotations if any, else fall back to a clipboard image."""
        if not self._doc.doc:
            QMessageBox.warning(self, "No PDF", "Open a PDF first.")
            return
        if self._canvas.has_clipboard_items():
            self._canvas.paste_clipboard_items()
            self._update_status("Pasted — drag to move, drag handles to resize")
        else:
            self.paste_image()

    def paste_image(self):
        """Paste a clipboard image (from Word, a screenshot, etc.) as a movable object."""
        if not self._doc.doc:
            QMessageBox.warning(self, "No PDF", "Open a PDF first.")
            return
        if QApplication.clipboard().image().isNull():
            self._update_status("Clipboard has no image to paste")
            return
        self._canvas._paste_from_clipboard()
        self._update_status("Pasted image — drag to move, drag handles to resize")

    def close_pdf(self):
        """Close the current document so the next Open starts fresh instead of appending."""
        if not self._doc.doc:
            return
        if not self._maybe_save_before_close():
            return
        self._clear_document()

    def _clear_document(self):
        """Drop the open document and return the window to its empty state.

        The prompt is the caller's job: this runs after the decision to close
        has been made, which is why both `close_pdf` and the X-closes-document
        path in `closeEvent` can share it.
        """
        self._close_org_render()
        self._close_panel_render()
        self._search_bar.hide()
        self._on_search_closed()
        self._doc.close()
        self._dirty = False
        self._canvas.set_document(self._doc)
        self._page_panel.set_document(self._doc)
        # Closing the doc must also clear the Organizer (it holds its own page
        # grid and isn't refreshed by the canvas/panel updates above). With no
        # doc open this empties the grid and disables its buttons.
        self._organizer.set_document(self._doc, None)
        self._current_page = 0
        self._update_status()

    def _after_successful_save(self, status: str):
        """Shared post-save bookkeeping for both Save and Save As.

        Both paths do the identical work once the write succeeds: clear the
        dirty/clean state, strip the baked markup back out of the live doc,
        drop baked image overlays, and re-sync the panel to the saved page.
        """
        self._dirty = False
        self._canvas.undo_stack.setClean()   # Ctrl+Z back here won't prompt to save
        self._strip_baked_annotations()
        self._canvas.drop_baked_image_items()  # avoid re-baking images on the next save
        self._refresh_panel_thumbnails()  # keep panel in sync with the saved page state
        # Rebuilding the panel resets its selection to row 0; restore the row to the
        # page actually being viewed so the thumbnail highlight stays put after save.
        self._page_panel.set_current_page(self._current_page)
        self._update_status(status)

    def save_pdf(self) -> bool:
        if not self._doc.doc:
            return False
        # A merged/untitled doc has no source file → force a destination via Save As.
        if self._doc.path is None:
            return self.save_pdf_as()
        self._flush_annotations()
        if self._doc.save():
            self._after_successful_save("Saved")
            return True
        QMessageBox.critical(self, "Save Error", "Could not save the PDF.")
        return False

    def save_pdf_as(self) -> bool:
        if not self._doc.doc:
            return False
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF As", dialog_start_dir(self._doc.path),
            "PDF Files (*.pdf)")
        if not path:
            return False
        remember_dialog_dir(path)
        self._flush_annotations()
        if self._doc.save(path):  # save() adopts `path` as the new canonical path
            self._after_successful_save(f"Saved to {path}")
            return True
        QMessageBox.critical(self, "Save Error", "Could not save the PDF.")
        return False

    def enhance_for_search(self):
        """Run OCR once, on demand, over every page that doesn't already have
        an extractable text layer — so scanned/image-only pages become
        searchable. Runs on a background thread with a progress dialog; the
        normal editing UI stays responsive and untouched while it runs.
        """
        if not self._doc.doc:
            QMessageBox.warning(self, "No PDF", "Open a PDF first.")
            return
        if self._ocr_thread is not None:
            QMessageBox.information(self, "OCR In Progress", "Already enhancing this document.")
            return

        # Bake current edits into the live doc first (same as a normal save)
        # so OCR runs against the up-to-date page content, not stale markup.
        self._flush_annotations()

        self._status.showMessage("Enhancing for search (OCR)…")
        self._ocr_thread, self._ocr_worker = run_ocr_enhance(
            self, self._doc, self._on_ocr_finished
        )

    def _on_ocr_finished(self, ocred_count: int, cancelled: bool, errors: list):
        self._ocr_thread = None
        self._ocr_worker = None
        # OCR rewrote page content streams directly on the live document, so
        # every cached render is stale and the panel/current page must be
        # redrawn from the new (now-searchable) page content.
        self._doc.invalidate_render_cache()
        self._canvas.reload_current_page()
        self._refresh_panel_thumbnails()
        self._page_panel.set_current_page(self._current_page)

        if cancelled:
            self._update_status(f"OCR cancelled: {ocred_count} page(s) enhanced before stopping")
        elif errors and ocred_count == 0:
            self._update_status("OCR failed (no pages enhanced)")
        elif ocred_count == 0:
            self._update_status("OCR: no pages needed enhancing (already searchable)")
        else:
            self._update_status(f"OCR: enhanced {ocred_count} page(s) for search")

        # Surface real failures instead of burying them in the console. The
        # big one in the field: no Tesseract language data on the machine.
        if errors and not cancelled:
            first = errors[0][1]
            if "tesseract" in first.lower() or "tessdata" in first.lower():
                detail = ("OCR needs the Tesseract language data, which was not "
                          "found on this computer.\n\nInstall Tesseract-OCR (UB "
                          "Mannheim build) or set the TESSDATA_PREFIX environment "
                          "variable, then try again.")
            else:
                detail = f"First error: {first}"
            QMessageBox.warning(
                self, "OCR Problem",
                f"OCR failed on {len(errors)} page(s). {ocred_count} page(s) "
                f"were enhanced.\n\n{detail}",
            )

        if ocred_count:
            self._mark_dirty()
            # In-app verification: per-page text layer check, so the user gets
            # proof the document is now searchable (plus Ctrl+F to try it).
            counts = self._doc.text_layer_report()
            no_text = [i + 1 for i, c in enumerate(counts) if c == 0]
            if no_text:
                shown = ", ".join(str(p) for p in no_text[:15])
                more = "…" if len(no_text) > 15 else ""
                verify = (f"Text layer check: {len(counts) - len(no_text)} of "
                          f"{len(counts)} pages searchable. Still no text on "
                          f"page(s) {shown}{more}.")
            else:
                verify = (f"Text layer check: all {len(counts)} pages now carry "
                          f"searchable text ({sum(counts)} characters total). "
                          f"Try it with Ctrl+F.")
            reply = QMessageBox.question(
                self, "Save Now?",
                f"Enhanced {ocred_count} page(s) for search.\n\n{verify}\n\nSave now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.save_pdf()

    # ------------------------------------------------------------------
    # Text search (Ctrl+F)
    # ------------------------------------------------------------------

    def _open_search(self):
        if not self._doc.doc:
            QMessageBox.warning(self, "No PDF", "Open a PDF first.")
            return
        self._tabs.setCurrentIndex(0)   # search lives in the Editor view
        self._search_bar.open_and_focus()

    def _on_search_term_changed(self, term: str):
        # Invalidate cached hits on every edit. Search-as-you-type kicks in
        # from the SECOND character (debounced); a single character would
        # light up half the document, so it only searches on Enter.
        self._search_term = None
        term = term.strip()
        if len(term) >= 2:
            self._search_timer.start()   # restart: debounce while typing
            return
        self._search_timer.stop()
        self._search_hits = []
        self._search_index = -1
        self._search_bar.set_count_text("")
        self._canvas.clear_search_hits()

    def _run_live_search(self):
        """Debounce landing: search the typed term and jump to the first hit
        at or after the current page (same landing rule as Enter)."""
        if not self._doc.doc or not self._search_bar.isVisible():
            return
        term = self._search_bar.term().strip()
        if len(term) < 2 or term == self._search_term:
            return
        self._compute_hits(term)
        if not self._search_hits:
            return
        start = next((i for i, (pn, _) in enumerate(self._search_hits)
                      if pn >= self._current_page), 0)
        self._search_index = start
        self._goto_current_hit()

    def _compute_hits(self, term: str):
        self._search_hits = self._doc.search_text(term)
        self._search_term = term
        self._search_index = -1
        if not self._search_hits:
            self._search_bar.set_count_text("No matches")
            self._canvas.clear_search_hits()

    def _search_step(self, delta: int):
        if not self._doc.doc:
            return
        term = self._search_bar.term().strip()
        if not term:
            return
        self._search_timer.stop()   # Enter beats a pending live search
        if term != self._search_term:
            self._compute_hits(term)
        if not self._search_hits:
            return
        n = len(self._search_hits)
        if self._search_index < 0:
            # First jump: land on the first hit at or after the current page.
            start = next((i for i, (pn, _) in enumerate(self._search_hits)
                          if pn >= self._current_page), 0)
            self._search_index = start if delta >= 0 else (start - 1) % n
        else:
            self._search_index = (self._search_index + delta) % n
        self._goto_current_hit()

    def _goto_current_hit(self):
        """Update the counter and bring the active hit into view."""
        n = len(self._search_hits)
        self._search_bar.set_count_text(f"{self._search_index + 1} of {n} matches")
        page, _ = self._search_hits[self._search_index]
        if page != self._current_page:
            self._current_page = page
            self._canvas.set_page(page, immediate=True)   # loads + emits page_loaded
            self._page_panel.set_current_page(page)
            self._update_status()
        else:
            self._apply_search_highlights()

    def _apply_search_highlights(self):
        """Paint every hit on the current page; emphasise the active one."""
        if not self._search_bar.isVisible() or not self._search_hits:
            return
        page_rects = [(i, r) for i, (pn, r) in enumerate(self._search_hits)
                      if pn == self._current_page]
        rects = [r for _, r in page_rects]
        current = next((k for k, (i, _) in enumerate(page_rects)
                        if i == self._search_index), -1)
        self._canvas.set_search_hits(rects, current)

    def _on_canvas_page_loaded(self, page_num: int):
        # _load_page just cleared any overlays; put back the ones that belong
        # to this page (no-op when the search bar is closed).
        self._apply_search_highlights()

    def _on_search_closed(self):
        self._search_timer.stop()
        self._search_hits = []
        self._search_index = -1
        self._search_term = None
        self._canvas.clear_search_hits()
        self._canvas.setFocus()

    def _delete_key(self):
        """Delete routes by what is in front and what has the keyboard: pages in
        the Organizer, pages in the Editor's thumbnail strip when the strip has
        focus, else the selected canvas objects.

        The menu action owns the Delete shortcut at window level, so it fires
        even while the strip has focus. That is why the routing lives here
        rather than only in the strip's own key handler (which still covers
        Backspace, which the menu doesn't claim).
        """
        if self._tabs.currentIndex() == 1:   # Organizer tab
            self._organizer.delete_selected()
        elif self._page_panel.isVisible() and self._page_panel.has_focus():
            self._delete_pages(self._page_panel.selected_rows())
        else:
            self._canvas.delete_selected()

    # ------------------------------------------------------------------
    # Page structure edits from the left panel (undoable)
    # ------------------------------------------------------------------

    def _delete_pages(self, rows: list):
        """Delete the panel's selected pages as one undoable step."""
        if not self._doc.doc or not rows:
            return
        if self._doc.page_count() - len(rows) < 1:
            QMessageBox.warning(self, "Cannot Delete",
                                "A document has to keep at least one page.")
            return
        self._pending_page_selection = None
        self._canvas.undo_stack.push(DeletePagesCommand(self, rows))
        count = len(rows)
        self._update_status(
            f"Deleted {count} page{'s' if count > 1 else ''}  (Ctrl+Z to undo)")

    def _reorder_pages(self, order: list, moved_rows: list):
        """Apply a drag from the panel as one undoable step.

        A drop that doesn't describe a clean permutation is dropped on the floor
        and the strip is rebuilt from the document, so a confused drag can never
        leave the two disagreeing.
        """
        if not self._doc.doc:
            return
        if not is_permutation(order, self._doc.page_count()):
            self._refresh_panel_thumbnails(current_page=self._current_page)
            return
        # Where the moved pages end up, so they stay selected after the rebuild.
        self._pending_page_selection = sorted(order.index(r) for r in moved_rows
                                              if r in order)
        self._canvas.undo_stack.push(
            ReorderPagesCommand(self, order, len(moved_rows)))
        count = len(moved_rows)
        self._update_status(
            f"Moved {count} page{'s' if count > 1 else ''}  (Ctrl+Z to undo)")

    def after_page_structure_change(self):
        """Re-sync the shell around a page delete or reorder, in either direction.

        The command has already changed the document and re-based the canvas's
        markup; this is everything that hangs off that. Called by both redo() and
        undo(), so an undo lands the app in exactly the state a fresh edit would.
        """
        self._mark_dirty()   # cleanChanged corrects this if we land back on saved
        self._current_page = self._canvas.current_page()
        select = self._pending_page_selection
        self._pending_page_selection = None
        self._refresh_panel_thumbnails(current_page=self._current_page, select=select)
        self._refresh_current_thumb()
        self._update_status()

    def delete_current_page(self):
        if not self._doc.doc or self._doc.page_count() <= 1:
            QMessageBox.warning(self, "Cannot Delete", "Cannot delete the only page.")
            return
        reply = QMessageBox.question(
            self, "Delete Page",
            f"Delete page {self._current_page + 1}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._doc.delete_page(self._current_page)
        self._mark_dirty()
        self._canvas.remove_page_annotations(self._current_page)
        # Page deletion renumbers/removes items the undo stack still references;
        # clear it so a later undo can't replay against stale page indices.
        # (Mirrors _on_pages_deleted — the Organizer delete path.)
        self._canvas.undo_stack.clear()
        self._refresh_panel_thumbnails()
        new_page = min(self._current_page, self._doc.page_count() - 1)
        self._current_page = new_page
        self._canvas.set_page(new_page, immediate=True)
        self._page_panel.set_current_page(new_page)
        self._update_status()

    # ------------------------------------------------------------------
    # Tab events
    # ------------------------------------------------------------------

    def _on_tab_changed(self, index: int):
        if index == 1:  # Organizer tab — (re)load a fresh, current snapshot of the pages
            self._refresh_organizer()

    def _make_markup_baked_render(self) -> PDFDocument:
        """A throwaway PDFDocument whose pages carry the current unsaved overlays
        baked in, for rendering thumbnails without mutating the live document.

        Shared by the Organizer and the left page panel — both need a clone with
        the same per-page markup baked in; only what they do with it differs.
        Caller owns the returned render and must close it (see _close_* helpers).
        """
        dicts_by_page = {
            pn: self._canvas.get_all_annotation_dicts(pn)
            for pn in range(self._doc.page_count())
        }
        render = PDFDocument()
        render.doc = self._doc.clone_with_annotations(dicts_by_page)
        return render

    def _refresh_organizer(self):
        """Load the Organizer with current pages, baking unsaved markup into the
        thumbnails via a throwaway clone (so the live document isn't mutated)."""
        self._close_org_render()
        if not self._doc.doc:
            self._organizer.set_document(self._doc, None)
            return
        self._status.showMessage("Loading organizer…")
        QApplication.processEvents()
        self._org_render = self._make_markup_baked_render()
        self._organizer.set_document(self._doc, self._org_render)
        self._update_status()

    def _close_org_render(self):
        if self._org_render is not None and self._org_render.doc is not None:
            try:
                self._org_render.doc.close()
            except Exception:
                pass
        self._org_render = None

    def _refresh_panel_thumbnails(self, current_page: int | None = None,
                                  select: list | None = None):
        """Rebuild the left page panel's thumbnails from a markup-baked clone so
        they match the page + live overlays exactly.

        The live document on its own can't be the panel's render source: drawn
        markup lives as Qt overlay items (not in the doc until save), and on open
        the doc still carries the previous save's BAKED markup right up until the
        strip step — so a panel rendered straight from _doc shows squares the page
        no longer has (and misses squares the page now shows). Baking the current
        overlays into a throwaway clone keeps every thumbnail in sync."""
        self._close_panel_render()
        if not self._doc.doc:
            self._page_panel.set_render_source(None)
            return
        self._panel_render = self._make_markup_baked_render()
        self._page_panel.set_render_source(self._panel_render,
                                           current_page=current_page,
                                           select=select)

    def _close_panel_render(self):
        if self._panel_render is not None and self._panel_render.doc is not None:
            try:
                self._panel_render.doc.close()
            except Exception:
                pass
        self._panel_render = None

    def _on_organizer_page_activated(self, page_num: int):
        self._tabs.setCurrentIndex(0)
        self._on_page_selected(page_num)

    def _on_pages_reordered_perm(self, new_order: list):
        # Organizer already reordered the live document; mirror it everywhere else.
        self._mark_dirty()
        self._canvas.reorder_pages(new_order)
        # Reorder re-bases every item's page_num; the undo stack's commands still
        # reference the old numbering, so undo would land items on the wrong page.
        # Structural page ops are incompatible with the item-level undo stack — clear it.
        self._canvas.undo_stack.clear()
        self._refresh_panel_thumbnails()
        self._current_page = self._canvas._current_page
        self._page_panel.set_current_page(self._current_page)
        self._refresh_current_thumb()
        self._update_status()

    def _on_pages_deleted(self, rows: list):
        if rows:
            self._mark_dirty()
        for row in rows:  # already in descending order from organizer
            self._canvas.remove_page_annotations(row)
        # Page deletion is structurally irreversible — the undo stack holds references
        # to items on pages that no longer exist. Clear it to prevent corrupted undos.
        self._canvas.undo_stack.clear()
        self._refresh_panel_thumbnails()
        if self._doc.doc:
            new_page = min(self._current_page, self._doc.page_count() - 1)
            self._current_page = new_page
            self._canvas.set_page(new_page, immediate=True)
            self._page_panel.set_current_page(new_page)
            self._refresh_current_thumb()
        self._update_status()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _flush_annotations(self):
        if not self._doc.doc:
            return
        for page_num in range(self._doc.page_count()):
            dicts = self._canvas.get_all_annotation_dicts(page_num)
            self._doc.write_annotations(page_num, dicts)
        # Embed the editable model so the document reopens with its objects editable.
        self._doc.write_annotation_model(self._canvas.export_annotation_model())

    def _strip_baked_annotations(self):
        """After saving, remove baked markup from the live fitz document.

        _flush_annotations() writes canvas items as PDF annotation objects so they
        survive a save. The canvas also renders them as Qt items; if the baked
        objects remain in the live doc, the next _load_page() produces a background
        pixmap that already includes them — every annotation then appears twice, the
        second copy as an unselectable ghost at a rotated position for rotated pages.
        """
        if not self._doc.doc:
            return
        for pn in range(self._doc.page_count()):
            self._doc.delete_tagged_annotations(pn)

    def _load_saved_annotations(self):
        """If the opened PDF carries our embedded model, rebuild editable objects.

        The baked markup is stripped first so reconstructed items don't render on
        top of it; it is re-baked on the next save.
        """
        model = self._doc.read_annotation_model()
        if not model:
            return
        for pn in range(self._doc.page_count()):
            self._doc.delete_tagged_annotations(pn)
        self._canvas.load_annotation_model(model)
        self._canvas.reload_current_page()
        # (The caller, open_pdf, rebuilds the left-panel thumbnails from a clone
        # with these restored overlays baked in — so pages that aren't the current
        # one don't keep showing now-stripped squares, or miss restored ones.)

    def _on_page_selected(self, page_num: int):
        if page_num == self._current_page and self._doc.doc:
            return
        self._current_page = page_num
        self._canvas.set_page(page_num)
        self._page_panel.set_current_page(page_num)
        self._update_status()

    def _on_canvas_page_changed(self, page_num: int):
        """The canvas turned the page itself (continuous scroll past an edge)."""
        if not self._doc.doc:
            return
        self._current_page = page_num
        self._canvas.set_page(page_num)
        self._page_panel.set_current_page(page_num)
        self._update_status()

    def _on_annotation_changed(self):
        # Keep the left page panel's thumbnail of the current page in sync with edits.
        # Derive dirty from the undo stack so that undoing back to the saved state clears
        # the modified flag automatically (via the cleanChanged signal connection).
        self._dirty = not self._canvas.undo_stack.isClean()
        self._update_title()
        self._refresh_current_thumb()

    def _refresh_current_thumb(self):
        if not self._doc.doc:
            return
        thumb = self._canvas.grab_current_thumbnail(self._page_panel.thumb_width())
        self._page_panel.update_page_thumbnail(self._current_page, thumb)
        # Patch the Organizer's thumbnail too, the same cheap way, so it doesn't
        # lag behind the Editor tab until the next full tab-change rebuild (which
        # re-clones the whole document via _refresh_organizer — much heavier).
        # Grabbed at the organizer's own (larger) thumb width rather than reusing
        # the panel's pixmap, so it isn't an upscaled/blurry copy.
        org_thumb = self._canvas.grab_current_thumbnail(self._organizer.thumb_width())
        self._organizer.update_page_thumbnail(self._current_page, org_thumb)

    def _focus_page_jump(self):
        """Ctrl+G / Page > Go to Page. Puts the cursor in the status-bar page box."""
        self._page_jump.focus_box()

    def _on_page_jump(self, page_num: int):
        """A page number was typed into the status-bar box.

        Moves the editor and, when it holds pages, the Organizer grid too, so
        whichever tab is in front lands on the page that was asked for.
        """
        self._on_page_selected(page_num)
        self._organizer.reveal_page(page_num)

    def _update_status(self, extra: str = ""):
        self._update_title()
        if self._doc.doc:
            name = self._doc.path or "Untitled"
            base = f"{name}  —  page {self._current_page + 1} of {self._doc.page_count()}"
            self._status.showMessage(f"{base}  {extra}".strip())
            self._page_jump.set_total(self._doc.page_count())
            self._page_jump.set_current_page(self._current_page)
        else:
            self._status.showMessage(extra or "Open a PDF to start  (Ctrl+O)")
            self._page_jump.set_total(0)

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
        what keeps them from drifting into two separate values.
        """
        if mode not in FIT_MODES:
            return
        self._canvas.set_fit_mode(mode)
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
    # Unsaved-changes (dirty) + untitled (merged) state
    # ------------------------------------------------------------------

    def _update_title(self):
        """Reflect the open file and unsaved state in the window title.

        Qt renders the '[*]' placeholder as '*' only while windowModified is True.
        """
        if not self._doc.doc:
            self.setWindowModified(False)
            self.setWindowTitle("Rapid PDF")
            return
        name = os.path.basename(self._doc.path) if self._doc.path else "Untitled"
        self.setWindowModified(self._dirty)
        self.setWindowTitle(f"Rapid PDF — {name}[*]")

    def _on_clean_changed(self, clean: bool):
        """Fired by QUndoStack when the stack crosses the clean boundary.

        `clean=True` means we've undone back to the last-saved state;
        `clean=False` means we've moved away from it.  Sync _dirty and the title.
        """
        self._dirty = not clean
        self._update_title()

    def _mark_dirty(self):
        self._dirty = True
        self._update_title()

    def _mark_untitled(self):
        """A merge produced a derived document with no source file → force Save As."""
        self._doc.path = None

    def _on_pages_added(self, count: int):
        # Organizer "+ Add Pages" merged another PDF in → derived, unsaved document.
        self._mark_untitled()
        self._mark_dirty()

    def _maybe_save_before_close(self) -> bool:
        """Prompt to save unsaved changes. Returns True if it's safe to proceed."""
        if not self._doc.doc or not self._dirty:
            return True
        reply = QMessageBox.question(
            self, "Unsaved Changes",
            "This document has unsaved changes. Save before closing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if reply == QMessageBox.StandardButton.Save:
            return self.save_pdf()      # may open Save As; a cancelled save aborts the close
        if reply == QMessageBox.StandardButton.Discard:
            return True
        return False                     # Cancel (or dialog dismissed)

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
        self._close_org_render()
        self._close_panel_render()
        # A check thread still running when the window goes is a crash on
        # exit. Bounded wait, see UpdateNotice.shutdown().
        self._update_notice.shutdown()
        self._doc.close()
        settings().flush()   # writes are debounced; the timer may never fire

    def closeEvent(self, event):
        """The X, Alt+F4, Ctrl+Q, and the session manager all land here.

        Written so the tab work can extend it: the only decision that changes
        with tabs is what `_clear_document` has to clear and whether more than
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
            bool(self._doc.doc)
            and not self._force_quit
            and settings().close.x_closes == "document"
        )

        # Unsaved changes are prompted for first and the answer overrides the
        # setting: Cancel aborts the close outright, whichever branch it was
        # heading for.
        if not self._maybe_save_before_close():
            self._force_quit = False
            event.ignore()
            return

        if close_document_only:
            self._clear_document()
            event.ignore()      # the window stays, empty
            return

        self._teardown_for_quit()
        super().closeEvent(event)
