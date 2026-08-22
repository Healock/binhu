[CmdletBinding()]
param(
    [string]$WorkspaceRoot = 'E:\bhzh-forth',
    [string]$PreviousFullPackage
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot 'workspace-layout.ps1')
$layout = Resolve-BinhuWorkspaceLayout -Root $WorkspaceRoot
$desktopRoot = Join-Path $layout.RepoRoot 'desktop'
$packRoot = Join-Path $layout.DataRoot '.build\win10-tauri\publish'
$outputRoot = Join-Path $layout.DataRoot 'artifacts\updates\win10-x64'
& (Join-Path $desktopRoot 'scripts\invoke-tauri.ps1') -Action Build -PublishDirectory $packRoot | Out-Host
if (Test-Path -LiteralPath $outputRoot) { Remove-Item -LiteralPath $outputRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$args = @{ Target = 'win10-x64'; PackDirectory = $packRoot; MainExecutable = 'BinhuWin10.exe'; OutputDirectory = $outputRoot }
if ($PreviousFullPackage) { $args.PreviousFullPackage = $PreviousFullPackage }
& (Join-Path $desktopRoot 'scripts\invoke-velopack.ps1') @args
& (Join-Path $desktopRoot 'scripts\write-release-checksums.ps1') -ReleaseDirectory $outputRoot
Write-Host $outputRoot
