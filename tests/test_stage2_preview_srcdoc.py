"""Stage 2 regression — live preview renders via authenticated fetch + srcdoc.

The logged-in editor's live preview was blank because of TWO stacked causes:

  1. ``iframe.src = '/api/preview?...'`` is a browser NAVIGATION, which the
     auth.js ``window.fetch`` patch cannot intercept — so the request carried no
     ``Authorization: Bearer`` and got a 401.
  2. The ``/api/preview`` response carries ``frame-ancestors 'none'`` +
     ``X-Frame-Options: DENY``, so even a 200 could not be framed.

The fix (Stage 2): ``refreshPreview()`` loads the preview HTML with the PATCHED
``fetch`` (bearer attached) and injects it via ``iframe.srcdoc`` — no navigation
response to authenticate or to frame-block. The server security headers / CSP
stay UNCHANGED.

Because a ``srcdoc`` document INHERITS the parent /app page's strict
``script-src 'self'`` CSP (no 'unsafe-inline'), the preview's paged.js handshake
+ content-size glue must be loaded as an EXTERNAL same-origin script
(``/static/preview-runtime.js``) rather than inline — otherwise it would be
CSP-blocked inside the frame.

These tests lock in all of the above so the bug cannot silently regress.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "tools" / "editor" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
AUTH_JS = (STATIC / "auth.js").read_text(encoding="utf-8")


class TestPreviewLoadPathUsesSrcdoc:
    def test_refreshpreview_assigns_srcdoc(self):
        """The preview is injected via srcdoc, not navigated to."""
        assert "iframe.srcdoc = withTrustedPreviewBase(html)" in APP_JS

    def test_refreshpreview_fetches_preview_then_sets_srcdoc(self):
        """It fetches /api/preview (patched fetch attaches the bearer) and
        feeds the response text into srcdoc."""
        # The preview URL is fetched (not navigated) ...
        assert "await fetch(url" in APP_JS
        # ... and its text becomes srcdoc with a browser-origin base URL.
        assert "const html = await res.text()" in APP_JS
        assert "withTrustedPreviewBase(html)" in APP_JS

    def test_refreshpreview_does_not_navigate_iframe_to_preview(self):
        """The old, broken pattern (an iframe.src = url ASSIGNMENT statement) is
        gone — that navigation can't carry the bearer and is frame-blocked.

        We strip line comments first so a back-ticked mention of the old
        pattern in an explanatory comment doesn't trip the guard.
        """
        code_lines = [
            ln for ln in APP_JS.splitlines() if not ln.lstrip().startswith("//")
        ]
        code = "\n".join(code_lines)
        assert "iframe.src = url;" not in code
        assert "iframe.src=url" not in code.replace(" ", "")
        # The src attribute is cleared before srcdoc so a stale URL can't win.
        assert 'iframe.removeAttribute("src")' in APP_JS

    def test_open_preview_fetches_html_before_new_tab_navigation(self):
        """The standalone preview button must not navigate directly to
        /api/preview; browser navigations cannot carry the bearer token."""
        block = APP_JS.split('$("#preview-open-btn").addEventListener("click"', 1)[1].split(
            '// Refresh button always forces', 1
        )[0]
        assert "await fetch(previewUrl({ embedded: false })" in block
        assert "new Blob([withTrustedPreviewBase(html)], { type: \"text/html\" })" in block
        assert "URL.createObjectURL(blob)" in block
        assert "popup.location.replace(objectUrl)" in block
        assert "window.open(url" not in block

    def test_srcdoc_base_comes_from_browser_origin_not_request_host(self):
        block = APP_JS.split("function withTrustedPreviewBase", 1)[1]
        block = block.split("function setupPreviewActions", 1)[0]
        assert "window.location.origin" in block
        assert "<base href=" in block


class TestAuthFetchAttachesBearer:
    """The srcdoc fix relies on auth.js's fetch-patch to attach the token."""

    def test_auth_patches_window_fetch(self):
        assert "window.fetch = function" in AUTH_JS

    def test_auth_sets_authorization_bearer_on_api_requests(self):
        assert 'headers.set("Authorization", "Bearer "' in AUTH_JS
        # ...and only for same-origin /api/* requests (the preview URL matches).
        assert "isApiRequest" in AUTH_JS


class TestTokenHydrationRaceClosed:
    """loadCv() must await the session so the FIRST /api/* fetch is authed."""

    def test_loadcv_awaits_auth_ready(self):
        # awaitAuthReady is defined ...
        assert "async function awaitAuthReady()" in APP_JS
        # ... awaits OnePagerAuth.ready ...
        assert "await auth.ready" in APP_JS
        # ... and loadCv calls it before any fetch.
        load_cv = APP_JS.split("async function loadCv()", 1)[1]
        head = load_cv[: load_cv.index("await resolveActiveId()")]
        assert "await awaitAuthReady()" in head


