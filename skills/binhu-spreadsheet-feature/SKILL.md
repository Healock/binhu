---
name: binhu-spreadsheet-feature
description: Design, implement, or review Binhu spreadsheet import/export features with explicit column, format, privacy, and fidelity boundaries.
---

# Binhu spreadsheet feature

Use for XLSX/XLS/CSV import, batch processing, preview, or export. Read `AGENTS.md`, relevant parser/export code, tests, and help docs. Use the spreadsheet skill when the task requires inspecting or producing a workbook artifact.

First establish supported formats, selected columns, header/row rules, duplicates, blanks, formulas, merged cells, styles, and whether fidelity is required. Read only the requested fields; do not log or copy full sensitive workbooks. Validate source/version/community/status rules where the business contract requires them and keep import, review, publish, and archive stages separate.

Test malformed/empty/duplicate rows and export reopening. State whether styles, widths, merges, images, formulas, and hidden sheets are preserved. Never submit user attachments or real personnel data.
