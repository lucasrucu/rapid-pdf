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

#define AppName "Rapid PDF"
#define AppVersion "1.5.0"
#define AppPublisher "Lucas Ruiz"
#define AppExeName "rapid-pdf.exe"
; Stable GUID for upgrades/uninstall â€” keep this fixed across versions.
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
; %LocalAppData%\Programs\Rapid PDF, no reason to ask.
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
Type: files; Name: "{autoprograms}\{#AppName}\{#AppName}.lnk"
Type: files; Name: "{autoprograms}\{#AppName}\Uninstall {#AppName}.lnk"
Type: dirifempty; Name: "{autoprograms}\{#AppName}"

[Icons]
; Single shortcut at the Start Menu Programs ROOT: this is what makes the app
; findable by typing "Rapid PDF" into Windows search. Uninstalling lives in
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
; ProgID with a clean "open" verb: right-click a .pdf > Open with Rapid PDF.
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document"; ValueType: string; ValueData: "PDF Document"; Flags: uninsdeletekey
; DefaultIcon is the icon Explorer paints on every .pdf FILE once the user
; picks Rapid PDF as their default handler. Up to 1.5.0 it pointed at the exe,
; so choosing us repainted every PDF on the machine with the gold app tile.
; That is the wrong image for the job: the app icon identifies the APP, the
; document icon identifies the FILE TYPE, and PDFs have read as a white page
; with a red PDF label for twenty years. Changing that just confuses people
; about what the file is. So this points at a document icon of our own
; (assets\pdf-document.ico, regen: python tools\make_document_icon.py); the
; exe, the Start-menu shortcut and the window all still carry rapid-pdf.ico.
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\DefaultIcon"; ValueType: string; ValueData: "{app}\pdf-document.ico,0"
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\shell\open"; ValueType: string; ValueData: "Open with {#AppName}"
; Explicit, even though Document is what the shell infers for a command-line
; verb: "open" makes a top-level window per item, so the 15-item cap is right.
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\shell\open"; ValueType: string; ValueName: "MultiSelectModel"; ValueData: "Document"
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\shell\open\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""
;
; Combine verb: select several PDFs > "Combine with Rapid PDF".
;
; Registered under SystemFileAssociations\.pdf, NOT on our ProgID. A verb on
; the ProgID only appears while Rapid PDF is the .pdf handler; the moment
; Adobe or Edge takes the association back, the entry vanishes.
; SystemFileAssociations verbs are merged into the menu for every .pdf
; whatever owns the extension, which is what this verb wants. Keep it in
; exactly ONE of the two places: the shell merges both, so registering it on
; the ProgID as well would show "Combine with Rapid PDF" twice.
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
; user CAN pick Rapid PDF for .pdf. Selectable, never forced, never prompted.
; The parent key is removed too when uninstall leaves it empty (verified: with
; only the Capabilities entry flagged, an empty Software\Rapid PDF lingered).
Root: HKCU; Subkey: "Software\{#AppName}"; ValueType: none; Flags: uninsdeletekeyifempty
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Fast PDF page management and markup"
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".pdf"; ValueData: "RapidPDF.Document"
Root: HKCU; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#AppName}"; ValueData: "Software\{#AppName}\Capabilities"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; -----------------------------------------------------------------------------
; CODE SIGNING (deferred â€” see docs/build.md "Adding code signing later").
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
// This matters most on the 1.5.0 -> next upgrade: those users already have
// RapidPDF.Document\DefaultIcon pointing at the exe and every PDF painted
// gold. The registry value changes under them; without this call nothing on
// screen changes until something else happens to invalidate the cache.
//
// SHCNE_ASSOCCHANGED is the documented way to do it and is what Explorer
// itself fires when you change a default app. It is also the honest limit of
// what an installer can do: it does not delete the icon cache database, so a
// machine that has wedged its cache may still need a sign-out, or a manual
// `ie4uinit.exe -show`. The new value is a different PATH from the old one
// (pdf-document.ico, not rapid-pdf.exe), which is the case the shell handles
// best, since cache entries are keyed on path plus index.
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
  // Same on the way out: the ProgID and its icon are gone, so whatever owns
  // .pdf next should be drawn instead of a stale Rapid PDF page.
  if CurUninstallStep = usPostUninstall then
    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, 0, 0);
end;
