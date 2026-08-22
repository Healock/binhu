[CmdletBinding()]
param(
    [string]$WorkspaceRoot = 'E:\bhzh-forth',
    [string]$ElectronArchive,
    [string]$VxKexInstaller
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$WorkspaceRoot = [System.IO.Path]::GetFullPath($WorkspaceRoot)
$desktopRoot = Join-Path $WorkspaceRoot 'source\desktop'
$releaseRoot = Join-Path $WorkspaceRoot 'release'
$buildRoot = Join-Path $WorkspaceRoot '.build\win7-vxkex'
$runtimeRoot = Join-Path $buildRoot 'runtime'
$artifactRoot = Join-Path $WorkspaceRoot 'artifacts'
$appVersion = (Get-Content (Join-Path $WorkspaceRoot 'source\VERSION') -Raw).Trim()

if (-not $ElectronArchive) {
    $ElectronArchive = Join-Path $releaseRoot 'electron-v36.0.0-win32-x64.zip'
}
if (-not $VxKexInstaller) {
    $VxKexInstaller = Join-Path $releaseRoot 'KexSetup_Release_1_2_1_2229.exe'
}

$ElectronArchive = [System.IO.Path]::GetFullPath($ElectronArchive)
$VxKexInstaller = [System.IO.Path]::GetFullPath($VxKexInstaller)
$expectedElectronHash = '3690467f4cb67752cdad90962bb3bee252dafcbfb12834d853e36d97117cd5b2'

foreach ($requiredFile in @($ElectronArchive, $VxKexInstaller)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Missing POC input: $requiredFile"
    }
}

$electronHash = (Get-FileHash -LiteralPath $ElectronArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($electronHash -ne $expectedElectronHash) {
    throw "Electron archive SHA-256 mismatch: $electronHash"
}
$vxkexHash = (Get-FileHash -LiteralPath $VxKexInstaller -Algorithm SHA256).Hash.ToLowerInvariant()

if (Test-Path -LiteralPath $buildRoot) {
    $resolvedBuildRoot = [System.IO.Path]::GetFullPath($buildRoot)
    if (-not $resolvedBuildRoot.StartsWith($WorkspaceRoot + [System.IO.Path]::DirectorySeparatorChar)) {
        throw "Refusing to replace build output outside workspace: $resolvedBuildRoot"
    }
    Remove-Item -LiteralPath $resolvedBuildRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null

Expand-Archive -LiteralPath $ElectronArchive -DestinationPath $runtimeRoot
if (-not (Test-Path (Join-Path $runtimeRoot 'electron.exe') -PathType Leaf)) {
    throw 'Electron archive did not contain electron.exe.'
}

& (Join-Path $desktopRoot 'apps\win7-electron\scripts\assemble-app.ps1') -RuntimeRoot $runtimeRoot

Rename-Item -LiteralPath (Join-Path $runtimeRoot 'electron.exe') -NewName 'BinhuWin7.exe'
Copy-Item -LiteralPath (Join-Path $desktopRoot 'apps\win10-tauri\src-tauri\icons\icon.ico') `
    -Destination (Join-Path $runtimeRoot 'BinhuWin7.ico')
New-Item -ItemType Directory -Force -Path (Join-Path $runtimeRoot 'prerequisites') | Out-Null
Copy-Item -LiteralPath $VxKexInstaller -Destination (Join-Path $runtimeRoot 'prerequisites\VxKex-Setup.exe')

$manifest = [ordered]@{
    schema_version = 1
    app_version = $appVersion
    target = 'windows-7-sp1-x64'
    provider = 'official-electron-with-vxkex'
    electron_version = '36.0.0'
    chromium_version = '136.0.7103.48'
    electron_archive = [System.IO.Path]::GetFileName($ElectronArchive)
    electron_archive_sha256 = $electronHash
    vxkex_version = '1.2.1.2229'
    vxkex_installer = [System.IO.Path]::GetFileName($VxKexInstaller)
    vxkex_installer_sha256 = $vxkexHash
    required_windows_updates = @('KB2533623', 'KB2670838')
    chromium_switches = @('disable-direct-composition')
    status = 'win7-real-machine-validation-required'
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runtimeRoot 'runtime-manifest.json') -Encoding UTF8

$installScript = @'
@echo off
setlocal
if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if "%PROCESSOR_ARCHITEW6432%"=="" (
  echo This package requires 64-bit Windows 7 SP1.
  pause
  exit /b 1
)
net session >nul 2>&1
if not "%errorlevel%"=="0" (
  echo Administrator rights are required to install and configure VxKex.
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "KEXDIR=%ProgramFiles%\VxKex"
set "INSTALLED_KEX_VERSION="
if exist "%KEXDIR%\KexCfg.exe" (
  for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "(Get-Item -LiteralPath '%KEXDIR%\KexCfg.exe').VersionInfo.FileVersion"`) do set "INSTALLED_KEX_VERSION=%%V"
)
if not "%INSTALLED_KEX_VERSION%"=="1.2.1.2229" (
  echo Installing or upgrading VxKex to 1.2.1.2229...
  start /wait "" "%~dp0prerequisites\VxKex-Setup.exe" /SILENTUNATTEND
)
if not exist "%KEXDIR%\KexCfg.exe" (
  echo VxKex installation was not completed.
  pause
  exit /b 1
)
if not exist "%KEXDIR%\VxKexLdr.exe" (
  echo VxKexLdr.exe is missing. VxKex must be repaired or reinstalled.
  pause
  exit /b 1
)

echo Enabling VxKex for BinhuWin7.exe...
"%KEXDIR%\KexCfg.exe" /EXE:"%~dp0BinhuWin7.exe" /ENABLE:1 /DISABLEFORCHILD:0 /WINVERSPOOF:WIN10
if errorlevel 1 (
  echo VxKex configuration failed.
  pause
  exit /b 1
)
echo VxKex configuration completed.
pause
'@
Set-Content -LiteralPath (Join-Path $runtimeRoot 'Install-VxKex.cmd') -Value $installScript -Encoding ASCII

$startScript = @'
@echo off
setlocal
if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if "%PROCESSOR_ARCHITEW6432%"=="" (
  echo This package requires 64-bit Windows 7 SP1.
  pause
  exit /b 1
)
set "KEXLDR=%ProgramFiles%\VxKex\VxKexLdr.exe"
if not exist "%KEXLDR%" (
  echo VxKex is not installed. Run Install-VxKex.cmd first.
  pause
  exit /b 1
)
start "" "%KEXLDR%" "%~dp0BinhuWin7.exe" --disable-direct-composition
'@
Set-Content -LiteralPath (Join-Path $runtimeRoot 'Start-Binhu.cmd') -Value $startScript -Encoding ASCII

$artifactBase = "Binhu-Win7-x64-VxKex-POC-$appVersion"
$artifactPath = Join-Path $artifactRoot "$artifactBase.zip"
if (Test-Path -LiteralPath $artifactPath) {
    Remove-Item -LiteralPath $artifactPath -Force
}
Compress-Archive -Path (Join-Path $runtimeRoot '*') -DestinationPath $artifactPath -CompressionLevel Optimal
$artifactHash = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $artifactRoot "$artifactBase.sha256") -Value "$artifactHash  $artifactBase.zip" -Encoding ASCII

Write-Host "VxKex POC: $artifactPath"
Write-Host "SHA-256: $artifactHash"
