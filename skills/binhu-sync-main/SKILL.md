---
name: binhu-sync-main
description: Safely synchronize a Binhu workspace or isolated worktree with the latest origin/main without overwriting dirty work.
---

# Binhu sync main

Use when updating a Binhu checkout, preparing a feature branch, or creating an isolated worktree. First identify the exact path; never assume `E:\Bhzh` is clean.

## Workflow

1. Read `AGENTS.md` and inspect `git status --short --branch`, `git worktree list`, remotes, current branch, and local/remote `main` commits.
2. If the target worktree has modifications or untracked files, preserve them and stop or choose a new worktree. Do not stash, delete, reset, checkout, rebase, or force-push without explicit authorization.
3. Fetch `origin main`. For a clean local `main`, use fast-forward-only synchronization. For feature work, create a new `codex/` branch/worktree from the resolved `origin/main`.
4. Verify path, branch, HEAD, upstream, and clean status; verify the original worktree's status is unchanged.

## Boundaries

This skill changes Git refs only when the user asked to synchronize/create the workspace. It never changes business data, servers, deployments, or production configuration. Report old/new commits and any dirty-worktree blockers.
