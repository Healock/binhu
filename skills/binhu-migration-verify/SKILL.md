---
name: binhu-migration-verify
description: Execute or prepare Binhu data migrations with the mandatory measure, migrate --apply, verify sequence and evidence safeguards.
---

# Binhu migration verify

Use only for an explicitly authorized migration or maintenance-window task. Read `AGENTS.md`, `docs/development-workflow.md`, `docs/operations.md`, `docs/known-risks.md`, and the named migration module.

Before writing: identify exact database/domain, target environment, run ID, backup scope, maintenance window, rollback switch, and current schema. Run `measure` and preserve its output outside disposable project paths. Review conflicts and stop if target identity or backup evidence is unclear.

Apply only with explicit authorization using the documented `migrate --apply`; then run `verify`. Confirm idempotency, counts, human confirmations, aliases, task references, feedback memory, and historical evidence were preserved. Never use broad deletion or production data as a test fixture. Report all three stages and real-MySQL scope separately.
