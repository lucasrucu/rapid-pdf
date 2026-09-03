import fitz
import json
import os
import re
import tempfile
from collections import OrderedDict
from PySide6.QtGui import QPixmap, QImage

from core.render_scale import AUTO, choose_render_scale
from core.resources import bundled_tessdata_dir
from core.settings import settings


def _resolve_tessdata() -> str | None:
    """Language data folder for the OCR engine embedded in PyMuPDF.

    Precedence: an explicit TESSDATA_PREFIX (the user knows best, e.g. for
    extra languages) > the bundled assets/tessdata folder (ships with the
    app, so OCR works on machines with no Tesseract install) > None, which
    lets PyMuPDF hunt for an installed Tesseract-OCR like before."""
    if os.environ.get("TESSDATA_PREFIX"):
        return None   # pdfocr_tobytes reads the env var itself
    return bundled_tessdata_dir()


RAPID_PDF_TAG = "rapid-pdf"
# Name of the embedded file that carries the editable annotation model, so a
# document saved by rapid-pdf reopens with its objects still movable/editable.
MODEL_EMBED_NAME = "rapid_pdf_model.json"

# How many rendered page pixmaps to keep in the LRU cache. A1 drawings rasterise
# to large QPixmaps (a 2384x1684pt page at zoom 1.5 is ~3576x2526 px ≈ 36 MB of
# 32-bit pixels). Keep this small so memory stays bounded on big documents while
# still covering the realistic hot pattern: lift + reload + a couple of
# page-switch round-trips all hit the same page+zoom.
RENDER_CACHE_MAX = 6


def source_is_readable(source) -> bool:
    """Whether pages can still be pulled out of this render source.

    THE QUESTION `if source.doc:` DOES NOT ANSWER, and getting that wrong is
    known bug 6 in docs/tabs-plan.md. PyMuPDF's `Document` defines `__len__`,
    so truth-testing a CLOSED document raises "document closed" rather than
    returning False. Every lazy thumbnail render in the app is scheduled on a
    zero timer and run later, which is exactly the window in which the clone it
    was scheduled against can have been closed, so each of them has to ask this
    instead of trusting a truth test.

    Takes any render source, not just a PDFDocument: the page panel and the
    Organizer are both handed stand-ins whose `.doc` is not a fitz document at
    all, and one of those has no opinion about being closed, so it is taken at
    its word.
    """
    if source is None:
        return False
    doc = getattr(source, "doc", None)
    if doc is None:
        return False
    try:
        return not doc.is_closed
    except AttributeError:
        return True          # not a fitz document; it cannot have been closed
    except Exception:
        return False         # anything a closed document raises means no


