; Script generated for ABOODVIDEO Studio
#ifndef BuildOutputDir
  #define BuildOutputDir AddBackslash(SourcePath) + "dist"
#endif
#ifndef StudioDistDir
  #define StudioDistDir AddBackslash(SourcePath) + "dist\\ABOODVIDEO-Studio"
#endif

#define MyAppName "ABOODVIDEO Studio"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#ifndef MyAppNumericVersion
  #define MyAppNumericVersion "1.0.0.0"
#endif
#define MyAppPublisher "ABOODVIDEO"
#define MyAppExeName "ABOODVIDEO Studio.exe"
#ifndef WebView2Bootstrapper
  #error WebView2Bootstrapper must point to the signed Microsoft Evergreen bootstrapper
#endif

[Setup]
AppId={{D374D82C-8E24-4B42-9AE2-F752C8A9772B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppNumericVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
OutputDir={#BuildOutputDir}
OutputBaseFilename=ABOODVIDEO-Studio-Setup-v{#MyAppVersion}
SetupIconFile={#SourcePath}\studio_platform.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#StudioDistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#WebView2Bootstrapper}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebViewKey = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function RuntimeInRegistry(Root: Integer): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(Root, WebViewKey, 'pv', Version);
  if Result then
    Result := (Version <> '') and (Version <> '0.0.0.0');
end;

function HasWebView2(): Boolean;
begin
  Result := RuntimeInRegistry(HKCU32) or RuntimeInRegistry(HKLM32);
  if IsWin64 then
    Result := Result or RuntimeInRegistry(HKCU64) or RuntimeInRegistry(HKLM64);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
begin
  Result := '';
  if HasWebView2() then
    exit;
  WizardForm.StatusLabel.Caption := 'Installing Microsoft WebView2 Runtime...';
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
      '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
  begin
    Result := 'Unable to start Microsoft WebView2 setup. Check your connection and retry.';
    exit;
  end;
  if ExitCode = 3010 then
    NeedsRestart := True;
  if not HasWebView2() then
    Result := 'Microsoft WebView2 Runtime is required. Connect to the internet and retry setup.';
end;
