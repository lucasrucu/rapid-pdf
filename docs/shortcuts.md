# Keyboard & mouse shortcuts

## Tools

| Key | Action |
|---|---|
| `V` | Select tool |
| `H` | Pan tool (hand) |
| `R` | Rectangle tool |
| `L` | Line tool |
| `T` | Text tool |

Pan moves the view and nothing else: with it active, nothing on the page can be
selected, moved or edited.

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
deleted: a document has to keep at least one. The strip carries no delete
button: select and press `Delete`, or use the right-click menu, or the
Organizer.

## Going to a page

| Key | Action |
|---|---|
| `Ctrl+G` | Jump to the page box in the status bar, number selected |
| Type a number, `Enter` | Go to that page (editor and Organizer both follow) |

The box sits at the right of the status bar with the page count beside it, and
tracks the page you are on as you scroll. A number past either end clamps to the
first or last page; an empty or abandoned box snaps back to the page on screen.
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
| `Ctrl+O` | Open PDFs, one tab each |
| `Ctrl+S` | Save |
| `Ctrl+Shift+S` | Save As |
| `Ctrl+W` | Close the current tab |
| `Ctrl+Q` | Quit |

Opening a second PDF gives it a second tab. It no longer appends its pages to
the document you are reading, which is what it did up to 1.5.0. To merge files,
ask for it by name: `File > Combine PDFs`, or `+ Add Pages` in the Organizer.

The window's X closes the window and everything in it. To have it close the
front PDF and leave the window up instead (which is what Rapid PDF did up to
1.5.0), pick the second choice under Closing in Edit > Preferences. Either way,
unsaved changes are prompted for first, once per document, and cancelling any
of those prompts cancels the whole close.

Open and Save As start in the last folder you used. Edit > Preferences can pin
them to one folder instead.

## Tabs

| Key | Action |
|---|---|
| `Ctrl+T` | New tab |
| `Ctrl+W` | Close the current tab |
| `Ctrl+PgDn` / `Ctrl+PgUp` | Next / previous tab by POSITION, wrapping |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` | Next / previous tab by RECENT USE |
| Middle-click a tab | Close it |
| Double-click empty bar space | New tab |
| Drag a tab sideways | Reorder it |
| Drag a tab off the bar | Tear it into its own window |
| Right-click a tab | Close, Close Others, Close to the Right, Move to New Window, Duplicate Tab, Copy Full Path, Open Containing Folder |

The two next/previous pairs are deliberately different orders. `Ctrl+PgDn` is
the tab to the right. `Ctrl+Tab` is the tab you were just in: hold Ctrl and keep
tapping Tab to walk further back through the ones you have visited, and let Ctrl
go to land on one. The list is frozen while Ctrl is down, so the walk keeps
going back instead of bouncing between the last two.

Dragging a tab DOWN off the bar tears it into its own window, which appears
under the cursor straight away and follows it. Drop it on another window's tab
bar and it docks there, at the gap the insertion line is showing. Drop it
anywhere else and it stays where you let go. Escape mid-drag puts it back where
it came from. Dragging sideways, however far, is only ever a reorder.

Closing the last tab closes the window. A tab with unsaved changes shows a dot
where its close button would be; hover it and the dot becomes the X again.

Tabs are named by the filename without `.pdf`, and two files with the same name
grow as much of their folder path as it takes to tell them apart. The full path
is in the tooltip. The chevron at the right of the bar lists every open
document, which is the answer when there are more tabs than fit.

Opening a file that is already open activates its tab rather than opening a
second copy of it.

Several documents open and none of them unsaved, and the X asks before closing
them all. Turn that off under Closing in Edit > Preferences. Unsaved documents
are always asked about, whatever that setting says.

## Preferences

| Key | Action |
|---|---|
| `Ctrl+,` | Edit > Preferences |

Everything the app remembers, on one page: the close behaviour above, the
theme, where file dialogs start, the page panel, and the page fit a document
opens in. There is no OK and no Cancel because every control applies as soon as
it is touched, which is what the theme and page panel already did from the View
menu. The dialog and the View menu are the same controls, so `Ctrl+B` and
`Ctrl+D` still work while it is open and it moves with them.

## Mouse

| Action | Result |
|---|---|
| Drag on empty space | Marquee group-select (selects anything the box touches) |
| `Shift+click` | Add / remove an object from the selection |
| `Ctrl+drag` | Duplicate selection |
| `Shift` while drawing | Constrain to square |
| Double-click a shape | Add or edit text inside it |
| `Ctrl+scroll` | Zoom in / out (centered on cursor) |
| Scroll past the bottom / top of a page | Turn to the next / previous page |
| Hold `Space` and drag | Pan, then back to the tool you were on |
| Middle-button drag | Pan, whatever the active tool is |
| Drag an image that is part of the page | Lifts it out and moves it (`Ctrl+Z` twice puts it back) |

The cursor says which it is: a hand only where a drag pans, an arrow or a move
cursor where a drag selects or moves something.

## View

The four icons at the right of the status bar are the view modes, one active at
a time, each naming itself on hover: fit page, fit width, fit height, and 100%
(actual size). Zooming by hand turns all four off, because the view is no longer
at any of them.

Trackpads scroll by the distance the fingers actually move, so two fingers cover
the same ground as the same gesture in any other app. Turning a page takes a
notch's worth of travel pushed against the edge, which is one click of a mouse
wheel or about 120px of finger, so a flick no longer runs through the document.
