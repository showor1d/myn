#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef WebViewBootstrapper
  #error WebViewBootstrapper is required
#endif
#define MyVersion "1.0"

[Setup]
AppId={{2F5DBF08-7D4E-4B56-B6A2-60FC5C94F1AE}
AppName=MYN
AppVersion={#MyVersion}
AppPublisher=MYN
DefaultDirName={localappdata}\Programs\MYN
DefaultGroupName=MYN
UsePreviousAppDir=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputBaseFilename=MYN-Setup-1.0-x64
Compression=lzma2/fast
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
CloseApplications=no
RestartApplications=no
SetupLogging=yes
ExtraDiskSpaceRequired=12000000000
SetupIconFile={#PayloadDir}\src\assets\myn-symbol.ico
UninstallDisplayIcon={app}\MYN.exe
VersionInfoVersion=1.0.0.0
VersionInfoDescription=MYN with private runtimes and backup restore
#ifdef TestMode
CreateUninstallRegKey=no
#endif

[Files]
; Explicit roots keep files created by smoke checks out of the installer.
Source: "{#PayloadDir}\src\*"; DestDir: "{app}\src"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\tests\*"; DestDir: "{app}\tests"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\build\*"; DestDir: "{app}\build"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\tools\dev_finish.py"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "{#PayloadDir}\runtime\*"; DestDir: "{app}\runtime"; Excludes: "speech\Lib\site-packages\torch\*,speech\Lib\site-packages\torchgen\*,speech\Lib\site-packages\functorch\*,speech\Lib\site-packages\torch-2.11.0+cu128.dist-info\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\local-stt\*"; DestDir: "{app}\local-stt"; Excludes: "whisper-large-v3-turbo\model.safetensors"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\AGENTS.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\MYN.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\package-manifest.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\dependency-versions.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WebViewBootstrapper}"; DestName: "webview2.exe"; Flags: dontcopy
Source: "install_guard.ps1"; Flags: dontcopy

[Icons]
#ifndef TestMode
Name: "{group}\MYN"; Filename: "{app}\MYN.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall MYN"; Filename: "{uninstallexe}"
Name: "{autodesktop}\MYN"; Filename: "{app}\MYN.exe"; WorkingDir: "{app}"
#endif

[Run]
Filename: "{app}\MYN.exe"; Description: "Open MYN and restore a backup"; Flags: nowait postinstall skipifsilent; Check: ComponentsReady

[UninstallRun]
Filename: "{app}\runtime\main\pythonw.exe"; Parameters: "-I -B ""{app}\src\install_components.py"" --uninstall --silent"; Flags: runhidden waituntilterminated; RunOnceId: "VerifiedSpeechComponents"

; No UninstallDelete: personal data, recovery copies and unknown files remain.
[Code]
var InstallComplete: Boolean;

function ComponentsReady: Boolean;
begin
  Result := InstallComplete;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Code: Integer; Args: String;
begin
  if CurStep <> ssPostInstall then Exit;
  Args := '-I -B "' + ExpandConstant('{app}\src\install_components.py') + '"';
  if WizardSilent then Args := Args + ' --silent';
  WizardForm.StatusLabel.Caption := 'Downloading and verifying speech components...';
  InstallComplete := Exec(ExpandConstant('{app}\runtime\main\pythonw.exe'), Args, ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, Code) and (Code = 0);
  if not InstallComplete then begin
    if not WizardSilent then
      MsgBox('Speech setup did not finish. Keep this folder, reconnect to the internet and run MYN Setup again. Your personal data is unchanged.', mbError, MB_OK);
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  if InstallComplete then Result := 0 else Result := 10;
end;

function HasWebView: Boolean;
var V: String;
begin
  Result := (RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) and (V <> '') and (V <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM32, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) and (V <> '') and (V <> '0.0.0.0'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer; Args: String; Diagnostic: AnsiString;
begin
  Result := '';
  ExtractTemporaryFile('install_guard.ps1');
  Args := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\install_guard.ps1') + '" -Root "' + ExpandConstant('{app}') + '" -DiagnosticPath "' + ExpandConstant('{tmp}\guard-error.txt') + '"';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Args, '', SW_HIDE, ewWaitUntilTerminated, Code)) or (Code <> 0) then begin
    Result := 'Close MYN before installing. If it is already closed, choose an empty folder or check folder access. Existing data was preserved.';
    if LoadStringFromFile(ExpandConstant('{tmp}\guard-error.txt'), Diagnostic) then
      Log('Install folder check: ' + String(Diagnostic));
    Exit;
  end;
  if not HasWebView then begin
    WizardForm.StatusLabel.Caption := 'Preparing display components. An internet connection is required...';
    ExtractTemporaryFile('webview2.exe');
    if (not Exec(ExpandConstant('{tmp}\webview2.exe'), '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, Code)) or ((Code <> 0) and (Code <> 3010)) then begin
      Result := 'The display components could not be installed. Connect to the internet and retry.';
      Exit;
    end;
    if not HasWebView then
      Result := 'The display components are not ready. Restart Windows and run setup again.';
  end;
end;
