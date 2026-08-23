# Binhu Windows desktop clients

This directory contains both Windows desktop targets. They bundle the same
production React build locally; the remote platform provides only HTTPS API,
authentication, business data and update files.

## Targets

- `apps/win7-electron`: Electron `36.0.0` packaged for Windows 7 SP1 x64.
  `BinhuWin7Launcher.exe` starts the packaged runtime through VxKex.
- `apps/win10-tauri`: Tauri v2 for Windows 10/11 using the system WebView2.
- `apps/shell-ui`: generated local React assets shared by both targets.
- `config/desktop.config.json`: package IDs, API endpoint and fixed update URLs.

Both clients use a frameless `1024x640` minimum window, local `/login` and
`/offline` routes, and the same credentialed API at
`https://www.h332a0a4b.nyat.app:48726/api`. No remote frontend, `file://` URL,
iframe, browser extension or local HTTP gateway is used.

## Version and updates

The repository `VERSION` file is the only application version source. Run
`npm.cmd run version:sync` after changing it and commit all synchronized files.

Velopack package identities and feeds are fixed:

```text
com.bhzh.binhu.win7.x64   https://47.100.44.36/updates/win7-x64/
com.bhzh.binhu.win10.x64 https://47.100.44.36/updates/win10-x64/
```

Version `0.25.15` is the full-only baseline. Every later release requires the
previous full package and must produce both a new full package and a delta.
Clients check 15 seconds after startup and every 6 hours thereafter. Update
failures leave the installed version and Offline Mode usable.

The update policy is served as `policy.stable.json`. When its
`minimumVersion` is newer than the installed version, online use is blocked by
the local frontend while Offline Mode remains available.

## Local builds

Keep downloaded prerequisites under `E:\bhzh-forth\release` and build tools
under `E:\bhzh-forth\.tooling`:

```powershell
cd E:\bhzh-forth\source\desktop
npm.cmd ci
npm.cmd run validate
npm.cmd run frontend:build
npm.cmd run electron:smoke
npm.cmd run tauri:fmt
npm.cmd run tauri:check
npm.cmd run release:win7
npm.cmd run release:win10
```

The release scripts also accept the repository root as `-WorkspaceRoot`, which
is the layout used by GitHub Actions. Output is written to
`artifacts/updates/<runtime>` below the supplied root.

## Release layout

Win7 produces a Velopack Setup/full/delta feed plus an Inno first-install
bootstrapper that installs the reviewed VxKex package once. Win10/11 builds a
Tauri release executable and gives that directory to Velopack; Tauri NSIS is
not used as the final installer.

Packages are currently unsigned. Release metadata therefore records
`signed=false`; Windows may show an unknown-publisher warning until a code
signing certificate is introduced.

## Server deployment

See `server/README.md`. The repository contains a restricted SSH publishing
gateway, Nginx configuration and short-lived IP certificate automation. These
files do not deploy themselves and must be reviewed on `47.100.44.36` before
installation.

## Verified on 2026-08-22

- Local React production build passed.
- Electron smoke test passed with Electron `36.0.0`.
- Tauri format/check/release build passed.
- Win7 and Win10/11 Velopack `0.25.15` full-only packages were produced.
- Win7 Inno first-install bootstrapper was produced.

Windows 7 hardware testing, live HTTPS update-server deployment, certificate
trust and an installed-client full/delta update cycle remain release gates.
