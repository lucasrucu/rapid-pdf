# rapid-pdf: document tabs and multi-window

Design record for the tabs work. Agreed with Lucas on 2026-08-29. Every file
and line reference below was read out of the source at commit `a198784`
(v1.5.0) and every Qt behaviour claim was run on this install, not taken from
documentation.

**ALL SIX PHASES ARE IN.** This started as a plan and it is now a plan with the
outcome written back into it. Where the two disagree the outcome wins, and each
phase section says what actually happened under a "what it did" heading. If you
are picking it up cold, read "Current state", then the phase you care about,
then "Known bugs", which is the honest list of what is fixed and what is not.

The one thing still outstanding for the set as a whole is the release: see
"Standing constraints".

## The goal

Adobe-style tabs.

1. Several PDFs open in one window, one tab each.
2. Pages draggable from one tab into another, the way they already reorder
   inside a document today.
3. Tabs draggable out of the window to make a second window.
4. A settings screen, including whether the X button closes the window or just
   closes the document.

## Current state

**This table is the state BEFORE phase 0, at commit `a198784`.** It is kept
because the sections under it are all written against it and because the
before-and-after is the useful part. What it looks like now:

| Thing | Before (a198784) | After phase 6 |
| --- | --- | --- |
| PySide6 / Qt | 6.11.1 | unchanged |
| PyMuPDF | 1.27.2.3 (MuPDF 1.27.2) | unchanged |
| Python | 3.12.10 | unchanged |
| App version | 1.5.0 | 1.5.0, unreleased since (see Standing constraints) |
| Packaging | PyInstaller onedir, then Inno Setup | unchanged |
| Tests | 201, 11 files | **589, 32 files** |
| `ui/main_window.py` | 1153 lines | 1497, and it is chrome only |
| Where a document lives | `MainWindow._doc` | `DocumentView`, one per tab |
| New modules | | `document_area`, `document_view`, `window_registry`, `tab_tear_off`, `page_drag`, `undo`, `session`, `settings`, `preferences_dialog` |

Verified, not assumed, at `a198784`:

| Thing | Value | Where |
| --- | --- | --- |
| PySide6 / Qt | 6.11.1 | `.venv`, checked at runtime |
| PyMuPDF | 1.27.2.3 (MuPDF 1.27.2) | same |
| Python | 3.12.10 | same |
| App version | 1.5.0 | `core/version.py:35` |
| Packaging | PyInstaller onedir, then Inno Setup | `rapid-pdf.spec:3`, `rapid-pdf.iss` |
| Tests | 201, all passing | `pytest --collect-only`, 11 files |
| `ui/main_window.py` | 1153 lines | |

### The app is strictly single-document

`MainWindow` owns exactly one `PDFDocument`, created in `__init__` at
`ui/main_window.py:41`. `_doc` is named on 77 lines of that file (79
occurrences of `self._doc`, one of the 77 lines is a comment). There is no
concept of a second document anywhere in the UI layer.

The per-document state that currently lives on the window, all in
`__init__` (`ui/main_window.py:38-77`):

| Field | Line | What it is |
| --- | --- | --- |
| `_doc` | 41 | the live `PDFDocument` |
| `_current_page` | 42 | viewed page |
| `_org_render` | 43 | throwaway markup-baked clone for the Organizer |
| `_panel_render` | 44 | throwaway markup-baked clone for the left panel |
| `_dirty` | 45 | unsaved changes exist |
| `_ocr_thread` / `_ocr_worker` | 48-49 | in-flight OCR |
| `_pending_page_selection` | 52 | rows to re-highlight after a structural edit |
| `_search_hits` / `_search_index` / `_search_term` | 55-57 | Ctrl+F state |
| `_search_timer` | 58 | search-as-you-type debounce |

`_force_quit` (line 47) and `_theme` (line 40) are the only two that are
genuinely window-level.

### The existing QTabWidget is not document tabs

`self._tabs = QTabWidget()` at `ui/main_window.py:113` is the Editor /
Organizer view switcher. Tab 0 (Editor) is added at line 164, tab 1
(Organizer) at line 172, and `_on_tab_changed` at line 840 exists only to
refresh the Organizer when you switch to it. Do not confuse this widget with
what we are building. It stays, one per document view.

### One canvas per document is forced by the code

`PDFCanvas.set_document` (`ui/canvas.py:883`) clears `_page_annotations`
(884), calls `_scene.clear()` (890) and `_undo_stack.clear()` (893). Pointing
one canvas at a second document therefore throws away the first document's
scene and its entire undo history. There is no path where a single canvas
serves two documents. This is a hard constraint on every phase below.

### `open_paths` appends, and that behaviour is going away

`open_paths` is at `ui/main_window.py:323`. Lines 329-336: if a document is
already open, the chosen files are appended to the end of it via
`_append_pdfs` (`:433`). Opening a second file today merges it into the first.
That is deliberately being removed. Once tabs exist, a second file opens a
second tab. `File > Combine PDFs` (`:369`) stays as the explicit way to merge.

Note the callers: `open_pdf` (`:321`) and the file dialog at `:407`. Tests
`tests/test_page_jump.py:209` and `tests/test_page_panel_edits.py:39` also
call `open_paths`, so the signature must not change under phase 1.

### There is no OS file drop

Nothing in the repo calls `setAcceptDrops` on a window, and `hasUrls`,
`urls()` and `text/uri-list` appear nowhere. The single `setAcceptDrops` call
is `ui/page_panel.py:441`, on the list viewport, for internal page reordering.
Dragging a PDF from Explorer onto the app does nothing today. Adding it is new
work, not a fix.

### `core/single_instance.py` already works

141 lines, complete and correct. `forward_to_primary` (`:34`) hands a launch
to the running instance over a `QLocalSocket`; `InstanceServer` (`:70`) owns
the `QLocalServer`, aggregates a burst of launches over a 700 ms window and
emits `batch_ready`. `main.py:39` wires that to `MainWindow.handle_cli_files`.

For tabs this is a rewiring job, not a build. The batch already arrives as one
list of paths. What changes is what the window does with it: today one merged
document, tomorrow one tab per path.

## The three findings that de-risk this

Each was measured. Keep the evidence, because each one is the reason a phase
is estimated the way it is.

### 1. The engine already does cross-document page merging

`CombineDialog` opens N independent `fitz` documents in `__init__`
(`ui/combine_dialog.py:62-67`), keeps them alive in `self._sources`, and
`_do_combine` (`:323`) merges them page by page:

```python
merged.insert_pdf(src_doc)                              # line 333, whole file
merged.insert_pdf(src_doc, from_page=payload[2],        # lines 335-336, one page
                  to_page=payload[2])
```

`core/pdf_document.py` already has the live-document equivalents in both
directions: `extract_pages` (`:422`) copies pages out of the live doc into a
standalone in-memory doc, `restore_pages` (`:439`) inserts them back at
chosen indices, and `clone_with_annotations` (`:376`) does a whole-document
`insert_pdf`.

