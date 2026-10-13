; Per-user installer: no administrator rights needed.
#define AppName "Calendar Alarm"
#define AppVersion "1.1.0"
#define AppExe "CalendarAlarm.exe"

[Setup]
AppId={{6F0B8C2E-4D1A-4B7E-9C35-CA1E0DA7A1A2}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Sullivan Bitho
DefaultDirName={localappdata}\Programs\CalendarAlarm
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=CalendarAlarm-Setup-{#AppVersion}
SetupIconFile=build\CalendarAlarm.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Files]
Source: "dist\CalendarAlarm\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Registry]
; The program registers itself at logon; the installer only cleans that entry up.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "CalendarAlarm"; Flags: dontcreatekey uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Lancer {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Saved passwords and iCal addresses (Windows Credential Manager).
Filename: "{app}\{#AppExe}"; Parameters: "--forget"; Flags: runhidden; RunOnceId: "ForgetSecrets"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\calendar-alarm"

[Code]
procedure StopApp();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopApp();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopApp();
  Result := True;
end;
