param(
  [string]$SkillsRoot = (Join-Path $PSScriptRoot '..\skills'),
  [string]$CodexSkillsRoot = (Join-Path $env:USERPROFILE '.codex\skills'),
  [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$resolvedSource = (Resolve-Path -LiteralPath $SkillsRoot).Path
if (-not (Test-Path -LiteralPath $CodexSkillsRoot)) {
  if ($WhatIf) { Write-Host "Would create $CodexSkillsRoot" } else { New-Item -ItemType Directory -Path $CodexSkillsRoot -Force | Out-Null }
}
$skills = Get-ChildItem -LiteralPath $resolvedSource -Directory | Sort-Object Name
if (-not $skills) { throw "No skills found under $resolvedSource" }
foreach ($skill in $skills) {
  $destination = Join-Path $CodexSkillsRoot $skill.Name
  if (Test-Path -LiteralPath $destination) { throw "Refusing to overwrite existing skill: $destination" }
  if ($WhatIf) { Write-Host "Would link $destination -> $($skill.FullName)"; continue }
  New-Item -ItemType Junction -Path $destination -Target $skill.FullName | Out-Null
  Write-Host "Linked $($skill.Name)"
}