The only gap is that `PDFDocument.insert_pdf` (`:456`) takes a **file path**,
opens it, inserts and closes it. There is no method that inserts from another
**live** `PDFDocument`. That method, call it `transfer_pages_from`, is about
40 lines: insert the range, delete the source pages, invalidate both render
caches. Everything underneath it exists.

### 2. Reparenting a live canvas between two windows works

Tested on this install (PySide6 6.11.1, Windows 11). A `PDFCanvas` with a
document loaded and the page turned to 1 was moved from one `QMainWindow`'s
layout into a second window's layout by calling `addWidget` on the
destination, with no `setParent(None)` anywhere. Result:

| Checked | Before | After |
| --- | --- | --- |
| `internalWinId()` | 0 | 0 |
| scene object identity | | same object |
| undo stack identity | | same object |
| viewport identity | | same object (`QWidget`) |
| scene item count | 1 | 1 |
| current page | 1 | 1 |
| `canvas.window()` | w1 | w2 |
| document still open | yes | yes |

Nothing reloaded, nothing was recreated, the undo history survived. This is
the whole reason tear-off is a gesture layer rather than a rebuild.

> **One line of that table stopped being true in phase 5: the undo stack.** It
> is the WINDOW's now, not the canvas's, so a view arriving in another window
> necessarily joins that window's history. Everything else in the table still
> holds, and it is still what makes the tear-off a reparent rather than a
> rebuild; the history is the one thing a cross-window move gives up, which is
> why cross-window PAGE moves are refused outright. See the undo decision
> below and `ui/undo.py`.

**Re-verified in phase 3 across the real path.** The table above was a bare
canvas moved between two layouts. `MainWindow.adopt` is what the menu item and
(later) the gesture actually call, and every identity in it survives that too:
run `tools/smoke_multi_window.py` with no `QT_QPA_PLATFORM` set and it prints
the same list, `internalWinId()` included, against genuine Windows handles.
Confirmed on 2026-08-29, PySide6 6.11.1, Windows 11: same scene, same undo
stack, same viewport, same `fitz` document, page unchanged, `internalWinId()`
0 before and after on both the canvas and the `DocumentView`.
`tests/test_multi_window.py` asserts the same set offscreen.

**It works because the canvas is a raster `QGraphicsView`.** `PDFCanvas`
subclasses `QGraphicsView` at `ui/canvas.py:676`, and `OpenGL`, `QOpenGL` and
`setViewport` appear nowhere in the repo. The viewport is a plain `QWidget`,
so it has no native window handle to destroy on a reparent.

> **Standing constraint.** If anyone ever sets an OpenGL viewport on the
> canvas for render performance, tear-off breaks and must be re-tested. An
> OpenGL viewport is a native window; reparenting it across top-level windows
> destroys and recreates the GL context, which loses the scene's backing
> store. Do not do this without redoing the test above.

### 3. Middle-click tab close is free

With `QTabBar.setTabsClosable(True)`, synthesising a middle-button press and
release over tab index 1 emitted `tabCloseRequested(1)`. With
`setTabsClosable(False)` the same events emitted nothing. Qt already
implements it. No custom event handling needed.

## Phases

| Phase | What | Estimate | Status |
| --- | --- | --- | --- |
| 0 | Settings module plus close rework | 1.5 d | **done**, `feat/settings-and-close` |
| 1 | Extract `DocumentView` from `MainWindow` | 2-3 d | **done**, `refactor/document-view` (the riskiest, as expected) |
| 2 | Tabs in one window | 1.5-2 d | **done**, `feat/document-tabs` |
| 3 | Multi-window via `WindowRegistry`, menu item only | 1.5-2 d | **done**, `feat/multi-window` |
| 4 | Tear-off drag gesture | 1-1.5 d | **done**, `feat/tab-tear-off` |
| 5 | Pages between tabs | 4 d | **done**, `feat/pages-between-tabs` |
| 6 | Session restore | 0.5-1 d | **done**, `feat/session-restore` |

The order is not arbitrary. Each phase leaves the app shippable, and phases 3
and 4 are split specifically so the hard, testable half lands before the
gesture half. That split paid: phase 4 turned out to be a thin gesture layer
over phase 3's methods, and the two things it hit that the design missed (the
frame offset, and `QTabBar` needing a release) were both gesture-only.

### Phase 0: settings and close rework

Branch `feat/settings-and-close`, already going. `core/settings.py` is new,
`ui/theme.py` is being touched. Gives us a settings surface and the
"X closes document vs closes window" preference, which the later phases need
to have a home for anyway.

### Phase 1: extract `DocumentView`

Move everything per-document out of `MainWindow` into a `DocumentView` widget:
the canvas, the page panel, the toolbar, the search bar, the Editor/Organizer
`QTabWidget`, the document, the two render clones, the dirty flag, the search
state.

`MainWindow` keeps the menu bar, the status bar, the theme, the update strip,
`_force_quit`, and `closeEvent`.

**Zero user-visible change. All 201 existing tests must pass unmodified.**

This is the riskiest phase, not the drag. Three things in `main_window.py` are
quietly load-bearing, and the worst of them fails silently.

**1. The flush / save / strip sequence.** This is what makes annotations
reopen editable. The four methods:

| Method | Line | Job |
| --- | --- | --- |
| `_flush_annotations` | 948 | write canvas items into the fitz doc as real PDF annotations, then embed the JSON model |
| `_strip_baked_annotations` | 957 | after the save, delete those baked objects from the live doc |
| `_load_saved_annotations` | 971 | on open, strip the baked copy and rebuild editable items from the model |
| `_after_successful_save` | 505 | the shared post-save bookkeeping both save paths run |

The order is fixed. `save_pdf` (`:522`) and `save_pdf_as` (`:535`) both call
`_flush_annotations`, then on success `_after_successful_save`, which calls
`setClean()` on the undo stack, then `_strip_baked_annotations` (`:514`), then
`drop_baked_image_items` (`ui/canvas.py:1317`), then rebuilds the panel.

Break this and **the file still saves**. It just no longer round-trips: the
markup comes back as flat pixels, or comes back twice, once as a live item and
once baked into the background. Nothing raises. No test catches a wrong
ordering that still writes a valid PDF. Move these four as a block, do not
re-sequence them, and open-save-close-reopen a marked-up file by hand before
calling phase 1 done.

**2. The markup-baked clone discipline.** `_make_markup_baked_render`
(`:844`) returns a throwaway `PDFDocument` with the current unsaved overlays
baked in. It exists because thumbnails rendered straight from the live doc
show the wrong markup: drawn markup lives as Qt overlay items and is not in
the doc until save, and on open the doc still carries the previous save's
baked markup right up until the strip step. The comment at `:889` spells this
out.

Two clones are held at once, and each has a paired closer:

| Clone | Made by | Closed by |
| --- | --- | --- |
| `_org_render` | `_refresh_organizer` (`:860`) | `_close_org_render` (`:873`) |
| `_panel_render` | `_refresh_panel_thumbnails` (`:881`) | `_close_panel_render` (`:901`) |

Lose the pairing and you leak a whole `fitz` document per tab switch, because
`_refresh_organizer` runs on every switch into the Organizer tab
(`_on_tab_changed`, `:840`). With ten document tabs open that is a fast route
to a gigabyte. Keep make and close together in `DocumentView`, and make
`DocumentView` close both in its own teardown.

