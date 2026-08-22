$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$desktopRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $desktopRoot '..')).Path
$tauriRoot = Join-Path $desktopRoot 'apps\win10-tauri'
$env:npm_config_cache = Join-Path $workspaceRoot '.tooling\npm-cache'
$env:TEMP = Join-Path $workspaceRoot '.temp'
$env:TMP = $env:TEMP

New-Item -ItemType Directory -Force -Path $env:npm_config_cache, $env:TEMP | Out-Null
& npm.cmd --prefix $tauriRoot install
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
