---
name: binhu-readonly-production-diagnosis
description: Conduct bounded, sanitized, read-only Binhu production/server diagnostics without repairing or deploying.
---

# Binhu read-only production diagnosis

Use for status, log, disk, Nginx, container, or health investigation explicitly limited to read-only work. Read `AGENTS.md`, `docs/operations.md`, and `docs/known-risks.md`.

Prefer the registered persistent SSH stdio MCP and one active production session. Use only fixed allowed targets. Read the smallest useful time window and output size; sanitize passwords, tokens, cookies, private keys, IDs, phones, full addresses, and business正文. Inspect health/version/environment identity, containers, ports, disk, and relevant errors only.

Do not restart, deploy, import, clean, delete, modify Nginx, change credentials, elevate privileges, or run repair commands. Report commands, timestamps, sanitized facts, and follow-up owner; unchanged state is a valid result.