**3. Undo stack ownership.** One canvas per document is a requirement, not a
preference. See `PDFCanvas.set_document` above: the stack is cleared on every
`set_document`. `DocumentView` therefore owns its canvas outright and never
shares it.

Do this in a worktree under a strict move-don't-change rule. If a test needs
an edit to pass, behaviour moved, not just code. Stop and find out why.

### Phase 2: tabs in one window

Done. `ui/document_area.py` holds a `DocumentTabBar` over a `QStackedWidget`,
index-parallel, asserted by `DocumentArea.check_invariant()`. `MainWindow` holds
one of those instead of one `DocumentView` and `MainWindow.view` is the front
one. Middle-click closes (finding 3, and it survived replacing Qt's own close
button with the dirty-dot one). `open_paths` no longer appends: a second file is
a second tab, and a file already open activates its tab. `handle_cli_files`
decides by VERB, so `--combine` stages a combine whatever the file count and a
plain open of N files opens N tabs.

Rebound on a tab switch, all in `MainWindow._on_front_view_changed`: the five
chrome signals (`_connect_view` / `_disconnect_view`), the Edit menu's Undo and
Redo (rebuilt, because `createUndoAction` binds an action to one stack for its
life), the page box and the title (`DocumentView.refresh_chrome`), and the fit
group (`_sync_fit_group`, which reads the arriving CANVAS rather than the
app-wide setting, since a manual zoom breaks the fit on one canvas only).

`close.confirm_multiple_tabs` finally has a reader and a checkbox. It is skipped
whenever any document is dirty, because the per-document save prompt is a better
version of the same question and two dialogs in a row are worse than one.

Not done here, on purpose: MRU `Ctrl+Tab`, "Move to New Window" and
background-tab clone release all went to phase 3. Of those, phase 3 shipped the
last two and **phase 4 finally shipped MRU `Ctrl+Tab`**. The registry keeps an
activation order for WINDOWS, which is a different list from a visit history for
TABS; `DocumentArea._mru` is now the second one. `step_current` is still
positional and stays that way.

### Phase 3: multi-window, menu-driven

Done. `ui/window_registry.py` tracks every open `MainWindow` in activation
order, owns app lifetime, and answers "where does this file go". `main.py` sets
`setQuitOnLastWindowClosed(False)` and the decision moves into
`WindowRegistry.unregister`: one place decides, so a torn-off window closing
while its parent lives and the parent closing while the child lives are the
same code path. Ownership is weak, a plain list dropped in `unregister`, with
`destroyed` connected to a `shiboken6.isValid` purge as the backstop.

`MainWindow.adopt` / `DocumentArea.adopt` take a live view out of another
window; `DocumentArea.detach` gives one up without tearing it down. Driven by
the tab menu's **Move to New Window** and **File > New Window** (`Ctrl+Shift+N`).
No dragging: that is phase 4, and keeping it out is what makes all of this
testable.

`batch_ready` now goes to `WindowRegistry.route_open` rather than one window's
`handle_cli_files`: a path already open anywhere raises its own tab, anything
else lands in the window last touched, no windows means make one, and
`--combine` still stages the dialog on the active window.

**Per-window chrome, and the third case is the one that was not obvious.** The
QSS and the QPalette are set on the `QApplication`, so those cover every window
for free. Two do not: `apply_mica` works on the HWND and a new top-level gets a
fresh one, so it is reapplied in `showEvent` (the call in `__init__` runs before
there is a handle to apply it to and is a no-op on every window); and the
code-drawn surfaces, which is why each window connects `theme_changed` for
itself. The third is the UPDATE STRIP, and it went the other way: it is one
check per APPLICATION, so `_should_check_for_updates` lets only the first window
run it. Three windows would otherwise make three GitHub requests, show three
strips for the same release, and race to stage the same download.

OS file drop is here too, which did not exist anywhere in the app before. A PDF
dropped on a window opens as a new tab IN THAT WINDOW, never appended to the
document on screen. Discriminated on `hasUrls()`, which is only ever true for a
shell drag, so it cannot collide with either internal page drag.

**Phase 3 built every mechanism the tear-off needs and all of it is testable
headless.** Window creation, adoption of a live view, undo-stack handover,
source-window teardown, close semantics with N windows open, app-quit rules. A
menu item can be driven by a test; a mouse gesture across two top-level windows
mostly cannot. Phase 4 is now a thin gesture layer that can be switched off
without losing multi-window.

`tools/smoke_multi_window.py` runs the whole path outside pytest with a real
event loop, on offscreen or on the real Windows platform. Re-run it natively
after anything that touches window creation, adoption or app lifetime: it is
where the reparent invariants below are checked against real window handles.

### Phase 4: the tear-off drag gesture

Done. `ui/tab_tear_off.py` holds the whole gesture and calls nothing but phase
3's move methods. `DocumentTabBar` forwards its three mouse events and its key
presses in, and paints the insertion line; `DocumentArea` owns the controller
and the MRU visit history. Two arguments were added rather than two new code
paths: `MainWindow.move_view_to_new_window(view, geometry=...)` places the torn
window under the cursor, and `move_view_to_window(view, target, at=...)` docks
at the index the insertion line was showing.

MRU `Ctrl+Tab` / `Ctrl+Shift+Tab` landed with it, which is the thing phase 2
deferred and phase 3 confirmed was still missing. `DocumentArea._mru` is a visit
history for TABS, written in `_set_current_view` and FROZEN while the walk is in
flight; without the freeze, holding Ctrl flips between the top two entries
forever. `step_current` was not touched: `Ctrl+PgDn` is still strictly
positional and the two orders are meant to differ. `MainWindow` notices Ctrl
coming up with an application-wide event filter installed only for the length of
the walk, because the release lands on whatever has focus and a canvas eats it
long before it would reach the window.

**Two things worth knowing that the design did not anticipate.**

- **The grab offset has to be measured against `frameGeometry()`, not the
  widget origin.** `move()` positions the FRAME. Offscreen the difference is
  2 px and invisible; on the real Windows platform the smoke script measures it
  at 70 px, which is the title bar plus the menu bar, so the tab would have sat
  that far above the cursor for the whole drag.
- **`QTabBar` has to be handed a release before the gesture takes over.**
  `setMovable(True)` means a reorder is already in flight when the threshold is
  crossed, and a `QTabBar` left mid-drag keeps a pressed index the next press
  inherits. `_settle_tab_bar` sends it one synthetic release, and the tab index
  is re-read afterwards because that reorder may have moved it.

**What phase 3 changed about it.** Less than expected, which was the goal.

- The gesture has exactly two calls to make: `MainWindow.move_view_to_window`
  for a drop onto an existing window, and something very close to
  `move_view_to_new_window` for a drop on empty desktop. Both already handle
  the source window emptying and closing behind them, and both already keep the
  order the reparent depends on.
