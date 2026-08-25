[CmdletBinding()]
param(
    [string]$WorkspaceRoot = 'E:\bhzh-forth',
    [string]$PreviousFullPackage,
    [string]$WebView2Bootstrapper,
    [switch]$AllowFullOnly
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
if ($AllowFullOnly) { $args.AllowFullOnly = $true }
& (Join-Path $desktopRoot 'scripts\invoke-velopack.ps1') @args
$velopackSetup = Get-ChildItem -LiteralPath $outputRoot -Filter '*Setup*.exe' -File | Select-Object -First 1
if (-not $velopackSetup) { throw 'Velopack did not produce a Setup executable for Win10/11.' }
$installerArgs = @{ WorkspaceRoot = $WorkspaceRoot; VelopackSetup = $velopackSetup.FullName }
if ($WebView2Bootstrapper) { $installerArgs.WebView2Bootstrapper = $WebView2Bootstrapper }
& (Join-Path $desktopRoot 'apps\win10-tauri\scripts\build-installer.ps1') @installerArgs | Out-Host
& (Join-Path $desktopRoot 'scripts\write-release-checksums.ps1') -ReleaseDirectory $outputRoot
Write-Host $outputRoot
