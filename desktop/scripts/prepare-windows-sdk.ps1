param(
    [string]$PackageVersion = '10.0.28000.2526',
    [string]$SdkVersion = '10.0.28000.0'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$desktopRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = (Resolve-Path (Join-Path $desktopRoot '..')).Path
$toolingRoot = Join-Path $workspaceRoot '.tooling\windows-sdk'
$packageRoot = Join-Path $toolingRoot 'packages'
$extractRoot = Join-Path $toolingRoot 'extracted'
$baseRoot = Join-Path $extractRoot 'base'
$x64Root = Join-Path $extractRoot 'x64'
$basePackage = Join-Path $packageRoot "Microsoft.Windows.SDK.CPP.$PackageVersion.nupkg"
$x64Package = Join-Path $packageRoot "Microsoft.Windows.SDK.CPP.x64.$PackageVersion.nupkg"
$kernelLibrary = Join-Path $x64Root 'c\um\x64\kernel32.Lib'
$resourceCompiler = Join-Path $baseRoot "c\bin\$SdkVersion\x64\rc.exe"

New-Item -ItemType Directory -Force -Path $packageRoot, $extractRoot | Out-Null
$lockPath = Join-Path $toolingRoot 'prepare.lock'
$lockStream = [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')

function Get-Package([string]$Path, [string]$Id) {
    if (Test-Path $Path -PathType Leaf) { return }
    $url = "https://www.nuget.org/api/v2/package/$Id/$PackageVersion"
    $temporary = "$Path.download"
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    Write-Host "Downloading $Id $PackageVersion into the workspace..."
    & curl.exe -L --fail --retry 3 -o $temporary $url
    if ($LASTEXITCODE -ne 0) { throw "Failed to download $Id." }
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Expand-Package([string]$Path, [string]$Destination) {
    if (Test-Path $Destination -PathType Container) { return }
    $temporary = "$Destination.extracting"
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $temporary | Out-Null
    & tar.exe -xf $Path -C $temporary
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
        throw "Failed to extract $Path."
    }
    Move-Item -LiteralPath $temporary -Destination $Destination
}

function Get-Sha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $sha256.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

try {
    Get-Package $basePackage 'Microsoft.Windows.SDK.CPP'
    Get-Package $x64Package 'Microsoft.Windows.SDK.CPP.x64'
    Expand-Package $basePackage $baseRoot
    Expand-Package $x64Package $x64Root

foreach ($required in @($kernelLibrary, $resourceCompiler)) {
    if (-not (Test-Path $required -PathType Leaf)) {
        throw "Windows SDK package is incomplete: $required"
    }
}

$manifest = [ordered]@{
    packageVersion = $PackageVersion
    sdkVersion = $SdkVersion
    basePackageSha256 = Get-Sha256 $basePackage
    x64PackageSha256 = Get-Sha256 $x64Package
    preparedAtUtc = [DateTime]::UtcNow.ToString('o')
}
    $manifest | ConvertTo-Json | Set-Content (Join-Path $toolingRoot 'manifest.json') -Encoding utf8
}
finally {
    $lockStream.Dispose()
}

Write-Host "Windows SDK ready: $SdkVersion"
