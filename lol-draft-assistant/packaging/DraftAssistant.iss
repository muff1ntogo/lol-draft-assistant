; Inno Setup script for the Draft Assistant installer (packaging/build-installer.ps1 runs it; see packaging/README.md).
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\Draft Assistant. The data folder,
; %LOCALAPPDATA%\DraftAssistant, is the app's own and isn't touched by installing, updating or uninstalling.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Draft Assistant"
#define AppExe "DraftAssistant.exe"
#define CliExe "draftassist-cli.exe"

[Setup]
AppId={{6F3D2B8E-5C1A-4E7B-9A2D-D4A7E1C3B9F0}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Draft Assistant
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=DraftAssistant-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
#ifexist "app.ico"
SetupIconFile=app.ico
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\DraftAssistant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} jobs (command prompt)"; Filename: "{cmd}"; \
  Parameters: "/k ""{app}\{#CliExe}"" --help"; WorkingDir: "{app}"
Name: "{group}\Data folder"; Filename: "{localappdata}\DraftAssistant"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Dirs]
Name: "{localappdata}\DraftAssistant"; Flags: uninsneveruninstall

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent
