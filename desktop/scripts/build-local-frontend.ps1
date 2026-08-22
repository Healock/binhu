[CmdletBinding()]
param(
    [string]$FrontendDist,
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$desktopRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repoRoot = (Resolve-Path (Join-Path $desktopRoot '..')).Path
$frontendRoot = (Resolve-Path (Join-Path $repoRoot 'frontend')).Path
$distRoot = if ($FrontendDist) {
    [System.IO.Path]::GetFullPath($FrontendDist)
} else {
    Join-Path $frontendRoot 'dist'
}
$targetRoot = [System.IO.Path]::GetFullPath((Join-Path $desktopRoot 'apps\shell-ui'))

if (-not $targetRoot.StartsWith($desktopRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw "Refusing to replace frontend outside desktop workspace: $targetRoot"
}

if (-not $SkipBuild) {
    Push-Location $frontendRoot
    try {
        & npm.cmd run build -- --mode desktop
        if ($LASTEXITCODE -ne 0) { throw 'Desktop frontend build failed.' }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path (Join-Path $distRoot 'index.html') -PathType Leaf)) {
    throw 'Desktop frontend build did not produce dist\index.html.'
}

if (-not (Test-Path $targetRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $targetRoot | Out-Null
}
Get-ChildItem -LiteralPath $targetRoot -Force | Remove-Item -Recurse -Force
Copy-Item -Path (Join-Path $distRoot '*') -Destination $targetRoot -Recurse -Force

Write-Host "Local frontend copied to $targetRoot"
