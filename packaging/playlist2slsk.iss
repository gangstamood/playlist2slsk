; Installateur Windows (Inno Setup). Construit par GitHub Actions après PyInstaller.
#define AppVer GetEnv("APP_VERSION")
#if AppVer == ""
  #define AppVer "1.0"
#endif

[Setup]
AppId={{8F5B3C2A-6D41-4E8B-9A57-2C1F0B7E3D19}
AppName=Playlist2Slsk
AppVersion={#AppVer}
AppPublisher=Lutin
AppPublisherURL=https://github.com/gangstamood/playlist2slsk
AppCopyright=© Lutin · GNU AGPL v3
LicenseFile=..\LICENSE
DefaultDirName={localappdata}\Programs\Playlist2Slsk
DefaultGroupName=Playlist2Slsk
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Playlist2Slsk-Windows-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Playlist2Slsk.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Playlist2Slsk\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "Lisez-moi-Windows.txt"; DestDir: "{app}"; DestName: "Lisez-moi.txt"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "Licence (GNU AGPL v3).txt"; Flags: ignoreversion
Source: "..\CREDITS.md"; DestDir: "{app}"; DestName: "Crédits.txt"; Flags: ignoreversion

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\Playlist2Slsk"; Filename: "{app}\Playlist2Slsk.exe"
Name: "{autoprograms}\Playlist2Slsk - Lisez-moi"; Filename: "{app}\Lisez-moi.txt"
Name: "{autodesktop}\Playlist2Slsk"; Filename: "{app}\Playlist2Slsk.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Playlist2Slsk.exe"; Description: "{cm:LaunchProgram,Playlist2Slsk}"; Flags: nowait postinstall skipifsilent
; Mise à jour lancée depuis l'appli (installation silencieuse) : on la rouvre à la fin.
Filename: "{app}\Playlist2Slsk.exe"; Flags: nowait; Check: Relaunch

[Code]
function Relaunch: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:relaunch|0}') = '1');
end;
