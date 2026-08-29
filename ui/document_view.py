"""One open PDF, and every piece of UI and state that belongs to just that PDF.

Split out of MainWindow, phase 1 of docs/tabs-plan.md. Nothing in here is new.
The document, the canvas, the page panel, the Organizer, the toolbar, the search
bar and the Editor/Organizer switcher all used to hang off the window, which is
exactly why the window could only ever hold one PDF. They hang off this widget
now, so a shell can hold more than one of them later.

The Editor/Organizer QTabWidget below is NOT document tabs. It is a view
switcher for one document and it belongs in here, one per open PDF. Document
tabs are phase 2 and they go around this widget, not inside it.

WHAT STAYED ON THE WINDOW, AND WHY. Three controls act on a document but read
as part of the frame: the status bar's page box, the status bar's fit-mode
group, and the single-key tool shortcuts (v/h/r/l/t). They stay up there and
call down here, for three reasons. They are chrome, drawn in the status bar and
the menus rather than in the document area. They have to keep working when the
front document changes, which is a rebind, not a second copy. And two live
DocumentViews would each own a window-context QShortcut for "v", which Qt
reports as ambiguous and refuses to route. The search bar went the other way:
it is per document (its hits are page numbers in THIS document), so it lives in
here.

THREE THINGS IN HERE ARE LOAD-BEARING, AND THE WORST ONE FAILS SILENTLY.

1. The flush / save / strip / reload sequence. `_flush_annotations`,
   `_after_successful_save`, `_strip_baked_annotations` and
   `_load_saved_annotations` are what make annotations reopen editable instead
   of as flat pixels. The order is fixed, and they moved as one block. Break it
   and the file still saves; it just stops round-tripping, with nothing raised.

2. The markup-baked clone discipline. `_make_markup_baked_render` hands out a
   throwaway PDFDocument, and every one of them has a paired closer sitting
   next to the thing that makes it:

       _org_render    made by _refresh_organizer         closed by _close_org_render
       _panel_render  made by _refresh_panel_thumbnails  closed by _close_panel_render

   Every clone is closed exactly once, and `teardown()` closes both. That
   pairing is the whole defence: `_refresh_organizer` runs on every switch into
   the Organizer tab, so a missing close leaks a full fitz document per switch.

   THE ORDER WITHIN A REBUILD IS ALSO LOAD-BEARING, and that was known bug 6.
   Build the new clone, hand it over, THEN close the old one; never close first
   and build after, because both widgets rasterise from a queued zero timer and
   anything pumped in between is a render against a closed document. When a
   clone is being released rather than replaced, the widget's pointer at it is
   dropped first (`release_render_source`).

3. Undo stack ownership. `PDFCanvas.set_document` clears the undo stack, so one
   canvas can only ever serve one document. This widget owns its canvas
   outright and never shares it.

PHASE 3 ADDED BACKGROUNDING, and it is where the memory goes. `set_active` is
called by the tab bar on every switch, and a view that is no longer in front
drops its render cache and both markup clones. Phase 2's numbers say which half
matters: one document's six-entry pixmap cache is 207 MB, and ten live
documents plus twenty markup clones came to 2 MB between them. So
`invalidate_render_cache` is the saving and the clones are tidiness. The live
fitz document and the canvas scene stay, because those are what make a switch
back instant and what phase 1's finding 2 proved survive a move between
windows.

Releasing a clone on every tab switch is also what made known bug 6 fire
reliably rather than intermittently, which is why it was fixed in the same
phase. See `_refresh_organizer`.

PHASE 6 ADDED THE PENDING TAB, which is a view that stands for a file it has
not read yet. `stage_path` claims one, `ensure_loaded` opens it, and
`set_active` is what calls that: a restored tab is opened the first time it
comes to the front and never before. Everything that used to ask
`has_document()` to mean "is this tab free" asks `is_empty()` instead, because
a pending tab is spoken for with nothing loaded in it. See ui/session.py.
"""

import os
import uuid

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QMessageBox, QTabWidget, QVBoxLayout,
    QWidget,
)

from core.ocr_worker import run_ocr_enhance
from core.page_ops import is_permutation
from core.pdf_document import PDFDocument
from core.settings import dialog_start_dir, remember_dialog_dir, settings
from ui.canvas import PDFCanvas
from ui.combine_dialog import CombineDialog
from ui.organizer import PageOrganizer
from ui.page_commands import (
    DeletePagesCommand, ReorderPagesCommand, TransferPagesCommand,
)
from ui.page_drag import find_source_view
from ui.page_panel import PagePanel
from ui.search_bar import SearchBar
from ui.theme import ThemeManager
from ui.toolbar import ToolBar

# Debounce for search-as-you-type: long enough that fast typing doesn't
# re-scan the document per keystroke, short enough to feel live.
SEARCH_DEBOUNCE_MS = 220


def _transfer_note(warnings: dict) -> str:
    """The one line that says what a page move quietly lost or renamed.

    PyMuPDF reports none of it: an internal link whose target did not come
    along is dropped, layers are flattened, and a colliding form-field name is
    rewritten to `name [27]`. All three are silent, and none of them has a
    generic repair, so the honest thing is to say it once where the user is
    already looking. Empty when nothing was lost, which is the normal case.
    """
    parts = []
    links = warnings.get("links", 0)
    if links:
        parts.append(f"{links} internal link{'s' if links > 1 else ''} dropped")
    if warnings.get("layers"):
        parts.append("layers flattened")
    widgets = warnings.get("widgets", 0)
    if widgets:
        parts.append(f"{widgets} form field{'s' if widgets > 1 else ''} may be renamed")
    return f"  ({', '.join(parts)})" if parts else ""


class DocumentView(QWidget):
    """Everything that belongs to ONE open document."""

    #: The title line needs re-reading (name and/or modified state moved).
    #: The window title is window chrome, so the shell sets it; this only says
    #: when. Emitted from exactly the places that used to call _update_title().
    title_changed = Signal()

    #: Unsaved-changes state crossed a boundary. True means dirty.
    dirty_changed = Signal(bool)

    #: The viewed page moved, or the document under it changed size. The shell
    #: drives the status bar's page box off this.
    page_changed = Signal(int)

    #: A line for the status bar.
    status_message = Signal(str)

    #: This view has been asked to go away (File > Close PDF). The document is
    #: already closed by the time it goes out; what is left is the tab, and
    #: whether the window goes with it. See MainWindow._on_view_close_requested.
    close_requested = Signal()

    #: Files were chosen and want opening. The view does not open them itself
    #: because where they land is a decision about TABS, which is the window's:
    #: a file already open activates its tab, and anything else gets a new one.
    paths_requested = Signal(list)

    #: Files were chosen for a staged combine. Same reason: the merge lands in
    #: a tab, and picking that tab is the window's job.
    combine_requested = Signal(list)

    #: Republished from the canvas: the user zoomed manually, so no fit mode is
    #: active any more. The fit control is window chrome (see the module
    #: docstring), so the event has to cross the seam to reach it.
    fit_mode_broken = Signal()

    #: A freshly opened document should start in the remembered view mode.
    #: Applying it means checking a status-bar button, which is the window's,
    #: so the view asks rather than does.
    default_fit_requested = Signal()

    #: This document is about to be changed by something the user did while
    #: looking at ANOTHER tab, so bring it to the front first. Phase 5: one undo
    #: stack per window means Ctrl+Z can reach across tabs, and an edit you
    #: cannot see being undone is worse than no undo at all. DocumentArea
    #: switches to it; see _view_wiring.
    activation_requested = Signal()

    def __init__(self, theme: ThemeManager, parent=None):
        super().__init__(parent)
        # The theme is the window's, borrowed: this widget re-tints its own
        # code-drawn surfaces and hands the palette to the Combine dialog.
        self._theme = theme
        self._doc = PDFDocument()
        # A file this tab STANDS FOR but has not opened yet. Session restore is
        # the only thing that sets it (see ui/session.py): eight A1 drawings
        # opened at once before the window is usable would make startup feel
        # broken, and opening big drawings fast is the whole pitch. The tab
        # exists, is named after the path, and calls PDFDocument.open() the
        # first time it is brought to the front. `_pending_view` is the page,
        # zoom and fit to land on when that happens.
        self._pending_path = None
        self._pending_view: dict = {}
        self._current_page = 0
        self._org_render = None  # throwaway clone backing the Organizer's markup thumbnails
        self._panel_render = None  # throwaway clone backing the left page panel's thumbnails
        self._dirty = False      # unsaved changes exist (annotations, page edits, merges)
        self._dirty_announced = False  # last value dirty_changed went out with
        # A stable name for THIS document, carried in a page drag's payload so
        # the drop can find its source in the live registry without holding a
        # reference to a widget that may be gone by the time the drop lands.
        self._doc_id = uuid.uuid4().hex
        # DIRTY IS A REVISION COUNTER NOW, not QUndoStack.isClean(). One stack
        # serves the whole window (see ui/undo.py), so its clean index cannot
        # answer the question for three documents at once. Every command that
        # touches this document bumps _rev on redo and drops it on undo; a save
        # records the value it was saved at; dirty is "they differ". A
        # _saved_rev of None means the saved state sat in a redo branch that has
        # since been thrown away and can never be returned to.
        self._rev = 0
        self._saved_rev = 0
        # Dirt that no command produced and no undo can take back: an Organizer
        # merge, a page delete that had to clear the history. OR'd into dirty
        # and cleared by a save.
        self._forced_dirty = False
        # Front tab or background tab. True to start: a view is built to be
        # shown, and the only thing this flag gates is the RELEASE, so nothing
        # is gained by making a brand new view rebuild what it has not built.
        self._active = True
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
        self._setup_ui()

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _setup_ui(self):
        root = QVBoxLayout(self)
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
        self._page_panel.set_view(self)
        self._page_panel.page_selected.connect(self._on_page_selected)
        self._page_panel.pages_delete_requested.connect(self._delete_pages)
        self._page_panel.pages_reorder_requested.connect(self._reorder_pages)
        self._page_panel.pages_transfer_requested.connect(self._on_pages_dropped)
        editor_layout.addWidget(self._page_panel)

        self._canvas = PDFCanvas()
        self._canvas.annotation_changed.connect(self._on_annotation_changed)
        self._canvas.page_changed.connect(self._on_canvas_page_changed)
        # The canvas's edits move THIS document's revision counter, which is
        # what the modified marker is read off now. No cleanChanged connection:
        # the stack is about to become the whole window's (see set_undo_stack),
        # and one stack's clean index cannot speak for three documents.
        self._canvas.set_revision_owner(self)

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
        self._organizer.set_view(self)
        self._organizer.pages_transfer_requested.connect(self._on_pages_dropped)
        self._organizer.page_activated.connect(self._on_organizer_page_activated)
        self._organizer.pages_reordered_perm.connect(self._on_pages_reordered_perm)
        self._organizer.pages_deleted.connect(self._on_pages_deleted)
        self._organizer.pages_added.connect(self._on_pages_added)
        self._organizer.needs_rebuild.connect(self._refresh_organizer)
        self._tabs.addTab(self._organizer, "Organizer")

        # The status bar's fit group listens for this; republish it so the
        # window never has to hold a reference to the canvas.
        self._canvas.fit_mode_broken.connect(self.fit_mode_broken)

    # ------------------------------------------------------------------
    # What the shell asks this view about itself
    # ------------------------------------------------------------------

    def has_document(self) -> bool:
        return self._doc.doc is not None

    def is_pending(self) -> bool:
        """A restored tab that has not been opened yet. See `_pending_path`."""
        return self._pending_path is not None and self._doc.doc is None

    def is_empty(self) -> bool:
        """Nothing open and nothing on the way: the blank tab a window starts with.

        `not has_document()` used to be the whole question, and a lazy tab is
        the case that made it two. Everywhere that means "is this tab free to
        open something into" asks this instead, because a pending tab is spoken
        for even though nothing is loaded in it yet.
        """
        return self._doc.doc is None and self._pending_path is None

    def page_count(self) -> int:
        return self._doc.page_count()

    def current_page(self) -> int:
        return self._current_page

    def document_path(self):
        """The file this document came from, or None for an untitled merge.

        A pending tab answers with the file it is going to open, which is what
        makes the tab label, the tooltip, the duplicate-tab check and
        `WindowRegistry.find_by_path` all work on a tab nobody has opened yet.
        """
        return self._doc.path if self._doc.doc else self._pending_path

    def document_name(self):
        """The basename a title should show, or None with nothing open."""
        if not self._doc.doc:
            return os.path.basename(self._pending_path) if self._pending_path else None
        return os.path.basename(self._doc.path) if self._doc.path else "Untitled"

    def is_dirty(self) -> bool:
        return self._dirty

    def fit_mode(self):
        """The page-fit mode THIS canvas is actually in, or None.

        Not the same thing as the remembered setting: a manual zoom breaks the
        fit on one canvas and leaves the app-wide default alone. The window
        reads this when a tab comes to the front, so the status bar shows the
        mode the document on screen is in rather than the one that was chosen
        for some other tab.
        """
        return self._canvas.fit_mode()

    def view_scale(self) -> float:
        """How far this canvas is zoomed. Saved with the session; see fit_mode.

        Only worth restoring when no fit mode is active, because a fit
        recomputes the scale from the window size the moment it is applied.
        """
        return self._canvas.view_scale()

    def refresh_chrome(self):
        """Re-announce everything the window's chrome reads off this view.

        Called when this view comes to the front, where the status line, the
        page box and the title are all still showing the tab that just left.
        One call because they already share one publisher.
        """
        self._update_status()

    def undo_stack(self):
        """The stack this view's edits go on: the WINDOW's, once it is in one.

        Phase 5 moved it. A cross-document page move is one action with two
        document-level effects, and there is no ordering of two stacks that
        undoes it without leaving a duplicate, so the window owns one stack and
        every tab in it shares it. `MainWindow` hands it over in `_new_view`
        and `adopt`; until then this is the canvas's own, which is what lets a
        DocumentView still be built with no window at all. See ui/undo.py.
        """
        return self._canvas.undo_stack

    def set_undo_stack(self, stack):
        """Join a window's shared history (or leave one, by passing None)."""
        self._canvas.set_undo_stack(stack)

    def doc_id(self) -> str:
        """A stable name for this document, for a page drag's payload."""
        return self._doc_id

    def transfer_label(self) -> str:
        """What to call this document in the other one's close prompt."""
        return self.document_name() or "an untitled document"

    def request_activation(self):
        """Ask to be brought to the front, before something changes under me."""
        self.activation_requested.emit()

    # -- dirty, as a revision counter -----------------------------------

    def note_revision(self, delta: int):
        """One command touching this document was applied (+1) or undone (-1)."""
        self._rev += int(delta)
        self._sync_dirty()

    def note_branch_dropped(self):
        """A redo branch is about to be thrown away by a new edit.

        If this document's last save lives in that branch, the counter can
        never come back to it, so the marker is retired and the document stays
        dirty until it is saved again. Same rule QUndoStack applies to its own
        clean index, scoped to one document.
        """
        if self._saved_rev is not None and self._saved_rev > self._rev:
            self._saved_rev = None

    def _sync_dirty(self):
        """Recompute the modified state and announce it if it moved."""
        self._dirty = bool(
            self._forced_dirty
            or self._saved_rev is None
            or self._rev != self._saved_rev
        )
        self._update_title()

    def _reset_revisions(self):
        """Back to "in sync with disk", for a fresh open or a close."""
        self._rev = 0
        self._saved_rev = 0
        self._forced_dirty = False
        self._dirty = False

    # ------------------------------------------------------------------
    # What the shell does to this view
    # ------------------------------------------------------------------

    def apply_palette(self, palette):
        """Re-tint the code-drawn surfaces inside this view."""
        self._canvas.apply_palette(palette)
        self._page_panel.apply_palette(palette)
        self._organizer.apply_palette(palette)
        self._toolbar.apply_palette(palette)

    def set_page_panel_visible(self, visible: bool):
        """Driven by the View menu's Show Page Panel action, which owns the
        setting. The panel itself is per document, so it lives here."""
        self._page_panel.setVisible(visible)

    def set_fit_mode(self, mode: str):
        """Apply a page-fit mode to this view's canvas.

        The choosing, the remembering and the button that shows it are the
        window's (see MainWindow.choose_fit_mode); this is just the applying.
        """
        self._canvas.set_fit_mode(mode)

    def trigger_tool(self, tool: str):
        """The v/h/r/l/t shortcuts, which are held at window level."""
        self._toolbar.trigger_tool(tool)

    def bring_to_front(self):
        self._canvas.bring_to_front()

    def send_to_back(self):
        self._canvas.send_to_back()

    def jump_to_page(self, page_num: int):
        """A page number was typed into the status bar's page box.

        Moves the editor and, when it holds pages, the Organizer grid too, so
        whichever tab is in front lands on the page that was asked for.
        """
        self._on_page_selected(page_num)
        self._organizer.reveal_page(page_num)

    def mark_clean(self):
        """Stop the close path asking about unsaved changes again.

        For the one caller that has already put the question itself (the update
        strip). Deliberately does not touch the title: neither did the line it
        replaces.
        """
        self._forced_dirty = False
        self._saved_rev = self._rev
        self._dirty = False

    def is_active(self) -> bool:
        """Whether this is the front tab of its window."""
        return self._active

    def set_active(self, active: bool):
        """Front tab, or backgrounded. Driven by DocumentArea on every switch.

        This is the memory rule from phase 3 of docs/tabs-plan.md, and phase 2's
        measurements decided its order of importance. Backgrounding drops:

          - THE PIXMAP CACHE, which is where the cost is. One document's six
            entries at A1 and zoom 1.5 measured 207 MB, and ten open documents
            measured 2.02 GB of caches against 2 MB of everything else. It is
            one call and it cannot fail. Measured saving, six A1 tabs with
            every cache full: 1249 MB down to 387 MB, about 173 MB a tab. It is
            173 and not 207 because the canvas scene holds the page currently
            on screen as its background item and QPixmap is implicitly shared,
            so that one entry is not freed by dropping the cache. The corollary
            is that a tab where only one page was ever rendered saves nothing.
          - THE TWO MARKUP CLONES, worth about a megabyte, released because
            leaving them is a slow leak rather than because it is a saving.

        What deliberately stays: the live fitz document and the canvas scene.
        Those are what make a tab switch instant, and what finding 2 in the
        plan proved survive a move between windows.

        AND IT IS WHERE A RESTORED TAB IS OPENED, above the early return rather
        than below it. A view is built with `_active` already True (nothing is
        gained by making a brand new view rebuild what it has not built), so
        the first tab of a restored window is made current without `_active`
        ever changing, and a load hung off the flag would never run for it.
        """
        if active:
            self.ensure_loaded()
        if active == self._active:
            return
        self._active = active
        if active:
            self._on_activated()
        else:
            self._on_backgrounded()

    def _on_backgrounded(self):
        """Give back what a document nobody is looking at does not need."""
        if not self._doc.doc:
            return
        self._doc.invalidate_render_cache()
        self._close_org_render()
        self._close_panel_render()

    def _on_activated(self):
        """Rebuild what backgrounding released. Idempotent, and cheap.

        Only the panel is rebuilt unconditionally: it is on screen the moment
        the tab is. The Organizer is rebuilt only when it is the tab being
        shown, because `_on_tab_changed` already rebuilds it on the way in and
        doing it here as well would clone the document twice for one switch.
        """
        if not self._doc.doc:
            return
        self._refresh_panel_thumbnails(current_page=self._current_page)
        if self._tabs.currentIndex() == 1:
            self._refresh_organizer()

    def rerender_for_screen_change(self):
        """Redraw after the window landed on a monitor at a different scale.

        Cached page pixmaps are DEVICE-dependent. Rendered for a 1.0 screen and
        shown on a 1.5 one they are soft, and the panel thumbnails go with
        them. Nothing about the document changed, so this is a cache drop and a
        redraw, the same three calls the OCR path makes for the same reason.

        Phase 4 needs it because the tear-off can put a document on another
        monitor in one gesture. `MainWindow._on_screen_changed` covers every
        later move of the window; the gesture checks the ratio itself on the
        drop, which is the one move that happens before there is a window to
        have listened to.
        """
        if not self._doc.doc:
            return
        self._doc.invalidate_render_cache()
        self._canvas.reload_current_page()
        self._refresh_panel_thumbnails(current_page=self._current_page)

    def teardown(self):
        """Release everything this view owns, on the way out for good."""
        self._pending_path = None
        self._pending_view = {}
        self._close_org_render()
        self._close_panel_render()
        self._doc.close()

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def open_pdf(self):
        """File > Open. Picks the files; the window decides where they land."""
        paths, _ = QFileDialog.getOpenFileNames(
            self.window(), "Open PDFs", dialog_start_dir(self._doc.path),
            "PDF Files (*.pdf)"
        )
        if not paths:
            return
        remember_dialog_dir(paths[0])
        self.paths_requested.emit(sorted(paths))

    def open_path(self, path: str) -> bool:
        """Open ONE file into this view, which must be empty.

        THIS IS WHERE THE APPEND USED TO BE. `open_paths` took a list, and with
        a document already open it merged every one of them onto the end of it.
        Opening a second PDF silently changed the one you were reading, and the
        next Save wrote that merge over the file. A second file is a second tab
        now, so this takes one path and never touches an open document.

        Appending still exists where somebody asked for it: the Organizer's
        Add Pages, and the Combine dialog.
        """
        if self._doc.doc:
            return False
        if not self._doc.open(path):
            # The document knows WHY, and for a password-protected file that
            # reason is the whole message (known bug 4: it used to open, then
            # throw on the first render). Falls back to the old line otherwise.
            QMessageBox.critical(
                self.window(), "Error",
                self._doc.last_open_error or f"Could not open:\n{path}")
            return False
        self._reset_revisions()       # freshly opened, in sync with disk
        self._canvas.set_document(self._doc)
        self._page_panel.set_document(self._doc)
        self._current_page = 0
        # Stale search hits reference the previous document's pages.
        self._search_hits = []
        self._search_index = -1
        self._search_term = None
        # A freshly opened file may carry an editable model to restore.
        self._load_saved_annotations()
        # Always rebuild the panel thumbnails from a markup-baked clone after open.
        # _load_saved_annotations already does this when it restores a model, but a
        # file with baked markup and no model (or none to restore) still needs the
        # panel re-rendered so it matches the page rather than the pre-strip doc.
        self._refresh_panel_thumbnails()
        self._apply_default_fit()
        self._update_status()
        return True

    def stage_path(self, path: str, page: int = 0, zoom: float = 0.0,
                   fit_mode: str | None = None) -> bool:
        """Claim this empty view for a file WITHOUT opening it. Restore only.

        The tab that results is named after `path` and behaves like an open tab
        everywhere the name is what matters (the label, the tooltip, the
        already-open check, the close prompt). What it does not do is read the
        file, which is the point: a window of eight A1 drawings comes up as
        fast as an empty one and each document is read when it is first looked
        at. `ensure_loaded` is where that happens.
        """
        if self._doc.doc or not path:
            return False
        self._pending_path = path
        self._pending_view = {"page": page, "zoom": zoom, "fit_mode": fit_mode}
        self.title_changed.emit()
        return True

    def ensure_loaded(self) -> bool:
        """Open a pending tab's file, now. True if there was one to open.

        Driven by `set_active`, so the first time a restored tab comes to the
        front it becomes an ordinary open document and every later question
        about it is answered the ordinary way. A file that has gone since the
        session was saved reports itself through `open_path`'s message box,
        which is the right amount of noise for one tab the user just clicked;
        the whole-session case is filtered before any of these are made (see
        ui/session.py).
        """
        path = self._pending_path
        if path is None or self._doc.doc:
            return False
        # Cleared FIRST. open_path refuses to run against a view it thinks is
        # already spoken for, and a failed open must leave an ordinary empty
        # tab rather than one that tries again on every switch.
        self._pending_path = None
        wanted = self._pending_view
        self._pending_view = {}
        if not self.open_path(path):
            self.title_changed.emit()
            return True
        page = wanted.get("page") or 0
        if 0 < page < self._doc.page_count():
            self.jump_to_page(page)
        fit = wanted.get("fit_mode")
        if fit:
            self.set_fit_mode(fit)
        else:
            zoom = wanted.get("zoom") or 0.0
            if zoom > 0:
                # No fit mode was active when this was saved, so the canvas was
                # at a hand-set zoom. `_apply_default_fit` has already put a fit
                # on it by now; taking that off is what restores what was there.
                self._canvas.set_fit_mode(None)
                self._canvas.set_view_scale(zoom)
        return True

    def combine_pdfs(self):
        """File > Combine PDFs: pick files, stage them as movable cards, merge
        only when Combine is clicked.

        The open document is no longer closed to make room. The merge is a new
        document, so it gets a tab of its own and whatever was being read stays
        where it was.
        """
        paths, _ = QFileDialog.getOpenFileNames(
            self.window(), "Combine PDFs", dialog_start_dir(self._doc.path),
            "PDF Files (*.pdf)"
        )
        if not paths:
            return
        remember_dialog_dir(paths[0])
        self.combine_requested.emit(sorted(paths))

    def combine_paths(self, paths: list):
        """Run the staged combine into this view, which must be empty."""
        if self._doc.doc or not paths:
            return
        self._combine_with_dialog(list(paths))

    def _combine_with_dialog(self, paths: list):
        """Run the staged-combine dialog and adopt its merged output.

        The dialog holds everything in memory: cancelling (or closing it)
        leaves the app and every input file exactly as they were."""
        dlg = CombineDialog(paths, palette=self._theme.palette, parent=self.window())
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
            QMessageBox.warning(self.window(), "No PDF", "Open a PDF first.")
            return
        if self._canvas.has_clipboard_items():
            self._canvas.paste_clipboard_items()
            self._update_status("Pasted — drag to move, drag handles to resize")
        else:
            self.paste_image()

    def paste_image(self):
        """Paste a clipboard image (from Word, a screenshot, etc.) as a movable object."""
        if not self._doc.doc:
            QMessageBox.warning(self.window(), "No PDF", "Open a PDF first.")
            return
        if QApplication.clipboard().image().isNull():
            self._update_status("Clipboard has no image to paste")
            return
        self._canvas._paste_from_clipboard()
        self._update_status("Pasted image — drag to move, drag handles to resize")

    def request_close(self) -> bool:
        """File > Close PDF (Ctrl+W): close the document and tell the shell
        this view was asked to go.

        The document half and the announcement are separate because the
        internal callers (a combine, a CLI launch) want the document closed
        without saying the view is finished.
        """
        if not self.close_document():
            return False
        self.close_requested.emit()
        return True

    def close_document(self) -> bool:
        """Close the current document so the next Open starts fresh instead of
        appending. False when there was nothing open, or the user cancelled."""
        if not self._doc.doc:
            if self._pending_path is None:
                return False
            # A restored tab nobody ever opened. There is no document to save
            # and nothing to release, so closing it is forgetting the file it
            # stood for. Answering False here would make Ctrl+W a dead key on
            # every tab of a freshly restored window.
            self._pending_path = None
            self._pending_view = {}
            self._update_status()
            return True
        if not self.maybe_save_before_close():
            return False
        self.clear_document()
        return True

    def clear_document(self):
        """Drop the open document and return the view to its empty state.

        The prompt is the caller's job: this runs after the decision to close
        has been made, which is why both `close_document` and the
        X-closes-document path in MainWindow.closeEvent can share it.
        """
        self._close_org_render()
        self._close_panel_render()
        self._search_bar.hide()
        self._on_search_closed()
        self._doc.close()
        self._reset_revisions()
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
        # Ctrl+Z back to here won't prompt to save. Not setClean(): the stack
        # is the whole window's, so its clean index cannot mean "B is saved"
        # without also claiming it for A and C. This document remembers the
        # revision it was written at instead, and the ledger of pages it swapped
        # with another tab is settled by the same write.
        self._forced_dirty = False
        self._saved_rev = self._rev
        self._dirty = False
        self._doc.clear_transfer_ledger()
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
        self._report_failed_save()
        return False

    def save_pdf_as(self) -> bool:
        if not self._doc.doc:
            return False
        path, _ = QFileDialog.getSaveFileName(
            self.window(), "Save PDF As", dialog_start_dir(self._doc.path),
            "PDF Files (*.pdf)")
        if not path:
            return False
        remember_dialog_dir(path)
        self._flush_annotations()
        if self._doc.save(path):  # save() adopts `path` as the new canonical path
            self._after_successful_save(f"Saved to {path}")
            return True
        self._report_failed_save()
        return False

    def _report_failed_save(self):
        """Say what went wrong, in the words PDFDocument put together.

        It used to say "Could not save the PDF" whatever had happened, which
        was actively misleading for the one failure that does not lose the
        work: an in-place save whose swap over the original fails writes the
        new content to a `.bak` beside it and adopts that file. The user has to
        be told, because the document in front of them is now a different file
        from the one they opened. The title and the tab label both follow the
        path, so both are re-read here.
        """
        QMessageBox.critical(
            self.window(), "Save Error",
            self._doc.last_save_error or "Could not save the PDF.")
        self.title_changed.emit()
        self._update_status()

    def enhance_for_search(self):
        """Run OCR once, on demand, over every page that doesn't already have
        an extractable text layer — so scanned/image-only pages become
        searchable. Runs on a background thread with a progress dialog; the
        normal editing UI stays responsive and untouched while it runs.
        """
        if not self._doc.doc:
            QMessageBox.warning(self.window(), "No PDF", "Open a PDF first.")
            return
        if self._ocr_thread is not None:
            QMessageBox.information(self.window(), "OCR In Progress",
                                    "Already enhancing this document.")
            return

        # Bake current edits into the live doc first (same as a normal save)
        # so OCR runs against the up-to-date page content, not stale markup.
        self._flush_annotations()

        self.status_message.emit("Enhancing for search (OCR)…")
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
                self.window(), "OCR Problem",
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
                self.window(), "Save Now?",
                f"Enhanced {ocred_count} page(s) for search.\n\n{verify}\n\nSave now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.save_pdf()

    # ------------------------------------------------------------------
    # Text search (Ctrl+F)
    # ------------------------------------------------------------------

    def open_search(self):
        if not self._doc.doc:
            QMessageBox.warning(self.window(), "No PDF", "Open a PDF first.")
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

    def delete_key(self):
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
            QMessageBox.warning(self.window(), "Cannot Delete",
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

    def _on_pages_dropped(self, payload: dict, at: int, copy: bool):
        """Pages from another document landed in this one's strip or grid.

        The widget has already checked the mime format and that the payload
        names a different document; this resolves the payload to a live view
        and hands it to the undoable edit. Resolving here rather than in the
        widget keeps the widget ignorant of documents, which is the same rule
        that stops it applying a reorder itself.
        """
        source = find_source_view(payload.get("doc_id", ""))
        if source is None:
            self._update_status("That document is no longer open")
            return
        self.transfer_pages_from(source, payload.get("rows", []), at, copy=copy)

    def transfer_pages_from(self, src_view, rows: list, at: int,
                            copy: bool = False) -> bool:
        """Take pages out of another open document into this one, undoably.

        The document half of dragging a page from one tab into another. The
        drop handler has already decided this is a genuine cross-document move
        (a drop back into the source is routed to the plain reorder instead),
        and the window has already refused a cross-WINDOW one; what is left is
        the edit.

        ONE COMMAND, on the WINDOW's stack, because it changes two documents
        and has to come back as one. See ui/undo.py and TransferPagesCommand.
        """
        if src_view is None or src_view is self or not self._doc.doc:
            return False
        if not src_view.has_document():
            return False
        rows = sorted({int(r) for r in rows if 0 <= int(r) < src_view.page_count()})
        if not rows:
            return False
        if not copy and src_view.page_count() - len(rows) < 1:
            # Matches the delete guard: a document has to keep a page. Refused
            # at dragMoveEvent time as well, so the user is not left holding a
            # drag that can never land.
            self._update_status("A document has to keep at least one page")
            return False
        command = TransferPagesCommand(self, src_view, rows, at, copy=copy)
        self.undo_stack().push(command)
        count = len(rows)
        pages = f"{count} page{'s' if count > 1 else ''}"
        verb = "Copied" if copy else "Moved"
        note = _transfer_note(command.warnings())
        self._update_status(
            f"{verb} {pages} from {src_view.transfer_label()}{note}"
            "  (Ctrl+Z to undo)")
        return True

    def transfer_warning(self) -> str:
        """What an unsaved cross-document move means for THIS document's close.

        Save is per document and is never atomic across two, deliberately: this
        is a field tool and the user has to know exactly what got written. So
        the close prompt says what is actually at stake rather than offering a
        "save both" button that would hide it.
        """
        lines = []
        for count, name in self._doc.transfers_sent:
            pages = f"{count} page{'s' if count > 1 else ''}"
            lines.append(
                f"{pages} moved out of here into {name}. Discarding leaves "
                f"{'them' if count > 1 else 'it'} in this file AND in {name}.")
        for count, name in self._doc.transfers_taken:
            pages = f"{count} page{'s' if count > 1 else ''}"
            lines.append(
                f"{pages} moved here from {name}. Discarding loses "
                f"{'them' if count > 1 else 'it'}.")
        return "\n\n".join(lines)

    def after_page_structure_change(self):
        """Re-sync the view around a page delete or reorder, in either direction.

        The command has already changed the document and re-based the canvas's
        markup; this is everything that hangs off that. Called by both redo() and
        undo(), so an undo lands the app in exactly the state a fresh edit would.
        """
        # Not _mark_dirty(): a page command moves the revision counter on both
        # its redo and its undo, so undoing back to the saved state has to be
        # able to land clean again.
        self._sync_dirty()
        self._current_page = self._canvas.current_page()
        select = self._pending_page_selection
        self._pending_page_selection = None
        self._refresh_panel_thumbnails(current_page=self._current_page, select=select)
        self._refresh_current_thumb()
        self._update_status()

    def delete_current_page(self):
        if not self._doc.doc or self._doc.page_count() <= 1:
            QMessageBox.warning(self.window(), "Cannot Delete",
                                "Cannot delete the only page.")
            return
        reply = QMessageBox.question(
            self.window(), "Delete Page",
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
        self._canvas.clear_own_history()
        self._refresh_panel_thumbnails()
        new_page = min(self._current_page, self._doc.page_count() - 1)
        self._current_page = new_page
        self._canvas.set_page(new_page, immediate=True)
        self._page_panel.set_current_page(new_page)
        self._update_status()

    # ------------------------------------------------------------------
    # Editor / Organizer switching, and the markup-baked render clones
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
        thumbnails via a throwaway clone (so the live document isn't mutated).

        KNOWN BUG 6 IS FIXED HERE, and the fix is the ORDER. This method used
        to close the previous clone as its first statement and only build the
        replacement several lines later, with a `processEvents()` in between.
        `PageOrganizer.refresh` queues `_render_visible` on a zero timer, so
        that pump ran a render against a fitz document that had just been
        closed and PyMuPDF raised "document closed" from inside a queued Qt
        callback, where PySide prints it to stderr and carries on. The first
        switch into the Organizer was clean because there was no previous clone
        to close, which is why nothing noticed for so long; the second broke
        every time there was still a render queued.

        Build, hand over, THEN close. The Organizer is pointing at a live
        document at every instant, so it no longer matters what runs during the
        pump.
        """
        if not self._doc.doc:
            self._organizer.set_document(self._doc, None)
            self._close_org_render()
            return
        self.status_message.emit("Loading organizer…")
        # Safe here: the Organizer is still rendering from the PREVIOUS clone,
        # which is still open, and stays open until the swap below is done.
        QApplication.processEvents()
        previous, self._org_render = self._org_render, None
        try:
            self._org_render = self._make_markup_baked_render()
        except Exception:
            # Nothing was handed over, so put the grid's source back rather
            # than leaving it pointing at a clone about to be closed.
            self._org_render = previous
            raise
        self._organizer.set_document(self._doc, self._org_render)
        self._close_render(previous)
        self._update_status()

    @staticmethod
    def _close_render(render):
        """Close one throwaway clone. The caller has already made sure nothing
        is still rendering from it."""
        if render is not None and render.doc is not None:
            try:
                render.doc.close()
            except Exception:
                pass

    def _close_org_render(self):
        # The pointer goes before the document does. The Organizer renders on a
        # queued zero timer, so closing the clone while the grid still names it
        # is exactly known bug 6; releasing (rather than replacing) the source
        # leaves the grid on the live document, which is still open.
        self._organizer.release_render_source(self._org_render)
        self._close_render(self._org_render)
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
        # Same ordering as _close_org_render, and for the same reason: the
        # panel renders its thumbnails on a queued zero timer too, so it has to
        # stop naming the clone before the clone is closed. It never bit here
        # because the only caller was the rebuild directly above, which closes
        # and re-hands-over with nothing pumped in between. Backgrounding calls
        # it on its own, which is where it would have.
        self._page_panel.release_render_source(self._panel_render)
        self._close_render(self._panel_render)
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
        self._canvas.clear_own_history()
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
        self._canvas.clear_own_history()
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
        # (The caller, open_path, rebuilds the left-panel thumbnails from a clone
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
        self._sync_dirty()
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
            self.status_message.emit(f"{base}  {extra}".strip())
        else:
            self.status_message.emit(extra or "Open a PDF to start  (Ctrl+O)")
        # The page box is in the status bar, so the window drives it; it reads
        # page_count()/has_document() off this view when this lands.
        self.page_changed.emit(self._current_page)

    def _apply_default_fit(self):
        """A freshly opened document starts in the remembered view mode.

        The mode is applied to this canvas, but choosing it also checks a
        status-bar button, so the window is the one that can do it.
        """
        self.default_fit_requested.emit()

    # ------------------------------------------------------------------
    # Unsaved-changes (dirty) + untitled (merged) state
    # ------------------------------------------------------------------

    def _update_title(self):
        """The title line moved: say so, and let the shell paint it.

        This kept its name and every one of its call sites; all that changed is
        that the two lines which set the window's title and modified flag are
        now the window's, because that is window chrome (see MainWindow).
        """
        self.title_changed.emit()
        if self._dirty != self._dirty_announced:
            self._dirty_announced = self._dirty
            self.dirty_changed.emit(self._dirty)

    def _mark_dirty(self):
        """Dirt no command produced and no undo can take back.

        An Organizer merge, an OCR pass, a page delete that had to clear the
        history: all of them change the document outside the undo stack, so
        they set a flag that only a save clears. Everything that IS undoable
        goes through the revision counter instead (see note_revision).
        """
        self._forced_dirty = True
        self._sync_dirty()

    def _mark_untitled(self):
        """A merge produced a derived document with no source file → force Save As."""
        self._doc.path = None

    def _on_pages_added(self, count: int):
        # Organizer "+ Add Pages" merged another PDF in → derived, unsaved document.
        self._mark_untitled()
        self._mark_dirty()

    def maybe_save_before_close(self) -> bool:
        """Prompt to save unsaved changes. Returns True if it's safe to proceed."""
        if not self._doc.doc or not self._dirty:
            return True
        warning = self.transfer_warning()
        reply = QMessageBox.question(
            self.window(), "Unsaved Changes",
            "This document has unsaved changes. Save before closing?"
            + (f"\n\n{warning}" if warning else ""),
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
