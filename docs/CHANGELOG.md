# Changelog

Everything worth knowing about each release of RapidPDF. The shape follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the numbers follow
[semver](https://semver.org/spec/v2.0.0.html), which is also how
`core/update/release.py` decides whether a release is newer than the build you
are running.

Releases before 1.6.0 were written up on the
[Releases page](https://github.com/lucasrucu/rapid-pdf/releases) and are not
backfilled here.

## [1.8.1] - 2026-09-04

The shell registration repairs itself now. Two reports, one cause: the
association keys were written by the installer and by nothing else, so anything
that changed them behind its back stayed changed.

### Fixed

- **RapidPDF is back in the right-click "Open with" menu.** It disappeared when
  an uninstall ran by accident on 2 September against a stale uninstall log.
  Inno's own cleanup flags removed `Software\Classes\RapidPDF.Document` and the
  `RapidPDF.Document` value under `Software\Classes\.pdf\OpenWithProgids`, and
  those two between them are the entire reason the app appears in that menu. A
  ProgID can be perfectly formed and still be invisible; it is that one value
  in the shared `.pdf` key that lists you. Nothing put them back afterwards,
  because the in-app updater replaces files and never writes a registry value.
- **The entry is labelled "RapidPDF" again, not the old tagline.** Explorer
  takes the name from a per-user cache under `Shell\MuiCache` that is keyed by
  exe PATH. The 1.8.0 rename shortened the name in the exe but did not move the
  exe, so the cache went on serving the old spaced name with its tagline on a
  machine whose exe already said `RapidPDF`. The ProgID now states
  `FriendlyAppName` outright, which outranks the cache, and a stale cached name
  is cleared on launch.
- **PDF files keep looking like PDFs.** Choosing RapidPDF as the default
  handler paints the white page with the red band, not the gold app tile. This
  was reported twice and fixed twice before without ever reaching the machine
  that reported it: the fix shipped in 1.6.0, but 1.6.0 and 1.7.0 were both
  installed through the in-app updater, which writes no registry, so
  `DefaultIcon` went on pointing at the exe across two releases that contained
  the fix. Measured rather than assumed: with the key absent Explorer paints
  the app tile, so deleting it is not a neutral act and is not an option.

### Added

- **The app asserts its own shell registration on every launch**
  (`core/shell_registration.py`). It writes only what differs, runs in the
  primary instance only, and never touches `UserChoice`: which app opens a PDF
  stays a decision for the person using the machine, and Windows hash protects
  that value precisely so installers cannot take it.
- **A second route into the "Open with" list**, under
  `Software\Classes\Applications\rapid-pdf.exe` with `SupportedTypes`. The
  entry no longer depends on a single value in a shared key surviving, and
  Explorer can now resolve the bare exe name it keeps in its own
  `FileExts\.pdf\OpenWithList`.

## [1.8.0] - 2026-09-04

The tab strip release, and three crashes. The strip was rebuilt to read like a
browser's, closing a tab is something you can take back now, and the gesture of
dragging a tab out into its own window no longer kills the process.

### Added

- **Reopen a closed tab, `Ctrl+Shift+T`.** The last ten closures, held across
  every window rather than per window, so a tab closed in a window that has
  since been closed itself still comes back. It restores the file, the page you
  were on and the zoom you were at, through the same path session restore uses,
  which is the only one that converts a remembered zoom back out of the raster
  scale it was stored in. A file deleted since is skipped without a dialog.

### Changed

- **The app is called RapidPDF.** One word, everywhere it names itself: the
  Windows version resource Explorer reads in "Open with" and Task Manager, the
  installer, the Start-menu and desktop shortcuts, the entry in
  Settings > Default apps, both right-click verbs, and the window title that
  reaches the taskbar and Alt+Tab. Upgrading removes the old shortcuts and the
  old `Software\Rapid PDF` registration instead of leaving a second set beside
  the new one. Two things keep the old spelling on purpose: an existing install
  stays in the folder it is already in, and the settings directory
  (`%LOCALAPPDATA%\Rapid PDF\settings.json`) is an identity rather than a
  label, so renaming it would quietly abandon everyone's preferences and saved
  session.

- **The tabs look like tabs.** The strip was labels with an accent underline:
  no border, no corner radius, nothing between one tab and the next, and an
  active fill three values away from the background, so the only thing marking
  the current document was a two-pixel line. Every tab is now a closed rounded
  rectangle, all four corners, floating on the strip with a gap underneath it.
  The active one is the only one filled and bordered and the inactive ones are
  bare, which leaves the strip quiet until you look for the document you are in.
  The hairline that used to separate one tab from the next is gone: the space
  does that job now.

- **The close button sits in the middle of the tab.** It was one pixel from the
  right edge and two and a half pixels high, in every tab, and no amount of
  padding moved it. Qt's stylesheet style works the button's position out for
  itself, reads neither the padding nor the margin, and never hands the question
  on, so the widget is positioned directly now, from the three places that can
  change a tab's geometry. The tab's own margins were made symmetric at the same
  time, since the vertical half of the error came from Qt centring on the tab
  rectangle while the stylesheet drew the tab inset from it.

- **The new-tab button is next to the tabs.** It sat at the far right of the
  caption, against the window controls, with an empty lane between it and the
  strip it belongs to, because the tab bar was stretched across the whole row.
  The bar now hugs its tabs, so the button follows the last one. A deliberate
  gap is kept after that tab: it is the gap a browser leaves, and it is also
  the only surface the double-click-to-open-a-tab gesture has to land on.

- **The two strip controls line up.** The "all open documents" chevron had a
  fixed width and no fixed height, so it stretched to the full height of the
  row while the tab bar beside it sat at its own; and the plus carried two
  stray pixels of bottom padding nudging its glyph off centre. Both are now
  26 by 26 and vertically centred. The chevron has also moved to the left of
  the tabs, where Edge puts the same control, and it draws its own arrowhead
  instead of setting one as text: it was U+2304, a thinly covered codepoint
  that renders as an empty box wherever a font does not carry it.

- **You can see where a dragged tab is going to land again.** Earlier in this
  release the wash, the outline and the insertion line were taken out, on the
  reasoning that the tab now joins the strip on approach and being able to see
  the real thing beats a highlight promising it. That turned out to hide the
  feature: the tab does join, but with nothing marking the strip it is joining,
  nobody could tell it had happened. All three are back, as what makes the join
  legible rather than as a substitute for it. The insertion line is read after
  the tab has joined, so it marks where the tab actually is, and it is drawn in
  the pressed accent because a gold line on a gold wash cannot be seen.

### Fixed

- **Dragging a tab out into its own window killed the app.** Exit code
  `0xC000041D`, reliably, on dragging a tab into a window whose only tab was an
  empty one and then on to a third window. The cause was a check on the message
  path asking Qt for a window's native handle with a call that creates the
  handle when there is not one. Closing a window destroys its handle, the next
  message for that window made a new one, making one dispatches two more
  messages straight back into the same code, and it went round until the stack
  ran out. Windows reports a stack overflow inside a window procedure as that
  exit code. The same question is asked now with a call that only ever reads.
  Alongside it, the work that closes or reparents a window is deferred off the
  message stack rather than run inside it, because that was what put a
  just-closed window in the queue to begin with, and the tear-off releases its
  hold on the mouse and keyboard before it changes a window rather than after.
  - Worth knowing: the 31 existing tear-off tests all passed against the broken
    code. They run offscreen, which has no window procedure and no native
    handles, so the boundary that was failing does not exist in the harness.
    This was reproduced and confirmed on a real desktop instead, and what the
    new tests pin is the source rule rather than the crash.

- **OCR could corrupt the document it was reading.** The background worker drove
  the live document directly, inserting and deleting pages, while the window
  drew from the same document. PyMuPDF documents are not safe to use from two
  threads. The worker opens its own copy now and hands the finished file back
  for the window to apply, which keeps the original path so the next save still
  writes where it always would.

- **A rendering path read freed memory.** Rendering a page at a given zoom built
  the image over a temporary buffer, and the image does not copy what it is
  handed, so the buffer was released for exactly the line that needed it.

- **The updater said it had finished after writing half an update.** It decided
  success on the copy step's exit code, which is zero when it copied nothing,
  and on the new exe existing as a name rather than as something of a plausible
  size. It counts the files actually on disk after the swap now and checks the
  size of the exe it swapped in, puts the old version back when either falls
  short, keeps the downloaded payload until those checks pass, and writes the
  numbers into `update.log` so a bad update names its own cause. This protects
  updates applied *from* 1.8.0 onward: the script that applies an update is
  written by the version you are updating away from.

- **The gold box while dragging a tab.** Tearing a tab off washes the target
  strip in the accent and outlines it, which is how the drop target says "this
  window". A window holding one empty document hides its tab strip, and the
  wash and the outline were painted on it anyway: on a bar a few pixels wide
  that is not a highlighted strip, it is a small gold square floating in the
  caption. Drop feedback is now skipped on a strip too narrow to carry it.

- **PDFs get a document icon, not the app tile.** Picking this app as the
  default PDF handler used to repaint every PDF on the machine with the gold
  app tile. It has pointed at a document icon of our own since 1.6.0, a white
  page with a red PDF band, and an attempt to go further and claim no icon at
  all is reverted here: with no icon named, Windows falls back to the app's
  own, so on the one machine that matters, one where somebody has deliberately
  made RapidPDF their default PDF app, claiming nothing delivers exactly the
  gold tile it was meant to remove.
  - A Windows limit worth knowing: there is no generic PDF icon to fall back
    on, and no registry value meaning "leave it alone". A file type's icon is
    the icon of the program that owns the type. The choice is our document
    icon or our app tile, and the document icon is the one that answers the
    complaint.
  - Explorer caches icons per file type in a database that survives a reboot.
    The installer asks the shell to re-read associations, which is the whole of
    what an installer can do; a machine still showing an old icon needs
    `ie4uinit.exe -show`, or a sign-out.

## [1.7.0] - 2026-09-02

The title bar release. The tabs moved into the top row of the window, and how
sharply a page is rasterised now depends on how big the page is.

### Added

- **Page sharpness**, under View in `Edit > Preferences`. Four choices:
  Automatic, Standard, Sharp and Sharpest. Automatic is the size rule described
  below. The other three pin every document to one raster scale, which is the
  way to force a large drawing sharper than the rule would give it, and to pay
  for that in render time. Like the default fit, the choice applies to
  documents opened after it, not to the ones already open, because the scale is
  the coordinate space every annotation on a live document is stored in.

### Changed

- **The tabs are in the title bar.** The strip you grab to drag the window is
  now the same strip the documents sit on, the way Chrome, Edge and Firefox
  have done it for years. Up to 1.6.0 it was three separate rows: the system
  title bar, the menu bar, and the tab strip under both of them. The window is
  frameless now and draws its own top row, carrying the app icon, the tabs, a
  button to open a new one, and the minimise, maximise and close buttons.
  Everything a real title bar does still works: Snap Layouts open when the
  pointer rests on the maximise button, the system menu opens on right click
  and on `Alt+Space`, and the window resizes from every edge and corner. The
  menu bar moved down a row and is otherwise untouched.
  - **Dragging a tab now shows where it will land.** The strip that counted as
    a dock target was about 46 pixels around the target window's tab bar, and
    the floating window being dragged covered the insertion line, so aiming at
    another window looked like nothing was happening at all.
  - **Double-clicking bare tab strip maximises the window** instead of opening
    a tab, which is what a title bar does everywhere else. `Ctrl+T` and the new
    tab button still open one.
  - **There is no window title text any more.** The document names are on the
    tabs. `Alt+Tab` and the taskbar still read the window title.
- **How sharply a page is drawn follows the page size.** Every document was
  rasterised at a fixed 1.5, about 108 DPI, and zooming only magnified that
  bitmap rather than rendering a new one, so small scanned text could not be
  made readable by any means the app offered. A4 and Letter now render at 3.0
  and A3 at 2.0. A2, A1 and A0 stay at 1.5, because raster cost is quadratic in
  scale and linear in page area: a sharp A1 costs about four times the memory,
  on the single most expensive page the app ever draws, and rendering happens
  on the GUI thread. Large-format drawings are vector line work and are legible
  already, so they are the pages that least need it and least afford it.

### Fixed

- **The app is called "Rapid PDF" and nothing more.** The Windows version
  resource carried a whole tagline in `FileDescription`, and that field is what
  Explorer prints in the "Open with" list, what Task Manager shows in the
  process row, and what the taskbar tooltip reads. So "Open with" offered an
  entry the width of the dialog. It is now the app name on its own. The
  description still exists where there is room for one: the About box, the
  installer's Default Apps entry, and the site.
- **A hand-zoomed tab could come back at the wrong size** after session
  restore. Found while changing the raster scale: the restored zoom was applied
  before the document's first page had finished rendering, so a tab left at a
  zoom you had set by hand reopened at a different one once the page arrived.
- The window title separates the app name from the filename with a plain
  hyphen instead of an em dash.

## [1.6.0] - 2026-08-29

The tabs release. Several PDFs open at once, in one window or several, and
pages move between them.

### Added

- **Document tabs.** Several PDFs open in one window, one tab each. Opening a
  second file gives it its own tab instead of appending its pages to the
  document you are reading, which is what it did up to 1.5.0. To merge files,
  ask for it by name: `File > Combine PDFs…`, or `+ Add Pages` in the Organizer.
- Tabs are named by filename, and two files with the same name grow as much of
  their folder path as it takes to tell them apart. `Ctrl+T` opens a tab,
  middle-click closes one, `Ctrl+PgDn` / `Ctrl+PgUp` walk them by position and
  `Ctrl+Tab` walks them in the order you last used them. The chevron at the
  right of the bar lists everything open. Opening a file that is already open
  raises its tab instead of opening a second copy.
- **Several windows.** `File > New Window` (`Ctrl+Shift+N`), or **Move to New
  Window** on a tab's right-click menu. Drag a tab down off the bar and it tears
  into a window that appears under the cursor and follows it. Drop it on another
  window's tab bar and it docks there, at the gap the insertion line is showing.
  Dragging sideways, however far, is only ever a reorder.
- **Pages between tabs.** Drag a page out of one document's thumbnail strip or
  Organizer and into another's. Hold `Ctrl` at the drop to copy instead of move.
  Annotations and unsaved markup come across, and it is one undoable action even
  though it changes two documents. Both tabs have to be in the same window.
  Internal links and layers do not travel, and the status bar says when that
  happened.
- **Session restore.** "Reopen the tabs I had open last time", under Startup in
  `Edit > Preferences`. Off until you turn it on. Windows and tabs come back on
  the screen, page and zoom you left them, and each document is read the first
  time you look at its tab rather than all at once, so a window of eight A1
  drawings opens as fast as an empty one.
- **Preferences** (`Ctrl+,`): startup, closing, theme, where file dialogs open,
  the page panel, and the fit a document opens at, all on one page. There is no
  OK and no Cancel because every control applies as soon as it is touched.
  Settings now live in one file at `%LOCALAPPDATA%\Rapid PDF\settings.json`.
- **The version is on screen.** `Help > Rapid PDF v1.6.0`, sitting directly
  above Check for Updates, and again under About in Preferences.
- **Pan tool.** Press `H`, or hold `Space` for as long as you need it and go
  back to the tool you were on, or drag with the middle button from any tool.
- **Drop a PDF onto the window** and it opens as a new tab in that window. This
  never worked before. It is never appended to the document on screen.
- A password-protected PDF now says it is password protected instead of opening
  and then failing on the first render.

### Changed

- **Fit is four icon buttons** at the right of the status bar: fit page, fit
  width, fit height, and 100% (actual size). One is active at a time, each names
  itself on hover, and zooming by hand turns all four off.
- **PDFs keep a document icon in Explorer.** Picking Rapid PDF as the default
  handler used to give every PDF the app's own icon.
- **Undo is one history per window**, shared by its tabs, rather than one per
  document. A page dragged from one tab into another is one action with two
  documents in it. Undoing something that happened in another tab brings that
  tab forward first, so you watch the edit come back.
- **The delete button is gone from the thumbnail strip.** Select and press
  `Delete`, use the right-click menu, or use the Organizer.
- Tabs in the background release their render cache. Six open A1 drawings cost
  387 MB instead of 1249 MB.
- Settings moved out of the registry (`HKCU\Software\Lucas\Rapid PDF`) into the
  settings file, migrated once on first run.

### Fixed

- **The X button closes the window and everything in it**, so the last window's
  X quits the app. It used to close only the document and leave an empty window
  up, which meant quitting took two presses. `Ctrl+W` (close the front tab) and
  `Ctrl+Q` (quit every window) were both dead keys and now work. If you want the
  old behaviour, it is the second choice under Closing in Preferences.
- **Windows shutdown is no longer blocked** with a PDF open.
- **Relaunching while Rapid PDF is already running raises the window.** A second
  launch with nothing to open used to be dropped, so it looked like the app had
  simply failed to start.
- **Combine with Rapid PDF now combines.** Selecting several PDFs in Explorer
  and picking it opened each file separately instead of merging them.
- **Undo works on the first drag of an embedded image.** It silently did
  nothing: the drag both lifted the image out of the page and moved it, and
  neither half went on the stack. (`Ctrl+Z` twice, one for each.)
- **The Organizer keeps updating its thumbnails.** From the second switch into
  it, a queued render read a page clone that had just been closed and the grid
  quietly kept whatever it already had.
- **Saving over a file that cannot be overwritten says so.** The work still goes
  to a `.bak` beside the original, but it is no longer silent: a dialog names
  the file and the tab renames itself to it, so it is clear that the `.bak` is
  now the open document and the original is untouched. Close whatever is holding
  the file and use Save As to put it back.
- Deleting a page no longer leaves a bookmark pointing nowhere in the saved
  file.
- Closing a tab announces its departure before it is torn down, so nothing is
  left reading a document that has gone.
- A document re-renders when its window is moved to a screen with a different
  DPI.
