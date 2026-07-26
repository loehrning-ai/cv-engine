from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from .layout_helpers import BASE_URL, require_live_server


def _json(route, payload: dict, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))


def _install_bootstrap_routes(page) -> None:
    page.route(
        "**/auth/config",
        lambda route: _json(
            route,
            {
                "supabase_url": "https://bootstrap-test.supabase.co",
                "supabase_publishable_key": "bootstrap-test-key",
                "demo_mode": True,
            },
        ),
    )
    page.route(
        "**/auth/demo-login",
        lambda route: _json(
            route,
            {
                "access_token": "bootstrap-access-token",
                "refresh_token": "bootstrap-refresh-token",
                "expires_at": int(time.time()) + 3600,
                "local_demo": True,
            },
        ),
    )


def test_real_supabase_vendor_and_demo_session_are_tab_scoped():
    require_live_server()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(device_scale_factor=1)
        page = context.new_page()
        _install_bootstrap_routes(page)
        try:
            with page.expect_response(lambda res: "/static/vendor/supabase-js-2.110.0.js" in res.url) as vendor_info:
                page.goto(f"{BASE_URL}/app", wait_until="domcontentloaded")
            assert vendor_info.value.status == 200
            page.wait_for_function(
                "() => !!(window.supabase && typeof window.supabase.createClient === 'function')",
                timeout=10000,
            )
            expect(page.locator("#auth-gate")).to_be_visible(timeout=10000)
            page.locator("#auth-demo-btn").click()
            page.wait_for_function(
                """
                () => !!(
                  window.OnePagerAuth &&
                  window.OnePagerAuth.getSession &&
                  window.OnePagerAuth.getSession() &&
                  window.sessionStorage.getItem("onepager.localDemoSession")
                )
                """,
                timeout=15000,
            )
            expect(page.locator("#auth-gate")).to_be_hidden(timeout=10000)
            assert page.evaluate("() => localStorage.getItem('onepager.localDemoSession')") is None

            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(
                "() => !!(window.OnePagerAuth && window.OnePagerAuth.getSession && window.OnePagerAuth.getSession())",
                timeout=15000,
            )
            expect(page.locator("#auth-gate")).to_be_hidden(timeout=10000)

            new_tab = context.new_page()
            _install_bootstrap_routes(new_tab)
            new_tab.goto(f"{BASE_URL}/app", wait_until="domcontentloaded")
            new_tab.wait_for_function(
                "() => !!(window.supabase && typeof window.supabase.createClient === 'function')",
                timeout=10000,
            )
            expect(new_tab.locator("#auth-gate")).to_be_visible(timeout=10000)
            assert new_tab.evaluate("() => localStorage.getItem('onepager.localDemoSession')") is None
        finally:
            context.close()
            browser.close()


