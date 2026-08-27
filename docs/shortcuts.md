# Keyboard & mouse shortcuts

## Tools

| Key | Action |
|---|---|
| `V` | Select tool |
| `H` | Highlight tool |
| `R` | Rectangle tool |
| `L` | Line tool |

## Editing

| Key | Action |
|---|---|
| `Delete` / `Backspace` | Delete selected annotation(s) |
| `Ctrl+Z` / `Ctrl+Y` | Undo / Redo |
| `Ctrl+C` / `Ctrl+V` | Copy / Paste selected annotation(s) |
| `Ctrl+A` | Select all annotations on the page |
| Arrow keys (`Shift` = 10px) | Nudge selected annotation(s) |

## Pages (left thumbnail strip, and the Organizer)

| Action | Result |
|---|---|
| Click a thumbnail | Select that page and show it in the editor |
| `Shift+click` | Extend the selection to a run of pages |
| `Ctrl+click` | Add or remove a single page from the selection |
| `Ctrl+A` | Select every page (right-click menu, or with the strip focused) |
| `Delete` / `Backspace` | Delete the selected page(s) in one step |
| Right-click | Delete / select-all menu |
| Drag a thumbnail | Move it; the line shows where it will land |
| Drag a multi-selection | Moves the whole selection together, order kept |
| `Ctrl+Z` | Undo a page delete or move (same stack as annotations) |

Deleting is not confirmed, because it is undoable. The last page cannot be
deleted: a document has to keep at least one.

### Thumbnail zoom (Organizer only)

| Action | Result |
|---|---|
| `Ctrl+scroll` | Grow / shrink the page thumbnails, anchored under the cursor |
| `Ctrl++` / `Ctrl+=` | One step bigger (numpad `+` too) |
| `Ctrl+-` | One step smaller (numpad `-` too) |
| `Ctrl+0` | Back to the default size |

Seven steps from half size to double. Thumbnails are re-rendered at the new
size rather than stretched, so zooming in makes a page genuinely readable. The
level is remembered between runs. Plain scrolling is unaffected, and the left
strip keeps its own size.

## Files

| Key | Action |
|---|---|
| `Ctrl+O` | Open / combine PDFs |
| `Ctrl+S` | Save |
| `Ctrl+Shift+S` | Save As |

## Mouse

| Action | Result |
|---|---|
| Drag on empty space | Marquee group-select (selects anything the box touches) |
| `Shift+click` | Add / remove an object from the selection |
| `Ctrl+drag` | Duplicate selection |
| `Shift` while drawing | Constrain to square |
| Double-click a shape | Add or edit text inside it |
| `Ctrl+scroll` | Zoom in / out (centered on cursor) |
