# rapid-pdf rendering & performance

How rapid-pdf keeps a daily-use editor responsive on large engineering drawings
(A1 Visio/AutoCAD sheets). Rasterising one A1 page at the default zoom costs
~100-150ms, so the design rule is simple: **rasterise as little as possible, as
late as possible, and never rasterise the same thing twice.**


## What rasterises, and when

| Surface | Strategy | Why |
|---|---|---|
| **Main canvas page** | Eager (you're looking at it), but **debounced** and **cached** | The current page must be sharp; rapid page-flips coalesce so you don't render every page you scroll past. |
| **Embedded-image scan** (`_compute_embedded_images`) | Lazy, deferred off page load, computed on first interaction | Most page views never touch an image; the `get_images` + `get_image_rects` scan is only paid when you actually hover/grab an image. |
| **Left page panel thumbnails** | Lazy: placeholders first, real thumbnails rendered only for rows in/near the viewport | A 100-page doc shows instantly; off-screen thumbnails never render until scrolled toward. |
| **Organizer grid thumbnails** | Lazy, same placeholder-then-on-scroll strategy as the page panel | Was eager (rendered *every* page on open, ~3ms/page → ~300ms freeze on a big doc); now near-instant. |

## Page-pixmap cache (`PDFDocument._render_cache`)

The single biggest win. `render_page_cached(page_num, zoom)` is an LRU cache of
rendered `QPixmap`s keyed by `(page_num, round(zoom, 4))`, bounded to
`RENDER_CACHE_MAX = 6` pages. The canvas calls it instead of the raw
`render_page` in both `_load_page` and the post-lift re-render.

**What it buys.** A cache hit is effectively free (~0ms vs ~120ms for an A1
page). The hot patterns that repeatedly render the *same* page+zoom now hit the
cache:

- **Image lift.** After removing an image placement the page is re-rendered;
  any later reload of that page (drop-baked-items after save, a page-switch
  back) is a cache hit instead of another full raster.
- **Back/forth page navigation.** Flipping to a page you just left is instant.
- **Reload-after-strip** on open, and organizer round-trips.

**Bounding memory.** An A1 page at zoom 1.5 is ~3576×2526 px ≈ 36 MB of 32-bit
pixels. Six of those is the ceiling; least-recently-used pages are evicted. This
keeps memory sane on a 200-page set while still covering the realistic working
window.

### Cache invalidation: the correctness contract

A stale cached pixmap is *worse than slowness*: it could show a lifted-out image
still in place, or markup that was just stripped. So every content mutation in
`PDFDocument` invalidates the cache, surgically where possible:

| Mutation | Invalidation | Scope |
|---|---|---|
| `remove_image_placement` (lift) | `invalidate_render_page(page_num)` | one page |
| `delete_tagged_annotations` (strip on open) | `invalidate_render_page(page_num)` | one page |
| `write_annotations` (markup rewrite) | `invalidate_render_page(page_num)` | one page |
| `save` (bake + in-place reopen) | `invalidate_render_cache()` | whole doc |
| `reorder` / `move_page` / `delete_page` / `insert_pdf` | `invalidate_render_cache()` | whole doc (indices shift) |
| `open` / `close` | `invalidate_render_cache()` | whole doc |

`render_page_cached` returns the **same** `QPixmap` instance on a hit, so callers
treat it as read-only: the lift crops with `.copy()` (a fresh pixmap), and
`setPixmap` only shares the reference. An empty/failed render is never cached, so
a transient failure (e.g. doc mid-close) can't shadow a later valid render.

This is covered by an explicit invalidation test that asserts the rendered
*bytes* actually change after a lift / save / reorder / delete, and stay stable
on a pure cache hit.

## Debounce / settle interaction model (canvas)

Two timers keep gestures smooth without sacrificing final sharpness; the cache
sits underneath both and doesn't disturb them.

- **Page-render debounce** (`PAGE_RENDER_DEBOUNCE_MS = 80`). Rapid page changes
  (wheel, arrow-key spam) restart an 80ms timer; only the page you land on
  renders. Scrubbing through 20 pages renders once, not 20 times.
- **Settle timer** (`SETTLE_MS = 140`). During active motion (drawing, dragging,
  resizing, marquee, nudge, zoom) the view drops to fast nearest-neighbour
  pixmap scaling (`_begin_active_render`); 140ms after motion stops it restores
  smooth scaling and repaints crisply. You get fluid motion mid-gesture and a
  sharp result at rest.

The background page item uses `ItemCoordinateCache` (not `DeviceCoordinateCache`)
+ `SmoothTransformation`. Item-coordinate caching keeps the cached tile bounded
to the native raster size; device-coordinate caching would balloon to hundreds
of MB when zoomed into an A1 drawing.

## Image lift: the path that felt slow

`_lift_embedded_image` converts an embedded raster into a movable object. On the
drag-threshold crossing it:

1. crops the displayed pixels for the lifted object (keeps orientation correct),
2. removes the image's single `cm /Name Do` placement operator (no white hole),
3. re-renders the page background **once** (now via `render_page_cached`, which
   misses because the page content just changed, renders, and caches the new
   pixmap), and
4. updates the embedded-image list.

Two cost reductions here:

- The **re-render is cached forward**: the unavoidable mid-gesture render is
  paid once; the next reload of that page is free.
- The post-lift bookkeeping **no longer rescans the page**. Previously it ran a
  full `_compute_embedded_images` (another `get_images` + `get_image_rects`
  scan). Since we know exactly which `xref` was removed (and `get_images` would
  no longer report it anyway), we just drop that xref from the existing scan
  list: O(images-removed) instead of a full rescan mid-gesture.

The genuinely unavoidable cost is the one re-render of the modified page
(~100-130ms on an A1 sheet). Removing it entirely would require rendering the
"image-removed" background incrementally rather than from scratch. See Proposals.

## Markup-baked-clone thumbnails

Thumbnails (page panel + organizer) must show the page **plus unsaved overlays**,
but the live document must not be mutated (that would double-render markup in the
editor). So `clone_with_annotations(dicts_by_page)` builds a throwaway
`fitz.Document` copy (`insert_pdf`) with the current overlays baked in, and
thumbnails render from that clone. Measured cost is low (~3-5ms for a 12-page
clone), so this stays eager and correctness-first. It is **not** a bottleneck
and was deliberately left untouched. The clone is its own `PDFDocument` instance
with its own (cold) cache, so it never interferes with the live document's cache.

## Save lifecycle

`save()` is built for data integrity first:

- **In-place save** writes to a `NamedTemporaryFile` in the same directory, then
  swaps it in with `os.replace` (atomic on Windows + POSIX). PyMuPDF can't write
  over its own open file, so the live handle is closed and set to `None` *before*
  the swap. Any failure leaves `self.doc is None` (recoverable) rather than
  pointing at a closed document. On a failed swap the new file is salvaged to a
  `.bak` so no work is lost. The freshly written file is reopened as the live doc.
- **Save-As / merged doc** writes directly to the target.
- Both use `garbage=4, deflate=True` for a compact output.

A successful save bakes markup/redactions into page content and (in-place)
reopens the document, so it clears the whole page-pixmap cache: the post-save
`drop_baked_image_items` / `_strip_baked_annotations` reloads then render fresh.

## Measured impact (synthetic A1 doc, 2384×1684pt, 12 pages)

| Operation | Before | After | Notes |
|---|---|---|---|
| `render_page` (A1, zoom 1.5) | ~120-135ms | unchanged (cold) | the raster cost itself |
| repeated render of same page+zoom | ~120ms each | **~0ms (cache hit)** | nav back/forth, reload, organizer round-trips |
| lift → later reload of the page | ~120ms | **~0ms (cache hit)** | the image-move lag Lucas reported |
| post-lift embedded-image bookkeeping | full `get_images` rescan | drop-one-xref from existing list | no mid-gesture page scan |
| organizer open (per page) | ~3ms/page eager (all pages) | lazy (only visible cells) | ~300ms→instant on a 100-page doc |
| `clone_with_annotations` (12 pages) | ~3-5ms | unchanged | not a bottleneck; left as-is |

## Proposals (not built; payoff / risk)

1. **Downscaled preview-then-sharpen render.** On page switch, show a fast
   low-zoom raster immediately, then replace it with the full-res render when it
   lands. Payoff: page switches *feel* instant even on a cache miss. Risk:
   medium. A visible "pop" to sharp; must interact cleanly with the existing
   debounce/settle timers and the cache.

2. **Background-thread rendering.** Move `get_pixmap` off the UI thread (worker +
   signal back with the finished `QPixmap`). Payoff: the ~120ms A1 render never
   blocks the event loop; combined with (1) the UI stays fully live during a
   cache miss. Risk: medium-high. PyMuPDF page access is not thread-safe across
   the same `Document`; needs a dedicated render document/handle or a lock, plus
   careful cancellation when the user flips pages faster than renders complete.

3. **Incremental "image-removed" background for lift.** Instead of re-rendering
   the whole page after removing an image placement, paint white/transparent over
   just the lifted rect in the existing pixmap (when the background behind it is
   known-uniform) or render only the dirty region. Payoff: kills the one
   unavoidable ~120ms mid-gesture render. Risk: high. Getting "what's behind the
   image" right is exactly the no-white-hole problem `remove_image_placement` was
   built to solve; a wrong incremental patch reintroduces holes. Only safe for
   the uniform-background case.

4. **Tile-based rendering for deep zoom.** Render the visible viewport region at
   high zoom as tiles rather than rasterising the whole page. Payoff: large, at
   very high zoom on A1 sheets (where a full-page raster is huge and mostly
   off-screen). Risk: high. Significant rework of the background-item model and
   the lift crop math, which assume one full-page pixmap.

5. **Incremental save** (`incremental=True`, append-only) for in-place saves of
   large unchanged docs. Payoff: faster saves on big files with small edits. Risk:
   medium. Incompatible with `garbage=4` compaction and the atomic-replace
   integrity model; would need its own carefully-tested path and trades file-size
   growth for speed.
