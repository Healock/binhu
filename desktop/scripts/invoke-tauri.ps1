param(
    [ValidateSet('Check', 'Build', 'FmtCheck')]
    [string]$Action = 'Check',
    [string]$PublishDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$desktopRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repoRoot = (Resolve-Path (Join-Path $desktopRoot '..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $repoRoot '..')).Path
$tauriRoot = Join-Path $desktopRoot 'apps\win10-tauri'
$rustRoot = Join-Path $tauriRoot 'src-tauri'
$sdkRoot = Join-Path $workspaceRoot '.tooling\windows-sdk\extracted'
$sdkVersion = '10.0.28000.0'
$cargoCandidates = @(
    (Join-Path $env:USERPROFILE '.cargo\bin\cargo.exe'),
    'C:\Users\Administrator\.cargo\bin\cargo.exe'
)
$cargoCommand = Get-Command cargo.exe -ErrorAction SilentlyContinue
if ($cargoCommand) { $cargoCandidates = @($cargoCommand.Source) + $cargoCandidates }
$cargo = $cargoCandidates | Where-Object { Test-Path $_ -PathType Leaf } | Select-Object -First 1
if (-not $cargo) { throw 'Rust Cargo was not found.' }
$cargoBin = Split-Path $cargo -Parent
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
        $vcvarsCandidates = @(
            (Join-Path $visualStudioRoot 'VC\Auxiliary\Build\vcvars64.bat')
        ) + $vcvarsCandidates
    }
}
$vcvars = $vcvarsCandidates | Where-Object { Test-Path $_ -PathType Leaf } | Select-Object -First 1
if (-not $vcvars) { throw 'Visual Studio C++ tools were not found.' }

& (Join-Path $PSScriptRoot 'prepare-windows-sdk.ps1')

$tempRoot = Join-Path $workspaceRoot '.temp'
$cargoHome = Join-Path $workspaceRoot '.tooling\cargo-home'
$targetRoot = Join-Path $workspaceRoot '.build\tauri-target'
$localAppData = Join-Path $workspaceRoot '.tooling\local-app-data'
New-Item -ItemType Directory -Force -Path $tempRoot, $cargoHome, $targetRoot, $localAppData | Out-Null

$env:TEMP = $tempRoot
$env:TMP = $tempRoot

$sdkBin = Join-Path $sdkRoot "base\c\bin\$sdkVersion\x64"
$sdkInclude = @(
    Join-Path $sdkRoot "base\c\Include\$sdkVersion\ucrt"
    Join-Path $sdkRoot "base\c\Include\$sdkVersion\shared"
    Join-Path $sdkRoot "base\c\Include\$sdkVersion\um"
    Join-Path $sdkRoot "base\c\Include\$sdkVersion\winrt"
    Join-Path $sdkRoot "base\c\Include\$sdkVersion\cppwinrt"
) -join ';'
$sdkLib = @(
    Join-Path $sdkRoot 'x64\c\um\x64'
    Join-Path $sdkRoot 'x64\c\ucrt\x64'
) -join ';'

switch ($Action) {
    'Check' { $workingDirectory = $rustRoot; $toolCommand = "`"$cargo`" check --locked" }
    'FmtCheck' { $workingDirectory = $rustRoot; $toolCommand = "`"$cargo`" fmt --check" }
    'Build' {
        $workingDirectory = $tauriRoot
        $toolCommand = "npm.cmd run build"
    }
}

$command = @(
    "`"$vcvars`""
    "set `"PATH=$cargoBin;$sdkBin;!PATH!`""
    "set `"LIB=$sdkLib;!LIB!`""
    "set `"INCLUDE=$sdkInclude;!INCLUDE!`""
    "set `"CARGO_HOME=$cargoHome`""
    "set `"CARGO_TARGET_DIR=$targetRoot`""
    "set `"LOCALAPPDATA=$localAppData`""
    "cd /d `"$workingDirectory`""
    $toolCommand
) -join ' && '

& cmd.exe /d /v:on /c $command
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($Action -eq 'Build') {
    $builtExecutable = Join-Path $targetRoot 'release\binhu-win10-tauri.exe'
    if (-not (Test-Path -LiteralPath $builtExecutable -PathType Leaf)) {
        throw "Tauri release executable was not produced: $builtExecutable"
    }
    if (-not $PublishDirectory) {
        $PublishDirectory = Join-Path $workspaceRoot '.build\win10-tauri\publish'
    }
    $publishRoot = [System.IO.Path]::GetFullPath($PublishDirectory)
    New-Item -ItemType Directory -Force -Path $publishRoot | Out-Null
    Get-ChildItem -LiteralPath $publishRoot -Force | Remove-Item -Recurse -Force
    Copy-Item -LiteralPath $builtExecutable -Destination (Join-Path $publishRoot 'BinhuWin10.exe') -Force
    Write-Host $publishRoot
}
