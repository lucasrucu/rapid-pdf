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
#define AppVersion "1.2.1"
#define AppPublisher "Lucas Ruiz"
#define AppExeName "rapid-pdf.exe"
; Stable GUID for upgrades/uninstall — keep this fixed across versions.
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
; ProgID with a clean "open" verb: right-click a .pdf > Open with Rapid PDF.
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document"; ValueType: string; ValueData: "PDF Document"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\DefaultIcon"; ValueType: string; ValueData: "{app}\{#AppExeName},0"
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\shell\open"; ValueType: string; ValueData: "Open with {#AppName}"
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\shell\open\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""
; Distinct combine verb: select several PDFs > "Combine with Rapid PDF".
; Explorer fires it once per selected file; the app's single-instance layer
; aggregates the burst into one Combine dialog (core/single_instance.py).
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\shell\combine"; ValueType: string; ValueData: "Combine with {#AppName}"
Root: HKCU; Subkey: "Software\Classes\RapidPDF.Document\shell\combine\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" --combine ""%1"""
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
; CODE SIGNING (deferred — see docs/build.md "Adding code signing later").
; When you have a signing cert/SignTool configured, define a sign tool in the
; Inno IDE (Tools -> Configure Sign Tools) named e.g. "signtool", then uncomment:
;   SignTool=signtool
; and add `SignTool=signtool` under [Setup] to sign the generated setup.exe.
; Sign the app exe (dist\rapid-pdf\rapid-pdf.exe) BEFORE compiling this script.
; -----------------------------------------------------------------------------