- `move_view_to_new_window` builds the destination with `show=False`, sizes and
  positions it, shows it, and only then moves the view. That is exactly the
  "create the real window immediately on crossing the tear threshold" decision
  below, so the gesture should call the same method rather than reimplement the
  sequence with a position of its own. Give it a `geometry` argument rather
  than a second code path.
- **The tab index has to be captured before the adopt, not after.**
  `DocumentArea.detach` takes an optional index for this reason: `insertWidget`
  on the destination reparents the view, and Qt removes a reparented widget
  from the old layout on its own, so by the time the source tidies up its stack
  no longer knows where the tab was. This is the one place the bar/stack
  invariant is briefly false, and it is re-asserted at the end of `detach`.
- **Do not disconnect a moving view's signals with a bare
  `signal.disconnect()`.** The destination is connected before the source lets
  go, so a blanket disconnect cuts the destination's wiring too. Every
  per-view connection in `DocumentArea` is a bound method for exactly this
  reason (see `_view_wiring`), and `MainWindow._disconnect_view` is guarded on
  which view is actually wired rather than merely tolerating a failure, because
  Qt answers an unmatched disconnect with a `RuntimeWarning` on stderr, not an
  exception.
- A view arriving in a window whose only tab is empty replaces that tab. The
  gesture gets that for free from `MainWindow.adopt`.
- Dropping the LAST tab out of a window is a no-op worth refusing in the
  gesture as well: the menu item is disabled at one tab, and the tear-off
  should have a threshold that never fires there rather than closing and
  recreating the window the user is dragging from.

### Phase 5: pages between tabs

Drag a page from one document's panel or organizer into another's. Needs
`transfer_pages_from` (finding 1, about 40 lines) and a mime-type discipline
(see below).

Four days, and the undo/dirty refactor is the uncertain part, not the drag. A
cross-document move dirties two documents and has to undo as one action across
both. See the undo decision below.

**What phase 4 changed about it.** Three things, none of them large.

- The three-gestures table below held. The tear-off never enters Qt's DnD
  system, so phase 5's `QDrag` cannot collide with it by construction, and the
  four `event.source() is not self` guards are still the only thing in the way.
- **A tear-off holds a mouse AND keyboard grab on the source tab bar for the
  length of the gesture.** If dropping pages ONTO a tab ever becomes a thing,
  it has to be a Qt drop on a bar that may be mid-grab. Starting a page drag
  from the tab bar is not possible today and should stay that way.
- `DocumentView.rerender_for_screen_change` is the existing "drop the cache and
  redraw" call, used by the DPI check on a drop and by the window's
  `screenChanged`. A cross-document page move should reuse it rather than grow
  a third copy of that sequence.

**What phase 5 actually did, and the four things worth knowing.**

Done. `ui/page_drag.py` holds the payload and the lookup, `ui/undo.py` holds
the window stack, `TransferPagesCommand` in `ui/page_commands.py` is the one
command, and `PDFDocument.transfer_pages_from` is the 40 lines the finding
promised. 562 tests, 27 of them new in `tests/test_pages_between_tabs.py`, and
`tools/smoke_multi_window.py` grew a step 4 that runs it on the real platform.

- **The undo stack moved to the window, and the dirty flag had to move with
  it.** `setClean()` marks one index on one stack, and one stack now carries
  three documents, so saving B would have cleared the modified marker on A.
  Each `DocumentView` keeps a revision counter instead: every command that
  touches it bumps the counter on redo and drops it on undo, and the counter it
  was last saved at is the marker. A dropped redo branch retires a marker that
  lived inside it, which is the rule `QUndoStack` applies to its own clean
  index. Six existing tests asserted the per-document stack and were rewritten;
  none of them found a bug, they were pinning the old contract.
- **A view leaving a window takes its history with it, or rather loses it.**
  There is no selective removal in `QUndoStack`, so `release_view` drops the
  whole stack, and only when the departing view is actually named in it
  (`WindowUndoStack.drop_history_for`). An unedited tab, which is the ordinary
  tear-off, costs nothing. This is the trade for cross-window moves being
  refused: two windows means two stacks and the duplicate-page problem returns.
- **`source() is self` did not have to go, only stop being the only check.**
  The plan said the four guards had to become mime-format checks, and they did;
  what was not obvious until the tests ran is that keeping `source() is self`
  as a SECOND way in costs nothing and means a drag carrying no payload at all
  still resolves to a reorder. Every one of the 535 existing tests passed
  unmodified through the drag layer because of it.
- **A `QMimeData` passed inline into a `QDropEvent` is a segfault.**
  `QDropEvent` does not take ownership, so an inline argument is collectable
  the moment the constructor returns and the handler reads freed memory. It
  took the smoke script down C++-side with no traceback. Hold it in a named
  local. Only bites code that BUILDS drop events, so the app itself is safe and
  the smoke script is not.

### Phase 6: session restore

Reopen the tabs and windows that were open last time. Small, and it depends on
everything above being stable.

**What it did.** Done. `core/settings.py` grew `startup.restore_tabs` (off by
default) and a `session.windows` block; `ui/session.py` holds the capture, the
recorder and the restore; `DocumentView` learned to stand for a file it has not
read. 589 tests, 26 of them new in `tests/test_session_restore.py`, and
`tools/smoke_multi_window.py` grew a step 8 that runs the whole thing on the
real platform, which is the only place the saved geometry and screen name are
checked against a window manager that has an opinion.

**The record.** Per window: geometry, screen name, the front tab, and its tabs
as `{path, page, zoom, fit_mode}`. `zoom` is the view transform's scale, NOT
`PDFCanvas._zoom`, which is the raster scale and is a fixed 1.5; the two were
easy to confuse and only one of them is what the user changed. A fit mode and a
zoom are both saved and only one is applied: a fit recomputes the scale from
the window size the moment it lands, so the zoom only means anything when no
fit was active.

**It lives in the settings store, not a second file.** A second file is a
second thing to find, to quarantine when it is corrupt, and to keep in step
with the first. `core/settings.py` already does all three. `session.windows` is
the first field in the schema that is a structure rather than a scalar, and
`_as_session_windows` is its validator and its normaliser at once, so a
hand-edited or half-written session reads back as "no session" exactly the way
a nonsense `x_closes` reads back as `"window"`, and the restore path never has
to check a type. Adding the two keys did NOT bump `schema_version`: adding a
key is not a migration.

**Only tabs with a real path come back, and that is the answer, not a
limitation to work around.** An untitled or merged document exists only in
memory. Writing it down means serialising the document into a cache directory,
which brings a disk-space policy, a cleanup policy, and a restore path that can
fail on a corrupt cache, all for a document the user has not named. It is
skipped silently, in the coercer, so it holds however the record got there.

**Unsaved annotation state is a deliberate non-goal.** The markup model already
round-trips through the saved PDF (`_flush_annotations` /
`_load_saved_annotations`), and an autosave-to-cache scheme here would be a
second copy of that machinery aimed at one case: a crash with unsaved markup.
That case is CRASH RECOVERY. It wants its own design and its own prompt on the
way back in, because a restored tab that silently came up dirty with no way to
say where the changes came from is worse than one that came up clean. Restored
tabs are exactly what is on disk.

