import fitz
import json
import os
import re
import tempfile
from PySide6.QtGui import QPixmap, QImage


RAPID_PDF_TAG = "rapid-pdf"
# Name of the embedded file that carries the editable annotation model, so a
# document saved by rapid-pdf reopens with its objects still movable/editable.
MODEL_EMBED_NAME = "rapid_pdf_model.json"


class PDFDocument:
    def __init__(self):
        self.doc: fitz.Document | None = None
        self.path: str | None = None

    def open(self, path: str) -> bool:
        try:
            if self.doc:
                self.doc.close()
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

    def page_count(self) -> int:
        return len(self.doc) if self.doc else 0

    def get_page_size(self, page_num: int) -> tuple[float, float]:
        if not self.doc or page_num >= len(self.doc):
            return (0.0, 0.0)
        # page.bound() gives the visible dimensions after rotation; page.rect does not.
        r = self.doc[page_num].bound()
        return (r.width, r.height)

    def render_page(self, page_num: int, zoom: float = 1.5) -> QPixmap:
        if not self.doc or page_num >= len(self.doc):
            return QPixmap()
        page = self.doc[page_num]
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = QImage(bytes(pix.samples), pix.width, pix.height, pix.stride,
                     QImage.Format.Format_RGB888)
        return QPixmap.fromImage(img)

    def render_thumbnail(self, page_num: int, max_width: int = 110) -> QPixmap:
        if not self.doc or page_num >= len(self.doc):
            return QPixmap()
        page = self.doc[page_num]
        # page.bound() gives the visible (post-rotation) dimensions; page.rect does not.
        zoom = max_width / page.bound().width
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = QImage(bytes(pix.samples), pix.width, pix.height, pix.stride,
                     QImage.Format.Format_RGB888)
        return QPixmap.fromImage(img)

    def save(self, path: str | None = None) -> bool:
        if not self.doc or not (self.path or path):
            return False
        target = path or self.path
        # An untitled (merged) doc has no current path → it's never an in-place save.
        is_same = self.path is not None and os.path.abspath(target) == os.path.abspath(self.path)
        try:
            if is_same:
                dir_path = os.path.dirname(os.path.abspath(target))
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir=dir_path) as tf:
                    tmp_path = tf.name
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
                    # Couldn't swap the new file in; salvage it so no work is lost.
                    bak = target + ".bak"
                    try:
                        os.replace(tmp_path, bak)
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
            return False

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
                return True
        return False

    def move_page(self, from_idx: int, to_idx: int):
        if self.doc:
            self.doc.move_page(from_idx, to_idx)

    def reorder(self, new_order: list):
        """Reorder pages so that new page i is the page currently at new_order[i].

        new_order must be a permutation of range(page_count). Annotations travel
        with their pages (verified: fitz keeps page contents on select()).
        """
        if self.doc and sorted(new_order) == list(range(len(self.doc))):
            self.doc.select(list(new_order))

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

    def insert_pdf(self, src_path: str, from_page: int = 0,
                   to_page: int = -1, start_at: int = -1):
        if not self.doc:
            return
        src = fitz.open(src_path)
        self.doc.insert_pdf(src, from_page=from_page, to_page=to_page, start_at=start_at)
        src.close()

    # ------------------------------------------------------------------
    # Editable annotation model (embedded JSON) — for save/reopen round-trip
    # ------------------------------------------------------------------

    def write_annotation_model(self, model: dict):
        """Embed the editable annotation model as a JSON file inside the PDF.

        Replaces any previous copy. Stored at the document (catalog) level so it
        survives page reorder/delete and a deflate+garbage save.
        """
        if not self.doc:
            return
        try:
            data = json.dumps(model).encode("utf-8")
            # Replace any previous copy (embfile_upd is unreliable for raw bytes
            # in this PyMuPDF build, so delete + add).
            if MODEL_EMBED_NAME in set(self.doc.embfile_names()):
                self.doc.embfile_del(MODEL_EMBED_NAME)
            self.doc.embfile_add(MODEL_EMBED_NAME, data)
        except Exception as e:
            print(f"Embed model error: {e}")

    def read_annotation_model(self) -> dict | None:
        """Return the embedded editable annotation model, or None if absent."""
        if not self.doc:
            return None
        try:
            if MODEL_EMBED_NAME not in set(self.doc.embfile_names()):
                return None
            data = self.doc.embfile_get(MODEL_EMBED_NAME)
            return json.loads(bytes(data).decode("utf-8"))
        except Exception as e:
            print(f"Read model error: {e}")
            return None

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

            try:
                if ann_type == "highlight":
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
                    if rect and image_bytes:
                        page.insert_image(rect, stream=image_bytes)
                        # Note: rect was already derotated above for rotated pages.

            except Exception as e:
                print(f"Annotation write error ({ann_type}): {e}")
