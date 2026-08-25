[CmdletBinding()]
param(
    [string]$WorkspaceRoot = 'E:\bhzh-forth',
    [Parameter(Mandatory = $true)]
    [string]$VelopackSetup,
    [string]$WebView2Bootstrapper,
    [string]$IsccPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

. (Join-Path $PSScriptRoot '..\..\..\scripts\workspace-layout.ps1')
$layout = Resolve-BinhuWorkspaceLayout -Root $WorkspaceRoot
$VelopackSetup = [System.IO.Path]::GetFullPath($VelopackSetup)
if (-not $WebView2Bootstrapper) {
    $WebView2Bootstrapper = Join-Path $layout.DataRoot 'release\MicrosoftEdgeWebView2Setup.exe'
}
$WebView2Bootstrapper = [System.IO.Path]::GetFullPath($WebView2Bootstrapper)
$artifactRoot = Join-Path $layout.DataRoot 'artifacts\updates\win10-x64'
$setupIcon = Join-Path $layout.RepoRoot 'desktop\apps\win10-tauri\src-tauri\icons\icon.ico'
$issPath = Join-Path $layout.RepoRoot 'desktop\apps\win10-tauri\installer\BinhuWin10Bootstrap.iss'

foreach ($requiredFile in @($VelopackSetup, $WebView2Bootstrapper, $setupIcon, $issPath)) {
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

# The Velopack setup and the public bootstrapper intentionally share the final
# filename. Copy the inner setup out of the release directory before Inno
# writes the WebView2-aware first-install executable over that name.
$innerSetupRoot = Join-Path $layout.DataRoot '.build\win10-bootstrap'
if (Test-Path -LiteralPath $innerSetupRoot) { Remove-Item -LiteralPath $innerSetupRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $innerSetupRoot | Out-Null
$innerSetup = Join-Path $innerSetupRoot 'Binhu-Velopack-Setup.exe'
Copy-Item -LiteralPath $VelopackSetup -Destination $innerSetup -Force
if ([System.IO.Path]::GetFullPath($VelopackSetup).StartsWith([System.IO.Path]::GetFullPath($artifactRoot) + [System.IO.Path]::DirectorySeparatorChar)) {
    Remove-Item -LiteralPath $VelopackSetup -Force
}

& $IsccPath "/DAppVersion=$appVersion" "/DNumericVersion=$numericVersion" `
    "/DVelopackSetup=$innerSetup" "/DWebView2Bootstrapper=$WebView2Bootstrapper" `
    "/DSetupIcon=$setupIcon" "/DOutputDir=$artifactRoot" $issPath
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed with exit code $LASTEXITCODE." }

$installerPath = Join-Path $artifactRoot "Binhu-Win10-x64-Setup-$appVersion.exe"
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) { throw "Installer was not produced: $installerPath" }
$hash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$installerPath.sha256" -Value "$hash  $([IO.Path]::GetFileName($installerPath))" -Encoding ASCII
Write-Host $installerPath
