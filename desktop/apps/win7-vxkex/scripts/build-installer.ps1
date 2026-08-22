[CmdletBinding()]
param(
    [string]$WorkspaceRoot = 'E:\bhzh-forth',
    [Parameter(Mandatory = $true)]
    [string]$VelopackSetup,
    [string]$IsccPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

. (Join-Path $PSScriptRoot '..\..\..\scripts\workspace-layout.ps1')
$layout = Resolve-BinhuWorkspaceLayout -Root $WorkspaceRoot
$VelopackSetup = [System.IO.Path]::GetFullPath($VelopackSetup)
$artifactRoot = Join-Path $layout.DataRoot 'artifacts\updates\win7-x64'
$vxkexInstaller = Join-Path $layout.DataRoot 'release\KexSetup_Release_1_2_1_2229.exe'
$setupIcon = Join-Path $layout.RepoRoot 'desktop\apps\win10-tauri\src-tauri\icons\icon.ico'
$issPath = Join-Path $layout.RepoRoot 'desktop\apps\win7-vxkex\installer\BinhuWin7VxKex.iss'

foreach ($requiredFile in @($VelopackSetup, $vxkexInstaller, $setupIcon, $issPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) { throw "Missing installer input: $requiredFile" }
}
if (-not $IsccPath) {
    $knownPaths = @(
        (Join-Path $layout.DataRoot '.tooling\InnoSetup6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    )
    $isccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($isccCommand) { $knownPaths = @($isccCommand.Source) + $knownPaths }
    $IsccPath = $knownPaths | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
}
if (-not $IsccPath) { throw 'Inno Setup compiler ISCC.exe was not found.' }

$appVersion = (Get-Content -LiteralPath (Join-Path $layout.RepoRoot 'VERSION') -Raw).Trim()
$versionParts = $appVersion.Split('.')
if ($versionParts.Count -ne 3) { throw "Source VERSION is not a three-part version: $appVersion" }
$numericVersion = "$($versionParts[0]).$($versionParts[1]).$($versionParts[2]).0"
New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null

& $IsccPath "/DAppVersion=$appVersion" "/DNumericVersion=$numericVersion" `
    "/DVelopackSetup=$VelopackSetup" "/DVxKexInstaller=$vxkexInstaller" `
    "/DSetupIcon=$setupIcon" "/DOutputDir=$artifactRoot" $issPath
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed with exit code $LASTEXITCODE." }

$installerPath = Join-Path $artifactRoot "Binhu-Win7-x64-Setup-$appVersion.exe"
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) { throw "Installer was not produced: $installerPath" }
$hash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$installerPath.sha256" -Value "$hash  $([IO.Path]::GetFileName($installerPath))" -Encoding ASCII
Write-Host $installerPath
