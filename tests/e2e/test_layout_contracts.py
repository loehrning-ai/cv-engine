from __future__ import annotations

import io
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

from .layout_helpers import (
    BASE_URL,
    CORE_VIEWPORTS,
    E2E_VIEWPORTS,
    Viewport,
    assert_clean_console,
    assert_clean_network,
    assert_inside_viewport,
    assert_no_blocked_writes,
    assert_no_horizontal_overflow,
    assert_no_text_overflow,
    assert_no_unexpected_visible_overflow,
    assert_no_visible_intersections,
    assert_touch_targets,
    add_top_document_init_script,
    auth_storage_state,
    console_collector,
    goto_route,
    install_editor_storage_reset,
    install_layout_vendor_routes,
    install_read_only_guards,
    network_collector,
    require_live_server,
    wait_for_editor_shell_ready,
    wait_for_preview_idle,
)


APP_TOPBAR_SELECTORS = [
    "#sidebar-toggle",
    ".crumbs",
    ".topbar-right > :not([hidden])",
]

SUBPAGE_CHROME_SELECTORS = [
    ".cl-topbar > .cl-back",
    ".cl-title",
    ".cl-nav",
    ".cl-utility-nav",
    ".cl-topbar-right > :not([hidden])",
]

CREDIT_STATES = [
    {"connected": True, "managed_credits_remaining": 0},
    {"connected": False, "managed_credits_remaining": 3},
    {"connected": False, "managed_credits_remaining": 1},
    {"connected": False, "managed_credits_remaining": 0},
]

SHARED_CHROME_VIEWPORTS = [
    Viewport(1440, 900),
    Viewport(1280, 720),
    Viewport(1121, 760),
    Viewport(1120, 760),
    Viewport(1000, 760),
    Viewport(840, 800),
    Viewport(820, 800),
    Viewport(761, 800),
    Viewport(760, 800),
    Viewport(430, 932),
    Viewport(390, 844),
    Viewport(320, 740),
]


@pytest.fixture(scope="module")
def browser():
    require_live_server()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


def new_page(browser):
    context = browser.new_context(
        device_scale_factor=1,
        storage_state=auth_storage_state(browser),
    )
    page = context.new_page()
    install_read_only_guards(page)
    console_errors, page_errors = console_collector(page)
    return context, page, console_errors, page_errors


def assert_required_visible_text_not_clipped(page, selectors: list[str]) -> None:
    result = page.evaluate(
        """
        (selectors) => {
          const missing = [];
          const clipped = [];
          for (const selector of selectors) {
            const visible = Array.from(document.querySelectorAll(selector)).filter((el) => {
              const cs = getComputedStyle(el);
              const r = el.getBoundingClientRect();
              const text = (el.textContent || "").replace(/\\s+/g, " ").trim();
              return !!text && !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0;
            });
            if (!visible.length) {
              missing.push({ selector, count: document.querySelectorAll(selector).length });
              continue;
            }
            visible.forEach((el) => {
              const cs = getComputedStyle(el);
              const r = el.getBoundingClientRect();
              const text = (el.textContent || "").replace(/\\s+/g, " ").trim();
              if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1) {
                clipped.push({
                  selector,
                  text,
                  scrollWidth: el.scrollWidth,
                  clientWidth: el.clientWidth,
                  scrollHeight: el.scrollHeight,
                  clientHeight: el.clientHeight,
                  rect: { left: r.left, right: r.right, width: r.width },
                  textOverflow: cs.textOverflow,
                });
              }
            });
          }
          return { missing, clipped };
        }
        """,
        selectors,
    )
    assert result["missing"] == []
    assert result["clipped"] == []


CATALOG_CV_ID = "10000000-0000-4000-8000-000000000001"
CATALOG_OLDER_CV_ID = "10000000-0000-4000-8000-000000000002"
CATALOG_LETTER_ID = "20000000-0000-4000-8000-000000000001"
CATALOG_JOB_LETTER_ID = "20000000-0000-4000-8000-000000000002"
CATALOG_JOB_ID = "30000000-0000-4000-8000-000000000001"


def catalog_fixture_page(browser, *, viewport: Viewport = Viewport(1440, 900), cvs_status: int = 200, documents_status: int = 200):
    context = browser.new_context(
        device_scale_factor=1,
        storage_state=auth_storage_state(browser),
        viewport={"width": viewport.width, "height": viewport.height},
    )
    page = context.new_page()
    install_editor_storage_reset(page)
    install_layout_vendor_routes(page)
    console_errors, page_errors = console_collector(page)
    cv_rows = [
        {"id": CATALOG_CV_ID, "name": "Primary CV", "job_id": None, "job_title": None, "updated_at": "2026-07-01T09:00:00Z"},
        {"id": CATALOG_OLDER_CV_ID, "name": "Archive CV", "job_id": None, "job_title": None, "updated_at": "2026-06-30T09:00:00Z"},
    ]
    documents = [
        {
            "id": CATALOG_CV_ID,
            "type": "cv",
            "name": "Primary CV",
            "updated_at": "2026-07-01T09:00:00Z",
            "actions": ["open", "build_pdf", "rename", "duplicate", "delete", "download_yaml"],
        },
        {
            "id": CATALOG_OLDER_CV_ID,
            "type": "cv",
            "name": "Archive CV",
            "updated_at": "2026-06-30T09:00:00Z",
            "actions": ["open", "build_pdf", "rename", "duplicate", "delete"],
        },
        {
            "id": CATALOG_LETTER_ID,
            "type": "cover_letter",
            "name": "Standalone Letter",
            "company": "Beta GmbH",
            "role": "Platform Engineer",
            "updated_at": "2026-06-29T09:00:00Z",
            "actions": ["open", "build_pdf", "rename", "delete"],
        },
        {
            "id": CATALOG_JOB_LETTER_ID,
            "type": "cover_letter",
            "name": "Acme Letter",
            "company": "Acme GmbH",
            "role": "Senior Data Engineer",
            "job_id": CATALOG_JOB_ID,
            "job_title": "Senior Data Engineer",
            "job_label": "Senior Data Engineer",
            "provenance_summary": "Linked to job: Senior Data Engineer",
            "updated_at": "2026-06-28T09:00:00Z",
            "actions": ["open", "build_pdf", "rename", "delete"],
        },
    ]
    calls: dict[str, list] = {"renames": []}

    def handle_api(route):
        req = route.request
        path = urlparse(req.url).path
        if path == "/api/cvs":
            if cvs_status >= 400:
                route.fulfill(status=cvs_status, content_type="application/json", body=json.dumps({"error": "cvs failed"}))
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"cvs": cv_rows, "count": 5, "limit": 5, "at_limit": True}))
        elif path == "/api/documents":
            if documents_status >= 400:
                route.fulfill(status=documents_status, content_type="application/json", body=json.dumps({"error": "documents failed"}))
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"documents": documents, "count": len(documents)}))
        elif path == "/api/cv":
            route.fulfill(status=200, headers={"X-OnePager-Revision": "rev"}, content_type="text/yaml", body="name: Primary CV\n")
        elif path.startswith("/api/cv/") and path.endswith("/rename") and req.method == "POST":
            cv_id = path.split("/")[-2]
            body = json.loads(req.post_data or "{}")
            calls["renames"].append({"kind": "cv", "id": cv_id, "body": body})
            for row in cv_rows:
                if row["id"] == cv_id:
                    row["name"] = body["name"]
            for doc in documents:
                if doc["id"] == cv_id:
                    doc["name"] = body["name"]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": cv_id, "name": body["name"]}))
        elif path.startswith("/api/cover-letter/") and path.endswith("/rename") and req.method == "POST":
            doc_id = path.split("/")[-2]
            body = json.loads(req.post_data or "{}")
            calls["renames"].append({"kind": "cover_letter", "id": doc_id, "body": body})
            for doc in documents:
                if doc["id"] == doc_id:
                    doc["name"] = body["name"]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": doc_id, "name": body["name"]}))
        elif path in {"/api/templates", "/api/themes", "/api/ats-profiles", "/api/extract/status", "/api/analytics/event", "/api/preview"}:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"templates": [], "themes": [], "profiles": [], "connected": False, "managed_credits_remaining": 3}))
        else:
            route.fallback()

    page.route("**/api/**", handle_api)
    return context, page, console_errors, page_errors, calls


LINEAGE_JOB_ID = "30000000-0000-4000-8000-000000000101"
LINEAGE_CV_IDS = {
    "usable": "10000000-0000-4000-8000-000000000101",
    "stale": "10000000-0000-4000-8000-000000000102",
    "archived": "10000000-0000-4000-8000-000000000103",
    "incomplete": "10000000-0000-4000-8000-000000000104",
}


def packet_lineage_fixture_page(browser, *, viewport: Viewport):
    context = browser.new_context(
        device_scale_factor=1,
        storage_state=auth_storage_state(browser),
        viewport={"width": viewport.width, "height": viewport.height},
    )
    page = context.new_page()
    install_editor_storage_reset(page)
    install_layout_vendor_routes(page)
    console_errors, page_errors = console_collector(page)
    request_failures, bad_responses = network_collector(page)
    active = {"cv_id": LINEAGE_CV_IDS["usable"]}
    score_calls: list[dict] = []
    write_requests: list[dict] = []

    def lineage(kind: str) -> dict:
        cv_id = LINEAGE_CV_IDS[kind]
        packet = {
            "packet_id": f"packet-{kind}",
            "id": f"packet-{kind}",
            "job_id": LINEAGE_JOB_ID,
            "source_cv_id": "10000000-0000-4000-8000-000000000100",
            "tailored_cv_id": cv_id,
            "cover_letter_id": f"20000000-0000-4000-8000-00000000010{len(kind)}",
            "source_cv_hash": "a" * 64,
            "job_content_hash": "b" * 64,
            "tailored_cv_hash": "c" * 64,
            "cover_letter_hash": "d" * 64,
            "score_system_id": "semantic_fit",
            "score_snapshot": {"score": 82},
            "score": 82,
            "prompt_version": "jobs_packet.v1",
            "packet_version": 1,
            "review_id": f"review-{kind}",
            "cover_letter_review_status": "confirmed",
            "cover_letter_review_confirmed_at": "2026-07-02T12:00:00+00:00",
            "cover_letter_review_id": f"review-{kind}",
            "document_role": "tailored_cv",
            "job_title": "Senior Data Engineer",
            "status": "packet_created",
            "stored_status": "packet_created",
            "stale_reasons": [],
        }
        if kind == "stale":
            packet.update({"status": "stale", "stored_status": "packet_created", "stale_reasons": ["source_cv_stale"]})
        elif kind == "archived":
            packet.update({"status": "archived", "stored_status": "archived", "archived_at": "2026-07-02T12:00:00+00:00"})
        elif kind == "incomplete":
            packet.update({
                "status": "review_applied",
                "stored_status": "review_applied",
                "cover_letter_review_status": "pending",
                "cover_letter_review_confirmed_at": None,
                "cover_letter_review_id": None,
            })
        return packet

    names = {
        "usable": "Usable CV",
        "stale": "Stale CV",
        "archived": "Archived CV",
        "incomplete": "Review CV",
    }
    order = ["usable", "stale", "archived", "incomplete"]

    def cv_rows() -> list[dict]:
        rows = []
        for index, kind in enumerate(order):
            cv_id = LINEAGE_CV_IDS[kind]
            rows.append(
                {
                    "id": cv_id,
                    "name": names[kind],
                    "job_id": LINEAGE_JOB_ID,
                    "job_title": "Data",
                    "job_label": "Data",
                    "updated_at": f"2026-07-02T12:0{index}:00Z",
                    "packet_lineage": lineage(kind),
                }
            )
        return rows

    def document_rows() -> list[dict]:
        rows = [
            {
                "id": row["id"],
                "type": "cv",
                "name": row["name"],
                "job_id": row["job_id"],
                "job_title": row["job_title"],
                "job_label": row["job_label"],
                "updated_at": "2026-07-02T13:00:00Z" if row["id"] == active["cv_id"] else row["updated_at"],
                "actions": ["open", "build_pdf", "rename", "duplicate", "delete"],
                "packet_lineage": row["packet_lineage"],
            }
            for row in cv_rows()
        ]
        active_id = active["cv_id"]
        rows.sort(key=lambda row: (row["id"] != active_id, row["updated_at"]), reverse=False)
        return rows

    def job_detail() -> dict:
        packets = [lineage(kind) for kind in order]
        return {
            "job": {
                "id": LINEAGE_JOB_ID,
                "title": "Data",
                "company": "Acme GmbH",
                "jd_text": "Title: Senior Data Engineer\nCompany: Acme GmbH\nSpark and Airflow.",
                "updated_at": "2026-07-02T12:00:00Z",
            },
            "packets": packets,
            "cvs": cv_rows(),
            "cover_letters": [],
        }

    def handle_api(route):
        req = route.request
        parsed = urlparse(req.url)
        path = parsed.path
        if req.method not in {"GET", "HEAD", "OPTIONS"} and path not in {"/auth/demo-login", "/api/ats-score"}:
            write_requests.append({"method": req.method, "path": path})
        if path == "/api/cvs":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"cvs": cv_rows(), "count": 4, "limit": 20, "at_limit": False}))
        elif path == "/api/documents":
            docs = document_rows()
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"documents": docs, "count": len(docs)}))
        elif path == "/api/cv":
            qs = parse_qs(parsed.query)
            active["cv_id"] = (qs.get("id") or [active["cv_id"]])[0]
            route.fulfill(status=200, headers={"X-OnePager-Revision": "rev"}, content_type="text/yaml", body=f"name: {active['cv_id']}\n")
        elif path == f"/api/jobs/{LINEAGE_JOB_ID}":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(job_detail()))
        elif path == "/api/ats-score":
            score_calls.append(json.loads(req.post_data or "{}"))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "score": 82,
                    "source": "deterministic",
                    "profile": {"id": "semantic_fit", "label": "Semantic fit"},
                    "matched": [{"term": "Spark", "importance": "must_have"}],
                    "gaps": [],
                    "tier1_coverage": 100,
                }),
            )
        elif path in {"/api/templates", "/api/themes", "/api/ats-profiles", "/api/extract/status", "/api/analytics/event", "/api/preview"}:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"templates": [], "themes": [], "profiles": [], "connected": False, "managed_credits_remaining": 3}))
        else:
            route.fallback()

    page.route("**/api/**", handle_api)
    return context, page, console_errors, page_errors, request_failures, bad_responses, score_calls, write_requests


@pytest.mark.parametrize("viewport", [Viewport(1440, 900), Viewport(390, 844)], ids=lambda v: v.label)
def test_catalog_sidebar_packet_lineage_states_are_visible_and_gate_ats_scoring(browser, viewport: Viewport):
    context, page, console_errors, page_errors, request_failures, bad_responses, score_calls, write_requests = packet_lineage_fixture_page(browser, viewport=viewport)
    try:
        goto_route(page, f"/app?id={LINEAGE_CV_IDS['usable']}", viewport)
        page.wait_for_selector("#sidebar-library .sidebar-library-row[data-library-kind='cv']", state="attached")

        def open_catalog():
            if viewport.width <= 760:
                page.locator("#mobile-utility-btn").click()
                page.wait_for_selector("#mobile-utility-popover:not([hidden])")
                page.locator('[data-mobile-action="document-catalog"]').click()
            else:
                page.locator("#cv-switcher").click()
            page.wait_for_selector("#cv-switcher-pop:not([hidden])")
            page.wait_for_selector("#cv-switcher-pop .cv-catalog-row[data-id]")

        expected = {
            "usable": ("packet-created", "packet-usable", "packet_created", "true", "Packet v1", "ready", "true", "82"),
            "stale": ("packet-stale", "packet-stale", "stale", "false", "Stale packet v1", "stale", "false", "ATS stale"),
            "archived": ("packet-archived", "packet-archived", "archived", "false", "Archived packet v1", "archived", "false", "ATS archived"),
            "incomplete": ("packet-incomplete", "packet-incomplete", "review_applied", "false", "Incomplete packet v1", "blocked", "false", "ATS blocked"),
        }
        open_catalog()
        for kind, (_label, packet_id, state, usable, text, _ats_state, _ats_usable, _badge_text) in expected.items():
            cv_id = LINEAGE_CV_IDS[kind]
            row = page.locator(f'#cv-switcher-pop .cv-catalog-row[data-id="{cv_id}"]')
            expect(row).to_have_attribute("data-doc-type", "cv")
            expect(row).to_have_attribute("data-packet-id", packet_id)
            expect(row).to_have_attribute("data-packet-state", state)
            expect(row).to_have_attribute("data-packet-version", "1")
            expect(row).to_have_attribute("data-packet-usable", usable)
            expect(row.locator(".cv-catalog-provenance")).to_contain_text(text)

        page.keyboard.press("Escape")
        for kind, (_label, packet_id, state, usable, text, ats_state, ats_usable, badge_text) in expected.items():
            cv_id = LINEAGE_CV_IDS[kind]
            open_catalog()
            page.locator(f'#cv-switcher-pop .cv-catalog-row[data-id="{cv_id}"] .cv-catalog-open').click()
            page.wait_for_url(f"**/app?id={cv_id}")
            sidebar = page.locator(f'#sidebar-library .sidebar-library-row[data-library-kind="cv"][data-id="{cv_id}"]')
            page.wait_for_selector(f'#sidebar-library .sidebar-library-row[data-library-kind="cv"][data-id="{cv_id}"]', state="attached")
            expect(sidebar).to_have_count(1)
            expect(sidebar).to_have_attribute("data-packet-id", packet_id)
            expect(sidebar).to_have_attribute("data-packet-state", state)
            expect(sidebar).to_have_attribute("data-packet-version", "1")
            expect(sidebar).to_have_attribute("data-packet-usable", usable)
            expect(sidebar.locator(".sidebar-library-meta")).to_contain_text(text)
            badge = page.locator("#ats-badge")
            expect(badge).to_have_attribute("data-state", ats_state)
            expect(badge).to_have_attribute("data-packet-usable", ats_usable)
            expect(badge).to_contain_text(badge_text)
            expect(page.locator("#ats-popover-hint")).to_be_attached()
            expect(page.locator("#ats-detail")).to_be_attached()

        assert score_calls
        assert {call["cv_id"] for call in score_calls} == {LINEAGE_CV_IDS["usable"]}
        missing_icons = page.evaluate(
            """
            () => Array.from(document.querySelectorAll(
              "#sidebar-library [data-icon], #cv-switcher-pop [data-icon], #ats-badge [data-icon]"
            )).map((el) => ({
              icon: el.dataset.icon,
              hasSvg: !!el.querySelector("svg.lucide"),
              html: el.innerHTML.trim(),
            })).filter((item) => !item.hasSvg)
            """
        )
        assert missing_icons == []
        assert write_requests == []
        assert_no_horizontal_overflow(page)
        assert_no_horizontal_overflow(page, "#sidebar-library")
        assert_no_text_overflow(page, [
            "#sidebar-library .sidebar-library-row",
            "#cv-switcher-pop .cv-catalog-row",
            "#cv-switcher-pop .cv-catalog-provenance",
            "#ats-badge",
        ])
        open_catalog()
        assert_required_visible_text_not_clipped(page, [
            "#cv-switcher-pop .cv-name",
            "#cv-switcher-pop .cv-catalog-provenance span",
        ])
        if viewport.width > 880:
            assert_required_visible_text_not_clipped(page, [
                "#sidebar-library .sidebar-library-name",
                "#sidebar-library .sidebar-library-meta",
            ])
        assert_clean_network(request_failures, bad_responses)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", CORE_VIEWPORTS, ids=lambda v: v.label)
