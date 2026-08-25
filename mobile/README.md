# Binhu Android client

The Android client uses Tauri 2 and packages the existing React frontend into
the APK. API requests continue to use the managed production service configured
in `frontend/.env.android`.

The arm64 client checks the Binhu update service after startup and every six
hours. It downloads a complete APK with HTTP range support, validates the file
length, SHA-256, package metadata, version and signing certificate, then opens
the Android system installer through a `FileProvider`. Android never installs
an update silently.

## Local build

```powershell
cd E:\bhzh-forth\source\mobile
npm.cmd install
npm.cmd run android:toolchain
npm.cmd run android:frontend
npm.cmd run android:init
npm.cmd run android:build:debug
```

The Android SDK and NDK can be installed under `E:\bhzh-forth\.tooling\android`
with `scripts/install-android-toolchain.ps1`. The script prints the environment
variables required by the Tauri CLI.

## Signed release build

Release builds require all four variables below and fail when any value is
missing:

```text
ANDROID_KEYSTORE_PATH
ANDROID_KEYSTORE_PASSWORD
ANDROID_KEY_ALIAS
ANDROID_KEY_PASSWORD
```

Build the repository version or a temporary candidate without editing
`VERSION`:

```powershell
.\mobile\scripts\invoke-android.ps1 -Action BuildRelease
.\mobile\scripts\invoke-android.ps1 -Action BuildRelease -VersionOverride 0.25.28
```

Use `mobile/scripts/package-android-release.py` to verify and stage the signed
APK, stable manifest, policy and checksums. The permanent signing key must stay
outside Git and must have an offline backup before production use.

## Local update protocol test

`mobile/scripts/local_update_server.py` serves a staged update directory with
the same manifest cache and byte-range behavior used by the public service. It
is intended for local protocol and device acceptance tests only. Production
clients always use the fixed HTTPS service at `47.100.44.36`.

Push notifications, camera integration and an offline database remain outside
the current Android scope.