**Tabs come back LAZY, and that is where the interactions are.**
`DocumentView.stage_path` claims an empty view for a path without reading it;
`ensure_loaded` opens it; `set_active` is what calls that. Eight A1 drawings
read at once before the window paints would make startup feel broken, and
opening big drawings fast is the whole pitch. Four things it touches, each
found by writing the test for it:

- **`set_active` had to load ABOVE its early return, not below it.** A view is
  built with `_active` already True, so the first tab of a restored window is
  made current without the flag ever changing, and a load hung off the flag
  never runs for exactly the tab that has to load.
- **`has_document()` stopped being the "is this tab free" question.** A pending
  tab answers False and is spoken for. `is_empty()` is the new question and
  every site that meant "free" now asks it: `_target_view`, `combine_paths`,
  the `adopt` placeholder, `close_tab`, and the multiple-tabs close warning. On
  `close_tab` in particular, asking `has_document()` made Ctrl+W a dead key on
  every tab of a freshly restored window.
- **`document_path()` answers with the pending path.** That is one line, and it
  is what makes the tab label, the disambiguation that walks up the path on a
  collision, the tooltip, `DocumentArea.index_of_path` and
  `WindowRegistry.find_by_path` all work on a tab nobody has opened. An
  Explorer double-click on a restored-but-unread file raises its tab, which
  then opens it, rather than opening a second copy beside it.
- **The dirty marker needed nothing.** A pending view has read nothing, so it
  is not dirty, and `_refresh_dirty` already guards on `has_document()`. The
  per-window undo stack needed nothing either: `_new_view` hands the stack over
  at construction, well before any file is read.

**Missing files are filtered before any tab is made.** A mapped drive that is
offline takes every tab with it at once, so the check is one `os.path.isfile`
per record up front and the whole answer is one status line: "3 files from the
last session could not be found." A file that vanishes between the restore and
the click still gets `open_path`'s message box, which is the right amount of
noise for one tab the user just clicked.

**When it is written, and which windows count.** On every window close and on
`aboutToQuit`. The one decision is in `MainWindow._record_session`: a window
closed while the application carries on drops out of the record, because the
user closed it on purpose; a window closed as part of a shutdown stays in,
because by `aboutToQuit` its views have been torn down and there is nothing
left to read off them. "Part of a shutdown" is `_force_quit` (the Quit menu) or
`_session_is_ending()` (Windows) or being the last window. Without that rule
the Quit menu, which closes windows one at a time, records only the last one.

**Two gates on the way in, both in `should_restore`.** The setting has to be
on, and the launch has to have carried nothing. A PDF double-clicked in
Explorer is a request to read that file; burying it under eight restored tabs
is not what anyone meant, and the same goes for a `--combine` verb. `main.py`
forwards a second launch and exits before this is asked, so it can only ever
run in the primary.

**`close.confirm_multiple_tabs` could reasonably default to false now, and it
is deliberately left true.** The warning exists because closing a window of
tabs loses your place, and session restore is the thing that gives your place
back. But restore is OFF by default, so flipping the warning's default would
take the safety net away from everybody who has not opted in. The honest
sequence is: ship this, and revisit the default if and when restore becomes the
default. Not in this phase.

## Design decisions

### `QTabBar` + `QStackedWidget`, not `QTabWidget`

`QTabWidget` keeps its tab bar and its page stack in lockstep, and the stack is
a private child (`qt_tabwidget_stackedwidget`) you do not own. Measured:
`removeTab(0)` does not delete the page widget, but it leaves that widget
hidden and still parented to the private stack. So the widget's parenting stays
bound to the `QTabWidget` you are trying to detach from.

Tear-off needs mid-drag states where the tab is gone from the bar but the
widget is still parented somewhere you control, and where the widget has moved
to a new window but no tab has been committed yet. Owning both halves gives
you those states directly. Fighting a private child for them does not.

### Manual mouse tracking for the tear-off, not `QDrag`

Three reasons, all fatal on their own:

1. `QDrag` gives you a static pixmap following the cursor. We want the real
   window following the cursor.
2. `QDrag.target()` returns `None` on a rejected drop, so you cannot tell
   "dropped on empty desktop, make a window" apart from "drop refused".
3. On Windows `QDrag::exec()` blocks the event loop for the duration of the
   drag. Nothing you want to animate during the drag can run.

So: grab the mouse on the tab bar, track it yourself, never enter Qt's
drag-and-drop system at all.

### Create the real window immediately on crossing the tear threshold

Do not drag a ghost outline and create the window on drop. Cross the
threshold, create the window right there, adopt the view into it, and move
that window with the cursor. What the user sees during the drag is the result,
not a preview of it. This also means the drop is a no-op, which removes the
whole class of "drop failed, where did my document go" bugs.

### Never call `setParent(None)` when moving a view between windows

On Windows, `setParent(None)` promotes the widget to a top-level, which
creates a real `HWND`. Reparenting it back into a layout then destroys that
`HWND`, and the widget's native resources go with it.

The order is: create the destination window first, adopt the view into its
layout, and only then remove it from the source. Finding 2 was measured with
exactly this order and `internalWinId()` never left 0.

### Three drag gestures must not collide

| Gesture | Mechanism | Payload |
| --- | --- | --- |
| Tab drag (tear-off) | manual mouse tracking, never enters Qt DnD | none |
| Page drag | `QDrag` | `application/x-rapidpdf-pages` |
| OS file drop | Qt DnD, incoming | `text/uri-list` |

Today both page drags build their mime data from
`self.model().mimeData(...)`, which produces Qt's default
`application/x-qabstractitemmodeldatalist`:

- `ui/page_panel.py:184-188`
- `ui/organizer.py:250-255`

Both views then guard on the drag's **source object**:

- `ui/page_panel.py:244` (`dragEnterEvent`), `:254` (`dragMoveEvent`),
  `:272` (`dropEvent`)
- `ui/organizer.py:364` (`dropEvent`)

Each is `if event.source() is not self: event.ignore()`. That works only
because a page can never come from anywhere else. The moment pages cross
tabs, a drag from another `DocumentView`'s panel is a legitimate drop and
`event.source() is not self` rejects it.

**Before cross-tab page dragging ships, those four guards must become
mime-format checks** against `application/x-rapidpdf-pages`, with the source
identity carried inside the payload rather than inferred from the object.
`setDragDropMode(InternalMove)` at `ui/page_panel.py:438` and
`ui/organizer.py:487` also has to go, since the move is no longer internal.

### Undo: one `QUndoStack` per window, not per document

A cross-document page move is one user action with two document-level effects:
removed from A, inserted into B. Split across two stacks, undoing on B
re-inserts the page into A while A's stack still thinks nothing happened, and
you get a duplicate. There is no ordering of two independent stacks that
avoids it.

One stack per window, with commands that name the documents they touch. This
is the part of phase 5 that is genuinely uncertain, because the canvas
currently owns its stack (`ui/canvas.py:745`, exposed at `:822`) and
`set_document` clears it (`:893`).

### Hand-roll rather than use `PySide6-QtAds`

