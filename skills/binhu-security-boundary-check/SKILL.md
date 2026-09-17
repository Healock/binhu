---
name: binhu-security-boundary-check
description: Audit Binhu changes for sensitive data leakage, forbidden external writes, environment confusion, and local-data-source violations.
---

# Binhu security boundary check

Use for a security review or before a PR touching data, integrations, clients, deployment, or tests. Read `AGENTS.md`, `docs/known-risks.md`, and relevant architecture/operations sections.

## Audit

Inspect the diff, staged files, tests, logs, fixtures, and generated artifacts for real IDs, phone numbers, addresses, passwords, tokens, cookies, private keys, photos, response bodies, or user attachments. Check that external identifiers are not local primary keys and that sensitive response text is not logged or audited.

Confirm current production rules: local MySQL is the business source; Tencent business reads/writes/OAuth/sync are not reintroduced; external residence/QMF systems are read-only unless a separately governed workflow says otherwise; shadow routes and accounts are not confused with Production, Staging, or Development. Check staging/production isolation and client API routing.

Classify findings as blocker, needs-review, or clear. Do not remove evidence or rewrite user data to make a scan pass.
