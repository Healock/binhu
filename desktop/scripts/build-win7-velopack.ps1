[CmdletBinding()]
param(
    [string]$WorkspaceRoot = 'E:\bhzh-forth',
    [string]$PreviousFullPackage,
    [string]$ElectronArchive,
    [string]$VxKexInstaller,
    [string]$IsccPath,
    [switch]$AllowFullOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot 'workspace-layout.ps1')
$layout = Resolve-BinhuWorkspaceLayout -Root $WorkspaceRoot
$desktopRoot = Join-Path $layout.RepoRoot 'desktop'
$prepareArgs = @{ WorkspaceRoot = $WorkspaceRoot }
if ($ElectronArchive) { $prepareArgs.ElectronArchive = $ElectronArchive }
if ($VxKexInstaller) { $prepareArgs.VxKexInstaller = $VxKexInstaller }
$packRoot = Join-Path $layout.DataRoot '.build\win7-velopack\pack'
& (Join-Path $desktopRoot 'scripts\prepare-win7-inputs.ps1') @prepareArgs | Out-Host
$outputRoot = Join-Path $layout.DataRoot 'artifacts\updates\win7-x64'
if (Test-Path -LiteralPath $outputRoot) { Remove-Item -LiteralPath $outputRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$args = @{ Target = 'win7-x64'; PackDirectory = $packRoot; MainExecutable = 'BinhuWin7Launcher.exe'; OutputDirectory = $outputRoot }
if ($PreviousFullPackage) { $args.PreviousFullPackage = $PreviousFullPackage }
if ($AllowFullOnly) { $args.AllowFullOnly = $true }
& (Join-Path $desktopRoot 'scripts\invoke-velopack.ps1') @args
$velopackSetup = Get-ChildItem -LiteralPath $outputRoot -Filter '*Setup*.exe' -File | Select-Object -First 1
if (-not $velopackSetup) { throw 'Velopack did not produce a Setup executable for Win7.' }
$innoArgs = @{ WorkspaceRoot = $WorkspaceRoot; VelopackSetup = $velopackSetup.FullName }
if ($IsccPath) { $innoArgs.IsccPath = $IsccPath }
& (Join-Path $desktopRoot 'apps\win7-vxkex\scripts\build-installer.ps1') @innoArgs | Out-Host
& (Join-Path $desktopRoot 'scripts\write-release-checksums.ps1') -ReleaseDirectory $outputRoot
Write-Host $outputRoot
