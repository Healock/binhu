---
name: binhu-help-doc-update
description: Add or update Binhu help-center documentation when a feature, page, permission, or workflow changes.
---

# Binhu help docs

Use when functionality, navigation, permissions, configuration, or user steps change. Read `AGENTS.md`, `backend/help_docs/`, and `docs/development-workflow.md`.

Place the user-facing baseline in `backend/help_docs/*.md`. Each document must have `slug`, `title`, `category`, `summary`, and `order` front matter, followed by a level-one heading. Keep instructions aligned with local data-source, autosave, permission, and environment rules. Never include real IDs, phones, passwords, tokens, cookies, addresses, personnel data, or credentials.

Run the help-document parser/tests and update count assertions only when the document set really changes. Online edits, revisions, and custom-content semantics remain server-controlled; this skill does not bypass them.
