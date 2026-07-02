import os
import fitz
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QTabWidget,
    QFileDialog, QMessageBox, QStatusBar, QApplication, QPushButton,
)
from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QIcon

# Debounce for search-as-you-type: long enough that fast typing doesn't
# re-scan the document per keystroke, short enough to feel live.
SEARCH_DEBOUNCE_MS = 220

from core.pdf_document import PDFDocument
from core.resources import app_icon_path
from core.ocr_worker import run_ocr_enhance
from ui.canvas import PDFCanvas
from ui.toolbar import ToolBar
from ui.page_panel import PagePanel
from ui.organizer import PageOrganizer
from ui.search_bar import SearchBar
from ui.combine_dialog import CombineDialog
from ui.theme import ThemeManager, apply_mica, themed_icon


class MainWindow(QMainWindow):
    def __init__(self, theme: ThemeManager | None = None):
        super().__init__()
        # Theme: use the passed-in manager, or stand one up (e.g. tests/smoke).
        self._theme = theme or ThemeManager(QApplication.instance())
        self._doc = PDFDocument()
        self._current_page = 0
        self._org_render = None  # throwaway clone backing the Organizer's markup thumbnails
        self._panel_render = None  # throwaway clone backing the left page panel's thumbnails
        self._dirty = False      # unsaved changes exist (annotations, page edits, merges)
        self._force_quit = False # Quit menu wants a real app quit, not "close PDF"
        self._ocr_thread = None  # active OCR QThread, or None when idle
        self._ocr_worker = None  # keep a ref alive alongside the thread
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

    def _apply_theme_surfaces(self, palette):
        """Re-tint the bits QSS can't reach: the canvas page backdrop, the thumbnail
        delegates, the toggle action's icon/label, and the Mica header on a switch."""
        self._canvas.set_backdrop_color(palette.canvas)
        self._page_panel.apply_palette(palette)
        self._organizer.apply_palette(palette)
        self._toolbar.apply_palette(palette)
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

        self._fit_btn = QPushButton("Fit")
        self._fit_btn.setCheckable(True)
        self._fit_btn.setFixedWidth(40)
        self._fit_btn.setFlat(True)
        self._fit_btn.setToolTip("Fit page to view (toggle)")
        self._fit_btn.toggled.connect(self._on_fit_toggled)
        self._canvas.fit_mode_broken.connect(self._on_fit_mode_broken)
        self._status.addPermanentWidget(self._fit_btn)

    def _setup_menu(self):
        mb = self.menuBar()

        fm = mb.addMenu("File")
        self._add_action(fm, "Open / Combine PDFs…", self.open_pdf, QKeySequence.StandardKey.Open)
        self._add_action(fm, "Combine PDFs…", self.combine_pdfs)
        self._add_action(fm, "Close PDF", self.close_pdf)
        fm.addSeparator()
        self._add_action(fm, "Save", self.save_pdf, QKeySequence.StandardKey.Save)
        self._add_action(fm, "Save As…", self.save_pdf_as, "Ctrl+Shift+S")
        fm.addSeparator()
        self._add_action(fm, "Enhance for Search (OCR)…", self.enhance_for_search)
        fm.addSeparator()
        self._add_action(fm, "Quit", self._quit_app, QKeySequence.StandardKey.Quit)

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

        pm = mb.addMenu("Page")
        self._add_action(pm, "Delete Current Page", self.delete_current_page)

        vm = mb.addMenu("View")
        # Side page panel show/hide, remembered across runs.
        self._panel_action = QAction("Show Page Panel", self)
        self._panel_action.setCheckable(True)
        self._panel_action.setShortcut("Ctrl+B")
        self._panel_action.toggled.connect(self._on_panel_toggled)
        vm.addAction(self._panel_action)
        panel_visible = QSettings("Lucas", "Rapid PDF").value(
            "ui/page_panel_visible", True, type=bool)
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

    def _toggle_theme(self):
        self._theme.toggle()

    def _on_panel_toggled(self, checked: bool):
        self._page_panel.setVisible(checked)
        QSettings("Lucas", "Rapid PDF").setValue("ui/page_panel_visible", checked)

    def _add_action(self, menu, label: str, slot, shortcut=None):
        action = QAction(label, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)

    def _setup_shortcuts(self):
        for key, tool in [("v", "select"), ("r", "rect"), ("l", "line"), ("t", "text")]:
            sc = QShortcut(key, self)
            sc.activated.connect(lambda t=tool: self._toolbar.trigger_tool(t))

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def open_pdf(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open / Combine PDFs", "", "PDF Files (*.pdf)"
        )
        if not paths:
            return
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
        self._update_status(
            f"Combined {len(paths)} files" if len(paths) > 1 else ""
        )

    def combine_pdfs(self):
        """File > Combine PDFs: pick files, stage them as movable cards, merge
        only when Combine is clicked. Any open document is closed first (with
        the usual save prompt)."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Combine PDFs", "", "PDF Files (*.pdf)"
        )
        if not paths:
            return
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
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF As", "", "PDF Files (*.pdf)")
        if not path:
            return False
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
        """Delete (keypad) routes by active tab: pages in the Organizer, else
        selected canvas objects in the Editor."""
        if self._tabs.currentIndex() == 1:   # Organizer tab
            self._organizer.delete_selected()
        else:
            self._canvas.delete_selected()

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

    def _refresh_panel_thumbnails(self):
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
        self._page_panel.set_render_source(self._panel_render)

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

    def _update_status(self, extra: str = ""):
        self._update_title()
        if self._doc.doc:
            name = self._doc.path or "Untitled"
            base = f"{name}  —  page {self._current_page + 1} of {self._doc.page_count()}"
            self._status.showMessage(f"{base}  {extra}".strip())
        else:
            self._status.showMessage(extra or "Open a PDF to start  (Ctrl+O)")

    def _on_fit_toggled(self, checked: bool):
        self._canvas.set_fit_mode(checked)

    def _on_fit_mode_broken(self):
        # User zoomed manually — turn the button off without re-triggering the signal.
        self._fit_btn.blockSignals(True)
        self._fit_btn.setChecked(False)
        self._fit_btn.blockSignals(False)

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

    def closeEvent(self, event):
        # The close button (X) closes an open PDF, not the app — only quitting
        # when nothing is open. The Quit menu sets _force_quit to override that.
        if self._doc.doc and not self._force_quit:
            if self._maybe_save_before_close():
                self._close_org_render()
                self._close_panel_render()
                self._search_bar.hide()
                self._on_search_closed()
                self._doc.close()
                self._dirty = False
                self._canvas.set_document(self._doc)
                self._page_panel.set_document(self._doc)
                # Also clear the Organizer grid (see close_pdf) so it doesn't
                # keep showing the closed document's pages.
                self._organizer.set_document(self._doc, None)
                self._current_page = 0
                self._update_status()
            event.ignore()   # keep the app running
            return
        # Quit the app (Quit menu, or X with nothing open) — prompt to save first.
        if not self._maybe_save_before_close():
            self._force_quit = False
            event.ignore()
            return
        self._close_org_render()
        self._close_panel_render()
        self._doc.close()
        super().closeEvent(event)
