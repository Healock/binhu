#ifndef AppVersion
  #define AppVersion "0.28.8"
#endif
#ifndef NumericVersion
  #define NumericVersion "0.28.8.0"
#endif
#ifndef VelopackSetup
  #error VelopackSetup must point to the Velopack setup executable.
#endif
#ifndef WebView2Bootstrapper
  #error WebView2Bootstrapper must point to the Microsoft WebView2 bootstrapper.
#endif
#ifndef SetupIcon
  #error SetupIcon must point to the application icon.
#endif

#define AppName "滨湖智慧平台"
#define AppPublisher "滨湖新城派出所"
#define AppId "{{4C5FC2A4-1A0D-4C79-9FA1-6DF2C8C53B23}"
#define WebView2AppGuid "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={tmp}\BinhuWin10Bootstrap
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
Uninstallable=no
CreateUninstallRegKey=no
OutputDir={#OutputDir}
OutputBaseFilename=Binhu-Win10-x64-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#SetupIcon}
SetupLogging=yes
VersionInfoVersion={#NumericVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#NumericVersion}
VersionInfoDescription={#AppName} Windows 10/11 首次安装程序
VersionInfoCompany={#AppPublisher}

[Files]
Source: "{#WebView2Bootstrapper}"; DestName: "MicrosoftEdgeWebView2Setup.exe"; Flags: dontcopy
Source: "{#VelopackSetup}"; DestName: "Binhu-Velopack-Setup.exe"; Flags: dontcopy

[Icons]
Name: "{userdesktop}\{#AppName}"; Filename: "{localappdata}\Bhzh\BinhuWin10\current\BinhuWin10.exe"; WorkingDir: "{localappdata}\Bhzh\BinhuWin10\current"; IconFilename: "{localappdata}\Bhzh\BinhuWin10\current\BinhuWin10.ico"; IconIndex: 0
Name: "{userappdata}\Microsoft\Windows\Start Menu\Programs\{#AppName}"; Filename: "{localappdata}\Bhzh\BinhuWin10\current\BinhuWin10.exe"; WorkingDir: "{localappdata}\Bhzh\BinhuWin10\current"; IconFilename: "{localappdata}\Bhzh\BinhuWin10\current\BinhuWin10.ico"; IconIndex: 0

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    DeleteFile(ExpandConstant('{userdesktop}\BinhuDesktop.lnk'));
    DeleteFile(ExpandConstant('{userappdata}\Microsoft\Windows\Start Menu\Programs\BinhuDesktop.lnk'));
  end;
end;

function ReadWebView2Version(RootKey: Integer; SubKey: String): String;
begin
  Result := '';
  RegQueryStringValue(RootKey, SubKey, 'pv', Result);
end;

function InstalledWebView2Version(): String;
var
  Version: String;
begin
  Result := '';
  Version := ReadWebView2Version(HKLM64, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{#WebView2AppGuid}');
  if Version = '' then
    Version := ReadWebView2Version(HKLM64, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{#WebView2AppGuid}');
  if Version = '' then
    Version := ReadWebView2Version(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{#WebView2AppGuid}');
  if Version = '' then
    Version := ReadWebView2Version(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{#WebView2AppGuid}');
  Result := Version;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  WebView2SetupPath: String;
  VelopackSetupPath: String;
begin
  Result := '';

  if InstalledWebView2Version() = '' then
  begin
    ExtractTemporaryFile('MicrosoftEdgeWebView2Setup.exe');
    WebView2SetupPath := ExpandConstant('{tmp}\MicrosoftEdgeWebView2Setup.exe');
    if not Exec(WebView2SetupPath, '/silent /install', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) then
    begin
      Result := '无法启动 WebView2 安装程序。';
      Exit;
    end;
    if ResultCode <> 0 then
    begin
      Result := Format('WebView2 安装失败，退出代码：%d。请确认电脑可以访问微软更新服务。', [ResultCode]);
      Exit;
    end;
    if InstalledWebView2Version() = '' then
    begin
      Result := 'WebView2 安装后仍未检测到运行时。';
      Exit;
    end;
  end;

  ExtractTemporaryFile('Binhu-Velopack-Setup.exe');
  VelopackSetupPath := ExpandConstant('{tmp}\Binhu-Velopack-Setup.exe');
  if not Exec(VelopackSetupPath,
    '--silent --installto "' + ExpandConstant('{localappdata}\Bhzh\BinhuWin10') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := '无法启动客户端安装程序。';
    Exit;
  end;
  if ResultCode <> 0 then
    Result := Format('客户端安装失败，退出代码：%d。', [ResultCode]);
end;
