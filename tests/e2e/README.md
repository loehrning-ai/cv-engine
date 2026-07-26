# E2E QA suite

Playwright-driven end-to-end tests for the CV editor. Uses headless
Chromium to validate every shipped feature against the live editor.

## Run

```bash
# 1. Start the editor
python3 tools/editor/server.py

# 2. In another shell, run the mutating full QA suite
pip install playwright
playwright install chromium
python3 -u tests/e2e/test_full_qa.py

# Read-only layout contracts, used by plans 018-029.
# These require an authenticated local session or ONEPAGER_DEMO_MODE=true.
ONEPAGER_E2E_BASE_URL=http://127.0.0.1:5567 python3 -m pytest tests/e2e/test_layout_contracts.py
```

## Read-only layout contracts

`test_layout_contracts.py` is intentionally separate from `test_full_qa.py`.
It blocks non-GET browser writes, uses `/app` as the editor route, keeps preview
authentication on the existing fetch-first/srcdoc path, and asserts geometry
instead of screenshots. Its scope is topbar/subpage overflow, route chrome,
canonical editor links, preview Fit bounds, text overflow, touch target size,
console errors, and accidental writes.

## What it covers

Browser test cases plus named full-QA checks across the editor, workflow,
visual, authentication, and route-layout suites: editor bootstrap, topbar elements,
appearance bar (font/density/accent), topbar stability on save,
neutral contact-link icons in the preview, Form ↔ YAML view
toggle, collapsible form sections (with localStorage persistence),
mode toggle (continuous ↔ paged), auto-render toggle (live ↔
manual), zoom controls + wrap-scroll-when-zoomed, outline
navigation, theme picker, all REST API endpoints, build endpoint
(returns binary PDF), Cmd+S save flow, form add/remove of an
experience entry, tour overlay, dark mode, section render order,
brand-glyph SVG, postMessage content-height pipeline.

A run prints `PASS` / `FAIL` per check and exits non-zero on any
failure. There must be no console errors and no uncaught page
errors during the run.
