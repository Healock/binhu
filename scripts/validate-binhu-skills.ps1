param([string]$SkillsRoot = (Join-Path $PSScriptRoot '..\skills'))
$ErrorActionPreference = 'Stop'
$validator = Join-Path $env:USERPROFILE '.codex\skills\.system\skill-creator\scripts\quick_validate.py'
if (-not (Test-Path -LiteralPath $validator)) { throw "Skill validator not found: $validator" }
$skills = Get-ChildItem -LiteralPath (Resolve-Path $SkillsRoot) -Directory | Sort-Object Name
foreach ($skill in $skills) {
  if (-not (Test-Path (Join-Path $skill.FullName 'SKILL.md'))) { throw "Missing SKILL.md: $($skill.FullName)" }
  if (-not (Test-Path (Join-Path $skill.FullName 'agents\openai.yaml'))) { throw "Missing agents/openai.yaml: $($skill.FullName)" }
  python $validator $skill.FullName
}
Write-Host "Validated $($skills.Count) Binhu skills."
