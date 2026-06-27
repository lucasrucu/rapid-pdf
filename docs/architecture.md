# rapid-pdf: architecture

How the app is put together: the modules, how they talk, the coordinate system, the save lifecycle, and the two features that took the most engineering (the editable round-trip and the embedded-image lift). For the rendering and caching detail, see [performance.md](performance.md). For the deep image-handling history, see [NATIVE-CONTENT-OBJECTS.md](NATIVE-CONTENT-OBJECTS.md) and [VISIO-IMAGE-DIAGNOSIS.md](VISIO-IMAGE-DIAGNOSIS.md).

## Layers at a glance

```
main.py                 entry point
  └─ ui/main_window.py  the shell: menus, tabs, lifecycle, dirty-state
       ├─ ui/page_panel.py   left thumbnail strip (Editor)
       ├─ ui/canvas.py       the annotation surface (Editor)
       ├─ ui/toolbar.py      tools + contextual style controls (Editor)
       └─ ui/organizer.py    page reorder/delete/add grid (Organizer tab)
  core/pdf_document.py   PyMuPDF wrapper used by every UI piece
```

The UI never touches `fitz` directly for document mutations: it goes through `core/PDFDocument`, which owns the live `fitz.Document`, the render cache, the save lifecycle, and the embedded annotation model. The canvas does read `self._doc.doc` for read-only page geometry (image rects, rotation), but all structural and content changes route through `PDFDocument` methods so cache invalidation stays centralized.

## Module responsibilities

### `core/pdf_document.py`: `PDFDocument`

The single wrapper over PyMuPDF. Responsibilities:

- **Open / close / save.** `save()` is integrity-first (atomic in-place replace with a `.bak` salvage path on failure). See the Save lifecycle section below.
- **Rendering** via `render_page`, `render_thumbnail`, and the LRU `render_page_cached` (keyed by page + zoom, bounded to six pages). Detail in [performance.md](performance.md).
- **Structural ops** `move_page`, `reorder`, `delete_page`, `insert_pdf`, each of which invalidates the render cache because page indices shift.
- **The editable annotation model.** `write_annotation_model` / `read_annotation_model` embed and recover a JSON description of the markup so a saved file reopens editable.
- **Writing markup** `write_annotations` converts the canvas's annotation dicts into real PDF annotation objects (tagged `rapid-pdf` so they can be found and stripped again).
- **The image-lift primitive** `remove_image_placement`, which deletes one image's content-stream placement without leaving a hole.
- **`clone_with_annotations`** produces a throwaway copy with unsaved markup baked in, used to render thumbnails without mutating the live document.

### `ui/canvas.py`: `PDFCanvas` and the annotation items

A `QGraphicsView` over a `QGraphicsScene`. This is the largest module. It holds:

- **The background page item** (a `QGraphicsPixmapItem` at z = -1) carrying the rasterized current page.
- **Annotation item classes**, all mixing in `AnnotationBase`:
  - `AnnotationItem` (base for rect-shaped items) → `HighlightItem`, `RectAnnotationItem`, `ImageAnnotationItem`.
  - `LineAnnotationItem` for straight lines.
  - `TextAnnotationItem` for free-floating text labels.
  Each item knows how to draw itself and its selection handles, serialize to an annotation dict (`to_annotation_dict`), and `clone()` itself for copy/paste and Ctrl+drag duplication.
- **The undo stack.** A `QUndoStack` with one `QUndoCommand` subclass per edit: `AddItemsCommand`, `RemoveItemsCommand`, `MoveCommand`, `NudgeCommand` (consecutive nudges merge into one), `ResizeCommand`, `StyleCommand`. The shared `_Command` base applies the edit live at construction time, so the first `redo()` fired by `push()` is a no-op. Structural page ops (reorder, delete) clear the stack, because item-level undo can't safely replay against renumbered pages.
- **Per-page annotation lists** (`_page_annotations`), keyed by page index. Only the current page's items are visible; the rest are hidden but retained, so flipping back to a page restores its markup instantly.
- **Interaction state** for drawing, dragging, marquee selection, resizing, duplication, and the copy-confirmation flash. Mouse events live at the bottom of the file.

### `ui/main_window.py`: `MainWindow`

The shell that wires everything together and owns the document lifecycle:

- Builds the two tabs and connects toolbar signals to canvas slots.
- Owns **open / combine** (multiple files append in filename order), **save / save-as**, **close**, and **quit**, including the unsaved-changes prompt and the dirty/untitled state shown in the window title.
- Coordinates **the flush → save → strip** sequence (see Save lifecycle).
- Keeps the page panel and organizer thumbnails in sync by rebuilding them from a markup-baked clone after edits.

### `ui/organizer.py`: `PageOrganizer`

A thumbnail grid for page management. Reordering uses Qt's native `InternalMove` drag-and-drop; after a drop it reads the new order back from each cell's stored page id and emits a permutation to the main window, which applies it to the live document and the canvas. It also handles delete-selected and add-pages-from-PDF. Thumbnails render lazily (placeholders first, real renders only for cells near the viewport).

### `ui/page_panel.py`: `PagePanel`

The narrow left strip of page thumbnails in the Editor. Clicking a thumbnail switches the canvas page. Like the organizer, it renders lazily and pulls thumbnails from a markup-baked clone so they always match the page plus unsaved overlays.

### `ui/toolbar.py`: `ToolBar` and `ColorToolButton`

The right-hand tool rail. Tool buttons (Select / Rectangle / Line / Text) plus contextual style controls that show only when relevant to the current tool or selection: an Office-style `ColorToolButton` for fill and line color (with a preset grid, recent colors, and line weights), a text color and size section, and an opacity preset dropdown. Selecting an object folds its style back into the controls without firing signals.

## Coordinate system

There are three spaces in play, and getting between them correctly is the source of most of the image-handling comments in the code.

- **Scene space** is the canvas's pixel space: the rendered page at the current zoom. Annotation items live here.
- **PDF user space** is the page's native, unrotated coordinate system that `fitz` annotation APIs expect.
- **Visible (post-rotation) space** is what you actually see for a rotated page.

The mapping rules the code relies on:

- `page.bound()` gives visible (post-rotation) dimensions; `page.rect` does not. Page sizing and thumbnail aspect ratios use `bound()`.
- When writing annotations on a **rotated** page, rects and points are converted from visible space to user space with `page.derotation_matrix` (see `write_annotations`). On unrotated pages no conversion is needed.
- For hit-testing and lifting **embedded images**, `page.get_image_rects()` returns rects in unrotated user space, and `page.rotation_matrix` maps unrotated user space to the rendered image for every page (rotated or not). The code deliberately uses `rotation_matrix`, not `transformation_matrix`, because the latter double-flips rot = 0 pages and puts the hit region on the wrong side. This was verified against ground truth (pixels that actually change when an image is removed).

## Save lifecycle

Saving is a three-step dance, because the canvas renders markup as live Qt items while the saved file needs them baked into the page content:

1. **Flush** (`_flush_annotations`). Every page's canvas items are written as tagged PDF annotation objects via `write_annotations`, and the editable JSON model is embedded via `write_annotation_model`.
2. **Save** (`PDFDocument.save`). An in-place save writes to a `NamedTemporaryFile` in the same directory and swaps it in atomically with `os.replace`. PyMuPDF can't write over its own open file, so the live handle is closed and set to `None` before the swap; any failure leaves `self.doc is None` (recoverable) rather than pointing at a closed document, and salvages the new file to `.bak`. A merged/untitled doc routes to Save-As. Both use `garbage=4, deflate=True`.
3. **Strip** (`_strip_baked_annotations`). After the save, the baked annotations are removed from the live document again. If they stayed, the next page render would include them in the background pixmap and every annotation would appear twice (the second copy an unselectable ghost). Images are handled separately: `drop_baked_image_items` drops their live overlays so a later save can't bake a second copy, while the baked image still shows from the content and stays re-liftable.

On open, the inverse runs: if the file carries an embedded model, the baked markup is stripped and the model is rebuilt into editable canvas items (`_load_saved_annotations`).

## Editable annotation model (the round-trip)

A normal "save annotations into a PDF" flattens markup into static objects you can't move again. rapid-pdf keeps markup editable across save and reopen by embedding a JSON description of every shape inside the PDF itself.

- On save, `export_annotation_model` (canvas) serializes each page's items to JSON, and `write_annotation_model` (document) embeds it as an attached file named `rapid_pdf_model.json` at the catalog level, so it survives page reorder, delete, and a `garbage=4` compaction.
- On open, `read_annotation_model` recovers it and `load_annotation_model` rebuilds the canvas items. If a malformed name tree left a stale duplicate copy (PyMuPDF can append a digit on a name collision), the reader picks the **richest** copy (the one describing the most annotations) so an older copy can never override the latest markup, and the writer purges every prefix-matching copy before re-adding.
- Images are intentionally excluded from the JSON model. They are baked into page content on save and stay editable on reopen through the embedded-image lift feature instead.

## Embedded-image lift pipeline

Lifting turns an image baked into the page into a movable, resizable object, the way a real editor moves it, without leaving a white hole.

1. **Detect.** On press over empty space, `_embedded_image_at` finds the smallest image under the cursor (so a small image on top of a full-page background is the one you grab). A near-full-page raster is skipped by design, because lifting a whole scanned page was the cause of an earlier "page turns into a flipped copy" bug.
2. **Crop the displayed pixels.** The lifted object's pixmap is cropped straight from the rendered page (via `rotation_matrix * zoom`), so it keeps the page's orientation. PDFs often place images with a flipped matrix; re-inserting the raw extracted bytes would look rotated, so cropping the render sidesteps the placement transform entirely. The cropped pixels are re-encoded as PNG so the displayed and saved forms stay identical.
3. **Remove the placement, not the pixels.** `remove_image_placement` deletes only the image's single `<a b c d e f> cm /Name Do` content-stream operator. That `cm` exists solely to place this image, so dropping it removes the image while leaving everything painted behind it intact. Visio and automation pages stamp each image on top of a full-page background raster; redacting the image's rect would blank the background underneath and leave a white hole, so the placement-removal approach is what makes a clean move possible. If the tight `cm /Name Do` pattern isn't found, the code falls back to pixel redaction.
4. **Re-render once and bookkeep.** The page background is re-rendered a single time (cached forward), and the lifted xref is dropped from the in-memory image list rather than triggering a full page rescan.

This is the most subtle part of the app. The full history and the alternatives that were rejected are documented in [NATIVE-CONTENT-OBJECTS.md](NATIVE-CONTENT-OBJECTS.md) and [VISIO-IMAGE-DIAGNOSIS.md](VISIO-IMAGE-DIAGNOSIS.md).

## Thumbnails without mutating the live document

Both the page panel and the organizer need to show each page plus its unsaved overlays, but the live document must not be mutated (that would double-render markup in the editor). So `clone_with_annotations(dicts_by_page)` builds a throwaway `fitz.Document` copy with the current overlays baked in, and the panels render thumbnails from that clone. The clone is its own `PDFDocument` with its own cache, so it never disturbs the live document's render cache. Cost detail is in [performance.md](performance.md).
