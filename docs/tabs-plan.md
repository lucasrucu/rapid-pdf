# rapid-pdf: document tabs and multi-window

Design record for the tabs work. Agreed with Lucas on 2026-08-29. Every file
and line reference below was read out of the source at commit `a198784`
(v1.5.0) and every Qt behaviour claim was run on this install, not taken from
documentation.

This is the plan, not a log. If you are picking it up cold, read "Current
state" and "The three findings" first. Those are the parts that stop you
rediscovering a week of work.

## The goal

Adobe-style tabs.

1. Several PDFs open in one window, one tab each.
2. Pages draggable from one tab into another, the way they already reorder
   inside a document today.
3. Tabs draggable out of the window to make a second window.
4. A settings screen, including whether the X button closes the window or just
   closes the document.

## Current state

Verified, not assumed.

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
| 0 | Settings module plus close rework | 1.5 d | in progress, `feat/settings-and-close` |
| 1 | Extract `DocumentView` from `MainWindow` | 2-3 d | **riskiest phase**, done on `refactor/document-view` |
| 2 | Tabs in one window | 1.5-2 d | |
| 3 | Multi-window via `WindowRegistry`, menu item only | 1.5-2 d | |
| 4 | Tear-off drag gesture | 1-1.5 d | |
| 5 | Pages between tabs | 4 d | |
| 6 | Session restore | 0.5-1 d | |

The order is not arbitrary. Each phase leaves the app shippable, and phases 3
and 4 are split specifically so the hard, testable half lands before the
gesture half.

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

`QTabBar` plus `QStackedWidget` of `DocumentView`s. Open a second file, get a
second tab. Middle-click closes (finding 3). `open_paths` stops appending.
`handle_cli_files` opens one tab per path instead of merging.

### Phase 3: multi-window, menu-driven

A `WindowRegistry` that tracks every open `MainWindow`, owns app lifetime
(quit when the last one goes), and moves a `DocumentView` from one window to
another. Driven by a **"Move to New Window" menu item**. No dragging in this
phase.

This is the key insight of the whole plan: **phase 3 builds every mechanism
the tear-off needs, and it is fully testable headless.** Window creation,
adoption of a live view, undo-stack handover, source-window teardown, close
semantics with N windows open, app-quit rules. A menu item can be driven by a
test; a mouse gesture across two top-level windows mostly cannot.

The payoff is that phase 4 becomes a thin gesture layer that can be switched
off without losing multi-window. If phase 4 is going badly, ship phase 3 and
the feature still works.

Background-tab memory release is phase 3 work, not later. See "Memory and
cost" below.

### Phase 4: the tear-off drag gesture

Roughly 400 lines in one file. It calls into phase 3's registry and does
nothing else. Detail in "Design decisions" below.

### Phase 5: pages between tabs

Drag a page from one document's panel or organizer into another's. Needs
`transfer_pages_from` (finding 1, about 40 lines) and a mime-type discipline
(see below).

Four days, and the undo/dirty refactor is the uncertain part, not the drag. A
cross-document move dirties two documents and has to undo as one action across
both. See the undo decision below.

### Phase 6: session restore

Reopen the tabs and windows that were open last time. Small, and it depends on
everything above being stable.

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

### 1. Windows shutdown is blocked by an unconditional `event.ignore()`

`ui/main_window.py:1122`, `closeEvent`. If a document is open and
`_force_quit` is false, the handler closes the document and then calls
`event.ignore()` at line 1140, unconditionally. Windows asks a process to
close on logout, restart and shutdown through exactly this path, so the app
refuses and the OS shows the "this app is preventing you from shutting down"
block. Needs a `QSessionManager` check, or at minimum to accept the close when
the app is being asked to quit rather than clicked.

### 2. Relaunch while running does nothing

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

### 3. Ctrl+Q, Ctrl+W and Ctrl+F4 are all dead keys

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

### 4. Encrypted PDFs report a successful open, then throw on first render

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

### 5. Deleting a page leaves a bookmark pointing at page -1

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

### 6. The SECOND switch into the Organizer throws, and loses the clone

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

Two candidate fixes, neither taken in phase 1 (move-don't-change): clear the
Organizer's render source before closing the clone
(`set_document(self._doc, None)` first), or build the new clone before closing
the old one and swap. **This matters for phase 3**, where backgrounded tabs
release their render clones and rebuild them on activation: that is exactly
this sequence, run far more often.

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

**Background tabs must release their render clones, and that is phase 3 work,
not later.** The rules:

- A backgrounded `DocumentView` closes `_org_render` and `_panel_render` and
  rebuilds them on activation. The closers already exist (`:873`, `:901`) and
  the rebuilds are already idempotent.
- A backgrounded document's pixmap cache gets dropped via
  `invalidate_render_cache` (`core/pdf_document.py:88`).
- The live `fitz` document and the canvas scene stay. Those are what make a
  tab switch instant and what finding 2 proved survive a move.

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

## Standing constraints

- **No new version is published until the whole set of adjustments lands.**
  Lucas, 2026-08-29. Phases can merge to `main` as they finish, but nothing
  gets a release tag or an installer until the set is done.
- **Phase 1 goes in a worktree, under a strict move-don't-change rule.** Move
  code, do not improve it. Improvements are a separate commit after the tests
  are green.
- **The 201 existing tests must pass unmodified through phase 1.** A test that
  needs an edit means behaviour moved, not just code. Stop and find out why
  before editing the test.
- **Do not put an OpenGL viewport on the canvas** without re-running the
  finding 2 reparent test. See the constraint box in that section.
