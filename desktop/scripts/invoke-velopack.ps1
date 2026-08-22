[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('win7-x64', 'win10-x64')]
    [string]$Target,
    [Parameter(Mandatory = $true)]
    [string]$PackDirectory,
    [Parameter(Mandatory = $true)]
    [string]$MainExecutable,
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [string]$PreviousFullPackage,
    [switch]$AllowFullOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$version = (Get-Content -LiteralPath (Join-Path $repoRoot 'VERSION') -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid VERSION: $version" }
$packRoot = [System.IO.Path]::GetFullPath($PackDirectory)
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
if (-not (Test-Path -LiteralPath (Join-Path $packRoot $MainExecutable) -PathType Leaf)) {
    throw "Velopack entry executable is missing: $MainExecutable"
}
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null

$isBaseline = $version -eq '0.25.15'
if ($PreviousFullPackage) {
    $previous = [System.IO.Path]::GetFullPath($PreviousFullPackage)
    if (-not (Test-Path -LiteralPath $previous -PathType Leaf)) { throw "Previous full package is missing: $previous" }
    Copy-Item -LiteralPath $previous -Destination (Join-Path $outputRoot ([System.IO.Path]::GetFileName($previous))) -Force
} elseif (-not $isBaseline -and -not $AllowFullOnly) {
    throw "Version $version requires the previous full package so Velopack can create a delta."
}

$packageId = if ($Target -eq 'win7-x64') { 'com.bhzh.binhu.win7.x64' } else { 'com.bhzh.binhu.win10.x64' }
$runtime = if ($Target -eq 'win7-x64') { 'win7-x64' } else { 'win10-x64' }
$deltaMode = if ($isBaseline -or -not $PreviousFullPackage) { 'None' } else { 'BestSpeed' }
$icon = Join-Path $repoRoot 'desktop\apps\win10-tauri\src-tauri\icons\icon.ico'
$vpk = (& (Join-Path $PSScriptRoot 'install-vpk.ps1') | Select-Object -Last 1).Trim()
$localDotnet = Join-Path (Resolve-Path (Join-Path $repoRoot '..')).Path '.tooling\dotnet'
if (Test-Path -LiteralPath (Join-Path $localDotnet 'dotnet.exe') -PathType Leaf) {
    $env:DOTNET_ROOT = $localDotnet
    $env:PATH = "$localDotnet;$env:PATH"
}

$arguments = @(
    'pack', '--packId', $packageId,
    '--packVersion', $version,
    '--packDir', $packRoot,
    '--mainExe', $MainExecutable,
    '--packAuthors', '滨湖新城派出所',
    '--packTitle', '滨湖智慧平台',
    '--icon', $icon,
    '--outputDir', $outputRoot,
    '--channel', 'stable',
    '--runtime', $runtime,
    '--delta', $deltaMode,
    '--noPortable'
)
if ($Target -eq 'win7-x64') { $arguments += '--skipVeloAppCheck' }
& $vpk @arguments
if ($LASTEXITCODE -ne 0) { throw "Velopack packaging failed for $Target with exit code $LASTEXITCODE." }

$setup = Get-ChildItem -LiteralPath $outputRoot -Filter '*Setup.exe' -File | Select-Object -First 1
if (-not $setup) { throw "Velopack Setup executable was not produced for $Target." }
$setupName = if ($Target -eq 'win7-x64') {
    "Binhu-Win7-x64-Velopack-Setup-$version.exe"
} else {
    "Binhu-Win10-x64-Setup-$version.exe"
}
if ($setup.Name -ne $setupName) {
    Rename-Item -LiteralPath $setup.FullName -NewName $setupName
}

$feed = Join-Path $outputRoot 'releases.stable.json'
if (-not (Test-Path -LiteralPath $feed -PathType Leaf)) { throw "Velopack feed was not produced: $feed" }
$full = Get-ChildItem -LiteralPath $outputRoot -Filter '*-full.nupkg' | Where-Object { $_.Name -match [regex]::Escape($version) }
if (-not $full) { throw "Velopack full package for $version was not produced." }
if (-not $isBaseline -and $PreviousFullPackage) {
    $delta = Get-ChildItem -LiteralPath $outputRoot -Filter '*-delta.nupkg' | Where-Object { $_.Name -match [regex]::Escape($version) }
    if (-not $delta) { throw "Velopack delta package for $version was not produced." }
} elseif ($isBaseline) {
    $unexpectedDelta = Get-ChildItem -LiteralPath $outputRoot -Filter '*-delta.nupkg'
    if ($unexpectedDelta) { throw 'The 0.25.15 baseline release must not contain delta packages.' }
}
Remove-Item -LiteralPath (Join-Path $outputRoot 'assets.stable.json') -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $outputRoot 'RELEASES-stable') -Force -ErrorAction SilentlyContinue

Write-Host "Velopack release: $outputRoot"
