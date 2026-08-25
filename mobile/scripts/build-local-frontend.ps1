[CmdletBinding()]
param(
    [string]$FrontendDist,
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$mobileRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repoRoot = (Resolve-Path (Join-Path $mobileRoot '..')).Path
$frontendRoot = (Resolve-Path (Join-Path $repoRoot 'frontend')).Path
$distRoot = if ($FrontendDist) {
    [System.IO.Path]::GetFullPath($FrontendDist)
} else {
    Join-Path $frontendRoot 'dist'
}
$targetRoot = [System.IO.Path]::GetFullPath((Join-Path $mobileRoot 'apps\shell-ui'))

if (-not $targetRoot.StartsWith($mobileRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw "Refusing to replace frontend outside mobile workspace: $targetRoot"
}

if (-not $SkipBuild) {
    Push-Location $frontendRoot
    try {
        & npm.cmd run build -- --mode android
        if ($LASTEXITCODE -ne 0) { throw 'Android frontend build failed.' }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path (Join-Path $distRoot 'index.html') -PathType Leaf)) {
    throw 'Android frontend build did not produce dist\index.html.'
}

if (-not (Test-Path $targetRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $targetRoot | Out-Null
}
Get-ChildItem -LiteralPath $targetRoot -Force | Remove-Item -Recurse -Force
Copy-Item -Path (Join-Path $distRoot '*') -Destination $targetRoot -Recurse -Force

Write-Host "Local Android frontend copied to $targetRoot"
