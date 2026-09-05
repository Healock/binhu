param(
  [ValidateSet('prepare','config','up','verify','smoke','stop')]
  [string]$Action = 'config'
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Keep the Windows helper on the same fail-closed implementation as the
# Linux/target-host operator. It must not maintain a second .env contract.
$python = Get-Command python -ErrorAction Stop
& $python.Source (Join-Path $PSScriptRoot 'operator.py') $Action
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}
