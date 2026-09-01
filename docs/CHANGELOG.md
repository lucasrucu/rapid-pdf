# Changelog

Everything worth knowing about each release of Rapid PDF. The shape follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the numbers follow
[semver](https://semver.org/spec/v2.0.0.html), which is also how
`core/update/release.py` decides whether a release is newer than the build you
are running.

Releases before 1.6.0 were written up on the
[Releases page](https://github.com/lucasrucu/rapid-pdf/releases) and are not
backfilled here.

## [Unreleased]

### Fixed

- **The app is called "Rapid PDF" and nothing more.** The Windows version
  resource carried a whole tagline in `FileDescription`, and that field is what
  Explorer prints in the "Open with" list, what Task Manager shows in the
  process row, and what the taskbar tooltip reads. So "Open with" offered an
  entry the width of the dialog. It is now the app name on its own. The
  description still exists where there is room for one: the About box, the
  installer's Default Apps entry, and the site.
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
