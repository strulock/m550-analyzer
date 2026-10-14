; BMW Analyzer installer (Inno Setup 6). Built by CI:
;   iscc /DAppVersion=0.3.3 installer\BMW-Analyzer.iss
; -> dist\BMW-Analyzer-Setup.exe
;
; Per-user: no admin prompt, installs to %LOCALAPPDATA%\Programs\BMW Analyzer.
; The app updates itself by downloading the next release's installer and
; running it silently.
; The AppId must not change: it ties upgrades and the uninstall entry together
; (n63read.update.APP_ID refers to it).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{15CE8A75-71CE-4317-BF3E-1730307815CD}
AppName=BMW Analyzer
AppVersion={#AppVersion}
AppVerName=BMW Analyzer {#AppVersion}
AppPublisher=strulock
AppPublisherURL=https://github.com/strulock/m550-analyzer
AppSupportURL=https://github.com/strulock/m550-analyzer/issues
AppUpdatesURL=https://github.com/strulock/m550-analyzer/releases
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\BMW Analyzer
DefaultGroupName=BMW Analyzer
DisableProgramGroupPage=yes
UninstallDisplayName=BMW Analyzer
UninstallDisplayIcon={app}\n63read.exe
CloseApplications=force
RestartApplications=no
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=BMW-Analyzer-Setup
SetupIconFile=..\src\n63read\data\n63read.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The folder build: n63read.exe, n63read-cli.exe and _internal\.
Source: "..\dist\BMW Analyzer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Start each version clean: nothing left over from the one before. The *.old,
; *.previous and *.new files are what the one-file builds' self-update left.
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\*.old"
Type: files; Name: "{app}\*.previous"
Type: files; Name: "{app}\*.new"

[Dirs]
; Drive logs, fault lists and diagnostics reports go here by default. Never
; removed on uninstall: it holds the user's own files.
Name: "{userdocs}\BMW Analyzer Logs"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\BMW Analyzer"; Filename: "{app}\n63read.exe"
Name: "{group}\BMW Analyzer Logs"; Filename: "{userdocs}\BMW Analyzer Logs"
Name: "{group}\BMW Analyzer command prompt"; Filename: "{cmd}"; Parameters: "/k cd /d ""{app}"" && n63read-cli.exe --help"; WorkingDir: "{app}"
Name: "{group}\Uninstall BMW Analyzer"; Filename: "{uninstallexe}"
Name: "{autodesktop}\BMW Analyzer"; Filename: "{app}\n63read.exe"; Tasks: desktopicon

[UninstallRun]
; Take n63read out of Claude Code / Claude Desktop, if it was added.
Filename: "{app}\n63read-cli.exe"; Parameters: "mcp --unregister"; Flags: runhidden waituntilterminated; RunOnceId: "UnregisterClaude"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{localappdata}\n63read\updates"

[Run]
Filename: "{app}\n63read.exe"; Description: "{cm:LaunchProgram,BMW Analyzer}"; Flags: nowait postinstall skipifsilent
; The app's "Restart to update" runs this silently with /RELAUNCH=1.
Filename: "{app}\n63read.exe"; Flags: nowait; Check: Relaunch

[Code]
function Relaunch: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:relaunch|0}') = '1');
end;
