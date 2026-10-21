#define MyVersion "0.13.2"

[Setup]
AppId={{2F5DBF08-7D4E-4B56-B6A2-60FC5C94F1AE}
AppName=MYN
AppVersion={#MyVersion}
AppPublisher=MYN
DefaultDirName={code:GetDefaultDir}
DefaultGroupName=MYN
UsePreviousAppDir=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=.
#ifdef TestMode
OutputBaseFilename=MYN_Setup_v0.13_test
CreateUninstallRegKey=no
#else
OutputBaseFilename=MYN_Setup_v0.13
#endif
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
VersionInfoVersion=0.13.2.0
VersionInfoDescription=MYN v0.13 Windows installer
UninstallDisplayIcon={app}\versions\{#MyVersion}\MYN.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\MYN\*"; DestDir: "{app}\versions\{#MyVersion}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\AGENTS.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "cleanup_legacy.ps1"; DestDir: "{app}\maintenance"; Flags: ignoreversion
Source: "known-program-files.json"; DestDir: "{app}\maintenance"; Flags: ignoreversion
Source: "..\src\*.py"; DestDir: "{app}\developer-source\src"; Flags: ignoreversion
Source: "..\src\core\*.py"; DestDir: "{app}\developer-source\src\core"; Flags: ignoreversion
Source: "..\tests\*.py"; DestDir: "{app}\developer-source\tests"; Flags: ignoreversion
Source: "installer.iss"; DestDir: "{app}\developer-source\build"; Flags: ignoreversion
Source: "myn_version.txt"; DestDir: "{app}\developer-source\build"; Flags: ignoreversion
Source: "build.ps1"; DestDir: "{app}\developer-source\build"; Flags: ignoreversion
Source: "cleanup_legacy.ps1"; DestDir: "{app}\developer-source\build"; Flags: ignoreversion
Source: "known-program-files.json"; DestDir: "{app}\developer-source\build"; Flags: ignoreversion
Source: "make_known_manifest.py"; DestDir: "{app}\developer-source\build"; Flags: ignoreversion

[Icons]
#ifndef TestMode
Name: "{group}\MYN"; Filename: "{app}\versions\{#MyVersion}\MYN.exe"
Name: "{group}\Uninstall MYN"; Filename: "{uninstallexe}"
Name: "{autodesktop}\MYN"; Filename: "{app}\versions\{#MyVersion}\MYN.exe"; Tasks: desktopicon
#endif

; Inno removes only files it installed. There is deliberately no broad
; UninstallDelete rule; data, backups, archives, local models and unknown files stay.

[Code]
var
  CleanupFailed: Boolean;

function GetDefaultDir(Param: String): String;
begin
  if DirExists('D:\MYN') or DirExists('D:\') then
    Result := 'D:\MYN'
  else
    Result := ExpandConstant('{localappdata}\Programs\MYN');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Arguments: String;
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;
  Arguments := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
    ExpandConstant('{app}\maintenance\cleanup_legacy.ps1') + '" -Root "' +
    ExpandConstant('{app}') + '"';
#ifdef TestArchiveDir
  Arguments := Arguments + ' -ArchiveDir "{#TestArchiveDir}"';
#endif
  Arguments := Arguments + ' -Apply';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
     Arguments, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then begin
    CleanupFailed := True;
    Log('Verified old-program cleanup failed. See maintenance\cleanup-error.log.');
    MsgBox('The new MYN files were installed, but verified old-program cleanup failed. ' +
      'Personal data was not removed. See maintenance\cleanup-error.log before retrying.', mbError, MB_OK);
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  if CleanupFailed then
    Result := 9
  else
    Result := 0;
end;
