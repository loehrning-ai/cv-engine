"""Read-only Playwright helpers for layout regression contracts.

These helpers intentionally do not reuse ``test_full_qa.py``. That script is a
mutating broad QA pass; this module is for geometry, reachability, and chrome
contracts against an already-running local editor.
"""
from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass
from typing import Iterable

import pytest
from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, expect


DEFAULT_BASE_URL = "http://127.0.0.1:5567"
BASE_URL = os.environ.get("ONEPAGER_E2E_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
_AUTH_STORAGE_STATE: dict | None = None


_SUPABASE_STUB = """
window.supabase = {
  createClient: () => ({
    auth: {
      getSession: async () => {
        const raw = window.localStorage.getItem("__layoutSupabaseSession") ||
          window.sessionStorage.getItem("onepager.localDemoSession");
        return { data: { session: raw ? JSON.parse(raw) : null }, error: null };
      },
      setSession: async (next) => {
        const session = {
          access_token: next && next.access_token,
          refresh_token: next && next.refresh_token,
        };
        window.localStorage.setItem("__layoutSupabaseSession", JSON.stringify(session));
        return { data: { session }, error: null };
      },
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }),
      signInWithOAuth: async () => ({ data: {}, error: null }),
      signInWithPassword: async () => ({ data: {}, error: null }),
      signOut: async () => {
        window.localStorage.removeItem("__layoutSupabaseSession");
        return { error: null };
      },
    },
  }),
};
"""


def install_layout_vendor_routes(page: Page) -> None:
    """Stub only Supabase; all other browser dependencies load locally."""

    page.route(
        "**/static/vendor/supabase-js-2.110.0.js",
        lambda route: route.fulfill(status=200, content_type="application/javascript", body=_SUPABASE_STUB),
    )


def add_top_document_init_script(page: Page, script: str) -> None:
    """Inject a trusted harness script only into the application document.

    Playwright init scripts execute in every attached frame. The CV preview is
    intentionally an opaque-origin ``sandbox="allow-scripts"`` frame, where
    Web Storage access raises a security error and top-page test doubles must
    not be installed.
    """

    page.add_init_script(
        f"""
        (() => {{
          if (window.top !== window) return;
          {script}
        }})();
        """
    )


@dataclass(frozen=True)
class Viewport:
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}"


CORE_VIEWPORTS = [
    Viewport(1440, 900),
    Viewport(1280, 800),
    Viewport(1100, 760),
    Viewport(930, 760),
    Viewport(920, 760),
    Viewport(915, 760),
    Viewport(900, 760),
    Viewport(881, 720),
    Viewport(880, 720),
    Viewport(820, 800),
    Viewport(800, 800),
    Viewport(761, 800),
    Viewport(760, 800),
    Viewport(600, 800),
    Viewport(430, 932),
    Viewport(390, 844),
    Viewport(320, 740),
]

E2E_VIEWPORTS = [
    Viewport(1440, 900),
    Viewport(1280, 800),
    Viewport(1100, 760),
    Viewport(930, 760),
    Viewport(920, 760),
    Viewport(915, 760),
    Viewport(900, 760),
    Viewport(880, 720),
    Viewport(820, 800),
    Viewport(390, 844),
    Viewport(320, 740),
]


def require_live_server() -> None:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/healthz", timeout=2) as res:
            if res.status >= 400:
                raise OSError(f"healthz returned {res.status}")
        with urllib.request.urlopen(f"{BASE_URL}/static/app.js", timeout=2) as res:
            if res.status >= 400:
                raise OSError(f"static app.js returned {res.status}")
        with urllib.request.urlopen(f"{BASE_URL}/static/vendor/supabase-js-2.110.0.js", timeout=2) as res:
            if res.status >= 400:
                raise OSError(f"static supabase vendor returned {res.status}")
    except OSError as exc:
        message = f"layout e2e requires a running editor at {BASE_URL}: {exc}"
        if os.environ.get("ONEPAGER_E2E_ALLOW_SKIP") == "1":
            pytest.skip(message)
        pytest.fail(message)


