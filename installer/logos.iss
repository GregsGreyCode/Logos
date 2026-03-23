; Logos Desktop Installer — Inno Setup 6 script
; Build: ISCC installer\logos.iss  (run from repo root after PyInstaller)
;
; Prerequisites:
;   1. Run PyInstaller first:  pyinstaller launcher/hermes_launcher.spec
;   2. Output must be at:      dist\Logos\Logos.exe
;   3. Inno Setup 6 installed: https://jrsoftware.org/isdl.php

#define MyAppName "Logos"
#define MyAppVersion "0.4.0"
#define MyAppPublisher "Greg"
#define MyAppURL "https://github.com/gregsgreycode/hermes"
#define MyAppExeName "Logos.exe"
#define MyOutputDir "installer\output"
#define MySourceDir "dist\Logos"

[Setup]
AppId={{E8A1F3D2-4B6C-4E7A-9F2B-3C5D8E1A0B4F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; No UAC elevation required — installs to %LOCALAPPDATA%
PrivilegesRequired=lowest
OutputDir={#MyOutputDir}
OutputBaseFilename=LogosSetup-{#MyAppVersion}
; Compression
Compression=lzma2/ultra64
SolidCompression=yes
; UI
WizardStyle=modern
; Icon (uncomment when logos.ico is available)
; SetupIconFile=launcher\logos.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; Windows 10 1903+ required (for the webview APIs)
MinVersion=10.0.18362

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupentry"; Description: "Start Logos automatically when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; Main application bundle (PyInstaller output)
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start Menu
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
; Desktop (optional)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Startup with Windows (optional task)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "{#MyAppName}"; \
  ValueData: """{app}\{#MyAppExeName}"""; \
  Flags: uninsdeletevalue; Tasks: startupentry

[Run]
; Launch after install
Filename: "{app}\{#MyAppExeName}"; \
  Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Graceful shutdown before uninstall
Filename: "taskkill"; Parameters: "/IM {#MyAppExeName} /F"; Flags: runhidden; \
  RunOnceId: "KillLogos"

[Code]
// Detect existing running instance and offer to close it before install
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if CheckForMutexes('LogosDesktop') then
  begin
    if MsgBox('Logos is currently running. Close it before installing?',
      mbConfirmation, MB_YESNO) = IDYES then
    begin
      Exec('taskkill', '/IM {#MyAppExeName} /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Sleep(1500);
    end;
  end;
end;
