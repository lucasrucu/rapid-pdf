# Graph Report - rapid-pdf  (2026-06-27)

## Corpus Check
- 15 files · ~17,160 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 441 nodes · 835 edges · 23 communities (17 shown, 6 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 10 edges (avg confidence: 0.65)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `1d4ce621`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]

## God Nodes (most connected - your core abstractions)
1. `PDFCanvas` - 73 edges
2. `MainWindow` - 45 edges
3. `PDFDocument` - 22 edges
4. `ToolBar` - 21 edges
5. `AnnotationItem` - 19 edges
6. `ColorToolButton` - 17 edges
7. `LineAnnotationItem` - 16 edges
8. `PageOrganizer` - 16 edges
9. `AnnotationBase` - 15 edges
10. `TextAnnotationItem` - 15 edges

## Surprising Connections (you probably didn't know these)
- `MainWindow` --uses--> `PDFDocument`  [INFERRED]
  ui/main_window.py → core/pdf_document.py
- `main()` --calls--> `MainWindow`  [EXTRACTED]
  main.py → ui/main_window.py
- `MainWindow` --uses--> `PDFCanvas`  [INFERRED]
  ui/main_window.py → ui/canvas.py
- `MainWindow` --uses--> `PageOrganizer`  [INFERRED]
  ui/main_window.py → ui/organizer.py
- `MainWindow` --uses--> `PagePanel`  [INFERRED]
  ui/main_window.py → ui/page_panel.py

## Import Cycles
- None detected.

## Communities (23 total, 6 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (15): QColor, QGraphicsLineItem, QGraphicsTextItem, QUndoStack, AnnotationBase, HighlightItem, LineAnnotationItem, Recolor just the outline, leaving any fill untouched. (+7 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (17): main(), QMainWindow, MainWindow, Insert each PDF's pages at the end of the current doc. Returns pages added., Copy the selected annotations into the in-app clipboard., Paste in-app copied annotations if any, else fall back to a clipboard image., Paste a clipboard image (from Word, a screenshot, etc.) as a movable object., Close the current document so the next Open starts fresh instead of appending. (+9 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (19): QListWidget, QListWidgetItem, QStyledItemDelegate, _DragList, PageOrganizer, Grid view for reviewing and reordering PDF pages., Draw each page thumbnail with its 'Page N' label centred BELOW it.      The li, doc = live document (edited in place). render = optional doc whose pages (+11 more)

### Community 3 - "Community 3"
Cohesion: 0.10
Nodes (11): QFrame, QLabel, QToolButton, QWidget, ColorToolButton, A small square chip used as the face icon of a color dropdown., An Office-style dropdown: a labeled button whose menu offers a color grid,, Reflect the selected object(s) in the controls without emitting signals. (+3 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (13): QUndoCommand, _Command, geometry_restore(), MoveCommand, NudgeCommand, # NOTE: in this PyMuPDF build transformation_matrix is only a y-flip (no, Capture every style/text attribute an annotation item might carry., Base for canvas edits. The edit is already applied live when the command is (+5 more)

### Community 5 - "Community 5"
Cohesion: 0.11
Nodes (9): QGraphicsRectItem, QPainter, QPixmap, QRectF, AnnotationItem, ImageAnnotationItem, Render the current page AS SHOWN (background + live overlays) to a thumbnail., Clipboard image rendered inside a resizable rect. (+1 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (25): Architecture, Canvas navigation, Feature requirements, Inspiration, Key design decisions, Keyboard shortcuts, Module 1 — Page Manager, Module 2 — Markup Editor (+17 more)

### Community 7 - "Community 7"
Cohesion: 0.11
Nodes (6): QGraphicsView, PDFCanvas, Fold each style property across the selection (value if all agree, else None)., Apply fn to each item and record one undoable StyleCommand., Outline color for rectangles / color for lines (the 'Line' control)., Fill color for rectangles (the 'Fill' control). Picking a color enables fill.

### Community 8 - "Community 8"
Cohesion: 0.11
Nodes (7): PDFDocument, Reorder pages so that new page i is the page currently at new_order[i]., Return a throwaway fitz.Document copy with the given markup baked in., Embed the editable annotation model as a JSON file inside the PDF.          Re, Return the embedded editable annotation model, or None if absent., Strip rapid-pdf's baked annotations from a page.          Used on open so reco, Replace all rapid-pdf-tagged annotations on this page with the given list.

### Community 9 - "Community 9"
Cohesion: 0.20
Nodes (7): QLineF, QPointF, geometry_snapshot(), Rebuild a canvas item from its JSON form (inverse of _item_to_json)., Return (xref, fitz.Rect) of the smallest embedded image under scene_pos, or None, Convert an embedded image into a movable/resizable object.          Removes on, True if an annotation item overlaps rect (touch/intersect semantics).

### Community 10 - "Community 10"
Cohesion: 0.12
Nodes (7): Re-render the current page (e.g. after stripping baked markup on open)., After a save, image objects have been baked into the page content stream., Apply a full page permutation: new page i holds old page new_order[i]., Force any debounced page render to happen now — before hit-testing,         thu, Position a continuous-scroll page turn at the top/bottom of the new page., Drop any in-progress marquee/drag so a page or tool switch starts clean., Switch to a page. Navigation debounces the heavy render so fast paging

### Community 11 - "Community 11"
Cohesion: 0.17
Nodes (4): AddItemsCommand, Add an item to the scene and its page's annotation list (idempotent)., Remove an item from the scene and its page's annotation list., Paste copied annotations onto the current page, offset slightly, and select them

### Community 12 - "Community 12"
Cohesion: 0.25
Nodes (7): Feasibility — PROVEN headless (2026-06-26), Hard parts / risks (why we phase it, not big-bang), Native content objects — migration plan, Phased plan, Recommendation, Target architecture — unify everything under the existing "lift" pattern, Why the change (the diagnosis behind it)

### Community 13 - "Community 13"
Cohesion: 0.25
Nodes (7): Known trade-offs (intentional), Moving images from Visio/automation PDFs — diagnosis & fix, The fix (this branch), The real cause (after correcting a wrong first pass), The rotation crop bug (the visual breakage), Validation (headless, against the real rotated file), What's in the file (measured)

### Community 14 - "Community 14"
Cohesion: 0.29
Nodes (6): Confirmed but NOT fixed here — needs a design decision, Confirmed NOT bugs (checked, to save the next reviewer time), Fixed in this branch (`feature/audit-fixes`) — verified, Image accumulation / double-render (CRITICAL) — all three reviewers flagged it, Other findings (backlog — not blocking), rapid-pdf audit — 2026-06-26

## Knowledge Gaps
- **36 isolated node(s):** `Why`, `Features`, `Tech stack`, `Getting started`, `Documentation` (+31 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `PDFCanvas` connect `Community 7` to `Community 0`, `Community 1`, `Community 4`, `Community 5`, `Community 8`, `Community 9`, `Community 10`, `Community 11`, `Community 15`, `Community 16`, `Community 17`, `Community 18`, `Community 19`, `Community 20`?**
  _High betweenness centrality (0.346) - this node is a cross-community bridge._
- **Why does `MainWindow` connect `Community 1` to `Community 8`, `Community 2`, `Community 3`, `Community 7`?**
  _High betweenness centrality (0.225) - this node is a cross-community bridge._
- **Why does `PDFDocument` connect `Community 8` to `Community 1`, `Community 5`?**
  _High betweenness centrality (0.087) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `MainWindow` (e.g. with `PDFDocument` and `PDFCanvas`) actually correct?**
  _`MainWindow` has 5 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Reorder pages so that new page i is the page currently at new_order[i].`, `Return a throwaway fitz.Document copy with the given markup baked in.`, `Embed the editable annotation model as a JSON file inside the PDF.          Re` to the rest of the system?**
  _110 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.06078316773816481 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.07127882599580712 - nodes in this community are weakly interconnected._