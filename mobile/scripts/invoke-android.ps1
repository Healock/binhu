[CmdletBinding()]
param(
    [ValidateSet('Init', 'Check', 'Test', 'BuildDebug', 'BuildRelease')]
    [string]$Action = 'Check',
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$VersionOverride = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$mobileRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repoRoot = (Resolve-Path (Join-Path $mobileRoot '..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $repoRoot '..')).Path
$appRoot = Join-Path $mobileRoot 'apps\android-tauri'
$sdkRoot = Join-Path $workspaceRoot '.tooling\android\sdk'
$ndkRoot = Join-Path $sdkRoot 'ndk\27.0.12077973'
$javaHome = 'C:\Program Files\Microsoft\jdk-17.0.9.8-hotspot'
$sdkVersion = '10.0.28000.0'
$windowsSdkRoot = Join-Path $workspaceRoot '.tooling\windows-sdk\extracted'
$cargo = (Get-Command cargo.exe -ErrorAction Stop).Source
$cargoBin = Split-Path $cargo -Parent

foreach ($required in @(
    (Join-Path $javaHome 'bin\java.exe'),
    (Join-Path $sdkRoot 'cmdline-tools\19.0\bin\sdkmanager.bat'),
    $ndkRoot
)) {
    if (-not (Test-Path $required)) {
        throw "Android toolchain component is missing: $required"
    }
}

$vcvarsCandidates = @(
    'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat',
    'C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat',
    'E:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat',
    'E:\BuildTools\VC\Auxiliary\Build\vcvars64.bat'
)
$vswhere = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'),
    (Join-Path $env:ProgramFiles 'Microsoft Visual Studio\Installer\vswhere.exe')
) | Where-Object { $_ -and (Test-Path $_ -PathType Leaf) } | Select-Object -First 1
if ($vswhere) {
    $visualStudioRoot = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($visualStudioRoot) {
        $vcvarsCandidates = @((Join-Path $visualStudioRoot 'VC\Auxiliary\Build\vcvars64.bat')) + $vcvarsCandidates
    }
}
$vcvars = $vcvarsCandidates | Where-Object { Test-Path $_ -PathType Leaf } | Select-Object -First 1
if (-not $vcvars) { throw 'Visual Studio C++ tools were not found.' }

& (Join-Path $repoRoot 'desktop\scripts\prepare-windows-sdk.ps1')

$tempRoot = Join-Path $workspaceRoot '.temp\android'
$cargoHome = Join-Path $workspaceRoot '.tooling\cargo-home'
$gradleHome = Join-Path $workspaceRoot '.tooling\gradle-home'
$targetRoot = Join-Path $workspaceRoot '.build\android-target'
New-Item -ItemType Directory -Force -Path $tempRoot, $cargoHome, $gradleHome, $targetRoot | Out-Null

$sdkBin = Join-Path $windowsSdkRoot "base\c\bin\$sdkVersion\x64"
$sdkInclude = @(
    Join-Path $windowsSdkRoot "base\c\Include\$sdkVersion\ucrt"
    Join-Path $windowsSdkRoot "base\c\Include\$sdkVersion\shared"
    Join-Path $windowsSdkRoot "base\c\Include\$sdkVersion\um"
    Join-Path $windowsSdkRoot "base\c\Include\$sdkVersion\winrt"
) -join ';'
$sdkLib = @(
    Join-Path $windowsSdkRoot 'x64\c\um\x64'
    Join-Path $windowsSdkRoot 'x64\c\ucrt\x64'
) -join ';'

switch ($Action) {
    'Init' { $toolCommand = 'npm.cmd run android:init' }
    'Check' { $toolCommand = 'cargo check --manifest-path src-tauri\Cargo.toml' }
    'Test' { $toolCommand = 'cargo test --manifest-path src-tauri\Cargo.toml' }
    'BuildDebug' { $toolCommand = 'npm.cmd run android:build:debug' }
    'BuildRelease' {
        if ($VersionOverride) {
            $overrideConfig = Join-Path $tempRoot "tauri-android-$VersionOverride.json"
            @{ version = $VersionOverride } | ConvertTo-Json -Compress | Set-Content -LiteralPath $overrideConfig -Encoding utf8
            $toolCommand = "npm.cmd run android:build:release -- --config `"$overrideConfig`""
        } else {
            $toolCommand = 'npm.cmd run android:build:release'
        }
    }
}

$command = @(
    "`"$vcvars`""
    "set `"JAVA_HOME=$javaHome`""
    "set `"ANDROID_HOME=$sdkRoot`""
    "set `"ANDROID_SDK_ROOT=$sdkRoot`""
    "set `"NDK_HOME=$ndkRoot`""
    "set `"TEMP=$tempRoot`""
    "set `"TMP=$tempRoot`""
    "set `"CARGO_HOME=$cargoHome`""
    "set `"GRADLE_USER_HOME=$gradleHome`""
    "set `"CARGO_TARGET_DIR=$targetRoot`""
    "set `"BINHU_ANDROID_APP_VERSION=$VersionOverride`""
    "set `"RUSTUP_DIST_SERVER=https://rsproxy.cn`""
    "set `"RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup`""
    "set `"PATH=$cargoBin;$(Join-Path $javaHome 'bin');$(Join-Path $sdkRoot 'platform-tools');$sdkBin;!PATH!`""
    "set `"LIB=$sdkLib;!LIB!`""
    "set `"INCLUDE=$sdkInclude;!INCLUDE!`""
    "cd /d `"$appRoot`""
    $toolCommand
) -join ' && '

& cmd.exe /d /v:on /c $command
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
