---
name: binhu-external-readonly-integration
description: Add or review Binhu integrations with external read-only systems using fixed contracts, safe retries, redaction, and bounded sessions.
---

# Binhu external read-only integration

Use for residence, QMF, or other external lookup work that must not write back. Read `AGENTS.md`, `docs/architecture.md`, `docs/known-risks.md`, and the system-specific contract before coding.

Define the exact allowed hosts/paths, HTTP methods, request fields, response whitelist, authentication/session lifetime, timeout, and retry policy. Separate transport failures, HTTP errors, structural errors, and business outcomes. Never add arbitrary proxying, write endpoints, automatic write retries, external IDs as local keys, or response-body/photograph/token logging.

Use mocks or synthetic fixtures for tests. Verify CORS/certificate/device behavior only in an authorized environment and report it separately from local tests. Stop when the upstream contract or write/read boundary is ambiguous.