def test_app_topbar_layout_is_non_overlapping_read_only(browser, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", viewport)
        page.wait_for_selector("#topbar")
        assert_no_horizontal_overflow(page)
        assert_inside_viewport(page, ["#topbar", *APP_TOPBAR_SELECTORS])
        assert_no_visible_intersections(page, APP_TOPBAR_SELECTORS)
        assert_no_text_overflow(page, ["#topbar button", "#topbar a", "#cv-switcher-label"])
        if viewport.width <= 430:
            assert_touch_targets(page, ["#topbar button", "#topbar a"], minimum=30)
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize(
    "viewport",
    [Viewport(820, 800), Viewport(761, 800), Viewport(760, 800), Viewport(700, 800), Viewport(390, 844), Viewport(320, 740)],
    ids=lambda v: v.label,
)
def test_editor_document_controls_remain_reachable_across_tablet_breakpoints(browser, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", viewport)
        page.wait_for_selector("#topbar")
        chrome = page.evaluate(
            """
            () => {
              const visible = (selector) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                const cs = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0;
              };
              return {
                docVisible: visible("#doc-actions-btn"),
                appearanceVisible: visible("#theme-btn"),
                utilityVisible: visible("#mobile-utility-btn"),
              };
            }
            """
        )
        if viewport.width > 760:
            assert chrome["docVisible"] is True
            assert chrome["appearanceVisible"] is True
        else:
            assert chrome["utilityVisible"] is True
            page.locator("#mobile-utility-btn").click()
            page.wait_for_selector("#mobile-utility-popover:not([hidden])")
            utility_text = page.locator("#mobile-utility-popover").inner_text()
            assert "Jobs" in utility_text
            assert "Cover letters" in utility_text
            assert "ATS" in utility_text
            assert "Document actions" in utility_text
            assert "CV Appearance" in utility_text
            assert_inside_viewport(page, ["#mobile-utility-popover"])
            assert_no_text_overflow(page, ["#mobile-utility-popover .mobile-utility-row"])
        assert_no_horizontal_overflow(page)
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_mobile_utility_opens_editor_popovers_without_same_click_close(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(390, 844))
        expect(page.locator("#sidebar-toggle")).to_be_hidden()
        checks = [
            ("appearance", "#theme-popover"),
            ("document", "#doc-actions-popover"),
            ("applications", "#workflow-popover"),
            ("ats-detail", "#ats-popover"),
        ]
        for action, popover in checks:
            page.locator("#mobile-utility-btn").click()
            page.wait_for_selector("#mobile-utility-popover:not([hidden])")
            page.locator(f'#mobile-utility-popover [data-mobile-action="{action}"]').click()
            page.wait_for_selector(f"{popover}:not([hidden])")
            expect(page.locator(popover)).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator(popover)).to_be_hidden()
        assert_no_horizontal_overflow(page)
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_ai_polish_controls_follow_provider_state_after_form_rebuild(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        page.route(
            "**/api/extract/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ai_available": False, "connected": False, "managed_credits_remaining": 0}),
            ),
        )
        goto_route(page, "/app", Viewport(390, 844))
        page.wait_for_function("() => document.querySelectorAll('.section-polish, .bullet-polish').length > 0")
        page.wait_for_function(
            """
            () => Array.from(document.querySelectorAll('.section-polish, .bullet-polish'))
              .every((el) => el.hidden)
            """
        )
        assert page.locator(".section-polish:not([hidden]), .bullet-polish:not([hidden])").count() == 0
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()

    context, page, console_errors, page_errors = new_page(browser)
    try:
        page.route(
            "**/api/extract/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ai_available": True, "connected": True, "managed_credits_remaining": 3}),
            ),
        )
        goto_route(page, "/app", Viewport(390, 844))
        page.wait_for_selector(".section-polish:not([hidden])")
        page.wait_for_selector(".bullet-polish:not([hidden])")
        assert_touch_targets(page, [".section-polish", ".bullet-polish"], minimum=30)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_german_topbar_labels_across_routes_and_mobile_utility(browser):
    required = {
        "/account": [
            ".cl-title span[data-i18n='account.title']",
            ".cl-utility-nav span[data-i18n='nav.account']",
        ],
        "/settings": [
            ".cl-title span[data-i18n='account.title']",
            ".cl-utility-nav span[data-i18n='nav.account']",
        ],
        "/tailor": [
            ".cl-title span[data-i18n='app.tailor']",
            ".cl-utility-nav span[data-i18n='nav.account']",
        ],
        "/jobs": [
            ".cl-title span[data-i18n='jobs.title']",
            ".cl-utility-nav span[data-i18n='nav.account']",
            "label[for='jb-filter-verification']",
        ],
        "/cover-letters": [
            ".cl-title span[data-i18n='cl.title']",
            ".cl-utility-nav span[data-i18n='nav.account']",
        ],
    }
    for route, selectors in required.items():
        context, page, console_errors, page_errors = new_page(browser)
        try:
            add_top_document_init_script(
                page,
                "localStorage.setItem('onepager.lang', 'de')",
            )
            goto_route(page, route, Viewport(1121, 760))
            page.wait_for_function("() => document.documentElement.lang === 'de'")
            assert_required_visible_text_not_clipped(page, selectors)
            assert_required_visible_text_not_clipped(page, [
                ".cl-topbar > .cl-back span:not([data-icon])",
                ".cl-nav a span:not([data-icon])",
                ".cl-utility-nav a span:not([data-icon])",
            ])
            assert_no_horizontal_overflow(page)
            assert_no_blocked_writes(page)
            assert_clean_console(console_errors, page_errors)
        finally:
            context.close()

    context, page, console_errors, page_errors = new_page(browser)
    try:
        add_top_document_init_script(
            page,
            "localStorage.setItem('onepager.lang', 'de')",
        )
        goto_route(page, "/app", Viewport(390, 844))
        page.locator("#mobile-utility-btn").click()
        page.wait_for_selector("#mobile-utility-popover:not([hidden])")
        utility = page.locator("#mobile-utility-popover")
        expect(utility.locator('[data-mobile-action="settings"]')).to_have_count(1)
        expect(utility.locator('[data-mobile-action="settings"]')).to_contain_text("Einstellungen")
        assert_required_visible_text_not_clipped(page, ["#mobile-utility-popover .mobile-utility-row span:not([data-icon])"])
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("viewport", SHARED_CHROME_VIEWPORTS, ids=lambda v: v.label)
def test_required_chrome_labels_do_not_clip_in_en_de_route_matrix(browser, lang: str, viewport: Viewport):
    required = {
        "/account": [".cl-title span[data-i18n='account.title']"],
        "/settings": [".cl-title span[data-i18n='account.title']"],
        "/tailor": [".cl-title span[data-i18n='app.tailor']"],
        "/jobs": [".cl-title span[data-i18n='jobs.title']", "label[for='jb-filter-verification']"],
        "/cover-letters": [".cl-title span[data-i18n='cl.title']"],
    }
    for route, selectors in required.items():
        context, page, console_errors, page_errors = new_page(browser)
        try:
            add_top_document_init_script(
                page,
                f"localStorage.setItem('onepager.lang', '{lang}')",
            )
            goto_route(page, route, viewport, login=route != "/privacy")
            page.wait_for_function(f"() => document.documentElement.lang === '{lang}'")
            assert_required_visible_text_not_clipped(page, selectors)
            if viewport.width > 1120:
                assert_required_visible_text_not_clipped(page, [".cl-utility-nav span[data-i18n='nav.account']"])
            else:
                hidden_nav = page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll(".cl-topbar > .cl-back, .cl-nav a, .cl-utility-nav a")).map((a) => ({
                      textVisible: Array.from(a.querySelectorAll("span:not([data-icon])")).some((span) => {
                        const cs = getComputedStyle(span);
                        const r = span.getBoundingClientRect();
                        return cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0;
                      }),
                      aria: a.getAttribute("aria-label") || "",
                      title: a.getAttribute("title") || "",
                      tooltip: a.getAttribute("data-tooltip") || "",
                      href: a.getAttribute("href") || "",
                      focusable: typeof a.focus === "function",
                    })).filter((item) => !item.textVisible)
                    """
                )
                assert all(item["aria"] and item["title"] and item["tooltip"] and item["href"] and item["focusable"] for item in hidden_nav)
            assert_no_horizontal_overflow(page)
            assert_clean_console(console_errors, page_errors)
        finally:
            context.close()

    context, page, console_errors, page_errors = new_page(browser)
    try:
        add_top_document_init_script(
            page,
            f"localStorage.setItem('onepager.lang', '{lang}')",
        )
        goto_route(page, "/app", viewport)
        page.wait_for_function(f"() => document.documentElement.lang === '{lang}'")
        switcher_visible = page.locator("#cv-switcher").evaluate(
            """el => {
              const cs = getComputedStyle(el);
              const r = el.getBoundingClientRect();
              return cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0;
            }"""
        )
        visible_label = page.locator("#cv-switcher-label").evaluate(
            """el => {
              const cs = getComputedStyle(el);
              const r = el.getBoundingClientRect();
              return cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0;
            }"""
        )
        if visible_label:
            assert_required_visible_text_not_clipped(page, ["#cv-switcher-label"])
        icon_controls = page.evaluate(
            """
            () => [
              "#sidebar-toggle",
              "#cv-switcher",
              "#save-btn",
              "#build-btn",
              "#theme-btn",
              "#template-btn",
              "#doc-actions-btn",
              "#refresh-btn",
              "#settings-btn",
              "#theme-toggle",
            ].map((selector) => {
              const el = document.querySelector(selector);
              if (!el) return null;
              const cs = getComputedStyle(el);
              const r = el.getBoundingClientRect();
              if (el.hidden || cs.display === "none" || cs.visibility === "hidden" || r.width <= 0 || r.height <= 0) return null;
              return {
                selector,
                aria: el.getAttribute("aria-label") || "",
                title: el.getAttribute("title") || "",
                tooltip: el.getAttribute("data-tooltip") || "",
                focusable: typeof el.focus === "function",
              };
            }).filter(Boolean)
            """
        )
        assert icon_controls
        assert all(item["aria"] and item["title"] and item["tooltip"] and item["focusable"] for item in icon_controls)
        if switcher_visible:
            aria = page.locator("#cv-switcher").get_attribute("aria-label") or ""
            title = page.locator("#cv-switcher").get_attribute("title") or ""
            tooltip = page.locator("#cv-switcher").get_attribute("data-tooltip") or ""
            assert aria and title and tooltip
            page.locator("#cv-switcher").focus()
            assert page.evaluate("() => document.activeElement && document.activeElement.id") == "cv-switcher"
            page.locator("#cv-switcher").press("Enter")
            page.wait_for_selector("#cv-switcher-pop:not([hidden])")
            page.keyboard.press("Escape")
            page.locator("#cv-switcher").press("Space")
            page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        else:
            page.locator("#mobile-utility-btn").click()
            page.wait_for_selector("#mobile-utility-popover:not([hidden])")
            row = page.locator('[data-mobile-action="document-catalog"]')
            expect(row).to_be_visible()
            row.focus()
            assert page.evaluate("() => document.activeElement?.dataset?.mobileAction") == "document-catalog"
            row.press("Enter")
            page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_settings_access_from_topbar_command_palette_and_mobile(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        add_top_document_init_script(
            page,
            """
            Object.keys(localStorage)
              .filter((key) => key.startsWith("cv.editor.collapsed::"))
              .forEach((key) => localStorage.removeItem(key));
            """
        )
        goto_route(page, "/app", Viewport(1440, 900))
        # Let the shell finish booting (form rebuild, ATS badge, preview,
        # fonts) before driving raw Tab focus: each of those can still mutate
        # the topbar mid-loop and shift the tab order under suite load.
        wait_for_editor_shell_ready(page)
        settings = page.locator("#settings-btn")
        expect(settings).to_be_visible()
        expect(page.locator("#mobile-utility-btn")).to_be_hidden()
        expect(settings).to_have_attribute("aria-label", re.compile("Settings"))
        page.locator("#sidebar-toggle").focus()
        focus_ids = []
        for _ in range(40):
            page.keyboard.press("Tab")
            active = page.evaluate("() => document.activeElement && (document.activeElement.id || document.activeElement.getAttribute('aria-label') || document.activeElement.tagName)")
            focus_ids.append(active)
            if active == "settings-btn":
                break
        assert "settings-btn" in focus_ids
        settings.press("Enter")
        page.wait_for_url(re.compile(r"/(settings|account#settings)$"))
        expect(page.locator("#settings")).to_be_visible()
        assert page.evaluate("location.pathname + location.hash") in ("/settings", "/account#settings")
        page.go_back()
        page.wait_for_url(re.compile(r"/app"))
        expect(page.locator("#settings-btn")).to_be_visible()
        page.locator("#settings-btn").focus()
        page.locator("#settings-btn").press("Space")
        page.wait_for_url(re.compile(r"/(settings|account#settings)$"))
        expect(page.locator("#settings")).to_be_visible()
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()

    context, page, console_errors, page_errors = new_page(browser)
    try:
        # Pin the palette's "Recent documents" group: it is fetched lazily
        # from /api/documents on open and rebuilds the result list when it
        # lands. Leftover demo-store documents (seeded by earlier tests) would
        # otherwise race the fill→Enter sequence and could hijack row 0.
        page.route(
            "**/api/documents",
            lambda route: route.fulfill(status=200, content_type="application/json", body='{"documents": []}'),
        )
        goto_route(page, "/app", Viewport(1440, 900))
        page.keyboard.press("Meta+K")
        page.wait_for_selector("#cmdk:not([hidden])")
        expect(page.locator(".cmdk-item").first).to_be_visible()
        page.locator(".cmdk-input").fill("settings")
        expect(page.locator(".cmdk-item").filter(has_text=re.compile("Settings", re.I)).first).to_be_visible()
        # Enter runs the ACTIVE row — wait until filtering settled on the
        # settings destination instead of whichever row happened to be first.
        expect(page.locator(".cmdk-item.is-active")).to_contain_text(re.compile("Settings & account", re.I))
        page.keyboard.press("Enter")
        page.wait_for_url(re.compile(r"/(settings|account#settings)$"))
        expect(page.locator("#settings")).to_be_visible()
        assert page.evaluate("location.pathname + location.hash") in ("/settings", "/account#settings")
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()

    context, page, console_errors, page_errors = new_page(browser)
    try:
        page.route(
            "**/api/documents",
            lambda route: route.fulfill(status=200, content_type="application/json", body='{"documents": []}'),
        )
        goto_route(page, "/app", Viewport(1440, 900))
        page.keyboard.press("Meta+K")
        page.wait_for_selector("#cmdk:not([hidden])")
        expect(page.locator(".cmdk-item").first).to_be_visible()
        page.locator(".cmdk-input").fill("app theme settings")
        expect(page.locator(".cmdk-item.is-active")).to_contain_text(re.compile("App theme settings", re.I))
        page.keyboard.press("Enter")
        page.wait_for_url(re.compile(r"/(settings|account#settings)$"))
        expect(page.locator("#settings")).to_be_visible()
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()

    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(820, 800))
        expect(page.locator("#settings-btn")).to_be_hidden()
        expect(page.locator("#mobile-utility-btn")).to_be_visible()
        page.locator("#mobile-utility-btn").click()
        page.wait_for_selector("#mobile-utility-popover:not([hidden])")
        expect(page.locator("#mobile-utility-popover [data-mobile-action='settings']")).to_have_count(1)
        expect(page.locator("#mobile-utility-popover [data-mobile-action='theme']")).to_have_count(0)
        expect(page.locator("#mobile-utility-popover a[href='/account']")).to_have_count(0)
        visible_signout = page.evaluate(
            """
            () => ["#logout-btn", "#mobile-utility-popover [data-mobile-action='signout']"].filter((selector) => {
              const el = document.querySelector(selector);
              if (!el) return false;
              const cs = getComputedStyle(el);
              const r = el.getBoundingClientRect();
              return !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" &&
                r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
            }).length
            """
        )
        assert visible_signout == 1
        page.keyboard.press("Escape")
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()

    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(390, 844))
        expect(page.locator("#settings-btn")).to_be_hidden()
        page.locator("#mobile-utility-btn").click()
        page.wait_for_selector("#mobile-utility-popover:not([hidden])")
        row = page.locator('[data-mobile-action="settings"]')
        expect(row).to_have_count(1)
        expect(page.locator('[data-mobile-action="theme"]')).to_have_count(0)
        for _ in range(18):
            page.keyboard.press("Tab")
            assert page.evaluate(
                """
                () => {
                  const active = document.activeElement;
                  return !!active && !!active.closest("#mobile-utility-popover");
                }
                """
            )
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("() => !!document.activeElement?.closest('#mobile-utility-popover')")
        row.click()
        page.wait_for_url(re.compile(r"/(settings|account#settings)$"))
        expect(page.locator("#settings")).to_be_visible()
        assert page.evaluate("location.pathname + location.hash") in ("/settings", "/account#settings")
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()

    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/account", Viewport(390, 844))
        visible_signout = page.evaluate(
            """
            () => ["#ac-logout", "#ac-mobile-logout", "#mobile-utility-popover [data-mobile-action='signout']"].filter((selector) => {
              const el = document.querySelector(selector);
              if (!el) return false;
              const cs = getComputedStyle(el);
              const r = el.getBoundingClientRect();
              return !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" &&
                r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
            }).length
            """
        )
        assert visible_signout == 1
        expect(page.locator("#ac-mobile-logout")).to_be_visible()
        page.locator("#mobile-utility-btn").click()
        page.wait_for_selector("#mobile-utility-popover:not([hidden])")
        visible_signout_with_utility = page.evaluate(
            """
            () => ["#ac-logout", "#ac-mobile-logout", "#mobile-utility-popover [data-mobile-action='signout']"].filter((selector) => {
              const el = document.querySelector(selector);
              if (!el) return false;
              const cs = getComputedStyle(el);
              const r = el.getBoundingClientRect();
              return !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0;
            }).length
            """
        )
        assert visible_signout_with_utility == 1
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize("route", ["/jobs", "/cover-letters", "/tailor", "/account", "/settings"])
@pytest.mark.parametrize(
    "viewport",
    [Viewport(1440, 900), Viewport(820, 800), Viewport(760, 800), Viewport(430, 932), Viewport(390, 844), Viewport(320, 740)],
    ids=lambda v: v.label,
)
def test_subpage_chrome_stays_inside_viewport_read_only(browser, route: str, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, route, viewport, login=route != "/privacy")
        page.wait_for_selector(".cl-topbar")
        assert_no_horizontal_overflow(page)
        assert_no_horizontal_overflow(page, ".cl-topbar")
        assert_no_unexpected_visible_overflow(page)
        assert_inside_viewport(page, [".cl-topbar", *SUBPAGE_CHROME_SELECTORS])
        assert_no_visible_intersections(page, SUBPAGE_CHROME_SELECTORS)
        assert_no_text_overflow(page, [".cl-topbar button", ".cl-topbar a", ".cl-title span:not([data-icon])", ".cl-credit"])
        if viewport.width <= 430:
            assert_touch_targets(page, [".cl-topbar button", ".cl-topbar a"], minimum=30)
            page.locator("#mobile-utility-btn").click()
            page.wait_for_selector("#mobile-utility-popover:not([hidden])")
            expect(page.locator("#mobile-utility-popover [data-mobile-action='document-catalog']")).to_be_hidden()
            page.keyboard.press("Escape")
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", [Viewport(1440, 900), Viewport(820, 800), Viewport(390, 844), Viewport(320, 740)], ids=lambda v: v.label)
def test_privacy_public_page_layout_stays_inside_viewport(browser, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/privacy", viewport, login=False)
        expect(page.locator(".lp-legal h1")).to_have_text("Privacy Policy")
        assert_no_horizontal_overflow(page)
        assert_no_unexpected_visible_overflow(page)
        assert_inside_viewport(page, [".lp-nav", ".lp-legal h1"])
        assert_no_text_overflow(page, [".lp-nav a", ".lp-legal h1", ".lp-footer"])
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_canonical_editor_links_do_not_point_to_landing(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        for route in ["/jobs", "/cover-letters", "/tailor", "/account"]:
            goto_route(page, route, Viewport(1280, 800))
            bad = page.evaluate(
                """
                () => Array.from(document.querySelectorAll('a[href="/"], a[href^="/?id="]')).map((a) => ({
                  href: a.getAttribute("href"),
                  text: (a.textContent || "").trim(),
                }))
                """
            )
            assert bad == [], {"route": route, "bad": bad}
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", [Viewport(1440, 900), Viewport(820, 800), Viewport(800, 800), Viewport(761, 800), Viewport(760, 800), Viewport(390, 844)])
def test_generation_credit_states_are_compact_and_accessible(browser, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/jobs", viewport)
        page.wait_for_selector("#jb-credit", state="attached")
        results = page.evaluate(
            """
            (states) => {
              const el = document.querySelector("#jb-credit");
              return states.map((state) => {
                window.OnePagerCredit.render(el, state);
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                return {
                  state: el.getAttribute("data-credit-state"),
                  text: el.textContent,
                  label: el.getAttribute("aria-label"),
                  width: r.width,
                  height: r.height,
                  hidden: el.hidden || cs.display === "none",
                  nowrap: cs.whiteSpace,
                  scrollWidth: el.scrollWidth,
                  clientWidth: el.clientWidth,
                };
              });
            }
            """,
            CREDIT_STATES,
        )
        for item in results:
            if viewport.width <= 760:
                assert item["hidden"] is True
                continue
            assert item["hidden"] is False
            assert item["height"] <= 32
            assert item["width"] <= 70
            assert item["nowrap"] == "nowrap"
            assert item["scrollWidth"] <= item["clientWidth"] + 1
            assert item["label"] and item["label"] != item["text"]
        assert_no_horizontal_overflow(page)
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_editor_popups_restore_focus_and_expose_expanded_state(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(1440, 900))
        page.wait_for_selector("#theme-btn")
        page.locator("#theme-btn").focus()
        page.keyboard.press("Enter")
        page.wait_for_selector("#theme-popover:not([hidden])")
        expanded = page.locator("#theme-btn").get_attribute("aria-expanded")
        controls = page.locator("#theme-btn").get_attribute("aria-controls")
        haspopup = page.locator("#theme-btn").get_attribute("aria-haspopup")
        assert expanded == "true"
        assert controls == "theme-popover"
        assert haspopup == "dialog"
        active_id = page.evaluate("document.activeElement && document.activeElement.id")
        assert active_id != "accent-input"
        page.mouse.click(8, 8)
        expect(page.locator("#theme-popover")).to_be_hidden()
        assert page.locator("#theme-btn").get_attribute("aria-expanded") == "false"
        page.locator("#theme-btn").focus()
        page.keyboard.press("Enter")
        page.wait_for_selector("#theme-popover:not([hidden])")
        page.keyboard.press("Escape")
        expect(page.locator("#theme-popover")).to_be_hidden()
        restored = page.evaluate("document.activeElement && document.activeElement.id")
        assert restored == "theme-btn"
        assert page.locator("#theme-btn").get_attribute("aria-expanded") == "false"
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_navbar_ia_keeps_command_search_out_of_primary_chrome(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(1440, 900))
        page.wait_for_selector("#topbar")
        chrome = page.evaluate(
            """
            () => {
              const topbar = document.querySelector("#topbar");
              const doc = document.querySelector("#doc-actions-btn");
              const appearance = document.querySelector("#theme-btn");
              const build = document.querySelector("#build-btn");
              const save = document.querySelector("#save-btn");
              const workflow = document.querySelector("#workflow-btn");
              const importBtn = document.querySelector("#import-btn");
              const importOwner = importBtn && importBtn.closest("#doc-actions-popover");
              const documentCluster = document.querySelector(".topbar-cluster-document");
              const rect = (el) => {
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return { left: r.left, right: r.right, width: r.width, text: (el.textContent || "").trim() };
              };
              return {
                visibleCommandTriggers: Array.from(topbar.querySelectorAll("[data-cmdk-open]")).filter((el) => {
                  const cs = getComputedStyle(el);
                  const r = el.getBoundingClientRect();
                  return !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0;
                }).length,
                hasDocumentActions: !!doc,
                documentExpanded: doc && doc.getAttribute("aria-expanded"),
                hasAppearance: !!appearance,
                appearanceControls: appearance && appearance.getAttribute("aria-controls"),
                hasWorkflow: !!workflow,
                importInDocumentActions: !!importOwner,
                commandClusterOwnsCurrentDoc: [save, build, appearance, doc].every((el) => el && el.closest(".topbar-cluster-document") === documentCluster),
                order: {
                  save: rect(save),
                  build: rect(build),
                  appearance: rect(appearance),
                  doc: rect(doc),
                  workflow: rect(workflow),
                },
              };
            }
            """
        )
        assert chrome["visibleCommandTriggers"] == 0
        assert chrome["hasDocumentActions"] is True
        assert chrome["documentExpanded"] == "false"
        assert chrome["hasAppearance"] is True
        assert chrome["appearanceControls"] == "theme-popover"
        assert chrome["hasWorkflow"] is True
        assert chrome["importInDocumentActions"] is True
        assert chrome["commandClusterOwnsCurrentDoc"] is True
        assert chrome["order"]["save"]["right"] <= chrome["order"]["build"]["left"] + 1
        assert chrome["order"]["build"]["right"] <= chrome["order"]["appearance"]["left"] + 1
        assert chrome["order"]["appearance"]["right"] <= chrome["order"]["doc"]["left"] + 1
        assert chrome["order"]["doc"]["right"] <= chrome["order"]["workflow"]["left"] + 1

        wait_for_editor_shell_ready(page)
        for route in ["/jobs", "/cover-letters", "/tailor", "/account"]:
            goto_route(page, route, Viewport(1440, 900))
            page.wait_for_selector(".cl-topbar")
            visible_commands = page.evaluate(
                """
                () => Array.from(document.querySelectorAll(".cl-topbar [data-cmdk-open]")).filter((el) => {
                  const cs = getComputedStyle(el);
                  const r = el.getBoundingClientRect();
                  return !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0;
                }).length
                """
            )
            assert visible_commands == 0
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_document_catalog_opens_and_document_actions_hold_management(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(1440, 900))
        page.wait_for_selector("#cv-switcher")
        current_visible = page.locator("#cv-switcher-label").inner_text().strip()
        current_label = page.locator("#cv-switcher").get_attribute("aria-label") or ""
        assert current_label.startswith("Open document catalog:")
        assert current_visible == "Document catalog"
        active_id = page.evaluate("localStorage.getItem('cv.editor.activeId') || ''")
        page.locator("#cv-switcher").click()
        page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        page.wait_for_selector("#cv-switcher-pop .cv-catalog-row")
        switcher = page.evaluate(
            """
            () => {
              const pop = document.querySelector("#cv-switcher-pop");
              const card = pop.querySelector(".cv-switcher-card").getBoundingClientRect();
              const trigger = document.querySelector("#cv-switcher").getBoundingClientRect();
              return {
                text: (pop.textContent || "").replace(/\\s+/g, " ").trim(),
                triggerWidth: trigger.width,
                left: card.left,
                right: card.right,
                top: card.top,
                bottom: card.bottom,
                width: card.width,
                height: card.height,
                viewport: { width: window.innerWidth, height: window.innerHeight },
                ariaModal: pop.getAttribute("aria-modal"),
                rows: Array.from(pop.querySelectorAll(".cv-catalog-row[data-id]")).map((row) => ({
                  id: row.dataset.id,
                  type: row.dataset.docType,
                  text: (row.textContent || "").replace(/\\s+/g, " ").trim(),
                  active: row.classList.contains("is-active"),
                  scrollWidth: row.scrollWidth,
                  clientWidth: row.clientWidth,
                })),
                filters: Array.from(pop.querySelectorAll("[data-doc-filter]")).map((btn) => btn.dataset.docFilter),
              };
            }
            """
        )
        assert switcher["triggerWidth"] <= 180
        assert switcher["ariaModal"] == "true"
        assert switcher["width"] >= 640
        assert switcher["left"] >= 0 and switcher["right"] <= switcher["viewport"]["width"]
        assert switcher["top"] >= 0 and switcher["bottom"] <= switcher["viewport"]["height"]
        assert {"all", "cv", "cover_letter"}.issubset(set(switcher["filters"]))
        if active_id:
            assert active_id in {row["id"] for row in switcher["rows"]}
        row_types = {row["type"] for row in switcher["rows"]}
        assert "cv" in row_types
        assert any(row["type"] == "cv" and (row["active"] or "Current CV" in row["text"]) for row in switcher["rows"])
        if "Open letter" in switcher["text"]:
            assert "cover_letter" in row_types
        assert all(row["scrollWidth"] <= row["clientWidth"] + 2 for row in switcher["rows"])
        assert "/cover-letters" not in switcher["text"]

        page.keyboard.press("Escape")
        expect(page.locator("#cv-switcher-pop")).to_be_hidden()
        assert page.evaluate("document.activeElement && document.activeElement.id") == "cv-switcher"

        page.locator("#cv-switcher").click()
        page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        page.keyboard.press("Tab")
        first_tab = page.evaluate("document.activeElement && (document.activeElement.id || document.activeElement.dataset.docFilter || document.activeElement.textContent.trim())")
        page.keyboard.press("Shift+Tab")
        shift_tab = page.evaluate("document.activeElement && (document.activeElement.id || document.activeElement.dataset.docFilter || document.activeElement.textContent.trim())")
        assert first_tab
        assert shift_tab
        page.mouse.click(2, 2)
        expect(page.locator("#cv-switcher-pop")).to_be_hidden()
        assert page.evaluate("document.activeElement && document.activeElement.id") == "cv-switcher"

        page.locator("#doc-actions-btn").click()
        page.wait_for_selector("#doc-actions-popover:not([hidden])")
        actions = page.evaluate(
            """
            () => Array.from(document.querySelectorAll("#doc-actions-popover button")).map((b) => b.id)
            """
        )
        assert {"import-btn", "cv-new-btn", "cv-rename-active-btn", "cv-duplicate-active-btn", "cv-download-btn", "cv-delete-active-btn"}.issubset(set(actions))
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_document_catalog_mobile_geometry_and_utility_access(browser):
    for viewport in [Viewport(820, 900), Viewport(760, 900), Viewport(390, 844), Viewport(320, 740)]:
        context, page, console_errors, page_errors = new_page(browser)
        try:
            goto_route(page, "/app", viewport)
            page.locator("#mobile-utility-btn").click()
            page.wait_for_selector("#mobile-utility-popover:not([hidden])")
            page.locator('[data-mobile-action="document-catalog"]').click()
            page.wait_for_selector("#cv-switcher-pop:not([hidden])")
            metrics = page.evaluate(
                """
                () => {
                  const pop = document.querySelector("#cv-switcher-pop");
                  const card = pop.querySelector(".cv-catalog-card").getBoundingClientRect();
                  const rows = Array.from(pop.querySelectorAll(".cv-catalog-row"));
                  return {
                    left: card.left,
                    right: card.right,
                    top: card.top,
                    bottom: card.bottom,
                    vw: innerWidth,
                    vh: innerHeight,
                    rowOverflow: rows.some((row) => row.scrollWidth > row.clientWidth + 2),
                    overscroll: getComputedStyle(pop.querySelector(".cv-catalog-list")).overscrollBehaviorY,
                  };
                }
                """
            )
            assert metrics["left"] >= 0 and metrics["right"] <= metrics["vw"]
            assert metrics["top"] >= 0 and metrics["bottom"] <= metrics["vh"]
            assert metrics["rowOverflow"] is False
            assert metrics["overscroll"] == "contain"
            page.locator('[data-doc-filter="cv"]').click()
            assert page.locator('[data-doc-filter="cv"]').get_attribute("aria-pressed") == "true"
            page.locator('[data-doc-filter="cover_letter"]').click()
            assert page.locator('[data-doc-filter="cover_letter"]').get_attribute("aria-pressed") == "true"
            assert_no_horizontal_overflow(page)
            assert_clean_console(console_errors, page_errors)
        finally:
            context.close()


def test_document_catalog_new_cv_limit_and_partial_fallback(browser):
    context, page, console_errors, page_errors, _calls = catalog_fixture_page(browser, documents_status=503)
    try:
        goto_route(page, f"/app?id={CATALOG_CV_ID}", Viewport(1440, 900))
        page.locator("#cv-switcher").click()
        page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        expect(page.locator("#cv-switcher-pop .cv-catalog-row")).to_have_count(2)
        expect(page.locator("#cv-catalog-limit-note")).to_contain_text("Free plan limit reached (5/5)")
        expect(page.locator("#cv-catalog-limit-note")).to_have_attribute("data-state", "limit")
        expect(page.locator("#cv-catalog-new-btn")).to_have_attribute("data-limit-state", "limit")
        expect(page.locator("#cv-catalog-new-btn")).to_have_attribute("aria-describedby", "cv-catalog-limit-note")
        assert "Could not load your CVs" not in page.locator("#cv-switcher-list").inner_text()
    finally:
        context.close()

    context, page, console_errors, page_errors, _calls = catalog_fixture_page(browser, cvs_status=500)
    try:
        page.goto(f"{BASE_URL}/app?id={CATALOG_CV_ID}", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        expect(page.locator("#auth-gate")).to_have_count(0)
        page.locator("#cv-switcher").click()
        page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        expect(page.locator("#cv-switcher-pop .cv-catalog-row")).to_have_count(4)
        assert "Could not load your CVs" not in page.locator("#cv-switcher-list").inner_text()
    finally:
        context.close()


def test_document_catalog_rename_dialog_accessibility_and_focus(browser):
    context, page, console_errors, page_errors, calls = catalog_fixture_page(browser)
    try:
        goto_route(page, f"/app?id={CATALOG_CV_ID}", Viewport(1440, 900))
        page.locator("#cv-switcher").click()
        page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        rename = page.locator(f'#cv-switcher-pop .cv-catalog-row[data-id="{CATALOG_OLDER_CV_ID}"] [data-catalog-action="rename"]')
        rename.click()
        dialog = page.locator('#rename-dialog [role="dialog"]')
        expect(dialog).to_be_visible()
        expect(dialog).to_have_attribute("aria-modal", "true")
        expect(page.get_by_role("textbox", name="Document name")).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#rename-dialog")).to_have_count(0)
        expect(rename).to_be_focused()

        rename.click()
        page.get_by_role("textbox", name="Document name").fill("Renamed Archive CV")
        page.keyboard.press("Enter")
        page.wait_for_function("() => !document.querySelector('#rename-dialog')")
        page.wait_for_function("(id) => document.activeElement && document.activeElement.dataset.id === id", arg=CATALOG_OLDER_CV_ID)
        assert calls["renames"][-1] == {"kind": "cv", "id": CATALOG_OLDER_CV_ID, "body": {"name": "Renamed Archive CV"}}
        expect(page.locator(f'#cv-switcher-pop .cv-catalog-row[data-id="{CATALOG_OLDER_CV_ID}"]')).to_contain_text("Renamed Archive CV")
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_document_catalog_row_actions_search_groups_and_mobile_focus(browser):
    context, page, console_errors, page_errors, _calls = catalog_fixture_page(browser)
    try:
        goto_route(page, f"/app?id={CATALOG_CV_ID}", Viewport(1440, 900))
        page.locator("#cv-switcher").click()
        page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        expect(page.locator('[data-catalog-section="cv"] .cv-catalog-row')).to_have_count(2)
        expect(page.locator('[data-catalog-section="cover_letter"] .cv-catalog-row')).to_have_count(1)
        expect(page.locator('[data-catalog-section="job_artifact"] .cv-catalog-row')).to_have_count(1)
        assert page.locator('[data-doc-count="all"]').inner_text() == "4"
        assert page.locator('[data-doc-count="cv"]').inner_text() == "2"
        assert page.locator('[data-doc-count="cover_letter"]').inner_text() == "2"

        page.locator("#cv-catalog-search").fill("Acme")
        expect(page.locator("#cv-switcher-pop .cv-catalog-row")).to_have_count(1)
        expect(page.locator("#cv-switcher-pop .cv-catalog-row")).to_contain_text("Acme Letter")
        page.locator("#cv-catalog-search").fill("no-match")
        expect(page.locator("#cv-switcher-list")).to_contain_text("No documents match this search.")

        page.locator("#cv-catalog-search").fill("")
        page.locator(f'#cv-switcher-pop .cv-catalog-row[data-id="{CATALOG_JOB_LETTER_ID}"] [data-catalog-action="job"]').click()
        page.wait_for_url(f"**/jobs?id={CATALOG_JOB_ID}")
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()

    context, page, console_errors, page_errors, _calls = catalog_fixture_page(browser, viewport=Viewport(390, 844))
    try:
        goto_route(page, f"/app?id={CATALOG_CV_ID}", Viewport(390, 844))
        page.locator("#mobile-utility-btn").click()
        page.wait_for_selector("#mobile-utility-popover:not([hidden])")
        page.locator('[data-mobile-action="document-catalog"]').click()
        page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        expect(page.locator("#cv-catalog-search")).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#cv-switcher-pop")).to_be_hidden()
        assert page.evaluate("document.activeElement && document.activeElement.id") == "mobile-utility-btn"
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_applications_popover_exposes_cover_letters_and_job_workflow(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(1440, 900))
        page.wait_for_selector("#workflow-btn")
        page.locator("#workflow-btn").click()
        page.wait_for_selector("#workflow-popover:not([hidden])")
        workflow = page.evaluate(
            """
            () => {
              const pop = document.querySelector("#workflow-popover");
              const cover = document.querySelector("#cover-letters-btn");
              const cvs = document.querySelector("#workflow-cvs-btn");
              const jobs = document.querySelector("#jobs-btn");
              const card = pop.querySelector(".workflow-card").getBoundingClientRect();
              return {
                text: (pop.textContent || "").replace(/\\s+/g, " ").trim(),
                cvHref: cvs && cvs.getAttribute("href"),
                coverHref: cover && cover.getAttribute("href"),
                jobsHref: jobs && jobs.getAttribute("href"),
                card: { left: card.left, right: card.right, top: card.top, bottom: card.bottom },
                viewport: { width: window.innerWidth, height: window.innerHeight },
              };
            }
            """
        )
        assert workflow["cvHref"] == "/app"
        assert workflow["coverHref"] == "/cover-letters"
        assert workflow["jobsHref"] == "/jobs"
        assert "Start with a job" in workflow["text"]
        assert "Document libraries" in workflow["text"]
        assert "Review/ATS" in workflow["text"]
        assert "Saved CVs" in workflow["text"]
        assert "Saved in Documents" in workflow["text"]
        assert "Saved cover letters" in workflow["text"]
        assert "Open drafts or generate a new letter" in workflow["text"]
        assert workflow["card"]["left"] >= 0
        assert workflow["card"]["right"] <= workflow["viewport"]["width"] + 1
        assert workflow["card"]["bottom"] <= workflow["viewport"]["height"] + 1

        page.locator("#cover-letters-btn").click()
        page.wait_for_url("**/cover-letters")
        page.wait_for_selector(".cl-title")
        assert page.locator(".cl-title").inner_text().strip() == "Cover letters"
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_template_picker_switches_preview_template(browser):
    context = browser.new_context(
        device_scale_factor=1,
        storage_state=auth_storage_state(browser),
    )
    page = context.new_page()
    install_editor_storage_reset(page)
    install_layout_vendor_routes(page)
    preview_templates: list[str] = []
    console_errors, page_errors = console_collector(page)
    try:
        page.set_viewport_size({"width": 1440, "height": 900})

        def handle_api(route):
            url = route.request.url
            if "/api/templates" in url:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "templates": [
                            {"id": "classic", "label": "Classic", "use_case": "Conservative roles", "renderer": "standard", "layout_family": "single_column", "column_count": 1, "ats_priority": "high", "customization_axes": ["accent", "font"], "paper_sizes": ["A4"], "one_page_risk": "normal"},
                            {"id": "technical", "label": "Technical", "use_case": "Engineering and data roles", "renderer": "standard", "layout_family": "compact", "column_count": 1, "ats_priority": "high", "customization_axes": ["accent", "font", "density"], "paper_sizes": ["A4"], "one_page_risk": "low"},
                        ]
                    }),
                )
                return
            if "/api/preview" in url:
                marker = "template="
                if marker in url:
                    preview_templates.append(url.split(marker, 1)[1].split("&", 1)[0])
                route.fulfill(status=200, content_type="text/html", body="<main class='page'>Preview</main>")
                return
            if url.endswith("/api/cv") and route.request.method == "GET":
                route.fulfill(
                    status=200,
                    content_type="text/yaml",
                    body='name: Template Test\ntemplate: classic\nfont: sans\ndensity: normal\naccent: "#111111"\ncontact: []\nexperience: []\neducation: []\nskills: []\nprojects: []\nleadership: []\nothers: []\n',
                )
                return
            if url.endswith("/api/cv") and route.request.method == "POST":
                route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
                return
            route.fallback()

        page.route("**/api/**", handle_api)
        page.goto(f"{BASE_URL}/app", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        page.locator("#template-btn").focus()
        page.keyboard.press("Enter")
        page.wait_for_selector("#template-modal:not([hidden])")
        expect(page.locator("#template-search")).to_be_focused()
        expect(page.locator("#theme-popover #template-select")).to_have_count(0)
        expect(page.locator("#template-current")).to_contain_text("Classic")
        expect(page.locator("#template-selected")).to_contain_text("Classic")
        expect(page.locator('#template-modal [data-template-card="technical"]')).to_be_visible()
        preview_count = len(preview_templates)
        page.locator('#template-modal [data-template-card="technical"]').click()
        expect(page.locator('#template-modal [data-template-card="technical"]')).to_have_attribute("aria-checked", "true")
        expect(page.locator("#template-selected")).to_contain_text("Technical")
        expect(page.locator("#template-apply")).to_be_enabled()
        page.wait_for_timeout(150)
        assert len(preview_templates) == preview_count
        page.locator("#template-apply").focus()
        page.keyboard.press("Tab")
        expect(page.locator("#template-close")).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(page.locator("#template-apply")).to_be_focused()
        with page.expect_response(lambda res: "/api/preview" in res.url and "template=technical" in res.url):
            page.locator("#template-apply").click()
        expect(page.locator("#template-modal")).to_be_hidden()
        expect(page.locator("#template-btn")).to_be_focused()
        assert "technical" in preview_templates

        page.locator("#template-btn").click()
        page.wait_for_selector("#template-modal:not([hidden])")
        expect(page.locator('#template-modal [data-template-card="technical"]')).to_have_attribute("aria-checked", "true")
        preview_count = len(preview_templates)
        page.locator('#template-modal [data-template-card="classic"]').click()
        expect(page.locator("#template-selected")).to_contain_text("Classic")
        page.locator("#template-cancel").click()
        expect(page.locator("#template-modal")).to_be_hidden()
        expect(page.locator("#template-btn")).to_be_focused()
        page.wait_for_timeout(150)
        assert len(preview_templates) == preview_count

        page.locator("#template-btn").click()
        page.wait_for_selector("#template-modal:not([hidden])")
        expect(page.locator('#template-modal [data-template-card="technical"]')).to_have_attribute("aria-checked", "true")
        page.locator('#template-modal [data-template-card="classic"]').click()
        page.keyboard.press("Escape")
        expect(page.locator("#template-modal")).to_be_hidden()
        expect(page.locator("#template-btn")).to_be_focused()
        page.locator("#template-btn").click()
        page.wait_for_selector("#template-modal:not([hidden])")
        expect(page.locator('#template-modal [data-template-card="technical"]')).to_have_attribute("aria-checked", "true")
        assert_no_horizontal_overflow(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_template_gallery_cards_click_render_and_do_not_overflow(browser):
    context = browser.new_context(
        device_scale_factor=1,
        storage_state=auth_storage_state(browser),
        viewport={"width": 390, "height": 844},
    )
    page = context.new_page()
    install_layout_vendor_routes(page)
    preview_templates: list[str] = []
    console_errors, page_errors = console_collector(page)
    try:
        def handle_api(route):
            url = route.request.url
            if "/api/templates" in url:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "templates": [
                            {"id": "classic", "label": "Classic", "use_case": "Conservative roles", "renderer": "standard", "layout_family": "single_column", "column_count": 1, "ats_priority": "high", "customization_axes": ["accent", "font"], "paper_sizes": ["A4"], "one_page_risk": "normal"},
                            {"id": "technical", "label": "Technical", "use_case": "Engineering and data roles", "renderer": "standard", "layout_family": "compact", "column_count": 1, "ats_priority": "high", "customization_axes": ["accent", "font", "density"], "paper_sizes": ["A4"], "one_page_risk": "low"},
                            {"id": "ats-compact", "label": "ATS Compact", "use_case": "Dense screening-friendly applications with long labels", "renderer": "standard", "layout_family": "compact", "column_count": 1, "ats_priority": "high", "customization_axes": ["accent", "font", "density"], "paper_sizes": ["A4"], "one_page_risk": "low"},
                        ]
                    }),
                )
                return
            if "/api/preview" in url:
                marker = "template="
                if marker in url:
                    preview_templates.append(url.split(marker, 1)[1].split("&", 1)[0])
                route.fulfill(status=200, content_type="text/html", body="<main class='page'>Preview</main>")
                return
            if url.endswith("/api/cv") and route.request.method == "GET":
                route.fulfill(
                    status=200,
                    content_type="text/yaml",
                    body='name: Template Test\ntemplate: classic\nfont: sans\ndensity: normal\naccent: "#111111"\ncontact: []\nexperience: []\neducation: []\nskills: []\nprojects: []\nleadership: []\nothers: []\n',
                )
                return
            if url.endswith("/api/cv") and route.request.method == "POST":
                route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
                return
            route.fallback()

        page.route("**/api/**", handle_api)
        page.goto(f"{BASE_URL}/app", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        page.locator("#template-btn").click()
        page.wait_for_selector("#template-modal:not([hidden])")
        expect(page.locator("#theme-popover #template-select")).to_have_count(0)
        expect(page.locator("#template-modal .template-card")).to_have_count(3)
        expect(page.locator("#template-search")).to_be_focused()
        assert_no_horizontal_overflow(page, "#template-modal")
        assert_inside_viewport(page, ["#template-modal .template-modal-card", "#template-search", "#template-cancel", "#template-apply"])
        assert_required_visible_text_not_clipped(page, ["#template-modal .template-card-title"])
        page.locator("#template-search").fill("ats")
        expect(page.locator("#template-modal .template-card")).to_have_count(1)
        expect(page.locator('#template-modal [data-template-card="ats-compact"]')).to_be_visible()
        expect(page.locator('#template-modal [data-template-card="ats-compact"]')).to_have_attribute("tabindex", "0")
        page.keyboard.press("Tab")
        expect(page.locator('#template-modal [data-template-card="ats-compact"]')).to_be_focused()
        page.keyboard.press("Space")
        expect(page.locator('#template-modal [data-template-card="ats-compact"]')).to_have_attribute("aria-checked", "true")
        expect(page.locator("#template-empty")).to_be_hidden()
        preview_count = len(preview_templates)
        page.locator('#template-modal [data-template-card="ats-compact"]').click()
        page.wait_for_timeout(150)
        assert len(preview_templates) == preview_count
        expect(page.locator("#template-selected")).to_contain_text("ATS Compact")
        page.locator("#template-apply").focus()
        page.keyboard.press("Tab")
        expect(page.locator("#template-close")).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(page.locator("#template-apply")).to_be_focused()
        with page.expect_response(lambda res: "/api/preview" in res.url and "template=ats-compact" in res.url):
            page.locator("#template-apply").click()
        assert "ats-compact" in preview_templates

        page.locator("#template-btn").click()
        page.wait_for_selector("#template-modal:not([hidden])")
        page.locator("#template-search").fill("zzzz")
        expect(page.locator("#template-modal .template-card")).to_have_count(0)
        expect(page.locator("#template-empty")).to_be_visible()
        assert_required_visible_text_not_clipped(page, ["#template-empty"])
        page.keyboard.press("Escape")
        expect(page.locator("#template-modal")).to_be_hidden()
        expect(page.locator("#template-btn")).to_be_focused()
        page.locator("#template-btn").click()
        page.wait_for_selector("#template-modal:not([hidden])")
        page.locator("#template-search").fill("tech")
        expect(page.locator('#template-modal [data-template-card="technical"]')).to_be_visible()
        with page.expect_response(lambda res: "/api/preview" in res.url and "template=technical" in res.url):
            page.locator('#template-modal [data-template-card="technical"]').click()
            page.locator("#template-apply").click()
        assert "technical" in preview_templates
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_template_gallery_localizes_german_cards_and_radiogroup(browser):
    context = browser.new_context(
        device_scale_factor=1,
        storage_state=auth_storage_state(browser),
        viewport={"width": 390, "height": 844},
    )
    page = context.new_page()
    install_layout_vendor_routes(page)
    console_errors, page_errors = console_collector(page)
    try:
        def handle_api(route):
            parsed = urlparse(route.request.url)
            if parsed.path == "/api/templates":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"templates": [
                        {"id": "classic", "label": "Classic", "use_case": "Academic and conservative applications", "renderer": "standard", "layout_family": "single_column", "column_count": 1, "ats_priority": "high", "customization_axes": ["accent", "font"], "paper_sizes": ["A4"], "one_page_risk": "normal"},
                        {"id": "technical", "label": "Technical", "use_case": "Engineering and data roles", "renderer": "standard", "layout_family": "compact", "column_count": 1, "ats_priority": "high", "customization_axes": ["accent", "font", "density"], "paper_sizes": ["A4"], "one_page_risk": "low"},
                    ]}),
                )
                return
            if parsed.path == "/api/preview":
                route.fulfill(status=200, content_type="text/html", body="<main class='page'>Preview</main>")
                return
            if parsed.path == "/api/cv" and route.request.method == "GET":
                route.fulfill(
                    status=200,
                    content_type="text/yaml",
                    body='name: Template Test\ntemplate: classic\nfont: sans\ndensity: normal\naccent: "#111111"\ncontact: []\nexperience: []\neducation: []\nskills: []\nprojects: []\nleadership: []\nothers: []\n',
                )
                return
            route.fallback()

        page.route("**/api/**", handle_api)
        page.goto(f"{BASE_URL}/app", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        page.evaluate("() => document.querySelector(\"#lang-toggle .lang-btn[data-lang='de']\")?.click()")
        page.wait_for_function("() => document.documentElement.lang === 'de'")
        page.locator("#template-btn").click()
        page.wait_for_selector("#template-modal:not([hidden])")
        expect(page.locator("#template-gallery")).to_have_attribute("aria-label", "Vorlagen")
        expect(page.locator('[data-template-card="classic"] .template-card-title')).to_have_text("Klassisch")
        expect(page.locator('[data-template-card="classic"] .template-card-use')).to_have_text("Akademische und konservative Bewerbungen")
        aria = page.locator('[data-template-card="classic"]').get_attribute("aria-label") or ""
        assert "Vorlage Klassisch auswählen" in aria
        assert "Academic and conservative applications" not in aria
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_unsaved_accent_change_updates_preview_before_cv_save(browser):
    context = browser.new_context(
        device_scale_factor=1,
        storage_state=auth_storage_state(browser),
        viewport={"width": 1440, "height": 900},
    )
    page = context.new_page()
    install_editor_storage_reset(page)
    install_layout_vendor_routes(page)
    preview_accents: list[str] = []
    saved_bodies: list[str] = []
    console_errors, page_errors = console_collector(page)
    try:
        def handle_api(route):
            parsed = urlparse(route.request.url)
            if parsed.path == "/api/cvs":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"cvs": [{"id": CATALOG_CV_ID, "name": "Accent Test", "updated_at": "2026-07-04T09:00:00Z"}], "count": 1}),
                )
                return
            if parsed.path == "/api/templates":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"templates": [
                        {"id": "classic", "label": "Classic", "use_case": "Conservative roles", "renderer": "standard", "layout_family": "single_column", "column_count": 1, "ats_priority": "high", "customization_axes": ["accent", "font"], "paper_sizes": ["A4"], "one_page_risk": "normal"},
                    ]}),
                )
                return
            if parsed.path == "/api/preview":
                accent = parse_qs(parsed.query).get("accent", ["#111111"])[0]
                preview_accents.append(accent)
                route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=(
                        "<main class='page' data-accent='{0}' style='--accent: {0};'>"
                        "<h1 class='section-title'>Preview</h1></main>"
                    ).format(accent),
                )
                return
            if parsed.path == "/api/cv" and route.request.method == "GET":
                route.fulfill(
                    status=200,
                    headers={"X-OnePager-Revision": "rev-accent"},
                    content_type="text/yaml",
                    body=(
                        'name: Accent Test\n'
                        'accent: "#111111"\n'
                        'template: classic\n'
                        'font: sans\n'
                        'density: normal\n'
                        'contact:\n'
                        '  - label: Email\n'
                        '    value: alex@example.com\n'
                        'experience:\n'
                        '  - role: Staff Engineer\n'
                        '    company: Acme GmbH\n'
                        '    location: Berlin\n'
                        '    start: "2021"\n'
                        '    end: Present\n'
                        '    bullets:\n'
                        '      - Built reliable data platforms.\n'
                        'education:\n'
                        '  - degree: M.Sc. Computer Science\n'
                        '    school: FAU Erlangen-Nuremberg\n'
                        '    start: "2018"\n'
                        '    end: "2020"\n'
                        'skills:\n'
                        '  - label: Data\n'
                        '    items: Spark, Airflow\n'
                        'projects: []\n'
                        'leadership: []\n'
                        'others: []\n'
                    ),
                )
                return
            if parsed.path == "/api/cv" and route.request.method == "POST":
                saved_bodies.append(route.request.post_data or "")
                route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
                return
            route.fallback()

        page.route("**/api/**", handle_api)
        page.goto(f"{BASE_URL}/app?id={CATALOG_CV_ID}", wait_until="domcontentloaded")
        page.wait_for_selector("#accent-input", state="attached")
        with page.expect_response(lambda res: "/api/preview" in res.url and "accent=%232266aa" in res.url):
            page.locator("#accent-input").evaluate(
                """(node) => {
                    node.value = "#2266aa";
                    node.dispatchEvent(new Event("input", { bubbles: true }));
                }"""
            )
        preview_page = page.frame_locator("#preview").locator("main.page")
        expect(preview_page).to_have_attribute("data-accent", "#2266aa")
        assert preview_page.evaluate(
            """pageEl =>
              getComputedStyle(pageEl).getPropertyValue("--accent").trim()
            """
        ) == "#2266aa"
        assert "#2266aa" in preview_accents
        assert saved_bodies == []
        with page.expect_response(lambda res: urlparse(res.url).path == "/api/cv" and res.request.method == "POST"):
            page.keyboard.press("Meta+s")
        assert saved_bodies
        assert "#2266aa" in saved_bodies[-1]
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_sidebar_library_exposes_storage_destinations(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(1440, 900))
        page.wait_for_selector("#sidebar-library")
        page.wait_for_function(
            """
            () => {
              const zone = document.querySelector("#sidebar-library");
              return !!(
                zone &&
                (zone.querySelector(".sidebar-library-row") || zone.querySelector(".sidebar-library-empty"))
              );
            }
            """,
            timeout=10000,
        )
        library = page.evaluate(
            """
            () => {
              const zone = document.querySelector("#sidebar-library");
              const rows = Array.from(zone.querySelectorAll(".sidebar-library-row")).map((row) => ({
                kind: row.dataset.libraryKind,
                href: row.getAttribute("href"),
                title: row.getAttribute("title"),
                aria: row.getAttribute("aria-label"),
                text: (row.textContent || "").replace(/\\s+/g, " ").trim(),
              }));
              return {
                title: (zone.querySelector(".sidebar-zone-title")?.textContent || "").trim(),
                rows,
                empty: !!zone.querySelector(".sidebar-library-empty"),
              };
            }
            """
        )
        assert library["title"] == "Library"
        assert library["rows"] or library["empty"] is True
        counts = {}
        for row in library["rows"]:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
            assert row["title"]
            assert row["aria"]
            if row["kind"] == "cv":
                assert row["href"].startswith("/app?id=")
            elif row["kind"] == "cover_letter":
                assert row["href"].startswith("/cover-letters?id=")
            elif row["kind"] == "job":
                assert row["href"].startswith("/jobs?id=")
            elif row["kind"] == "catalog":
                assert row["href"] == "#"
        assert counts.get("cv", 0) <= 3
        assert counts.get("cover_letter", 0) <= 3
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
            context.close()


def test_settings_job_sources_status_and_defaults_are_browser_only(browser, tmp_path):
    context = browser.new_context(
        device_scale_factor=1,
        storage_state=auth_storage_state(browser),
        viewport={"width": 820, "height": 800},
        accept_downloads=True,
    )
    page = context.new_page()
    install_layout_vendor_routes(page)
    console_errors, page_errors = console_collector(page)
    import_preview_bodies = []
    try:
        page.route("**/api/jobs/sources/status", lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "sources": {
                    "manual": {"enabled": True, "supports_preview": True},
                    "linkedin": {
                        "enabled": True,
                        "adapter_configured": False,
                        "local_db_configured": True,
                        "local_db_available": True,
                        "latest_seen_at": "2026-06-16",
                        "age_days": 15,
                        "stale": True,
                        "row_count": 103774,
                        "warnings": ["no_cookies", "no_stealth", "manual_review_required", "local_db_stale"],
                    },
                },
                "policy": {
                    "no_stored_cookies": True,
                    "no_blind_linkedin_fetch": True,
                    "no_stealth_automation": True,
                    "operator_controlled_sources": True,
                    "local_db_read_only": True,
                },
            }),
        ))
        page.route("**/api/jobs", lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "jobs": [{
                    "id": CATALOG_JOB_ID,
                    "title": "Data Engineer",
                    "company": "Acme GmbH",
                    "status": "open",
                    "field": "Data / ML",
                    "job_family": "Data / ML",
                    "source_country": "DE",
                    "source_city": "Berlin",
                    "source_region": "Berlin",
                    "source_location": "Berlin, Germany",
                    "remote_mode": "remote",
                    "seniority": "senior",
                    "verification_status": "needs_review",
                    "experience_fit": {"required_years": 5, "candidate_years": 4.5},
                    "jd_text": "5+ years required. Spark and Airflow.",
                }],
                "count": 1,
            }),
        ))
        def handle_import_run(route):
            body = json.loads(route.request.post_data or "{}")
            import_preview_bodies.append(body)
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps({
                    "id": "55555555-5555-4555-8555-555555555555",
                    "results": [{
                        "id": "66666666-6666-4666-8666-666666666666",
                        "title": body["postings"][0]["title"] or "Data Engineer",
                        "company": "Manual Import",
                        "source_provider": body["provider"],
                        "verification_status": "user_provided",
                    }],
                }),
            )

        page.route("**/api/jobs/import-runs", handle_import_run)
        goto_route(page, "/settings", Viewport(820, 800))
        page.wait_for_selector("#job-sources")
        page.locator("#ac-theme-dark").click()
        page.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'dark'")
        assert page.locator("#ac-theme-dark").get_attribute("aria-pressed") == "true"
        page.locator("#ac-theme-light").click()
        page.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'light'")
        assert page.locator("#ac-theme-light").get_attribute("aria-pressed") == "true"
        page.locator("#ac-default-accent").evaluate("(node) => { node.value = '#224466'; node.dispatchEvent(new Event('input', { bubbles: true })); }")
        page.locator("#ac-default-font").select_option("mono")
        page.locator("#ac-default-density").select_option("tight")
        page.locator("#ac-default-template").select_option("technical")
        page.locator("#ac-default-paper").select_option("creme")
        page.locator("#ac-save-defaults").click()
        new_cv_defaults = page.evaluate("JSON.parse(localStorage.getItem('cv.editor.new_cv_defaults'))")
        assert new_cv_defaults == {
            "accent": "#224466",
            "font": "mono",
            "density": "tight",
            "template": "technical",
            "paper": "creme",
        }
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#job-sources")
        expect(page.locator("#ac-default-accent")).to_have_value("#224466")
        expect(page.locator("#ac-default-font")).to_have_value("mono")
        expect(page.locator("#ac-default-density")).to_have_value("tight")
        expect(page.locator("#ac-default-template")).to_have_value("technical")
        expect(page.locator("#ac-default-paper")).to_have_value("creme")
        expect(page.locator("#ac-job-source-status")).to_contain_text("Local jobs DB available")
        expect(page.locator("#ac-job-source-status")).to_contain_text("2026-06-16")
        expect(page.locator("#job-sources")).not_to_contain_text("li_at")
        expect(page.locator('#job-sources input[type="password"]')).to_have_count(0)
        page.locator("#ac-job-source-source").select_option("all")
        page.locator("#ac-job-source-title").fill("Data Engineer")
        page.locator("#ac-job-profile-family").fill("Data / ML")
        page.locator("#ac-job-profile-titles").fill("Data Engineer, Analytics Engineer")
        page.locator("#ac-job-profile-mode").select_option("strict")
        page.locator("#ac-job-source-country").fill("DE")
        page.locator("#ac-job-source-region").fill("Berlin")
        page.locator("#ac-job-profile-cities").fill("Berlin, Warsaw")
        page.locator("#ac-job-source-remote").select_option("remote")
        page.locator("#ac-job-source-seniority").fill("senior")
        page.locator("#ac-job-profile-min-years").fill("5")
        page.locator("#ac-job-profile-ranking").fill("title=.35, location=.25, freshness=.2, experience=.2")
        page.locator("#ac-save-job-sources").click()
        stored = page.evaluate("JSON.parse(localStorage.getItem('cv.editor.job_source_defaults'))")
        assert stored["schema"] == "onepager.job_profile.v1"
        assert stored["source"] == "all"
        assert stored["title"] == "Data Engineer"
        assert stored["family"] == "Data / ML"
        assert stored["titles"] == ["Data Engineer", "Analytics Engineer"]
        assert stored["search_mode"] == "strict"
        assert stored["country"] == "DE"
        assert stored["region"] == "Berlin"
        assert stored["cities"] == ["Berlin", "Warsaw"]
        assert stored["remote_mode"] == "remote"
        assert stored["seniority"] == "senior"
        assert stored["min_years"] == "5"
        assert stored["ranking"]["title"] == 0.35
        with page.expect_download() as download_info:
            page.locator("#ac-export-job-profile").click()
        download = download_info.value
        assert download.suggested_filename == "onepager-job-profile.json"
        exported_path = tmp_path / "onepager-job-profile.json"
        download.save_as(exported_path)
        exported_profile = json.loads(exported_path.read_text())
        assert exported_profile == stored
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#job-sources")
        expect(page.locator("#ac-job-profile-mode")).to_have_value("strict")
        expect(page.locator("#ac-job-source-title")).to_have_value("Data Engineer")
        expect(page.locator("#ac-job-profile-cities")).to_have_value("Berlin, Warsaw")
        page.locator("#ac-reset-job-sources").click()
        assert page.evaluate("localStorage.getItem('cv.editor.job_source_defaults')") is None
        expect(page.locator("#ac-job-source-title")).to_have_value("")
        goto_route(page, "/jobs", Viewport(820, 800))
        page.wait_for_selector(".jb-search-sidebar")
        expect(page.locator("#jb-search")).to_have_value("")
        expect(page.locator("#jb-search-mode")).to_have_value("flexible")
        expect(page.locator("#jb-filter-family")).to_have_value("")
        goto_route(page, "/settings", Viewport(820, 800))
        page.wait_for_selector("#job-sources")
        page.once("dialog", lambda dialog: dialog.accept(json.dumps(exported_profile)))
        page.locator("#ac-import-job-profile").click()
        expect(page.locator("#ac-job-profile-mode")).to_have_value("strict")
        expect(page.locator("#ac-job-source-title")).to_have_value("Data Engineer")
        expect(page.locator("#ac-job-profile-cities")).to_have_value("Berlin, Warsaw")
        goto_route(page, "/jobs", Viewport(820, 800))
        page.wait_for_selector(".jb-search-sidebar")
        expect(page.locator("#jb-search")).to_have_value("Data Engineer")
        expect(page.locator("#jb-import-source")).to_have_value("all")
        expect(page.locator("#jb-filter-provider")).to_have_value("")
        expect(page.locator("#jb-search-mode")).to_have_value("strict")
        expect(page.locator("#jb-filter-family")).to_have_value("Data / ML")
        expect(page.locator("#jb-filter-country")).to_have_value("DE")
        expect(page.locator("#jb-filter-city")).to_have_value("Berlin, Warsaw")
        expect(page.locator("#jb-filter-min-years")).to_have_value("5")
        page.locator("#jb-import-details").evaluate("node => node.open = true")
        page.locator("#jb-import-jd").fill("Title: Data Engineer\\nCompany: Manual Import\\nBuild data products.")
        page.locator("#jb-import-preview").click()
        expect(page.locator(".jb-import-row")).to_have_count(1)
        assert import_preview_bodies[0]["provider"] == "manual"
        assert import_preview_bodies[0]["filters"]["source"] == "all"
        page.locator("#jb-import-source").select_option("")
        page.locator("#jb-import-preview").click()
        expect(page.locator(".jb-import-row")).to_have_count(1)
        assert import_preview_bodies[-1]["provider"] == "manual"
        assert import_preview_bodies[-1]["filters"]["source"] == ""
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector(".jb-search-sidebar")
        expect(page.locator("#jb-search-mode")).to_have_value("strict")
        expect(page.locator("#jb-filter-family")).to_have_value("Data / ML")
        expect(page.locator("#jb-filter-city")).to_have_value("Berlin, Warsaw")
        expect(page.locator("#jb-grid")).to_contain_text("Data Engineer")
        page.evaluate(
            """
            () => localStorage.setItem("cv.editor.job_source_defaults", JSON.stringify({
              schema: "onepager.job_profile.v1",
              source: "",
              title: "Data Engineer",
              search_mode: "strict"
            }))
            """
        )
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector(".jb-search-sidebar")
        expect(page.locator("#jb-import-source")).to_have_value("")
        expect(page.locator("#jb-filter-provider")).to_have_value("")
        expect(page.locator("#jb-grid")).to_contain_text("Data Engineer")
        page.evaluate(
            """
            () => localStorage.setItem("cv.editor.job_source_defaults", JSON.stringify({
              schema: "onepager.job_profile.v1",
              title: "Data Engineer",
              search_mode: "strict"
            }))
            """
        )
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector(".jb-search-sidebar")
        expect(page.locator("#jb-import-source")).to_have_value("manual")
        expect(page.locator("#jb-filter-provider")).to_have_value("")
        expect(page.locator("#jb-grid")).to_contain_text("Data Engineer")
        page.evaluate(
            """
            () => localStorage.setItem("cv.editor.job_source_defaults", JSON.stringify({
              schema: "onepager.job_profile.v1",
              source: "bad",
              title: "Data Engineer",
              search_mode: "strict"
            }))
            """
        )
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector(".jb-search-sidebar")
        expect(page.locator("#jb-import-source")).to_have_value("manual")
        expect(page.locator("#jb-filter-provider")).to_have_value("")
        expect(page.locator("#jb-grid")).to_contain_text("Data Engineer")
        goto_route(page, "/settings", Viewport(820, 800))
        page.wait_for_selector("#job-sources")
        expect(page.locator("#ac-job-source-source")).to_have_value("")
        assert_no_horizontal_overflow(page)
        assert_no_unexpected_visible_overflow(page)
        assert_no_text_overflow(page, ["#ac-job-source-status", ".jb-search-sidebar .cl-label"])
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize(
    "viewport",
    [
        Viewport(1440, 900),
        Viewport(1280, 800),
        Viewport(1100, 760),
        Viewport(820, 800),
        Viewport(760, 800),
        Viewport(390, 844),
        Viewport(320, 740),
    ],
    ids=lambda v: v.label,
)
@pytest.mark.parametrize("lang", ["en", "de"])
def test_jobs_filter_sidebar_layout_with_long_titles_across_viewports(browser, viewport: Viewport, lang: str):
    context, page, console_errors, page_errors = new_page(browser)
    long_title = "Principal Machine Learning Platform Engineer for Applied Forecasting and Decision Intelligence"
    jobs = [
        {
            "id": CATALOG_JOB_ID,
            "title": long_title,
            "company": "Acme GmbH",
            "status": "open",
            "field": "Data / ML",
            "job_family": "Data and machine learning",
            "preferred_titles": ["Machine Learning Engineer", "Data Scientist"],
            "search_mode": "flexible",
            "source_provider": "manual",
            "source_country": "DE",
            "source_city": "Berlin",
            "source_region": "Berlin",
            "source_location": "Berlin, Germany",
            "remote_mode": "hybrid",
            "seniority": "senior",
            "verification_status": "needs_review",
            "experience_fit": {
                "required_years": 5,
                "candidate_years": 4.5,
                "fit": "near",
                "evidence": ["5+ years required", "2021-2026 candidate evidence"],
                "uncertain": True,
            },
            "last_seen_at": "2099-07-01T10:00:00Z",
            "jd_text": "Title: Principal Machine Learning Platform Engineer\nCompany: Acme GmbH\n5+ years required. Python and forecasting.",
        },
        {
            "id": "30000000-0000-4000-8000-000000000002",
            "title": "Support Engineer",
            "company": "Beta GmbH",
            "status": "saved",
            "field": "Support",
            "job_family": "Operations",
            "source_provider": "other",
            "source_country": "US",
            "source_city": "Austin",
            "source_location": "Austin, Texas",
            "remote_mode": "onsite",
            "seniority": "junior",
            "verification_status": "stale",
            "jd_text": "Support users.",
        },
    ]
    try:
        add_top_document_init_script(
            page,
            f"localStorage.setItem('onepager.lang', '{lang}')",
        )

        def handle_api(route):
            path = urlparse(route.request.url).path
            if path == "/api/jobs" and route.request.method == "GET":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"jobs": jobs, "count": len(jobs)}))
                return
            if path in {f"/api/jobs/{item['id']}" for item in jobs} and route.request.method == "GET":
                job_id = path.rsplit("/", 1)[-1]
                job = next((item for item in jobs if item["id"] == job_id), jobs[0])
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"job": job, "cvs": [], "cover_letters": []}))
                return
            route.fallback()

        page.route("**/api/**", handle_api)
        goto_route(page, f"/jobs?id={CATALOG_JOB_ID}", viewport)
        page.wait_for_function(f"() => document.documentElement.lang === '{lang}'")
        page.wait_for_selector(".jb-search-sidebar")
        expect(page.locator("#jb-search-mode")).to_be_visible()
        expect(page.locator("#jb-filter-family")).to_be_visible()
        expect(page.locator("#jb-filter-city")).to_be_visible()
        expect(page.locator("#jb-filter-min-years")).to_be_visible()
        expect(page.locator("#jb-grid")).to_contain_text("Prüfung nötig" if lang == "de" else "Needs review")
        expect(page.locator("#jb-grid")).to_contain_text("Jahre" if lang == "de" else "years")
        page.locator("#jb-search").focus()
        reached_card = False
        for _ in range(90):
            page.keyboard.press("Tab")
            if page.locator(".jb-card-detail").first.evaluate("(el) => document.activeElement === el"):
                reached_card = True
                break
        assert reached_card
        page.keyboard.press("Enter")
        expect(page.locator("#jb-detail")).to_be_visible()
        expect(page.locator("#jb-detail-title")).to_contain_text(long_title)
        assert_no_horizontal_overflow(page)
        assert_no_horizontal_overflow(page, ".jb-main")
        assert_no_horizontal_overflow(page, ".jb-saved-filters")
        if viewport.width >= 1000:
            assert_inside_viewport(page, [".jb-search-sidebar", ".jb-results-panel"])
        assert_required_visible_text_not_clipped(page, [
            ".jb-search-sidebar .cl-label",
            'label[for="jb-filter-verification"]',
            ".jb-card-title",
            ".jb-pill",
        ])
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_document_catalog_german_labels_and_accessible_names(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        add_top_document_init_script(
            page,
            "localStorage.setItem('onepager.lang', 'de')",
        )
        goto_route(page, "/app", Viewport(1440, 900))
        page.wait_for_function("() => document.documentElement.lang === 'de'")
        page.wait_for_selector("#cv-switcher")
        label = page.locator("#cv-switcher-label").inner_text().strip()
        aria = page.locator("#cv-switcher").get_attribute("aria-label") or ""
        tooltip = page.locator("#cv-switcher").get_attribute("data-tooltip") or ""
        assert label == "Dokumentkatalog"
        assert aria.startswith("Dokumentkatalog öffnen:")
        assert "Dokumentkatalog öffnen" in tooltip
        page.locator("#cv-switcher").click()
        page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        page.wait_for_selector("#cv-switcher-pop .cv-catalog-row")
        catalog = page.evaluate(
            """
            () => {
              const pop = document.querySelector("#cv-switcher-pop");
              return {
                title: (pop.querySelector("#cv-catalog-title")?.textContent || "").trim(),
                filters: Array.from(pop.querySelectorAll("[data-doc-filter]")).map((btn) => (btn.querySelector("[data-i18n]")?.textContent || "").trim()),
                counts: Array.from(pop.querySelectorAll("[data-doc-count]")).map((count) => count.textContent.trim()),
                footer: (pop.querySelector(".cv-catalog-foot")?.textContent || "").replace(/\\s+/g, " ").trim(),
                close: pop.querySelector("#cv-catalog-close")?.getAttribute("aria-label") || "",
                width: pop.querySelector(".cv-catalog-card").getBoundingClientRect().width,
              };
            }
            """
        )
        assert catalog["title"] == "Dokumentkatalog"
        assert {"Alle", "Lebensläufe", "Anschreiben"}.issubset(set(catalog["filters"]))
        assert all(value.isdigit() for value in catalog["counts"])
        assert "Neuer Lebenslauf" in catalog["footer"]
        assert catalog["close"] == "Dokumentkatalog schließen"
        assert catalog["width"] >= 640
        assert_no_horizontal_overflow(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_account_new_cv_handoff_runs_typed_editor_action(browser):
    context = browser.new_context(
        device_scale_factor=1,
        storage_state=auth_storage_state(browser),
    )
    page = context.new_page()
    install_layout_vendor_routes(page)
    add_top_document_init_script(
        page,
        """
        (() => {
          for (const key of Object.keys(window.localStorage)) {
            if (key.startsWith("cv.editor.") && !key.includes("supabase")) {
              window.localStorage.removeItem(key);
            }
          }
          window.localStorage.setItem("cv.editor.tourCompleted", "true");
          window.localStorage.setItem("cv.editor.theme", "light");
          window.localStorage.setItem("cv.editor.new_cv_defaults", JSON.stringify({
            accent: "#224466",
            font: "mono",
            density: "tight",
            template: "technical",
            paper: "creme"
          }));
          window.localStorage.setItem("onepager.lang", "en");
        })();
        """
    )
    console_errors, page_errors = console_collector(page)
    created_id = "20000000-0000-4000-8000-000000000001"
    calls = {"create": 0, "load_created": 0}
    saved_contents: list[dict] = []

    base_yaml = (
        "name: Existing CV\n"
        "role: Data Engineer\n"
        "accent: '#1f6f43'\n"
        "font: serif\n"
        "density: normal\n"
        "summary: Existing\n"
    )
    created_yaml = base_yaml.replace("Existing CV", "Created from account handoff")

    def handle_api(route):
        url = route.request.url
        path = "/" + url.split("/", 3)[3].split("?", 1)[0] if "://" in url else url
        if path == "/api/cv" and route.request.method == "GET":
            if created_id in url:
                calls["load_created"] += 1
                body = created_yaml
            else:
                body = base_yaml
            route.fulfill(status=200, headers={"X-OnePager-Revision": "rev"}, content_type="text/plain; charset=utf-8", body=body)
            return
        if path == "/api/preview" and route.request.method == "GET":
            # The simulated created id does not exist server-side; the real
            # server correctly 404s explicit unknown ids. Stub the preview so
            # the editor's refreshPreview does not log a console error.
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body="<!doctype html><html><body><main></main></body></html>",
            )
            return
        if path == "/api/cv" and route.request.method == "POST":
            payload = json.loads(route.request.post_data or "{}")
            saved_contents.append(payload)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "id": payload.get("id"),
                    "updated_at": "rev-defaults",
                    "bytes": len((payload.get("content") or "").encode("utf-8")),
                }),
            )
            return
        if path == "/api/cvs" and route.request.method == "POST":
            calls["create"] += 1
            route.fulfill(
                status=201,
                content_type="application/json",
                body=json.dumps({"id": created_id, "name": "Untitled CV", "updated_at": "2026-06-30T20:00:00Z"}),
            )
            return
        if path == "/api/cvs" and route.request.method == "GET":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"cvs": [{"id": created_id, "name": "Untitled CV", "updated_at": "2026-06-30T20:00:00Z"}]}),
            )
            return
        if path == "/api/documents":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "documents": [
                            {
                                "id": created_id,
                                "type": "cv",
                                "name": "Untitled CV",
                                "updated_at": "2026-06-30T20:00:00Z",
                                "last_used_at": "2026-06-30T20:00:00Z",
                                "open_url": f"/app?id={created_id}",
                                "actions": ["open", "download_yaml"],
                            }
                        ],
                        "count": 1,
                    }
                ),
            )
            return
        route.fallback()

    try:
        page.route("**/api/**", handle_api)
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(f"{BASE_URL}/app?action=new-cv", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        page.wait_for_function(
            """(id) => new URL(location.href).searchParams.get("id") === id && !new URL(location.href).searchParams.has("action")""",
            arg=created_id,
        )
        page.wait_for_function("""() => document.querySelector("#status")?.textContent.includes("Ready") || document.querySelector("#status")?.textContent.includes("Saved")""")
        assert calls["create"] == 1
        assert calls["load_created"] >= 1
        assert len(saved_contents) == 1
        assert saved_contents[0]["id"] == created_id
        saved_yaml = saved_contents[0]["content"]
        assert "name: Created from account handoff" in saved_yaml
        assert "accent: '#224466'" in saved_yaml or 'accent: "#224466"' in saved_yaml
        assert "font: mono" in saved_yaml
        assert "density: tight" in saved_yaml
        assert "template: technical" in saved_yaml
        assert "paper: creme" in saved_yaml
        assert "name: Existing CV" not in saved_yaml
        active_label = page.locator("#cv-switcher").get_attribute("aria-label") or ""
        assert "Untitled CV" in active_label
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_sidebar_library_caps_and_catalog_preserves_long_document_names(browser):
    context, page, console_errors, page_errors = new_page(browser)
    current_cv_id = "00000000-0000-4000-8000-000000000001"
    older_cv_id = "00000000-0000-4000-8000-000000000004"
    opened = {"older": False}

    long_cv_name = (
        "Extremely long enterprise transformation curriculum vitae for a principal platform "
        "architect candidate with governance and migration evidence"
    )
    long_letter_name = (
        "Extremely long cover letter for the senior data platform leadership application "
        "linked to the Berlin analytics operating model"
    )

    def cv_rows() -> list[dict]:
        older_last_used = "2026-06-30T20:00:00Z" if opened["older"] else "2026-06-20T08:00:00Z"
        return [
            {
                "id": current_cv_id,
                "type": "cv",
                "name": "Current CV",
                "updated_at": "2026-06-30T10:00:00Z",
                "last_used_at": "2026-06-30T10:00:00Z",
                "open_url": f"/app?id={current_cv_id}",
                "actions": ["open", "download_yaml"],
            },
            {
                "id": "00000000-0000-4000-8000-000000000002",
                "type": "cv",
                "name": long_cv_name,
                "updated_at": "2026-06-29T10:00:00Z",
                "last_used_at": "2026-06-29T10:00:00Z",
                "open_url": "/app?id=00000000-0000-4000-8000-000000000002",
                "actions": ["open", "download_yaml"],
            },
            {
                "id": "00000000-0000-4000-8000-000000000003",
                "type": "cv",
                "name": "Recent CV Three",
                "updated_at": "2026-06-28T10:00:00Z",
                "last_used_at": "2026-06-28T10:00:00Z",
                "open_url": "/app?id=00000000-0000-4000-8000-000000000003",
                "actions": ["open", "download_yaml"],
            },
            {
                "id": older_cv_id,
                "type": "cv",
                "name": "Older unopened CV that should move into the top three after open",
                "updated_at": "2026-06-27T10:00:00Z",
                "last_used_at": older_last_used,
                "open_url": f"/app?id={older_cv_id}",
                "actions": ["open", "download_yaml"],
            },
            {
                "id": "00000000-0000-4000-8000-000000000005",
                "type": "cv",
                "name": "Hidden CV Five",
                "updated_at": "2026-06-26T10:00:00Z",
                "last_used_at": "2026-06-26T10:00:00Z",
                "open_url": "/app?id=00000000-0000-4000-8000-000000000005",
                "actions": ["open", "download_yaml"],
            },
        ]

    letters = [
        {
            "id": f"10000000-0000-4000-8000-00000000000{i}",
            "type": "cover_letter",
            "name": long_letter_name if i == 5 else f"Cover letter {i}",
            "company": "Acme GmbH" if i == 5 else "",
            "role": "Senior Data Engineer" if i == 5 else "",
            "job_id": "30000000-0000-4000-8000-000000000001" if i == 5 else None,
            "job_label": "Senior Data Engineer at Acme GmbH" if i == 5 else "",
            "updated_at": f"2026-06-2{i}T10:00:00Z",
            "last_used_at": f"2026-06-2{i}T10:00:00Z",
            "open_url": f"/cover-letters?id=10000000-0000-4000-8000-00000000000{i}",
            "actions": ["open", "build_pdf"],
        }
        for i in range(1, 6)
    ]

    def handle_api(route):
        url = route.request.url
        path = "/" + url.split("/", 3)[3].split("?", 1)[0] if "://" in url else url
        if path == "/api/cv" and route.request.method == "GET":
            if older_cv_id in url:
                opened["older"] = True
            route.fulfill(
                status=200,
                content_type="text/plain; charset=utf-8",
                body=(
                    "name: Stub CV\n"
                    "role: Principal Platform Architect\n"
                    "accent: '#1f6f43'\n"
                    "font: serif\n"
                    "density: normal\n"
                    "summary: Stub\n"
                ),
            )
            return
        if path == "/api/preview" and route.request.method == "GET":
            # Simulated ids don't exist server-side; the real server correctly
            # 404s explicit unknown ids. Stub the preview to keep the console
            # clean for assert_clean_console.
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body="<!doctype html><html><body><main></main></body></html>",
            )
            return
        if path == "/api/documents":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"documents": cv_rows() + letters, "count": 10}),
            )
            return
        route.fallback()

    try:
        page.route("**/api/**", handle_api)
        goto_route(page, "/app", Viewport(1440, 900))
        page.wait_for_selector("#sidebar-library .sidebar-library-row")
        first_library = page.evaluate(
            """
            () => Array.from(document.querySelectorAll("#sidebar-library .sidebar-library-row")).map((row) => ({
              kind: row.dataset.libraryKind,
              href: row.getAttribute("href"),
              title: row.getAttribute("title"),
              aria: row.getAttribute("aria-label"),
              text: (row.textContent || "").replace(/\\s+/g, " ").trim(),
            }))
            """
        )
        cv_rows_before = [row for row in first_library if row["kind"] == "cv"]
        letter_rows_before = [row for row in first_library if row["kind"] == "cover_letter"]
        assert len(cv_rows_before) == 3
        assert len(letter_rows_before) == 3
        assert all(row["title"] and row["aria"] for row in first_library)
        assert not any(older_cv_id in row["href"] for row in cv_rows_before)
        assert any(long_cv_name == row["title"] for row in cv_rows_before)
        assert any(long_letter_name == row["title"] for row in letter_rows_before)

        page.locator("#cv-switcher").click()
        page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        page.wait_for_selector("#cv-switcher-pop .cv-catalog-row[data-id]")
        catalog = page.evaluate(
            """
            () => Array.from(document.querySelectorAll("#cv-switcher-pop .cv-catalog-row[data-id]")).map((row) => ({
              id: row.dataset.id,
              title: row.getAttribute("title"),
              aria: row.getAttribute("aria-label"),
              text: (row.textContent || "").replace(/\\s+/g, " ").trim(),
              clipped: row.scrollWidth > row.clientWidth + 2,
            }))
            """
        )
        assert len(catalog) == 10
        assert any(row["title"] == long_cv_name and long_cv_name in row["aria"] for row in catalog)
        assert any(row["title"] == long_letter_name and long_letter_name in row["aria"] for row in catalog)
        assert any(row["title"] == long_letter_name and "Job artifact" in row["text"] for row in catalog)
        assert not any(row["clipped"] for row in catalog)

        page.locator(f'#cv-switcher-pop .cv-catalog-row[data-id="{older_cv_id}"] .cv-catalog-open').click()
        page.wait_for_url(f"**/app?id={older_cv_id}")
        page.wait_for_function(
            """(id) => Array.from(document.querySelectorAll("#sidebar-library .sidebar-library-row[data-library-kind='cv']")).some((row) => row.href.includes(id))""",
            arg=older_cv_id,
        )
        cv_rows_after = page.evaluate(
            """
            () => Array.from(document.querySelectorAll("#sidebar-library .sidebar-library-row[data-library-kind='cv']")).map((row) => row.getAttribute("href") || "")
            """
        )
        assert len(cv_rows_after) == 3
        assert any(older_cv_id in href for href in cv_rows_after)
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_document_catalog_german_long_names_mobile_no_clipping(browser):
    context, page, console_errors, page_errors, _calls = catalog_fixture_page(browser, viewport=Viewport(390, 844))
    long_name = "Lebenslauf fuer internationale Plattformarchitektur und datengetriebene Transformation"
    try:
        add_top_document_init_script(
            page,
            f"""
            localStorage.setItem('onepager.lang', 'de');
            window.__catalogLongName = {json.dumps(long_name)};
            """
        )
        page.route(
            "**/api/documents",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "documents": [
                        {
                            "id": CATALOG_CV_ID,
                            "type": "cv",
                            "name": long_name,
                            "updated_at": "2026-07-01T09:00:00Z",
                            "actions": ["open", "build_pdf", "rename", "duplicate", "delete", "download_yaml"],
                        },
                        {
                            "id": CATALOG_LETTER_ID,
                            "type": "cover_letter",
                            "name": "Sehr langes Anschreiben fuer die Senior Data Engineering Bewerbung",
                            "company": "Acme GmbH",
                            "role": "Senior Data Engineer",
                            "updated_at": "2026-06-29T09:00:00Z",
                            "actions": ["open", "build_pdf", "rename", "delete"],
                        },
                    ],
                    "count": 2,
                }),
            ),
        )
        goto_route(page, "/app", Viewport(390, 844))
        page.wait_for_function("() => document.documentElement.lang === 'de'")
        page.locator("#mobile-utility-btn").click()
        page.locator('[data-mobile-action="document-catalog"]').click()
        page.wait_for_selector("#cv-switcher-pop:not([hidden])")
        page.wait_for_selector("#cv-switcher-pop .cv-catalog-row[data-id]")
        assert_no_horizontal_overflow(page, "#cv-switcher-pop")
        assert_required_visible_text_not_clipped(page, ["#cv-switcher-pop .cv-name", "#cv-switcher-pop .cv-catalog-action"])
        rows = page.evaluate(
            """
            () => Array.from(document.querySelectorAll("#cv-switcher-pop .cv-catalog-row[data-id]")).map((row) => ({
              title: row.getAttribute("title") || "",
              aria: row.getAttribute("aria-label") || "",
              clipped: row.scrollWidth > row.clientWidth + 1,
            }))
            """
        )
        assert any(row["title"] == long_name and long_name in row["aria"] for row in rows)
        assert not any(row["clipped"] for row in rows)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_all_editor_sections_collapse_expand_keyboard_and_persist(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(1440, 900))
        wait_for_editor_shell_ready(page)
        page.wait_for_selector(".form-section-head.is-collapsible")
        page.locator("#collapse-all-btn").click()
        page.wait_for_function(
            """
            () => {
              const sections = Array.from(document.querySelectorAll(".form-section[data-section-key]"))
                .filter((sec) => sec.querySelector(".form-section-head.is-collapsible") && sec.querySelector(".form-section-body"));
              return sections.length > 0 && sections.every((sec) => sec.querySelector(".form-section-body").hidden);
            }
            """
        )
        key = page.evaluate(
            """
            () => Object.keys(localStorage)
              .find((item) => item.startsWith("cv.editor.collapsed::") && localStorage.getItem(item) && localStorage.getItem(item) !== "{}")
              ?.replace("cv.editor.collapsed::", "") || ""
            """
        )
        assert key
        stored = page.evaluate("(id) => localStorage.getItem('cv.editor.collapsed::' + id)", key)
        assert stored and stored != "{}"
        page.reload(wait_until="domcontentloaded")
        wait_for_editor_shell_ready(page)
        page.wait_for_function(
            """
            () => {
              const sections = Array.from(document.querySelectorAll(".form-section[data-section-key]"))
                .filter((sec) => sec.querySelector(".form-section-head.is-collapsible") && sec.querySelector(".form-section-body"));
              return sections.length > 0 && sections.every((sec) =>
                sec.querySelector(".form-section-body").hidden &&
                sec.querySelector(".form-section-head.is-collapsible").getAttribute("aria-expanded") === "false"
              );
            }
            """
        )
        page.keyboard.press("Meta+K")
        page.wait_for_selector("#cmdk:not([hidden])")
        page.locator(".cmdk-input").fill("expand all")
        page.keyboard.press("Enter")
        page.wait_for_function(
            """
            () => Array.from(document.querySelectorAll(".form-section[data-section-key]"))
              .filter((sec) => sec.querySelector(".form-section-head.is-collapsible") && sec.querySelector(".form-section-body"))
              .every((sec) => !sec.querySelector(".form-section-body").hidden)
            """
        )
        cleared = page.evaluate("(id) => localStorage.getItem('cv.editor.collapsed::' + id)", key)
        assert cleared is None
        btn_label = page.locator("#collapse-all-btn .collapse-all-label").inner_text().strip()
        assert btn_label == "Collapse all"
        page.keyboard.press("Meta+K")
        page.wait_for_selector("#cmdk:not([hidden])")
        page.locator(".cmdk-input").fill("collapse all")
        page.keyboard.press("Enter")
        page.wait_for_function(
            """
            () => document.querySelector("#collapse-all-btn")?.getAttribute("aria-pressed") === "true"
            """
        )
        page.locator(".form-section-head.is-collapsible").first.click()
        page.wait_for_function(
            """
            () => document.querySelector("#collapse-all-btn")?.getAttribute("aria-pressed") === "mixed"
            """
        )
        page.locator('.view-tab[data-view="yaml"]').click()
        expect(page.locator("#collapse-all-btn")).to_be_hidden()
        page.keyboard.press("Meta+K")
        page.wait_for_selector("#cmdk:not([hidden])")
        page.locator(".cmdk-input").fill("collapse all")
        expect(page.locator(".cmdk-item").filter(has_text=re.compile("Collapse all", re.I))).to_have_count(0)
        page.keyboard.press("Escape")
        page.locator('.view-tab[data-view="form"]').click()
        expect(page.locator("#collapse-all-btn")).to_be_visible()
        page.wait_for_function(
            """
            () => {
              const sections = Array.from(document.querySelectorAll(".form-section[data-section-key]"))
                .filter((sec) => sec.querySelector(".form-section-head.is-collapsible") && sec.querySelector(".form-section-body"));
              const hidden = sections.filter((sec) => sec.querySelector(".form-section-body").hidden).length;
              return sections.length > 1 && hidden > 0 && hidden < sections.length &&
                document.querySelector("#collapse-all-btn")?.getAttribute("aria-pressed") === "mixed";
            }
            """
        )
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(150)
        mobile_aria = page.evaluate(
            """
            () => {
              const btn = document.querySelector("#collapse-all-btn");
              const label = btn.querySelector(".collapse-all-label");
              const cs = getComputedStyle(label);
              const focusStyle = getComputedStyle(btn, ":focus-visible");
              btn.focus();
              return {
                aria: btn.getAttribute("aria-label") || "",
                tooltip: btn.getAttribute("data-tooltip") || "",
                pressed: btn.getAttribute("aria-pressed"),
                labelHidden: cs.display === "none" || cs.visibility === "hidden",
                outline: getComputedStyle(btn).outlineStyle || focusStyle.outlineStyle,
              };
            }
            """
        )
        assert mobile_aria["pressed"] == "mixed"
        assert mobile_aria["aria"]
        assert mobile_aria["tooltip"]
        assert mobile_aria["labelHidden"] is True
        expect(page.locator("#collapse-all-btn")).to_be_visible()
        page.locator("#collapse-all-btn").focus()
        assert page.evaluate("() => document.activeElement && document.activeElement.id") == "collapse-all-btn"
        page.locator("#collapse-all-btn").press("Enter")
        page.wait_for_function(
            """
            () => {
              const sections = Array.from(document.querySelectorAll(".form-section[data-section-key]"))
                .filter((sec) => sec.querySelector(".form-section-head.is-collapsible") && sec.querySelector(".form-section-body"));
              return sections.length > 0 && sections.every((sec) => sec.querySelector(".form-section-body").hidden) &&
                document.querySelector("#collapse-all-btn")?.getAttribute("aria-pressed") === "true";
            }
            """
        )
        page.locator("#collapse-all-btn").press("Space")
        page.wait_for_function(
            """
            () => {
              const sections = Array.from(document.querySelectorAll(".form-section[data-section-key]"))
                .filter((sec) => sec.querySelector(".form-section-head.is-collapsible") && sec.querySelector(".form-section-body"));
              return sections.length > 0 && sections.every((sec) => !sec.querySelector(".form-section-body").hidden) &&
                document.querySelector("#collapse-all-btn")?.getAttribute("aria-pressed") === "false";
            }
            """
        )
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(150)
        page.locator("#collapse-all-btn").click()
        page.wait_for_function(
            """
            () => document.querySelector("#collapse-all-btn")?.getAttribute("aria-pressed") === "true"
            """
        )
        page.evaluate("() => document.querySelector(\"#lang-toggle .lang-btn[data-lang='de']\")?.click()")
        page.wait_for_function("() => document.documentElement.lang === 'de'")
        de_label = page.locator("#collapse-all-btn .collapse-all-label").inner_text().strip()
        assert de_label == "Alle ausklappen"
        assert "ausklappen" in (page.locator("#collapse-all-btn").get_attribute("aria-label") or "").lower()
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_editor_focus_mode_navigates_one_section_and_overview_restores_all(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(1440, 900))
        wait_for_editor_shell_ready(page)
        total = page.locator("#form-host .form-section[data-section-key]").count()
        assert total > 1

        page.locator('.form-mode-btn[data-form-mode="focus"]').click()
        expect(page.locator("body")).to_have_attribute("data-form-mode", "focus")
        expect(page.locator('.form-mode-btn[data-form-mode="focus"]')).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#focus-section-nav")).to_be_visible()
        expect(page.locator("#form-host .form-section[data-section-key]:not([hidden])")).to_have_count(1)

        first_key = page.locator("#form-host .form-section[data-section-key]:not([hidden])").get_attribute("data-section-key")
        first_progress = int(page.locator("#focus-section-progress").get_attribute("aria-valuenow"))
        page.locator("#focus-section-next").click()
        expect(page.locator("#form-host .form-section[data-section-key]:not([hidden])")).to_have_count(1)
        second_key = page.locator("#form-host .form-section[data-section-key]:not([hidden])").get_attribute("data-section-key")
        second_progress = int(page.locator("#focus-section-progress").get_attribute("aria-valuenow"))
        assert second_key != first_key
        assert second_progress == first_progress + 1

        target_outline = page.locator(".outline-item[data-slug]").nth(3)
        target_key = target_outline.get_attribute("data-slug")
        target_outline.click()
        expect(page.locator(f'.form-section[data-section-key="{target_key}"]')).to_be_visible()
        expect(page.locator(f'.outline-item[data-slug="{target_key}"]')).to_have_class(re.compile(r"\bactive\b"))
        stored = page.evaluate(
            """
            () => {
              const key = Object.keys(localStorage).find((item) => item.startsWith("cv.editor.activeSection::"));
              return {
                mode: localStorage.getItem("cv.editor.formMode"),
                active: key ? [key, localStorage.getItem(key)] : null,
              };
            }
            """
        )
        assert stored["mode"] == "focus"
        assert stored["active"] and stored["active"][1] == target_key

        page.locator('.form-mode-btn[data-form-mode="overview"]').click()
        expect(page.locator("body")).to_have_attribute("data-form-mode", "overview")
        expect(page.locator("#focus-section-nav")).to_be_hidden()
        expect(page.locator("#form-host .form-section[data-section-key]:not([hidden])")).to_have_count(total)

        collapse_head = page.locator("#form-host .form-section-head.is-collapsible").first
        collapsed_key = collapse_head.evaluate(
            "node => node.closest('.form-section[data-section-key]').dataset.sectionKey"
        )
        collapse_body = collapse_head.locator(
            "xpath=following-sibling::*[contains(concat(' ', normalize-space(@class), ' '), ' form-section-body ')][1]"
        )
        collapse_head.click()
        expect(collapse_body).to_be_hidden()
        page.locator('.form-mode-btn[data-form-mode="focus"]').click()
        page.locator(f'.outline-item[data-slug="{collapsed_key}"]').click()
        expect(collapse_body).to_be_visible()
        page.locator('.form-mode-btn[data-form-mode="overview"]').click()
        expect(collapse_body).to_be_hidden()

        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('.form-mode-btn[data-form-mode="focus"]').click()
        expect(page.locator("#form-host .form-section[data-section-key]:not([hidden])")).to_have_count(1)
        assert_no_horizontal_overflow(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", [Viewport(1440, 900), Viewport(820, 800), Viewport(390, 844), Viewport(320, 740)], ids=lambda v: v.label)
def test_all_form_card_inputs_and_action_buttons_do_not_intersect(browser, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", viewport)
        page.wait_for_selector(".form-card")
        section_counts = page.evaluate(
            """
            () => Object.fromEntries(["experience", "projects", "leadership", "others"].map((key) => [
              key,
              document.querySelectorAll(`.form-section[data-section-key="${key}"] .form-card`).length,
            ]))
            """
        )
        assert all(section_counts[key] > 0 for key in ("experience", "projects", "leadership", "others")), section_counts
        offenders = page.evaluate(
            """
            () => {
	              const controlsSelector = [
	                ".form-field input",
	                ".form-field textarea",
	                ".form-field select",
	                ".bullet-input",
	                ".bullet-actions",
	                ".chips-box",
	                "[contenteditable='true']"
	              ].join(",");
              const out = [];
              const intersects = (a, b) => (
                a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top
              );
	              document.querySelectorAll(".form-card").forEach((card, cardIndex) => {
                const actions = card.querySelector(".form-card-actions");
                if (!actions) return;
                const ar = actions.getBoundingClientRect();
                const padded = { left: ar.left - 8, right: ar.right + 8, top: ar.top - 8, bottom: ar.bottom + 8 };
                card.querySelectorAll(controlsSelector).forEach((control) => {
                  const cs = getComputedStyle(control);
                  const cr = control.getBoundingClientRect();
                  if (control.hidden || cs.display === "none" || cs.visibility === "hidden" || cr.width <= 0 || cr.height <= 0) return;
                  if (intersects(padded, cr)) {
                    out.push({
                      cardIndex,
                      sectionKey: card.closest(".form-section[data-section-key]")?.dataset.sectionKey || "",
                      action: { left: ar.left, right: ar.right, top: ar.top, bottom: ar.bottom },
                      control: { tag: control.tagName, left: cr.left, right: cr.right, top: cr.top, bottom: cr.bottom },
                      text: (control.getAttribute("aria-label") || control.id || control.name || "").slice(0, 80),
                    });
                  }
                });
              });
              return out;
            }
            """
        )
        assert offenders == []
        assert_no_horizontal_overflow(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", [Viewport(1440, 900), Viewport(820, 800), Viewport(390, 844), Viewport(320, 740)], ids=lambda v: v.label)
def test_editor_structure_and_repeated_add_buttons_have_clearance(browser, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", viewport)
        page.wait_for_selector("#form-section-structure .form-structure-list")
        preset_labels = [
            re.sub(r"\s+", " ", label).strip()
            for label in page.locator("#form-section-structure .form-structure-presets .form-add-btn").all_inner_texts()
        ]
        assert preset_labels == []
        repeated_add_labels = [
            re.sub(r"\s+", " ", page.locator(selector).inner_text()).strip()
            for selector in (
                '.form-section[data-section-key="experience"] > .form-section-body > .form-list + .form-add-btn',
                '.form-section[data-section-key="projects"] > .form-section-body > .form-list + .form-add-btn',
            )
        ]
        assert repeated_add_labels == ["Add role", "Add project"]
        offenders = page.evaluate(
            """
            () => {
              const out = [];
              const directMinimum = 14;
              const structureMinimum = 24;
              const pairMinimum = 8;
              const visible = (node) => {
                if (!node) return false;
                const cs = getComputedStyle(node);
                const r = node.getBoundingClientRect();
                return !node.hidden && cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0;
              };
              const rect = (node) => {
                const r = node.getBoundingClientRect();
                return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
              };
              const separated = (a, b, gap) => (
                b.left >= a.right + gap ||
                a.left >= b.right + gap ||
                b.top >= a.bottom + gap ||
                a.top >= b.bottom + gap
              );

              document.querySelectorAll(".form-section[data-section-key]").forEach((section) => {
                const key = section.dataset.sectionKey || "";
                const list = section.querySelector(":scope .form-section-body > .form-list");
                const add = section.querySelector(":scope .form-section-body > .form-add-btn");
                if (!visible(list) || !visible(add)) return;
                const clearance = add.getBoundingClientRect().top - list.getBoundingClientRect().bottom;
                if (clearance < directMinimum) {
                  out.push({
                    key,
                    label: (add.textContent || "").trim().replace(/\\s+/g, " "),
                    clearance,
                    minimum: directMinimum,
                  });
                }
              });

              const structureList = document.querySelector("#form-section-structure .form-structure-list");
              const presetButtons = Array.from(document.querySelectorAll("#form-section-structure .form-structure-presets .form-add-btn"));
              if (visible(structureList)) {
                const listBottom = structureList.getBoundingClientRect().bottom;
                const visibleButtons = presetButtons.filter(visible);
                visibleButtons.forEach((button) => {
                  const clearance = button.getBoundingClientRect().top - listBottom;
                  if (clearance < structureMinimum) {
                    out.push({
                      key: "structure",
                      label: (button.textContent || "").trim().replace(/\\s+/g, " "),
                      clearance,
                      minimum: structureMinimum,
                    });
                  }
                });
                visibleButtons.forEach((button, idx) => {
                  visibleButtons.slice(idx + 1).forEach((other) => {
                    if (!separated(rect(button), rect(other), pairMinimum)) {
                      out.push({
                        key: "structure",
                        label: `${(button.textContent || "").trim()} / ${(other.textContent || "").trim()}`,
                        clearance: 0,
                        minimum: pairMinimum,
                      });
                    }
                  });
                });
              }
              return out;
            }
            """
        )
        assert offenders == []
        assert_no_horizontal_overflow(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", [Viewport(1440, 900), Viewport(1280, 800)], ids=lambda v: v.label)
def test_splitter_left_drag_stops_before_editor_becomes_cramped(browser, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", viewport)
        page.wait_for_selector("#resizer")
        for _ in range(50):
            if not page.locator("#panes").evaluate("(el) => el.classList.contains('is-stacked')"):
                break
            page.wait_for_timeout(100)
        assert not page.locator("#panes").evaluate("(el) => el.classList.contains('is-stacked')")
        box = page.locator("#resizer").bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(0, box["y"] + box["height"] / 2, steps=20)
        page.mouse.up()
        page.wait_for_timeout(150)
        metrics = page.evaluate(
            """
            () => {
              const editor = document.querySelector("#editor-pane").getBoundingClientRect();
              const preview = document.querySelector("#preview-pane").getBoundingClientRect();
              const section = document.querySelector(".form-section").getBoundingClientRect();
              const card = document.querySelector(".form-card")?.getBoundingClientRect();
              const resizer = document.querySelector("#resizer");
              return {
                editorWidth: editor.width,
                previewWidth: preview.width,
                sectionWidth: section.width,
                cardWidth: card ? card.width : null,
                ariaMin: Number(resizer.getAttribute("aria-valuemin")),
                ariaNow: Number(resizer.getAttribute("aria-valuenow")),
                split: Number(localStorage.getItem("cv.editor.split")),
                bodyOverflow: document.body.scrollWidth - document.body.clientWidth,
              };
            }
            """
        )
        assert metrics["editorWidth"] >= 511
        assert metrics["previewWidth"] >= 339
        assert metrics["sectionWidth"] >= 430
        if metrics["cardWidth"] is not None:
            assert metrics["cardWidth"] >= 390
        assert metrics["ariaNow"] >= metrics["ariaMin"]
        assert metrics["split"] >= metrics["ariaMin"] - 1
        assert metrics["bodyOverflow"] <= 1
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_splitter_preserves_user_split_across_stacked_resize(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(1440, 900))
        page.wait_for_selector("#resizer")
        resizer_box = page.locator("#resizer").bounding_box()
        assert resizer_box is not None
        drag_y = resizer_box["y"] + resizer_box["height"] / 2
        page.mouse.move(resizer_box["x"] + resizer_box["width"] / 2, drag_y)
        page.mouse.down()
        page.mouse.move(resizer_box["x"] + 130, drag_y, steps=8)
        page.mouse.up()
        page.wait_for_function(
            "() => Number(localStorage.getItem('cv.editor.split')) > 50"
        )
        desktop_split = page.evaluate("() => Number(localStorage.getItem('cv.editor.split'))")
        resizer_box = page.locator("#resizer").bounding_box()
        assert resizer_box is not None
        drag_y = resizer_box["y"] + resizer_box["height"] / 2
        page.mouse.move(resizer_box["x"] + resizer_box["width"] / 2, drag_y)
        page.mouse.down()
        page.mouse.move(resizer_box["x"] + 1000, drag_y, steps=10)
        page.mouse.up()
        page.wait_for_function(
            "(previous) => Number(localStorage.getItem('cv.editor.split')) > previous + 5",
            arg=desktop_split,
        )
        desktop_split = page.evaluate("() => Number(localStorage.getItem('cv.editor.split'))")
        page.set_viewport_size({"width": 1280, "height": 720})
        for _ in range(50):
            if not page.locator("#panes").evaluate("(el) => el.classList.contains('is-stacked')"):
                break
            page.wait_for_timeout(100)
        tablet_metrics = page.evaluate(
            """
            () => {
              const editor = document.querySelector("#editor-pane").getBoundingClientRect();
              const preview = document.querySelector("#preview-pane").getBoundingClientRect();
              return {
                stacked: document.querySelector("#panes").classList.contains("is-stacked"),
                split: Number(localStorage.getItem("cv.editor.split")),
                editorWidth: editor.width,
                previewWidth: preview.width,
              };
            }
            """
        )
        assert tablet_metrics["stacked"] is False
        assert abs(tablet_metrics["split"] - desktop_split) <= 2
        assert tablet_metrics["editorWidth"] >= 511
        assert tablet_metrics["previewWidth"] >= 339
        page.set_viewport_size({"width": 820, "height": 800})
        for _ in range(50):
            if page.locator("#panes").evaluate("(el) => el.classList.contains('is-stacked')"):
                break
            page.wait_for_timeout(100)
        assert page.locator("#panes").evaluate("(el) => el.classList.contains('is-stacked')")
        assert abs(page.evaluate("() => Number(localStorage.getItem('cv.editor.split'))") - desktop_split) <= 2
        page.set_viewport_size({"width": 390, "height": 844})
        for _ in range(50):
            if page.locator("#panes").evaluate("(el) => el.classList.contains('is-stacked')"):
                break
            page.wait_for_timeout(100)
        mobile_metrics = page.evaluate(
            """
            () => ({
              stacked: document.querySelector("#panes").classList.contains("is-stacked"),
              split: Number(localStorage.getItem("cv.editor.split")),
              bodyOverflow: document.body.scrollWidth - document.body.clientWidth,
            })
            """
        )
        assert mobile_metrics["stacked"] is True
        assert abs(mobile_metrics["split"] - desktop_split) <= 2
        assert mobile_metrics["bodyOverflow"] <= 1
        page.set_viewport_size({"width": 1440, "height": 900})
        for _ in range(50):
            if not page.locator("#panes").evaluate("(el) => el.classList.contains('is-stacked')"):
                break
            page.wait_for_timeout(100)
        metrics = page.evaluate(
            """
            () => {
              const editor = document.querySelector("#editor-pane").getBoundingClientRect();
              const preview = document.querySelector("#preview-pane").getBoundingClientRect();
              return {
                stacked: document.querySelector("#panes").classList.contains("is-stacked"),
                split: Number(localStorage.getItem("cv.editor.split")),
                editorWidth: editor.width,
                previewWidth: preview.width,
              };
            }
            """
        )
        assert metrics["stacked"] is False
        assert abs(metrics["split"] - desktop_split) <= 2
        assert metrics["editorWidth"] >= 511
        assert metrics["previewWidth"] >= 339
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_yaml_view_uses_yaml_pane_floor_instead_of_form_stack_floor(browser):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(1100, 760))
        page.locator('.view-tab[data-view="yaml"]').click()
        for _ in range(50):
            if not page.locator("#panes").evaluate("(el) => el.classList.contains('is-stacked')"):
                break
            page.wait_for_timeout(100)
        metrics = page.evaluate(
            """
            () => {
              const editor = document.querySelector("#editor-pane").getBoundingClientRect();
              const preview = document.querySelector("#preview-pane").getBoundingClientRect();
              return {
                stacked: document.querySelector("#panes").classList.contains("is-stacked"),
                editorWidth: editor.width,
                previewWidth: preview.width,
              };
            }
            """
        )
        assert metrics["stacked"] is False
        assert metrics["editorWidth"] >= 359
        assert metrics["previewWidth"] >= 339
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize(
    "viewport",
    [Viewport(1100, 760), Viewport(930, 760), Viewport(920, 760), Viewport(915, 760), Viewport(900, 760), Viewport(820, 800)],
    ids=lambda v: v.label,
)
def test_form_view_stack_boundary_across_plan_096_widths(browser, viewport: Viewport):
    expected_pane_widths = {
        1100: 880,
        930: 710,
        920: 700,
        915: 695,
        900: 680,
        820: 820,
    }
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", viewport)
        page.wait_for_selector("#panes")
        page.locator('.view-tab[data-view="form"]').click()
        page.wait_for_timeout(250)
        metrics = page.evaluate(
            """
            () => {
              const panes = document.querySelector("#panes");
              const editor = document.querySelector("#editor-pane").getBoundingClientRect();
              const preview = document.querySelector("#preview-pane").getBoundingClientRect();
              const resizer = document.querySelector("#resizer");
              const pr = panes.getBoundingClientRect();
              const stacked = panes.classList.contains("is-stacked");
              return {
                viewport: window.innerWidth,
                paneWidth: pr.width,
                stacked,
                editorWidth: editor.width,
                previewWidth: preview.width,
                resizerVisible: getComputedStyle(resizer).display !== "none",
                resizerDisabled: resizer.getAttribute("aria-disabled"),
                bodyOverflow: document.body.scrollWidth - document.body.clientWidth,
              };
            }
            """
        )
        assert abs(metrics["paneWidth"] - expected_pane_widths[viewport.width]) <= 1
        assert metrics["stacked"] is True
        if metrics["stacked"]:
            assert metrics["resizerVisible"] is False
            assert metrics["resizerDisabled"] == "true"
            assert metrics["editorWidth"] >= viewport.width - 24 or metrics["editorWidth"] >= metrics["paneWidth"] - 2
            assert metrics["previewWidth"] >= metrics["paneWidth"] - 2
        else:
            assert metrics["resizerVisible"] is True
            assert metrics["resizerDisabled"] == "false"
            assert metrics["editorWidth"] >= 511
            assert metrics["previewWidth"] >= 339
        assert metrics["bodyOverflow"] <= 1
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.no_retry
@pytest.mark.parametrize("viewport", [Viewport(1440, 900), Viewport(820, 800), Viewport(390, 844), Viewport(320, 740)], ids=lambda v: v.label)
def test_repeated_item_and_bullet_reorder_controls_are_reachable(browser, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)

    def handle_cv_save(route):
        parsed = urlparse(route.request.url)
        if parsed.path == "/api/cv" and route.request.method == "POST":
            route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
            return
        route.fallback()

    try:
        # Exercise the complete autosave path without mutating the module-wide
        # local demo CV. Retries and later viewport cases must start from the
        # same backend state as the first attempt.
        page.route("**/api/cv*", handle_cv_save)
        goto_route(page, "/app", viewport)
        page.evaluate(
            """() => {
              window.__layoutAllowedWrites = [{ method: "POST", path: "/api/cv" }];
            }"""
        )
        page.wait_for_selector('.form-section[data-section-key="experience"] .form-card')
        section = page.locator('.form-section[data-section-key="experience"]')
        if section.locator(".form-section-head.is-collapsible").get_attribute("aria-expanded") == "false":
            section.locator(".form-section-head.is-collapsible").click()
        cards = section.locator(".form-card")
        assert cards.count() >= 2
        first_value = cards.nth(0).locator("input, textarea").first.input_value()
        second_value = cards.nth(1).locator("input, textarea").first.input_value()
        move_down = cards.nth(0).locator('.form-card-actions button[aria-label="Move down"]')
        move_down.scroll_into_view_if_needed()
        expect(move_down).to_be_visible()
        move_down.click()
        expect(cards.nth(0).locator("input, textarea").first).to_have_value(second_value)
        expect(cards.nth(1).locator("input, textarea").first).to_have_value(first_value)
        move_down = cards.nth(0).locator('.form-card-actions button[aria-label="Move down"]')
        move_down.scroll_into_view_if_needed()
        move_down.focus()
        assert page.evaluate("() => document.activeElement?.getAttribute('aria-label')") == "Move down"
        move_down.press("Enter")
        expect(cards.nth(0).locator("input, textarea").first).to_have_value(first_value)
        expect(cards.nth(1).locator("input, textarea").first).to_have_value(second_value)
        cards.nth(0).locator("input, textarea").first.fill(first_value + " keyboard proof")
        expect(cards.nth(0).locator("input, textarea").first).to_have_value(first_value + " keyboard proof")

        add_item = section.locator(".form-add-btn").first
        before_count = cards.count()
        add_item.scroll_into_view_if_needed()
        expect(add_item).to_be_visible()
        add_item.click()
        expect(cards).to_have_count(before_count + 1)
        remove_item = cards.nth(before_count).locator('.form-card-actions button[aria-label="Remove"]')
        remove_item.scroll_into_view_if_needed()
        expect(remove_item).to_be_visible()
        remove_item.focus()
        assert page.evaluate("() => document.activeElement?.getAttribute('aria-label')") == "Remove"
        remove_item.press("Enter")
        expect(cards).to_have_count(before_count)

        card = cards.nth(0)
        add_bullet = card.locator(".bullet-add-btn").first
        add_bullet.scroll_into_view_if_needed()
        expect(add_bullet).to_be_visible()
        add_bullet.click()
        add_bullet.focus()
        add_bullet.press("Enter")
        bullet_inputs = card.locator(".bullet-row .bullet-input")
        expect(bullet_inputs.nth(1)).to_be_visible()
        bullet_inputs.nth(0).fill("Browser reorder proof bullet A")
        bullet_inputs.nth(1).fill("Browser reorder proof bullet B")
        bullet_down = card.locator('.bullet-row').nth(0).locator('.bullet-actions button[aria-label="Move down"]')
        bullet_down.focus()
        assert page.evaluate("() => document.activeElement?.getAttribute('aria-label')") == "Move down"
        bullet_down.press("Enter")
        expect(card.locator(".bullet-row .bullet-input").nth(0)).to_have_value("Browser reorder proof bullet B")
        expect(card.locator(".bullet-row .bullet-input").nth(1)).to_have_value("Browser reorder proof bullet A")
        card.locator(".bullet-row .bullet-input").nth(0).fill("Browser writable bullet after reorder")
        expect(card.locator(".bullet-row .bullet-input").nth(0)).to_have_value("Browser writable bullet after reorder")
        remove_bullet = card.locator('.bullet-row').nth(0).locator('.bullet-actions button[aria-label="Remove bullet"]')
        remove_bullet.focus()
        remove_bullet.press("Enter")
        expect(card.locator(".bullet-row .bullet-input").first).to_be_visible()
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", [Viewport(1440, 900), Viewport(820, 800), Viewport(390, 844), Viewport(320, 740)], ids=lambda v: v.label)
def test_editor_longform_fields_and_bullets_wrap_without_horizontal_clipping(browser, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", viewport)
        # .form-host exists before the async CV load rebuilds the form. Wait
        # for the loaded editor shell and the two controls this contract
        # measures so an initial empty-model render cannot race the assertion.
        wait_for_editor_shell_ready(page)
        page.wait_for_selector(".form-field textarea", state="visible")
        page.wait_for_selector(".bullet-input", state="visible")
        metrics = page.evaluate(
            """
            () => {
              const longText = "Led a cross-functional migration of a data product into a governed platform, coordinating release risk, stakeholder feedback, documentation, observability, and measurable operational improvements across teams.";
              const textareas = Array.from(document.querySelectorAll(".form-field textarea"));
              const description = textareas.find((ta) => {
                const label = ta.closest(".form-field")?.querySelector(".form-field-label")?.textContent || "";
                return /Description|Note|Title|Authors/.test(label);
              }) || textareas[0];
              if (description) {
                description.value = longText;
                description.dispatchEvent(new Event("input", { bubbles: true }));
              }
              const bullet = document.querySelector(".bullet-input");
              if (bullet) {
                bullet.value = longText;
                bullet.dispatchEvent(new Event("input", { bubbles: true }));
              }
              const measure = (el) => {
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {
                  tag: el.tagName,
                  width: r.width,
                  height: r.height,
                  scrollWidth: el.scrollWidth,
                  clientWidth: el.clientWidth,
                  scrollHeight: el.scrollHeight,
                  clientHeight: el.clientHeight,
                  whiteSpace: getComputedStyle(el).whiteSpace,
                };
              };
              const row = bullet && bullet.closest(".bullet-row");
              const actions = row && row.querySelector(".bullet-actions");
              const rr = row && row.getBoundingClientRect();
              const ar = actions && actions.getBoundingClientRect();
              const br = bullet && bullet.getBoundingClientRect();
              const clear = br && ar ? (
                ar.left >= br.right + 8 ||
                br.left >= ar.right + 8 ||
                ar.top >= br.bottom + 8 ||
                br.top >= ar.bottom + 8
              ) : false;
              return {
                description: measure(description),
                bullet: measure(bullet),
                actionInside: rr && ar ? ar.right <= rr.right + 1 && ar.left >= rr.left - 1 : false,
                actionClearance: clear,
              };
            }
            """
        )
        assert metrics["description"] is not None
        assert metrics["description"]["tag"] == "TEXTAREA"
        assert metrics["description"]["height"] > 36
        assert metrics["description"]["scrollWidth"] <= metrics["description"]["clientWidth"] + 1
        assert metrics["bullet"] is not None
        assert metrics["bullet"]["tag"] == "TEXTAREA"
        assert metrics["bullet"]["height"] > 36
        assert metrics["bullet"]["scrollWidth"] <= metrics["bullet"]["clientWidth"] + 1
        assert metrics["actionInside"] is True
        assert metrics["actionClearance"] is True
        assert_no_horizontal_overflow(page)
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", [Viewport(1440, 900), Viewport(1280, 720), Viewport(820, 800), Viewport(390, 844)], ids=lambda v: v.label)
def test_workflow_primary_actions_are_in_first_viewport_or_sticky(browser, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        checks = [
            ("/cover-letters", "#cl-generate"),
            # The collapsed add-job disclosure is the first-view action. The
            # submit button is intentionally hidden until the user opens it.
            ("/jobs", "#jb-add-details > summary"),
            ("/tailor", "#tl-score"),
            ("/tailor", "#tl-tailor"),
        ]
        for route, selector in checks:
            goto_route(page, route, viewport)
            page.wait_for_selector(selector)
            action = page.evaluate(
                """
                (selector) => {
                  const el = document.querySelector(selector);
                  const r = el.getBoundingClientRect();
                  const cs = getComputedStyle(el);
                  return {
                    visible: !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0,
                    top: r.top,
                    bottom: r.bottom,
                    right: r.right,
                    viewportHeight: window.innerHeight,
                    viewportWidth: window.innerWidth,
                  };
                }
                """,
                selector,
            )
            assert action["visible"] is True, {"route": route, "selector": selector, "action": action}
            assert action["top"] >= -1, {"route": route, "selector": selector, "action": action}
            assert action["bottom"] <= action["viewportHeight"] + 1, {"route": route, "selector": selector, "action": action}
            assert action["right"] <= action["viewportWidth"] + 1, {"route": route, "selector": selector, "action": action}
            assert_no_horizontal_overflow(page)
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", E2E_VIEWPORTS, ids=lambda v: v.label)
def test_preview_fit_bounds_are_parent_owned(browser, viewport: Viewport):
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", viewport)
        page.wait_for_selector("#preview")
        refresh_visible = page.locator("#refresh-btn").evaluate(
            """el => {
              const cs = getComputedStyle(el);
              const r = el.getBoundingClientRect();
              return cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0;
            }"""
        )
        if refresh_visible:
            page.locator("#refresh-btn").click()
        else:
            page.locator("#mobile-utility-btn").click()
            page.wait_for_selector("#mobile-utility-popover:not([hidden])")
            page.locator('[data-mobile-action="refresh"]').click()
        wait_for_preview_idle(page, timeout=20000)
        preview_body = page.frame_locator("#preview").locator("body")
        preview_body.wait_for(state="attached", timeout=10000)
        page.locator("#zoom-fit").click()
        page.evaluate(
            """() => new Promise((resolve) =>
              requestAnimationFrame(() => requestAnimationFrame(resolve))
            )"""
        )
        body_metrics = preview_body.evaluate(
            """body => ({
              bodyPadding: getComputedStyle(body).padding,
              bodyOverflowX: getComputedStyle(body).overflowX,
              bodyScrollW: body.scrollWidth,
              bodyClientW: body.clientWidth,
            })"""
        )
        metrics = page.evaluate(
            """
            () => {
              const wrap = document.querySelector("#preview-wrap");
              const stage = document.querySelector("#preview-stage");
              const iframe = document.querySelector("#preview");
              const wr = wrap.getBoundingClientRect();
              const ir = iframe.getBoundingClientRect();
              const margin = {
                top: ir.top - wr.top,
                left: ir.left - wr.left,
                right: wr.right - ir.right,
                bottom: wr.bottom - ir.bottom,
              };
              return {
                wrap: { left: wr.left, right: wr.right, top: wr.top, bottom: wr.bottom },
                iframe: { left: ir.left, right: ir.right, top: ir.top, bottom: ir.bottom, width: ir.width, height: ir.height },
                margin,
                wrapScrollTop: wrap.scrollTop,
                wrapScrollLeft: wrap.scrollLeft,
                wrapScrollWidth: wrap.scrollWidth,
                wrapClientWidth: wrap.clientWidth,
                wrapScrollHeight: wrap.scrollHeight,
                wrapClientHeight: wrap.clientHeight,
                stageBg: getComputedStyle(stage).backgroundColor,
                iframeBg: getComputedStyle(iframe).backgroundColor,
                previewMessageType: stage.dataset.previewMessageType || "",
                contentHeight: Number(stage.dataset.contentHeight || 0),
                contentWidth: Number(stage.dataset.contentWidth || 0),
              };
            }
            """
        )
        metrics.update(body_metrics)
        assert metrics["bodyPadding"] in ("0px", "0px 0px")
        assert metrics["previewMessageType"] in ("cv-content-rendered", "paged-rendered")
        assert metrics["contentHeight"] > 0
        assert metrics["contentWidth"] > 0
        assert metrics["iframe"]["left"] >= metrics["wrap"]["left"] - 1
        assert metrics["iframe"]["right"] <= metrics["wrap"]["right"] + 1
        assert metrics["iframe"]["top"] >= metrics["wrap"]["top"] - 1
        assert metrics["margin"]["top"] >= 9
        assert metrics["margin"]["left"] >= 9
        assert metrics["margin"]["right"] >= 9
        assert metrics["wrapScrollTop"] <= 1
        assert metrics["wrapScrollLeft"] <= 1
        assert metrics["wrapScrollWidth"] <= metrics["wrapClientWidth"] + 2
        assert metrics["wrapScrollHeight"] <= metrics["wrapClientHeight"] + 2
        assert metrics["bodyScrollW"] <= metrics["bodyClientW"] + 1
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_layout_contract_base_url_documented():
    assert BASE_URL.startswith("http://127.0.0.1") or BASE_URL.startswith("http://localhost")


_RASTER_DENSITIES = ("tight", "normal", "airy")
_RASTER_PAPER_BACKGROUNDS = {
    "white": "#ffffff",
    "creme": "#fff7e8",
    "soft-gray": "#f4f5f2",
    "blue-tint": "#eef6ff",
}
_RASTER_PAPERS = tuple(_RASTER_PAPER_BACKGROUNDS)
_RASTER_TEMPLATES = (
    ("classic", "standard", "serif", "#1f6f43"),
    ("modern", "standard", "sans", "#8a2b2b"),
    ("sidebar", "rail", "sans", "#0f766e"),
    ("executive", "standard", "serif", "#7a4c00"),
    ("technical", "standard", "sans", "#2266aa"),
    ("ats-compact", "standard", "sans", "#6d28d9"),
    ("consulting", "standard", "serif", "#8a4f14"),
    ("minimal", "standard", "sans", "#0f5f6b"),
)
_RASTER_FIXTURE_YAML = (Path(__file__).resolve().parents[2] / "content" / "cv.example.yaml").read_text(
    encoding="utf-8"
)
_RASTER_KNOWN_OVERFLOW_CASES = {("sidebar", "airy")}


def _raster_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for template_index, (template, renderer, font, accent) in enumerate(_RASTER_TEMPLATES):
        for density_index, density in enumerate(_RASTER_DENSITIES):
            paper = _RASTER_PAPERS[(template_index + density_index) % len(_RASTER_PAPERS)]
            theme = "dark" if (template_index + density_index) % 4 == 3 else "light"
            background = "#221d18" if theme == "dark" else _RASTER_PAPER_BACKGROUNDS[paper]
            cases.append(
                {
                    "name": f"{template}-{density}-{paper}-{theme}",
                    "template": template,
                    "renderer": renderer,
                    "density": density,
                    "paper": paper,
                    "theme": theme,
                    "font": font,
                    "accent": accent,
                    "background": background,
                    "text": "light" if theme == "dark" else "dark",
                    "accent_pixels": theme == "light",
                    "expect_one_page": (template, density) not in _RASTER_KNOWN_OVERFLOW_CASES,
                }
            )
    return cases


RASTER_CASES = _raster_cases()


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def _near_rgb(pixel: tuple[int, int, int], expected: tuple[int, int, int], tolerance: int) -> bool:
    return all(abs(pixel[i] - expected[i]) <= tolerance for i in range(3))


def _image_stats(png: bytes, *, background: str, accent: str) -> dict[str, float]:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        rgb = image.convert("RGB")
        pixels = list(rgb.getdata())
    total = len(pixels)
    background_rgb = _hex_rgb(background)
    accent_rgb = _hex_rgb(accent)
    dark_pixels = sum(1 for r, g, b in pixels if r < 90 and g < 90 and b < 90)
    light_pixels = sum(1 for r, g, b in pixels if r > 185 and g > 180 and b > 170)
    background_pixels = sum(1 for px in pixels if _near_rgb(px, background_rgb, 18))
    accent_pixels = sum(1 for px in pixels if _near_rgb(px, accent_rgb, 42))
    sample_step = max(1, total // 5000)
    sampled_unique = len(set(pixels[::sample_step]))
    return {
        "total": float(total),
        "dark_ratio": dark_pixels / total,
        "light_ratio": light_pixels / total,
        "background_ratio": background_pixels / total,
        "accent_pixels": float(accent_pixels),
        "sampled_unique": float(sampled_unique),
    }


def _rendered_page_metrics(page) -> dict:
    return page.evaluate(
        """
        () => {
          const paged = Array.from(document.querySelectorAll(".pagedjs_page"));
          const looseRendered = document.querySelector("main.page, .page");
          const candidates = paged.length
            ? paged.map((page) => {
                const rendered = page.querySelector("main.page, .page");
                const textLength = rendered ? (rendered.textContent || "").trim().length : 0;
                return { page, rendered, textLength };
              }).filter((entry) => entry.rendered)
            : looseRendered
              ? [{ page: looseRendered, rendered: looseRendered, textLength: (looseRendered.textContent || "").trim().length }]
              : [];
          candidates.sort((a, b) => b.textLength - a.textLength);
          const selected = candidates[0];
          const rendered = selected && selected.rendered;
          const page = selected && selected.page;
          const html = document.documentElement;
          if (!page || !rendered) return { missing: true };
          const r = page.getBoundingClientRect();
          const cs = getComputedStyle(page);
          return {
            missing: false,
            previewTheme: html.getAttribute("data-preview-theme"),
            previewMode: html.getAttribute("data-preview-mode"),
            density: html.getAttribute("data-density"),
            template: rendered.getAttribute("data-template"),
            renderer: rendered.getAttribute("data-renderer"),
            paper: rendered.getAttribute("data-paper"),
            pageCount: paged.length || document.querySelectorAll("main.page, .page").length,
            page: { x: r.x, y: r.y, width: r.width, height: r.height },
            scrollWidth: page.scrollWidth,
            clientWidth: page.clientWidth,
            scrollHeight: page.scrollHeight,
            clientHeight: page.clientHeight,
            overflowX: cs.overflowX,
            overflowY: cs.overflowY,
            textLength: selected.textLength,
          };
        }
        """
    )


def _seed_raster_fixture(context, token: str) -> str:
    response = context.request.post(
        f"{BASE_URL}/api/cv",
        data=json.dumps({"content": _RASTER_FIXTURE_YAML}),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    assert response.status == 200, response.text()
    body = response.json()
    doc_id = body.get("id")
    assert doc_id, body
    return str(doc_id)


@pytest.mark.parametrize("case", RASTER_CASES, ids=lambda c: c["name"])
def test_rendered_cv_raster_matrix_has_pixels_and_one_page_geometry(browser, case):
    """Stage 7 raster proof: render real preview HTML, screenshot the CV page,
    and inspect pixels plus page geometry. This catches blank preview output,
    clipped/overflowing content, missing paper/theme application, and missing
    text pixels without committing generated screenshots."""
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/app", Viewport(900, 1300))
        # The editor starts an authenticated preview fetch during boot. Let
        # that cycle finish before replacing the /app document with the raster
        # fixture; otherwise navigation aborts the in-flight fetch and the old
        # realm correctly reports ``TypeError: Failed to fetch`` to the console.
        wait_for_preview_idle(page)
        token = page.evaluate(
            """
            () => window.OnePagerAuth &&
              window.OnePagerAuth.getSession &&
              window.OnePagerAuth.getSession() &&
              window.OnePagerAuth.getSession().access_token
            """
        )
        assert token, "authenticated preview request needs a bearer token"
        doc_id = _seed_raster_fixture(context, token)
        query = urlencode(
            {
                "id": doc_id,
                "theme": case["theme"],
                "mode": "paged",
                "density": case["density"],
                "font": case["font"],
                "accent": case["accent"],
                "template": case["template"],
                "paper": case["paper"],
                "embed": "1",
            }
        )
        response = context.request.get(
            f"{BASE_URL}/api/preview?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status == 200, response.text()
        html = response.text()
        assert "main" in html and "page" in html

        page.set_viewport_size({"width": 900, "height": 1300})
        # Serve the preview HTML through a REAL same-origin navigation instead
        # of page.set_content(). set_content swaps the DOM but keeps the /app
        # JS realm alive: editor intervals (e.g. the 8s ``refreshLastSaved``
        # ticker in app.js) then keep firing against nodes that no longer
        # exist and throw uncaught pageerrors ("Cannot read properties of null
        # (reading 'setAttribute')") — the observed full-suite flake. A
        # navigation destroys the editor realm first, so no app timer survives
        # into the preview document.
        page.route(
            "**/__e2e_raster_fixture__",
            lambda route: route.fulfill(status=200, content_type="text/html", body=html),
        )
        page.goto(f"{BASE_URL}/__e2e_raster_fixture__", wait_until="load")
        page.wait_for_selector("main.page, .page", timeout=10000)
        page.wait_for_function(
            "() => document.documentElement.classList.contains('pagedjs-ready')",
            timeout=20000,
        )
        page.wait_for_selector(".pagedjs_page", timeout=10000)
        page.evaluate(
            "() => document.fonts && document.fonts.ready ? document.fonts.ready : true"
        )
        metrics = _rendered_page_metrics(page)
        assert metrics["missing"] is False
        assert metrics["previewTheme"] == case["theme"]
        assert metrics["previewMode"] == "paged"
        assert metrics["density"] == case["density"]
        assert metrics["template"] == case["template"]
        assert metrics["renderer"] == case["renderer"]
        assert metrics["paper"] == case["paper"]
        if case["expect_one_page"]:
            assert metrics["pageCount"] == 1
        else:
            assert metrics["pageCount"] >= 1
        assert metrics["textLength"] > 1000
        assert 735 <= metrics["page"]["width"] <= 850
        assert 1050 <= metrics["page"]["height"] <= 1165
        assert metrics["scrollWidth"] <= metrics["clientWidth"] + 3
        assert metrics["scrollHeight"] <= metrics["clientHeight"] + 3

        clip = {
            "x": max(0, int(metrics["page"]["x"])),
            "y": max(0, int(metrics["page"]["y"])),
            "width": max(1, int(metrics["page"]["width"])),
            "height": max(1, int(metrics["page"]["height"])),
        }
        png = page.screenshot(clip=clip)
        stats = _image_stats(
            png,
            background=case["background"],
            accent=case["accent"],
        )
        assert stats["sampled_unique"] >= 24, (case["name"], stats)
        assert stats["background_ratio"] >= 0.18, (case["name"], stats)
        if case["text"] == "dark":
            assert stats["dark_ratio"] >= 0.006, (case["name"], stats)
        else:
            assert stats["light_ratio"] >= 0.006, (case["name"], stats)
        if case["accent_pixels"]:
            assert stats["accent_pixels"] >= 30, (case["name"], stats)

        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def test_template_modal_thumbnails_cache_skeleton_and_error_states(browser):
    """Plan 107 F4/F5: thumbnails fetch once per (template, appearance) and a
    failed preview shows a visible message instead of a silent blank card."""
    context = browser.new_context(
        device_scale_factor=1,
        storage_state=auth_storage_state(browser),
    )
    page = context.new_page()
    install_editor_storage_reset(page)
    install_layout_vendor_routes(page)
    thumb_urls: list[str] = []
    console_errors, page_errors = console_collector(page)
    templates_body = json.dumps({
        "templates": [
            {"id": "classic", "label": "Classic", "use_case": "Conservative roles", "renderer": "standard", "layout_family": "single_column", "column_count": 1, "ats_priority": "high", "customization_axes": ["accent", "font"], "paper_sizes": ["A4"], "one_page_risk": "normal"},
            {"id": "technical", "label": "Technical", "use_case": "Engineering and data roles", "renderer": "standard", "layout_family": "compact", "column_count": 1, "ats_priority": "high", "customization_axes": ["accent", "font", "density"], "paper_sizes": ["A4"], "one_page_risk": "low"},
        ]
    })
    cv_body = 'name: Template Test\ntemplate: classic\nfont: sans\ndensity: normal\naccent: "#111111"\ncontact: []\nexperience: []\neducation: []\nskills: []\nprojects: []\nleadership: []\nothers: []\n'
    fail_thumbs = {"on": False}
    try:
        page.set_viewport_size({"width": 1440, "height": 900})

        def handle_api(route):
            url = route.request.url
            if "/api/templates" in url:
                route.fulfill(status=200, content_type="application/json", body=templates_body)
                return
            if "/api/preview" in url:
                if "thumb=1" in url:
                    thumb_urls.append(url)
                    if fail_thumbs["on"]:
                        route.fulfill(status=500, content_type="text/plain", body="boom")
                        return
                route.fulfill(status=200, content_type="text/html", body="<main class='page'>Preview</main>")
                return
            if url.endswith("/api/cv") and route.request.method == "GET":
                route.fulfill(status=200, content_type="text/yaml", body=cv_body)
                return
            if url.endswith("/api/cv") and route.request.method == "POST":
                route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
                return
            route.fallback()

        page.route("**/api/**", handle_api)
        page.goto(f"{BASE_URL}/app", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        # F5: nothing fetches thumbnails while the modal is closed.
        assert thumb_urls == []

        page.locator("#template-btn").focus()
        page.keyboard.press("Enter")
        page.wait_for_selector("#template-modal:not([hidden])")
        expect(page.locator('#template-modal .template-preview-frame[data-loaded="true"]')).to_have_count(2)
        open_count = len(thumb_urls)
        assert open_count == 2, thumb_urls

        # Searching re-renders from the cache: zero new fetches.
        page.locator("#template-search").fill("tech")
        page.wait_for_timeout(400)
        expect(page.locator('#template-modal [data-template-card="technical"]')).to_be_visible()
        assert len(thumb_urls) == open_count

        # Reopening reuses the cache too.
        page.keyboard.press("Escape")
        expect(page.locator("#template-modal")).to_be_hidden()
        page.locator("#template-btn").click()
        page.wait_for_selector("#template-modal:not([hidden])")
        page.wait_for_timeout(300)
        assert len(thumb_urls) == open_count
        page.keyboard.press("Escape")

        assert_clean_console(console_errors, page_errors)

        # F4: a failed preview surfaces a visible per-card message. A fresh
        # page starts with a cold cache, so the failing fetch really runs.
        fail_thumbs["on"] = True
        page2 = context.new_page()
        install_editor_storage_reset(page2)
        install_layout_vendor_routes(page2)
        page2.set_viewport_size({"width": 1440, "height": 900})
        page2.route("**/api/**", handle_api)
        page2.goto(f"{BASE_URL}/app", wait_until="domcontentloaded")
        page2.wait_for_load_state("networkidle")
        page2.locator("#template-btn").focus()
        page2.keyboard.press("Enter")
        page2.wait_for_selector("#template-modal:not([hidden])")
        expect(page2.locator("#template-modal .template-preview-shell[data-preview-error]")).to_have_count(2)
        error_text = page2.locator("#template-modal .template-preview-shell").first.get_attribute("data-preview-error")
        assert error_text, "failed thumbnail must carry a visible error message"
        page2.keyboard.press("Escape")
    finally:
        context.close()


PLAN108_TOPBAR_BAND = [
    Viewport(1440, 900),
    Viewport(1380, 900),
    Viewport(1300, 900),
    Viewport(1240, 900),
    Viewport(1180, 900),
    Viewport(1121, 760),
    Viewport(1120, 760),
    Viewport(1024, 768),
    Viewport(900, 800),
]


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("sidebar_open", [True, False])
def test_plan108_topbar_never_clips_across_sidebar_and_language(browser, lang: str, sidebar_open: bool):
    """Plan 108 stage 1 hostile case. German labels + sidebar open in the
    ~1120-1440 viewport band was exactly the combination that clipped
    "PDF erstellen" at its left edge (justify-content:flex-end +
    overflow:hidden with a non-shrinking document cluster, measured against
    the viewport while the sidebar stole 256px of workspace). Assert that at
    every width, in both languages, with the sidebar in both states, every
    visible topbar control sits fully inside the topbar and no visible label
    text is clipped — the only allowed response to tight width is an
    intentional collapse state, never a cut control."""
    for viewport in PLAN108_TOPBAR_BAND:
        context, page, console_errors, page_errors = new_page(browser)
        try:
            add_top_document_init_script(
                page,
                f"localStorage.setItem('onepager.lang', '{lang}')",
            )
            goto_route(page, "/app", viewport)
            page.wait_for_function(f"() => document.documentElement.lang === '{lang}'")
            if not sidebar_open:
                toggle = page.locator("#sidebar-toggle")
                if toggle.is_visible():
                    toggle.click()
                    page.wait_for_timeout(300)
            out_of_bounds = page.evaluate(
                """
                () => {
                  const bar = document.querySelector('#topbar');
                  if (!bar) return [{ missing: '#topbar' }];
                  const barRect = bar.getBoundingClientRect();
                  const offenders = [];
                  const controls = bar.querySelectorAll(
                    'button, .crumbs, .topbar-cluster, .lang-toggle, #status, #last-saved, #ats-badge'
                  );
                  controls.forEach((el) => {
                    const cs = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    if (el.hidden || cs.display === 'none' || cs.visibility === 'hidden' || r.width <= 0 || r.height <= 0) return;
                    if (el.closest('[hidden], .popover, .modal')) return;
                    if (r.left < barRect.left - 0.5 || r.right > barRect.right + 0.5) {
                      offenders.push({
                        id: el.id || el.className,
                        left: r.left,
                        right: r.right,
                        barLeft: barRect.left,
                        barRight: barRect.right,
                      });
                    }
                  });
                  return offenders;
                }
                """
            )
            assert not out_of_bounds, (
                f"lang={lang} viewport={viewport.width}x{viewport.height} "
                f"sidebar_open={sidebar_open}: controls clipped by the topbar "
                f"safety net instead of collapsing: {out_of_bounds}"
            )
            assert_no_text_overflow(page, ["#topbar button", "#cv-switcher-label"])
            assert_no_horizontal_overflow(page)
            assert_clean_console(console_errors, page_errors)
        finally:
            context.close()


@pytest.mark.parametrize("lang", ["en", "de"])
def test_plan108_topbar_no_clip_with_unsaved_state_and_logout(browser, lang: str):
    """Plan 108 stage 6 armor: the width-budget worst case ALL AT ONCE —
    German labels, sidebar open, the auth-injected #logout-btn present,
    AND the unsaved-state chrome active (status flips to Modified after a
    real edit). Runs the riskiest band widths from the stage-1 sweep."""
    for viewport in (Viewport(1280, 900), Viewport(1180, 900), Viewport(1120, 760)):
        context, page, console_errors, page_errors = new_page(browser)
        try:
            add_top_document_init_script(
                page,
                f"localStorage.setItem('onepager.lang', '{lang}')",
            )
            goto_route(page, "/app", viewport)
            page.wait_for_function(f"() => document.documentElement.lang === '{lang}'")
            name_field = page.locator("#form-host input").first
            if name_field.count() and name_field.is_visible():
                name_field.click()
                page.keyboard.type(" ")
                page.wait_for_timeout(400)
            clipped = page.evaluate(
                """
                () => {
                  const bar = document.querySelector('#topbar');
                  const barRect = bar.getBoundingClientRect();
                  const offenders = [];
                  bar.querySelectorAll('button, .crumbs, .topbar-cluster').forEach((el) => {
                    const cs = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    if (el.hidden || cs.display === 'none' || cs.visibility === 'hidden' || r.width <= 0) return;
                    if (el.closest('[hidden], .popover, .modal')) return;
                    if (r.left < barRect.left - 0.5 || r.right > barRect.right + 0.5) {
                      offenders.push({ id: el.id || el.className, left: r.left, barLeft: barRect.left });
                    }
                  });
                  return offenders;
                }
                """
            )
            assert not clipped, f"{lang} {viewport.width}: {clipped}"
            assert_no_text_overflow(page, ["#topbar button"])
            assert_no_horizontal_overflow(page)
        finally:
            context.close()


TOOL_PAGE_HEAD_ROUTES = ["/cover-letters", "/tailor", "/jobs"]

TOOL_PAGE_HEAD_VIEWPORTS = [
    Viewport(1440, 900),
    Viewport(900, 760),
    Viewport(760, 800),
    Viewport(430, 932),
    Viewport(390, 844),
    Viewport(320, 740),
]

TOOL_PAGE_HEAD_PROBE = """
() => {
  const head = document.querySelector(".tool-page-head");
  if (!head) return null;
  const rect = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      left: r.left, right: r.right, top: r.top, bottom: r.bottom,
      width: r.width, height: r.height,
    };
  };
  const headStyle = getComputedStyle(head);
  const lifecycle = document.querySelector(".application-lifecycle");
  const steps = Array.from(document.querySelectorAll(".application-lifecycle li"));
  return {
    head: rect(head),
    headOverflowX: head.scrollWidth - head.clientWidth,
    rowGap: parseFloat(headStyle.rowGap) || 0,
    flexDirection: headStyle.flexDirection,
    flexWrap: headStyle.flexWrap,
    sub: rect(document.querySelector(".tool-page-sub")),
    status: rect(document.querySelector(".tool-page-status")),
    lifecycle: lifecycle
      ? {
          ...rect(lifecycle),
          scrollWidth: lifecycle.scrollWidth,
          clientWidth: lifecycle.clientWidth,
          overflowX: getComputedStyle(lifecycle).overflowX,
        }
      : null,
    lastStep: rect(steps[steps.length - 1] || null),
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
    docScrollWidth: document.documentElement.scrollWidth,
  };
}
"""


@pytest.mark.parametrize("route", TOOL_PAGE_HEAD_ROUTES)
@pytest.mark.parametrize("viewport", TOOL_PAGE_HEAD_VIEWPORTS, ids=lambda v: v.label)
def test_tool_page_head_is_bounded_and_fully_contained(browser, route: str, viewport: Viewport):
    """The shared page head must never inflate or lay content out off-canvas.

    `.tool-page-head` flips to `flex-direction: column` at <=760px. Flex basis
    always follows the main axis, so a row-axis `flex: <g> <s> <length|%>` left
    on a child silently becomes a HEIGHT there, and a surviving `flex-wrap:
    wrap` turns the stack into a multi-column layout whose extra columns land
    outside the viewport. Both failures are invisible to page-level overflow
    checks — `.cl-main` clips the overflow and `document.scrollWidth` stays
    clean — so this asserts the head's own geometry instead.
    """
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, route, viewport)
        page.wait_for_selector(".tool-page-head")
        probe = page.evaluate(TOOL_PAGE_HEAD_PROBE)
        assert probe is not None, route
        context_msg = {"route": route, "viewport": viewport.label, "probe": probe}

        # Nothing inside the head may be laid out beyond its own client box.
        assert probe["headOverflowX"] <= 1, context_msg
        assert probe["head"]["left"] >= -0.5, context_msg
        assert probe["head"]["right"] <= probe["viewportWidth"] + 0.5, context_msg
        assert probe["docScrollWidth"] <= probe["viewportWidth"] + 1, context_msg

        if viewport.width <= 760:
            assert probe["flexDirection"] == "column", context_msg
            # A column stack must not wrap — wrapping opens off-canvas columns.
            assert probe["flexWrap"] == "nowrap", context_msg
            # The head only introduces the page; it must not eat the fold.
            assert probe["head"]["height"] <= probe["viewportHeight"] * 0.42, context_msg
            # The stack's own row gap is the only space allowed between the
            # page intro and the next head row. An inflated flex basis shows up
            # here as hundreds of blank pixels.
            if probe["sub"] and probe["status"]:
                blank = probe["status"]["top"] - probe["sub"]["bottom"]
                assert blank <= probe["rowGap"] + 6, {"blank": blank, **context_msg}
        else:
            assert probe["flexDirection"] == "row", context_msg

        if probe["lifecycle"] is not None:
            rail = probe["lifecycle"]
            assert rail["left"] >= -0.5, context_msg
            assert rail["right"] <= probe["viewportWidth"] + 0.5, context_msg
            assert rail["overflowX"] == "auto", context_msg
            if rail["scrollWidth"] > rail["clientWidth"] + 1:
                # The rail owns the intentional horizontal scroll: every step
                # must stay reachable inside it without moving the page.
                reach = page.evaluate(
                    """
                    () => {
                      const rail = document.querySelector(".application-lifecycle");
                      rail.scrollLeft = rail.scrollWidth;
                      const railRect = rail.getBoundingClientRect();
                      const steps = rail.querySelectorAll("li");
                      const last = steps[steps.length - 1].getBoundingClientRect();
                      return {
                        scrollLeft: rail.scrollLeft,
                        lastRight: last.right,
                        railRight: railRect.right,
                        railLeft: railRect.left,
                        lastLeft: last.left,
                        docScrollWidth: document.documentElement.scrollWidth,
                        viewportWidth: window.innerWidth,
                      };
                    }
                    """
                )
                assert reach["scrollLeft"] > 0, {"reach": reach, **context_msg}
                assert reach["lastRight"] <= reach["railRight"] + 1, {"reach": reach, **context_msg}
                assert reach["lastLeft"] >= reach["railLeft"] - 1, {"reach": reach, **context_msg}
                assert reach["docScrollWidth"] <= reach["viewportWidth"] + 1, {"reach": reach, **context_msg}
                page.evaluate("() => { document.querySelector('.application-lifecycle').scrollLeft = 0; }")

            # Keyboard focus must never land on a step the user cannot see at
            # all (WCAG 2.2 SC 2.4.11 Focus Not Obscured, Minimum), and it must
            # never drag the page sideways.
            links = page.locator(".application-lifecycle li > a")
            for index in range(links.count()):
                link = links.nth(index)
                link.focus()
                focus_state = page.evaluate(
                    """
                    () => {
                      const el = document.activeElement;
                      const rail = document.querySelector(".application-lifecycle");
                      const r = el.getBoundingClientRect();
                      const rr = rail.getBoundingClientRect();
                      const cs = getComputedStyle(el);
                      return {
                        id: el.id,
                        focusedIsStep: rail.contains(el),
                        visibleWidth: Math.min(r.right, rr.right) - Math.max(r.left, rr.left),
                        outlineStyle: cs.outlineStyle,
                        docScrollWidth: document.documentElement.scrollWidth,
                        viewportWidth: window.innerWidth,
                        scrollLeft: Math.round(rail.scrollLeft),
                      };
                    }
                    """
                )
                assert focus_state["focusedIsStep"], {"focus": focus_state, **context_msg}
                assert focus_state["visibleWidth"] > 0, {"focus": focus_state, **context_msg}
                assert focus_state["outlineStyle"] != "none", {"focus": focus_state, **context_msg}
                assert focus_state["docScrollWidth"] <= focus_state["viewportWidth"] + 1, {
                    "focus": focus_state,
                    **context_msg,
                }

        assert_no_horizontal_overflow(page)
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()


def wheel_until_inside_viewport(
    page,
    scroller: str,
    selector: str,
    *,
    max_wheels: int = 14,
    step: int = 380,
) -> None:
    """Reach `selector` using real wheel events only.

    Playwright's `scroll_into_view_if_needed` will happily reveal an element a
    trackpad user can never reach, so physical reachability needs physical
    scrolling. Hovering the scroller's left gutter keeps the wheel off nested
    scrollers such as the job-description textarea.
    """
    state_probe = """
      (selector) => {
        const el = document.querySelector(selector);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {
          inside: r.top >= -0.5 && r.bottom <= window.innerHeight + 0.5
                  && r.left >= -0.5 && r.right <= window.innerWidth + 0.5,
          top: r.top,
          bottom: r.bottom,
          viewportHeight: window.innerHeight,
        };
      }
    """
    page.locator(scroller).hover(position={"x": 6, "y": 60})
    last = None
    for _ in range(max_wheels + 1):
        last = page.evaluate(state_probe, selector)
        assert last is not None, f"{selector} is not in the DOM"
        if last["inside"]:
            return
        page.mouse.wheel(0, step)
        page.wait_for_timeout(90)
    pytest.fail(f"{selector} never became reachable by physical scrolling: {last}")


@pytest.mark.parametrize(
    "viewport",
    [Viewport(430, 932), Viewport(390, 844), Viewport(320, 740)],
    ids=lambda v: v.label,
)
def test_cover_letter_generator_and_library_reachable_by_physical_scroll(
    browser, viewport: Viewport
):
    """Every generator and saved-letter control must be trackpad-reachable.

    An inflated page head still passes sticky-action checks, so this walks the
    real scroll container with wheel events and refuses to fall back to
    Playwright's automatic locator scrolling.
    """
    context, page, console_errors, page_errors = new_page(browser)
    try:
        goto_route(page, "/cover-letters", viewport)
        page.wait_for_selector("#cl-generate")
        scroll_state = page.locator(".cl-main").evaluate(
            """
            el => ({
              overflowY: getComputedStyle(el).overflowY,
              scrollHeight: el.scrollHeight,
              clientHeight: el.clientHeight,
            })
            """
        )
        assert scroll_state["overflowY"] == "auto", scroll_state
        assert scroll_state["scrollHeight"] > scroll_state["clientHeight"], scroll_state

        for selector in ["#cl-cv", "#cl-jd", "#cl-generate", ".cl-library-panel", "#cl-list"]:
            wheel_until_inside_viewport(page, ".cl-main", selector)

        moved = page.locator(".cl-main").evaluate("el => el.scrollTop")
        assert moved > 0, "physical wheel scrolling never moved the pane"
        assert_no_horizontal_overflow(page)
        assert page.locator(".cl-main").evaluate("el => el.scrollLeft") == 0
        assert_no_blocked_writes(page)
        assert_clean_console(console_errors, page_errors)
    finally:
        context.close()
