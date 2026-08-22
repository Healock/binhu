[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$desktopRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$runtimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$resourcesRoot = Join-Path $runtimeRoot 'resources'
$appRoot = Join-Path $resourcesRoot 'app'

if (-not (Test-Path (Join-Path $runtimeRoot 'electron.exe') -PathType Leaf)) {
    throw "Electron-compatible runtime is missing electron.exe: $runtimeRoot"
}
if (Test-Path $appRoot) {
    Remove-Item -LiteralPath $appRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $appRoot | Out-Null

foreach ($item in @('package.json', 'package-lock.json', 'config', 'packages')) {
    Copy-Item -LiteralPath (Join-Path $desktopRoot $item) -Destination $appRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $appRoot 'apps') | Out-Null
Copy-Item -LiteralPath (Join-Path $desktopRoot 'apps\shell-ui') -Destination (Join-Path $appRoot 'apps') -Recurse -Force
Copy-Item -LiteralPath (Join-Path $desktopRoot 'apps\win7-electron') -Destination (Join-Path $appRoot 'apps') -Recurse -Force

$packagedRuntime = Join-Path $appRoot 'apps\win7-electron\runtime'
if (Test-Path $packagedRuntime) {
    Remove-Item -LiteralPath $packagedRuntime -Recurse -Force
}

Push-Location $appRoot
try {
    & npm.cmd ci --omit=dev --ignore-scripts
    if ($LASTEXITCODE -ne 0) { throw 'Unable to install packaged Electron production dependencies.' }
}
finally {
    Pop-Location
}

Write-Host "Electron application assembled at $appRoot"
