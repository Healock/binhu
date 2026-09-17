---
name: binhu-submit-pr
description: Create a Binhu pull request with the repository template, local validation, evidence, and explicit merge/deploy boundaries.
---

# Binhu submit PR

Use when the user asks to submit, open, or prepare a pull request for this repository. Do not use this skill to merge, deploy, tag, or alter production unless separately authorized.

## Required workflow

1. Read `AGENTS.md`, `.github/pull_request_template.md`, and relevant `docs/development-workflow.md` sections. Inspect `git status`, branch, remote, and the diff before staging anything.
2. Confirm the branch is based on an appropriate current `origin/main` or record the exact baseline. Protect unrelated dirty and untracked files; never use `reset --hard`, `checkout --`, or force-push to solve ambiguity.
3. Review changed paths for credentials, tokens, cookies, private keys, real IDs/phones/addresses, user attachments, generated secrets, and unrelated work. Run relevant tests, `git diff --check`, and build checks.
4. Fill every heading in the current PR template, including the ten required headings and applicable local-data-source sections. Use facts and clearly label unverified MySQL, external-system, device, CI, merge, and deployment states.
5. Before creating the PR, validate the body with `python desktop/scripts/validate_pr_body.py <event-json>` (use a temporary event file outside the repository if needed). Do not create a knowingly invalid PR.
6. Stage only intended files, commit with a descriptive message, push the branch, and create the PR with base `main`. Re-read PR base, head, commit, checks, mergeability, and state afterward.

## Stop conditions and report

Stop for missing authorization, an unsafe target, unresolved unrelated changes, invalid template content, secrets, or failed checks that may indicate a code problem. A pre-existing build failure may be reported only with its exact command and cause; do not call it a pass. Report commit, branch, PR URL/number, CI results, merge state, deployment state, and remaining acceptance gaps separately.
