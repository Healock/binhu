[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ReleaseDirectory)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$root = [IO.Path]::GetFullPath($ReleaseDirectory)
$checksumPath = Join-Path $root 'checksums.sha256'
Remove-Item -LiteralPath $checksumPath -Force -ErrorAction SilentlyContinue
$lines = Get-ChildItem -LiteralPath $root -File | Sort-Object Name | ForEach-Object {
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($_.Name)"
}
Set-Content -LiteralPath $checksumPath -Value $lines -Encoding ASCII
Write-Host $checksumPath
