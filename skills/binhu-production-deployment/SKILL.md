---
name: binhu-production-deployment
description: Gate an explicitly authorized Binhu Production deployment and report deployment evidence without inventing approval or acceptance.
---

# Binhu production deployment

Use only after explicit authorization to deploy. Read `AGENTS.md`, `docs/operations.md`, `docs/development-workflow.md`, the release workflow, and applicable risk docs.

Before triggering, require the PR merged to `main`, CI success, exact 40-character lowercase commit that is an ancestor of `origin/main`, SemVer/version evidence, release scope, backup scope, rollback plan, migration decision, and deployment window. Confirm no conflicting maintenance or background task condition.

Use the approved GitHub `Deploy production` workflow/gateway only. Afterward verify release manifest, health, version, Bootstrap `production` identity, containers, logs, and required read-only business checks. Report code/CI/merge/deployment/business acceptance separately. Never self-approve missing gates or run destructive recovery.
