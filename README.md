# rapid-pdf

A fast, focused PDF editor for page management and visual markup — no OCR, no form-field scanning, no wait.

## Why

Acrobat runs OCR and form-field detection every time it opens a file, which makes large technical PDFs slow to work with. rapid-pdf does only the two things that matter for field work — reorganizing pages and adding markup — and does them instantly.

## Features

- **Page manager** — open, combine, delete, and reorder pages from a thumbnail panel
- **Markup tools** — highlight, rectangle, and line annotations with a custom RGBA color picker and opacity control
- **Object editing** — select, move, resize (8-point handles), Ctrl+drag to duplicate, marquee group-select, copy/paste, undo/redo
- **Text in shapes** — double-click any shape to add auto-fitting text
- **Faithful saves** — annotations are written as PDF-spec objects on top of the original page; nothing is re-encoded, resized, or clipped, and existing annotations are preserved

## Tech stack

Python 3.11+ · [PySide6](https://doc.qt.io/qtforpython/) (Qt6) · [PyMuPDF](https://pymupdf.readthedocs.io/) (fitz)

## Getting started

```bash
pip install -r requirements.txt
python main.py            # or: python main.py path/to/file.pdf
```

On Windows you can also just run `run.bat`, which uses the bundled `.venv`.

## Documentation

- [Keyboard & mouse shortcuts](docs/shortcuts.md)
- [Product requirements & architecture](docs/PRD.md)
