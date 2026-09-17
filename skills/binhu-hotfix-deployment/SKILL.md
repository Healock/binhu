---
name: binhu-hotfix-deployment
description: Gate and execute an authorized Binhu backend hotfix using the immutable candidate and documented rollback procedure.
---

# Binhu hotfix deployment

Use only when the user explicitly authorizes the documented production backend hotfix. Read `AGENTS.md`, `docs/hotfix-deployment.md`, `docs/operations.md`, and the workflow.

Require an already merged commit, immutable PR built artifact, manifest with exact commit/version/SHA-256 and `ready_for_hotfix=true`, supported backend-only no-migration scope, verified backup, maintenance window, and `HOTFIX_ID`. Reject frontend, database, external-write, or unclear scope.

Trigger only the approved gateway/workflow. Monitor the bounded window, then check health, Bootstrap production identity, version, logs, and rollback signals. On failure use only the documented program/config rollback; record that hotfix completion is not full Dev to Staging to Production acceptance.
