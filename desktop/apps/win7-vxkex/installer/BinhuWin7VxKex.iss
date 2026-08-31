#ifndef AppVersion
  #define AppVersion "0.28.0"
#endif
#ifndef NumericVersion
  #define NumericVersion "0.28.0.0"
#endif
#ifndef VelopackSetup
  #error VelopackSetup must point to the baseline Velopack Setup executable.
#endif
#ifndef VxKexInstaller
  #error VxKexInstaller must point to the reviewed VxKex installer.
#endif
#ifndef SetupIcon
  #error SetupIcon must point to the application icon.
#endif

#define AppName "滨湖智慧平台"
#define AppPublisher "滨湖新城派出所"
#define AppId "{{9274429F-0654-4EA0-BD21-09ED2F5FE937}"
#define RequiredVxKexVersion "1.2.1.2229"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={tmp}\BinhuWin7Bootstrap
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=6.1sp1
Uninstallable=no
CreateUninstallRegKey=no
OutputDir={#OutputDir}
OutputBaseFilename=Binhu-Win7-x64-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#SetupIcon}
SetupLogging=yes
VersionInfoVersion={#NumericVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#NumericVersion}
VersionInfoDescription={#AppName} Windows 7 首次安装程序
VersionInfoCompany={#AppPublisher}

[Files]
Source: "{#VxKexInstaller}"; DestName: "VxKex-Setup.exe"; Flags: dontcopy
Source: "{#VelopackSetup}"; DestName: "Binhu-Velopack-Setup.exe"; Flags: dontcopy

[Icons]
Name: "{userdesktop}\{#AppName}"; Filename: "{localappdata}\Bhzh\BinhuWin7\current\BinhuWin7Launcher.exe"; WorkingDir: "{localappdata}\Bhzh\BinhuWin7\current"; IconFilename: "{localappdata}\Bhzh\BinhuWin7\current\BinhuWin7Launcher.exe"; IconIndex: 0
Name: "{userappdata}\Microsoft\Windows\Start Menu\Programs\{#AppName}"; Filename: "{localappdata}\Bhzh\BinhuWin7\current\BinhuWin7Launcher.exe"; WorkingDir: "{localappdata}\Bhzh\BinhuWin7\current"; IconFilename: "{localappdata}\Bhzh\BinhuWin7\current\BinhuWin7Launcher.exe"; IconIndex: 0

[Code]
var
  VxKexRestartRequired: Boolean;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    DeleteFile(ExpandConstant('{userdesktop}\BinhuDesktop.lnk'));
    DeleteFile(ExpandConstant('{userappdata}\Microsoft\Windows\Start Menu\Programs\BinhuDesktop.lnk'));
  end;
end;

function GetVxKexDirectory(): String;
begin
  Result := ExpandConstant('{autopf64}\VxKex');
end;

function GetVxKexConfigPath(): String;
begin
  Result := AddBackslash(GetVxKexDirectory()) + 'KexCfg.exe';
end;

function InstalledVxKexVersion(): String;
var
  Version: String;
begin
  Result := '';
  if GetVersionNumbersString(GetVxKexConfigPath(), Version) then
    Result := Version;
end;

function ConfigureVxKexFor(TargetPath: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(GetVxKexConfigPath(),
    '/EXE:"' + TargetPath + '" /ENABLE:1 /DISABLEFORCHILD:0 /WINVERSPOOF:WIN10',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  CurrentVersion: String;
  ResultCode: Integer;
  VxKexLoader: String;
  VelopackSetupPath: String;
begin
  Result := '';
  CurrentVersion := InstalledVxKexVersion();
  if CurrentVersion <> '{#RequiredVxKexVersion}' then
  begin
    ExtractTemporaryFile('VxKex-Setup.exe');
    if not Exec(ExpandConstant('{tmp}\VxKex-Setup.exe'), '/SILENTUNATTEND', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) then
    begin
      Result := '无法启动 VxKex 安装程序。';
      Exit;
    end;
    if (ResultCode <> 0) and (ResultCode <> 3010) then
    begin
      Result := Format('VxKex 安装失败，退出代码：%d。', [ResultCode]);
      Exit;
    end;
    if ResultCode = 3010 then
    begin
      NeedsRestart := True;
      VxKexRestartRequired := True;
    end;
    if InstalledVxKexVersion() <> '{#RequiredVxKexVersion}' then
    begin
      Result := 'VxKex 安装后版本校验失败。';
      Exit;
    end;
  end;

  ExtractTemporaryFile('Binhu-Velopack-Setup.exe');
  VelopackSetupPath := ExpandConstant('{tmp}\Binhu-Velopack-Setup.exe');
  if not ConfigureVxKexFor(VelopackSetupPath) then
  begin
    Result := '无法为客户端安装程序启用 Win7 兼容层。';
    Exit;
  end;
  VxKexLoader := AddBackslash(GetVxKexDirectory()) + 'VxKexLdr.exe';
  if not FileExists(VxKexLoader) then
  begin
    Result := '未找到 VxKex 启动器，请重新安装 VxKex。';
    Exit;
  end;
  if not Exec(VxKexLoader,
    '"' + VelopackSetupPath + '" --silent --installto "' + ExpandConstant('{localappdata}\Bhzh\BinhuWin7') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := '无法启动客户端安装程序。';
    Exit;
  end;
  if ResultCode <> 0 then
    Result := Format('客户端安装失败，退出代码：%d。', [ResultCode]);
end;

function NeedRestart(): Boolean;
begin
  Result := VxKexRestartRequired;
end;