QtAds is a full docking framework. We want one gesture and one registry. It
would bring a large dependency, a second layout system alongside our own, its
own serialisation format, and a styling surface that fights `ui/theme.py`.
Phase 4 is about 400 lines of our own code that we can read.

**Condition for reversing this call:** if phase 4 overruns badly, or if the
app ever grows genuine dockable panels (a properties dock, a comments pane, a
side-by-side compare). Either of those flips the trade. Revisit it then, not
before.

## Known bugs found during the design pass

Found while reading the source for this plan. None are caused by the tabs
work; several get worse under it.

**Where they stand now that all six phases are in:**

| # | What | Status |
| --- | --- | --- |
| 1 | Windows shutdown blocked by `event.ignore()` | **fixed** in phase 0/3, `_session_is_ending()` |
| 2 | Relaunch while running does nothing | **fixed**, `InstanceServer._flush` + `route_open` |
| 3 | `Ctrl+Q` / `Ctrl+W` dead, `File > Quit` shows "Exit" | **fixed** for Ctrl+Q and Ctrl+W. `Ctrl+F4` is still nothing, on purpose |
| 4 | Encrypted PDFs open then throw on first render | **fixed** in phase 5 |
| 5 | Deleting a page leaves a bookmark at page -1 | **fixed** in phase 5 |
| 6 | The second switch into the Organizer throws | **fixed** in phase 3 |
| 7 | A save that cannot overwrite silently writes a `.bak` | **fixed** in phase 3 |

**Nothing on this list is still open**, with one deliberate exception noted
under bug 3. What IS still open is in "What is not done", below.

### 1. Windows shutdown is blocked by an unconditional `event.ignore()` - FIXED in phase 0/3

`ui/main_window.py:1122`, `closeEvent`. If a document is open and
`_force_quit` is false, the handler closes the document and then calls
`event.ignore()` at line 1140, unconditionally. Windows asks a process to
close on logout, restart and shutdown through exactly this path, so the app
refuses and the OS shows the "this app is preventing you from shutting down"
block. Needs a `QSessionManager` check, or at minimum to accept the close when
the app is being asked to quit rather than clicked.

**How it was fixed.** `_connect_session_manager` listens for
`commitDataRequest`; `_session_is_ending()` reads that flag OR Qt's own
`isSavingSession()`, because neither is reliable alone; and `closeEvent` takes
that branch first, with no prompt and no `event.ignore()`. Phase 6 writes the
session on the same branch, before the teardown, because everything that
happens there is on Windows's clock.

### 2. Relaunch while running does nothing - FIXED

Double-clicking the shortcut while the app is already running should raise the
existing window. It does nothing at all. Path:

- `main.py:25` calls `forward_to_primary([], False)`, which delivers an empty
  payload successfully and returns `True`, so the new process exits at
  `main.py:26`.
- The primary receives it and calls `add_launch([], False)`.
- `InstanceServer._flush` (`core/single_instance.py:125`) ends with
  `if files:` at line 133, so with no files it emits nothing.

Net: the second process dies, the first never hears about it. The fix belongs
in `_flush`: an empty batch should still emit a raise-and-focus.

**How it was fixed, and it went exactly where the plan said.** `_flush` gates
on `_pending_launch` rather than on `files`, so an empty batch still emits, and
`WindowRegistry.route_open` treats a batch with no paths as "raising IS the
job". `tests/test_single_instance_relaunch.py` covers it.

### 3. Ctrl+Q and Ctrl+W were dead keys - FIXED. Ctrl+F4 still is, on purpose

- `Ctrl+Q` and `Ctrl+W` appear nowhere in the source. The only hits are the
  Quit action at `ui/main_window.py:208` and a docstring at `:1118` that
  claims Ctrl+Q works. It does not.
- `File > Quit` is bound to `QKeySequence.StandardKey.Quit`. On Windows that
  standard key resolves to the sequence whose display text is **"Exit"**
  (measured: `QKeySequence(StandardKey.Quit).toString()` returns `'Exit'`, and
  it is not empty, so Qt renders it in the menu). There is no key on a normal
  keyboard that produces it. The menu shows a shortcut nobody can press.
- `File > Close PDF` (`:200`) has no shortcut at all.
  `QKeySequence.StandardKey.Close` resolves to `Ctrl+F4` on Windows but is
  never used, so `Ctrl+F4` does nothing either.

Under tabs this matters more, because `Ctrl+W` closing a tab is the thing
everyone will reach for first.

**How it was fixed.** `File > Quit` is spelled `"Ctrl+Q"` rather than
`StandardKey.Quit`, so the menu shows a key that exists; `File > Close PDF` is
`"Ctrl+W"` and closes the front TAB. `Ctrl+F4` is deliberately still unbound:
`StandardKey.Close` resolving to it is a Windows convention for MDI child
windows, this app has tabs rather than MDI children, and a second key for the
same thing is worth less than one key everybody already knows.

### 4. Encrypted PDFs report a successful open, then throw on first render - FIXED in phase 5

`PDFDocument.open` (`core/pdf_document.py:102`) returns `True` for a
password-protected PDF, because `fitz.open` succeeds and only sets
`needs_pass`. Measured on an AES-256 file:

```
open(encrypted) -> True
  needs_pass: 1   is_encrypted: True
  page_count: 2
  render_page RAISED: ValueError: document closed or encrypted
```

So the app accepts the file, shows a two-page document, and then blows up on
the first render. `open` needs a `needs_pass` check and either a password
prompt or a clean refusal.

### 5. Deleting a page leaves a bookmark pointing at page -1 - FIXED in phase 5

`PDFDocument.delete_page` (`core/pdf_document.py:400`) calls
`fitz.Document.delete_page`, which renumbers the table of contents but leaves
the deleted page's own entry pointing at `-1`. Measured on a 3-page file with
one bookmark per page:

```
before:                    [[1,'Page One',1], [1,'Page Two',2], [1,'Page Three',3]]
after delete_page(1):      [[1,'Page One',1], [1,'Page Two',-1], [1,'Page Three',2]]
after save and reopen:     [[1,'Page One',1], [1,'Page Two',-1], [1,'Page Three',2]]
```

It survives the save, so the file we write has a broken bookmark in it. Same
applies to `delete_pages` (`:405`). The fix is to drop TOC entries whose
target went to `-1` after any deletion.

### 6. The SECOND switch into the Organizer throws, and loses the clone — FIXED in phase 3

Found during phase 1, and measured on `main` at `ab0506a` before the split, so
it is not something the split introduced. `_refresh_organizer` closes the old
render clone as its first statement and only later hands the Organizer a new
one, but in between it calls `QApplication.processEvents()`:

```
self._close_org_render()          # the Organizer's render source is now closed
...
QApplication.processEvents()      # the Organizer's queued render runs anyway
self._org_render = self._make_markup_baked_render()
```

The Organizer's `_render_visible` (`ui/organizer.py:748`) reads `src.doc`,
PyMuPDF raises `ValueError: document closed`, and the exception unwinds out of
`_refresh_organizer` before the new clone is ever assigned. Net effect on a
second switch into the Organizer: a stack trace on stderr and `_org_render`
left at `None`, so the grid keeps whatever it had. The first switch is clean,
because there is no previous clone to close, which is why nothing has noticed.

