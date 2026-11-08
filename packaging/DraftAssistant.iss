; Inno Setup script for the Draft Assistant installer (packaging/build-installer.ps1 runs it; see packaging/README.md).
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\Draft Assistant. The data folder,
; %LOCALAPPDATA%\DraftAssistant, is the app's own and isn't touched by installing, updating or uninstalling.
;
; A first install asks two optional things: the Riot API key, and a source checkout to bring the data across from.
; An update (the app is already set up, or it runs silently from the app's Update button) skips both, and a silent
; update starts the app again when it's done.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Draft Assistant"
#define AppExe "DraftAssistant.exe"
#define CliExe "draftassist-cli.exe"
#define DataDir "{localappdata}\DraftAssistant"

[Setup]
AppId={{6F3D2B8E-5C1A-4E7B-9A2D-D4A7E1C3B9F0}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Draft Assistant
AppUpdatesURL=https://github.com/muff1ntogo/lol-draft-assistant/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=DraftAssistant-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=yes
#ifexist "app.ico"
SetupIconFile=app.ico
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\DraftAssistant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; An update replaces the program files whole: no stale modules from the version before
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} jobs (command prompt)"; Filename: "{cmd}"; \
  Parameters: "/k ""{app}\{#CliExe}"" --help"; WorkingDir: "{app}"
Name: "{group}\Data folder"; Filename: "{#DataDir}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Dirs]
Name: "{#DataDir}"; Flags: uninsneveruninstall

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent
; an update from the app's Update button runs silently: start the app again
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: WizardSilent

[Code]
var
  KeyPage: TInputQueryWizardPage;
  InvitePage: TInputQueryWizardPage;
  ImportPage: TInputDirWizardPage;
  ImportArgs: String;

function AlreadySetUp(): Boolean;
begin
  Result := FileExists(ExpandConstant('{#DataDir}\data\draftassist.sqlite'));
end;

procedure InitializeWizard();
begin
  KeyPage := CreateInputQueryPage(wpSelectTasks, 'Riot API key',
    'Optional: lets the app read match history and scout your opponents.',
    'Paste your key from developer.riotgames.com. Leave it empty to skip; you can add it later in Settings.');
  KeyPage.Add('Riot API key:', False);
  InvitePage := CreateInputQueryPage(KeyPage.ID, 'Team invite',
    'Optional: use the shared team statistics instead of crawling locally.',
    'Paste the invite code from your team admin. Leave it empty for solo mode or team-admin setup.');
  InvitePage.Add('Invite code:', False);
  ImportPage := CreateInputDirPage(InvitePage.ID, 'Bring your data across',
    'Optional: did you use the app from a source checkout before?',
    'Pick that lol-draft-assistant folder (the one with data\ and .env in it) to copy your database, key and tags ' +
    'into the app. If the project still sits one folder down, pick that inner folder. Leave it empty to start fresh.',
    False, '');
  ImportPage.Add('');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := ((PageID = KeyPage.ID) or (PageID = InvitePage.ID) or (PageID = ImportPage.ID)) and AlreadySetUp();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = ImportPage.ID) and (Trim(ImportPage.Values[0]) <> '') and
     not FileExists(AddBackslash(Trim(ImportPage.Values[0])) + 'data\draftassist.sqlite') then
  begin
    MsgBox('That folder has no data\draftassist.sqlite. Pick the folder with data\ and .env in it, or leave it empty.',
      mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Folder, Key: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  Folder := Trim(ImportPage.Values[0]);
  if (Trim(InvitePage.Values[0]) <> '') and not AlreadySetUp() then
    Exec(ExpandConstant('{app}\{#CliExe}'), 'account join "' + Trim(InvitePage.Values[0]) + '"', '', SW_HIDE, ewWaitUntilTerminated,
      ResultCode);
  if (Folder <> '') and not AlreadySetUp() then
  begin
    ImportArgs := 'import-data "' + Folder + '"';
    if Trim(InvitePage.Values[0]) <> '' then
      ImportArgs := ImportArgs + ' --user';
    Exec(ExpandConstant('{app}\{#CliExe}'), ImportArgs, '', SW_HIDE, ewWaitUntilTerminated,
      ResultCode);
  end;
  Key := Trim(KeyPage.Values[0]);
  if Key <> '' then
  begin
    ForceDirectories(ExpandConstant('{#DataDir}'));
    { appended after any imported .env: the last RIOT_API_KEY line wins (config.parse_env_file) }
    SaveStringToFile(ExpandConstant('{#DataDir}\.env'), #13#10 + 'RIOT_API_KEY=' + Key + #13#10, True);
  end;
end;
