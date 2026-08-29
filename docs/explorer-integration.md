# Explorer integration: the right-click Combine verb

`docs/tabs-plan.md` does not exist on this branch (it lives on the unmerged
`docs/tabs-plan` branch), so this is its own file rather than a duplicate of
the tabs plan. Fold it in if the two ever meet.

Status: **the Combine verb was already registered and already reachable. It
was broken, not missing.** Fixed here.

## What was actually wrong

Selecting several PDFs, right-clicking, and picking "Combine with Rapid PDF"
opened them in separate windows instead of combining them.

Explorer fires a static shell verb **once per selected file**, so five PDFs
means five `rapid-pdf.exe --combine "<one file>"` processes. `main.py` was
supposed to let the first one become the primary and have the rest forward
their paths to it over a `QLocalServer` named pipe.

Two defects, compounding:

1. **`QLocalServer.listen()` is not exclusive on Windows.** Qt creates the
   named pipe without `FILE_FLAG_FIRST_PIPE_INSTANCE`, so several processes
   can all "listen" on the same name and Windows spreads incoming clients
   across the pipe instances. Proven directly: two `QLocalServer`s on one name
   both return `True` from `listen()`. So `listen()` could never elect a
   primary, and nothing else was doing it.

2. **The election happened far too late.** `main.py` called
   `forward_to_primary()` first, then built the theme and the whole
   `MainWindow`, and only then called `listen()`. Every sibling process that
   started during that window found no pipe, decided it was the primary, and
   built its own window.

Reproduced with a real five-process `--combine` burst: **three processes each
got `listen() == True`**, and the batch split 1 / 1 / 3 across three separate
windows. That is exactly the reported symptom.

A third, smaller problem: the 700 ms aggregation window is gap-based, which is
right in principle, but it is measured between arrivals and Explorer's
per-file processes cold-start against each other. On a 15-file burst the
arrivals spread about 5 seconds, so a flat 700 ms window splits the batch even
with a single correct primary.

## The fix

`core/single_instance.py`:

- `claim_primary()` elects the primary with a `QSharedMemory` segment.
  `create()` fails with `AlreadyExists` while any process holds it, and
  Windows destroys the mapping when the last handle closes, so a crashed
  primary leaves nothing stale behind. This is exact, unlike a lock file with
  staleness heuristics.
- `forward_to_primary()` now retries. A secondary knows a primary exists
  (it lost the claim), so it keeps trying to reach the pipe instead of giving
  up and opening a window. If the claim frees up, the primary died and the
  secondary takes over. No deadlock: the claim holder is never ambiguous.
- The aggregation window widens once a second launch lands
  (`BURST_AGGREGATE_MS`, 1500 ms) because by then it is certainly a multi-file
  operation, with a hard `MAX_AGGREGATE_MS` ceiling of 10 s. A single-file open
  still uses the fast 700 ms path.
- `arm()` gates emission, so a batch cannot be emitted before the window is
  wired to `batch_ready`.

`main.py`:

- Elect first, before anything slow. Then `listen()` **before** building the
  window, so the gap between "a primary exists" and "the primary is
  listening" is as small as possible.

Verified after the fix: 5-file burst gives one election, four forwards, one
batch of 5. 15-file burst gives one batch of 15, including a straggler whose
process started nearly 7 seconds behind the others.

## Installer registration

`rapid-pdf.iss` changed too, for reasons that are real but were not the bug:

- **The combine verb moved from the ProgID to
  `SystemFileAssociations\.pdf\shell\RapidPDF.Combine`.** A verb on
  `RapidPDF.Document` only appears while Rapid PDF owns the `.pdf`
  association. It does today (`FileExts\.pdf\UserChoice` = `RapidPDF.Document`),
  which is why the entry was findable at all. The moment Adobe or Edge takes
  the association back it would vanish. `SystemFileAssociations` verbs are
  merged in for every `.pdf` whatever owns the extension.
  Keep it in exactly one place: the shell merges both, so leaving it on the
  ProgID as well would show the entry twice. The old ProgID key is deleted on
  upgrade.
- **`MultiSelectModel=Player` on the combine verb.** Without it a command-line
  verb is treated as `Document`, and the shell hides it entirely past 15
  selected files. `Player` raises that to 100 and matches what the verb does:
  one dialog, any number of inputs.
  ([Employing the Verb Selection Model](https://learn.microsoft.com/en-us/windows/win32/shell/how-to-employ-the-verb-selection-model))
- `MultiSelectModel=Document` stated explicitly on `open`, which is what it
  already inferred.

**Unverified until the next install.** No build was run, so the new registry
layout has not been exercised by a real setup. Check after the next install
that the entry appears exactly once, and appears for a PDF when Rapid PDF is
not the default handler.

## Where the entry lives in the Windows 11 menu

It is a legacy static shell verb, so it appears under **"Show more options"**
(or `Shift`+`F10`, or `Shift`+right-click), never in the compact Windows 11
menu.

## Future item, not done here: the compact Windows 11 menu

To put "Combine with Rapid PDF" in the main Windows 11 context menu, the app
must implement `IExplorerCommand` and register a `windows.fileExplorerContextMenus`
extension, which requires **package identity**. An unpackaged Win32 app gets
that only through a **sparse MSIX package**.
([Extending the Context Menu in Windows 11](https://blogs.windows.com/windowsdeveloper/2021/07/19/extending-the-context-menu-and-share-dialog-in-windows-11/))

Real cost, so it can be judged rather than guessed at:

- A COM in-process DLL implementing `IExplorerCommand`. Python cannot ship
  this; it means a small C++ or Rust DLL as a second build artifact.
- A sparse package (`AppxManifest.xml` + `MakeAppx` + registration at first
  run), added to the build alongside PyInstaller and Inno Setup.
- **Signing becomes mandatory.** A sparse package will not register unless
  signed by a certificate the machine trusts. Rapid PDF ships unsigned today
  (`docs/build.md`, "Adding code signing later"), so this pulls the deferred
  signing work forward, cert and all.
- Uninstall has to deregister the package as well as remove files.

Worth noting the upside beyond cosmetics: `IExplorerCommand` is a COM verb, so
it is invoked **once with the whole selection** instead of once per file. That
would delete the entire single-instance aggregation problem this document is
about, and remove the process-per-file cost (15 PDFs currently means 15
process starts). It is the correct long-term answer, and it is a real project,
not a tweak.

## Residual risk

If a launch lands after a batch has already flushed, it emits as a second
batch rather than joining the first. The widened burst window makes this
unlikely, but it is not impossible on a very heavily loaded machine. The
proper fix is the COM verb above, which removes the burst entirely.
