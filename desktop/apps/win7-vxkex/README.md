# Windows 7 VxKex POC

This POC runs the shared local frontend with the official Electron 36 x64
runtime (Chromium 136) and uses VxKex as the Windows 7 compatibility layer.

The generated test kit contains:

- `BinhuWin7.exe`: renamed Electron runtime entry point;
- `resources/app`: the local Binhu desktop application;
- `prerequisites/VxKex-Setup.exe`: the reviewed VxKex installer input;
- `Install-VxKex.cmd`: installs and enables VxKex for `BinhuWin7.exe`;
- `Start-Binhu.cmd`: starts the client through `VxKexLdr.exe` after VxKex has
  been configured;
- `runtime-manifest.json`: source versions and SHA-256 values.

VxKex is a system compatibility component and requires administrator rights.
The POC does not install it silently when the application starts. Run
`Install-VxKex.cmd` once on a Windows 7 SP1 x64 test machine, review the UAC
prompt, and then use `Start-Binhu.cmd`.

The generated configuration command uses numeric boolean values for
compatibility with older VxKex installations during an upgrade.

Electron 36 declares Windows 10.0 in its PE operating system and subsystem
fields. Launching `BinhuWin7.exe` directly on Windows 7 therefore fails before
the per-application compatibility DLL can be injected. `Start-Binhu.cmd` uses
the VxKex loader so the process creation version check is patched first.

Recommended Windows 7 prerequisites from the VxKex project are KB2533623 and
KB2670838.

## Single-file installer

Run `scripts\build-installer.ps1` to create the single EXE installer. It embeds
the local frontend, Electron runtime, and the reviewed VxKex installer. The
installer requires administrator rights, installs to
`%ProgramFiles%\Bhzh\BinhuWin7`, configures VxKex, and creates shortcuts that
launch the application through `VxKexLdr.exe` without a command window.

Uninstalling Binhu removes its per-application VxKex configuration but leaves
the system-wide VxKex installation in place because other applications may be
using it.
