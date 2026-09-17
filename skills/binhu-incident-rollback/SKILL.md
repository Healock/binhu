---
name: binhu-incident-rollback
description: Plan or execute an authorized Binhu rollback with ancestry, dependency, data, and post-rollback verification safeguards.
---

# Binhu incident rollback

Use for a rollback request or incident recovery. Read `AGENTS.md`, `docs/operations.md`, `docs/hotfix-deployment.md`, risk docs, and the relevant release/deployment ledger.

Identify the exact bad release, environment, commit ancestry, deployment record, changed data/schema, external effects, and rollback authorization. Prefer reversible release/config rollback; for Git history use reverse-order `git revert` with parent selection, never destructive reset. Preserve unrelated fixes and failed evidence.

After rollback verify health, version, environment identity, containers, logs, migrations/data invariants, and business symptoms. State separately whether code was reverted, deployment completed, data repaired, and service/business acceptance restored. Stop when rollback direction or data reversibility is unclear.
