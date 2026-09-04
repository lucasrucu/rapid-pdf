; Inno Setup script for rapid-pdf.
; Wraps the PyInstaller onedir output (dist\rapid-pdf\) into a real installer:
; setup wizard, Start-menu + optional desktop shortcut, uninstaller, app icon.
;
; Build:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" rapid-pdf.iss
; Output: installer_output\rapid-pdf-setup-{#AppVersion}.exe
;
; Per-user install (PrivilegesRequired=lowest) -> installs to %LocalAppData%,
; NO UAC prompt. Smoother for a personal/showcase tool. Switch to a per-machine
; install (Program Files, admin) by setting PrivilegesRequired=admin and
; DefaultDirName to {autopf}.

; The shipped name is one word. It reaches the Start-menu and desktop
; shortcuts, the install folder, the uninstall entry, the Default Apps entry
; and both shell verbs, so it is defined once and never spelled out again.
; The OLD spelling, "Rapid PDF" with a space, is still written literally in a
; few places below, and only there: cleaning up something an earlier build
; created has to name what that build actually created.
#define AppName "RapidPDF"
#define OldAppName "Rapid PDF"
#define AppVersion "1.8.0"
#define AppPublisher "Lucas Ruiz"
#define AppExeName "rapid-pdf.exe"
; Stable GUID for upgrades/uninstall. Keep this fixed across versions.
#define AppId "{{A7E3C9F1-4B2D-4E6A-9C8F-1D5B7A0E3F42}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Skip the "where to install" page: per-user installs always land in
; %LocalAppData%\Programs\RapidPDF, no reason to ask. An UPGRADE keeps
; whatever folder the previous install used, because Inno reads it back out of
; the uninstall registration and AppId has not changed, so machines that
; installed 1.7.0 or earlier stay in %LocalAppData%\Programs\Rapid PDF and are
; not duplicated by the rename.
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=installer_output
OutputBaseFilename=rapid-pdf-setup-{#AppVersion}
SetupIconFile=assets\rapid-pdf.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Do NOT inherit task choices from a previous install: 1.1.0 stored
; desktopicon=unchecked in its uninstall log, so an upgrade would silently
; keep skipping the desktop icon even though it now defaults to on
; (verified: 1.1.0 -> 1.2.1 /SILENT upgrade produced no desktop shortcut
; until this line was added).
UsePreviousTasks=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Desktop icon ON by default (the 1.1.0 installer shipped this unchecked, so
; no desktop shortcut ever appeared; nobody ticks wizard checkboxes).
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; The whole PyInstaller onedir folder. The wildcard + recursesubdirs pulls every
; DLL, Qt plugin, and bundled asset. That includes assets\tessdata\
; eng.traineddata (the OCR language data; PyMuPDF embeds the engine, this
; file is the only OCR dependency that must ship).
Source: "dist\rapid-pdf\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The .pdf FILE icon, copied to the app root on purpose. PyInstaller buries
; bundled data under {app}\_internal\assets\, and the shell holds DefaultIcon
; as a literal path forever; keeping it at {app}\pdf-document.ico means a
; PyInstaller layout change can never leave every PDF on the machine iconless.
Source: "assets\pdf-document.ico"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; 1.1.0 put its shortcuts in a "Rapid PDF" Start Menu FOLDER ({group}); from
; 1.2.1 there is a single link at the Programs root (better for Windows
; search). Clear the old folder on upgrade so no duplicate/stale entries
; survive (AppId is unchanged, so 1.2.1 over 1.1.0 upgrades in place).
; Named with the OLD spelling on purpose: that is what those builds wrote.
Type: files; Name: "{autoprograms}\{#OldAppName}\{#OldAppName}.lnk"
Type: files; Name: "{autoprograms}\{#OldAppName}\Uninstall {#OldAppName}.lnk"
Type: dirifempty; Name: "{autoprograms}\{#OldAppName}"
; 1.2.1 to 1.7.0 shipped shortcuts named "Rapid PDF" at the Programs root and
; on the desktop. The [Icons] entries below now write "RapidPDF" instead, and
; a shortcut is a file: without these lines an upgrade leaves the old pair
; sitting beside the new pair and the Start menu offers the app twice.
Type: files; Name: "{autoprograms}\{#OldAppName}.lnk"
Type: files; Name: "{autodesktop}\{#OldAppName}.lnk"

[Icons]
; Single shortcut at the Start Menu Programs ROOT: this is what makes the app
; findable by typing "RapidPDF" into Windows search. Uninstalling lives in
; Settings > Apps, so it gets no shortcut of its own.
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; All per-user (HKCU), no admin needed. Keys of our own carry uninsdeletekey;
; values planted in SHARED keys (.pdf\OpenWithProgids, RegisteredApplications)
; carry uninsdeletevalue, so uninstall removes exactly what was added.
;
; 1.5.0 and earlier put the combine verb on the ProgID. It now lives under
; SystemFileAssociations (see below); drop the old key on upgrade or the shell
; merges both and shows "Combine with Rapid PDF" twice.
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\shell\combine"; ValueType: none; Flags: deletekey
;
; DefaultIcon: a DOCUMENT icon, and the argument for deleting the key instead
; was made and rejected. Read this before changing it a fourth time.
;
; DefaultIcon on this ProgID is the icon Explorer paints on EVERY .pdf on the
; machine, and only while this ProgID owns the .pdf association. Three
; positions have been held:
;   up to 1.5.0 it pointed at rapid-pdf.exe, so picking us as the default
;   handler repainted every PDF on the machine with the gold app tile;
;   1.6.0 and 1.7.0 pointed it at a document icon of our own drawing;
;   a later change deleted the key outright, on the argument that a PDF viewer
;   has no business restyling somebody's file type on install.
;
; THE DELETION WAS REVERTED, because its own reasoning does not hold for the
; person this is built for. Windows has no generic PDF icon to fall back to:
; the icon of a file type IS the icon of the ProgID that owns it. With
; DefaultIcon absent the shell falls back to the FIRST ICON OF THE EXE in
; shell\open\command, so a user who makes RapidPDF their default PDF app gets
; the gold app tile back. The deletion was defended as safe because "almost
; nobody does that". Lucas does, and his words on 2026-09-02 were: "if i select
; rapid pdf they all turn into yellow icons and make no sense and confuses, id
; like to let the icon maintain defaul pdf icon regular icon instead". Deleting
; the key delivers him the exact thing he was complaining about.
;
; So this points at a document icon of our own: assets\pdf-document.ico, a
; white page with a folded corner and a red PDF band, which is what a PDF has
; looked like for twenty years. Regenerate with
; python tools\make_document_icon.py. The exe, the Start-menu shortcut and the
; window all still carry rapid-pdf.ico: the app icon identifies the APP and the
; document icon identifies the FILE TYPE, and they are two different jobs.
;
; "Leave it alone" is not available. There is no registry value meaning that,
; and absent is not neutral. Given the choice is between our document icon and
; our app tile, the document icon is the one that answers the complaint.
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\DefaultIcon"; ValueType: string; ValueData: "{app}\pdf-document.ico,0"
;
; ProgID with a clean "open" verb: right-click a .pdf > Open with RapidPDF.
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document"; ValueType: string; ValueData: "PDF Document"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\shell\open"; ValueType: string; ValueData: "Open with {#AppName}"
; Explicit, even though Document is what the shell infers for a command-line
; verb: "open" makes a top-level window per item, so the 15-item cap is right.
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\shell\open"; ValueType: string; ValueName: "MultiSelectModel"; ValueData: "Document"
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\shell\open\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""
;
; Combine verb: select several PDFs > "Combine with RapidPDF".
;
; Registered under SystemFileAssociations\.pdf, NOT on our ProgID. A verb on
; the ProgID only appears while RapidPDF is the .pdf handler; the moment
; Adobe or Edge takes the association back, the entry vanishes.
; SystemFileAssociations verbs are merged into the menu for every .pdf
; whatever owns the extension, which is what this verb wants. Keep it in
; exactly ONE of the two places: the shell merges both, so registering it on
; the ProgID as well would show "Combine with RapidPDF" twice.
;
; MultiSelectModel=Player is load-bearing. Without it a command-line verb is
; treated as Document, which the shell HIDES entirely past 15 selected files
; (docs: "Employing the Verb Selection Model"). Player raises that to 100 and
; matches what the verb actually does: one Combine dialog, any number of
; inputs. Explorer still fires the command once per selected file; the app's
; single-instance layer elects one primary and aggregates the burst into a
; single Combine call (core/single_instance.py).
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations"; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf"; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell"; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\RapidPDF.Combine"; ValueType: string; ValueName: "MUIVerb"; ValueData: "Combine with {#AppName}"; Flags: uninsdeletekey
; This one stays the APP icon, and should. A context-menu verb icon says which
; program is about to run, not what the file is, so the gold tile is right here.
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\RapidPDF.Combine"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#AppExeName},0"
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\RapidPDF.Combine"; ValueType: string; ValueName: "MultiSelectModel"; ValueData: "Player"
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\RapidPDF.Combine\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" --combine ""%1"""
; Offer the ProgID as a .pdf handler: shows in "Open with" and carries the
; verbs above. Never touches the user's chosen default.
Root: HKCU; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "RapidPDF.Document"; ValueData: ""; Flags: uninsdeletevalue
; Default Programs registration: appears in Settings > Default apps so the
; user CAN pick RapidPDF for .pdf. Selectable, never forced, never prompted.
; The parent key is removed too when uninstall leaves it empty (verified: with
; only the Capabilities entry flagged, an empty Software\Rapid PDF lingered).
;
; The key is named off AppName, so the rename moves it: 1.2.1 through 1.7.0
; wrote Software\Rapid PDF and a RegisteredApplications value pointing into it.
; Left alone that becomes a second entry in Settings > Default apps, named with
; the old spelling and pointing at Capabilities that nothing maintains. Both go
; first, and only then is the new pair written.
Root: HKCU; Subkey: "Software\RegisteredApplications"; ValueType: none; ValueName: "{#OldAppName}"; Flags: deletevalue
Root: HKCU; Subkey: "Software\{#OldAppName}"; ValueType: none; Flags: deletekey
Root: HKCU; Subkey: "Software\{#AppName}"; ValueType: none; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Fast PDF page management and markup"
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".pdf"; ValueData: "RapidPDF.Document"
Root: HKCU; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#AppName}"; ValueData: "Software\{#AppName}\Capabilities"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; -----------------------------------------------------------------------------
; CODE SIGNING (deferred, see docs/build.md "Adding code signing later").
; When you have a signing cert/SignTool configured, define a sign tool in the
; Inno IDE (Tools -> Configure Sign Tools) named e.g. "signtool", then uncomment:
;   SignTool=signtool
; and add `SignTool=signtool` under [Setup] to sign the generated setup.exe.
; Sign the app exe (dist\rapid-pdf\rapid-pdf.exe) BEFORE compiling this script.
; -----------------------------------------------------------------------------

[Code]
// Tell the shell the file associations moved. Explorer caches the icon for a
// file type and will happily keep drawing the old one for the rest of the
// session (and past a reboot, out of iconcache_*.db) unless it is told.
//
// This matters most to anyone upgrading from 1.5.0 or earlier, who has
// RapidPDF.Document\DefaultIcon pointing at the exe and every PDF painted
// gold, and to anyone on 1.6.0 or 1.7.0, who has it pointing at our document
// icon. This install deletes that key outright; without this call nothing on
// screen changes until something else happens to invalidate the cache.
//
// SHCNE_ASSOCCHANGED is the documented way to do it and is what Explorer
// itself fires when you change a default app. It is also the honest limit of
// what an installer can do: it does not delete the icon cache database, so a
// machine that has wedged its cache still needs `ie4uinit.exe -show`, or a
// sign-out, or explorer.exe restarted with
// %LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache_*.db removed. Removing
// the value is also the weaker of the two cases for the shell: it repaints
// most reliably when a cached path is REPLACED by a different path, and here
// there is no new path at all, only an association that has gone back to
// whoever else owns it.
const
  SHCNE_ASSOCCHANGED = $08000000;
  SHCNF_IDLIST       = $00000000;

procedure SHChangeNotify(wEventId: Integer; uFlags: Cardinal;
  dwItem1, dwItem2: Cardinal);
  external 'SHChangeNotify@shell32.dll stdcall';

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, 0, 0);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // Same on the way out: the ProgID is gone, so whatever owns .pdf next
  // should be drawn instead of a stale RapidPDF entry.
  if CurUninstallStep = usPostUninstall then
    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, 0, 0);
end;
