# rapid-pdf — Product Requirements Document

## Problem
Adobe Acrobat is slow on large, complex PDFs because it runs OCR and form-field detection every time a file is opened. The two workflows that matter — page management and visual markup — don't need any of that. rapid-pdf does those two things only, and does them fast.

## Target user
Field engineers who regularly annotate multi-page technical PDFs (reports, annexes, drawings) and need to move, delete, or merge pages and add color markup without waiting for a 10-second load.

## Inspiration
- **Bluebeam Revu** — object interaction model, Ctrl+drag to duplicate, smooth canvas
- **Adobe Acrobat** — annotation types and PDF compatibility

---

## Feature requirements

### Module 1 — Page Manager
| Feature | MVP | Phase 2 |
|---|---|---|
| Open a PDF | ✓ | |
| Page thumbnail panel (left sidebar) | ✓ | |
| Switch pages by clicking thumbnail | ✓ | |
| Delete current page | ✓ | |
| Insert pages from another PDF | ✓ | |
| Drag-reorder pages in thumbnail panel | | ✓ |
| Multi-page view (scroll all pages) | | ✓ |

### Module 2 — Markup Editor
| Feature | MVP | Phase 2 |
|---|---|---|
| Highlight tool — draw filled semi-transparent rect | ✓ | |
| Rectangle tool — stroked rect with optional fill | ✓ | |
| Select tool — click to select, drag to move | ✓ | |
| Ctrl+drag to duplicate any annotation | ✓ | |
| Delete selected annotation (Delete key) | ✓ | |
| Custom RGBA color picker | ✓ | |
| Opacity slider (5–100%) | ✓ | |
| Preset color swatches | ✓ | |
| Line tool (Shift-constrained to horizontal/vertical) | ✓ | |
| Text inside shapes — double-click to add/edit; auto-fits font to shape | ✓ | |
| Resize handles on selected annotation (8-point drag) | ✓ | |
| Fill toggle for Rectangle tool (uses current color at 40% opacity) | ✓ | |
| Undo/redo stack (Ctrl+Z / Ctrl+Y) | ✓ (done 2026-06-16) | |
| Marquee group-select (Bluebeam touch/intersect) + Shift-click additive | ✓ (done 2026-06-16) | |
| Group move (drag whole selection together) | ✓ (done 2026-06-16) | |
| Copy / paste annotations (Ctrl+C / Ctrl+V) | ✓ (done 2026-06-16) | |
| Arrow-key nudge of selection (Shift = 10px) | ✓ (done 2026-06-16) | |
| Group resize (proportional bounding-box scale) | | ✓ |
| Z-order undo (Bring to Front / Send to Back not yet on undo stack) | | ✓ |
| Align / distribute toolbar (left/center/right, distribute evenly) | | ✓ |
| Persistent grouping (group/ungroup across sessions) | | ✓ |
| Snap-to-grid / alignment guides | | ✓ |

### Canvas navigation
| Feature | MVP | Phase 2 |
|---|---|---|
| Ctrl+scroll to zoom (centered on cursor) | ✓ | |
| Scrollbars for panning | ✓ | |
| Space+drag to pan | | ✓ |

### Persistence
| Feature | MVP | Phase 2 |
|---|---|---|
| Save annotations to same PDF | ✓ | |
| Save As (copy to new path) | ✓ | |
| Annotations survive round-trip (visible in Adobe) | ✓ | |
| Existing PDF annotations preserved on save | ✓ | |

---

## Non-functional requirements
- Open a 200-page PDF in < 3 seconds (no OCR, no form processing)
- Zero internet dependency
- Packageable as a single Windows .exe (PyInstaller — Phase 3)
- **PDF fidelity on save** — source page dimensions and content are never altered. Annotations are written as PDF-spec objects on top of the original page; nothing is re-encoded, resized, or clipped. PDFs with non-standard page sizes (A0, custom landscape, mixed sizes) are opened and saved exactly as-is. The only data written is rapid-pdf's own annotation layer.

---

## Architecture

```
rapid-pdf/
├── main.py                 # Entry point; accepts optional path arg
├── requirements.txt
├── core/
│   ├── __init__.py
│   └── pdf_document.py     # PyMuPDF wrapper (open, save, render, page ops, write_annotations)
└── ui/
    ├── __init__.py
    ├── canvas.py           # PDFCanvas (QGraphicsView) — drawing, selection, zoom
    ├── toolbar.py          # ToolBar — tool buttons, color presets, opacity slider
    ├── page_panel.py       # PagePanel — thumbnail list
    └── main_window.py      # MainWindow — layout, menu, keyboard shortcuts, file ops
```

### Key design decisions
1. **Hybrid annotation model** — Annotations are `QGraphicsRectItem` subclasses during editing. On save, they are written to PyMuPDF as PDF-spec annotation objects tagged with `title="rapid-pdf"`. Existing (pre-existing) PDF annotations are untouched.
2. **No live sync** — Annotations are only written to the PDF document on Save. The rendered page pixmap is the background; canvas items are overlaid.
3. **Per-page annotation store** — `_page_annotations: dict[int, list[AnnotationItem]]` in the canvas. Items are shown/hidden on page switch without re-creating them.

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `V` | Select tool |
| `H` | Highlight tool |
| `R` | Rectangle tool |
| `L` | Line tool |
| `Delete` / `Backspace` | Delete selected annotation(s) |
| `Ctrl+O` | Open PDF |
| `Ctrl+S` | Save |
| `Ctrl+Shift+S` | Save As |
| `Ctrl+Z` / `Ctrl+Y` | Undo / Redo |
| `Ctrl+C` / `Ctrl+V` | Copy / Paste selected annotation(s) |
| `Ctrl+A` | Select all annotations on the page |
| `Ctrl+scroll` | Zoom in/out (centered on cursor) |
| `Shift` (while drawing) | Constrain to square |
| Drag on empty space | Marquee group-select (selects anything the box touches) |
| `Shift+click` | Add / remove an object from the selection |
| `Ctrl+drag` | Duplicate selection |
| Arrow keys (`Shift` = 10px) | Nudge selected annotation(s) |

---

## Tech stack
- **Python 3.11+**
- **PyMuPDF (fitz)** ≥ 1.24 — PDF rendering, page ops, annotation writing
- **PySide6** ≥ 6.7 — GUI (Qt6, LGPL license)
