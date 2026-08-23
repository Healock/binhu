$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$config = Get-Content (Join-Path $root 'config\desktop.config.json') -Raw | ConvertFrom-Json

if ($config.serverUrl -notlike 'https://*') { throw 'serverUrl must be HTTPS.' }
if ($config.apiBaseUrl -notlike 'https://*/api') { throw 'apiBaseUrl must be an HTTPS /api endpoint.' }
if ($config.initialRoute -ne '/login') { throw 'Desktop initialRoute must be /login.' }
foreach ($path in @(
    'apps\shell-ui\index.html',
    'apps\win7-electron\src\main.js',
    'apps\win7-electron\src\preload.js',
    'apps\win7-electron\src\updater.js',
    'apps\win7-electron\scripts\assemble-app.ps1',
    'apps\win7-vxkex\launcher\BinhuWin7Launcher.cpp',
    'apps\win10-tauri\src-tauri\tauri.conf.json',
    'apps\win10-tauri\src-tauri\icons\icon.ico',
    'apps\win10-tauri\src-tauri\src\main.rs',
    'scripts\prepare-windows-sdk.ps1',
    'scripts\invoke-tauri.ps1',
    'scripts\install-tauri-deps.ps1',
    'packages\desktop-contract\src\index.ts'
)) {
    if (-not (Test-Path (Join-Path $root $path) -PathType Leaf)) { throw "Missing desktop file: $path" }
}