def install_read_only_guards(page: Page) -> None:
    install_layout_vendor_routes(page)
    page.add_init_script(
        """
        (() => {
          // Playwright injects this helper into every frame. The production
          // preview is deliberately opaque-origin and cannot access Web
          // Storage; the read-only harness belongs only in the top document.
          if (window.top !== window) return;
          const blocked = [];
          const allowed = [];
          const nativeFetch = window.fetch.bind(window);
          window.__layoutBlockedWrites = blocked;
          window.__layoutAllowedWritesSeen = allowed;
          const isAllowedWrite = (method, path, url) => {
            const rules = Array.isArray(window.__layoutAllowedWrites)
              ? window.__layoutAllowedWrites
              : [];
            return rules.some((rule) => {
              const ruleMethod = String((rule && rule.method) || "GET").toUpperCase();
              if (ruleMethod !== method) return false;
              if (rule.path && String(rule.path) === path) return true;
              if (rule.url && String(url || "").includes(String(rule.url))) return true;
              return false;
            });
          };
          window.fetch = (input, init = {}) => {
            const method = String((init && init.method) || "GET").toUpperCase();
            const url = typeof input === "string" ? input : input && input.url;
            const path = (() => {
              try { return new URL(url, window.location.origin).pathname; }
              catch (_) { return String(url || ""); }
            })();
            if (!["GET", "HEAD", "OPTIONS"].includes(method) && isAllowedWrite(method, path, url)) {
              allowed.push({ method, url: String(url || ""), path });
              return nativeFetch(input, init);
            }
            if (!["GET", "HEAD", "OPTIONS"].includes(method) && path !== "/auth/demo-login") {
              blocked.push({ method, url: String(url || "") });
              return Promise.reject(new Error("Blocked write during read-only layout test: " + method + " " + url));
            }
            return nativeFetch(input, init);
          };
          const nativeSend = XMLHttpRequest.prototype.send;
          const nativeOpen = XMLHttpRequest.prototype.open;
          XMLHttpRequest.prototype.open = function(method, url) {
            this.__layoutMethod = String(method || "GET").toUpperCase();
            this.__layoutUrl = String(url || "");
            return nativeOpen.apply(this, arguments);
          };
          XMLHttpRequest.prototype.send = function() {
            let path = this.__layoutUrl;
            try { path = new URL(this.__layoutUrl, window.location.origin).pathname; } catch (_) {}
            if (!["GET", "HEAD", "OPTIONS"].includes(this.__layoutMethod || "GET") && isAllowedWrite(this.__layoutMethod || "GET", path, this.__layoutUrl)) {
              allowed.push({ method: this.__layoutMethod, url: this.__layoutUrl, path });
              return nativeSend.apply(this, arguments);
            }
            if (!["GET", "HEAD", "OPTIONS"].includes(this.__layoutMethod || "GET") && path !== "/auth/demo-login") {
              blocked.push({ method: this.__layoutMethod, url: this.__layoutUrl });
              throw new Error("Blocked XHR write during read-only layout test");
            }
            return nativeSend.apply(this, arguments);
          };
          if (window.sessionStorage.getItem("__layoutReadOnlyStorageResetDone") !== "true") {
            window.localStorage.setItem("cv.editor.tourCompleted", "true");
            window.localStorage.setItem("cv.editor.theme", "light");
            if (!window.localStorage.getItem("onepager.lang")) {
              window.localStorage.setItem("onepager.lang", "en");
            }
            const preservedEditorKeys = new Set([
              "cv.editor.atsProfile",
              "cv.editor.job_source_defaults",
            ]);
            for (const key of Object.keys(window.localStorage)) {
              if (key.startsWith("cv.editor.") &&
                  !key.includes("supabase") &&
                  !preservedEditorKeys.has(key)) {
                window.localStorage.removeItem(key);
              }
            }
            window.sessionStorage.setItem("__layoutReadOnlyStorageResetDone", "true");
          }
          window.localStorage.setItem("cv.editor.tourCompleted", "true");
          window.localStorage.setItem("cv.editor.theme", "light");
          if (!window.localStorage.getItem("onepager.lang")) {
            window.localStorage.setItem("onepager.lang", "en");
          }
        })();
        """
    )


