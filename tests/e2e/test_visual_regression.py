"""Plan 108 stage 6 — visual-regression baselines for the app chrome.

Absorbs TODOS.md P2 "visual-regression tests for the 5 CSS fixes": the
chrome surfaces most prone to silent pixel drift (editor topbar EN/DE x
light/dark, subpage chrome, the command palette, and the dark-mode card
contrast surface) are screenshotted under deterministic conditions
(reduced motion, tour pre-dismissed, fixed viewport/DPR) and compared
against checked-in baselines.

Baseline lifecycle:
- The unsuffixed ``*.png`` files are reviewed macOS renders. CI and other
  Linux runs use the reviewed ``*.linux.png`` files. Unsupported platforms
  fail instead of silently comparing against a different renderer.
- First run on a supported platform (no matching baseline file) writes that
  platform's baseline and SKIPS with a note — review the image, then commit it.
- Later runs fail when more than TOLERANCE of pixels differ; the diff
  artifacts land next to the baseline as *.actual.png for inspection.
- Intentional restyles: delete the stale baseline for each supported platform,
  re-run on that platform, review, and commit the new images.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

from .layout_helpers import (
    BASE_URL,
    Viewport,
    add_top_document_init_script,
    auth_storage_state,
    goto_route,
    install_layout_vendor_routes,
    require_live_server,
    wait_for_editor_shell_ready,
)

BASELINE_DIR = Path(__file__).parent / "baselines"
TOLERANCE = 0.015  # fraction of pixels allowed to differ (AA jitter head-room)
PER_CHANNEL_SLACK = 12  # ignore sub-threshold antialiasing deltas

SURFACES = [
    # (name, route, lang, theme, clip)
    ("editor-topbar-light-en", "/app", "en", "light", {"x": 0, "y": 0, "width": 1440, "height": 48}),
    ("editor-topbar-dark-de", "/app", "de", "dark", {"x": 0, "y": 0, "width": 1440, "height": 48}),
    ("jobs-chrome-light-de", "/jobs", "de", "light", {"x": 0, "y": 0, "width": 1440, "height": 48}),
    ("editor-dark-cards", "/app", "en", "dark", None),
    ("palette-open-light-en", "/app", "en", "light", None),
]

# Full-page surfaces hide user data before the shot: baselines are committed
# to the repo and must never carry CV content (the plan-108 fixture-only
# rule). Chrome, cards, labels and the palette stay visible — field VALUES
# and the rendered preview do not.
REDACT_CSS = """
#preview, #preview-pane iframe, input, textarea, select,
.cv-row .cv-name, #cv-switcher-label { visibility: hidden !important; }
"""


def _baseline_path(name: str) -> Path:
    if sys.platform.startswith("linux"):
        return BASELINE_DIR / f"{name}.linux.png"
    if sys.platform == "darwin":
        return BASELINE_DIR / f"{name}.png"
    pytest.fail(f"no reviewed visual-regression baseline for platform {sys.platform!r}")


@pytest.fixture(scope="module")
def browser():
    require_live_server()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


_FIXTURE_CV_ID: str | None = None
_FIXTURE_YAML = (Path(__file__).resolve().parents[2] / "content" / "cv.example.yaml").read_text(
    encoding="utf-8"
)


def _fixture_cv_id(browser) -> str:
    """Seed ONE deterministic CV (the committed example YAML) for visual runs.

    The demo store is a shared in-memory, server-side store that earlier
    tests keep appending to (raster fixtures, workflow docs). "Most recently
    updated" — and with it which CV the editor loads by default, the sidebar
    library rows, and the palette's Recent-documents group — therefore
    depends on suite order. Seeding our own doc and pinning the list
    endpoints to it (see _shoot) removes that dependence entirely.
    """

    global _FIXTURE_CV_ID
    if _FIXTURE_CV_ID:
        return _FIXTURE_CV_ID
    context = browser.new_context(
        device_scale_factor=1,
        storage_state=auth_storage_state(browser),
    )
    page = context.new_page()
    install_layout_vendor_routes(page)
    try:
        goto_route(page, "/app", Viewport(1440, 900))
        token = page.evaluate(
            """
            () => window.OnePagerAuth &&
              window.OnePagerAuth.getSession &&
              window.OnePagerAuth.getSession() &&
              window.OnePagerAuth.getSession().access_token
            """
        )
        assert token, "visual fixture seeding needs a bearer token"
        response = context.request.post(
            f"{BASE_URL}/api/cv",
            data=json.dumps({"content": _FIXTURE_YAML}),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        assert response.status == 200, response.text()
        doc_id = response.json().get("id")
        assert doc_id
        _FIXTURE_CV_ID = str(doc_id)
        return _FIXTURE_CV_ID
    finally:
        context.close()


def _pin_demo_store(page, cv_id: str) -> None:
    """Make every document-list-driven surface deterministic for the shot.

    - /api/cvs      → exactly our fixture row, so resolveActiveId() loads the
                      fixture CV (and its no-job-link row keeps the ATS badge
                      in the synchronous ``empty`` state — no scoring fetch).
    - /api/documents → empty: the sidebar renders its deterministic
                      "No saved documents." placeholder and the ⌘K palette
                      never shows a Recent-documents group, so leftover
                      demo-store rows cannot change the pixels. (It also keeps
                      document names out of the committed baselines — see the
                      fixture-only rule above.)
    - /api/jobs     → empty: the sidebar's Job-outputs group stays absent.

    The /api/cv document GET itself stays REAL: the form, preview and chrome
    render from genuine server data — only the ambient store state is pinned.
    """

    cvs_body = json.dumps({"cvs": [{"id": cv_id, "name": "Visual Baseline Fixture"}]})
    page.route("**/api/cvs", lambda route: route.fulfill(
        status=200, content_type="application/json", body=cvs_body,
    ))
    page.route("**/api/documents", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"documents": []}',
    ))
    page.route("**/api/jobs", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"jobs": []}',
    ))


def _settle(page, route: str) -> None:
    """Condition-based replacement for the old fixed 900/700ms sleeps."""
    if route == "/app":
        # Form rebuilt, ATS badge terminal, fonts loaded, preview paged.js
        # cycle done (spinner hidden, dim gone, page-count pill settled).
        wait_for_editor_shell_ready(page)
        # The (stubbed-empty) sidebar library has rendered its placeholder.
        page.wait_for_selector(
            ".sidebar-library-empty, .sidebar-library-row, .sidebar-library-error",
            state="visible",
        )
    elif route == "/jobs":
        page.wait_for_selector(".jb-search-sidebar, .jb-grid", state="visible")
    page.evaluate("() => (document.fonts && document.fonts.ready) || true")


def _shoot(browser, route: str, lang: str, theme: str, clip, open_palette: bool) -> bytes:
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        device_scale_factor=1,
        reduced_motion="reduce",
        storage_state=auth_storage_state(browser),
        # Test-only: lets the redaction <style> bypass the app CSP.
        bypass_csp=True,
    )
    page = context.new_page()
    install_layout_vendor_routes(page)
    _pin_demo_store(page, _fixture_cv_id(browser))
    add_top_document_init_script(
        page,
        f"localStorage.setItem('onepager.lang', '{lang}');"
        f"localStorage.setItem('cv.editor.theme', '{theme}');"
        "localStorage.setItem('cv.editor.tourCompleted', 'true');"
    )
    try:
        # goto_route performs the demo login click — the auth session lives
        # in sessionStorage, which Playwright storage_state does NOT carry.
        goto_route(page, route, Viewport(1440, 900))
        page.wait_for_function(f"() => document.documentElement.lang === '{lang}'")
        _settle(page, route)
        if clip is None:
            page.add_style_tag(content=REDACT_CSS)
            # Wait for the redaction to actually apply instead of a fixed 150ms.
            page.wait_for_function(
                "() => getComputedStyle(document.querySelector('#preview')).visibility === 'hidden'"
            )
        if open_palette:
            page.keyboard.press("Meta+k")
            page.wait_for_selector("#cmdk:not([hidden])")
            # List is built synchronously on open; the Recent-documents fetch
            # is stubbed empty, so the rows below are the final state.
            page.wait_for_selector(".cmdk-item", state="visible")
        # Two frames: let the final layout/paint land before reading pixels.
        page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
        return page.screenshot(clip=clip) if clip else page.screenshot()
    finally:
        context.close()


def _diff_ratio(a: Image.Image, b: Image.Image) -> float:
    if a.size != b.size:
        return 1.0
    arr_a = np.asarray(a.convert("RGB"), dtype=np.int16)
    arr_b = np.asarray(b.convert("RGB"), dtype=np.int16)
    changed = (np.abs(arr_a - arr_b) > PER_CHANNEL_SLACK).any(axis=2)
    return float(changed.mean())


@pytest.mark.parametrize(
    ("name", "route", "lang", "theme", "clip"),
    SURFACES,
    ids=[s[0] for s in SURFACES],
)
def test_chrome_surface_matches_baseline(browser, name, route, lang, theme, clip):
    BASELINE_DIR.mkdir(exist_ok=True)
    baseline_path = _baseline_path(name)
    shot = _shoot(browser, route, lang, theme, clip, open_palette=name.startswith("palette"))

    if not baseline_path.exists():
        baseline_path.write_bytes(shot)
        pytest.skip(f"baseline created: {baseline_path} — review and commit it")

    actual_path = BASELINE_DIR / f"{name}.actual.png"
    actual_path.write_bytes(shot)
    import io

    ratio = _diff_ratio(Image.open(baseline_path), Image.open(io.BytesIO(shot)))
    if ratio <= TOLERANCE:
        actual_path.unlink(missing_ok=True)
    assert ratio <= TOLERANCE, (
        f"{name}: {ratio:.2%} of pixels differ from the baseline "
        f"(tolerance {TOLERANCE:.2%}). If this restyle is intentional, delete "
        f"{baseline_path.name}, re-run to regenerate, review, and commit. "
        f"Actual render kept at {actual_path}"
    )