$electronMain = Get-Content (Join-Path $root 'apps\win7-electron\src\main.js') -Raw
if (-not $electronMain.StartsWith("const { VelopackApp } = require('velopack')") -or
    $electronMain.IndexOf('VelopackApp.build()') -gt $electronMain.IndexOf("require('electron')") -or
    $electronMain.IndexOf('setAutoApplyOnStartup(false)') -gt $electronMain.IndexOf("require('electron')")) {
    throw 'Electron must initialize Velopack before Electron startup.'
}
if ($electronMain -notmatch 'contextIsolation:\s*true' -or $electronMain -notmatch 'nodeIntegration:\s*false') {
    throw 'Electron shell security defaults are missing.'
}
if ($electronMain -notmatch 'frame:\s*false' -or $electronMain -notmatch 'minWidth:\s*1280' -or $electronMain -notmatch 'minHeight:\s*960') {
    throw 'Electron must be frameless with a minimum size of 1280x960.'
}
if ($electronMain -notmatch "registerSchemesAsPrivileged" -or $electronMain -notmatch "binhu://app/login") {
    throw 'Electron local assets must use the private binhu protocol.'
}
foreach ($forbidden in @('offlineButtonScript', 'loadURL(config.onlineUrl)', 'WebviewUrl::External')) {
    if ($electronMain -match [regex]::Escape($forbidden)) {
        throw "Electron must not load or modify a remote frontend: $forbidden"
    }
}
$tauriShell = Get-Content (Join-Path $root 'apps\win10-tauri\src-tauri\src\lib.rs') -Raw
foreach ($command in @('desktop_config', 'open_online', 'open_offline')) {
    if ($tauriShell -notmatch $command) { throw "Tauri shell is missing command: $command" }
}
$tauriMain = Get-Content (Join-Path $root 'apps\win10-tauri\src-tauri\src\main.rs') -Raw
if ($tauriMain.IndexOf('velopack::VelopackApp::build()') -lt 0 -or
    $tauriMain.IndexOf('velopack::VelopackApp::build()') -gt $tauriMain.IndexOf('binhu_win10_tauri_lib::run(')) {
    throw 'Tauri must initialize Velopack before starting the application.'
}
$tauriConfig = Get-Content (Join-Path $root 'apps\win10-tauri\src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json
$tauriWindow = $tauriConfig.app.windows[0]
if ($tauriWindow.decorations -ne $false) { throw 'Tauri must use the local frameless title bar.' }
if ($tauriWindow.minWidth -ne 1280 -or $tauriWindow.minHeight -ne 960) {
    throw 'Tauri minimum window size must be 1280x960.'
}
$tauriCsp = [string]$tauriConfig.app.security.csp
if ($tauriCsp -notmatch "connect-src[^;]*\bhttp://tauri\.localhost") {
    throw 'Tauri CSP must allow the bundled release-notes.json same-origin request.'
}
$repoRoot = (Resolve-Path (Join-Path $root '..')).Path
$titleBar = Get-Content (Join-Path $repoRoot 'frontend\src\components\DesktopTitleBar.tsx') -Raw
foreach ($control in @('window-minimize-button', 'window-maximize-button', 'window-close-button')) {
    if ($titleBar -notmatch $control) { throw "Desktop title bar is missing control: $control" }
}
$electronPackage = Get-Content (Join-Path $root 'package.json') -Raw | ConvertFrom-Json
if ($electronPackage.devDependencies.electron -ne '36.0.0') {
    throw 'Win7 Electron must remain pinned to 36.0.0.'
}
$win7 = $config.targets.win7
$win10 = $config.targets.'win10-plus'
if ($win7.packageId -ne 'com.bhzh.binhu.win7.x64' -or $win7.runtimeId -ne 'win7-x64' -or
    $win7.updateUrl -ne 'https://47.100.44.36/updates/win7-x64/') {
    throw 'Win7 package identity or update URL is invalid.'
}
if ($win10.packageId -ne 'com.bhzh.binhu.win10.x64' -or $win10.runtimeId -ne 'win10-x64' -or
    $win10.updateUrl -ne 'https://47.100.44.36/updates/win10-x64/') {
    throw 'Win10 package identity or update URL is invalid.'
}
$electronUpdater = Get-Content (Join-Path $root 'apps\win7-electron\src\updater.js') -Raw
if ($electronUpdater -notmatch [regex]::Escape($win7.updateUrl) -or
    $electronUpdater -notmatch 'INITIAL_CHECK_DELAY_MS\s*=\s*15_000' -or
    $electronUpdater -notmatch 'CHECK_INTERVAL_MS\s*=\s*6\s*\*\s*60\s*\*\s*60') {
    throw 'Electron update source or schedule is invalid.'
}
if ($tauriShell -notmatch [regex]::Escape($win10.updateUrl) -or
    $tauriShell -notmatch 'Duration::from_secs\(15\)' -or
    $tauriShell -notmatch 'Duration::from_secs\(6\s*\*\s*60\s*\*\s*60\)') {
    throw 'Tauri update source or schedule is invalid.'
}
$preload = Get-Content (Join-Path $root 'apps\win7-electron\src\preload.js') -Raw
$bridge = Get-Content (Join-Path $repoRoot 'frontend\src\desktop\bridge.ts') -Raw
foreach ($api in @('getUpdateStatus', 'checkForUpdates', 'downloadUpdate', 'restartAndApply', 'subscribeUpdateState')) {
    if ($preload -notmatch $api -or $bridge -notmatch $api) { throw "Desktop update API is missing: $api" }
}
$launcher = Get-Content (Join-Path $root 'apps\win7-vxkex\launcher\BinhuWin7Launcher.cpp') -Raw
if ($launcher -notmatch 'VxKexLdr\.exe' -or $launcher -notmatch 'BinhuWin7\.exe') {
    throw 'Win7 launcher must start the packaged Electron runtime through VxKex.'
}
$mandatoryGate = Get-Content (Join-Path $repoRoot 'frontend\src\components\MandatoryUpdateGate.tsx') -Raw
if ($mandatoryGate -notmatch "navigate\('/offline'\)" -or $mandatoryGate -notmatch 'status\?\.mandatory') {
    throw 'Mandatory updates must block online use while preserving Offline Mode.'
}
foreach ($forbidden in @('WebviewUrl::External', 'initialization_script', 'OFFLINE_BUTTON_SCRIPT')) {
    if ($tauriShell -match [regex]::Escape($forbidden)) {
        throw "Tauri must not load or modify a remote frontend: $forbidden"
    }
}
$shellIndex = Get-Content (Join-Path $root 'apps\shell-ui\index.html') -Raw
if ($shellIndex -notmatch '/assets/index-' -or $shellIndex -match 'online-button') {
    throw 'Shared shell must contain the built React frontend.'
}

$sourceVersionPath = Join-Path $repoRoot 'VERSION'
if (Test-Path $sourceVersionPath) {
    $sourceVersion = (Get-Content $sourceVersionPath -Raw).Trim()
    if ($config.appVersion -ne $sourceVersion) {
        throw "Desktop version $($config.appVersion) does not match source version $sourceVersion."
    }
}

Write-Host "Desktop architecture OK: $($config.appVersion)"
