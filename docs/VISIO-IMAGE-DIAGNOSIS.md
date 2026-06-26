# Why rapid-pdf can't move images from Visio/automation PDFs

Diagnosed 2026-06-26 against a real file: `NoE Drawings COMM-0311 (last 3).pdf` (Visio → print-to-PDF, 3 pages, with images added by automation). This is Lucas's actual painpoint: Adobe can move these images, rapid-pdf can't.

## What the file actually contains (measured, not assumed)

| Page | Size | Rotation | Raster images | Vector paths |
|---|---|---|---|---|
| 0 | 2448×1584 | (large drawing) | 3 (1 full-page bg + 2 small) | ~4 |
| 1 | 792×612 *(displayed)* | **90°** (mediabox 612×792) | 3 small | **~9,190** (26k lines) |
| 2 | 792×612 *(displayed)* | **90°** | 3 small | ~9,113 |

The images are drawn **directly in the page content stream** (`/Im0 Do`, `/Im1 Do`, `/Im2 Do`) — not nested in Form XObjects, not annotations.

## The two findings that matter

**1. Detection already works.** rapid-pdf's `get_images()` + `get_image_rects()` finds every image with its correct rectangle. So "rapid-pdf doesn't recognize the image" is **not** a detection failure.

**2. The lift (removal) is what fails.** rapid-pdf moves an image by *redacting* the original and dropping a draggable copy in its place. On these files, `apply_redactions(images=PDF_REDACT_IMAGE_REMOVE)` **removes nothing** — proven by redacting the image's exact rect, an inflated rect, the rect in every rotation space, and **the entire page**. All left the image in place. So the lift produces (at best) a floating duplicate over an original that never goes away → the image is effectively un-moveable.

**Root cause:** two things compound —
- These pages are **rotated 90°**. `get_image_rects` returns rects in *unrotated* (mediabox) space, which fall outside the *displayed* page box. rapid-pdf's lift and redaction don't fully reconcile the two spaces (the same mismatch even crashed a clipped render during diagnosis).
- More fundamentally, **redaction-based removal doesn't work on these content-stream images at all**, even ignoring rotation. The lift's whole "redact the original" strategy is the wrong tool here.

This is exactly why **Adobe** can move them (it does true content-stream editing — it rewrites the image's placement matrix) while **Edge and rapid-pdf** can only move their *own* objects: neither does content-stream editing of foreign objects.

## The fix: move the image by editing its placement matrix (the Adobe way)

Each image is positioned by a `cm` (matrix) operator right before its `Do`:
```
q  <a b c d e f> cm  /Im0 Do  Q
```
`e, f` are the translation. **Moving the image = rewriting that `cm`**, not redacting it. This is the content-stream approach from [rapid-pdf-movable-objects.md](../../ai-assistant/research/rapid-pdf-movable-objects.md) and the foundation of the chosen "native content objects" direction — and it works identically for any app's objects, which is the cross-app behavior Lucas wants.

The Do/cm operators are confirmed present in the file, so the approach is structurally viable. The real work is doing it **robustly**:
- Consolidate the page's content stream(s) (`clean_contents`) and locate the `q…cm…/ImN Do…Q` block for the target image (map resource name → xref).
- Rewrite the `cm` translation to the new position; handle the **90° page rotation** (convert the on-screen drag delta into the unrotated content-stream space).
- Handle nested `q/Q` graphics-state, multiple images, and shared XObjects without corrupting the stream.

## Plan

- **Phase A — proof:** headlessly move one image in this exact file via `cm` edit; confirm by re-reading its rect and rendering. Pin down the rotation transform. *(Probed 2026-06-26: the `cm` directly before `/Im1 Do` is `[269 0 0 253 2154 205]` but the image renders at `(457,652)` — so an **enclosing `q…cm…Q` transform** combines with it. The move must resolve the full CTM stack, not edit a single `cm`. This is the crux of Phase A.)*
- **Phase B — integrate:** replace the image lift's redaction step with `cm`-edit move-in-place for content-stream images; keep the rotation-correct mapping. Validate in rapid-pdf on this file + in Adobe.
- **Phase C — generalize:** extend the same content-stream handling to vector objects (`get_drawings`) so Visio shapes become moveable too — the full "native content" goal.

## Status

Diagnosis complete and file-verified. Fix is identified and structurally confirmed but **not yet built** — it's real content-stream engineering, not a one-line change, and it must be validated against this file in both rapid-pdf and Adobe. This sample file is the test gate.
