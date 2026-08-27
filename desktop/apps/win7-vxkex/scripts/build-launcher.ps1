[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$source = (Resolve-Path (Join-Path $PSScriptRoot '..\launcher\BinhuWin7Launcher.cpp')).Path
$resourceSource = (Resolve-Path (Join-Path $PSScriptRoot '..\launcher\BinhuWin7Launcher.rc')).Path
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
$desktopRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$repoRoot = (Resolve-Path (Join-Path $desktopRoot '..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $repoRoot '..')).Path
$sdkRoot = Join-Path $workspaceRoot '.tooling\windows-sdk\extracted'
$sdkVersion = '10.0.28000.0'
$vcvarsCandidates = @(
    'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat',
    'C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat',
    'E:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat',
    'E:\BuildTools\VC\Auxiliary\Build\vcvars64.bat'
)
$vswhere = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'),
    (Join-Path $env:ProgramFiles 'Microsoft Visual Studio\Installer\vswhere.exe')
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
if ($vswhere) {
    $visualStudioRoot = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($visualStudioRoot) {
        $vcvarsCandidates = @(
            (Join-Path $visualStudioRoot 'VC\Auxiliary\Build\vcvars64.bat')
        ) + $vcvarsCandidates
    }
}
$vcvars = $vcvarsCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $vcvars) { throw 'Visual Studio C++ build tools were not found.' }
& (Join-Path $desktopRoot 'scripts\prepare-windows-sdk.ps1')

$sdkBin = Join-Path $sdkRoot "base\c\bin\$sdkVersion\x64"
$sdkInclude = @(
    Join-Path $sdkRoot "base\c\Include\$sdkVersion\ucrt"
    Join-Path $sdkRoot "base\c\Include\$sdkVersion\shared"
    Join-Path $sdkRoot "base\c\Include\$sdkVersion\um"
) -join ';'
$sdkLib = @(
    Join-Path $sdkRoot 'x64\c\um\x64'
    Join-Path $sdkRoot 'x64\c\ucrt\x64'
) -join ';'

New-Item -ItemType Directory -Force -Path $output | Out-Null
$exe = Join-Path $output 'BinhuWin7Launcher.exe'
$object = Join-Path $output 'BinhuWin7Launcher.obj'
$resource = Join-Path $output 'BinhuWin7Launcher.res'
$command = @(
    "`"$vcvars`""
    "set `"PATH=$sdkBin;!PATH!`""
    "set `"LIB=$sdkLib;!LIB!`""
    "set `"INCLUDE=$sdkInclude;!INCLUDE!`""
    "rc.exe /nologo /fo`"$resource`" `"$resourceSource`""
    "cl.exe /nologo /utf-8 /std:c++17 /O2 /MT /EHsc /DUNICODE /D_UNICODE /Fo`"$object`" /Fe`"$exe`" `"$source`" `"$resource`" /link /SUBSYSTEM:WINDOWS user32.lib shell32.lib ole32.lib"
) -join ' && '
& cmd.exe /d /v:on /s /c $command
if ($LASTEXITCODE -ne 0) { throw "Win7 launcher compilation failed with exit code $LASTEXITCODE." }
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw "Launcher was not produced: $exe" }
Remove-Item -LiteralPath $object, $resource -Force -ErrorAction SilentlyContinue

Write-Host "Win7 launcher: $exe"
