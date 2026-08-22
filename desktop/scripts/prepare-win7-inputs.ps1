[CmdletBinding()]
param(
    [string]$WorkspaceRoot = 'E:\bhzh-forth',
    [string]$ElectronArchive,
    [string]$VxKexInstaller
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot 'workspace-layout.ps1')
$layout = Resolve-BinhuWorkspaceLayout -Root $WorkspaceRoot
$desktopRoot = Join-Path $layout.RepoRoot 'desktop'
$releaseRoot = Join-Path $layout.DataRoot 'release'
$buildRoot = Join-Path $layout.DataRoot '.build\win7-velopack'
$packRoot = Join-Path $buildRoot 'pack'
if (-not $ElectronArchive) { $ElectronArchive = Join-Path $releaseRoot 'electron-v36.0.0-win32-x64.zip' }
if (-not $VxKexInstaller) { $VxKexInstaller = Join-Path $releaseRoot 'KexSetup_Release_1_2_1_2229.exe' }
$ElectronArchive = [IO.Path]::GetFullPath($ElectronArchive)
$VxKexInstaller = [IO.Path]::GetFullPath($VxKexInstaller)
$expectedElectronHash = '3690467f4cb67752cdad90962bb3bee252dafcbfb12834d853e36d97117cd5b2'
$expectedVxKexHash = '7db81065591ab62f2086af84ff5cbf0d021589715024ba893e22a74f6c8708cd'

foreach ($input in @(
    @{ Path = $ElectronArchive; Hash = $expectedElectronHash },
    @{ Path = $VxKexInstaller; Hash = $expectedVxKexHash }
)) {
    if (-not (Test-Path -LiteralPath $input.Path -PathType Leaf)) { throw "Missing Win7 input: $($input.Path)" }
    $actual = (Get-FileHash -LiteralPath $input.Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $input.Hash) { throw "SHA-256 mismatch for $($input.Path): $actual" }
}

if (Test-Path -LiteralPath $buildRoot) { Remove-Item -LiteralPath $buildRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $packRoot | Out-Null
Expand-Archive -LiteralPath $ElectronArchive -DestinationPath $packRoot
if (-not (Test-Path -LiteralPath (Join-Path $packRoot 'electron.exe') -PathType Leaf)) {
    throw 'Electron archive did not contain electron.exe.'
}
& (Join-Path $desktopRoot 'apps\win7-electron\scripts\assemble-app.ps1') -RuntimeRoot $packRoot
Rename-Item -LiteralPath (Join-Path $packRoot 'electron.exe') -NewName 'BinhuWin7.exe'
Copy-Item -LiteralPath (Join-Path $desktopRoot 'apps\win10-tauri\src-tauri\icons\icon.ico') `
    -Destination (Join-Path $packRoot 'BinhuWin7.ico') -Force
& (Join-Path $desktopRoot 'apps\win7-vxkex\scripts\build-launcher.ps1') -OutputDirectory $packRoot

$version = (Get-Content -LiteralPath (Join-Path $layout.RepoRoot 'VERSION') -Raw).Trim()
$manifest = [ordered]@{
    schemaVersion = 1; appVersion = $version; target = 'win7-x64'; electronVersion = '36.0.0'
    chromiumVersion = '136.0.7103.48'; electronArchiveSha256 = $expectedElectronHash
    vxkexVersion = '1.2.1.2229'; vxkexInstallerSha256 = $expectedVxKexHash; signed = $false
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $packRoot 'runtime-manifest.json') -Encoding UTF8
Write-Host $packRoot