It only bites when the Organizer has a render queued at that moment, so it is
intermittent by hand and reliable from a test that switches out and back.

Two candidate fixes were written up here, and phase 3 took the second one plus
a narrowed version of the first, because they answer different halves:

- **`_refresh_organizer` builds, hands over, THEN closes.** The old clone stays
  open, and stays the grid's source, until the new one has been handed to it.
  The `processEvents()` is still there and no longer matters, because the
  Organizer is pointing at a live document at every instant.
- **`release_render_source(clone)` on both the Organizer and the page panel.**
  For the other caller, `_close_org_render` / `_close_panel_render`, which
  genuinely are closing a clone and not replacing it (backgrounding, and
  clearing the document). It drops the pointer WITHOUT a refresh, and only when
  the grid is still on that clone: `set_document(doc, None)` would rebuild every
  thumbnail off the live document, which is the opposite of what backgrounding
  is for. Anything already drawn stays drawn and later cells fall back to the
  live document, which is still open.

**And the line it actually raised from is worth keeping**, because it reads as
a null check and is not:

```python
if not src or not src.doc:      # ui/organizer.py, ui/page_panel.py
```

PyMuPDF's `Document` defines `__len__`, so truth-testing a CLOSED document
raises `ValueError: document closed` rather than answering False.
`core.pdf_document.source_is_readable` is the question that can be asked
safely, and every lazy renderer asks it now. It takes any render source rather
than a `PDFDocument`, because both widgets are handed stand-ins in tests whose
`.doc` is not a fitz document at all.

**One correction to the description above, found while fixing it.** The
exception does not unwind out of `_refresh_organizer` on the current code: it
is raised inside a queued Qt callback, and PySide prints it to stderr and
carries on, so `_org_render` IS reassigned and the only symptom is a stack
trace nobody reads plus thumbnails that quietly stop updating. That is worse,
not better, and it is why `tests/test_organizer_clone_lifecycle.py` asserts on
the state every render saw when it started rather than waiting for an exception
to arrive somewhere it can be caught. Those tests fail against the pre-fix
ordering (verified by reverting it).

### 7. A save that cannot overwrite silently writes a `.bak` — FIXED in phase 3

Found in phase 3, because multi-window is what makes it ordinary. `save()`
writes an in-place save to a temp file beside the target and swaps it over with
`os.replace`. When that swap fails (the file is open in Acrobat, read-only,
held by a sync client, or open in a second Rapid PDF window), the new content is
salvaged as `<name>.pdf.bak` so no work is lost. That part is right.

The salvage was **silent**. `save()` returned False, the window said "Could not
save the PDF", and `self.path` was left naming the original. Four things then
disagreed: the live document was the `.bak`, the title bar and the tab named the
original, the original on disk still held the old content, and the next Save
would write to whichever of them the path said. The user had every reason to
believe their edits were in the file they opened.

**What was chosen, and why.** Both ends are closed rather than one:

- **The `.bak` is adopted as the document's path.** The live document IS that
  file, so letting `path` name the original is exactly what makes the app
  describe a file it is not holding. Everything downstream follows the path, so
  the title bar and the tab now read `drawing.pdf.bak` and the next Save writes
  there. The label being ugly is the point: it is visible.
- **`PDFDocument.last_save_error` carries the reason** and the window shows it
  verbatim instead of "Could not save the PDF". It names both files and says
  what to do (close whatever is holding the original, then Save As over it).

The alternative considered and rejected was to keep `path` on the original and
just warn. That leaves the divergence in place and relies on the user reading
one dialog; the next Save would then attempt the same doomed swap on a document
whose content had already moved.

The dirty flag is untouched, because `save()` still returns False, so the
document is still unsaved as far as every close path is concerned and Save As
is the escape hatch. `tests/test_save_failure.py` pins the wording, the adopted
path, and that the original on disk is left byte-for-byte as it was.

## Memory and cost

Each open document holds up to three `fitz` documents:

1. the live document (`_doc`),
2. `_org_render`, the Organizer's markup-baked clone,
3. `_panel_render`, the left panel's markup-baked clone.

Both clones come from `_make_markup_baked_render` (`ui/main_window.py:844`),
which does a whole-document `insert_pdf` into a fresh doc
(`clone_with_annotations`, `core/pdf_document.py:376`). A clone is a full copy
of the page content, not a reference.

On top of that, every `PDFDocument` carries an LRU pixmap cache bounded to six
entries (`RENDER_CACHE_MAX = 6`, `core/pdf_document.py:34`). In practice only
the live document's cache fills: the canvas is the only caller of
`render_page_cached` (`ui/canvas.py:1595` and `:1828`), and the two clones are
read through `render_thumbnail`, which bypasses the cache entirely.

One A1 page at the default zoom of 1.5 is 1684 x 2384 pt, so 2526 x 3576
pixels, so 9.03 million pixels. As a 32-bit `QPixmap` that is **about 36 MB**.
(The `QImage` intermediate is `Format_RGB888`, `core/pdf_document.py:140`, so
roughly 27 MB before conversion.) Six of those is around 216 MB **for one
document**.

Ten tabs of A1 drawings could approach a gigabyte.

### Measured with tabs actually open (phase 2)

Numbers, not arithmetic. Measured on this install with the Windows working set
(`GetProcessMemoryInfo`), A1 portrait at 1684 x 2384 pt.

| Measured | Value |
| --- | --- |
| one A1 page cached at zoom 1.0 | 1684 x 2384 px, 32-bit, **15.3 MB** |
| one A1 page cached at zoom 1.5 | 2526 x 3576 px, 32-bit, **34.5 MB** |
| one document's full render cache (6 entries, zoom 1.5) | **207 MB** |
| ten documents' full render caches | **2.02 GB** |
| ten tabs open, one page viewed in each, default fit | **+345 MB** (about 35 MB a tab) |
| ten live fitz documents PLUS twenty markup-baked clones, no renders | **+2 MB** |

The estimate above was right about the ceiling and **wrong about where the
weight is**, which changes what phase 3 should do first.

**The clones are nearly free. The pixmap cache is the whole cost.** Ten live
documents and their twenty clones came to two megabytes, because
`clone_with_annotations` copies page content streams and a drawing is vector
data. That cost scales with the FILE, so a 20 MB drawing clones to roughly
20 MB, and even then it is an order of magnitude under the 207 MB of pixmaps
that one document's render cache holds regardless of how big the file is.

So the phase 3 rule to write first is **drop a backgrounded document's pixmap
cache** (`invalidate_render_cache`, `core/pdf_document.py:88`), which is one
call, cannot fail, and recovers 200 MB a tab. Releasing the render clones is
still worth doing and it is still where the second-switch bug lives (known bug
6), but on these numbers it is the smaller half of the job, not the headline.

**Background tabs must release their render clones, and that is phase 3 work,
not later.** The rules, all now implemented in `DocumentView.set_active`:

- A backgrounded `DocumentView` closes `_org_render` and `_panel_render` and
  rebuilds them on activation. The closers already exist (`:873`, `:901`) and
  the rebuilds are already idempotent.
