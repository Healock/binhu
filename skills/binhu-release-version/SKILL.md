---
name: binhu-release-version
description: Prepare a Binhu SemVer release and release evidence without confusing PR, merge, deployment, client release, or acceptance states.
---

# Binhu release version

Use when preparing a release, version bump, release notes, or tag. Read `AGENTS.md`, `docs/operations.md`, `docs/development-workflow.md`, and the relevant release workflow.

Read the root `VERSION`, classify the change as patch/minor/major under SemVer, and check PR/CI/Dev/Staging/Production acceptance evidence. Update version and release notes only within authorized scope. Confirm desktop/mobile version synchronization when applicable.

Create a `vX.Y.Z` tag only after the requested acceptance and deployment gates are complete. Never imply that a PR or merge is a release. Report version, exact commit, tag, CI, environment deployment, client release, and business acceptance independently; stop when any required gate is missing.
