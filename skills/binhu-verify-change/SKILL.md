---
name: binhu-verify-change
description: Select and run Binhu verification for code, documentation, frontend, deployment, or data changes and report evidence boundaries.
---

# Binhu verify change

Use for pre-commit, pre-PR, or explicit verification requests. Read `AGENTS.md` and `docs/development-workflow.md`; inspect changed paths before choosing checks.

## Checks

- Always run `git diff --check` and inspect status.
- Backend: run targeted tests, Python compilation, and repository unittest/pytest commands appropriate to the paths.
- Frontend: run the relevant Node test files, responsive/layout checks, and the production build when dependencies permit.
- Help/docs: validate front matter, parsing, counts, links, and sensitive-content rules.
- Deploy/config: inspect YAML/Shell/PowerShell contracts and run available deployment tests without contacting production.
- Migration: do not apply anything here; route to `binhu-migration-verify`.

Record exact commands, counts, failures, and whether real MySQL, external systems, Dev, Staging, Production, or devices were actually tested. A local pass is not an environment acceptance claim.
