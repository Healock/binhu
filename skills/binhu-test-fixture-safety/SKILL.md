---
name: binhu-test-fixture-safety
description: Create or review safe synthetic Binhu test, import, and acceptance fixtures without copying real business data.
---

# Binhu test fixture safety

Use for test data, fixtures, screenshots, load inputs, or acceptance samples. Read `AGENTS.md` and relevant test/feature docs.

Use clearly fictional, traceable, cleanable names, IDs, phones, addresses, accounts, and dates. Prefer mocks/stubs for external systems. Never copy user attachments, production rows, real IDs, tokens, photos, or credentials into tests, logs, screenshots, Git, or shared environments. Mark fixtures synthetic and keep a cleanup path.

Review generated output and repository status before handoff. If real data is already present, stop, avoid echoing it, preserve evidence safely, and report remediation ownership rather than masking it.
