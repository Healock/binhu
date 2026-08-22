[CmdletBinding()]
param(
    [string]$Version = '1.2.110-ge826545'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $repoRoot '..')).Path
$toolRoot = Join-Path $workspaceRoot ".tooling\vpk\$Version"
$vpk = Join-Path $toolRoot 'vpk.exe'
if (Test-Path -LiteralPath $vpk -PathType Leaf) {
    Write-Output $vpk
    exit 0
}

$dotnetCandidates = @(
    (Join-Path $workspaceRoot '.tooling\dotnet\dotnet.exe')
)
$dotnetCommand = Get-Command dotnet.exe -ErrorAction SilentlyContinue
if ($dotnetCommand) { $dotnetCandidates = @($dotnetCommand.Source) + $dotnetCandidates }
$dotnet = $dotnetCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $dotnet) {
    $dotnet = (& (Join-Path $PSScriptRoot 'install-dotnet.ps1') | Select-Object -Last 1).Trim()
}
New-Item -ItemType Directory -Force -Path $toolRoot | Out-Null
& $dotnet tool install --tool-path $toolRoot vpk --version $Version
if ($LASTEXITCODE -ne 0) { throw "Unable to install vpk $Version." }
if (-not (Test-Path -LiteralPath $vpk -PathType Leaf)) { throw "vpk.exe was not installed: $vpk" }
Write-Output $vpk
