; NEXUS VIDEO AI - per-user Windows installer (Inno Setup 6). Built by installer/windows/build.ps1, which passes
; /DAppVersion, /DSourceDir (the staged app: runtime\, ffmpeg\, nexus\, NEXUS.pyw, install.json) and /DOutDir.
; No admin rights; program files in %LOCALAPPDATA%\Programs\NEXUS VIDEO AI; user data in %LOCALAPPDATA%\NEXUS\data
; (kept on uninstall unless the user ticks the removal box). The app binds to 127.0.0.1 only; no tunnel, no firewall rule.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
[Setup]
AppId={{6E1B2C9A-4F7D-4E55-9C3B-2A7F0D5E8B11}
AppName=NEXUS VIDEO AI
AppVersion={#AppVersion}
AppPublisher=NEXUS VIDEO AI (owner build)
DefaultDirName={localappdata}\Programs\NEXUS VIDEO AI
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=NEXUS_VIDEO_AI_Setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=no
UninstallDisplayName=NEXUS VIDEO AI
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\NEXUS VIDEO AI"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\NEXUS.pyw"""; WorkingDir: "{app}"
Name: "{autoprograms}\Stop NEXUS VIDEO AI"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-m nexus stop"; WorkingDir: "{app}"
Name: "{autodesktop}\NEXUS VIDEO AI"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\NEXUS.pyw"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; import an earlier NEXUS data folder (source-checkout .nexus) when the user chose one; never overwrites existing data
Filename: "{app}\runtime\python.exe"; Parameters: "-m nexus import-home --from ""{code:ImportDir}"""; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; Check: WantImport
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\NEXUS.pyw"""; WorkingDir: "{app}"; Description: "Start NEXUS VIDEO AI"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\runtime\python.exe"; Parameters: "-m nexus stop"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "StopNexus"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\nexus"
Type: filesandordirs; Name: "{app}\runtime"

[Code]
var
  ImportAsk: TInputOptionWizardPage;
  ImportPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  // A dir page refuses an empty path ("You must enter a full path with drive letter"), which aborted the silent
  // install and blocked users without old data. The import is now opt-in: a checkbox first, the folder page only
  // when it is ticked, pre-filled with a valid default.
  ImportAsk := CreateInputOptionPage(wpSelectTasks, 'Existing NEXUS data (optional)',
    'Did you use NEXUS before from a downloaded folder?',
    'Tick the box only if you want to import projects, settings and the encrypted vault from an earlier ".nexus" ' +
    'data folder. Nothing is imported if this computer already has NEXUS data, and the old folder is never changed.',
    False, False);
  ImportAsk.Add('Import data from an earlier NEXUS folder');
  ImportAsk.Values[0] := False;
  ImportPage := CreateInputDirPage(ImportAsk.ID, 'Existing NEXUS data (optional)',
    'Choose the earlier ".nexus" data folder', 'Select the folder, then click Next.', False, '');
  ImportPage.Add('');
  ImportPage.Values[0] := ExpandConstant('{userdocs}');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = ImportPage.ID) and (not ImportAsk.Values[0]);
end;

function ImportDir(Param: String): String;
begin
  Result := ImportPage.Values[0];
end;

function WantImport: Boolean;
begin
  Result := ImportAsk.Values[0] and (ImportPage.Values[0] <> '') and DirExists(ImportPage.Values[0]);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  // upgrade: stop the running app so files can be replaced; user data is not touched
  if FileExists(ExpandConstant('{app}\runtime\python.exe')) then
    Exec(ExpandConstant('{app}\runtime\python.exe'), '-m nexus stop', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\NEXUS\data');
    if DirExists(DataDir) and (not UninstallSilent) then
      if MsgBox('Also delete your NEXUS data (projects, renders, backups and the encrypted vault) in' + #13#10 + DataDir + '?' + #13#10 + #13#10 +
                'Choose No to keep it for a later reinstall (recommended).', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
