# Native content objects: migration plan

**Decision (2026-06-26):** rapid-pdf should write the objects you create as **native page content** (content-stream objects) instead of PDF **annotations**. Goal: objects are editable in Adobe's *Edit PDF* mode and movable across apps, not stuck in the comment layer. Bonus: rapid-pdf gains recognition of objects *other apps* created (Adobe shapes, images, text) for free.

## Why the change (the diagnosis behind it)

Today rapid-pdf saves shapes/lines/highlights/text as **annotations** (`add_rect_annot`, `add_line_annot`, `add_freetext_annot`). Adobe treats annotations as **comments**:
- Adobe *All tools / Comment* view → selectable ✅
- Adobe *Edit PDF* view → invisible ❌ (Edit PDF only touches real page content)

Images are the exception. They're already written to the content stream (`insert_image`), which is why they behave differently from the markup. Making everything content-stream removes that split.

## Feasibility: PROVEN headless (2026-06-26)

| Step | Result |
|---|---|
| **Draw** a rect natively (`page.draw_rect`) | content-stream vector ✓ |
| **Detect** it back (`page.get_drawings()`) | exact bbox `[60,60,180,140]` + color `(1,0,0)` ✓ |
| **Survive** save + reopen | yes, Adobe would see native editable content ✓ |
| **Remove** one (`apply_redactions(graphics=PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)`) | 1 rect → 0, clean ✓ |
| **Text** (`insert_textbox` → `get_text` → `PDF_REDACT_TEXT_REMOVE`) | round-trips + removable ✓ |

Conclusion: the **draw → detect → redact → reconstruct** loop works, and it works on *any* app's content-stream objects.

## Target architecture: unify everything under the existing "lift" pattern

rapid-pdf already lifts images (`_compute_embedded_images` → `_lift_embedded_image`). Extend that same pattern to every object type:

- **Write:** `draw_rect` / `draw_line` / `insert_textbox` / `insert_image` instead of `add_*_annot`.
- **Detect** (on open + on hover): `get_drawings()` (vectors), `get_text("dict")` (text), `get_images()` (images) → one list of liftable objects, regardless of origin.
- **Lift** (to move/edit): redact the object's tight rect (`REMOVE_IF_TOUCHED` for vectors, `TEXT_REMOVE` for text, `IMAGE_REMOVE` for images) + reconstruct as a Qt item, same as images today.
- **Re-bake** on save: redraw at the new position.
- **Embedded JSON model** stays, but now for *high-fidelity round-trip of rapid-pdf's own objects* (exact dash/opacity/fill). Foreign objects reconstruct best-effort from `get_drawings`.

## Hard parts / risks (why we phase it, not big-bang)

1. **Strip symmetry.** Annotations are stripped by their `"rapid-pdf"` tag; content-stream objects can't be tagged. Avoiding double-render on reopen must use redaction-by-rect or full reconstruct-from-content, which is lossier than `delete_annot`. (Same root issue as the image double-bake bug.)
2. **Fidelity.** `get_drawings` gives geometry; reconstructing exact style (dash, opacity, even-odd fill) needs care. Our own objects stay exact via the model; foreign ones are best-effort.
3. **Redaction overlap.** `REMOVE_IF_TOUCHED` removes anything touching the rect, so overlapping objects can get clipped. Needs tight per-object rects + guards.
4. **Text is hardest.** Native editable text means font embedding/measurement; may keep text as a special case or a styled textbox.
5. **It replaces the save lifecycle.** `write_annotations` + the canvas serialization get rewritten. High test burden; needs **Adobe + rapid-pdf** verification, which only Lucas can do hands-on.

## Phased plan

- **Phase 0, Feasibility.** ✅ Done (this doc).
- **Phase 1, Rectangles, end to end.** Write rects as `draw_rect`; detect via `get_drawings` on open; lift via redaction. Validate in **Adobe Edit PDF** (selectable/movable) + rapid-pdf round-trip. **Go/no-go gate** before touching anything else.
- **Phase 2, Lines + highlights.** Same pattern.
- **Phase 3, Images.** Already content-stream; fold in the double-bake fix (flatten-on-save + reload) here.
- **Phase 4, Text.** Decide native textbox vs keep-as-annotation after Phases 1-3.
- **Phase 5, Cross-app ingest.** Extend hover/lift to foreign `get_drawings`/`get_text` objects → Adobe-created shapes & text become movable in rapid-pdf.

## Recommendation

Build **Phase 1 (rectangles)** as a vertical slice on this branch, you test it in Adobe + rapid-pdf, and only then commit to converting the rest. Don't rewrite the whole save lifecycle before one object type is proven across both apps.
