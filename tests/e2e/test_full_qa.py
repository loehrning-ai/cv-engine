"""Full QA pass for the CV editor.

Validates every shipped feature end-to-end and reports PASS/FAIL with
detail. Uses Playwright headless against the live editor at :5567.
"""
from __future__ import annotations

import os
import re
import sys
import time
from playwright.sync_api import expect, sync_playwright
import yaml

try:
    from .layout_helpers import auth_storage_state, install_editor_storage_reset, install_layout_vendor_routes, require_live_server
except ImportError:  # pragma: no cover - direct script execution
    from layout_helpers import auth_storage_state, install_editor_storage_reset, install_layout_vendor_routes, require_live_server

URL = os.environ.get("ONEPAGER_E2E_BASE_URL", "http://127.0.0.1:5567").rstrip("/") + "/app"

PASS_LIST: list[str] = []
FAIL_LIST: list[tuple[str, str]] = []
WARN_LIST: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS_LIST.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL_LIST.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


def warn(name: str, detail: str):
    WARN_LIST.append((name, detail))
    print(f"  WARN  {name}  {detail}")


def print_report() -> None:
    total = len(PASS_LIST) + len(FAIL_LIST)
    print()
    print("=" * 60)
    print(f"  {len(PASS_LIST)} / {total} passed   {len(WARN_LIST)} warnings")
    print("=" * 60)
    if FAIL_LIST:
        print("\nFAILS:")
        for name, det in FAIL_LIST:
            print(f"  - {name}  {det}")
    if WARN_LIST:
        print("\nWARNINGS:")
        for name, det in WARN_LIST:
            print(f"  - {name}  {det}")


def ensure_appearance_open(page) -> None:
    if page.evaluate("document.querySelector('#theme-popover')?.hidden !== false"):
        page.click("#theme-btn", force=True)
    page.wait_for_selector("#theme-popover:not([hidden])")


def wait_for_theme_rows(page) -> int:
    rows = 0
    for _ in range(50):
        rows = page.evaluate("document.querySelectorAll('#theme-popover .theme-row').length")
        if rows:
            return rows
        page.wait_for_timeout(100)
    return rows


def fetch_text(page, path: str) -> dict:
    return page.evaluate(
        """async (path) => {
            const r = await fetch(path);
            const text = await r.text();
            return {
                ok: r.ok,
                status: r.status,
                contentType: r.headers.get("content-type") || "",
                text,
            };
        }""",
        path,
    )


def fetch_json(page, path: str) -> dict:
    return page.evaluate(
        """async (path) => {
            const r = await fetch(path);
            const text = await r.text();
            let data = null;
            try { data = JSON.parse(text); } catch (_) {}
            return {
                ok: r.ok,
                status: r.status,
                contentType: r.headers.get("content-type") || "",
                data,
                body: text.slice(0, 180),
            };
        }""",
        path,
    )


def yaml_field(text: str, field: str, pattern: str = r"(.+)") -> str | None:
    try:
        data = yaml.safe_load(text) or {}
        if isinstance(data, dict) and field in data:
            return str(data[field]).strip().strip("'\"")
    except Exception:
        pass
    match = re.search(rf"^{re.escape(field)}:\s*{pattern}$", text, re.MULTILINE)
    if not match:
        return None
    return (match.group(1) or "").strip().strip("'\"")


def wait_for_preview_section_order(page, *, timeout_ms: int = 6000) -> list[str]:
    deadline = time.time() + timeout_ms / 1000
    order: list[str] = []
    while time.time() < deadline:
        try:
            order = page.frame_locator("#preview").locator('[id^="sec-"]').evaluate_all(
                "(sections) => sections.map((section) => section.id)"
            )
        except Exception:
            order = []
        if len(order) >= 4:
            return order
        page.wait_for_timeout(150)
    return order


def preview_document_metrics(page) -> dict:
    """Inspect the opaque preview through Playwright, never through app code."""

    return page.frame_locator("#preview").locator("html").evaluate(
        """(html) => {
          const doc = html.ownerDocument;
          const pages = Array.from(doc.querySelectorAll(".pagedjs_page"));
          const page = pages[0] || doc.querySelector(".page");
          if (!page) {
            return {
              pageHeight: 0,
              pageWidth: 0,
              contentHeight: 0,
              contentWidth: 0,
            };
          }
          const pageRect = page.getBoundingClientRect();
          let bounds = null;
          (pages.length ? pages : [page]).forEach((entry) => {
            const rect = entry.getBoundingClientRect();
            if (!bounds) {
              bounds = {
                left: rect.left,
                top: rect.top,
                right: rect.right,
                bottom: rect.bottom,
              };
              return;
            }
            bounds.left = Math.min(bounds.left, rect.left);
            bounds.top = Math.min(bounds.top, rect.top);
            bounds.right = Math.max(bounds.right, rect.right);
            bounds.bottom = Math.max(bounds.bottom, rect.bottom);
          });
          return {
            pageHeight: Math.ceil(pageRect.height || page.scrollHeight || 0),
            pageWidth: Math.ceil(pageRect.width || page.scrollWidth || 0),
            contentHeight: Math.ceil(bounds ? bounds.bottom - bounds.top : pageRect.height),
            contentWidth: Math.ceil(bounds ? bounds.right - bounds.left : pageRect.width),
          };
        }"""
    )


