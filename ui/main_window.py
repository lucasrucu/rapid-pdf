import os
import fitz
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QTabWidget,
    QFileDialog, QMessageBox, QStatusBar, QApplication,
)
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtCore import Qt

from core.pdf_document import PDFDocument
from ui.canvas import PDFCanvas
from ui.toolbar import ToolBar
from ui.page_panel import PagePanel
from ui.organizer import PageOrganizer

_APP_STYLE = """
QMainWindow, QWidget { background-color: #1a1a1a; color: #d4d4d4; }
QMenuBar { background-color: #252525; color: #d4d4d4; border-bottom: 1px solid #333; }
QMenuBar::item:selected { background-color: #0078d4; color: #fff; }
QMenu { background-color: #252525; color: #d4d4d4; border: 1px solid #444; }
QMenu::item:selected { background-color: #0078d4; color: #fff; }
QStatusBar { background-color: #1a1a1a; color: #666; border-top: 1px solid #2a2a2a; }
QTabWidget::pane { border: none; background-color: #1a1a1a; }
QTabBar::tab {
    background-color: #252525;
    color: #888;
    border: 1px solid #333;
    border-bottom: none;
    padding: 5px 18px;
    min-width: 80px;
}
QTabBar::tab:selected { background-color: #1a1a1a; color: #e0e0e0; border-top: 2px solid #0078d4; }
QTabBar::tab:hover:!selected { background-color: #2e2e2e; color: #bbb; }
QScrollBar:vertical {
    background: #1e1e1e; width: 10px; margin: 0;
}
QScrollBar::handle:vertical {
    background: #3a3a3a; border-radius: 5px; min-height: 20px;
}
QScrollBar::handle:vertical:hover { background: #555; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: #1e1e1e; height: 10px; margin: 0;
}
QScrollBar::handle:horizontal {
    background: #3a3a3a; border-radius: 5px; min-width: 20px;
}
QScrollBar::handle:horizontal:hover { background: #555; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._doc = PDFDocument()
        self._current_page = 0
        self._org_render = None  # throwaway clone backing the Organizer's markup thumbnails
        self._dirty = False      # unsaved changes exist (annotations, page edits, merges)
        self._update_title()
        self.setMinimumSize(1100, 720)
        self.setStyleSheet(_APP_STYLE)
        self._setup_ui()
        self._setup_menu()
        self._setup_shortcuts()

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
        editor_layout.addWidget(self._canvas, stretch=1)

        self._toolbar = ToolBar()
        self._toolbar.tool_changed.connect(self._canvas.set_tool)
        self._toolbar.color_changed.connect(self._canvas.set_color)
        self._toolbar.opacity_changed.connect(self._canvas.set_opacity)
        self._toolbar.fill_toggled.connect(self._canvas.set_fill_enabled)
        self._toolbar.line_width_changed.connect(self._canvas.set_line_width)
        self._toolbar.font_size_changed.connect(self._canvas.set_font_size)
        self._toolbar.font_color_changed.connect(self._canvas.set_font_color)
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

    def _setup_menu(self):
        mb = self.menuBar()

        fm = mb.addMenu("File")
        self._add_action(fm, "Open / Combine PDFs…", self.open_pdf, QKeySequence.StandardKey.Open)
        self._add_action(fm, "Insert Pages from PDF…", self.insert_pages)
        self._add_action(fm, "Close PDF", self.close_pdf)
        fm.addSeparator()
        self._add_action(fm, "Save", self.save_pdf, QKeySequence.StandardKey.Save)
        self._add_action(fm, "Save As…", self.save_pdf_as, "Ctrl+Shift+S")
        fm.addSeparator()
        self._add_action(fm, "Quit", self.close, QKeySequence.StandardKey.Quit)

        em = mb.addMenu("Edit")
        undo_act = self._canvas.undo_stack.createUndoAction(self, "Undo")
        undo_act.setShortcut(QKeySequence.StandardKey.Undo)
        redo_act = self._canvas.undo_stack.createRedoAction(self, "Redo")
        redo_act.setShortcut(QKeySequence.StandardKey.Redo)
        em.addAction(undo_act)
        em.addAction(redo_act)
        em.addSeparator()
        self._add_action(em, "Copy", self.copy_selection, QKeySequence.StandardKey.Copy)
        self._add_action(em, "Paste", self.paste, QKeySequence.StandardKey.Paste)
        self._add_action(em, "Delete Selected", self._canvas.delete_selected,
                         QKeySequence.StandardKey.Delete)
        em.addSeparator()
        self._add_action(em, "Bring to Front", self._canvas.bring_to_front, "Ctrl+]")
        self._add_action(em, "Send to Back", self._canvas.send_to_back, "Ctrl+[")

        pm = mb.addMenu("Page")
        self._add_action(pm, "Delete Current Page", self.delete_current_page)

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
        paths = sorted(paths)  # combine in filename order

        # A document is already open → append the chosen PDFs to the end.
        if self._doc.doc:
            added = self._append_pdfs(paths)
            self._page_panel.refresh()
            self._update_status(
                f"Appended {added} page(s) from {len(paths)} file(s)"
            )
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
        self._update_status(
            f"Combined {len(paths)} files" if len(paths) > 1 else ""
        )

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
        self._doc.close()
        self._dirty = False
        self._canvas.set_document(self._doc)
        self._page_panel.set_document(self._doc)
        self._current_page = 0
        self._update_status()

    def insert_pages(self):
        if not self._doc.doc:
            QMessageBox.warning(self, "No PDF", "Open a PDF first.")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Select PDFs to Insert", "", "PDF Files (*.pdf)")
        if not paths:
            return
        paths = sorted(paths)
        insert_at = self._current_page + 1
        total = 0
        errors = []
        for path in paths:
            try:
                src = fitz.open(path)
                count = len(src)
                src.close()
                self._doc.insert_pdf(path, start_at=insert_at)
                insert_at += count
                total += count
            except Exception as e:
                errors.append(f"{path}: {e}")
        if total:
            # A merge produces a derived document → untitled + unsaved.
            self._mark_untitled()
            self._mark_dirty()
        self._page_panel.refresh()
        if total:
            self._update_status(f"Inserted {total} pages after page {self._current_page + 1}")
        if errors:
            QMessageBox.critical(self, "Insert Error", "\n".join(errors))

    def save_pdf(self) -> bool:
        if not self._doc.doc:
            return False
        # A merged/untitled doc has no source file → force a destination via Save As.
        if self._doc.path is None:
            return self.save_pdf_as()
        self._flush_annotations()
        if self._doc.save():
            self._dirty = False
            self._update_status("Saved")
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
            self._dirty = False
            self._update_status(f"Saved to {path}")
            return True
        QMessageBox.critical(self, "Save Error", "Could not save the PDF.")
        return False

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
        self._page_panel.refresh()
        new_page = min(self._current_page, self._doc.page_count() - 1)
        self._current_page = new_page
        self._canvas.set_page(new_page)
        self._page_panel.set_current_page(new_page)
        self._update_status()

    # ------------------------------------------------------------------
    # Tab events
    # ------------------------------------------------------------------

    def _on_tab_changed(self, index: int):
        if index == 1:  # Organizer tab — (re)load a fresh, current snapshot of the pages
            self._refresh_organizer()

    def _refresh_organizer(self):
        """Load the Organizer with current pages, baking unsaved markup into the
        thumbnails via a throwaway clone (so the live document isn't mutated)."""
        self._close_org_render()
        if not self._doc.doc:
            self._organizer.set_document(self._doc, None)
            return
        self._status.showMessage("Loading organizer…")
        QApplication.processEvents()
        dicts_by_page = {
            pn: self._canvas.get_all_annotation_dicts(pn)
            for pn in range(self._doc.page_count())
        }
        clone = self._doc.clone_with_annotations(dicts_by_page)
        self._org_render = PDFDocument()
        self._org_render.doc = clone
        self._organizer.set_document(self._doc, self._org_render)
        self._update_status()

    def _close_org_render(self):
        if self._org_render is not None and self._org_render.doc is not None:
            try:
                self._org_render.doc.close()
            except Exception:
                pass
        self._org_render = None

    def _on_organizer_page_activated(self, page_num: int):
        self._tabs.setCurrentIndex(0)
        self._on_page_selected(page_num)

    def _on_pages_reordered_perm(self, new_order: list):
        # Organizer already reordered the live document; mirror it everywhere else.
        self._mark_dirty()
        self._canvas.reorder_pages(new_order)
        self._page_panel.refresh()
        self._current_page = self._canvas._current_page
        self._page_panel.set_current_page(self._current_page)
        self._refresh_current_thumb()
        self._update_status()

    def _on_pages_deleted(self, rows: list):
        if rows:
            self._mark_dirty()
        for row in rows:  # already in descending order from organizer
            self._canvas.remove_page_annotations(row)
        self._page_panel.refresh()
        if self._doc.doc:
            new_page = min(self._current_page, self._doc.page_count() - 1)
            self._current_page = new_page
            self._canvas.set_page(new_page)
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

    def _on_page_selected(self, page_num: int):
        if page_num == self._current_page and self._doc.doc:
            return
        self._current_page = page_num
        self._canvas.set_page(page_num)
        self._page_panel.set_current_page(page_num)
        self._update_status()

    def _on_annotation_changed(self):
        # Keep the left page panel's thumbnail of the current page in sync with edits.
        self._mark_dirty()
        self._refresh_current_thumb()

    def _refresh_current_thumb(self):
        if not self._doc.doc:
            return
        thumb = self._canvas.grab_current_thumbnail(self._page_panel.thumb_width())
        self._page_panel.update_page_thumbnail(self._current_page, thumb)

    def _update_status(self, extra: str = ""):
        self._update_title()
        if self._doc.doc:
            name = self._doc.path or "Untitled"
            base = f"{name}  —  page {self._current_page + 1} of {self._doc.page_count()}"
            self._status.showMessage(f"{base}  {extra}".strip())
        else:
            self._status.showMessage(extra or "Open a PDF to start  (Ctrl+O)")

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

    def closeEvent(self, event):
        if not self._maybe_save_before_close():
            event.ignore()
            return
        self._close_org_render()
        self._doc.close()
        super().closeEvent(event)
