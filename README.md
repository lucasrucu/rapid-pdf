# rapid-pdf

A fast, focused desktop PDF editor for page management and visual markup. No OCR, no form-field scanning, no wait.

## What it is

Acrobat runs OCR and form-field detection every time it opens a file, which makes large technical PDFs slow to work with. rapid-pdf does only the two things that matter for field work, reorganizing pages and adding markup, and does them instantly. Open an A1 engineering drawing, move or delete pages, drop highlights and shapes, and save, all without the wait.

Built in Python with PySide6 (Qt6) and PyMuPDF. Single window, dark theme, Windows-first.

## Key features

- **Page manager**: open, combine, reorder, delete, and add pages from a thumbnail grid.
- **Markup tools**: highlight, rectangle, and line annotations with an Office-style color picker, opacity presets, and line weights.
- **Object editing**: select, move, resize with 8-point handles, Ctrl+drag to duplicate, marquee group-select, copy/paste, and full undo/redo.
- **Embedded-image lift**: grab an image baked into the page and move or resize it like any other object, with no white hole left behind.
- **Text in shapes**: double-click any shape to add auto-fitting text.
- **Faithful saves**: markup is written as PDF-spec annotation objects on top of the original page. Nothing is re-encoded, resized, or clipped, and existing annotations are preserved.
- **Editable round-trip**: a document saved by rapid-pdf reopens with its objects still movable and editable (the model travels embedded in the PDF).

## Quickstart

```bash
pip install -r requirements.txt
python main.py            # or: python main.py path/to/file.pdf
```

On Windows you can also run `run.bat`, which uses the bundled `.venv`.

## Architecture

```mermaid
flowchart TD
    A[main.py] --> B[MainWindow]
    B --> E[Editor tab]
    B --> O[Organizer tab]
    E --> C[Canvas]
    E --> T[Toolbar]
    E --> P[PagePanel]
    C --> D[core/PDFDocument]
    O --> D
    P --> D
    D --> F[PyMuPDF / fitz]
    F --> G[(PDF file)]
```

`MainWindow` owns the lifecycle and two tabs. The **Editor** holds the page panel (thumbnail strip), the canvas (the annotation surface), and the toolbar. The **Organizer** is a drag-to-reorder page grid. Everything reads and writes through `core/PDFDocument`, a thin wrapper over PyMuPDF that owns rendering, the page cache, saves, and the embedded annotation model.

See [Architecture](docs/architecture.md) for the full walkthrough.

## File structure

```
rapid-pdf/
├── main.py            # entry point: builds the app, opens a CLI-passed PDF
├── core/
│   └── pdf_document.py  # PyMuPDF wrapper: render cache, save lifecycle, annotation model
├── ui/
│   ├── main_window.py   # window, menus, tabs, save/open lifecycle, dirty-state
│   ├── canvas.py        # the editor: annotation items, undo stack, image lift, marquee
│   ├── toolbar.py       # tools and contextual color/opacity/weight controls
│   ├── organizer.py     # page reorder / delete / add grid
│   └── page_panel.py    # left-hand thumbnail strip
├── docs/              # architecture, performance, UI, build, shortcuts, PRD
├── prototypes/        # throwaway UI restyle preview (not shipped)
├── requirements.txt   # pymupdf, PySide6
└── run.bat            # Windows launcher using the bundled .venv
```

Full annotated tree: [File structure](docs/file-structure.md).

## Tech stack

[Python](https://www.python.org/) 3.11+ · [PySide6](https://doc.qt.io/qtforpython/) (Qt6) · [PyMuPDF](https://pymupdf.readthedocs.io/) (fitz)

## Documentation

- [Architecture](docs/architecture.md): modules, coordinate system, save lifecycle, image-lift pipeline.
- [File structure](docs/file-structure.md): annotated tree of every file and its role.
- [Performance & rendering](docs/performance.md): page cache, lazy thumbnails, debounce/settle, save integrity.
- [UI direction](docs/ui.md): styling options and the recommended path.
- [Build & packaging](docs/build.md): freezing to an installable Windows app.
- [Keyboard & mouse shortcuts](docs/shortcuts.md): every key and gesture.
- [Product requirements](docs/PRD.md): the problem, target user, and feature scope.
