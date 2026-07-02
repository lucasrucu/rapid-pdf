import fitz
import json
import os
import re
import tempfile
from collections import OrderedDict
from PySide6.QtGui import QPixmap, QImage


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


class PDFDocument:
    def __init__(self):
        self.doc: fitz.Document | None = None
        self.path: str | None = None
        # LRU cache of rendered page pixmaps keyed by (page_num, zoom_key).
        # A cache hit makes a repeated render_page of the same page+zoom free
        # (the lift re-render, reload-after-strip, organizer/page round-trips).
        # MUST be invalidated whenever a page's content changes — a stale pixmap
        # showing a lifted-out image still present, or old baked markup, is a
        # correctness regression worse than slowness. See invalidate_* below and
        # the call sites in canvas/main_window.
        self._render_cache: "OrderedDict[tuple, QPixmap]" = OrderedDict()

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

    def open(self, path: str) -> bool:
        try:
            if self.doc:
                self.doc.close()
            self.invalidate_render_cache()   # new document — no stale pixmaps
            self.doc = fitz.open(path)
            self.path = path
            return True
        except Exception as e:
            print(f"Open error: {e}")
            return False

    def close(self):
        if self.doc:
            self.doc.close()
        self.doc = None
        self.path = None
        self.invalidate_render_cache()

    def page_count(self) -> int:
        return len(self.doc) if self.doc else 0

    def get_page_size(self, page_num: int) -> tuple[float, float]:
        if not self.doc or page_num >= len(self.doc):
            return (0.0, 0.0)
        # page.bound() gives the visible dimensions after rotation; page.rect does not.
        r = self.doc[page_num].bound()
        return (r.width, r.height)

    @staticmethod
    def _render_page_at_zoom(page, zoom: float) -> QPixmap:
        """Rasterise a fitz page at the given uniform zoom into an opaque QPixmap.

        Shared by render_page (fixed zoom) and render_thumbnail (zoom derived
        from a target width) so the fitz→QImage→QPixmap conversion lives once.
        """
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = QImage(bytes(pix.samples), pix.width, pix.height, pix.stride,
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
        if not self.doc or not (self.path or path):
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
                    except Exception as bak_err:
                        print(f"Save recovery error: {bak_err}")
                        # Last resort: try reopening the original (unchanged on disk).
                        try:
                            self.doc = fitz.open(target)
                        except Exception:
                            pass
                    raise RuntimeError(
                        f"Could not overwrite the original file.\n"
                        f"Your work was saved to: {bak}"
                    ) from move_err
                # Reopen the freshly written file as the live document.
                self.doc = fitz.open(target)
            else:
                self.doc.save(target, garbage=4, deflate=True)
            # Adopt the target as the canonical path so later saves write in place.
            self.path = target
            return True
        except Exception as e:
            print(f"Save error: {e}")
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
        invisible, searchable text layer, via PyMuPDF/mupdf's built-in OCR
        backend — no separate Tesseract install or tessdata files are
        required for English at runtime (confirmed empirically on this
        PyMuPDF build: page.get_pixmap(...).pdfocr_tobytes(tessdata=None)
        finds its own bundled language data).

        Only meant to be called on pages that fail page_has_text() (i.e.
        scanned/image-only pages) — this rasterizes the page, so running it
        on a page that already has real vector text/graphics would destroy
        that content, not just add a text layer alongside it.

        Note: fitz.Page.get_textpage_ocr() alone does NOT persist a text
        layer into the saved file — it only returns an in-memory TextPage
        for immediate extraction. Producing bytes via Pixmap.pdfocr_tobytes()
        and splicing that in as the new page is what actually survives
        doc.save() and a later reopen (verified by testing).

        IMPORTANT dependency note: pdfocr_tobytes(tessdata=None) makes
        PyMuPDF go hunting for an INSTALLED Tesseract-OCR (TESSDATA_PREFIX
        env var, `tesseract` on PATH, `where tesseract`). PyMuPDF does not
        bundle language data, and neither does our installer. On a machine
        without Tesseract this raises RuntimeError("No tessdata specified
        and Tesseract is not installed"). Callers must surface that to the
        user instead of swallowing it.

        Returns True on success; raises on OCR failure so the caller can
        report the real reason (the old behavior of returning False buried
        the missing-Tesseract error).
        """
        if not self.doc or page_num >= len(self.doc):
            return False
        page = self.doc[page_num]
        pix = page.get_pixmap(dpi=dpi)
        ocr_bytes = pix.pdfocr_tobytes(compress=True, language=language, tessdata=None)
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

    def delete_page(self, page_num: int):
        if self.doc and 0 <= page_num < len(self.doc):
            self.doc.delete_page(page_num)
            self.invalidate_render_cache()   # pages after this one renumbered

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
