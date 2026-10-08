; Inno Setup script (https://jrsoftware.org/isinfo.php). Build with:  iscc /DAppVersion=0.1.0 installer\ModpackDoctor.iss
; Expects PyInstaller's output in dist\ModpackDoctor\ (see ModpackDoctor.spec).
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{B7D0C1E4-52A1-4C3B-9E57-6A0F3D9B21C8}
AppName=Modpack Doctor
AppVersion={#AppVersion}
AppPublisher=Modpack Doctor
DefaultDirName={autopf}\Modpack Doctor
DefaultGroupName=Modpack Doctor
DisableProgramGroupPage=yes
; Installs for the current user by default (no admin prompt); the user can choose "all users" in the dialog.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=ModpackDoctor-Setup-{#AppVersion}
SetupIconFile=ModpackDoctor.ico
UninstallDisplayIcon={app}\ModpackDoctor.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\ModpackDoctor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Modpack Doctor"; Filename: "{app}\ModpackDoctor.exe"
Name: "{group}\Uninstall Modpack Doctor"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Modpack Doctor"; Filename: "{app}\ModpackDoctor.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ModpackDoctor.exe"; Description: "Launch Modpack Doctor"; Flags: nowait postinstall skipifsilent

; Settings and unpacked packs live in the user's folders (~\.modpack_doctor and ~\ModpackDoctor) and are
; deliberately left alone on uninstall.
