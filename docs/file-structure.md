# rapid-pdf: file structure

An annotated tree of the repository, with a sentence on each file's role. For how the modules fit together, see [architecture.md](architecture.md).

```
rapid-pdf/
├── main.py
├── run.bat
├── requirements.txt
├── smoke_test.py
├── core/
│   ├── __init__.py
│   └── pdf_document.py
├── ui/
│   ├── __init__.py
│   ├── main_window.py
│   ├── canvas.py
│   ├── toolbar.py
│   ├── organizer.py
│   └── page_panel.py
├── prototypes/
│   └── ui_preview.py
└── docs/
    ├── architecture.md
    ├── file-structure.md
    ├── performance.md
    ├── ui.md
    ├── build.md
    ├── shortcuts.md
    ├── PRD.md
    ├── AUDIT-2026-06-26.md
    ├── NATIVE-CONTENT-OBJECTS.md
    └── VISIO-IMAGE-DIAGNOSIS.md
```

## Entry points

| Path | Role |
|---|---|
| `main.py` | Application entry point. Creates the `QApplication`, applies the Fusion style, builds `MainWindow`, and opens a PDF passed as a command-line argument. |
| `run.bat` | Windows launcher. Runs `main.py` with the bundled `.venv` Python so you can double-click to start. |
| `requirements.txt` | The two runtime dependencies: `pymupdf` and `PySide6`. |
| `smoke_test.py` | Headless smoke test. Runs the app offscreen against a test PDF to catch import and startup breakage. |

## `core/`: document layer

| Path | Role |
|---|---|
| `core/__init__.py` | Package marker. |
| `core/pdf_document.py` | `PDFDocument`, the one wrapper over PyMuPDF (`fitz`). Owns the live document, the LRU page-pixmap render cache, the integrity-first save lifecycle, structural page ops (reorder/delete/insert), the editable embedded-JSON annotation model, markup writing, and the no-hole `remove_image_placement` image-lift primitive. |

## `ui/`: interface layer

| Path | Role |
|---|---|
| `ui/__init__.py` | Package marker. |
| `ui/main_window.py` | `MainWindow`, the application shell. Builds the Editor and Organizer tabs, the menus, and the status bar; owns open/combine, save/save-as, close, and quit, the unsaved-changes prompt, the dirty/untitled title state, and the flush → save → strip coordination. Also keeps the page panel and organizer thumbnails in sync via markup-baked clones. |
| `ui/canvas.py` | `PDFCanvas` (a `QGraphicsView`) plus every annotation item class (`HighlightItem`, `RectAnnotationItem`, `ImageAnnotationItem`, `LineAnnotationItem`, `TextAnnotationItem`) and the undo-command classes. The editing surface: drawing, selection, move/resize handles, marquee, copy/paste, Ctrl+drag duplicate, undo/redo, the embedded-image lift, and the JSON model round-trip. The largest module. |
| `ui/toolbar.py` | `ToolBar` and `ColorToolButton`. The right-hand tool rail: Select/Rectangle/Line/Text tools and the contextual style controls (Office-style fill and line color pickers with presets, recents, and line weights; text color and size; opacity presets) that show only when relevant. |
| `ui/organizer.py` | `PageOrganizer`, the page-management grid. Native drag-to-reorder, delete-selected, and add-pages-from-PDF, with lazily rendered thumbnails pulled from a markup-baked clone. |
| `ui/page_panel.py` | `PagePanel`, the narrow left-hand thumbnail strip in the Editor. Click to switch page; thumbnails render lazily and stay in sync with unsaved overlays. |

## `prototypes/`: throwaway experiments (not shipped)

| Path | Role |
|---|---|
| `prototypes/ui_preview.py` | A standalone PySide6 window showing the current toolbar next to the proposed restyle. Supports the [UI direction](ui.md) decision and ships nothing into the app. |

## `docs/`: documentation

| Path | Role |
|---|---|
| `docs/architecture.md` | The architecture walkthrough: modules, coordinate system, save lifecycle, editable model, image-lift pipeline. |
| `docs/file-structure.md` | This file. |
| `docs/performance.md` | Rendering and performance: the page-pixmap cache and its invalidation contract, lazy thumbnails, the debounce/settle interaction model, the save lifecycle, measured impact, and proposals. |
| `docs/ui.md` | UI direction: the realistic options for a more professional, glassy look and the recommended path (refined QSS + icons, optional Win11 Mica). |
| `docs/build.md` | Build and packaging research: turning the app into an installable Windows application (PyInstaller onedir + Inno Setup, signing notes). Research only, nothing built yet. |
| `docs/shortcuts.md` | The full keyboard and mouse shortcut reference. |
| `docs/PRD.md` | Product requirements: the problem, target user, inspiration, and feature scope. |
| `docs/AUDIT-2026-06-26.md` | An adversarial code audit of the perf, image, and save-integrity paths, with findings verified against the source. |
| `docs/NATIVE-CONTENT-OBJECTS.md` | Migration plan and history for writing objects as native page content vs annotations, and the cross-app editability goal. |
| `docs/VISIO-IMAGE-DIAGNOSIS.md` | The diagnosis and fix for moving images from Visio / automation PDFs (rotated pages, automation-stamped images), the deep background behind the image-lift approach. |

## Not in version control

The working tree also contains a `.venv/` (the bundled virtual environment), `__pycache__/` directories, and local scratch files (test PDFs, captured stdout/stderr). These are runtime or build artifacts, not part of the source.