def test_opaque_preview_executes_local_runtime_and_pagedjs():
    """Chromium proof for the production sandbox/CSP boundary.

    The frame must remain opaque-origin, load both root-relative local
    scripts under the inherited ``script-src 'self'`` policy, run Paged.js,
    and deliver a source-bound ``paged-rendered`` message with origin null.
    """
    require_live_server()
    import render_preview

    repo = Path(__file__).resolve().parents[2]
    preview_html = render_preview.render_cv_html(
        repo / "content" / "cv.example.yaml",
        theme="light",
        mode="paged",
        density="normal",
        embedded=True,
    )
    preview_html = preview_html.replace(
        "<head>",
        f'<head><base href="{BASE_URL}/">',
        1,
    )
    with sync_playwright() as pw:
        # A null-origin sandbox loading a loopback target triggers Chromium's
        # local-network permission gate before CSP is evaluated. Production is
        # public-origin → public-origin, so disable only that localhost-only
        # gate; CSP, CORS, sandboxing, and SOP remain enabled and asserted.
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-features="
                "LocalNetworkAccessChecks,"
                "PrivateNetworkAccessChecks,"
                "PrivateNetworkAccessRespectPreflightResults",
            ],
        )
        context = browser.new_context(device_scale_factor=1)
        app_response = context.request.get(f"{BASE_URL}/app")
        assert app_response.status == 200
        app_csp = app_response.headers.get("content-security-policy", "")
        assert "script-src 'self'" in app_csp
        assert "'unsafe-inline'" not in next(
            directive
            for directive in app_csp.split(";")
            if directive.strip().startswith("script-src")
        )
        page = context.new_page()
        static_responses: dict[str, int] = {}
        static_failures: list[str] = []
        console_errors: list[str] = []
        page_errors: list[str] = []

        def record_response(response) -> None:
            for path in (
                "/static/preview-runtime.js",
                "/static/vendor/pagedjs-0.4.3/paged.polyfill.js",
            ):
                if response.url.endswith(path):
                    static_responses[path] = response.status

        def record_failure(request) -> None:
            if "/static/preview-runtime.js" in request.url or "/static/vendor/pagedjs-0.4.3/" in request.url:
                static_failures.append(request.url)

        page.on("response", record_response)
        page.on("requestfailed", record_failure)
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.add_init_script(
            """
            (() => {
              if (window.top !== window) return;
              window.__opaquePreviewMessages = [];
              window.addEventListener("message", (event) => {
                const data = event.data;
                if (!data || !["paged-rendered", "paged-error", "cv-content-rendered"].includes(data.type)) return;
                const frame = document.querySelector("#preview");
                window.__opaquePreviewMessages.push({
                  type: data.type,
                  origin: event.origin,
                  sourceMatches: !!frame && event.source === frame.contentWindow,
                  pages: Number.isFinite(data.pages) ? data.pages : null,
                  sectionCount: Array.isArray(data.sections) ? data.sections.length : -1,
                });
              });
            })();
            """
        )
        page.route(
            "**/__opaque_preview_probe__",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                headers={"Content-Security-Policy": app_csp},
                body=(
                    '<!doctype html><html><body>'
                    '<iframe id="preview" sandbox="allow-scripts" '
                    'referrerpolicy="no-referrer"></iframe>'
                    "</body></html>"
                ),
            ),
        )
        try:
            page.goto(f"{BASE_URL}/__opaque_preview_probe__", wait_until="domcontentloaded")
            page.locator("#preview").evaluate("(frame, html) => { frame.srcdoc = html; }", preview_html)
            page.wait_for_function(
                """
                () => {
                  const messages = window.__opaquePreviewMessages || [];
                  return messages.some((message) =>
                    message.type === "paged-rendered" &&
                    message.pages >= 1 &&
                    message.sectionCount > 0
                  );
                }
                """,
                timeout=30000,
            )
            evidence = page.evaluate(
                """
                () => {
                    const frame = document.querySelector("#preview");
                    return {
                      sandbox: frame && frame.getAttribute("sandbox"),
                      parentCanReadDocument: !!(frame && frame.contentDocument),
                      messages: window.__opaquePreviewMessages || [],
                  };
                }
                """
            )
            rendered = [m for m in evidence["messages"] if m["type"] == "paged-rendered"]
            assert evidence["sandbox"] == "allow-scripts"
            assert evidence["parentCanReadDocument"] is False
            assert rendered, evidence
            assert all(m["origin"] == "null" for m in rendered), rendered
            assert all(m["sourceMatches"] is True for m in rendered), rendered
            assert all((m["pages"] or 0) >= 1 for m in rendered), rendered
            assert all(m["sectionCount"] > 0 for m in rendered), rendered

            # The same server HTML is opened as a blob by the real "open
            # preview" action. Its browser-origin <base> plus the compatibility
            # adapter must keep Paged.js and root-relative assets functional.
            blob_url = page.evaluate(
                """
                (html) => URL.createObjectURL(
                  new Blob([html], { type: "text/html" })
                )
                """,
                preview_html,
            )
            page.goto(blob_url, wait_until="load")
            page.wait_for_function(
                """
                () => document.documentElement.classList.contains("pagedjs-ready") &&
                  document.querySelectorAll(".pagedjs_page").length >= 1
                """,
                timeout=30000,
            )
            blob_state = page.evaluate(
                """
                () => ({
                  base: document.baseURI,
                  pages: document.querySelectorAll(".pagedjs_page").length,
                })
                """
            )
            assert blob_state["base"] == f"{BASE_URL}/"
            assert blob_state["pages"] >= 1
            assert static_responses == {
                "/static/preview-runtime.js": 200,
                "/static/vendor/pagedjs-0.4.3/paged.polyfill.js": 200,
            }
            assert static_failures == []
            assert console_errors == [], console_errors
            assert page_errors == [], page_errors
        finally:
            context.close()
            browser.close()
