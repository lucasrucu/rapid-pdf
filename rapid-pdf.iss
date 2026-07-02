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
#define AppVersion "1.1.0"
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

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole PyInstaller onedir folder. The wildcard + recursesubdirs pulls every
; DLL, Qt plugin, and bundled asset. That includes assets\tessdata\
; eng.traineddata (the OCR language data; PyMuPDF embeds the engine, this
; file is the only OCR dependency that must ship).
Source: "dist\rapid-pdf\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

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