class TestPreviewRuntimeIsExternalNotInline:
    """Under srcdoc the inherited CSP blocks inline <script>, so the preview
    glue must be served as an external same-origin file."""

    def _render(self, mode: str) -> str:
        import render_preview as rp  # importable via conftest sys.path

        cv = REPO / "content" / "cv.example.yaml"
        return rp.render_cv_html(
            cv, theme="light", mode=mode, density="normal"
        )

    def test_paged_references_external_runtime(self):
        html = self._render("paged")
        assert 'src="/static/preview-runtime.js"' in html
        assert 'src="/static/vendor/pagedjs-0.4.3/paged.polyfill.js"' in html
        assert "http://127.0.0.1:5567" not in html
        assert "unpkg.com" not in html
        assert "cdn.jsdelivr.net" not in html

    def test_continuous_references_external_runtime(self):
        html = self._render("continuous")
        assert 'src="/static/preview-runtime.js"' in html

    def test_no_inline_postmessage_in_preview_html(self):
        """The blocked-by-CSP inline glue must be gone from the document."""
        for mode in ("paged", "continuous"):
            html = self._render(mode)
            assert "window.parent.postMessage" not in html

    def test_runtime_loads_before_pagedjs_polyfill(self):
        """preview-runtime.js sets window.PagedConfig, which the polyfill reads
        at ready-time — so the runtime must come first in document order."""
        import render_preview as rp

        html = self._render("paged")
        assert html.index("preview-runtime.js") < html.index(rp.PAGEDJS_PATH)

    def test_runtime_uses_pagedconfig_after_hook(self):
        """The external runtime reports pages via the documented PagedConfig
        after-hook (works without inline handler-registration)."""
        runtime = (STATIC / "preview-runtime.js").read_text(encoding="utf-8")
        assert "window.PagedConfig" in runtime
        assert '"paged-rendered"' in runtime
        assert '"cv-content-rendered"' in runtime

    def test_runtime_reports_paged_error_on_polyfill_timeout(self):
        """If paged.js never runs, the iframe must not falsely claim one page."""
        runtime = (STATIC / "preview-runtime.js").read_text(encoding="utf-8")
        timeout_block = runtime.split("Safety net:", 1)[1]
        assert '"paged-error"' in timeout_block
        assert 'type: "paged-rendered"' not in timeout_block


class TestPreviewRuntimeIsServed:
    """The srcdoc-injected external script must actually be reachable (and
    auth-exempt — it carries no per-user data)."""

    def test_static_runtime_served_200(self, client):
        res = client.get("/static/preview-runtime.js")
        assert res.status_code == 200
        body = res.get_data(as_text=True)
        assert "PagedConfig" in body


class TestServerSecurityHeadersUnchanged:
    """The fix must NOT relax the clickjacking/XSS protections."""

    def test_app_csp_keeps_strict_script_src(self, client):
        res = client.get("/app")
        csp = res.headers.get("Content-Security-Policy", "")
        script_directive = next(
            (d for d in csp.split(";") if d.strip().startswith("script-src")), ""
        )
        assert "'unsafe-inline'" not in script_directive

    def test_preview_still_frame_blocked_at_header_level(self, client, fake_supabase):
        """/api/preview keeps frame-ancestors 'none' + X-Frame-Options: DENY —
        srcdoc is what makes the preview work despite these, not a relaxation."""
        res = client.get("/api/preview")
        csp = res.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in csp
        assert res.headers.get("X-Frame-Options") == "DENY"


class TestPreviewFramesRemainSandboxed:
    def test_main_preview_is_sandboxed_without_navigation_capabilities(self):
        index = (STATIC / "index.html").read_text(encoding="utf-8")
        frame = index.split('id="preview"', 1)[1].split("</iframe>", 1)[0]
        assert 'sandbox="allow-scripts"' in frame
        assert "allow-same-origin" not in frame
        assert 'referrerpolicy="no-referrer"' in frame
        assert "allow-forms" not in frame
        assert "allow-popups" not in frame
        assert "allow-top-navigation" not in frame

    def test_template_previews_use_the_same_sandbox(self):
        frame = APP_JS.split('class="template-preview-frame"', 1)[1].split(
            "</iframe>", 1
        )[0]
        assert 'sandbox="allow-scripts"' in frame
        assert "allow-same-origin" not in frame
        assert 'referrerpolicy="no-referrer"' in frame
        assert "allow-forms" not in frame
        assert "allow-popups" not in frame
        assert "allow-top-navigation" not in frame
