; Inno Setup script for Modpack Doctor. Built by tools/build_windows.py (or the Release workflow).
; Run from the repository folder:  ISCC /DAppVersion=4.1 tools\installer.iss

#ifndef AppVersion
  #define AppVersion "0.0"
#endif

[Setup]
AppId={{6F3C9B52-8D1E-4B7A-9C0E-4D2A7E1F5B38}
AppName=Modpack Doctor
AppVersion={#AppVersion}
AppPublisher=Modpack Doctor
AppSupportURL=https://relay.darkmoonhollow.xyz
DefaultDirName={autopf}\Modpack Doctor
DefaultGroupName=Modpack Doctor
DisableProgramGroupPage=yes
; Lets the user pick the install folder (the default page is not skipped) and install just for themselves without admin.
DisableDirPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile=..\LICENSE
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\ModpackDoctor.exe
OutputDir=..\dist
OutputBaseFilename=ModpackDoctor-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startmenuicon"; Description: "Create a &Start Menu shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\ModpackDoctor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Modpack Doctor"; Filename: "{app}\ModpackDoctor.exe"; Tasks: startmenuicon
Name: "{autodesktop}\Modpack Doctor"; Filename: "{app}\ModpackDoctor.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ModpackDoctor.exe"; Description: "Start Modpack Doctor"; Flags: nowait postinstall skipifsilent
; An update started by the app itself runs this installer silently; start the app again afterwards.
Filename: "{app}\ModpackDoctor.exe"; Flags: nowait; Check: WizardSilent
