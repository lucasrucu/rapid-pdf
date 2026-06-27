# Moving images from Visio/automation PDFs — diagnosis & fix

Diagnosed and fixed 2026-06-26 against a real file: `NoE Drawings COMM-0311 (last 3).pdf` (Visio → print-to-PDF, 3 pages rotated 90°, with images added by automation). Painpoint: Adobe can move these images, rapid-pdf couldn't.

## What's in the file (measured)

Images are drawn **directly in the page content stream** (`/Im0 Do` … on each page) — not annotations, not nested in Form XObjects. Pages 1–2 are rotated 90° and carry ~9,000 vector paths each (the Visio drawing). Each page also has 3 small raster images (logos/stamps).

## The real cause (after correcting a wrong first pass)

> **Correction:** an earlier pass concluded "redaction can't remove these images, so we need content-stream surgery." That was a **test artifact** — `get_image_rects` was read from a *stale page object* that wasn't re-fetched after `apply_redactions`. Re-checking with `get_image_info` (which reflects actual rendering) and a re-fetched page shows redaction works fine.

Verified end-to-end with rapid-pdf's **real code** on the real file:
- **Detection works** — `_compute_embedded_images` finds all images with correct rects.
- **Lift works** — `_lift_embedded_image` redacts the original out (`still drawn → False`) and creates a movable copy.
- **Move + save round-trip works** — lift → move → save → reopen leaves the image at its new position, **count unchanged (no duplicate)**.

So the machinery was fine. **The only gap was interaction:** lifting an image only fired on a click-and-**drag** past a threshold; a plain click did nothing. You clicked, nothing happened, and reasonably concluded the app couldn't touch them. (Adobe can also move objects other apps made because it edits page content; Edge — like rapid-pdf before this — only moves its own objects.)

## The rotation crop bug (the visual breakage)

The first interaction fix made the lift *reachable*, but on these 90°-rotated pages it was visually broken: grabbing made much of the page look wrong and the cutout was the wrong pixels in the wrong place. Root cause, confirmed by rendering the result and **looking** at it:

`_lift_embedded_image` mapped the image's PDF rect to screen pixels with `page.transformation_matrix`. In this PyMuPDF build that matrix is **only a y-flip with the unrotated height — it carries no rotation**. So on rotated pages the crop/placement landed in the wrong region (it grabbed drawing lines instead of the image, at the wrong spot). `page.rotation_matrix` gives the correct PDF→displayed mapping there. Fix: pick `transformation_matrix` when `rotation == 0`, else `rotation_matrix`. Verified visually — the cutout is now the actual image (a legend box) at its true location, and the page stays intact.

## The fix (this branch)

0. **Rotation-correct crop/placement** (above) — the lifted image is the right pixels at the right place on rotated pages.
1. **Click-to-grab** (`canvas.py`, `mouseReleaseEvent`): a plain click on an embedded image now lifts it into a movable, selected object — reachable with a click, not just an obscure drag. The hand cursor on hover (already shipped) signals it's grabbable. The near-full-page guard still prevents lifting a whole-page scan. Shift+click preserves selection; double-click no longer opens the "text in shape" dialog over an image.
2. **Double-bake fix** (`drop_baked_image_items`, called after each save): images bake into the page content stream and can't be stripped like tagged annotations, so the live overlay was being re-baked on every save (1→2→3 copies). After a save, the overlay is dropped and the page re-rendered, so the baked image shows once and stays re-liftable. Verified: image count stays constant across repeated saves.

## Known trade-offs (intentional)

- A lift isn't on the undo stack (same as the pre-existing drag-lift). It's non-destructive — the image just becomes a movable copy — so this is acceptable; making it undoable would require reversing the content-stream redaction (risky surgery on rotated pages).
- The double-bake fix clears the undo stack on a save *only when an image was actually dropped* (a pasted-image Add command could otherwise redo a dropped overlay back in). Shape/text-only sessions keep their undo history.

## Validation (headless, against the real rotated file)

Detection, lift, click-to-grab, Shift+click (no lift, selection intact), double-click exclusion, and no-double-bake-across-saves all pass; `smoke_test.py` passes. GUI feel (the actual drag/click in the window) still needs hands-on testing — that's what this branch is for.