def install_editor_storage_reset(page: Page, *, once: bool = False) -> None:
    guard = (
        "if (window.sessionStorage.getItem('__onepagerEditorStorageResetDone') === 'true') return;"
        if once
        else ""
    )
    mark = (
        "window.sessionStorage.setItem('__onepagerEditorStorageResetDone', 'true');"
        if once
        else ""
    )
    script = """
        (() => {
          // Init scripts run in child frames too. Never touch Web Storage in
          // the opaque-origin sandboxed preview.
          if (window.top !== window) return;
          __ONEPAGER_RESET_GUARD__
          const preservedEditorKeys = new Set([
            "cv.editor.atsProfile",
            "cv.editor.job_source_defaults",
          ]);
          for (const key of Object.keys(window.localStorage)) {
            if (key.startsWith("cv.editor.") && !key.includes("supabase") && !preservedEditorKeys.has(key)) {
              window.localStorage.removeItem(key);
            }
          }
          window.localStorage.setItem("cv.editor.tourCompleted", "true");
          window.localStorage.setItem("cv.editor.theme", "light");
          if (!window.localStorage.getItem("onepager.lang")) {
            window.localStorage.setItem("onepager.lang", "en");
          }
          __ONEPAGER_RESET_MARK__
        })();
        """
    page.add_init_script(
        script
        .replace("__ONEPAGER_RESET_GUARD__", guard)
        .replace("__ONEPAGER_RESET_MARK__", mark)
    )


def assert_no_blocked_writes(page: Page) -> None:
    blocked = page.evaluate("window.__layoutBlockedWrites || []")
    unexpected = [
        item
        for item in blocked
        if not (item.get("method") == "POST" and item.get("url") == "/api/ats-score")
    ]
    assert unexpected == []


def wait_for_network_settle(page: Page, *, timeout: int = 5000) -> None:
    """Let background requests settle, without making layout tests depend on quiescence.

    This used to wait for ``networkidle``. That state is now unreachable: every
    ``/api/`` response carries ``Cache-Control: no-store`` because it can
    contain a complete CV, so the shell's polling — ``/api/cvs``, ``/api/key``,
    ``/api/extract/status``, ``/api/jobs`` — goes to the network every time and
    the browser never sees 500 ms of quiet. The wait therefore timed out on
    every navigation and, because the timeout is swallowed by design, did so
    invisibly: the suite simply spent an extra five seconds per ``goto`` and
    ran itself into the job's 30-minute cap.

    ``load`` is a real state that is genuinely reached, so it settles what this
    helper was always documented to settle. Every call site follows it with an
    explicit signal — ``wait_for_editor_shell_ready``, ``expect(...)``, or a
    ``wait_for_function`` — and those, not this, are what the assertions rest
    on.
    """

    try:
        page.wait_for_load_state("load", timeout=timeout)
    except PlaywrightTimeoutError:
        pass


_EDITOR_PREVIEW_IDLE_JS = """
() => {
  const stage = document.querySelector("#preview-stage");
  const msg = stage && stage.dataset ? stage.dataset.previewMessageType : "";
  const msgDone = ["paged-rendered", "cv-content-rendered", "paged-error"].includes(msg);
  const loading = document.querySelector("#preview-loading");
  const loadingHidden = !loading || loading.hidden ||
    getComputedStyle(loading).display === "none";
  const iframe = document.querySelector("#preview");
  const dimmed = !!(iframe && iframe.classList.contains("is-recomputing"));
  return msgDone && loadingHidden && !dimmed;
}
"""


def wait_for_preview_idle(page: Page, *, timeout: int = 30000) -> None:
    """Wait until the /app preview iframe finished its paged.js render cycle.

    The ready signal is the iframe → parent postMessage handshake recorded on
    ``#preview-stage`` (``data-preview-message-type`` / ``data-content-height``),
    not a wall-clock guess: the spinner is hidden, the recompute dim is gone,
    and a terminal message type was seen. The state must then HOLD for a short
    stability window so a back-to-back second refresh cycle (boot can schedule
    more than one) cannot slip the caller in mid-render.
    """

    page.wait_for_function(_EDITOR_PREVIEW_IDLE_JS, timeout=timeout)
    page.wait_for_timeout(250)
    page.wait_for_function(_EDITOR_PREVIEW_IDLE_JS, timeout=timeout)


