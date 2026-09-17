---
name: binhu-desktop-client-release
description: Build and validate Binhu Windows/Android desktop-client release artifacts while separating CI, packaging, installation, and device acceptance.
---

# Binhu desktop client release

Use for desktop/mobile client build, packaging, release, or acceptance. Read `AGENTS.md`, `docs/operations.md`, desktop/mobile READMEs, and `desktop-release.yml`.

Check root `VERSION` synchronization, Windows 7 Supermium-Electron compatibility, Windows 10/Tauri and Android targets, desktop bridge/download/save-as behavior, minimum window size, environment/session isolation, and release notes/checksums. Run the scoped CI/build/test scripts available locally; do not hide missing SDKs or dependencies.

Record artifact names, hashes, commit, workflow result, and platform coverage. CI/package success is not installation or real-device acceptance. Publishing or signing requires explicit release authorization.
