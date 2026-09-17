---
name: binhu-frontend-responsive-check
description: Verify Binhu frontend pages across required desktop, narrow, theme, and high-density layout conditions.
---

# Binhu frontend responsive check

Use for new pages, substantial UI changes, table/panel changes, or explicit responsive acceptance. Read `AGENTS.md`, relevant frontend tests, and the page/component styles.

Inspect or exercise 1024x640 minimum support plus 1280x720, 1280x960, 1680x1050, and 1920x1080, including Windows 125% scaling where available. Check compact/standard/wide behavior, light/dark themes, sidebar states, long addresses, tables, fixed action columns, filters, empty/loading/error states, and scroll ownership. Verify semantic parent `gap` spacing rather than relying on child margins or `space-y`.

Record viewport/device method and observations. A static test pass is not visual acceptance; do not claim device validation unless actually performed.