def wait_for_editor_shell_ready(page: Page, *, timeout: int = 30000) -> None:
    """Wait until the /app shell is fully booted and its chrome is stable.

    Covers every async mutation of the visible chrome after goto: the form
    rebuild (cards visible), the preview render cycle, webfonts, and the ATS
    badge reaching a terminal state (anything but ``loading``). Use this
    instead of fixed sleeps before driving keyboard focus (tab order shifts
    while these settle) or before screenshots.
    """

    page.wait_for_selector(".form-card", state="visible", timeout=timeout)
    page.wait_for_function(
        """
        () => {
          const badge = document.querySelector("#ats-badge");
          if (!badge || !badge.dataset) return true;
          const state = badge.dataset.state || "";
          return !!state && state !== "loading";
        }
        """,
        timeout=timeout,
    )
    page.evaluate("() => (document.fonts && document.fonts.ready) || true")
    wait_for_preview_idle(page, timeout=timeout)


def goto_route(page: Page, route: str, viewport: Viewport, *, login: bool = True) -> None:
    page.set_viewport_size({"width": viewport.width, "height": viewport.height})
    page.goto(f"{BASE_URL}{route}", wait_until="domcontentloaded")
    if login:
        login_if_demo_available(page)
    wait_for_network_settle(page)


def _has_auth_session(page: Page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => !!(
                  window.OnePagerAuth &&
                  window.OnePagerAuth.getSession &&
                  window.OnePagerAuth.getSession()
                )
                """
            )
        )
    except Exception:
        return False


def _assert_authenticated_api(page: Page) -> None:
    status = page.wait_for_function(
        """
        () => new Promise((resolve) => {
          let attempts = 0;
          const probe = () => {
            attempts += 1;
            fetch("/api/cvs", { cache: "no-store", credentials: "same-origin" })
              .then((res) => resolve(res.status && res.status < 400 ? res.status : false))
              .catch(() => {
                if (attempts >= 5) resolve(false);
                else setTimeout(probe, 250);
              });
          };
          probe();
        })
        """,
        timeout=30000,
    )
    status = status.json_value()
    assert status and status < 400, f"authenticated /api/cvs probe returned {status}"


def login_if_demo_available(page: Page) -> None:
    try:
        page.wait_for_function(
            """
            () => window.OnePagerAuth || document.querySelector("#auth-gate")
            """,
            timeout=10000,
        )
    except PlaywrightTimeoutError:
        pytest.fail("auth did not initialize")
    if _has_auth_session(page):
        _assert_authenticated_api(page)
        return

    gate = page.locator("#auth-gate")
    try:
        gate.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeoutError:
        if _has_auth_session(page):
            _assert_authenticated_api(page)
            return
        pytest.fail("auth gate did not appear and no session was available")
    demo = page.locator("#auth-demo-btn")
    if demo.count() == 0:
        pytest.skip("layout e2e needs an authenticated or demo-enabled local session")
    demo.click()
    wait_for_network_settle(page)
    expect(gate).to_be_hidden(timeout=15000)
    page.wait_for_function(
        """
        () => !!(
          window.OnePagerAuth &&
          window.OnePagerAuth.getSession &&
          window.OnePagerAuth.getSession()
        )
        """,
        timeout=20000,
    )
    _assert_authenticated_api(page)


def auth_storage_state(browser: Browser) -> dict:
    global _AUTH_STORAGE_STATE
    if _AUTH_STORAGE_STATE is not None:
        return _AUTH_STORAGE_STATE
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    install_layout_vendor_routes(page)
    install_editor_storage_reset(page)
    try:
        page.goto(f"{BASE_URL}/app", wait_until="domcontentloaded")
        login_if_demo_available(page)
        wait_for_network_settle(page)
        page.evaluate(
            """
            () => {
              const session = window.OnePagerAuth &&
                window.OnePagerAuth.getSession &&
                window.OnePagerAuth.getSession();
              if (session && session.access_token) {
                window.localStorage.setItem("__layoutSupabaseSession", JSON.stringify(session));
              }
            }
            """
        )
        _AUTH_STORAGE_STATE = context.storage_state()
        return _AUTH_STORAGE_STATE
    finally:
        context.close()


def visible_rects(page: Page, selectors: Iterable[str]) -> list[dict]:
    return page.evaluate(
        """
        (selectors) => selectors.flatMap((selector) => {
          return Array.from(document.querySelectorAll(selector)).map((el) => {
            const cs = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            const visible = !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" &&
              r.width > 0 && r.height > 0;
            return {
              selector,
              id: el.id || "",
              classes: el.className || "",
              text: (el.textContent || "").trim().replace(/\\s+/g, " ").slice(0, 80),
              left: r.left,
              right: r.right,
              top: r.top,
              bottom: r.bottom,
              width: r.width,
              height: r.height,
              scrollWidth: el.scrollWidth,
              clientWidth: el.clientWidth,
              visible,
            };
          }).filter((item) => item.visible);
        })
        """,
        list(selectors),
    )


def assert_inside_viewport(page: Page, selectors: Iterable[str]) -> None:
    bad = page.evaluate(
        """
        (selectors) => {
          const vw = window.innerWidth;
          const vh = window.innerHeight;
          return selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)).map((el) => {
            const cs = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            if (el.hidden || cs.display === "none" || cs.visibility === "hidden" || r.width <= 0 || r.height <= 0) return null;
            const outside = r.left < -0.5 || r.right > vw + 0.5 || r.top < -0.5 || r.bottom > vh + 0.5;
            return outside ? { selector, id: el.id || "", text: (el.textContent || "").trim(), rect: { left: r.left, right: r.right, top: r.top, bottom: r.bottom }, viewport: { width: vw, height: vh } } : null;
          }).filter(Boolean));
        }
        """,
        list(selectors),
    )
    assert bad == []


def assert_no_horizontal_overflow(page: Page, selector: str = "body") -> None:
    overflow = page.evaluate(
        """
        (selector) => {
          const el = document.querySelector(selector);
          if (!el) return null;
          return {
            selector,
            scrollWidth: Math.ceil(el.scrollWidth),
            clientWidth: Math.ceil(el.clientWidth),
            bodyScrollWidth: Math.ceil(document.body.scrollWidth),
            viewportWidth: window.innerWidth,
          };
        }
        """,
        selector,
    )
    assert overflow is not None
    assert overflow["scrollWidth"] <= overflow["clientWidth"] + 1, overflow
    assert overflow["bodyScrollWidth"] <= overflow["viewportWidth"] + 1, overflow


def assert_no_unexpected_visible_overflow(page: Page) -> None:
    offenders = page.evaluate(
        """
        () => {
          const allowed = [
            ".CodeMirror-scroll", "#editor-host", "#yaml-host", "#preview-wrap", "#preview",
            ".jb-import-preview-list", ".jb-grid", ".cl-library-list", ".tl-diff",
            ".cmdk-list", ".theme-grid", ".cv-catalog-list", "textarea", "input",
            // Visually-hidden a11y text (.sr-only) is intentionally clipped to
            // 1px — its scrollWidth always exceeds clientWidth by design.
            ".sr-only"
          ];
          const isVisible = (el) => {
            const cs = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0;
          };
          return Array.from(document.querySelectorAll("body *")).map((el) => {
            if (!isVisible(el)) return null;
            if (allowed.some((selector) => el.matches(selector) || el.closest(selector))) return null;
            const cs = getComputedStyle(el);
            if (cs.overflowX === "auto" || cs.overflowX === "scroll") return null;
            const delta = Math.ceil(el.scrollWidth) - Math.ceil(el.clientWidth);
            if (delta <= 2) return null;
            const r = el.getBoundingClientRect();
            if (r.width >= window.innerWidth - 1 && Math.ceil(document.body.scrollWidth) <= window.innerWidth + 1) return null;
            return {
              tag: el.tagName,
              id: el.id || "",
              className: String(el.className || ""),
              text: (el.textContent || "").trim().slice(0, 120),
              delta,
              scrollWidth: Math.ceil(el.scrollWidth),
              clientWidth: Math.ceil(el.clientWidth),
            };
          }).filter(Boolean).slice(0, 25);
        }
        """
    )
    assert offenders == []


def assert_no_visible_intersections(page: Page, selectors: Iterable[str]) -> None:
    rects = visible_rects(page, selectors)
    overlaps = []
    for i, left in enumerate(rects):
        for right in rects[i + 1 :]:
            intersects = (
                left["left"] < right["right"] - 0.5
                and left["right"] > right["left"] + 0.5
                and left["top"] < right["bottom"] - 0.5
                and left["bottom"] > right["top"] + 0.5
            )
            if intersects:
                overlaps.append({"a": left, "b": right})
    assert overlaps == []


def assert_no_text_overflow(page: Page, selectors: Iterable[str]) -> None:
    offenders = page.evaluate(
        """
        (selectors) => selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)).map((el) => {
          const cs = getComputedStyle(el);
          const r = el.getBoundingClientRect();
          if (el.hidden || cs.display === "none" || cs.visibility === "hidden" || r.width <= 0 || r.height <= 0) return null;
          if (!((el.textContent || "").trim())) return null;
          const overflowX = el.scrollWidth > el.clientWidth + 1 && cs.textOverflow !== "ellipsis";
          const overflowY = el.scrollHeight > el.clientHeight + 1 && cs.whiteSpace === "nowrap";
          return overflowX || overflowY ? {
            selector,
            id: el.id || "",
            text: (el.textContent || "").trim(),
            scrollWidth: el.scrollWidth,
            clientWidth: el.clientWidth,
            scrollHeight: el.scrollHeight,
            clientHeight: el.clientHeight,
          } : null;
        }).filter(Boolean))
        """,
        list(selectors),
    )
    assert offenders == []


def assert_touch_targets(page: Page, selectors: Iterable[str], minimum: int = 30) -> None:
    offenders = page.evaluate(
        """
        ({ selectors, minimum }) => selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)).map((el) => {
          const cs = getComputedStyle(el);
          const r = el.getBoundingClientRect();
          if (el.hidden || cs.display === "none" || cs.visibility === "hidden" || r.width <= 0 || r.height <= 0) return null;
          return (r.width < minimum || r.height < minimum) ? {
            selector,
            id: el.id || "",
            text: (el.textContent || "").trim(),
            width: r.width,
            height: r.height,
          } : null;
        }).filter(Boolean))
        """,
        {"selectors": list(selectors), "minimum": minimum},
    )
    assert offenders == []


def console_collector(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on(
        "console",
        lambda msg: console_errors.append(f"{msg.type}: {msg.text}") if msg.type == "error" else None,
    )
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    return console_errors, page_errors


def network_collector(page: Page) -> tuple[list[str], list[dict]]:
    request_failures: list[str] = []
    bad_responses: list[dict] = []
    page.on("requestfailed", lambda req: request_failures.append(f"{req.method} {req.url} {req.failure}"))

    def _on_response(res):
        status = res.status
        if status < 400:
            return
        url = res.url
        if "/favicon.ico" in url:
            return
        bad_responses.append({"status": status, "url": url})

    page.on("response", _on_response)
    return request_failures, bad_responses


def assert_clean_network(request_failures: list[str], bad_responses: list[dict], allow_statuses: Iterable[int] = ()) -> None:
    allowed = set(allow_statuses)
    assert request_failures == []
    unexpected = [item for item in bad_responses if item["status"] not in allowed]
    assert unexpected == []


def assert_clean_console(console_errors: list[str], page_errors: list[str]) -> None:
    ignored = (
        "Blocked write during read-only layout test",
    )
    filtered = [err for err in console_errors if not any(part in err for part in ignored)]
    assert filtered == [], filtered
    assert page_errors == [], page_errors