def main():
    require_live_server()
    strict_writes = os.environ.get("ONEPAGER_FULL_QA_STRICT_WRITES", "1") != "0"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1480, "height": 920},
            device_scale_factor=2,
            storage_state=auth_storage_state(browser),
        )
        # Capture console errors
        console_errors: list[str] = []
        page_errors: list[str] = []
        bad_responses: list[dict[str, object]] = []
        page = ctx.new_page()
        install_layout_vendor_routes(page)
        install_editor_storage_reset(page, once=True)
        page.on("console", lambda msg: console_errors.append(f"{msg.type}: {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "response",
            lambda res: bad_responses.append({"status": res.status, "url": res.url}) if res.status >= 400 else None,
        )

        page.goto(URL, wait_until="domcontentloaded")
        auth_gate = page.locator("#auth-gate")
        try:
            auth_visible = auth_gate.count() > 0 and auth_gate.is_visible(timeout=2000)
        except Exception:
            auth_visible = False
        if auth_visible:
            page.locator("#auth-demo-btn").click()
            try:
                auth_gate.wait_for(state="hidden", timeout=20000)
                check("demo login hides auth gate", True)
            except Exception:
                detail = page.evaluate(
                    "document.querySelector('#auth-gate .auth-msg')?.textContent?.trim() || "
                    "'auth gate stayed visible after demo login'"
                )
                check("demo login hides auth gate", False, detail)
                browser.close()
                print_report()
                sys.exit(1)
        page.wait_for_load_state("networkidle")
        page.locator("#preview-stage").wait_for(state="attached")
        page.wait_for_timeout(2500)
        active_cv_id = page.evaluate("localStorage.getItem('cv.editor.activeId') || ''")
        active_cv_path = f"/api/cv?id={active_cv_id}" if active_cv_id else "/api/cv"

        # ───────────────────────────────────────────────────────
        # 1.  Bootstrap & DOM presence
        # ───────────────────────────────────────────────────────
        print("\n--- 1. Bootstrap ---")
        check("active CV id resolved", bool(re.fullmatch(r"[0-9a-fA-F-]{36}", active_cv_id)), active_cv_id)
        check("page title identifies editor", "editor" in page.title().lower() or "cv" in page.title().lower())
        check("form mounted", page.evaluate("document.querySelector('#form-host > *') !== null"))
        check("preview-stage exists", page.evaluate("document.querySelector('#preview-stage') !== null"))
        check("topbar exists", page.evaluate("document.querySelector('#topbar') !== null"))
        check("sidebar exists", page.evaluate("document.querySelector('#sidebar') !== null"))
        check("statusbar exists", page.evaluate("document.querySelector('#statusbar') !== null"))
        sections = page.evaluate("document.querySelectorAll('.form-section[data-section-key]').length")
        check("form sections (>=4)", sections >= 4, f"got {sections}")

        # ───────────────────────────────────────────────────────
        # 2.  Topbar elements
        # ───────────────────────────────────────────────────────
        print("\n--- 2. Topbar elements ---")
        check("save button", page.evaluate("document.querySelector('#save-btn') !== null"))
        check("build button", page.evaluate("document.querySelector('#build-btn') !== null"))
        check("theme picker button", page.evaluate("document.querySelector('#theme-btn') !== null"))
        check("theme toggle button", page.evaluate("document.querySelector('#theme-toggle') !== null"))
        check("font select", page.evaluate("document.querySelector('#font-select') !== null"))
        check("density select", page.evaluate("document.querySelector('#density-select') !== null"))
        check(
            "accent color control",
            page.evaluate(
                "document.querySelector('#accent-trigger.appearance-color') !== null && "
                "document.querySelector('#accent-input[type=\"color\"]') !== null && "
                "document.querySelector('#accent-value') !== null"
            ),
        )
        check("import button", page.evaluate("document.querySelector('#import-btn') !== null"))
        check("status pill", page.evaluate("document.querySelector('#status') !== null"))
        check("last-saved label", page.evaluate("document.querySelector('#last-saved') !== null"))

        # ───────────────────────────────────────────────────────
        # 3.  Appearance bar functionality
        # ───────────────────────────────────────────────────────
        print("\n--- 3. Appearance bar ---")
        font_opts = page.evaluate(
            "Array.from(document.querySelector('#font-select')?.options||[]).map(o=>o.value)"
        )
        check("font options serif/sans/mono", set(font_opts) == {"serif", "sans", "mono"}, str(font_opts))
        density_opts = page.evaluate(
            "Array.from(document.querySelector('#density-select')?.options||[]).map(o=>o.value)"
        )
        check("density options tight/normal/airy", set(density_opts) == {"tight", "normal", "airy"}, str(density_opts))
        # Try changing density — verify by hitting /api/cv after save
        ensure_appearance_open(page)
        page.select_option("#density-select", "tight")
        page.wait_for_timeout(500)
        page.keyboard.press("Meta+s")
        page.wait_for_timeout(2200)
        density_resp = fetch_text(page, active_cv_path)
        density_now = yaml_field(density_resp.get("text") or "", "density", r"(\w+)")
        if density_now == "tight" or strict_writes:
            check("changing density persists to YAML", density_now == "tight", f"yaml density = {density_now}")
        else:
            warn("changing density persistence skipped", f"live demo store unavailable or conflicted; yaml density = {density_now}")
        page.select_option("#font-select", "mono")
        page.wait_for_timeout(500)
        page.keyboard.press("Meta+s")
        page.wait_for_timeout(2200)
        font_resp = fetch_text(page, active_cv_path)
        font_now = yaml_field(font_resp.get("text") or "", "font", r"(\w+)")
        if font_now == "mono" or strict_writes:
            check("changing font persists to YAML", font_now == "mono", f"yaml font = {font_now}")
        else:
            warn("changing font persistence skipped", f"live demo store unavailable or conflicted; yaml font = {font_now}")
        page.select_option("#density-select", "normal")
        page.select_option("#font-select", "serif")
        page.wait_for_timeout(500)
        page.keyboard.press("Meta+s")
        page.wait_for_timeout(2200)

        # ───────────────────────────────────────────────────────
        # 4.  Topbar stability on save
        # ───────────────────────────────────────────────────────
        print("\n--- 4. Topbar stability ---")
        before = page.evaluate("document.querySelector('#build-btn')?.getBoundingClientRect().x")
        page.keyboard.press("Meta+s")
        page.wait_for_timeout(50)
        during = page.evaluate("document.querySelector('#build-btn')?.getBoundingClientRect().x")
        page.wait_for_timeout(2200)
        after = page.evaluate("document.querySelector('#build-btn')?.getBoundingClientRect().x")
        max_shift = max(abs((before or 0) - (during or 0)), abs((before or 0) - (after or 0)))
        check("build-btn x position stable on save", max_shift < 5, f"max shift = {max_shift}px")

        # ───────────────────────────────────────────────────────
        # 5.  Neutral contact-link icons in preview
        # ───────────────────────────────────────────────────────
        print("\n--- 5. Contact-link icons in preview ---")
        page.wait_for_timeout(1500)
        parent_can_read_preview = page.evaluate(
            "Boolean(document.querySelector('#preview')?.contentDocument)"
        )
        check("preview parent DOM remains opaque", not parent_can_read_preview)
        body_html = page.frame_locator("#preview").locator("body").inner_html(timeout=10000)
        check("preview HTML present", len(body_html) > 200, f"len={len(body_html)}")
        check("Neutral contact icon colour", "#374151" in body_html.upper())
        check("No LinkedIn logo colour", "#0A66C2" not in body_html.upper())
        check("No GitHub logo colour", "#181717" not in body_html.upper())
        check("Inline SVG icons", "brand-icon" in body_html or "<svg" in body_html.lower())

        # ───────────────────────────────────────────────────────
        # 6.  Form ↔ YAML toggle
        # ───────────────────────────────────────────────────────
        print("\n--- 6. View toggle ---")
        page.click(".view-tab[data-view='yaml']", force=True)
        page.wait_for_timeout(500)
        yaml_show = page.evaluate("getComputedStyle(document.querySelector('#editor-host')).display !== 'none'")
        check("YAML view shows", yaml_show)
        check("CodeMirror present", page.evaluate("document.querySelector('.CodeMirror') !== null"))
        marker_name = "YAML Form Rebuild QA"
        current_yaml = page.evaluate(
            """() => {
                const wrap = document.querySelector(".CodeMirror");
                const cm = wrap && wrap.CodeMirror;
                if (cm && typeof cm.getValue === "function") return String(cm.getValue() || "");
                return String(document.querySelector("#editor")?.value || "");
            }"""
        )
        next_yaml = (
            re.sub(r"^name:\s*.*$", f"name: {marker_name}", current_yaml, flags=re.MULTILINE)
            if re.search(r"^name:\s*.*$", current_yaml, flags=re.MULTILINE)
            else f"name: {marker_name}\n{current_yaml}"
        )
        yaml_mutated = page.evaluate(
            """({ yaml, marker }) => {
                const wrap = document.querySelector(".CodeMirror");
                const cm = wrap && wrap.CodeMirror;
                if (cm && typeof cm.setValue === "function") {
                    cm.setValue(yaml);
                    if (typeof cm.save === "function") cm.save();
                    return String(cm.getValue() || "").includes(marker);
                }
                const textarea = document.querySelector("#editor");
                if (!textarea) return false;
                textarea.value = yaml;
                textarea.dispatchEvent(new Event("input", { bubbles: true }));
                textarea.dispatchEvent(new Event("change", { bubbles: true }));
                return textarea.value.includes(marker);
            }""",
            {"yaml": next_yaml, "marker": marker_name},
        )
        page.wait_for_timeout(400)
        check("YAML buffer mutated for rebuild proof", yaml_mutated)
        page.click(".view-tab[data-view='form']", force=True)
        page.wait_for_timeout(400)
        form_show = page.evaluate("getComputedStyle(document.querySelector('#form-host')).display !== 'none'")
        check("Form view returns", form_show)
        rebuilt_name = page.evaluate(
            """() => {
                const fields = Array.from(document.querySelectorAll('.form-field'));
                const field = fields.find((node) => {
                    const section = node.closest('.form-section')?.getAttribute('data-section-key') || '';
                    const label = (node.querySelector('.form-field-label')?.textContent || '')
                        .replace(/\\s*\\*\\s*$/, '')
                        .trim();
                    return section === 'header' && /^(Full name|Name)$/i.test(label);
                });
                const input = field && field.querySelector('input, textarea');
                return input ? input.value : '';
            }"""
        )
        check("YAML edit rebuilds Form fields", rebuilt_name == marker_name, f"form name = {rebuilt_name!r}")

        # ───────────────────────────────────────────────────────
        # 7.  Collapsible sections + persistence
        # ───────────────────────────────────────────────────────
        print("\n--- 7. Collapsible sections ---")
        head_sel = ".form-section[data-section-key] .form-section-head.is-collapsible"
        head_count = page.evaluate(f"document.querySelectorAll('{head_sel}').length")
        check("collapsible heads exist", head_count >= 4, f"got {head_count}")
        if head_count:
            page.evaluate(f"document.querySelector('{head_sel}').click()")
            page.wait_for_timeout(150)
            collapsed = page.evaluate(f"document.querySelector('{head_sel}').getAttribute('aria-expanded') === 'false'")
            check("section collapses on click", collapsed)
            # Reload, verify persistence
            page.reload()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1800)
            still_collapsed = page.evaluate(f"document.querySelector('{head_sel}')?.getAttribute('aria-expanded') === 'false'")
            check("collapse state persists across reload", still_collapsed)
            # Restore
            if still_collapsed:
                page.evaluate(f"document.querySelector('{head_sel}').click()")
                page.wait_for_timeout(150)

        # ───────────────────────────────────────────────────────
        # 8.  Mode toggle (continuous ↔ paged)
        # ───────────────────────────────────────────────────────
        print("\n--- 8. Mode toggle ---")
        page.click("#mode-toggle .seg-btn[data-mode='continuous']", force=True)
        page.wait_for_timeout(1800)
        cont_active = page.evaluate("document.querySelector('#mode-toggle .seg-btn[data-mode=\"continuous\"]').classList.contains('is-active')")
        check("continuous mode active", cont_active)
        page.click("#mode-toggle .seg-btn[data-mode='paged']", force=True)
        page.wait_for_timeout(2000)
        paged_active = page.evaluate("document.querySelector('#mode-toggle .seg-btn[data-mode=\"paged\"]').classList.contains('is-active')")
        check("paged mode active", paged_active)
        page_count_visible = page.evaluate("!document.querySelector('#page-count')?.hidden")
        check("page-count pill visible in paged mode", page_count_visible)

        # ───────────────────────────────────────────────────────
        # 9.  Auto-render toggle
        # ───────────────────────────────────────────────────────
        print("\n--- 9. Auto-render toggle ---")
        page.click("#auto-render .seg-btn[data-auto='manual']", force=True)
        page.wait_for_timeout(150)
        manual_active = page.evaluate("document.querySelector('#auto-render .seg-btn[data-auto=\"manual\"]').classList.contains('is-active')")
        check("manual mode toggles", manual_active)
        page.click("#auto-render .seg-btn[data-auto='live']", force=True)
        page.wait_for_timeout(150)
        live_active = page.evaluate("document.querySelector('#auto-render .seg-btn[data-auto=\"live\"]').classList.contains('is-active')")
        check("live mode toggles", live_active)

        # ───────────────────────────────────────────────────────
        # 10.  Zoom controls + wrap scroll
        # ───────────────────────────────────────────────────────
        print("\n--- 10. Zoom controls + scroll ---")
        page.wait_for_function("""() => {
            const w = document.querySelector('#preview-wrap');
            const stage = document.querySelector('#preview-stage');
            return !!(w && stage && w.clientHeight > 0 && stage.getBoundingClientRect().height > 0);
        }""")
        zoom_ready = False
        for _ in range(16):
            page.click("#zoom-in", force=True)
            page.wait_for_timeout(100)
            zoom_ready = page.evaluate("""() => {
                const w = document.querySelector('#preview-wrap');
                return w.classList.contains('is-zoomed-in') && (w.scrollHeight - w.clientHeight) > 50;
            }""")
            if zoom_ready:
                break
        page.wait_for_timeout(400)
        zoomed = page.evaluate("document.querySelector('#preview-wrap').classList.contains('is-zoomed-in')")
        check("is-zoomed-in class added", zoomed)
        # Wrap should have scroll capacity
        scroll_cap = page.evaluate("(() => { const w=document.querySelector('#preview-wrap'); return w.scrollHeight - w.clientHeight; })()")
        check("wrap is scrollable when zoomed", scroll_cap > 50, f"scroll capacity = {scroll_cap}px")
        # Real wheel scroll
        wrap_box = page.evaluate("(() => { const r = document.querySelector('#preview-wrap').getBoundingClientRect(); return {x: r.x + r.width/2, y: r.y + 80}; })()")
        page.evaluate("document.querySelector('#preview-wrap').scrollTop = 0")
        before_scroll = page.evaluate("document.querySelector('#preview-wrap').scrollTop")
        page.mouse.move(int(wrap_box["x"]), int(wrap_box["y"]))
        page.mouse.wheel(0, 250)
        page.wait_for_timeout(250)
        after_scroll = page.evaluate("document.querySelector('#preview-wrap').scrollTop")
        check("mouse-wheel scrolls wrap when zoomed", (after_scroll - before_scroll) > 30, f"delta {after_scroll - before_scroll}")
        # Reset zoom
        page.click("#zoom-fit", force=True)
        page.wait_for_timeout(400)
        not_zoomed = page.evaluate("!document.querySelector('#preview-wrap').classList.contains('is-zoomed-in')")
        check("zoom-fit removes is-zoomed-in", not_zoomed)

        # ───────────────────────────────────────────────────────
        # 11.  Outline navigation
        # ───────────────────────────────────────────────────────
        print("\n--- 11. Outline ---")
        outline_count = page.evaluate("document.querySelectorAll('#outline-list .outline-item').length")
        check("outline has entries (>=4)", outline_count >= 4, f"got {outline_count}")
        # Click an Education outline entry
        clicked = page.evaluate("""() => {
            const items = document.querySelectorAll('#outline-list .outline-item');
            for (const it of items) {
                if ((it.textContent||'').trim().toLowerCase().includes('education')) { it.click(); return true; }
            }
            return false;
        }""")
        check("outline item click fires", clicked)

        # ───────────────────────────────────────────────────────
        # 12.  Theme picker — open + count + apply
        # ───────────────────────────────────────────────────────
        print("\n--- 12. Theme picker ---")
        ensure_appearance_open(page)
        popover_visible = page.evaluate("!document.querySelector('#theme-popover').hidden")
        check("theme popover opens", popover_visible)
        theme_rows = wait_for_theme_rows(page)
        check("theme picker has >=3 themes", theme_rows >= 3, f"got {theme_rows}")
        # Apply the LAST theme (deterministic — different from likely current)
        if theme_rows >= 2:
            accent_resp_before = fetch_text(page, active_cv_path)
            accent_before = yaml_field(
                accent_resp_before.get("text") or "",
                "accent",
                r"['\"]?(#[0-9a-fA-F]+)",
            )
            accent_before = accent_before.lower() if accent_before else None
            # Read the theme accents from /api/themes (authoritative)
            themes_resp = fetch_json(page, "/api/themes")
            themes_data = (themes_resp.get("data") or {}).get("themes") or []
            # Pick a theme whose accent differs from accent_before
            target_idx = next(
                (i for i, t in enumerate(themes_data) if (t.get("accent") or "").lower() != (accent_before or "").lower()),
                len(themes_data) - 1,
            )
            page.evaluate(f"document.querySelectorAll('#theme-popover .theme-row')[{target_idx}]?.click()")
            page.wait_for_timeout(500)
            page.keyboard.press("Meta+s")
            page.wait_for_timeout(2200)
            accent_resp_after = fetch_text(page, active_cv_path)
            accent_after = yaml_field(
                accent_resp_after.get("text") or "",
                "accent",
                r"['\"]?(#[0-9a-fA-F]+)",
            )
            accent_after = accent_after.lower() if accent_after else None
            check("applying theme changes accent in YAML", accent_before != accent_after, f"{accent_before} → {accent_after} (target idx {target_idx})")
            # restore default (idx 0)
            page.click("#theme-btn", force=True)
            wait_for_theme_rows(page)
            page.evaluate("document.querySelectorAll('#theme-popover .theme-row')[0]?.click()")
            page.wait_for_timeout(500)
            page.keyboard.press("Meta+s")
            page.wait_for_timeout(2200)
        else:
            warn("theme apply skipped", "fewer than 2 themes available")

        page.keyboard.press("Escape")
        current_template_resp = fetch_text(page, active_cv_path)
        current_template = yaml_field(current_template_resp.get("text") or "", "template", r"([\\w-]+)")
        target_template = "modern" if current_template == "sidebar" else "sidebar"
        page.click("#template-btn", force=True)
        page.wait_for_selector(f'#template-modal [data-template-card="{target_template}"]')
        page.locator(f'#template-modal [data-template-card="{target_template}"]').click()
        expect(page.locator("#template-apply")).to_be_enabled()
        page.locator("#template-apply").click()
        page.wait_for_function("() => document.querySelector('#template-modal')?.hidden === true")
        ensure_appearance_open(page)
        page.locator('[data-paper-theme="creme"]').click()
        page.wait_for_timeout(800)
        page.keyboard.press("Meta+s")
        page.wait_for_timeout(2200)
        appearance_resp = fetch_text(page, active_cv_path)
        template_now = yaml_field(appearance_resp.get("text") or "", "template", r"([\\w-]+)")
        paper_now = yaml_field(appearance_resp.get("text") or "", "paper", r"([\\w-]+)")
        if template_now == target_template or strict_writes:
            check("template gallery selection persists to YAML", template_now == target_template, f"yaml template = {template_now}")
        else:
            warn("template persistence skipped", f"live demo store unavailable or conflicted; yaml template = {template_now}")
        if paper_now == "creme" or strict_writes:
            check("paper theme persists to YAML", paper_now == "creme", f"yaml paper = {paper_now}")
        else:
            warn("paper persistence skipped", f"live demo store unavailable or conflicted; yaml paper = {paper_now}")
        preview_page = page.frame_locator("#preview").locator("main.page").first
        try:
            expect(preview_page).to_have_attribute("data-template", target_template, timeout=10000)
            expect(preview_page).to_have_attribute("data-paper", "creme", timeout=10000)
        except Exception:
            pass
        preview_appearance = {
            "template": preview_page.get_attribute("data-template") or "",
            "paper": preview_page.get_attribute("data-paper") or "",
        }
        check(
            "preview uses selected template and paper",
            preview_appearance.get("template") == target_template and preview_appearance.get("paper") == "creme",
            str(preview_appearance),
        )
        page.keyboard.press("Escape")
        page.click("#template-btn", force=True)
        page.wait_for_selector('#template-modal [data-template-card="classic"]')
        page.locator('#template-modal [data-template-card="classic"]').click()
        if target_template != "classic":
            page.locator("#template-apply").click()
        else:
            page.keyboard.press("Escape")
        page.wait_for_function("() => document.querySelector('#template-modal')?.hidden === true")
        ensure_appearance_open(page)
        page.locator('[data-paper-theme="white"]').click()
        page.wait_for_timeout(500)
        page.keyboard.press("Meta+s")
        page.wait_for_timeout(2200)

        # ───────────────────────────────────────────────────────
        # 13.  API endpoints
        # ───────────────────────────────────────────────────────
        print("\n--- 13. API endpoints ---")
        api_cv = fetch_text(page, active_cv_path)
        check("/api/cv 200", api_cv.get("ok"), str(api_cv))
        api_schema = fetch_json(page, "/api/schema")
        schema_data = api_schema.get("data") or {}
        check("/api/schema returns sections", api_schema.get("ok") and len(schema_data.get("sections") or []) >= 5, str(api_schema))
        api_themes = fetch_json(page, "/api/themes")
        themes_data = api_themes.get("data") or {}
        check("/api/themes returns themes", api_themes.get("ok") and len(themes_data.get("themes") or []) >= 3, str(api_themes))
        api_outline = fetch_json(page, f"/api/outline?id={active_cv_id}")
        outline_data = api_outline.get("data") or {}
        check("/api/outline returns items", api_outline.get("ok") and len(outline_data.get("items") or []) >= 5, str(api_outline))
        api_preview = page.evaluate(
            """async (cvId) => {
                const params = new URLSearchParams({
                    theme: "light", mode: "continuous", density: "normal", id: cvId,
                });
                const r = await fetch("/api/preview?" + params.toString());
                return { ok: r.ok, status: r.status };
            }""",
            active_cv_id,
        )
        check("/api/preview 200", api_preview.get("ok"), str(api_preview))

        # ───────────────────────────────────────────────────────
        # 14.  Build endpoint — returns binary PDF on success
        # ───────────────────────────────────────────────────────
        print("\n--- 14. Build endpoint ---")
        build_resp = page.evaluate("""async (cvId) => {
            try {
                const r = await fetch('/api/build', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ id: cvId }),
                });
                const ct = r.headers.get('content-type') || '';
                const ab = await r.arrayBuffer();
                return { ok: r.ok, status: r.status, contentType: ct, bytes: ab.byteLength };
            } catch (e) { return { ok: false, error: String(e) }; }
        }""", active_cv_id)
        if build_resp.get("ok") or strict_writes:
            check("/api/build POST succeeds", build_resp.get("ok"), str(build_resp))
        else:
            warn("/api/build POST skipped", f"live demo store unavailable: {build_resp}")
            bad_responses[:] = [
                item for item in bad_responses
                if "/api/build" not in str(item.get("url", ""))
            ]
        if build_resp.get("ok"):
            check("build response is PDF", "pdf" in (build_resp.get("contentType") or ""), str(build_resp))
            check("build PDF > 10KB", (build_resp.get("bytes") or 0) > 10000, f"bytes = {build_resp.get('bytes')}")

        # ───────────────────────────────────────────────────────
        # 15.  Save flow — change name field via form input + Cmd+S
        # ───────────────────────────────────────────────────────
        print("\n--- 15. Save flow ---")
        name_input = page.locator('input[type="text"]').first
        original_name = name_input.input_value()
        name_input.fill("Alex Hartman QA")
        page.wait_for_timeout(150)
        page.keyboard.press("Meta+s")
        page.wait_for_timeout(2200)
        # Verify the YAML document returned by the API now contains the new name
        name_resp = fetch_text(page, active_cv_path)
        yaml_name = yaml_field(name_resp.get("text") or "", "name")
        if yaml_name == "Alex Hartman QA" or strict_writes:
            check("Cmd+S persists name change to YAML", yaml_name == "Alex Hartman QA", f"yaml = {yaml_name}")
        else:
            status_text = page.evaluate("document.querySelector('#status')?.textContent || ''")
            warn("Cmd+S persistence skipped", f"live demo store unavailable or conflicted; yaml = {yaml_name}; status = {status_text!r}")
        last_saved_text = page.evaluate("document.querySelector('#last-saved')?.textContent || ''")
        check("last-saved label populated", len(last_saved_text.strip()) > 0, repr(last_saved_text))
        # Bytes-info shows size
        bytes_text = page.evaluate("document.querySelector('#bytes-info')?.textContent")
        check("bytes-info shows size", bytes_text and "B" in bytes_text, str(bytes_text))
        # Restore name
        name_input.fill(original_name)
        page.keyboard.press("Meta+s")
        page.wait_for_timeout(2000)

        # ───────────────────────────────────────────────────────
        # 16.  Form interaction — add then delete an experience entry
        # ───────────────────────────────────────────────────────
        print("\n--- 16. Form add / remove ---")
        before_card_count = page.evaluate("""() => {
            const sec = document.querySelector('.form-section[data-section-key="experience"]');
            return sec ? sec.querySelectorAll('.form-card').length : -1;
        }""")
        added_clicked = page.evaluate("""() => {
            const sec = document.querySelector('.form-section[data-section-key="experience"]');
            if (!sec) return { clicked: false, reason: "no section" };
            // Section may be collapsed — expand if needed
            const head = sec.querySelector('.form-section-head.is-collapsible');
            if (head && head.getAttribute('aria-expanded') === 'false') head.click();
            // Use the proper section-level "Add" button
            const add = sec.querySelector('.form-add-btn');
            if (!add) return { clicked: false, reason: "no .form-add-btn" };
            add.click();
            return { clicked: true };
        }""")
        page.wait_for_timeout(500)
        after_card_count = page.evaluate("""() => {
            const sec = document.querySelector('.form-section[data-section-key="experience"]');
            return sec ? sec.querySelectorAll('.form-card').length : -1;
        }""")
        check(
            "Add Experience adds a card",
            added_clicked.get("clicked") and after_card_count == before_card_count + 1,
            f"{before_card_count} → {after_card_count}; add result = {added_clicked}",
        )
        # Now delete the card we just added (last one)
        if after_card_count > before_card_count:
            deleted_clicked = page.evaluate("""() => {
                const sec = document.querySelector('.form-section[data-section-key="experience"]');
                const cards = sec.querySelectorAll('.form-card');
                const last = cards[cards.length - 1];
                if (!last) return false;
                const del = last.querySelector('button[data-action="delete"], button[aria-label*="delete" i], button[aria-label*="remove" i], .item-delete-btn');
                if (!del) {
                    const btns = Array.from(last.querySelectorAll('button'));
                    const target = btns.find(b => /(delete|remove|trash)/i.test(b.getAttribute('aria-label') || b.textContent || ''));
                    if (!target) return false;
                    target.click();
                    return true;
                }
                del.click();
                return true;
            }""")
            page.wait_for_timeout(500)
            final_card_count = page.evaluate("""() => {
                const sec = document.querySelector('.form-section[data-section-key="experience"]');
                return sec ? sec.querySelectorAll('.form-card').length : -1;
            }""")
            check(
                "Delete Experience removes the card",
                deleted_clicked and final_card_count == before_card_count,
                f"{after_card_count} → {final_card_count}",
            )

        # ───────────────────────────────────────────────────────
        # 17.  Tour pre-dismissed
        # ───────────────────────────────────────────────────────
        print("\n--- 17. Tour ---")
        tour_hidden = page.evaluate("document.querySelector('#tour')?.hidden !== false")
        check("tour stays hidden when seen", tour_hidden)

        # ───────────────────────────────────────────────────────
        # 18.  Dark mode
        # ───────────────────────────────────────────────────────
        print("\n--- 18. Dark mode ---")
        # Click theme-toggle to cycle to dark
        cycled = False
        for _ in range(4):
            page.click("#theme-toggle", force=True)
            page.wait_for_timeout(800)
            cur = page.evaluate("document.documentElement.dataset.theme")
            if cur == "dark":
                cycled = True
                break
        check("theme cycles to dark", cycled)
        if cycled:
            # App chrome dark mode must not change rendered CV/PDF colors.
            page.wait_for_timeout(800)
            chrome_scheme = page.evaluate("getComputedStyle(document.documentElement).colorScheme")
            check("app chrome owns native dark color scheme", "dark" in chrome_scheme, f"color-scheme={chrome_scheme}")
            iframe_theme = (
                page.frame_locator("#preview")
                .locator("html")
                .get_attribute("data-preview-theme")
                or ""
            )
            check("rendered CV preview remains PDF-matching light theme", iframe_theme == "light", f"theme={iframe_theme}")
        # Reset to light
        for _ in range(4):
            page.click("#theme-toggle", force=True)
            page.wait_for_timeout(800)
            cur = page.evaluate("document.documentElement.dataset.theme")
            if cur == "light":
                break

        # ───────────────────────────────────────────────────────
        # 19.  Section render order in PDF
        # ───────────────────────────────────────────────────────
        print("\n--- 19. Section order in preview ---")
        order = wait_for_preview_section_order(page)
        check("preview has multiple sections", len(order) >= 4, f"got {order}")
        # Should start with sec-header
        if order:
            check("first section is header", order[0] == "sec-header", f"got {order[0]}")
            # And the order should match registered sections (experience first)
            exp_idx = next((i for i, s in enumerate(order) if "experience" in s), -1)
            edu_idx = next((i for i, s in enumerate(order) if "education" in s), -1)
            if exp_idx >= 0 and edu_idx >= 0:
                check("experience before education", exp_idx < edu_idx)

        # ───────────────────────────────────────────────────────
        # 20.  Outline + form sections in registry order
        # ───────────────────────────────────────────────────────
        print("\n--- 20. Registry order honoured ---")
        outline_order = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('#outline-list .outline-item'))
                .map(e => e.getAttribute('data-slug') || e.textContent.trim().toLowerCase());
        }""")
        # First should be header
        if outline_order:
            check("outline starts with header", outline_order[0] in ("header", "Header"), f"got {outline_order[:3]}")

        # ───────────────────────────────────────────────────────
        # 21.  No console / page errors
        # ───────────────────────────────────────────────────────
        print("\n--- 21. JS error log ---")
        ignored_console = (
            "Failed to load resource: the server responded with a status of 400",
            "Failed to load resource: the server responded with a status of 409",
            "Failed to load resource: the server responded with a status of 500",
            "Failed to load resource: the server responded with a status of 503",
        )
        filtered_console = [
            err for err in console_errors
            if strict_writes or not any(marker in err for marker in ignored_console)
        ]
        if filtered_console:
            FAIL_LIST.append(("console errors", str(filtered_console[:3])))
            print(f"  FAIL  console errors  {filtered_console[:3]}")
        elif console_errors:
            warn("console errors ignored", str(console_errors[:3]))
        else:
            check("no console errors", True)
        if page_errors:
            FAIL_LIST.append(("page errors", str(page_errors[:3])))
            print(f"  FAIL  page errors  {page_errors[:3]}")
        else:
            check("no page errors", True)
        if bad_responses:
            FAIL_LIST.append(("http errors", str(bad_responses[:5])))
            print(f"  FAIL  http errors  {bad_responses[:5]}")
        else:
            check("no http errors", True)

        # ───────────────────────────────────────────────────────
        # 22.  Logo present and correctly styled
        # ───────────────────────────────────────────────────────
        print("\n--- 22. Brand glyph ---")
        check(
            "brand-mark renders SVG",
            page.evaluate("document.querySelector('.brand-mark svg') !== null"),
        )
        check(
            "brand-name reads 'CV Copilot'",
            page.evaluate("document.querySelector('.brand-name')?.textContent?.trim()").replace(" ", " ") == "CV Copilot",
        )

        # ───────────────────────────────────────────────────────
        # 23.  Preview iframe receives content height (zoom-scroll fix)
        # ───────────────────────────────────────────────────────
        print("\n--- 23. Content-height postMessage ---")
        try:
            page.wait_for_function(
                """() => {
                    const stage = document.querySelector('#preview-stage');
                    return Number(stage?.dataset.contentHeight || 0) > 0 &&
                           Number(stage?.dataset.pageHeight || 0) > 0 &&
                           stage?.dataset.previewMessageType !== '';
                }""",
                timeout=8000,
            )
        except Exception:
            pass
        ch_state = page.evaluate("""() => {
            const stage = document.querySelector('#preview-stage');
            const wrap = document.querySelector('#preview-wrap');
            return {
              stageH: stage.getBoundingClientRect().height,
              wrapH: wrap.clientHeight,
              messageType: stage.dataset.previewMessageType || '',
              contentH: Number(stage.dataset.contentHeight || 0),
              contentW: Number(stage.dataset.contentWidth || 0),
              pageH: Number(stage.dataset.pageHeight || 0),
              pageW: Number(stage.dataset.pageWidth || 0)
            };
        }""")
        doc_metrics = preview_document_metrics(page)
        ch_state.update({
            "docPageH": doc_metrics["pageHeight"],
            "docPageW": doc_metrics["pageWidth"],
            "docContentH": doc_metrics["contentHeight"],
            "docContentW": doc_metrics["contentWidth"],
        })
        check(
            "preview posted content-height metrics",
            ch_state["messageType"] in {"cv-content-rendered", "paged-rendered"} and
            ch_state["contentH"] > 0 and ch_state["contentW"] > 0 and
            ch_state["pageH"] > 0 and ch_state["pageW"] > 0,
            str(ch_state),
        )
        check(
            "posted content-height matches iframe document",
            abs(ch_state["contentH"] - ch_state["docContentH"]) <= 8 and
            abs(ch_state["contentW"] - ch_state["docContentW"]) <= 8 and
            abs(ch_state["pageH"] - ch_state["docPageH"]) <= 8 and
            abs(ch_state["pageW"] - ch_state["docPageW"]) <= 8,
            str(ch_state),
        )
        check(
            "stage at fit zoom is sized from rendered content",
            ch_state["stageH"] > 0 and ch_state["wrapH"] > 0,
            str(ch_state),
        )

        browser.close()


    # ───────────────────────────────────────────────────────
    # Final report
    # ───────────────────────────────────────────────────────
    print_report()
    sys.exit(1 if FAIL_LIST else 0)


if __name__ == "__main__":
    main()


def test_full_qa():
    try:
        main()
    except SystemExit as exc:
        assert exc.code == 0
