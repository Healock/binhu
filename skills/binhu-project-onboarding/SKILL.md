---
name: binhu-project-onboarding
description: Establish Binhu repository, environment, documentation, and workspace context before a new agent starts work.
---

# Binhu project onboarding

Use when entering the Binhu repository or when project context is uncertain. Read `AGENTS.md` first; then read only task-relevant `docs/architecture.md`, `docs/development-workflow.md`, `docs/operations.md`, and `docs/known-risks.md`. Read `AGENTS.local.md` only when the user explicitly requests server, credential, or recovery work.

Inspect path, worktrees, branch, remote, status, `VERSION`, application entrypoints, and environment boundaries. Treat Production, Staging, and Development as delivery environments; do not silently convert historical Shadow terminology into a current delivery environment. Record local limitations such as no Docker/MySQL without repeatedly probing unavailable services.

Return a concise context ledger: repository/worktree, branch/commit, dirty state, relevant docs, allowed data sources, external-system boundaries, and unverified capabilities.
