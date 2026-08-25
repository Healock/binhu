[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $repoRoot '..')).Path
$toolingRoot = Join-Path $workspaceRoot '.tooling\android'
$sdkRoot = Join-Path $toolingRoot 'sdk'
$downloadRoot = Join-Path $toolingRoot 'downloads'
$archivePath = Join-Path $downloadRoot 'commandlinetools-win-13114758_latest.zip'
$archiveUrl = 'https://dl.google.com/android/repository/commandlinetools-win-13114758_latest.zip'
$archiveSha1 = '54a582f3bf73e04253602f2d1c80bd5868aac115'
$commandLineRoot = Join-Path $sdkRoot 'cmdline-tools\19.0'
$sdkManager = Join-Path $commandLineRoot 'bin\sdkmanager.bat'
$jdkCandidates = @(
    'C:\Program Files\Microsoft\jdk-17.0.9.8-hotspot',
    'C:\Program Files\Eclipse Adoptium\jdk-17*',
    $env:JAVA_HOME
)
$javaHome = $jdkCandidates |
    Where-Object { $_ } |
    ForEach-Object { Get-Item $_ -ErrorAction SilentlyContinue } |
    Where-Object { Test-Path (Join-Path $_.FullName 'bin\java.exe') -PathType Leaf } |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $javaHome) {
    throw 'JDK 17 was not found. Install a 64-bit JDK 17 before preparing Android.'
}

New-Item -ItemType Directory -Force -Path $toolingRoot, $sdkRoot, $downloadRoot | Out-Null
if (-not (Test-Path $archivePath -PathType Leaf)) {
    Write-Host 'Downloading Android command-line tools...'
    Invoke-WebRequest -UseBasicParsing $archiveUrl -OutFile $archivePath
}
$sha1 = [System.Security.Cryptography.SHA1]::Create()
$archiveStream = [System.IO.File]::OpenRead($archivePath)
try {
    $actualSha1 = ([System.BitConverter]::ToString($sha1.ComputeHash($archiveStream))).Replace('-', '').ToLowerInvariant()
}
finally {
    $archiveStream.Dispose()
    $sha1.Dispose()
}
if ($actualSha1 -ne $archiveSha1) {
    throw "Android command-line tools checksum mismatch: $actualSha1"
}

if (-not (Test-Path $sdkManager -PathType Leaf)) {
    $extractRoot = Join-Path $toolingRoot 'commandline-tools-extracted'
    if (Test-Path $extractRoot) { Remove-Item -LiteralPath $extractRoot -Recurse -Force }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force
    New-Item -ItemType Directory -Force -Path (Split-Path $commandLineRoot -Parent) | Out-Null
    if (Test-Path $commandLineRoot) { Remove-Item -LiteralPath $commandLineRoot -Recurse -Force }
    Move-Item -LiteralPath (Join-Path $extractRoot 'cmdline-tools') -Destination $commandLineRoot
    Remove-Item -LiteralPath $extractRoot -Recurse -Force
}

$env:JAVA_HOME = $javaHome
$env:ANDROID_HOME = $sdkRoot
$env:ANDROID_SDK_ROOT = $sdkRoot
$env:PATH = "$(Join-Path $javaHome 'bin');$(Join-Path $sdkRoot 'platform-tools');$env:PATH"

Write-Host 'Accepting Android SDK licenses...'
1..100 | ForEach-Object { 'y' } | & $sdkManager --sdk_root=$sdkRoot --licenses | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Android SDK license acceptance failed.' }

Write-Host 'Installing pinned Android SDK and NDK packages...'
& $sdkManager --sdk_root=$sdkRoot `
    'platform-tools' `
    'platforms;android-36' `
    'build-tools;36.0.0' `
    'ndk;27.0.12077973'
if ($LASTEXITCODE -ne 0) { throw 'Android SDK package installation failed.' }

$previousDistServer = $env:RUSTUP_DIST_SERVER
$previousUpdateRoot = $env:RUSTUP_UPDATE_ROOT
$env:RUSTUP_DIST_SERVER = 'https://rsproxy.cn'
$env:RUSTUP_UPDATE_ROOT = 'https://rsproxy.cn/rustup'
& rustup target add aarch64-linux-android armv7-linux-androideabi i686-linux-android x86_64-linux-android
if ($null -eq $previousDistServer) { Remove-Item Env:RUSTUP_DIST_SERVER -ErrorAction SilentlyContinue } else { $env:RUSTUP_DIST_SERVER = $previousDistServer }
if ($null -eq $previousUpdateRoot) { Remove-Item Env:RUSTUP_UPDATE_ROOT -ErrorAction SilentlyContinue } else { $env:RUSTUP_UPDATE_ROOT = $previousUpdateRoot }
if ($LASTEXITCODE -ne 0) { throw 'Rust Android target installation failed.' }

Write-Host "JAVA_HOME=$javaHome"
Write-Host "ANDROID_HOME=$sdkRoot"
Write-Host "NDK_HOME=$(Join-Path $sdkRoot 'ndk\27.0.12077973')"