- A backgrounded document's pixmap cache gets dropped via
  `invalidate_render_cache` (`core/pdf_document.py:88`).
- The live `fitz` document and the canvas scene stay. Those are what make a
  tab switch instant and what finding 2 proved survive a move.

### Measured after phase 3 shipped it

`tools/measure_tab_memory.py`, six A1 tabs, every page turned to in each so
every cache is full, Windows working set, two subprocesses so the two
configurations cannot contaminate each other:

| | Growth over baseline |
| --- | --- |
| holding everything, as before phase 3 | **+1249 MB** |
| releasing on background | **+387 MB** |
| saved | **863 MB, about 173 MB a backgrounded tab** |

**173 and not 207, and the difference is the useful part.** The canvas scene
holds the page currently on screen as its background item, and `QPixmap` is
implicitly shared, so the cache entry for that page and the scene item are the
same memory. Dropping the cache frees the five entries nobody else is holding
and leaves the sixth alone.

**The corollary caught the first attempt at this measurement, so write it
down.** A tab where only ONE page has ever been rendered saves NOTHING: its one
cache entry is the page the scene is holding. Ten tabs each opened and left on
page 1 measured +355 MB with the release and +355 MB without it. The saving is
real for the case it is about, which is someone paging through a set of
drawings, and it is zero for someone who opens ten files and looks at the first
page of each. Both are worth knowing; the second is why "recovers 200 MB a tab"
was too strong as written.

## What comes along with a page when it moves between documents

Measured with `insert_pdf` on PyMuPDF 1.27.2.3, copying one page out of a
3-page source into an empty document.

| Travels? | What | Detail |
| --- | --- | --- |
| Yes | PDF annotations | a Square annot on the source page arrived intact |
| **No** | unsaved rapid-pdf markup | it is Qt scene items, not in the file yet |
| **No** | lifted images | `_item_to_json` returns `None` for `type == "image"` (`ui/canvas.py:1224-1225`) |
| **No** | internal GOTO links | source link to page 2 became `links: []`, silently, no error |
| **No** | layers (OCG) | source `{5: {'name': 'LayerA', ...}}` became `{}` |
| Yes, renamed | form fields | inserting the same page twice gave `['name1', 'name1 [17]']`, silently, and the rename survives save |
| Invalidated | digital signature on the target | `save()` always rewrites the whole file |

Consequences for phase 5:

**Unsaved markup has to be transported as JSON.** The mechanism already
exists: `_item_to_json` (`ui/canvas.py:1216`) and `_item_from_dict` (`:1250`),
with `export_annotation_model` (`:1241`) and `load_annotation_model` (`:1295`)
as the whole-document wrappers. A page move must serialise the source page's
items, carry them in the drag payload, and rebuild them on the destination
canvas. It cannot rely on `insert_pdf` for this.

**Lifted images must be baked first.** `_item_to_json` deliberately skips
images because they are baked into page content on save and recovered by the
embedded-image lift feature. That means the JSON path loses them. A page
carrying live image items has to have those baked into the source page before
the transfer, or they vanish.

**Internal links and layers are lost quietly.** No exception, no warning. The
docstring on `extract_pages` (`core/pdf_document.py:422`) already admits this
for the undo stash. If it needs to be visible to the user, that is a status
message we write, because PyMuPDF will not tell us.

**Duplicate form field names are silently renamed.** `name1` becomes
`name1 [17]` (the xref number appended). Any downstream tool reading those
fields by name breaks. Worth a warning if we ever see a moved page carrying
widgets.

**Signatures are already invalidated by every save today.** `save()`
(`core/pdf_document.py:157`) always writes a full rewrite with `garbage=4,
deflate=True` (lines 175 and 209), never incremental. Page moves do not make
this worse; they just make it more likely someone notices.

## What is not done

Everything on the phase list shipped. This is what a reader coming to it cold
should know is still missing or deliberately refused, so it is not rediscovered
as a bug.

**Deliberate, and settled. Do not "fix" these without reopening the decision.**

- **Pages cannot be dragged between WINDOWS.** Two windows are two undo stacks
  and there is no ordering of two stacks that undoes a cross-document move
  without leaving a duplicate page. A whole document moves between windows
  fine; a page does not. Refused at drop time.
- **A document moved to another window loses its undo history.** `QUndoStack`
  has no selective removal, so `release_view` drops the whole stack, and only
  when the departing view is actually named in it. An unedited tab, which is
  the ordinary tear-off, costs nothing.
- **Untitled and merged documents are not restored**, and **unsaved markup is
  not restored**. See phase 6 above for both, including why crash recovery is a
  separate feature rather than a flag on this one.
- **`Ctrl+F4` does nothing.** See known bug 3.
- **`view.organizer_zoom_index` has no Preferences control.** Ctrl+wheel in the
  Organizer is a better way to do the same thing.
- **`close.confirm_multiple_tabs` still defaults to true.** The reasoning is at
  the end of phase 6.

**Genuinely outstanding.**

- **Nothing has been released.** Version is still 1.5.0 and no installer has
  been cut for any of this. See Standing constraints.
- **`docs/architecture.md` and `docs/file-structure.md` still describe the
  single-document app.** `architecture.md:83` still says `MainWindow` "builds
  the two tabs", which now means the Editor/Organizer switcher inside a
  `DocumentView`. `README.md` and `docs/shortcuts.md` were brought up to date
  in phase 6; those two were not.
- **A cross-document page move silently loses internal links and layers.** It
  is REPORTED (the status line names what went), not fixed, because PyMuPDF
  drops them inside `insert_pdf` and there is no hook. Same for form fields
  being renamed, and for signatures, which every save already invalidates.
- **Session restore does not remember the Editor/Organizer switcher.** A tab
  restored into the Organizer comes back in the Editor. Nobody has asked for
  it; it would be a fifth field on the tab record.
- **The multiple-tabs close warning counts restored tabs that were never
  opened.** That is on purpose (the warning is about losing your place) but it
  means the count in the dialog can be higher than the number of documents the
  app has actually read.

## Standing constraints

- **No new version is published until the whole set of adjustments lands.**
  Lucas, 2026-08-29. Phases can merge to `main` as they finish, but nothing
  gets a release tag or an installer until the set is done. **The set is now
  done, so this constraint is what is left to act on: the release is the next
  decision, not a phase.**
- **Phase 1 goes in a worktree, under a strict move-don't-change rule.** Move
  code, do not improve it. Improvements are a separate commit after the tests
  are green. (Held. Phase 1 landed with no behaviour change.)
- **The 201 existing tests must pass unmodified through phase 1.** A test that
  needs an edit means behaviour moved, not just code. Stop and find out why
  before editing the test. (Held. The only tests rewritten across the whole
  set were the six in phase 5 that pinned the per-document undo stack, and one
  in phase 6 that pins the schema's shape on purpose. None of them found a
  bug; each was pinning a contract that had deliberately changed.)
- **Do not put an OpenGL viewport on the canvas** without re-running the
  finding 2 reparent test. See the constraint box in that section.
