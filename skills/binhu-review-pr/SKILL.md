---
name: binhu-review-pr
description: Perform an evidence-based Binhu pull-request review for correctness, security, documentation, tests, and release boundaries without modifying the PR.
---

# Binhu review PR

Use for review requests or merge-readiness checks. Read `AGENTS.md`, the PR body/template, changed files, relevant architecture/workflow/risk docs, and CI results.

Check base/head and ancestry, scope, local data-source rules, permissions, connection-pool behavior, external read-only boundaries, sensitive logging, help docs, tests, responsive layout, migrations, and release claims. Treat CI failures separately from existing infrastructure/dependency failures. Do not edit code, approve, merge, deploy, or retag unless separately asked and authorized.

Report findings with severity, file/line evidence, required action, and explicit residual risks. State whether the PR is mergeable based on evidence, not only GitHub's badge.
