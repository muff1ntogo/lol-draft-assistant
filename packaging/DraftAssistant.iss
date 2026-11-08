; Inno Setup script for the Draft Assistant installer (packaging/build-installer.ps1 runs it; see packaging/README.md).
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\Draft Assistant. The data folder,
; %LOCALAPPDATA%\DraftAssistant, is the app's own and isn't touched by installing, updating or uninstalling.
;
; A first install asks how the app will be used (docs/shared-data.md, Stage 5): join a team with an invite code, the
; team's admin (the shared database's URL and tokens), or solo (today's behaviour); then the Riot API key and a source
; checkout to bring the data across from. Secrets go to a file in Setup's own temp folder (deleted by the app as soon as
; it has read them, and by Setup when it ends), never onto a command line, and from there into Windows Credential
; Manager: nothing secret is written into the install folder. An update (the app is already set up, or it runs
; silently from the app's Update button) skips every page, and a silent update starts the app again when it's done.

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
  ModePage: TInputOptionWizardPage;
  InvitePage: TInputQueryWizardPage;
  AdminPage: TInputQueryWizardPage;
  KeyPage: TInputQueryWizardPage;
  ImportPage: TInputDirWizardPage;

function AlreadySetUp(): Boolean;
begin
  Result := FileExists(ExpandConstant('{#DataDir}\data\draftassist.sqlite'));
end;

function JoinMode(): Boolean;
begin
  Result := ModePage.SelectedValueIndex = 0;
end;

function AdminMode(): Boolean;
begin
  Result := ModePage.SelectedValueIndex = 1;
end;

procedure InitializeWizard();
begin
  ModePage := CreateInputOptionPage(wpSelectTasks, 'How will you use Draft Assistant?',
    'You can change this later in Settings, Account.', '', True, False);
  ModePage.Add('Join my team: paste the invite code your team admin sent. The app pulls the team''s stats.');
  ModePage.Add('Team admin: you crawl and publish the stats for the team (Turso database and tokens).');
  ModePage.Add('Solo: crawl on this computer, nothing shared (as before).');
  ModePage.SelectedValueIndex := 2;
  InvitePage := CreateInputQueryPage(ModePage.ID, 'Team invite',
    'Paste the invite code from your team admin.', 'It starts with dav1. and is stored in Windows Credential Manager.');
  InvitePage.Add('Invite code:', True);
  AdminPage := CreateInputQueryPage(InvitePage.ID, 'Team admin',
    'Your Turso database (turso.tech).', 'Both tokens go to Windows Credential Manager, never into the install folder.');
  AdminPage.Add('Database URL (libsql://...):', False);
  AdminPage.Add('Organisation:', False);
  AdminPage.Add('Database name:', False);
  AdminPage.Add('Your display name:', False);
  AdminPage.Add('Full-access database token:', True);
  AdminPage.Add('Platform API token:', True);
  KeyPage := CreateInputQueryPage(AdminPage.ID, 'Riot API key',
    'Optional for team members: needed for your own match history and to scout opponents. The admin needs it to crawl.',
    'Paste your key from developer.riotgames.com. Leave it empty to skip; you can add it later in Settings.');
  KeyPage.Add('Riot API key:', False);
  ImportPage := CreateInputDirPage(KeyPage.ID, 'Bring your data across',
    'Optional: did you use the app from a source checkout before?',
    'Pick that lol-draft-assistant folder (the one with data\ and .env in it) to copy your database, key and tags ' +
    'into the app. Team members bring only their own games, roster, pools and settings: the stats come from the team. ' +
    'Leave it empty to start fresh.',
    False, '');
  ImportPage.Add('');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if AlreadySetUp() and ((PageID = ModePage.ID) or (PageID = InvitePage.ID) or (PageID = AdminPage.ID) or
                         (PageID = KeyPage.ID) or (PageID = ImportPage.ID)) then
    Result := True
  else if (PageID = InvitePage.ID) and not JoinMode() then
    Result := True
  else if (PageID = AdminPage.ID) and not AdminMode() then
    Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  I: Integer;
begin
  Result := True;
  if (CurPageID = InvitePage.ID) and (Pos('dav1.', Trim(InvitePage.Values[0])) <> 1) then
  begin
    MsgBox('An invite code starts with dav1. Paste the whole code your admin sent, or go Back and pick Solo.',
      mbError, MB_OK);
    Result := False;
  end;
  if CurPageID = AdminPage.ID then
    for I := 0 to 5 do
      if Trim(AdminPage.Values[I]) = '' then
      begin
        MsgBox('Every field is needed for the team admin (Settings, Account takes them later too).', mbError, MB_OK);
        Result := False;
        Exit;
      end;
  if (CurPageID = ImportPage.ID) and (Trim(ImportPage.Values[0]) <> '') and
     not FileExists(AddBackslash(Trim(ImportPage.Values[0])) + 'data\draftassist.sqlite') then
  begin
    MsgBox('That folder has no data\draftassist.sqlite. Pick the folder with data\ and .env in it, or leave it empty.',
      mbError, MB_OK);
    Result := False;
  end;
end;

procedure RunCli(Args: String);
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{app}\{#CliExe}'), Args, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Folder, Key, SecretFile: String;
begin
  if (CurStep <> ssPostInstall) or AlreadySetUp() then
    Exit;
  Folder := Trim(ImportPage.Values[0]);
  if Folder <> '' then
  begin
    if JoinMode() then
      RunCli('import-data "' + Folder + '" --user')
    else
      RunCli('import-data "' + Folder + '"');
  end;
  { secrets: a file in Setup's temp folder, read and deleted by the app; never on the command line }
  if JoinMode() then
  begin
    SecretFile := ExpandConstant('{tmp}\invite.txt');
    SaveStringToFile(SecretFile, Trim(InvitePage.Values[0]), False);
    RunCli('account join --file "' + SecretFile + '"');
    DeleteFile(SecretFile);
  end;
  if AdminMode() then
  begin
    SecretFile := ExpandConstant('{tmp}\admin.txt');
    SaveStringToFile(SecretFile,
      'url=' + Trim(AdminPage.Values[0]) + #13#10 + 'org=' + Trim(AdminPage.Values[1]) + #13#10 +
      'database=' + Trim(AdminPage.Values[2]) + #13#10 + 'name=' + Trim(AdminPage.Values[3]) + #13#10 +
      'db_token=' + Trim(AdminPage.Values[4]) + #13#10 + 'platform_token=' + Trim(AdminPage.Values[5]) + #13#10, False);
    RunCli('account admin --file "' + SecretFile + '"');
    DeleteFile(SecretFile);
  end;
  Key := Trim(KeyPage.Values[0]);
  if Key <> '' then
  begin
    ForceDirectories(ExpandConstant('{#DataDir}'));
    { appended after any imported .env: the last RIOT_API_KEY line wins (config.parse_env_file) }
    SaveStringToFile(ExpandConstant('{#DataDir}\.env'), #13#10 + 'RIOT_API_KEY=' + Key + #13#10, True);
  end;
end;
