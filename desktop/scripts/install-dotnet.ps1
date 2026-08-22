[CmdletBinding()]
param([string]$Channel = '8.0')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $repoRoot '..')).Path
$installRoot = Join-Path $workspaceRoot '.tooling\dotnet'
$dotnet = Join-Path $installRoot 'dotnet.exe'
if (Test-Path -LiteralPath $dotnet -PathType Leaf) { Write-Output $dotnet; exit 0 }
$scriptPath = Join-Path $workspaceRoot '.tooling\dotnet-install.ps1'
New-Item -ItemType Directory -Force -Path (Split-Path $scriptPath -Parent), $installRoot | Out-Null
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    & curl.exe -L --fail --retry 3 -o $scriptPath https://dot.net/v1/dotnet-install.ps1
    if ($LASTEXITCODE -ne 0) { throw 'Unable to download the official .NET install script.' }
}
& $scriptPath -Channel $Channel -InstallDir $installRoot -NoPath
if (-not (Test-Path -LiteralPath $dotnet -PathType Leaf)) { throw 'The local .NET SDK installation did not produce dotnet.exe.' }
Write-Output $dotnet