class PDFDocument:
    def __init__(self):
        self.doc: fitz.Document | None = None
        self.path: str | None = None
        # Why the last save() returned False, in words fit to show a user, or
        # None. Set on every failure and cleared at the top of every save, so
        # the caller reads it immediately after a False and never later.
        self.last_save_error: str | None = None
        # Why the last open() returned False, in words fit to show a user, or
        # None. Same contract as last_save_error: read it straight after a False.
        self.last_open_error: str | None = None
        # Cross-document page moves this document has been part of SINCE ITS
        # LAST SAVE, so the close prompt can say what is actually at stake.
        # Two lists of plain strings (the other document's display name):
        #   sent_to    - pages that left here and are now living in that file
        #   taken_from - pages that arrived here out of that file
        # Set by the transfer command, cleared by save(). See
        # DocumentView.transfer_warning.
        self.transfers_sent: list[tuple[int, str]] = []
        self.transfers_taken: list[tuple[int, str]] = []
        # LRU cache of rendered page pixmaps keyed by (page_num, zoom_key).
        # A cache hit makes a repeated render_page of the same page+zoom free
        # (the lift re-render, reload-after-strip, organizer/page round-trips).
        # MUST be invalidated whenever a page's content changes — a stale pixmap
        # showing a lifted-out image still present, or old baked markup, is a
        # correctness regression worse than slowness. See invalidate_* below and
        # the call sites in canvas/main_window.
        self._render_cache: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        # The raster scale this document's pages are drawn at, decided once from
        # page geometry on first ask and then never again. None means "not yet
        # decided"; see render_scale() for why it is settled once and not
        # recomputed when the setting changes.
        self._render_scale: float | None = None

    # ------------------------------------------------------------------
    # Rendered-page pixmap cache
    # ------------------------------------------------------------------

    @staticmethod
    def _zoom_key(zoom: float) -> float:
        # Round so tiny float drift on zoom doesn't defeat the cache, while
        # genuinely different zoom levels still key separately.
        return round(float(zoom), 4)

    def render_page_cached(self, page_num: int, zoom: float = 1.5) -> QPixmap:
        """render_page with an LRU pixmap cache keyed by (page_num, zoom).

        Returns the SAME QPixmap instance for repeated calls — callers must treat
        it as read-only (copy() before cropping; setPixmap shares it, which is
        fine). Any mutation of the page's content must call invalidate_render_page
        (single page) or invalidate_render_cache (whole doc) first.
        """
        key = (page_num, self._zoom_key(zoom))
        pix = self._render_cache.get(key)
        if pix is not None:
            self._render_cache.move_to_end(key)   # mark most-recently-used
            return pix
        pix = self.render_page(page_num, zoom)
        # Don't cache an empty/failed render (e.g. doc closed); a later valid
        # render must not be shadowed by a cached blank.
        if not pix.isNull():
            self._render_cache[key] = pix
            self._render_cache.move_to_end(key)
            while len(self._render_cache) > RENDER_CACHE_MAX:
                self._render_cache.popitem(last=False)   # evict least-recently-used
        return pix

    def invalidate_render_page(self, page_num: int):
        """Drop every cached zoom-level for one page (its content changed)."""
        for key in [k for k in self._render_cache if k[0] == page_num]:
            del self._render_cache[key]

    def invalidate_render_cache(self):
        """Drop the whole cache (doc reopened/saved, pages reordered/deleted)."""
        self._render_cache.clear()

    def adopt(self, fitz_doc):
        """Take ownership of an in-memory fitz document (e.g. the Combine
        dialog's merged output). The document has no path yet, so the first
        save is forced through Save As; nothing touches disk until then."""
        if self.doc:
            self.doc.close()
        self.invalidate_render_cache()
        self._render_scale = None    # different document, different geometry
        self.doc = fitz_doc
        self.path = None

    def replace_from_bytes(self, payload: bytes) -> bool:
        """Swap this document's CONTENT for `payload`, keeping its identity.

        The landing point for work done on a private copy of this document on
        a background thread: see core/ocr_worker.py, which OCRs its own
        independent fitz.Document because PyMuPDF documents are not thread
        safe, and hands the finished file back as bytes for the UI thread to
        apply here.

        Not `adopt`, and the difference is the whole reason this exists.
        `adopt` takes over from a genuinely different document, so it drops the
        path and forces the next save through Save As. This is the SAME
        document with new page content, so the path stays and the next Ctrl+S
        writes where it always would have.

        `_render_scale` is deliberately NOT reset either. Page geometry is
        unchanged by an OCR pass, and the scale is baked into the scene
        coordinates of every annotation on the document and into the undo
        stack behind them: see `render_scale` for why changing it under a live
        document is not a thing that can be done in isolation.
        """
        if not payload:
            return False
        try:
            replacement = fitz.open("pdf", payload)
        except Exception as e:
            self.last_open_error = f"Could not apply the result:\n{e}"
            return False
        path = self.path
        if self.doc:
            self.doc.close()
        self.doc = replacement
        self.path = path
        self.invalidate_render_cache()
        return True

    def open(self, path: str) -> bool:
        """Open a file, or return False with the reason in last_open_error.

        THE needs_pass CHECK IS NOT OPTIONAL, and it is known bug 4 in
        docs/tabs-plan.md. fitz.open() SUCCEEDS on a password-protected PDF: it
        hands back a document that reports a real page count and then raises
        "document closed or encrypted" on the first render. The app used to
        accept the file, draw an empty two-page document and blow up as soon as
        anything asked for a pixmap. Refusing here is the whole fix, and it is
        also why the page-transfer work does not have to special-case encrypted
        documents: one can never be open in the first place.
        """
        self.last_open_error = None
        try:
            if self.doc:
                self.doc.close()
            self.invalidate_render_cache()   # new document — no stale pixmaps
            self._render_scale = None        # and a fresh scale decision
            self.doc = fitz.open(path)
            if getattr(self.doc, "needs_pass", False):
                self.doc.close()
                self.doc = None
                self.last_open_error = (
                    "This PDF is password protected, so it cannot be opened."
                )
                return False
            self.path = path
            return True
        except Exception as e:
            print(f"Open error: {e}")
            self.doc = None
            self.last_open_error = f"Could not open the PDF:\n{e}"
            return False

    def close(self):
        if self.doc:
            self.doc.close()
        self.doc = None
        self.path = None
        self.clear_transfer_ledger()
        self.invalidate_render_cache()
        self._render_scale = None

    def is_open(self) -> bool:
        """Whether there is a document here that can still be read.

        NOT the same question as `if pdf.doc:`. See `source_is_readable`.
        """
        return source_is_readable(self)

    def page_count(self) -> int:
        return len(self.doc) if self.doc else 0

    def get_page_size(self, page_num: int) -> tuple[float, float]:
        if not self.doc or page_num >= len(self.doc):
            return (0.0, 0.0)
        # page.bound() gives the visible dimensions after rotation; page.rect does not.
        r = self.doc[page_num].bound()
        return (r.width, r.height)

    def render_scale(self) -> float:
        """The raster scale for THIS document, decided once and then fixed.

        Asked for on the open path, before anything is drawn, and answered from
        `page.bound()` on the first page, which costs nothing: the page's size
        is in the PDF's own structure, so no rasterisation is needed to learn
        it. See core/render_scale.py for the megapixel budget behind the choice
        and the measurements that set it.

        MEMOISED ON PURPOSE, and the memo is the feature rather than an
        optimisation. Annotations live in scene space, which is rendered-pixel
        space, so the scale is baked into the coordinates of every mark on the
        document and into the undo stack behind them. Changing it while a
        document is open would mean rescaling all of that in step. So the
        answer is computed on first use and returned unchanged forever after:
        the canvas can call this on every page load, a settings change lands on
        the next tab the user opens, and a save (which reopens the file in
        place) keeps the scale the markup was drawn against.

        Reset only where a genuinely different document takes this object over:
        `open`, `adopt` and `close`.
        """
        if self._render_scale is None:
            width, height = self.get_page_size(0)
            try:
                setting = settings().view.render_scale
            except Exception:
                # A settings store that cannot be built must not stop a file
                # opening. Auto is the default anyway, so this loses nothing
                # but an explicit override nobody can read.
                setting = AUTO
            self._render_scale = choose_render_scale(
                width, height, self.page_count(), setting)
        return self._render_scale

    @staticmethod
    def _render_page_at_zoom(page, zoom: float) -> QPixmap:
        """Rasterise a fitz page at the given uniform zoom into an opaque QPixmap.

        Shared by render_page (fixed zoom) and render_thumbnail (zoom derived
        from a target width) so the fitz→QImage→QPixmap conversion lives once.

        THE BUFFER IS BOUND TO A NAME ON PURPOSE, and it is not a style choice.
        QImage does NOT copy the memory it is handed; it borrows it and expects
        the caller to keep it alive for as long as the QImage is. This used to
        read `QImage(bytes(pix.samples), ...)`, where the bytes object was an
        unnamed temporary whose last reference died the moment the QImage
        constructor returned. Every render after that point was reading freed
        memory, and `QPixmap.fromImage` on the next line was the one deep copy
        that had to happen while the buffer was still valid. It usually got
        away with it, because a just-freed block is usually still intact, which
        is exactly what makes this class of bug show up as a random crash
        rather than a reproducible one.
        """
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        samples = pix.samples          # keep the buffer alive past fromImage
        img = QImage(samples, pix.width, pix.height, pix.stride,
                     QImage.Format.Format_RGB888)
        return QPixmap.fromImage(img)

    def render_page(self, page_num: int, zoom: float = 1.5) -> QPixmap:
        if not self.doc or page_num >= len(self.doc):
            return QPixmap()
        return self._render_page_at_zoom(self.doc[page_num], zoom)

    def render_thumbnail(self, page_num: int, max_width: int = 110) -> QPixmap:
        if not self.doc or page_num >= len(self.doc):
            return QPixmap()
        page = self.doc[page_num]
        # page.bound() gives the visible (post-rotation) dimensions; page.rect does not.
        zoom = max_width / page.bound().width
        return self._render_page_at_zoom(page, zoom)

    def save(self, path: str | None = None) -> bool:
        """Write the document. False means nothing was written where it was asked.

        A False ALWAYS leaves `last_save_error` set to something worth showing
        the user, and the in-place path has one failure mode that needs saying
        out loud. If the finished file cannot be swapped over the original (it
        is open in Acrobat, it is read-only, a sync client has it locked, or
        another Rapid PDF window is holding it), the new content is salvaged
        next to it as `<name>.pdf.bak` so no work is lost.

        THAT SALVAGE USED TO BE SILENT. `save()` returned False, the window
        said "Could not save the PDF", and `self.path` was left naming the
        original. Four things then disagreed: the live document was the .bak,
        the title bar and the tab named the original, the original on disk
        still held the old content, and the next Save wrote to whichever of
        them the path said. Two windows can now hold the same file, which makes
        a losing swap ordinary rather than exotic, so it is closed from both
        ends: the .bak is ADOPTED as the document's path, so everything
        downstream names the file that actually holds the work, and the reason
        goes in `last_save_error` for the caller to show.
        """
        self.last_save_error = None
        if not self.doc or not (self.path or path):
            self.last_save_error = "There is no document to save."
            return False
        target = path or self.path
        # An untitled (merged) doc has no current path → it's never an in-place save.
        is_same = self.path is not None and os.path.abspath(target) == os.path.abspath(self.path)
        # A save bakes markup/redactions into page content and (in-place) reopens
        # the document. Every cached page pixmap is now stale (would still show
        # pre-bake content); drop them all.
        self.invalidate_render_cache()
        tmp_path = None
        try:
            if is_same:
                dir_path = os.path.dirname(os.path.abspath(target))
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir=dir_path) as tf:
                    tmp_path = tf.name
                # If this write raises, the outer except cleans up tmp_path (the
                # doc is still open and untouched, so the save simply fails safely).
                self.doc.save(tmp_path, garbage=4, deflate=True)
                # PyMuPDF can't write over its own open file, so close before the
                # swap. Drop the handle to None immediately: if anything below
                # fails, the except must never leave self.doc pointing at a closed
                # document (that would make every later render/save raise
                # "document closed" with no way to recover from the UI).
                self.doc.close()
                self.doc = None
                try:
                    # os.replace is atomic and overwrites on both POSIX and Windows.
                    # shutil.move falls back to a non-atomic copy when the target
                    # already exists on Windows, which can leave a truncated,
                    # corrupt original if the process dies mid-copy.
                    os.replace(tmp_path, target)
                except Exception as move_err:
                    # Couldn't swap the new file in; salvage the new content so no
                    # work is lost. Reopen from the .bak so the document stays live.
                    bak = target + ".bak"
                    try:
                        os.replace(tmp_path, bak)
                        self.doc = fitz.open(bak)
                        # Adopt it. The live document IS this file now, so
                        # letting `path` keep naming the original is what makes
                        # the two diverge without anybody being told.
                        self.path = bak
                        raise RuntimeError(
                            f"Could not overwrite:\n{target}\n\n"
                            f"Your work was saved to:\n{bak}\n\n"
                            "That file is now the open document. The original "
                            "is unchanged. Close whatever is holding it (another "
                            "window, Acrobat, a sync client) and use Save As to "
                            "put this back over it."
                        ) from move_err
                    except RuntimeError:
                        raise
                    except Exception as bak_err:
                        print(f"Save recovery error: {bak_err}")
                        # Last resort: try reopening the original (unchanged on disk).
                        try:
                            self.doc = fitz.open(target)
                        except Exception:
                            pass
                        raise RuntimeError(
                            f"Could not overwrite:\n{target}\n\n"
                            "The edits could not be written anywhere and are "
                            "still only in this window. Use Save As to put them "
                            "somewhere writable before closing it."
                        ) from move_err
                # Reopen the freshly written file as the live document.
                self.doc = fitz.open(target)
            else:
                self.doc.save(target, garbage=4, deflate=True)
            # Adopt the target as the canonical path so later saves write in place.
            self.path = target
            # Whatever this document owed the other side of a page move is now
            # on disk, so the close prompt has nothing left to warn about.
            self.clear_transfer_ledger()
            return True
        except Exception as e:
            print(f"Save error: {e}")
            # The window shows this verbatim, so a RuntimeError raised above
            # carries its own wording and anything else gets a line built here.
            self.last_save_error = (
                str(e) if isinstance(e, RuntimeError)
                else f"Could not save:\n{target}\n\n{e}")
            # If the temp file was written but never renamed into place (the swap
            # succeeds by renaming it away, and the .bak path renames it too), it's
            # orphaned next to the target — clean it up so failed saves don't litter.
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False

    # ------------------------------------------------------------------
    # OCR ("Enhance for Search") — on-demand, explicit only
    # ------------------------------------------------------------------

    def page_has_text(self, page_num: int) -> bool:
        """True if this page already carries an extractable text layer.

        Used to skip pages that don't need OCR — most pages in a normal
        editing session already have real text, so this keeps a full-document
        OCR pass fast and avoids garbling/duplicating existing text.
        """
        if not self.doc or page_num >= len(self.doc):
            return False
        try:
            return bool(self.doc[page_num].get_text().strip())
        except Exception:
            return False

    def ocr_page(self, page_num: int, language: str = "eng", dpi: int = 150) -> bool:
        """Replace this page's content with an OCR'd version carrying an
        invisible, searchable text layer, via the Tesseract engine compiled
        into PyMuPDF (no tesseract.exe needed at runtime).

        Only meant to be called on pages that fail page_has_text() (i.e.
        scanned/image-only pages) — this rasterizes the page, so running it
        on a page that already has real vector text/graphics would destroy
        that content, not just add a text layer alongside it.

        Note: fitz.Page.get_textpage_ocr() alone does NOT persist a text
        layer into the saved file — it only returns an in-memory TextPage
        for immediate extraction. Producing bytes via Pixmap.pdfocr_tobytes()
        and splicing that in as the new page is what actually survives
        doc.save() and a later reopen (verified by testing).

        Dependency note: the OCR ENGINE is embedded in PyMuPDF, but the
        LANGUAGE DATA is not. _resolve_tessdata() supplies it: a user-set
        TESSDATA_PREFIX first, then the bundled assets/tessdata (ships in
        the installer, so OCR works on machines without Tesseract), then
        PyMuPDF's own hunt for an installed Tesseract-OCR. Only if all
        three come up empty does this raise RuntimeError("No tessdata
        specified and Tesseract is not installed"). Callers must surface
        errors to the user instead of swallowing them.

        Returns True on success; raises on OCR failure so the caller can
        report the real reason (the old behavior of returning False buried
        the missing-tessdata error).
        """
        if not self.doc or page_num >= len(self.doc):
            return False
        page = self.doc[page_num]
        pix = page.get_pixmap(dpi=dpi)
        ocr_bytes = pix.pdfocr_tobytes(compress=True, language=language,
                                       tessdata=_resolve_tessdata())
        ocr_src = fitz.open("pdf", ocr_bytes)
        try:
            # Insert the OCR'd replacement right after the original, then
            # drop the original, which keeps this page's position in the
            # document unchanged.
            self.doc.insert_pdf(ocr_src, from_page=0, to_page=0, start_at=page_num + 1)
        finally:
            ocr_src.close()
        self.doc.delete_page(page_num)
        self.invalidate_render_page(page_num)
        return True

    def page_char_count(self, page_num: int) -> int:
        """Number of extractable text characters on the page (0 = no text
        layer). Used to verify, in-app, that an OCR pass actually produced
        searchable text."""
        if not self.doc or page_num >= len(self.doc):
            return 0
        try:
            return len(self.doc[page_num].get_text().strip())
        except Exception:
            return 0

    def text_layer_report(self) -> list[int]:
        """Per-page character counts for the whole document, index = page."""
        return [self.page_char_count(pn) for pn in range(self.page_count())]

    def search_text(self, needle: str) -> list[tuple[int, "fitz.Rect"]]:
        """Find every occurrence of `needle` (case-insensitive, as PyMuPDF
        does) across the document. Returns [(page_num, rect), ...] in page
        order; rects are in the page's displayed coordinate space, the same
        space render_page rasterises (so scene coords = rect * zoom)."""
        hits: list[tuple[int, fitz.Rect]] = []
        if not self.doc or not needle:
            return hits
        for pn in range(len(self.doc)):
            try:
                for r in self.doc[pn].search_for(needle):
                    hits.append((pn, r))
            except Exception as e:
                print(f"Search error (page {pn}): {e}")
        return hits

    def remove_image_placement(self, page_num: int, xref: int) -> bool:
        """Remove the single content-stream draw of `xref` on this page, non-destructively.

        Visio/automation pages (e.g. from noe_painter) stamp each image with one
        `<a b c d e f> cm /Name Do` operator on top of a full-page background raster.
        Redacting the image's rect to "erase" it also blanks the background pixels
        underneath -> a white hole. Deleting just that one placement operator removes
        the image while leaving everything behind it untouched (no hole), the way a
        real PDF editor moves an object.

        Only the tight `cm` (six numbers) immediately-before-`Do` form is removed —
        that cm exists solely to place this image, so dropping it is self-contained.
        Returns True if a placement was removed; False if the safe pattern wasn't
        found (caller should fall back to redaction).
        """
        if not self.doc or page_num >= len(self.doc):
            return False
        page = self.doc[page_num]
        name = None
        for im in page.get_images(full=True):
            if im[0] == xref:
                name = im[7]
                break
        if not name:
            return False
        esc = re.escape(name.encode("latin-1"))
        # six-number cm directly followed by the image's /Name Do
        pat = re.compile(rb'(?:-?[\d.]+\s+){6}cm\s*/' + esc + rb'\s+Do\b')
        for sx in page.get_contents():
            raw = self.doc.xref_stream(sx)
            new, n = pat.subn(b'', raw)
            if n >= 1:
                self.doc.update_stream(sx, new)
                # This page's content changed (image placement gone). Drop its
                # cached pixmap so a reload can't show the still-present image.
                self.invalidate_render_page(page_num)
                return True
        return False

    def page_content_snapshot(self, page_num: int) -> list | None:
        """The raw bytes of every content stream on this page, with their xrefs.

        Paired with restore_page_content() so an edit that only rewrites a content
        stream (remove_image_placement) can be put back byte for byte. Returns
        None when the page can't be read, which callers treat as "not undoable".
        """
        if not self.doc or page_num >= len(self.doc):
            return None
        try:
            return [(sx, self.doc.xref_stream(sx))
                    for sx in self.doc[page_num].get_contents()]
        except Exception as e:
            print(f"Content snapshot failed: {e}")
            return None

    def restore_page_content(self, page_num: int, snapshot) -> bool:
        """Put a page_content_snapshot() back, and drop the page's cached pixmap."""
        if not self.doc or not snapshot or page_num >= len(self.doc):
            return False
        try:
            for sx, raw in snapshot:
                self.doc.update_stream(sx, raw)
        except Exception as e:
            print(f"Content restore failed: {e}")
            return False
        # The page's content changed back, so the cached pixmap is stale.
        self.invalidate_render_page(page_num)
        return True

    def move_page(self, from_idx: int, to_idx: int):
        if self.doc:
            self.doc.move_page(from_idx, to_idx)
            self.invalidate_render_cache()   # page indices shifted

    def reorder(self, new_order: list):
        """Reorder pages so that new page i is the page currently at new_order[i].

        new_order must be a permutation of range(page_count). Annotations travel
        with their pages (verified: fitz keeps page contents on select()).
        """
        if self.doc and sorted(new_order) == list(range(len(self.doc))):
            self.doc.select(list(new_order))
            self.invalidate_render_cache()   # page indices changed

    def clone_with_annotations(self, dicts_by_page: dict):
        """Return a throwaway fitz.Document copy with the given markup baked in.

        Lets us render thumbnails that include unsaved annotations WITHOUT mutating
        the live document (which would double-render markup in the editor). Caller
        owns the returned doc and should close() it when done.
        """
        clone = fitz.open()
        try:
            if self.doc:
                clone.insert_pdf(self.doc)
                writer = PDFDocument()
                writer.doc = clone           # reuse write_annotations on the clone
                try:
                    for pn, dicts in dicts_by_page.items():
                        if dicts and 0 <= pn < len(clone):
                            writer.write_annotations(pn, dicts)
                finally:
                    writer.doc = None        # detach so it never closes the clone
        except Exception:
            clone.close()
            raise
        return clone

    def strip_dangling_toc(self) -> int:
        """Drop bookmarks whose target page no longer exists. Returns how many.

        KNOWN BUG 5 in docs/tabs-plan.md, and it is older than the page-move
        work. `fitz.Document.delete_page` renumbers the table of contents but
        leaves the DELETED page's own entry pointing at -1, and that survives a
        save, so the file we write carries a broken bookmark. Measured on a
        3-page file with one bookmark per page:

            after delete_page(1): [[1,'One',1], [1,'Two',-1], [1,'Three',2]]

        A page number of 0 or less is fitz's "no destination" marker (real pages
        are 1-based here), so both are dropped. Cheap enough to run after every
        delete, and it does nothing at all on a document with no bookmarks.
        """
        if not self.doc:
            return 0
        try:
            toc = self.doc.get_toc(simple=True)
        except Exception:
            return 0
        if not toc:
            return 0
        kept = [entry for entry in toc if len(entry) > 2 and entry[2] > 0]
        dropped = len(toc) - len(kept)
        if dropped:
            try:
                self.doc.set_toc(kept)
            except Exception:
                return 0
        return dropped

    def delete_page(self, page_num: int):
        if self.doc and 0 <= page_num < len(self.doc):
            self.doc.delete_page(page_num)
            self.strip_dangling_toc()
            self.invalidate_render_cache()   # pages after this one renumbered

    def delete_pages(self, page_nums: list) -> list:
        """Delete a whole selection of pages in one go.

        Indices are into the CURRENT document and may arrive in any order or
        with duplicates. Returns the ascending list actually deleted, which is
        what an undo needs to put them back at.
        """
        if not self.doc:
            return []
        rows = sorted({p for p in page_nums if 0 <= p < len(self.doc)})
        if not rows:
            return []
        for page_num in reversed(rows):   # descending, so the rest stay valid
            self.doc.delete_page(page_num)
        self.strip_dangling_toc()
        self.invalidate_render_cache()
        return rows

    def extract_pages(self, page_nums: list):
        """A standalone in-memory PDF holding copies of `page_nums`, ascending.

        This is the stash that makes a page delete undoable: take the copy
        first, then delete. Page content and annotations travel with the copy
        (insert_pdf keeps both). Document-level things a lone page cannot carry
        by itself, such as a link pointing at another page, do not, so an undone
        delete restores what you can see rather than a byte-identical page.
        Caller owns the returned document.
        """
        stash = fitz.open()
        if not self.doc:
            return stash
        for page_num in sorted({p for p in page_nums if 0 <= p < len(self.doc)}):
            stash.insert_pdf(self.doc, from_page=page_num, to_page=page_num)
        return stash

    def restore_pages(self, stash, positions: list):
        """Put stashed pages back at the indices they held before a delete.

        `positions` lines up with the stash's own page order (both ascending).
        Inserting lowest-first is what keeps the arithmetic trivial: everything
        below an insertion point is already back in place, so the next position
        is still correct with no adjustment.
        """
        if not self.doc or stash is None:
            return
        for k, at in enumerate(sorted(positions)):
            if k >= len(stash):
                break
            self.doc.insert_pdf(stash, from_page=k, to_page=k,
                                start_at=max(0, min(at, len(self.doc))))
        self.invalidate_render_cache()   # page set/indices changed

    # ------------------------------------------------------------------
    # Moving pages between two LIVE documents (phase 5 of docs/tabs-plan.md)
    # ------------------------------------------------------------------

    def transfer_pages_from(self, src: "PDFDocument", rows: list, at: int) -> int:
        """Copy `rows` out of another OPEN document and land them at `at`.

        The counterpart of `restore_pages` for a source that is a live document
        rather than a stash, and the whole engine half of dragging a page from
        one tab into another. A MOVE is this call followed by
        `src.delete_pages(rows)`; a copy is this call on its own.

        ONE insert_pdf PER ROW, deliberately. A multi-selection can be
        non-contiguous, and one call per page keeps the arithmetic trivial:
        everything already inserted sits below the next insertion point, so the
        k-th row goes to `at + k` with no adjustment.

        `src` MUST be a different Document object. PyMuPDF's insert_pdf refuses
        to read a document into itself, so a drop back into the document the
        pages came from is routed to the plain reorder path instead of here
        (see PagePanel's drop handling). Asserted rather than tolerated: a
        silent no-op would look like a page that vanished.

        What travels and what does not is measured in docs/tabs-plan.md ("What
        comes along with a page"). Annotations, links to the outside world,
        fonts, page size and rotation travel. Internal GOTO links pointing
        outside the copied range, layers, and unsaved rapid-pdf markup do not;
        the first two are reported by transfer_report() so the UI can say so,
        and markup is carried separately as JSON by the command.
        """
        if not self.doc or src is None or src.doc is None:
            return 0
        if src.doc is self.doc:
            raise ValueError("transfer_pages_from cannot read a document into itself")
        rows = sorted({int(r) for r in rows if 0 <= int(r) < src.page_count()})
        if not rows:
            return 0
        at = max(0, min(int(at), self.page_count()))
        for k, row in enumerate(rows):
            self.doc.insert_pdf(src.doc, from_page=row, to_page=row,
                                start_at=at + k, links=True, annots=True,
                                widgets=True)
        self.invalidate_render_cache()   # page set/indices changed
        return len(rows)

    def transfer_report(self, rows: list) -> dict:
        """What moving `rows` OUT of this document will quietly lose or rename.

        PyMuPDF reports none of this: it drops an internal link whose target is
        outside the copied range, flattens layers, and renames a colliding form
        field, all silently and with no exception. Read before the move, so the
        UI can say it once in the status bar rather than leaving the user to
        find out on the next save.

        Keys: `links` (internal GOTO links that will not survive), `layers`
        (True when the source has optional content groups at all), `widgets`
        (form fields on the moved pages, which are the ones a name collision
        can rename in the destination).
        """
        out = {"links": 0, "layers": False, "widgets": 0}
        if not self.doc:
            return out
        rows = sorted({int(r) for r in rows if 0 <= int(r) < self.page_count()})
        moving = set(rows)
        for row in rows:
            try:
                page = self.doc[row]
            except Exception:
                continue
            try:
                for link in page.get_links():
                    if link.get("kind") == fitz.LINK_GOTO and link.get("page") not in moving:
                        out["links"] += 1
            except Exception:
                pass
            try:
                out["widgets"] += sum(1 for _ in page.widgets())
            except Exception:
                pass
        try:
            out["layers"] = bool(self.doc.get_ocgs())
        except Exception:
            out["layers"] = False
        return out

    def note_pages_sent(self, count: int, to_name: str):
        """Record that `count` pages left here for `to_name` and are not saved."""
        self.transfers_sent.append((int(count), to_name))

    def note_pages_taken(self, count: int, from_name: str):
        """Record that `count` pages arrived here out of `from_name`, unsaved."""
        self.transfers_taken.append((int(count), from_name))

    def forget_last_transfer(self, sent: bool):
        """Undo's half of the ledger: drop the entry the redo just wrote."""
        ledger = self.transfers_sent if sent else self.transfers_taken
        if ledger:
            ledger.pop()

    def clear_transfer_ledger(self):
        self.transfers_sent = []
        self.transfers_taken = []

    def insert_pdf(self, src_path: str, from_page: int = 0,
                   to_page: int = -1, start_at: int = -1):
        if not self.doc:
            return
        src = fitz.open(src_path)
        self.doc.insert_pdf(src, from_page=from_page, to_page=to_page, start_at=start_at)
        src.close()
        self.invalidate_render_cache()   # page set/indices changed

    # ------------------------------------------------------------------
    # Editable annotation model (embedded JSON) — for save/reopen round-trip
    # ------------------------------------------------------------------

    def _model_embed_names(self) -> list[str]:
        """Every embedded entry that holds a rapid-pdf model.

        On a malformed/garbage-collected name tree, embfile_add can append a digit
        on a name collision (e.g. 'rapid_pdf_model.json2'), leaving a stale second
        copy that embfile_del(MODEL_EMBED_NAME) never removes. Matching the base
        name as a PREFIX catches every such copy so writes can purge them all and
        reads can ignore the stale ones.
        """
        if not self.doc:
            return []
        try:
            return [n for n in self.doc.embfile_names()
                    if n == MODEL_EMBED_NAME or n.startswith(MODEL_EMBED_NAME)]
        except Exception:
            return []

    def write_annotation_model(self, model: dict):
        """Embed the editable annotation model as a JSON file inside the PDF.

        Replaces any previous copy. Stored at the document (catalog) level so it
        survives page reorder/delete and a deflate+garbage save.
        """
        if not self.doc:
            return
        try:
            data = json.dumps(model).encode("utf-8")
            # Purge EVERY previous copy, not just the exact base name. A prior
            # save could have left a suffixed duplicate ('…json2'); if even one
            # stale copy survived, read_annotation_model could pick it and silently
            # restore an OLD set of annotations (e.g. only pages 0-1), so newer
            # pages' markup would vanish on reopen. (embfile_upd is unreliable for
            # raw bytes in this PyMuPDF build, so delete + add.)
            for name in self._model_embed_names():
                try:
                    self.doc.embfile_del(name)
                except Exception:
                    pass
            self.doc.embfile_add(MODEL_EMBED_NAME, data)
        except Exception as e:
            print(f"Embed model error: {e}")

    def read_annotation_model(self) -> dict | None:
        """Return the embedded editable annotation model, or None if absent.

        If the file carries more than one copy (a stale duplicate from an older
        save), pick the richest — the one describing the most annotations — so a
        leftover earlier copy can never override the latest saved markup.
        """
        if not self.doc:
            return None
        best, best_count = None, -1
        for name in self._model_embed_names():
            try:
                data = self.doc.embfile_get(name)
                model = json.loads(bytes(data).decode("utf-8"))
            except Exception as e:
                print(f"Read model error ({name}): {e}")
                continue
            count = sum(len(v) for v in model.get("pages", {}).values())
            if count > best_count:
                best, best_count = model, count
        return best

    def delete_tagged_annotations(self, page_num: int):
        """Strip rapid-pdf's baked annotations from a page.

        Used on open so reconstructed editable items don't double-render on top of
        the markup that was baked into the file on the previous save.
        """
        if not self.doc or page_num >= len(self.doc):
            return
        page = self.doc[page_num]
        for a in list(page.annots()):
            if a.info.get("title") == RAPID_PDF_TAG:
                page.delete_annot(a)
        # Baked markup just stripped from this page → its cached render is stale.
        self.invalidate_render_page(page_num)

    def write_annotations(self, page_num: int, annotations: list):
        """Replace all rapid-pdf-tagged annotations on this page with the given list.

        Annotation dicts carry fitz_rects in the page's visible coordinate space
        (matching the canvas render). For rotated pages, fitz annotation APIs expect
        PDF user space coords, so we apply the page's derotation matrix to convert.
        """
        if not self.doc or page_num >= len(self.doc):
            return
        page = self.doc[page_num]

        # For rotated pages, annotation rects/points are in visible (rendered) space
        # but fitz expects native PDF user space. Derotation converts between the two.
        derot = page.derotation_matrix if page.rotation != 0 else None

        # Page content is about to change (markup rewritten) → drop its cache.
        self.invalidate_render_page(page_num)

        # Remove only our tagged annotations
        to_delete = [a for a in page.annots() if a.info.get("title") == RAPID_PDF_TAG]
        for a in to_delete:
            page.delete_annot(a)

        for ann in annotations:
            ann_type = ann.get("type")
            rect = ann.get("fitz_rect")
            color = ann.get("color")
            opacity = ann.get("opacity", 1.0)

            # Convert from visible space to PDF user space for rotated pages.
            if rect is not None and derot is not None:
                rect = fitz.Rect(rect) * derot

            # Normalize the rect so matrix multiplication can't produce an inverted
            # (negative-width/height) rect that crashes PyMuPDF's C layer.
            if rect is not None:
                rect = fitz.Rect(rect).normalize()

            try:
                if ann_type == "highlight":
                    if rect is None or rect.is_empty or rect.is_infinite:
                        print(f"Annotation write skipped (highlight): degenerate rect {rect}")
                        continue
                    annot = page.add_rect_annot(rect)
                    fill = color if color else (1.0, 1.0, 0.0)
                    annot.set_colors(fill=fill, stroke=fill)
                    annot.set_opacity(opacity)
                    annot.set_border(width=0)
                    info = annot.info
                    info["title"] = RAPID_PDF_TAG
                    annot.set_info(info)
                    annot.update()

                elif ann_type == "rect":
                    if rect is None or rect.is_empty or rect.is_infinite:
                        print(f"Annotation write skipped (rect): degenerate rect {rect}")
                        continue
                    annot = page.add_rect_annot(rect)
                    stroke = ann.get("stroke_color") or color or (0.0, 0.0, 0.0)
                    fill = ann.get("fill_color")
                    colors = {"stroke": stroke}
                    if fill:
                        colors["fill"] = fill
                    annot.set_colors(colors)
                    annot.set_opacity(opacity)
                    annot.set_border(width=ann.get("line_width", 2))
                    info = annot.info
                    info["title"] = RAPID_PDF_TAG
                    if ann.get("text"):
                        info["content"] = ann["text"]
                    annot.set_info(info)
                    annot.update()

                elif ann_type == "line":
                    p1 = ann.get("p1")
                    p2 = ann.get("p2")
                    if p1 and p2:
                        if derot is not None:
                            p1 = fitz.Point(p1) * derot
                            p2 = fitz.Point(p2) * derot
                        annot = page.add_line_annot(p1, p2)
                        stroke = ann.get("color") or (0.0, 0.0, 0.0)
                        annot.set_colors(stroke=stroke)
                        annot.set_opacity(opacity)
                        annot.set_border(width=ann.get("line_width", 2))
                        info = annot.info
                        info["title"] = RAPID_PDF_TAG
                        annot.set_info(info)
                        annot.update()

                elif ann_type == "text":
                    text = ann.get("text", "")
                    font_size = ann.get("font_size", 12)
                    color = ann.get("color", (0.0, 0.0, 0.0))
                    if rect and text:
                        if rect.is_empty or rect.is_infinite:
                            print(f"Annotation write skipped (text): degenerate rect {rect}")
                            continue
                        annot = page.add_freetext_annot(
                            rect, text,
                            fontsize=font_size,
                            text_color=color,
                            fill_color=None,
                        )
                        info = annot.info
                        info["title"] = RAPID_PDF_TAG
                        annot.set_info(info)
                        annot.update()

                elif ann_type == "image":
                    image_bytes = ann.get("image_bytes")
                    if not image_bytes:
                        print("Annotation write skipped (image): no image_bytes")
                        continue
                    if rect is None or rect.is_empty or rect.is_infinite:
                        print(f"Annotation write skipped (image): degenerate rect {rect}")
                        continue
                    if rect.width < 1 or rect.height < 1:
                        print(f"Annotation write skipped (image): rect too small {rect}")
                        continue
                    # rotate=page.rotation counteracts the page's own rotation so
                    # the image content appears upright in the rendered view. Without
                    # this, a page rotated 90° would bake the image rotated 90° as
                    # well, making it appear wrong after the save/auto-reload cycle.
                    # The rect was already derotated above for rotated pages.
                    page.insert_image(rect, stream=image_bytes,
                                      rotate=page.rotation)

            except Exception as e:
                print(f"Annotation write error ({ann_type}): {e}")
