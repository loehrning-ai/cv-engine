from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

from .layout_helpers import (
    BASE_URL,
    E2E_VIEWPORTS,
    Viewport,
    assert_clean_console,
    assert_clean_network,
    assert_inside_viewport,
    assert_no_horizontal_overflow,
    assert_no_text_overflow,
    add_top_document_init_script,
    console_collector,
    install_editor_storage_reset,
    install_layout_vendor_routes,
    network_collector,
    require_live_server,
    wait_for_network_settle,
)


JOB_ID = "11111111-1111-4111-8111-111111111111"
CV_ID = "22222222-2222-4222-8222-222222222222"
TAILORED_CV_ID = "33333333-3333-4333-8333-333333333333"
LETTER_ID = "44444444-4444-4444-8444-444444444444"
PACKET_ID = "77777777-7777-4777-8777-777777777777"
PROFILE_IDS = ["keyword_screen", "strict_requirements", "semantic_fit"]
PROFILE_LABELS = {
    "keyword_screen": "Keyword screen",
    "strict_requirements": "Strict requirements",
    "semantic_fit": "Semantic fit",
}
PROFILE_SCORES = {"keyword_screen": 61, "strict_requirements": 40, "semantic_fit": 83}
JD_TEXT = "Title: Senior Data Engineer\nCompany: Acme GmbH\nSpark and Airflow required."


@pytest.fixture(scope="module")
def browser():
    require_live_server()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


def _json(route, payload: dict, status: int = 200) -> None:
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(payload),
    )


def _assert_application_context_href(
    locator,
    *,
    path: str,
    document_id: str,
    job_id: str,
    cv_id: str,
    source_cv_id: str,
) -> None:
    """Assert a fully-populated application-context link.

    Takes a locator, not a string, on purpose. These hrefs are built
    progressively: `linkedDocumentHref` in jobs.js reads `source_cv_id` off the
    packet, and packets arrive on a later fetch than the linked-document rows,
    so the first render emits a three-parameter href and a subsequent render
    adds the fourth. A one-shot `get_attribute` taken right after the element
    becomes visible can therefore capture the intermediate form and fail with a
    three-key vs four-key mismatch — intermittently, and only under whole-suite
    timing. Poll for the final shape before reading.
    """
    expect(locator).to_have_attribute("href", re.compile(r"[?&]source_cv_id="))
    parsed = urlparse(locator.get_attribute("href") or "")
    assert parsed.path == path
    assert parse_qs(parsed.query) == {
        "id": [document_id],
        "job_id": [job_id],
        "cv_id": [cv_id],
        "source_cv_id": [source_cv_id],
    }


def _install_auth_stub(page) -> None:
    supabase_stub = """
      window.supabase = {
        createClient: () => ({
          auth: {
            onAuthStateChange: () => {},
            getSession: () => Promise.resolve({ data: { session: { access_token: "e2e-token" } } }),
            setSession: () => Promise.resolve({ data: { session: { access_token: "e2e-token" } } }),
            signOut: () => Promise.resolve({})
          }
        })
      };
    """
    add_top_document_init_script(page, supabase_stub)
    add_top_document_init_script(
        page,
        """
        (() => {
          window.__openedPdfTargets = [];
          window.open = (url) => {
            const loc = {};
            Object.defineProperty(loc, "href", {
              set(value) {
                this._href = String(value);
                window.__openedPdfTargets.push(this._href);
              },
              get() {
                return this._href || url || "about:blank";
              }
            });
            const win = {
              closed: false,
              opener: null,
              document: { title: "", body: { textContent: "" } },
              location: loc,
              close() { this.closed = true; }
            };
            return win;
          };
        })();
        """
    )

    def route_static(route):
      route.fulfill(status=200, content_type="application/javascript", body=supabase_stub)

    page.route("**/static/vendor/supabase-js-2.110.0.js", route_static)
    page.route("**/auth/config", lambda route: _json(route, {
        "supabase_url": "https://stub.supabase.co",
        "supabase_publishable_key": "stub",
        "demo_mode": False,
    }))
    page.route("**/api/extract/status", lambda route: _json(route, {
        "connected": False,
        "managed_credits_remaining": 3,
    }))
    page.route("**/api/cvs", lambda route: _json(route, {
        "cvs": [{"id": CV_ID, "name": "Source CV"}],
        "count": 1,
    }))


def _pdf(route) -> None:
    route.fulfill(status=200, content_type="application/pdf", body=b"%PDF-1.4\n%%EOF\n")


def _prepare_authenticated_page(page, viewport: Viewport) -> tuple[list[str], list[str], list[str], list[dict]]:
    page.set_viewport_size({"width": viewport.width, "height": viewport.height})
    install_editor_storage_reset(page)
    _install_auth_stub(page)
    # Default capability stub (plan 107): tailor probes
    # /api/jobs/sources/status at boot for the URL-fetch capability. Tests
    # register their routes AFTER this one, so theirs win; a test handler's
    # route.fallback() (or no handler at all) lands here instead of hitting
    # the live server with the fake bearer token (a 401 console error). The
    # empty payload means "no capability signal" — the client fails open.
    page.route(
        "**/api/jobs/sources/status",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"
        ),
    )
    # Same contract for the palette's recent-documents fetch (plan 108
    # stage 4): opening ⌘K requests /api/documents; without a default stub
    # a test's route.fallback() would hit the live server with the fake
    # bearer (401 console error). Empty list = "no recents group".
    page.route(
        "**/api/documents*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"documents": [], "count": 0}',
        ),
    )
    # Jobs now owns application readiness. Most workflow tests exercise a
    # narrower Jobs behavior and register their own API route afterwards; a
    # fallback must still receive a deterministic, non-actionable readiness
    # response instead of reaching the live server with the fake token.
    page.route(
        "**/api/jobs/*/apply/readiness*",
        lambda route: _json(route, {
            "ready": False,
            "blockers": [{
                "code": "reviewed_packet_missing",
                "message": "A reviewed application packet is required.",
            }],
            "attempts": [],
            "target": {
                "url": "",
                "host": "",
                "classification": "invalid",
                "open_allowed": False,
                "reason_code": "invalid_application_url",
            },
            "packet": None,
            "config": {"available": False, "enabled": False, "kill_switch_active": True},
            "actions": {"manual_open": False, "dry_run": False, "submit": False},
        }),
    )
    console_errors, page_errors = console_collector(page)
    request_failures, bad_responses = network_collector(page)
    return console_errors, page_errors, request_failures, bad_responses


def _goto_authenticated(page, route: str) -> None:
    page.goto(f"{BASE_URL}{route}", wait_until="domcontentloaded")
    wait_for_network_settle(page)
    expect(page.locator("#auth-gate")).to_have_count(0)


def _run_palette_action(page, query: str) -> None:
    page.keyboard.press("Meta+K")
    page.wait_for_selector("#cmdk:not([hidden])")
    page.locator(".cmdk-input").fill(query)
    expect(page.locator(".cmdk-item").first).to_be_visible()
    page.keyboard.press("Enter")
    expect(page.locator("#cmdk")).to_be_hidden()


def _job_payload(has_packet: bool) -> dict:
    return {
        "job": {
            "id": JOB_ID,
            "title": "Senior Data Engineer",
            "company": "Acme GmbH",
            "status": "open",
            "jd_text": JD_TEXT,
            "field": "Data",
            "confidence": 82,
            "deadline": None,
            "why_fit": "Strong data platform match.",
            "source_state": {
                "state": "fresh",
                "stale": False,
                "full_jd_status": "reviewed",
                "requires_full_jd_review": False,
                "revalidation_mode": "manual_review_only",
            },
            "experience_fit": {
                "required_years": 5,
                "candidate_years": 4.5,
                "missing_years": 0.5,
                "fit": "near",
                "ranking_contribution": {
                    "bucket": "near",
                    "weighted_score": 13.5,
                    "reason": "Candidate years are close to requirement.",
                    "evidence": ["5 years required", "4.5 years candidate evidence"],
                },
            },
        },
        "cvs": ([{"id": TAILORED_CV_ID, "name": "Source CV - reviewed for Acme", "updated_at": "now"}] if has_packet else []),
        "cover_letters": ([{"id": LETTER_ID, "name": "Acme cover letter", "company": "Acme GmbH", "role": "Senior Data Engineer", "updated_at": "now"}] if has_packet else []),
        "packets": ([_packet_payload()] if has_packet else []),
    }


def _packet_payload(**overrides) -> dict:
    payload = {
        "id": PACKET_ID,
        "job_id": JOB_ID,
        "source_cv_id": CV_ID,
        "tailored_cv_id": TAILORED_CV_ID,
        "cover_letter_id": LETTER_ID,
        "status": "packet_created",
        "review_status": "reviewed",
        "score_system_id": "semantic_fit",
        "prompt_version": "jobs_packet.v1",
        "score_snapshot": {"score": 88},
        "source_cv_hash": "a" * 64,
        "job_content_hash": "b" * 64,
        "tailored_cv_hash": "c" * 64,
        "cover_letter_hash": "d" * 64,
        "cover_letter_review_status": "confirmed",
        "cover_letter_review_confirmed_at": "2026-07-02T12:00:00+00:00",
        "cover_letter_review_id": "review-job",
        "packet_version": 1,
        "document_links": {
            "source_cv": f"/app?id={CV_ID}&job_id={JOB_ID}&cv_id={CV_ID}&source_cv_id={CV_ID}",
            "tailored_cv": (
                f"/app?id={TAILORED_CV_ID}&job_id={JOB_ID}"
                f"&cv_id={TAILORED_CV_ID}&source_cv_id={CV_ID}"
            ),
            "cover_letter": (
                f"/cover-letters?id={LETTER_ID}&job_id={JOB_ID}"
                f"&cv_id={TAILORED_CV_ID}&source_cv_id={CV_ID}"
            ),
        },
    }
    payload.update(overrides)
    return payload


def _packet_lineage_payload(**overrides) -> dict:
    packet = _packet_payload()
    payload = {
        "packet_id": packet["id"],
        "job_id": packet["job_id"],
        "job_title": "Senior Data Engineer",
        "job_company": "Acme GmbH",
        "document_role": "tailored_cv",
        "source_cv_id": packet["source_cv_id"],
        "tailored_cv_id": overrides.pop("document_id", CV_ID),
        "cover_letter_id": packet["cover_letter_id"],
        "source_cv_hash": packet["source_cv_hash"],
        "job_content_hash": packet["job_content_hash"],
        "tailored_cv_hash": packet["tailored_cv_hash"],
        "cover_letter_hash": packet["cover_letter_hash"],
        "score_system_id": packet["score_system_id"],
        "score": packet["score_snapshot"]["score"],
        "prompt_version": "jobs_packet.v1",
        "packet_version": packet["packet_version"],
        "review_id": packet["cover_letter_review_id"],
        "cover_letter_review_status": packet["cover_letter_review_status"],
        "cover_letter_review_confirmed_at": packet["cover_letter_review_confirmed_at"],
        "cover_letter_review_id": packet["cover_letter_review_id"],
        "status": packet["status"],
        "stored_status": packet["status"],
        "review_status": packet["review_status"],
        "stale_reasons": [],
        "archived_at": None,
        "document_links": packet["document_links"],
    }
    payload.update(overrides)
    return payload


def _profile_meta(profile: str) -> dict:
    label = PROFILE_LABELS[profile]
    return {
        "id": profile,
        "label": label,
        "purpose": f"{label} purpose.",
        "best_for": "Browser contract test.",
        "method": profile,
        "description": f"{label} simulation.",
        "explanation": "Transparent browser-fixture scoring only.",
        "rubric_preferred": profile == "semantic_fit",
        "disclaimer": "Transparent CV Copilot simulation; not certified by or backed by an ATS vendor.",
        "simulation": True,
        "vendor_backed": False,
    }


def _profiles_payload() -> dict:
    return {"profiles": [_profile_meta(p) for p in PROFILE_IDS], "default_profile": "semantic_fit"}


def _ats_payload(profile: str) -> dict:
    rows = {
        p: {
            "score": PROFILE_SCORES[p],
            "source": "deterministic",
            "tier1_coverage": 40,
            "tier1_target": 70,
            "matched": [{"term": "Spark", "importance": "required"}],
            "gaps": [{"term": "Airflow", "importance": "required"}],
            "profile": _profile_meta(p),
        }
        for p in PROFILE_IDS
    }
    active = dict(rows[profile])
    active["profile_results"] = {p: rows[p] for p in PROFILE_IDS if p != profile}
    return active


def _expected_profile_body(profile: str, *, job_id: str | None = None, include_jd_text: bool = True) -> dict:
    body = {
        "cv_id": CV_ID,
        "profile": profile,
        "compare_profiles": [p for p in PROFILE_IDS if p != profile],
    }
    if include_jd_text:
        body["jd_text"] = JD_TEXT
    if job_id:
        body["job_id"] = job_id
    return body


def test_jobs_generation_links_packet_rows_and_pdf_actions(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    generated = {"value": False, "builds": [], "confirm_calls": []}
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            parsed = urlparse(req.url)
            path = parsed.path
            if path == "/api/jobs":
                _json(route, {
                    "jobs": [{
                        "id": JOB_ID,
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "status": "open",
                        "field": "Data",
                        "deadline": None,
                        "confidence": 82,
                        "why_fit": "Strong data platform match.",
                        "has_cover": generated["value"],
                        "cover_id": LETTER_ID if generated["value"] else None,
                    }]
                })
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, _job_payload(generated["value"]))
            elif path == f"/api/jobs/{JOB_ID}/review" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["source_cv_id"] == CV_ID
                _json(route, {
                    "review_token": "signed.generate.review",
                    "review_id": "review-generate",
                    "source_cv_id": CV_ID,
                    "source_hash": "a" * 64,
                    "source_revision": "now",
                    "jd_hash": "b" * 64,
                    "tailored_hash": "c" * 64,
                    "provider": "anthropic",
                    "model": "claude-test",
                    "created_at": "2026-07-02T12:00:00+00:00",
                    "job_id": JOB_ID,
                    "name": "Source CV - reviewed for Acme",
                    "diff": {"changed": True, "added": 1, "removed": 1, "lines": ["--- source-cv.yaml", "+++ reviewed-copy.yaml", "+Spark and Airflow"]},
                    "ats": {"score": 88, "experience_fit": {"candidate_years": 4.5, "required_years": 5, "fit": "near"}},
                })
            elif path == f"/api/jobs/{JOB_ID}/apply-review" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["source_cv_id"] == CV_ID
                assert body["review_token"] == "signed.generate.review"
                assert "cover_letter_review_confirmed" not in body
                _json(route, {
                    "tailored_cv_id": TAILORED_CV_ID,
                    "cover_letter_id": LETTER_ID,
                    "packet_id": PACKET_ID,
                    "cover_letter_hash": "d" * 64,
                    "cover_letter_review_status": "pending",
                    "requires_cover_letter_review": True,
                    "ats": {"score": 88},
                }, status=201)
            elif path == f"/api/jobs/{JOB_ID}/packets/{PACKET_ID}/confirm-cover-letter-review" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["cover_letter_hash"] == "d" * 64
                generated["confirm_calls"].append(body)
                generated["value"] = True
                _json(route, {
                    "packet_id": PACKET_ID,
                    "cover_letter_review_status": "confirmed",
                    "cover_letter_review_confirmed_at": "2026-07-02T12:00:00+00:00",
                })
            elif path == "/api/build":
                generated["builds"].append(json.loads(req.post_data or "{}"))
                _pdf(route)
            elif path == f"/api/cover-letter/{LETTER_ID}/build":
                generated["builds"].append({"letter": LETTER_ID})
                _pdf(route)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        wait_for_network_settle(page)
        page.locator("#jb-generate").click()
        expect(page.locator("#jb-review-panel")).to_be_visible()
        page.locator("#jb-apply-review").click()
        expect(page.locator("#jb-gen-feedback")).to_contain_text("Cover letter generated")
        page.locator("#jb-apply-review").click()
        expect(page.locator("#jb-gen-feedback")).to_contain_text("Review and confirm the cover-letter grounding")
        page.locator("#jb-approve-cover-review").check()
        page.locator("#jb-apply-review").click()
        expect(page.locator("#jb-cvs .jb-linked-row")).to_have_count(1)
        expect(page.locator("#jb-letters .jb-linked-row")).to_have_count(1)
        _assert_application_context_href(
            page.locator("#jb-cvs .jb-linked-open"),
            path="/app", document_id=TAILORED_CV_ID, job_id=JOB_ID,
            cv_id=TAILORED_CV_ID, source_cv_id=CV_ID,
        )
        _assert_application_context_href(
            page.locator("#jb-letters .jb-linked-open"),
            path="/cover-letters", document_id=LETTER_ID, job_id=JOB_ID,
            cv_id=TAILORED_CV_ID, source_cv_id=CV_ID,
        )
        assert_no_horizontal_overflow(page)
        assert_no_text_overflow(page, [".jb-linked-row", ".jb-generate-btn", "#jb-gen-feedback"])
        assert_inside_viewport(page, [".jb-linked-row", ".jb-linked-pdf"])
        page.locator("#jb-cvs .jb-linked-pdf").click()
        page.locator("#jb-letters .jb-linked-pdf").click()
        page.wait_for_timeout(250)
        assert {"id": TAILORED_CV_ID} in generated["builds"]
        assert {"letter": LETTER_ID} in generated["builds"]
        targets = page.evaluate("window.__openedPdfTargets")
        assert len(targets) >= 2
        assert all(target != "about:blank" for target in targets)
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses, allow_statuses=(409,))
    finally:
        context.close()


def test_jobs_repository_star_status_delete_and_palette_focus(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    job = {
        "id": JOB_ID,
        "title": "Senior Data Engineer",
        "company": "Acme GmbH",
        "status": "open",
        "field": "Data",
        "deadline": None,
        "confidence": 82,
        "why_fit": "Strong data platform match.",
        "starred": False,
    }
    deleted = {"value": False}
    patch_bodies: list[dict] = []
    status_bodies: list[dict] = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))
        add_top_document_init_script(page, "window.confirm = () => true;")

        def detail_payload() -> dict:
            payload = _job_payload(False)
            payload["job"].update(job)
            return payload

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": ([] if deleted["value"] else [job]), "count": 0 if deleted["value"] else 1})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                if deleted["value"]:
                    _json(route, {"error": "missing"}, status=404)
                else:
                    _json(route, detail_payload())
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "PATCH":
                body = json.loads(req.post_data or "{}")
                patch_bodies.append(body)
                job["starred"] = body.get("starred") is True
                updated = dict(job)
                updated["source_metadata"] = {"starred": job["starred"], "origin": "local_jobs_db_read_only"}
                _json(route, updated)
            elif path == f"/api/jobs/{JOB_ID}/status" and req.method == "POST":
                status_bodies.append(json.loads(req.post_data or "{}"))
                _json(route, {"error": "simulated status write failure"}, status=500)
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "DELETE":
                deleted["value"] = True
                _json(route, {"deleted": JOB_ID})
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")

        page.locator(".jb-card-star").click()
        expect(page.locator("#jb-star")).to_have_attribute("aria-pressed", "true")
        assert patch_bodies[-1] == {"starred": True}

        page.locator("#jb-status-select").select_option("closed")
        expect(page.locator("#jb-status-select")).to_have_value("open")
        expect(page.locator("#jb-gen-feedback")).to_contain_text("simulated status write failure")
        assert status_bodies[-1] == {"status": "closed"}
        expect(page.locator(".jb-badge-open").first).to_contain_text(re.compile("open", re.I))

        _run_palette_action(page, "Add job")
        assert page.locator("#jb-add-details").evaluate("node => node.open") is True
        assert page.evaluate("document.activeElement && document.activeElement.id") == "jb-title"

        page.locator("#jb-delete").click()
        expect(page.locator("#jb-grid .jb-card")).to_have_count(0)
        expect(page.locator("#jb-detail-empty")).to_be_visible()
        console_errors[:] = [err for err in console_errors if "status of 500" not in err]
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses, allow_statuses=(500,))
    finally:
        context.close()


def test_jobs_status_select_serializes_writes(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    status_bodies: list[dict] = []
    held_routes = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [_job_payload(False)["job"]], "count": 1})
            elif path == "/api/cvs" and req.method == "GET":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV"}]})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, _job_payload(False))
            elif path == f"/api/jobs/{JOB_ID}/status" and req.method == "POST":
                status_bodies.append(json.loads(req.post_data or "{}"))
                held_routes.append(route)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        page.locator("#jb-status-select").select_option("speculative")
        expect(page.locator("#jb-status-select")).to_be_disabled()
        assert status_bodies == [{"status": "speculative"}]

        page.locator("#jb-status-select").evaluate(
            "node => { node.value = 'closed'; node.dispatchEvent(new Event('change', { bubbles: true })); }"
        )
        page.wait_for_timeout(50)
        assert status_bodies == [{"status": "speculative"}]

        held_routes[0].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "speculative"}),
        )
        expect(page.locator("#jb-status-select")).to_be_enabled()
        expect(page.locator("#jb-status-select")).to_have_value("speculative")
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_jobs_mobile_long_text_wraps_without_detail_overlap(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    long = "SeniorDataEngineer" * 16
    job = {
        "id": JOB_ID,
        "title": long,
        "company": long,
        "status": "open",
        "field": "Data",
        "deadline": None,
        "confidence": 82,
        "why_fit": long,
        "source_provider": "linkedin",
        "verification_status": "stale",
        "last_seen_at": long,
        "source_metadata": {"origin": long, "site": "linkedin"},
    }
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(360, 740))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [job], "count": 1})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                payload = _job_payload(False)
                payload["job"].update(job)
                payload["job"]["source_state"] = {
                    "state": "stale",
                    "stale": True,
                    "full_jd_status": "missing",
                    "requires_full_jd_review": True,
                    "last_seen_at": long,
                    "verified_at": long,
                    "revalidation_mode": long,
                }
                _json(route, payload)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        metrics = page.evaluate(
            """
            () => {
              const box = (selector) => {
                const el = document.querySelector(selector);
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {
                  top: r.top,
                  bottom: r.bottom,
                  width: r.width,
                  scrollWidth: el.scrollWidth,
                  clientWidth: el.clientWidth,
                };
              };
              return {
                bodyScrollWidth: document.documentElement.scrollWidth,
                viewportWidth: window.innerWidth,
                sidebar: box(".jb-search-sidebar"),
                detail: box(".jb-detail-pane"),
                grid: box("#jb-grid"),
                card: box(".jb-card"),
                why: box(".jb-why"),
                sourceStatus: box("#jb-source-revalidation-status"),
                experience: box("#jb-experience-fit"),
              };
            }
            """
        )
        assert metrics["bodyScrollWidth"] <= metrics["viewportWidth"] + 1, metrics
        assert metrics["sidebar"]["bottom"] <= metrics["detail"]["top"] + 1, metrics
        for key in ("grid", "card", "why", "sourceStatus", "experience"):
            assert metrics[key]["scrollWidth"] <= metrics[key]["clientWidth"] + 1, (key, metrics[key], metrics)
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_jobs_review_first_flow_links_packet_rows(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    generated = {"value": False, "apply_calls": [], "confirm_calls": []}
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            parsed = urlparse(req.url)
            path = parsed.path
            if path == "/api/jobs":
                _json(route, {
                    "jobs": [{
                        "id": JOB_ID,
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "status": "open",
                        "field": "Data",
                        "deadline": None,
                        "confidence": 82,
                        "why_fit": "Strong data platform match.",
                        "has_cover": generated["value"],
                        "cover_id": LETTER_ID if generated["value"] else None,
                    }]
                })
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, _job_payload(generated["value"]))
            elif path == f"/api/jobs/{JOB_ID}/review" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["source_cv_id"] == CV_ID
                _json(route, {
                    "review_token": "signed.job.review",
                    "review_id": "review-job",
                    "source_cv_id": CV_ID,
                    "source_hash": "a" * 64,
                    "source_revision": "now",
                    "jd_hash": "b" * 64,
                    "tailored_hash": "c" * 64,
                    "provider": "anthropic",
                    "model": "claude-test",
                    "created_at": "2026-07-02T12:00:00+00:00",
                    "job_id": JOB_ID,
                    "name": "Source CV - reviewed for Acme",
                    "summary": "2 changes proposed. ATS 88%.",
                    "diff": {"lines": ["--- source-cv.yaml", "+++ reviewed-copy.yaml", "-old bullet", "+new bullet"]},
                    "ats": {"score": 88, "experience_fit": {"candidate_years": 4.5, "required_years": 5, "fit": "near"}},
                })
            elif path == f"/api/jobs/{JOB_ID}/apply-review" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["source_cv_id"] == CV_ID
                assert body["review_token"] == "signed.job.review"
                generated["apply_calls"].append(body)
                _json(route, {
                    "tailored_cv_id": TAILORED_CV_ID,
                    "cover_letter_id": LETTER_ID,
                    "packet_id": PACKET_ID,
                    "cover_letter_hash": "d" * 64,
                    "cover_letter_review_status": "pending",
                    "requires_cover_letter_review": True,
                    "ats": {"score": 88},
                }, status=201)
            elif path == f"/api/jobs/{JOB_ID}/packets/{PACKET_ID}/confirm-cover-letter-review" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["cover_letter_hash"] == "d" * 64
                generated["confirm_calls"].append(body)
                generated["value"] = True
                _json(route, {
                    "packet_id": PACKET_ID,
                    "cover_letter_review_status": "confirmed",
                    "cover_letter_review_confirmed_at": "2026-07-02T12:00:00+00:00",
                })
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        page.locator("#jb-generate").click()
        expect(page.locator("#jb-review-panel")).to_be_visible()
        expect(page.locator("#jb-review-diff")).to_contain_text("+new bullet")
        assert_no_text_overflow(page, ["#jb-review-panel", "#jb-review-summary", "#jb-review-diff"])
        page.locator("#jb-apply-review").click()
        expect(page.locator("#jb-gen-feedback")).to_contain_text("Cover letter generated")
        assert generated["apply_calls"]
        expect(page.locator("#jb-review-panel")).to_be_visible()
        page.locator("#jb-apply-review").click()
        expect(page.locator("#jb-gen-feedback")).to_contain_text("Review and confirm the cover-letter grounding")
        assert generated["confirm_calls"] == []
        page.locator("#jb-approve-cover-review").check()
        page.locator("#jb-apply-review").click()
        assert generated["confirm_calls"][-1]["cover_letter_hash"] == "d" * 64
        expect(page.locator("#jb-cvs .jb-linked-row")).to_have_count(1)
        expect(page.locator("#jb-letters .jb-linked-row")).to_have_count(1)
        _assert_application_context_href(
            page.locator("#jb-cvs .jb-linked-open"),
            path="/app", document_id=TAILORED_CV_ID, job_id=JOB_ID,
            cv_id=TAILORED_CV_ID, source_cv_id=CV_ID,
        )
        _assert_application_context_href(
            page.locator("#jb-letters .jb-linked-open"),
            path="/cover-letters", document_id=LETTER_ID, job_id=JOB_ID,
            cv_id=TAILORED_CV_ID, source_cv_id=CV_ID,
        )
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_jobs_reload_restores_pending_cover_review(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    state = {"confirmed": False, "confirm_bodies": []}
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def detail_payload() -> dict:
            payload = _job_payload(True)
            if not state["confirmed"]:
                payload["packets"] = [_packet_payload(
                    status="review_applied",
                    stored_status="review_applied",
                    cover_letter_review_status="pending",
                    cover_letter_review_confirmed_at=None,
                )]
            return payload

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [detail_payload()["job"]], "count": 1})
            elif path == "/api/cvs" and req.method == "GET":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV"}]})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, detail_payload())
            elif path == f"/api/jobs/{JOB_ID}/packets/{PACKET_ID}/confirm-cover-letter-review" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                state["confirm_bodies"].append(body)
                state["confirmed"] = True
                _json(route, {
                    "packet_id": PACKET_ID,
                    "cover_letter_review_status": "confirmed",
                    "cover_letter_review_confirmed_at": "2026-08-01T12:00:00+00:00",
                })
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")

        expect(page.locator("#jb-review-panel")).to_be_visible()
        expect(page.locator("#jb-cover-review-panel")).to_be_visible()
        expect(page.locator("#jb-apply-review .jb-apply-label")).to_have_text("Confirm review")
        _assert_application_context_href(
            page.locator("#jb-cover-grounding-status a"),
            path="/cover-letters", document_id=LETTER_ID, job_id=JOB_ID,
            cv_id=TAILORED_CV_ID, source_cv_id=CV_ID,
        )
        expect(page.locator(f'.jb-packet-row[data-packet-id="{PACKET_ID}"]')).to_have_attribute(
            "data-packet-state", "review_applied"
        )

        page.locator("#jb-apply-review").click()
        expect(page.locator("#jb-gen-feedback")).to_contain_text("Review and confirm")
        assert state["confirm_bodies"] == []
        page.locator("#jb-approve-cover-review").check()
        page.locator("#jb-apply-review").click()

        expect(page.locator("#jb-review-panel")).to_be_hidden()
        expect(page.locator(f'.jb-packet-row[data-packet-id="{PACKET_ID}"]')).to_have_attribute(
            "data-packet-state", "packet_created"
        )
        assert state["confirm_bodies"] == [{"cover_letter_hash": "d" * 64}]
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_jobs_packet_state_renders_no_packet_stale_applied_regenerated_and_archived_states(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    state = {"job_status": "open", "packet": None}
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/jobs":
                _json(route, {"jobs": [{"id": JOB_ID, "title": "Senior Data Engineer", "company": "Acme GmbH", "status": state["job_status"], "has_cover": True, "cover_id": LETTER_ID}]})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                payload = _job_payload(True)
                payload["job"]["status"] = state["job_status"]
                payload["packets"] = [state["packet"]] if state["packet"] else []
                _json(route, payload)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)

        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        expect(page.locator("#jb-packet-state .jb-packet-row")).to_have_attribute("data-packet-state", "none")
        expect(page.locator("#jb-packet-state")).to_contain_text("No packet")

        state["packet"] = _packet_payload(status="stale")
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        expect(page.locator("#jb-packet-state .jb-packet-row")).to_have_attribute("data-packet-state", "stale")
        expect(page.locator("#jb-packet-state")).to_contain_text("Stale packet")
        expect(page.locator("#jb-cvs .jb-linked-row")).to_have_attribute("data-packet-state", "stale")

        state["job_status"] = "applied"
        state["packet"] = _packet_payload(status="packet_created")
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        expect(page.locator("#jb-packet-state .jb-packet-row")).to_have_attribute("data-packet-state", "packet_created")
        expect(page.locator("#jb-packet-state")).to_contain_text("Packet")
        expect(page.locator("#jb-cvs .jb-linked-row")).to_have_attribute("data-packet-state", "packet_created")
        expect(page.locator("#jb-cvs .jb-linked-row")).to_have_attribute("data-packet-usable", "true")
        expect(page.locator("#jb-cvs .jb-linked-row")).to_have_attribute("data-score-system-id", "semantic_fit")

        state["job_status"] = "open"
        state["packet"] = _packet_payload(status="packet_created", packet_version=2)
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        expect(page.locator("#jb-packet-state .jb-packet-row")).to_have_attribute("data-packet-state", "regenerated")
        expect(page.locator("#jb-packet-state")).to_contain_text("Packet")
        expect(page.locator("#jb-packet-state")).to_contain_text("v2")
        expect(page.locator("#jb-packet-state")).to_contain_text("semantic_fit")
        expect(page.locator("#jb-cvs .jb-linked-row")).to_have_attribute("data-packet-state", "regenerated")
        expect(page.locator("#jb-cvs .jb-linked-row")).to_have_attribute("data-packet-usable", "true")
        expect(page.locator("#jb-cvs")).to_contain_text("Packet v2")

        state["packet"] = _packet_payload(status="archived", archived_at="2026-07-02T13:00:00+00:00")
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        expect(page.locator("#jb-packet-state .jb-packet-row")).to_have_attribute("data-packet-state", "archived")
        expect(page.locator("#jb-packet-state")).to_contain_text("Archived packet")
        expect(page.locator("#jb-packet-state [data-packet-action]")).to_have_count(0)
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_jobs_regenerate_action_opens_review_panel(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    calls = {"regenerate": 0}
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/jobs":
                _json(route, {"jobs": [{"id": JOB_ID, "title": "Senior Data Engineer", "company": "Acme GmbH", "status": "open", "has_cover": True, "cover_id": LETTER_ID}]})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, _job_payload(True))
            elif path == f"/api/jobs/{JOB_ID}/packets/{PACKET_ID}/regenerate" and req.method == "POST":
                calls["regenerate"] += 1
                _json(route, {
                    "review_token": "signed.regenerate.review",
                    "review_id": "review-regenerate",
                    "source_cv_id": CV_ID,
                    "source_hash": "a" * 64,
                    "source_revision": "now",
                    "jd_hash": "b" * 64,
                    "tailored_hash": "e" * 64,
                    "provider": "anthropic",
                    "model": "claude-test",
                    "created_at": "2026-07-02T12:30:00+00:00",
                    "job_id": JOB_ID,
                    "packet_mode": "regenerate",
                    "replaces_packet_id": PACKET_ID,
                    "regenerates_packet_id": PACKET_ID,
                    "name": "Source CV - regenerated for Acme",
                    "diff": {"lines": ["--- v1.yaml", "+++ v2.yaml", "-old packet bullet", "+replacement packet bullet"]},
                    "ats": {"score": 89, "experience_fit": {"candidate_years": 4.5, "required_years": 5, "fit": "near"}},
                }, status=201)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        expect(page.locator("#jb-packet-state .jb-packet-row")).to_have_attribute("data-packet-state", "packet_created")
        page.get_by_role("button", name="Regenerate").click()
        expect(page.locator("#jb-review-panel")).to_be_visible()
        expect(page.locator("#jb-review-diff")).to_contain_text("+replacement packet bullet")
        expect(page.locator("#jb-gen-feedback")).to_contain_text("Regeneration review ready")
        assert calls["regenerate"] == 1
        assert_no_horizontal_overflow(page)
        assert_no_text_overflow(page, ["#jb-review-panel", "#jb-review-summary", "#jb-review-diff", "#jb-gen-feedback"])
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", E2E_VIEWPORTS, ids=lambda v: v.label)
def test_tailor_review_apply_report_is_unclipped(browser, viewport: Viewport):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    applied = {"value": False}
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, viewport)

        def handle_api(route):
            req = route.request
            parsed = urlparse(req.url)
            path = parsed.path
            if path.endswith("/tailor/review"):
                _json(route, {
                    "review_token": "signed.review",
                    "source_cv_id": CV_ID,
                    "review_id": "review-1",
                    "name": "Source CV - reviewed",
                    "diff": {
                        "changed": True,
                        "added": 1,
                        "removed": 1,
                        "truncated": False,
                        "lines": ["--- source-cv.yaml", "+++ reviewed-copy.yaml", "-old bullet", "+new bullet"],
                    },
                    "ats": {"score": 88, "source": "deterministic", "tier1_coverage": 100, "tier1_target": 70, "gaps": [], "matched": []},
                    "gaps": [],
                    "applied": ["rewrite"],
                    "warnings": [],
                })
            elif path.endswith("/tailor/apply"):
                body = json.loads(req.post_data or "{}")
                assert body["review_token"] == "signed.review"
                applied["value"] = True
                _json(route, {
                    "tailored_cv_id": TAILORED_CV_ID,
                    "name": "Source CV - reviewed",
                    "ats": {"score": 88, "source": "deterministic", "gaps": [], "matched": []},
                    "gaps": [],
                    "applied": ["rewrite"],
                    "warnings": [],
                }, status=201)
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            elif path == "/api/build":
                _pdf(route)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, "/tailor")
        page.locator("#tl-jd").fill("Title: Senior Data Engineer\\nCompany: Acme GmbH\\nSpark and Airflow required.")
        _run_palette_action(page, "review changes")
        expect(page.locator("#tl-review")).to_be_visible()
        expect(page.locator("#tl-review-diff")).to_contain_text("+new bullet")
        assert_no_horizontal_overflow(page)
        assert_no_text_overflow(page, ["#tl-review", "#tl-review-summary", ".tl-gauge-actions"])
        page.locator("#tl-apply-review").scroll_into_view_if_needed()
        assert_inside_viewport(page, ["#tl-apply-review"])
        _run_palette_action(page, "apply as new copy")
        expect(page.locator("#tl-open-tailored")).to_be_visible()
        expect(page.locator("#tl-build-tailored")).to_be_visible()
        assert applied["value"] is True
        page.locator("#tl-build-tailored").click()
        page.wait_for_timeout(250)
        targets = page.evaluate("window.__openedPdfTargets")
        assert targets and all(target != "about:blank" for target in targets)
        if viewport.width <= 840:
            main_scroll = page.evaluate(
                """
                () => {
                  const main = document.querySelector(".cl-main");
                  return {
                    rectLeft: Math.round(main.getBoundingClientRect().left),
                    scrollLeft: main.scrollLeft,
                    overflowX: getComputedStyle(main).overflowX,
                  };
                }
                """
            )
            assert main_scroll["rectLeft"] == 0
            assert main_scroll["scrollLeft"] == 0
            # CSS Overflow computes `clip` to `hidden` when the other axis is
            # scrollable. Both values enforce no horizontal user scrolling.
            assert main_scroll["overflowX"] in {"clip", "hidden"}
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


@pytest.mark.parametrize("profile", PROFILE_IDS)
def test_tailor_profiles_post_selected_score_system_and_render_transparency(browser, profile):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    score_bodies: list[dict] = []
    review_bodies: list[dict] = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/ats-profiles":
                _json(route, _profiles_payload())
            elif path == "/api/cvs":
                _json(route, {"cvs": [{
                    "id": CV_ID,
                    "name": "Source CV",
                    "job_id": JOB_ID,
                    "job_title": "Senior Data Engineer",
                    "packet_lineage": _packet_lineage_payload(document_id=CV_ID),
                }]})
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            elif path == f"/api/jobs/{JOB_ID}":
                _json(route, _job_payload(False))
            elif path == "/api/ats-score" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                score_bodies.append(body)
                _json(route, _ats_payload(body["profile"]))
            elif path.endswith("/tailor/review") and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                review_bodies.append(body)
                _json(route, {
                    "review_token": "signed.review",
                    "review_id": "review-tailor",
                    "job_id": JOB_ID,
                    "source_cv_id": CV_ID,
                    "source_hash": "a" * 64,
                    "source_revision": "rev",
                    "jd_hash": "b" * 64,
                    "tailored_hash": "c" * 64,
                    "provider": "anthropic",
                    "model": "claude-test",
                    "created_at": "2026-07-02T12:00:00+00:00",
                    "diff": {"changed": True, "added": 1, "removed": 0, "lines": ["+Spark and Airflow"]},
                    "ats": _ats_payload(body["profile"]),
                    "gaps": [],
                    "applied": [],
                    "warnings": [],
                })
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/tailor?id={CV_ID}&job_id={JOB_ID}")
        page.locator("#tl-profile").select_option(profile)
        expect(page.locator("#tl-jd")).to_have_value(JD_TEXT)
        expect(page.locator("#tl-selected-job-context")).to_contain_text("Senior Data Engineer")
        expect(page.locator("#tl-selected-job-context")).to_contain_text("Acme GmbH")
        page.locator("#tl-score").click()

        expect(page.locator("#tl-score-num")).to_have_text(str(PROFILE_SCORES[profile]))
        expect(page.locator("#tl-profile-comparison .tl-profile-row")).to_have_count(3)
        expect(page.locator("#tl-profile-comparison .tl-profile-row.is-active")).to_contain_text(PROFILE_LABELS[profile])
        expect(page.locator("#tl-profile-comparison")).to_contain_text("Simulations, not vendor-backed")
        expect(page.locator("#tl-profile-comparison")).to_contain_text("CV Copilot simulation")
        expect(page.locator("#tl-profile-comparison")).to_contain_text("Not vendor-backed")
        expect(page.locator(".tl-profile-note")).to_contain_text("not certified by or backed by an ATS vendor")
        assert score_bodies[-1] == _expected_profile_body(profile, job_id=JOB_ID, include_jd_text=False)

        page.locator("#tl-tailor").click()
        expect(page.locator("#tl-review")).to_be_visible()
        expect(page.locator("#tl-review-lineage")).to_contain_text("Review review-tailor")
        expect(page.locator("#tl-review-lineage")).to_contain_text("source aaaaaaaaaa")
        expect(page.locator("#tl-review-lineage")).to_contain_text("JD bbbbbbbbbb")
        expect(page.locator("#tl-review-lineage")).to_contain_text("tailored cccccccccc")
        expect(page.locator("#tl-review-lineage")).to_contain_text("anthropic / claude-test")
        assert review_bodies[-1] == _expected_profile_body(profile, job_id=JOB_ID, include_jd_text=False)

        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_tailor_profile_change_invalidates_inflight_score(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    score_bodies: list[dict] = []
    held_routes = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/ats-profiles":
                _json(route, _profiles_payload())
            elif path == "/api/cvs":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV", "job_id": JOB_ID}]})
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            elif path == f"/api/jobs/{JOB_ID}":
                _json(route, _job_payload(False))
            elif path == "/api/ats-score" and req.method == "POST":
                score_bodies.append(json.loads(req.post_data or "{}"))
                held_routes.append(route)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/tailor?cv_id={CV_ID}&job_id={JOB_ID}")
        expect(page.locator("#tl-jd")).to_have_value(JD_TEXT)

        page.locator("#tl-profile").select_option("semantic_fit")
        page.locator("#tl-score").click()
        expect(page.locator("#tl-score")).to_be_disabled()
        assert score_bodies[-1]["profile"] == "semantic_fit"

        page.locator("#tl-profile").select_option("strict_requirements")
        expect(page.locator("#tl-score")).to_be_enabled()
        expect(page.locator("#tl-empty")).to_be_visible()
        expect(page.locator("#tl-score-num")).to_have_text("—")

        held_routes[0].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_ats_payload("semantic_fit")),
        )
        page.wait_for_timeout(100)
        expect(page.locator("#tl-empty")).to_be_visible()
        expect(page.locator("#tl-score-num")).to_have_text("—")
        expect(page.locator("#tl-profile-comparison")).to_be_hidden()
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_tailor_palette_and_url_detach_do_not_use_stale_selected_job(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    score_bodies: list[dict] = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/ats-profiles":
                _json(route, _profiles_payload())
            elif path == "/api/cvs":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV", "job_id": JOB_ID, "job_title": "Senior Data Engineer"}]})
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            elif path == f"/api/jobs/{JOB_ID}":
                _json(route, _job_payload(False))
            elif path == "/api/ats-score" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                score_bodies.append(body)
                _json(route, _ats_payload("semantic_fit"))
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/tailor?cv_id={CV_ID}&job_id={JOB_ID}")
        expect(page.locator("#tl-jd")).to_have_value(JD_TEXT)
        page.keyboard.press("Meta+K")
        page.wait_for_selector("#cmdk:not([hidden])")
        page.locator(".cmdk-input").fill("apply as new copy")
        expect(page.locator(".cmdk-item")).to_have_count(0)
        page.keyboard.press("Escape")

        page.locator("#tl-jd-url").fill("https://example.com/new-job")
        expect(page.locator("#tl-jd")).to_have_value("")
        expect(page.locator("#tl-selected-job-context")).to_be_hidden()
        assert "job_id=" not in page.url
        page.locator("#tl-score").click()
        expect(page.locator("#tl-score-num")).to_have_text(str(PROFILE_SCORES["semantic_fit"]))
        assert score_bodies[-1]["jd_url"] == "https://example.com/new-job"
        assert "jd_text" not in score_bodies[-1]
        assert "job_id" not in score_bodies[-1]
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_tailor_saved_job_dropdown_scores_against_job_id(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    score_bodies: list[dict] = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/ats-profiles":
                _json(route, _profiles_payload())
            elif path == "/api/cvs":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV"}]})
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [{
                    "id": JOB_ID,
                    "title": "Senior Data Engineer",
                    "company": "Acme GmbH",
                    "status": "open",
                    "starred": True,
                }], "count": 1})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, _job_payload(False))
            elif path == "/api/ats-score" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                score_bodies.append(body)
                _json(route, _ats_payload("semantic_fit"))
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/tailor?cv_id={CV_ID}")
        expect(page.locator("#tl-job")).to_be_visible()
        page.locator("#tl-job").select_option(JOB_ID)
        expect(page).to_have_url(re.compile(r".*[?&]job_id=" + re.escape(JOB_ID)))
        expect(page.locator("#tl-jd")).to_have_value(JD_TEXT)
        expect(page.locator("#tl-selected-job-context")).to_contain_text("Senior Data Engineer")
        page.locator("#tl-score").click()

        expect(page.locator("#tl-score-num")).to_have_text(str(PROFILE_SCORES["semantic_fit"]))
        assert score_bodies[-1] == _expected_profile_body("semantic_fit", job_id=JOB_ID, include_jd_text=False)
        page.locator("#tl-jd").fill("Manual override job description")
        expect(page.locator("#tl-job")).to_have_value("")
        assert "job_id=" not in page.url
        page.locator("#tl-score").click()
        expect(page.locator("#tl-score-num")).to_have_text(str(PROFILE_SCORES["semantic_fit"]))
        assert score_bodies[-1]["jd_text"] == "Manual override job description"
        assert "job_id" not in score_bodies[-1]
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_tailor_cv_linked_job_syncs_url_and_dropdown(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    alt_cv_id = "55555555-5555-4555-8555-555555555555"
    alt_job_id = "66666666-6666-4666-8666-666666666666"
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        jobs = {
            JOB_ID: {
                "id": JOB_ID,
                "title": "Senior Data Engineer",
                "company": "Acme GmbH",
                "status": "open",
                "starred": True,
                "jd_text": JD_TEXT,
            },
            alt_job_id: {
                "id": alt_job_id,
                "title": "ML Platform Lead",
                "company": "Beta GmbH",
                "status": "open",
                "starred": False,
                "jd_text": "Title: ML Platform Lead\nCompany: Beta GmbH\nOwn model deployment platforms.",
            },
        }

        def job_detail(job_id: str) -> dict:
            payload = _job_payload(False)
            payload["job"].update(jobs[job_id])
            return payload

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/ats-profiles":
                _json(route, _profiles_payload())
            elif path == "/api/cvs":
                _json(route, {"cvs": [
                    {"id": CV_ID, "name": "Source CV", "job_id": JOB_ID, "job_title": "Senior Data Engineer"},
                    {"id": alt_cv_id, "name": "ML CV", "job_id": alt_job_id, "job_title": "ML Platform Lead"},
                ]})
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": list(jobs.values()), "count": len(jobs)})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, job_detail(JOB_ID))
            elif path == f"/api/jobs/{alt_job_id}" and req.method == "GET":
                _json(route, job_detail(alt_job_id))
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/tailor?cv_id={alt_cv_id}")
        expect(page).to_have_url(re.compile(r".*[?&]job_id=" + re.escape(alt_job_id)))
        expect(page.locator("#tl-job")).to_have_value(alt_job_id)
        expect(page.locator("#tl-jd")).to_have_value(re.compile("ML Platform Lead"))
        expect(page.locator("#tl-selected-job-context")).to_contain_text("ML Platform Lead")

        page.locator("#tl-cv").select_option(CV_ID)
        expect(page).to_have_url(re.compile(r".*[?&]job_id=" + re.escape(JOB_ID)))
        expect(page.locator("#tl-job")).to_have_value(JOB_ID)
        expect(page.locator("#tl-jd")).to_have_value(re.compile("Senior Data Engineer"))
        expect(page.locator("#tl-selected-job-context")).to_contain_text("Senior Data Engineer")
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_tailor_review_clears_stale_source_review(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path.endswith("/tailor/review"):
                _json(route, {
                    "review_token": "signed.review",
                    "source_cv_id": CV_ID,
                    "diff": {"changed": True, "added": 1, "removed": 0, "lines": ["+Spark"]},
                    "ats": _ats_payload("semantic_fit"),
                    "gaps": [],
                    "applied": [],
                    "warnings": [],
                })
            elif path.endswith("/tailor/apply") and req.method == "POST":
                _json(route, {"error": "source changed", "code": "source_changed", "hint": "Review the current CV before applying."}, status=409)
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, "/tailor")
        page.locator("#tl-jd").fill(JD_TEXT)
        page.locator("#tl-tailor").click()
        expect(page.locator("#tl-review")).to_be_visible()
        page.locator("#tl-apply-review").click()
        expect(page.locator("#tl-review")).to_be_hidden()
        expect(page.locator("#tl-feedback")).to_contain_text("Review the current CV before applying.")
        assert_clean_network(request_failures, bad_responses, allow_statuses=(409,))
    finally:
        context.close()


@pytest.mark.parametrize("profile", PROFILE_IDS)
def test_app_ats_profile_selector_posts_selected_system_and_vendor_copy(browser, profile):
    initial = next(p for p in PROFILE_IDS if p != profile)
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    score_bodies: list[dict] = []
    try:
        page.set_viewport_size({"width": 1440, "height": 900})
        add_top_document_init_script(page, f"""
        (() => {{
          for (const key of Object.keys(localStorage)) {{
            if (key.startsWith("cv.editor.") && !key.includes("supabase")) localStorage.removeItem(key);
          }}
          localStorage.setItem("cv.editor.tourCompleted", "true");
          localStorage.setItem("cv.editor.theme", "light");
          localStorage.setItem("onepager.lang", "en");
          localStorage.setItem("cv.editor.atsProfile", {json.dumps(initial)});
        }})();
        """)
        install_layout_vendor_routes(page)
        _install_auth_stub(page)
        console_errors, page_errors = console_collector(page)
        request_failures, bad_responses = network_collector(page)

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/ats-profiles":
                _json(route, _profiles_payload())
            elif path == "/api/cvs":
                _json(route, {"cvs": [{
                    "id": CV_ID,
                    "name": "Source CV",
                    "job_id": JOB_ID,
                    "job_title": "Senior Data Engineer",
                    "packet_lineage": _packet_lineage_payload(document_id=CV_ID),
                }]})
            elif path == "/api/cv":
                route.fulfill(status=200, headers={"X-OnePager-Revision": "rev"}, content_type="text/yaml", body="name: Source CV\nskills:\n  - label: Data\n    items: Spark, Airflow\n")
            elif path == f"/api/jobs/{JOB_ID}":
                payload = _job_payload(False)
                payload["packets"] = [_packet_payload(tailored_cv_id=CV_ID)]
                _json(route, payload)
            elif path == "/api/ats-score" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                score_bodies.append(body)
                _json(route, _ats_payload(body["profile"]))
            elif path in {"/api/preview", "/api/templates", "/api/themes", "/api/documents", "/api/analytics/event"}:
                _json(route, {"documents": [], "templates": [], "themes": []})
            else:
                _json(route, {})

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/app?id={CV_ID}")
        expect(page.locator("#ats-badge")).to_have_attribute("data-state", "ready")
        page.locator("#ats-badge").click()
        page.locator("#ats-profile-select").select_option(profile)

        expect(page.locator("#ats-badge .ats-badge-score")).to_have_text(str(PROFILE_SCORES[profile]))
        assert score_bodies[-1] == _expected_profile_body(profile, job_id=JOB_ID)
        expect(page.locator("#ats-detail .ats-profile-row")).to_have_count(3)
        expect(page.locator("#ats-detail .ats-profile-row.is-active")).to_contain_text(PROFILE_LABELS[profile])
        expect(page.locator("#ats-detail")).to_contain_text("CV Copilot simulation")
        expect(page.locator("#ats-detail")).to_contain_text("Not vendor-backed")
        expect(page.locator("#ats-detail")).to_contain_text("Transparent CV Copilot simulation; not certified by or backed by an ATS vendor.")
        expect(page.locator("#ats-detail .ats-profile-compare-head")).to_contain_text("Simulations, not vendor-backed")

        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


@pytest.mark.parametrize(
    ("lineage_overrides", "expected_state", "expected_label"),
    [
        ({"status": "stale", "stale_reasons": ["job_description_stale"]}, "stale", "ATS stale"),
        ({"status": "archived", "archived_at": "2026-07-02T13:00:00+00:00"}, "archived", "ATS archived"),
        ({"document_role": "source_cv"}, "blocked", "ATS blocked"),
        ({"source_cv_hash": ""}, "blocked", "ATS blocked"),
        ({"prompt_version": "jobs_packet.v0"}, "blocked", "ATS blocked"),
        ({"score_system_id": ""}, "blocked", "ATS blocked"),
        ({"score": None, "score_snapshot": {}}, "blocked", "ATS blocked"),
    ],
)
def test_app_ats_badge_blocks_unusable_packet_lineage(browser, lineage_overrides, expected_state, expected_label):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    score_bodies: list[dict] = []
    try:
        page.set_viewport_size({"width": 1440, "height": 900})
        install_layout_vendor_routes(page)
        _install_auth_stub(page)
        console_errors, page_errors = console_collector(page)
        request_failures, bad_responses = network_collector(page)

        lineage = _packet_lineage_payload(document_id=CV_ID, **lineage_overrides)

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/ats-profiles":
                _json(route, _profiles_payload())
            elif path == "/api/cvs":
                _json(route, {"cvs": [{
                    "id": CV_ID,
                    "name": "Source CV",
                    "job_id": JOB_ID,
                    "job_title": "Senior Data Engineer",
                    "packet_lineage": lineage,
                }]})
            elif path == "/api/cv":
                route.fulfill(status=200, headers={"X-OnePager-Revision": "rev"}, content_type="text/yaml", body="name: Source CV\nskills:\n  - label: Data\n    items: Spark, Airflow\n")
            elif path == f"/api/jobs/{JOB_ID}":
                payload = _job_payload(False)
                payload["packets"] = [_packet_payload(tailored_cv_id=CV_ID, **lineage_overrides)]
                _json(route, payload)
            elif path == "/api/ats-score" and req.method == "POST":
                score_bodies.append(json.loads(req.post_data or "{}"))
                _json(route, _ats_payload("semantic_fit"))
            elif path in {"/api/preview", "/api/templates", "/api/themes", "/api/documents", "/api/analytics/event"}:
                _json(route, {"documents": [], "templates": [], "themes": []})
            else:
                _json(route, {})

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/app?id={CV_ID}")

        expect(page.locator("#ats-badge")).to_have_attribute("data-state", expected_state)
        expect(page.locator("#ats-badge")).to_have_attribute("data-packet-usable", "false")
        expect(page.locator("#ats-badge .ats-badge-label")).to_have_text(expected_label)
        assert score_bodies == []
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_editor_preserves_explicit_job_and_source_context_for_tailor_hop(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    try:
        page.set_viewport_size({"width": 1440, "height": 900})
        install_layout_vendor_routes(page)
        _install_auth_stub(page)
        console_errors, page_errors = console_collector(page)
        request_failures, bad_responses = network_collector(page)

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/ats-profiles":
                _json(route, _profiles_payload())
            elif path == "/api/cvs":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Unlinked source CV"}]})
            elif path == "/api/cv":
                route.fulfill(
                    status=200,
                    headers={"X-OnePager-Revision": "rev"},
                    content_type="text/yaml",
                    body="name: Source CV\nskills:\n  - label: Data\n    items: Spark, Airflow\n",
                )
            elif path in {"/api/preview", "/api/templates", "/api/themes", "/api/documents", "/api/analytics/event"}:
                _json(route, {"documents": [], "templates": [], "themes": []})
            else:
                _json(route, {})

        page.route("**/api/**", handle_api)
        _goto_authenticated(
            page,
            f"/app?id={CV_ID}&job_id={JOB_ID}&cv_id={CV_ID}&source_cv_id={CV_ID}",
        )

        # `updateTailorLinks()` rewrites this href once the CV and job context
        # resolve, so a one-shot read right after navigation can catch the
        # pre-context value — the same race that made the linked-CV lineage
        # assertions intermittent. Poll for the settled href first.
        tailor_link = page.locator("#tailor-btn")
        expect(tailor_link).to_have_attribute(
            "href", re.compile(r"^/tailor\?.*[?&]source_cv_id=")
        )
        href = tailor_link.get_attribute("href") or ""
        assert href.startswith("/tailor?")
        assert f"job_id={JOB_ID}" in href
        assert f"cv_id={CV_ID}" in href
        assert f"source_cv_id={CV_ID}" in href
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_jobs_linked_cv_preserves_source_lineage_through_editor_to_tailor(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    try:
        page.set_viewport_size({"width": 1440, "height": 900})
        install_layout_vendor_routes(page)
        _install_auth_stub(page)
        console_errors, page_errors = console_collector(page)
        request_failures, bad_responses = network_collector(page)
        lineage = _packet_lineage_payload(document_id=TAILORED_CV_ID)

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [_job_payload(True)["job"]], "count": 1})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, _job_payload(True))
            elif path == f"/api/jobs/{JOB_ID}/apply/readiness" and req.method == "GET":
                _json(route, {
                    "ready": False,
                    "blockers": [{"code": "pdf_missing", "message": "Build the packet PDFs."}],
                    "attempts": [],
                    "packet": _packet_payload(),
                    "target": {"url": "", "host": "", "classification": "invalid", "open_allowed": False},
                    "config": {"available": False, "enabled": False, "kill_switch_active": True},
                    "actions": {"manual_open": False, "dry_run": False, "submit": False},
                })
            elif path == "/api/cvs":
                _json(route, {"cvs": [
                    {"id": CV_ID, "name": "Source CV"},
                    {
                        "id": TAILORED_CV_ID,
                        "name": "Source CV - reviewed for Acme",
                        "job_id": JOB_ID,
                        "job_title": "Senior Data Engineer",
                        "packet_lineage": lineage,
                    },
                ]})
            elif path == "/api/cv":
                route.fulfill(
                    status=200,
                    headers={"X-OnePager-Revision": "rev"},
                    content_type="text/yaml",
                    body="name: Source CV - reviewed for Acme\nskills:\n  - label: Data\n    items: Spark, Airflow\n",
                )
            elif path == "/api/preview":
                route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<!doctype html><html><body><main>Tailored CV preview</main></body></html>",
                )
            elif path == "/api/ats-profiles":
                _json(route, _profiles_payload())
            elif path == "/api/ats-score" and req.method == "POST":
                _json(route, _ats_payload("semantic_fit"))
            elif path == "/api/templates":
                _json(route, {"templates": []})
            elif path == "/api/themes":
                _json(route, {"themes": []})
            elif path == "/api/documents":
                _json(route, {"documents": [], "count": 0})
            else:
                _json(route, {})

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")

        linked_cv = page.locator("#jb-cvs .jb-linked-open")
        expect(linked_cv).to_be_visible()
        _assert_application_context_href(
            linked_cv,
            path="/app", document_id=TAILORED_CV_ID, job_id=JOB_ID,
            cv_id=TAILORED_CV_ID, source_cv_id=CV_ID,
        )
        linked_cv.click()
        page.wait_for_url(re.compile(r"/app\?"))
        expect(page.locator("#tailor-btn")).to_be_attached()
        _assert_application_context_href(
            page.locator("#tailor-btn"),
            path="/tailor", document_id=TAILORED_CV_ID, job_id=JOB_ID,
            cv_id=TAILORED_CV_ID, source_cv_id=CV_ID,
        )
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_jobs_local_search_creates_review_import_preview(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    local_search_called = {"value": False}
    import_preview_bodies: list[dict] = []
    linkedin_requests: list[str] = []

    def record_request(req):
        host = (urlparse(req.url).hostname or "").lower()
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            linkedin_requests.append(req.url)

    page.on("request", record_request)
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))
        add_top_document_init_script(
            page,
            """
            localStorage.setItem("cv.editor.job_source_defaults", JSON.stringify({
              schema: "onepager.job_profile.v1",
              source: "linkedin",
              title: "li_at=secret-session-cookie",
              titles: ["Data Engineer", "Analytics Engineer", "session_token=secret"],
              family: "Data / ML",
              cities: ["Berlin", "Warsaw", "authorization=Bearer secret"],
              source_external_id: "sk-not-a-profile-field",
              min_years: "5",
              ranking: { title: 0.35, location: 0.25, freshness: 0.2, experience: 0.2 },
              freshness: "fresh"
            }));
            """
        )

        def handle_api(route):
            req = route.request
            parsed = urlparse(req.url)
            path = parsed.path
            if path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            elif path == "/api/jobs/local-search" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                serialized = json.dumps(body)
                assert "li_at" not in serialized
                assert "cookie" not in serialized.lower()
                assert "session" not in serialized.lower()
                assert "authorization" not in serialized.lower()
                assert body["filters"]["source"] == "linkedin"
                assert body["query"] == "Data Engineer"
                assert body["filters"]["titles"] == ["Data Engineer", "Analytics Engineer"]
                assert body["filters"]["family"] == "Data / ML"
                assert body["filters"]["city"] == "Berlin, Warsaw"
                assert body["filters"]["region"] == "Berlin"
                assert body["filters"]["freshness"] == "fresh"
                assert body["filters"]["min_years"] == "5"
                assert body["filters"]["ranking"]["title"] == 0.35
                local_search_called["value"] = True
                _json(route, {
                    "results": [{
                        "id": "fp-1",
                        "source_external_id": "fp-1",
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "jd_text": "Build Spark and Airflow pipelines.",
                        "source_url": "https://www.linkedin.com/jobs/view/1",
                        "source_provider": "linkedin",
                        "source_type": "csv_import",
                        "job_family": "Data Engineer",
                        "source_city": "Berlin",
                        "source_location": "Berlin, Germany",
                        "remote_mode": "remote",
                        "seniority": "senior",
                        "verification_status": "stale",
                        "search_criteria": {"schema": "onepager.job_search_criteria.v1", "titles": ["Data Engineer", "Analytics Engineer"]},
                        "match_reasons": ["normalized title match", "city/location match"],
                        "broader_match": False,
                    }],
                    "count": 1,
                    "stale": True,
                })
            elif path == "/api/jobs/import-runs" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                import_preview_bodies.append(body)
                serialized = json.dumps(body)
                assert "li_at" not in serialized
                assert "cookie" not in serialized.lower()
                assert "session" not in serialized.lower()
                assert "authorization" not in serialized.lower()
                posting = body["postings"][0]
                if posting.get("source_provider"):
                    assert posting["source_provider"] == "linkedin"
                    assert posting["verification_status"] == "stale"
                    assert posting["search_criteria"]["schema"] == "onepager.job_search_criteria.v1"
                    assert "normalized title match" in posting["match_reasons"]
                else:
                    assert body["provider"] == "linkedin"
                    assert posting["title"] == "Data Engineer"
                _json(route, {
                    "id": "55555555-5555-4555-8555-555555555555",
                    "results": [{
                        "id": "66666666-6666-4666-8666-666666666666",
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "source_location": "Berlin, Germany",
                        "remote_mode": "remote",
                        "seniority": "senior",
                        "source_provider": "linkedin",
                        "verification_status": "stale",
                        "match_reasons": ["normalized title match", "city/location match"],
                    }],
                }, status=202)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, "/jobs")
        rewritten_profile = page.evaluate("() => localStorage.getItem('cv.editor.job_source_defaults')")
        assert rewritten_profile
        rewritten_text = rewritten_profile.lower()
        for forbidden in ("li_at", "session_token", "authorization", "bearer", "secret-session-cookie", "sk-not-a-profile-field"):
            assert forbidden not in rewritten_text
        page.locator("#jb-import-details").evaluate("node => node.open = true")
        page.locator("#jb-import-title").fill("Data Engineer")
        page.locator("#jb-import-region").fill("Berlin")
        page.locator("#jb-import-remote").select_option("remote")
        page.locator("#jb-import-seniority").fill("senior")
        page.locator("#jb-import-jd").fill("Title: Data Engineer\\nCompany: Acme GmbH\\nBuild Spark pipelines.")
        _run_palette_action(page, "preview job import")
        expect(page.locator(".jb-import-row")).to_have_count(1)
        assert import_preview_bodies and import_preview_bodies[-1]["provider"] == "linkedin"
        page.locator("#jb-local-search").click()
        expect(page.locator(".jb-import-row")).to_have_count(1)
        expect(page.locator(".jb-import-row-title")).to_contain_text("Senior Data Engineer")
        expect(page.locator(".jb-pill--verify")).to_contain_text("Stale")
        expect(page.locator(".jb-match-reasons--import")).to_contain_text("normalized title match")
        expect(page.locator("#jb-import-save-selected")).to_be_enabled()
        assert local_search_called["value"] is True
        assert linkedin_requests == []
        assert_no_horizontal_overflow(page)
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_jobs_local_search_strict_and_flexible_semantic_modes(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    search_bodies: list[dict] = []
    import_bodies: list[dict] = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))
        add_top_document_init_script(
            page,
            """
            localStorage.setItem("cv.editor.job_source_defaults", JSON.stringify({
              schema: "onepager.job_profile.v1",
              source: "linkedin",
              title: "Data Engineer",
              titles: ["Data Engineer"],
              family: "Data / ML",
              search_mode: "strict",
              country: "DE",
              cities: ["Berlin"],
              min_years: "5",
              ranking: { title: 0.35, location: 0.25, freshness: 0.2, experience: 0.2 }
            }));
            """
        )

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            elif path == "/api/jobs/local-search" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                search_bodies.append(body)
                assert body["filters"]["source"] == "linkedin"
                assert body["filters"]["title"] == "Data Engineer"
                assert body["filters"]["titles"] == ["Data Engineer"]
                assert body["filters"]["family"] == "Data / ML"
                assert body["filters"]["country"] == "DE"
                assert body["filters"]["city"] == "Berlin"
                assert body["filters"]["min_years"] == "5"
                assert body["filters"]["ranking"]["title"] == 0.35
                mode = body["filters"]["search_mode"]
                if mode == "strict":
                    _json(route, {"results": [], "count": 0})
                elif mode == "flexible":
                    _json(route, {
                        "results": [{
                            "id": "fp-flex-1",
                            "source_external_id": "fp-flex-1",
                            "title": "Analytics Platform Engineer",
                            "company": "Acme GmbH",
                            "jd_text": "Broader data platform role with Spark ownership.",
                            "source_url": "https://www.linkedin.com/jobs/view/flex-1",
                            "source_provider": "linkedin",
                            "source_type": "csv_import",
                            "job_family": "Data / ML",
                            "source_country": "DE",
                            "source_city": "Berlin",
                            "source_location": "Berlin, Germany",
                            "remote_mode": "hybrid",
                            "seniority": "senior",
                            "verification_status": "needs_review",
                            "search_criteria": {
                                "schema": "onepager.job_search_criteria.v1",
                                "job_family": "Data / ML",
                                "titles": ["Data Engineer"],
                                "search_mode": "flexible",
                                "countries": ["DE"],
                                "cities": ["Berlin"],
                                "min_years": 5,
                                "source": "linkedin",
                                "verification": "needs_review",
                            },
                            "match_reasons": ["broader family match", "city/location match"],
                            "broader_match": True,
                        }],
                        "count": 1,
                    })
                else:
                    raise AssertionError(f"unexpected search mode: {mode}")
            elif path == "/api/jobs/import-runs" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                import_bodies.append(body)
                assert body["postings"][0]["broader_match"] is True
                assert "broader family match" in body["postings"][0]["match_reasons"]
                assert body["postings"][0]["search_criteria"]["search_mode"] == "flexible"
                _json(route, {
                    "id": "55555555-5555-4555-8555-555555555555",
                    "results": body["postings"],
                }, status=202)
            elif path in {"/api/templates", "/api/themes", "/api/documents", "/api/analytics/event"}:
                _json(route, {"documents": [], "templates": [], "themes": []})
            else:
                _json(route, {})

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, "/jobs")
        page.locator("#jb-import-details").evaluate("node => node.open = true")
        page.locator("#jb-local-search").click()
        expect(page.locator("#jb-import-feedback")).to_contain_text("No import rows returned.")
        expect(page.locator(".jb-import-row")).to_have_count(0)

        page.locator("#jb-search-mode").select_option("flexible")
        page.locator("#jb-local-search").click()
        expect(page.locator(".jb-import-row")).to_have_count(1)
        expect(page.locator(".jb-import-row-title")).to_contain_text("Analytics Platform Engineer")
        expect(page.locator(".jb-match-reasons--import .jb-why-label")).to_contain_text("Broader match")
        expect(page.locator(".jb-match-reasons--import")).to_contain_text("broader family match")
        assert [body["filters"]["search_mode"] for body in search_bodies] == ["strict", "flexible"]
        assert len(import_bodies) == 1
        assert_no_horizontal_overflow(page)
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_live_fixture_job_search_save_tailor_apply_open_and_pdf_flow(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    state = {
        "saved": False,
        "packet": False,
        "builds": [],
        "confirmed": False,
        "attempts": [],
        "apply_posts": [],
    }
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def job_row() -> dict:
            return {
                "id": JOB_ID,
                "title": "Senior Data Engineer",
                "company": "Acme GmbH",
                "status": "open",
                "field": "Data",
                "confidence": 82,
                "why_fit": "Strong data platform match.",
                "source_provider": "other",
                "source_external_id": "fp-live",
                "source_country": "DE",
                "source_region": "Berlin",
                "source_city": "Berlin",
                "source_location": "Berlin, Germany",
                "remote_mode": "remote",
                "seniority": "senior",
                "verification_status": "needs_review",
                "jd_text": JD_TEXT,
                "source_url": "https://example.test/jobs/fp-live",
                "has_cover": state["packet"],
                "cover_id": LETTER_ID if state["packet"] else None,
            }

        def readiness_payload() -> dict:
            packet = _packet_payload() if state["packet"] else None
            cv_built = {"id": TAILORED_CV_ID} in state["builds"]
            letter_built = {"letter": LETTER_ID} in state["builds"]
            blockers = []
            if not packet:
                blockers.append({
                    "code": "reviewed_packet_missing",
                    "message": "A reviewed application packet is required.",
                })
            else:
                if not cv_built:
                    blockers.append({
                        "code": "tailored_cv_pdf_missing",
                        "message": "Build the tailored CV PDF.",
                    })
                if not letter_built:
                    blockers.append({
                        "code": "cover_letter_pdf_missing",
                        "message": "Build the cover-letter PDF.",
                    })
                if cv_built and letter_built and not state["confirmed"]:
                    blockers.extend([
                        {"code": "profile_answers_incomplete", "message": "Confirm profile answers."},
                        {"code": "legal_answers_unconfirmed", "message": "Confirm legal answers."},
                        {"code": "visa_answers_unconfirmed", "message": "Confirm visa answers."},
                    ])
            ready = bool(packet and not blockers)
            return {
                "job_id": JOB_ID,
                "ready": ready,
                "blockers": blockers,
                "attempts": list(reversed(state["attempts"])),
                "target": {
                    "url": "https://example.test/jobs/fp-live",
                    "host": "example.test",
                    "classification": "unknown",
                    "method": "manual_open",
                    "open_allowed": True,
                    "dry_run_allowed": False,
                    "submit_allowed": False,
                    "automation_allowed": False,
                    "reason_code": "unknown_manual_open_only",
                    "reason": "Unknown hosts are manual/open only.",
                },
                "packet": packet,
                "config": {"available": True, "enabled": True, "kill_switch_active": False},
                "requires_confirmation": True,
                "required_confirmations": [
                    "user_confirmed",
                    "reviewed_packet_confirmed",
                    "profile_answers_complete",
                    "legal_answers_confirmed",
                    "visa_answers_confirmed",
                ],
                "actions": {"manual_open": ready, "dry_run": False, "submit": False},
            }

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/jobs" and req.method == "GET":
                jobs = [job_row()] if state["saved"] else []
                _json(route, {"jobs": jobs, "count": len(jobs)})
            elif path == "/api/jobs/local-search" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["filters"]["region"] == "Berlin"
                _json(route, {
                    "results": [{
                        "id": "fp-live",
                        "source_external_id": "fp-live",
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "jd_text": JD_TEXT,
                        "source_url": "https://example.test/jobs/fp-live",
                        "source_provider": "other",
                        "source_type": "csv_import",
                        "source_country": "DE",
                        "source_region": "Berlin",
                        "source_city": "Berlin",
                        "source_location": "Berlin, Germany",
                        "remote_mode": "remote",
                        "seniority": "senior",
                        "verification_status": "needs_review",
                    }],
                    "count": 1,
                    "stale": False,
                })
            elif path == "/api/jobs/import-runs" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["postings"][0]["source_provider"] == "other"
                assert body["postings"][0]["verification_status"] == "needs_review"
                _json(route, {
                    "id": "55555555-5555-4555-8555-555555555555",
                    "results": [{
                        "id": "66666666-6666-4666-8666-666666666666",
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "source_location": "Berlin, Germany",
                        "remote_mode": "remote",
                        "seniority": "senior",
                        "source_provider": "other",
                        "verification_status": "needs_review",
                    }],
                }, status=202)
            elif path == "/api/jobs/import-runs/55555555-5555-4555-8555-555555555555/save-selected" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["result_ids"] == ["66666666-6666-4666-8666-666666666666"]
                state["saved"] = True
                _json(route, {"saved_count": 1, "jobs": [job_row()]})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                payload = _job_payload(state["packet"])
                payload["job"].update(job_row())
                _json(route, payload)
            elif path == f"/api/jobs/{JOB_ID}/review" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["source_cv_id"] == CV_ID
                _json(route, {
                    "review_token": "signed.live.review",
                    "review_id": "review-live",
                    "source_cv_id": CV_ID,
                    "source_hash": "a" * 64,
                    "source_revision": "now",
                    "jd_hash": "b" * 64,
                    "tailored_hash": "c" * 64,
                    "provider": "anthropic",
                    "model": "claude-test",
                    "created_at": "2026-07-02T12:00:00+00:00",
                    "job_id": JOB_ID,
                    "name": "Source CV - reviewed for Acme",
                    "summary": "2 changes proposed. ATS 88%.",
                    "diff": {"changed": True, "added": 1, "removed": 1, "lines": ["--- source-cv.yaml", "+++ reviewed-copy.yaml", "+Spark and Airflow"]},
                    "ats": {"score": 88, "experience_fit": {"candidate_years": 4.5, "required_years": 5, "fit": "near"}},
                })
            elif path == f"/api/jobs/{JOB_ID}/apply-review" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["source_cv_id"] == CV_ID
                assert body["review_token"] == "signed.live.review"
                _json(route, {
                    "tailored_cv_id": TAILORED_CV_ID,
                    "cover_letter_id": LETTER_ID,
                    "packet_id": PACKET_ID,
                    "cover_letter_hash": "d" * 64,
                    "cover_letter_review_status": "pending",
                    "requires_cover_letter_review": True,
                    "ats": {"score": 88},
                }, status=201)
            elif path == f"/api/jobs/{JOB_ID}/packets/{PACKET_ID}/confirm-cover-letter-review" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body["cover_letter_hash"] == "d" * 64
                state["packet"] = True
                _json(route, {
                    "packet_id": PACKET_ID,
                    "cover_letter_review_status": "confirmed",
                    "cover_letter_review_confirmed_at": "2026-07-02T12:00:00+00:00",
                })
            elif path == f"/api/jobs/{JOB_ID}/apply/readiness" and req.method == "GET":
                _json(route, readiness_payload())
            elif path == f"/api/jobs/{JOB_ID}/apply/open" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                assert body == {
                    "user_confirmed": True,
                    "reviewed_packet_confirmed": True,
                    "profile_answers_complete": True,
                    "legal_answers_confirmed": True,
                    "visa_answers_confirmed": True,
                    "packet_id": PACKET_ID,
                }
                state["apply_posts"].append(body)
                attempt = {
                    "id": "88888888-8888-4888-8888-888888888888",
                    "job_id": JOB_ID,
                    "packet_id": PACKET_ID,
                    "action": "manual_open",
                    "status": "manual_open_ready",
                    "reason_code": "unknown_manual_open_only",
                    "target_host": "example.test",
                    "target_classification": "unknown",
                    "submitted": False,
                    "automation_performed": False,
                    "created_at": "2026-08-01T12:00:00+00:00",
                }
                if len(state["apply_posts"]) == 1:
                    _json(route, {
                        "status": "manual_open_ready",
                        "submitted": False,
                        "automation_performed": False,
                        "open_url": "https://mismatch.example/jobs/fp-live",
                        "target": readiness_payload()["target"],
                        "packet": _packet_payload(),
                        "attempt": attempt,
                    }, status=202)
                    return
                state["confirmed"] = True
                state["attempts"].append(attempt)
                _json(route, {
                    "status": "manual_open_ready",
                    "submitted": False,
                    "automation_performed": False,
                    "open_url": "https://example.test/jobs/fp-live",
                    "target": readiness_payload()["target"],
                    "packet": _packet_payload(),
                    "attempt": attempt,
                }, status=202)
            elif path == "/api/build" and req.method == "POST":
                state["builds"].append(json.loads(req.post_data or "{}"))
                _pdf(route)
            elif path == f"/api/cover-letter/{LETTER_ID}/build":
                state["builds"].append({"letter": LETTER_ID})
                _pdf(route)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, "/jobs")
        page.locator("#jb-import-details").evaluate("node => node.open = true")
        page.locator("#jb-import-title").fill("Data Engineer")
        page.locator("#jb-import-region").fill("Berlin")
        page.locator("#jb-import-remote").select_option("remote")
        page.locator("#jb-import-seniority").fill("senior")
        page.locator("#jb-local-search").click()
        expect(page.locator(".jb-import-row")).to_have_count(1)
        expect(page.locator("#jb-import-save-selected")).to_be_enabled()
        page.locator("#jb-import-save-selected").click()
        expect(page.locator("#jb-grid")).to_contain_text("Senior Data Engineer")
        page.locator(f'.jb-card[data-id="{JOB_ID}"] .jb-card-detail').click()
        expect(page.locator("#jb-detail-title")).to_contain_text("Senior Data Engineer")
        page.locator("#jb-generate").click()
        expect(page.locator("#jb-review-panel")).to_be_visible()
        page.locator("#jb-apply-review").click()
        expect(page.locator("#jb-gen-feedback")).to_contain_text("Cover letter generated")
        page.locator("#jb-approve-cover-review").check()
        page.locator("#jb-apply-review").click()
        expect(page.locator("#jb-cvs .jb-linked-row")).to_have_count(1)
        expect(page.locator("#jb-letters .jb-linked-row")).to_have_count(1)
        _assert_application_context_href(
            page.locator("#jb-cvs .jb-linked-open"),
            path="/app", document_id=TAILORED_CV_ID, job_id=JOB_ID,
            cv_id=TAILORED_CV_ID, source_cv_id=CV_ID,
        )
        _assert_application_context_href(
            page.locator("#jb-letters .jb-linked-open"),
            path="/cover-letters", document_id=LETTER_ID, job_id=JOB_ID,
            cv_id=TAILORED_CV_ID, source_cv_id=CV_ID,
        )
        with page.expect_response(lambda res: res.url.endswith("/api/build")):
            page.locator("#jb-cvs .jb-linked-pdf").click()
        with page.expect_response(lambda res: f"/api/cover-letter/{LETTER_ID}/build" in res.url):
            page.locator("#jb-letters .jb-linked-pdf").click()
        page.wait_for_timeout(250)
        assert {"id": TAILORED_CV_ID} in state["builds"]
        assert {"letter": LETTER_ID} in state["builds"]
        targets = page.evaluate("window.__openedPdfTargets")
        assert len(targets) >= 2
        assert all(target != "about:blank" for target in targets)
        opened_before_manual_handoff = list(targets)
        page.locator("#jb-readiness-refresh").click()
        expect(page.locator("#jb-readiness-body")).to_have_attribute("data-readiness-state", "blocked")
        expect(page.locator("#jb-apply-confirmations")).to_be_enabled()
        for confirmation in page.locator("[data-apply-confirmation]").all():
            confirmation.check()
        expect(page.locator("#jb-apply-open-confirm")).to_be_enabled()
        with page.expect_response(lambda res: res.url.endswith(f"/api/jobs/{JOB_ID}/apply/open") and res.status == 202):
            page.locator("#jb-apply-open-confirm").click()
        expect(page.locator("#jb-apply-result")).to_contain_text("did not match")
        expect(page.locator("#jb-apply-open-link")).to_be_hidden()
        expect(page.locator("#jb-apply-confirmations")).to_be_enabled()
        for confirmation in page.locator("[data-apply-confirmation]").all():
            expect(confirmation).not_to_be_checked()
            confirmation.check()
        expect(page.locator("#jb-apply-open-confirm")).to_be_enabled()
        with page.expect_response(lambda res: res.url.endswith(f"/api/jobs/{JOB_ID}/apply/open") and res.status == 202):
            page.locator("#jb-apply-open-confirm").click()
        expect(page.locator("#jb-apply-open-link")).to_be_visible()
        # Polling form: this href is populated by the readiness fetch, so a
        # one-shot equality read races the render that fills it in.
        expect(page.locator("#jb-apply-open-link")).to_have_attribute(
            "href", "https://example.test/jobs/fp-live"
        )
        expect(page.locator("#jb-apply-attempts")).to_contain_text("manual_open_ready")
        expect(page.locator("#jb-apply-attempts")).to_contain_text("example.test")
        expect(page.locator("#jb-readiness-body")).to_have_attribute("data-readiness-state", "ready")
        assert len(state["apply_posts"]) == 2
        assert page.evaluate("window.__openedPdfTargets") == opened_before_manual_handoff
        assert page.url.startswith(f"{BASE_URL}/jobs")
        assert_no_horizontal_overflow(page)
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_jobs_saved_filters_and_detail_provenance(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    jobs = [
        {
            "id": JOB_ID,
            "title": "Senior Data Engineer",
            "company": "Acme GmbH",
            "status": "open",
            "field": "Data",
            "source_provider": "linkedin",
            "source_external_id": "li-1",
            "source_country": "DE",
            "source_region": "Berlin",
            "source_location": "Berlin, Germany",
            "remote_mode": "remote",
            "seniority": "senior",
            "salary_text": "EUR 80k",
            "verification_status": "needs_review",
            "last_seen_at": "2099-07-01T10:00:00Z",
            "expires_at": "2099-08-01",
            "source_metadata": {
                "origin": "local_jobs_db_read_only",
                "site": "linkedin",
                "rank_bucket": "Apply first",
                "priority_rank": 2,
                "crawl_policy": "li_at=secret-cookie Bearer abc session_token",
            },
            "jd_text": JD_TEXT,
        },
        {
            "id": "11111111-1111-4111-8111-111111111112",
            "title": "Platform Engineer",
            "company": "Beta GmbH",
            "status": "saved",
            "field": "Platform",
            "source_provider": "manual",
            "source_country": "US",
            "source_region": "California",
            "source_location": "San Francisco, CA",
            "remote_mode": "hybrid",
            "seniority": "mid",
            "verification_status": "user_provided",
            "source_metadata": {"rank_bucket": "Strong volume", "priority_rank": 1},
            "jd_text": "Build platform services.",
        },
        {
            "id": "11111111-1111-4111-8111-111111111113",
            "title": "Support Engineer",
            "company": "Gamma GmbH",
            "status": "saved",
            "field": "Support",
            "source_provider": "other",
            "source_country": "DE",
            "source_region": "Hamburg",
            "source_location": "Hamburg, Germany",
            "remote_mode": "onsite",
            "seniority": "junior",
            "verification_status": "stale",
            "last_seen_at": "2026-05-01T10:00:00Z",
            "expires_at": "2026-06-01",
            "source_metadata": {"rank_bucket": "Apply first", "priority_rank": 1},
            "jd_text": "Support enterprise users.",
        },
    ]
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": jobs, "count": len(jobs)})
            elif path in {f"/api/jobs/{item['id']}" for item in jobs} and req.method == "GET":
                job_id = path.rsplit("/", 1)[-1]
                job = next((item for item in jobs if item["id"] == job_id), None)
                if job:
                    _json(route, {"job": job, "cvs": [], "cover_letters": []})
                else:
                    _json(route, {"error": "missing"}, status=404)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        expect(page.locator("#jb-grid .jb-card")).to_have_count(3)
        expect(page.locator("#jb-grid")).to_contain_text("fresh")
        expect(page.locator("#jb-grid")).to_contain_text("stale")

        page.locator("#jb-filter-country").fill("DE")
        expect(page.locator("#jb-grid .jb-card")).to_have_count(2)
        page.locator("#jb-filter-region").fill("Berlin")
        expect(page.locator("#jb-grid .jb-card")).to_have_count(1)
        expect(page.locator("#jb-grid .jb-card-title")).to_have_text("Senior Data Engineer")

        page.locator("#jb-filter-region").fill("")
        page.locator("#jb-filter-provider").select_option("other")
        expect(page.locator("#jb-grid .jb-card")).to_have_count(1)
        expect(page.locator("#jb-grid .jb-card-title")).to_have_text("Support Engineer")

        page.locator("#jb-filter-provider").select_option("")
        page.locator("#jb-filter-verification").select_option("needs_review")
        expect(page.locator("#jb-grid .jb-card-title")).to_have_text("Senior Data Engineer")

        page.locator("#jb-filter-verification").select_option("")
        page.locator("#jb-filter-remote").select_option("onsite")
        expect(page.locator("#jb-grid .jb-card-title")).to_have_text("Support Engineer")

        page.locator("#jb-filter-remote").select_option("")
        page.locator("#jb-filter-seniority").fill("senior")
        expect(page.locator("#jb-grid .jb-card-title")).to_have_text("Senior Data Engineer")

        page.locator("#jb-filter-seniority").fill("")
        page.locator("#jb-filter-freshness").select_option("fresh")
        expect(page.locator("#jb-grid .jb-card")).to_have_count(1)
        expect(page.locator("#jb-grid .jb-card-title")).to_have_text("Senior Data Engineer")
        page.locator("#jb-filter-freshness").select_option("stale")
        expect(page.locator("#jb-grid .jb-card")).to_have_count(1)
        expect(page.locator("#jb-grid .jb-card-title")).to_have_text("Support Engineer")

        page.locator("#jb-filter-country").fill("")
        page.locator("#jb-filter-freshness").select_option("")
        page.locator("#jb-filter-rank-bucket").select_option("Apply first")
        expect(page.locator("#jb-grid .jb-card")).to_have_count(2)
        expect(page.locator("#jb-grid")).not_to_contain_text("Platform Engineer")
        page.locator("#jb-sort").select_option("shortlist_rank")
        expect(page.locator("#jb-grid .jb-card-title")).to_have_text(["Support Engineer", "Senior Data Engineer"])
        page.locator("#jb-filter-rank-bucket").select_option("")
        expect(page.locator("#jb-grid .jb-card-title")).to_have_text(["Support Engineer", "Senior Data Engineer", "Platform Engineer"])

        page.locator(f'#jb-grid .jb-card[data-id="{JOB_ID}"] .jb-card-detail').click()
        expect(page.locator(".jb-provenance-grid")).to_be_visible()
        provenance = page.locator(".jb-provenance-grid")
        for text in ("Source", "External ID", "Verification", "Location", "Remote", "Seniority", "Salary", "Last seen", "Expires", "Origin"):
            expect(provenance).to_contain_text(text)
        for text in ("linkedin", "li-1", "Needs review", "Berlin, Germany", "remote", "senior", "EUR 80k", "2099-07-01", "2099-08-01", "local_jobs_db_read_only"):
            expect(provenance).to_contain_text(text)
        for secret in ("li_at", "secret-cookie", "Bearer", "authorization", "session_token"):
            expect(provenance).not_to_contain_text(secret)

        assert_no_horizontal_overflow(page)
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_jobs_german_verification_and_provenance_labels(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    job = {
        "id": JOB_ID,
        "title": "Senior Data Engineer",
        "company": "Acme GmbH",
        "status": "open",
        "source_provider": "linkedin",
        "source_external_id": "li-1",
        "source_location": "Berlin, Germany",
        "verification_status": "needs_review",
        "jd_text": JD_TEXT,
    }
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))
        add_top_document_init_script(
            page,
            'localStorage.setItem("onepager.lang", "de");',
        )

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [job], "count": 1})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, {"job": job, "cvs": [], "cover_letters": []})
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/jobs?id={JOB_ID}")
        page.wait_for_function("() => document.documentElement.lang === 'de'")
        expect(page.locator("#jb-grid")).to_contain_text("Prüfung nötig")
        page.locator(f'#jb-grid .jb-card[data-id="{JOB_ID}"] .jb-card-detail').click()
        expect(page.locator(".jb-provenance-grid")).to_contain_text("Prüfung")
        expect(page.locator(".jb-provenance-grid")).to_contain_text("Prüfung nötig")
        expect(page.locator(".jb-provenance-grid")).not_to_contain_text("needs_review")
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_cover_letters_saved_job_dropdown_generates_with_job_id(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    generate_bodies: list[dict] = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            parsed = urlparse(req.url)
            if parsed.path == "/api/cvs":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV"}], "count": 1})
            elif parsed.path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [{
                    "id": JOB_ID,
                    "title": "Senior Data Engineer",
                    "company": "Acme GmbH",
                    "status": "open",
                    "starred": True,
                }], "count": 1})
            elif parsed.path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, _job_payload(False))
            elif parsed.path == "/api/documents":
                _json(route, {"documents": [], "count": 0})
            elif parsed.path == "/api/cover-letter/generate" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                generate_bodies.append(body)
                _json(route, {
                    "id": LETTER_ID,
                    "name": "Acme cover letter",
                    "body": "Dear Acme team,\n\nI build Spark platforms.",
                    "company": "Acme GmbH",
                    "role": "Senior Data Engineer",
                    "packet_lineage": _packet_lineage_payload(document_role="cover_letter"),
                }, status=201)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/cover-letters?job_id={JOB_ID}")
        expect(page.locator("#cl-job")).to_be_visible()
        expect(page.locator("#cl-job")).to_have_value(JOB_ID)
        expect(page).to_have_url(re.compile(r".*[?&]job_id=" + re.escape(JOB_ID)))
        expect(page.locator("#cl-jd")).to_have_value(JD_TEXT)
        page.locator("#cl-generate").click()
        expect(page.locator("#cl-body")).to_have_value(re.compile("Spark platforms"))

        assert generate_bodies[-1]["cv_id"] == CV_ID
        assert generate_bodies[-1]["job_id"] == JOB_ID
        assert "jd_text" not in generate_bodies[-1]
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_cover_letters_cv_context_switch_reloads_the_linked_job_before_generation(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    job_b = "55555555-5555-4555-8555-555555555555"
    cv_b = "66666666-6666-4666-8666-666666666666"
    job_b_jd = "Title: Platform Engineer\nCompany: Beta GmbH\nKubernetes and Go required."
    generate_bodies: list[dict] = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            parsed = urlparse(req.url)
            if parsed.path == "/api/cvs":
                _json(route, {
                    "cvs": [
                        {"id": CV_ID, "name": "Source A"},
                        {"id": cv_b, "name": "Tailored B", "job_id": job_b},
                    ],
                    "count": 2,
                })
            elif parsed.path == "/api/jobs" and req.method == "GET":
                _json(route, {
                    "jobs": [
                        {"id": JOB_ID, "title": "Senior Data Engineer", "company": "Acme GmbH", "status": "open"},
                        {"id": job_b, "title": "Platform Engineer", "company": "Beta GmbH", "status": "open"},
                    ],
                    "count": 2,
                })
            elif parsed.path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, _job_payload(False))
            elif parsed.path == f"/api/jobs/{job_b}" and req.method == "GET":
                payload = _job_payload(False)
                payload["job"].update({"id": job_b, "title": "Platform Engineer", "company": "Beta GmbH", "jd_text": job_b_jd})
                _json(route, payload)
            elif parsed.path == "/api/documents":
                _json(route, {"documents": [], "count": 0})
            elif parsed.path == "/api/cover-letter/generate" and req.method == "POST":
                generate_bodies.append(json.loads(req.post_data or "{}"))
                _json(route, {"id": LETTER_ID, "name": "Beta letter", "body": "Grounded Beta letter."}, status=201)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/cover-letters?job_id={JOB_ID}&cv_id={CV_ID}")
        expect(page.locator("#cl-jd")).to_have_value(JD_TEXT)

        page.locator("#cl-cv").select_option(cv_b)
        expect(page.locator("#cl-job")).to_have_value(job_b)
        expect(page.locator("#cl-jd")).to_have_value(job_b_jd)
        page.locator("#cl-generate").click()
        expect(page.locator("#cl-body")).to_have_value("Grounded Beta letter.")

        assert generate_bodies == [{"cv_id": cv_b, "job_id": job_b, "tone": "auto", "recipient": ""}]
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_cover_letters_manual_job_override_detaches_saved_job_url(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    generate_bodies: list[dict] = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            parsed = urlparse(req.url)
            if parsed.path == "/api/cvs":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV"}], "count": 1})
            elif parsed.path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [{
                    "id": JOB_ID,
                    "title": "Senior Data Engineer",
                    "company": "Acme GmbH",
                    "status": "open",
                }], "count": 1})
            elif parsed.path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, _job_payload(False))
            elif parsed.path == "/api/documents":
                _json(route, {"documents": [], "count": 0})
            elif parsed.path == "/api/cover-letter/generate" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                generate_bodies.append(body)
                _json(route, {
                    "id": LETTER_ID,
                    "name": "Manual cover letter",
                    "body": "Manual description letter",
                    "company": "Manual GmbH",
                    "role": "Platform Engineer",
                }, status=201)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/cover-letters?job_id={JOB_ID}")
        expect(page.locator("#cl-jd")).to_have_value(JD_TEXT)
        page.locator("#cl-jd").fill("Manual cover-letter job description")
        expect(page.locator("#cl-job")).to_have_value("")
        assert "job_id=" not in page.url
        page.locator("#cl-generate").click()
        expect(page.locator("#cl-body")).to_have_value(re.compile("Manual description"))

        assert generate_bodies[-1]["cv_id"] == CV_ID
        assert generate_bodies[-1]["jd_text"] == "Manual cover-letter job description"
        assert "job_id" not in generate_bodies[-1]
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_cover_generation_binds_output_to_dispatched_inputs(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    generate_bodies: list[dict] = []
    held_routes = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/cvs" and req.method == "GET":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV"}]})
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            elif path == "/api/documents" and req.method == "GET":
                _json(route, {"documents": [], "count": 0})
            elif path == "/api/cover-letter/generate" and req.method == "POST":
                generate_bodies.append(json.loads(req.post_data or "{}"))
                if len(generate_bodies) == 1:
                    held_routes.append(route)
                    return
                _json(route, {
                    "id": LETTER_ID,
                    "name": "Acme cover letter",
                    "body": "Grounded generated body",
                    "company": "Acme GmbH",
                    "role": "Senior Data Engineer",
                    "language": "en",
                    "updated_at": "revision-1",
                }, status=201)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, "/cover-letters")
        page.locator("#cl-jd").fill(JD_TEXT)
        page.locator("#cl-tone").select_option("formal")
        page.locator("#cl-recipient").fill("Alice Hiring")
        page.locator("#cl-generate").click()
        expect(page.locator("#cl-generate")).to_be_disabled()
        assert generate_bodies[-1]["tone"] == "formal"
        assert generate_bodies[-1]["recipient"] == "Alice Hiring"

        page.locator("#cl-tone").select_option("concise")
        page.locator("#cl-recipient").fill("Bob Hiring")
        held_routes[0].fulfill(
            status=201,
            content_type="application/json",
            body=json.dumps({
                "id": "77777777-7777-4777-8777-777777777777",
                "name": "Superseded letter",
                "body": "Superseded generated body",
                "company": "Acme GmbH",
                "role": "Senior Data Engineer",
                "language": "en",
                "updated_at": "superseded-revision",
            }),
        )
        expect(page.locator("#cl-generate")).to_be_enabled()
        expect(page.locator("#cl-result-name")).to_have_text("No letter selected")

        page.locator("#cl-generate").click()
        expect(page.locator("#cl-body")).to_have_value("Grounded generated body")
        expect(page.locator("#cl-envelope-recipient")).to_have_value("Bob Hiring")
        expect(page.locator("#cl-envelope-tone")).to_have_value("concise")
        assert generate_bodies[-1]["tone"] == "concise"
        assert generate_bodies[-1]["recipient"] == "Bob Hiring"
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


@pytest.mark.parametrize("viewport", [Viewport(1440, 900), Viewport(820, 800), Viewport(390, 844)], ids=lambda v: v.label)
def test_cover_letter_selected_body_and_pdf_controls_are_reachable(browser, viewport: Viewport):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    lineage = _packet_lineage_payload(
        document_role="cover_letter",
        tailored_cv_id=TAILORED_CV_ID,
        cover_letter_id=LETTER_ID,
    )
    stale_lineage = dict(lineage, status="stale", stale_reasons=["cover_letter_stale"])
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, viewport)

        def handle_api(route):
            parsed = urlparse(route.request.url)
            if parsed.path == "/api/documents":
                _json(route, {
                    "documents": [{
                        "id": LETTER_ID,
                        "type": "cover_letter",
                        "name": "Acme cover letter",
                        "company": "Acme GmbH",
                        "role": "Senior Data Engineer",
                        "job_id": JOB_ID,
                        "packet_lineage": lineage,
                    }],
                    "count": 1,
                })
            elif parsed.path == "/api/jobs" and route.request.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            elif parsed.path == f"/api/jobs/{JOB_ID}" and route.request.method == "GET":
                # The deep-linked document synchronizes its saved-job context
                # on load and again when opened from the library. Keep that
                # read inside this test's deterministic API surface.
                _json(route, _job_payload(False))
            elif parsed.path == "/api/cover-letter" and route.request.method == "GET":
                _json(route, {
                    "id": LETTER_ID,
                    "name": "Acme cover letter",
                    "body": "Dear Acme team,\\n\\nI build reliable Spark and Airflow data platforms.\\n\\nBest,\\nTim",
                    "packet_lineage": lineage,
                })
            elif parsed.path == "/api/cover-letter" and route.request.method == "POST":
                _json(route, {
                    "saved": True,
                    "id": LETTER_ID,
                    "packet_lineage": stale_lineage,
                })
            elif parsed.path == f"/api/cover-letter/{LETTER_ID}/build":
                _pdf(route)
            elif parsed.path == f"/api/cover-letter/{LETTER_ID}/preview":
                route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<article><h1>Acme cover letter</h1><p>Spark and Airflow</p></article>",
                )
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/cover-letters?id={LETTER_ID}")
        expect(page.locator('.cl-lib-row[data-packet-state="packet_created"]')).to_have_count(1)
        if viewport.width <= 840:
            scroll_state = page.locator(".cl-main").evaluate(
                """
                el => ({
                  overflowY: getComputedStyle(el).overflowY,
                  scrollTop: el.scrollTop,
                  scrollHeight: el.scrollHeight,
                  clientHeight: el.clientHeight,
                })
                """
            )
            assert scroll_state["overflowY"] == "auto"
            assert scroll_state["scrollHeight"] > scroll_state["clientHeight"]
            page.locator(".cl-main").hover(position={"x": 20, "y": 120})
            page.mouse.wheel(0, 2000)
            page.wait_for_function(
                "(before) => document.querySelector('.cl-main').scrollTop > before",
                arg=scroll_state["scrollTop"],
            )
            assert_inside_viewport(page, [".cl-lib-open"])
        page.locator(".cl-lib-open").click()
        expect(page.locator("#cl-body")).to_have_value(re.compile("Spark and Airflow"))
        expect(page.locator("#cl-packet-lineage")).to_be_visible()
        expect(page.locator("#cl-packet-lineage")).to_have_attribute("data-packet-state", "packet_created")
        expect(page.locator("#cl-packet-lineage")).to_contain_text("v1")
        expect(page.locator("#cl-packet-lineage")).to_contain_text("confirmed")
        expect(page.locator("#cl-save")).to_be_visible()
        expect(page.locator("#cl-download")).to_be_visible()
        expect(page.locator("#cl-wordcount")).to_contain_text("word")
        if viewport.width <= 840:
            # The physical wheel above, rather than Playwright's automatic
            # locator scrolling, must make the selected document controls
            # reachable to an actual pointer/trackpad user.
            assert_inside_viewport(page, ["#cl-save", "#cl-download", "#cl-body", "#cl-wordcount"])
        page.locator("#cl-save").scroll_into_view_if_needed()
        assert_inside_viewport(page, ["#cl-save", "#cl-download"])
        page.locator("#cl-body").scroll_into_view_if_needed()
        assert_inside_viewport(page, ["#cl-body"])
        page.locator("#cl-body").fill("Dear Acme team,\\n\\nEdited after packet.")
        page.locator("#cl-save").click()
        expect(page.locator("#cl-packet-lineage")).to_have_attribute("data-packet-state", "stale")
        expect(page.locator("#cl-packet-lineage")).to_contain_text("cover_letter_stale")
        assert_no_horizontal_overflow(page)
        assert_no_text_overflow(page, ["#cl-result-name", ".cl-result-actions", ".cl-lib-row"])
        page.locator("#cl-wordcount").scroll_into_view_if_needed()
        assert_inside_viewport(page, ["#cl-wordcount"])
        page.locator("#cl-preview-toggle").click()
        expect(page.locator("#cl-preview")).to_be_visible()
        page.locator("#cl-body").fill("Dear Acme team,\\n\\nEdited after packet with preview intent.")
        page.locator("#cl-download").click()
        page.wait_for_timeout(250)
        targets = page.evaluate("window.__openedPdfTargets")
        assert targets and all(target != "about:blank" for target in targets)
        expect(page.locator("#cl-preview")).to_be_visible()
        if viewport.width <= 840:
            main_scroll = page.evaluate(
                """
                () => {
                  const main = document.querySelector(".cl-main");
                  return {
                    rectLeft: Math.round(main.getBoundingClientRect().left),
                    scrollLeft: main.scrollLeft,
                    overflowX: getComputedStyle(main).overflowX,
                  };
                }
                """
            )
            assert main_scroll["rectLeft"] == 0
            assert main_scroll["scrollLeft"] == 0
            assert main_scroll["overflowX"] in {"clip", "hidden"}
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_cover_letter_save_queue_advances_partial_success_revision(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    posts: list[dict] = []
    held_routes = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/cvs" and req.method == "GET":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV"}]})
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            elif path == "/api/documents" and req.method == "GET":
                _json(route, {
                    "documents": [{"id": LETTER_ID, "type": "cover_letter", "name": "Queued save letter"}],
                    "count": 1,
                })
            elif path == "/api/cover-letter" and req.method == "GET":
                _json(route, {
                    "id": LETTER_ID,
                    "name": "Queued save letter",
                    "body": "Initial body",
                    "updated_at": "revision-1",
                })
            elif path == "/api/cover-letter" and req.method == "POST":
                posts.append(json.loads(req.post_data or "{}"))
                if len(posts) == 1:
                    held_routes.append(route)
                    return
                _json(route, {
                    "saved": True,
                    "id": LETTER_ID,
                    "updated_at": "revision-3",
                })
            elif path == f"/api/cover-letter/{LETTER_ID}/preview":
                route.fulfill(status=200, content_type="text/html", body="<article>Latest saved body</article>")
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/cover-letters?id={LETTER_ID}")
        expect(page.locator("#cl-body")).to_have_value("Initial body")

        page.locator("#cl-body").fill("First snapshot")
        page.locator("#cl-save").click()
        expect(page.locator("#cl-save")).to_be_disabled()
        assert len(posts) == 1
        assert posts[0]["body"] == "First snapshot"
        assert posts[0]["revision"] == "revision-1"

        page.locator("#cl-body").fill("Latest snapshot")
        page.locator("#cl-preview-toggle").click()
        page.wait_for_timeout(50)
        assert len(posts) == 1

        held_routes[0].fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps({
                "error": "Letter saved; stale PDF cleanup is pending.",
                "code": "artifact_cleanup_unavailable",
                "document_updated": True,
                "updated_at": "revision-2",
            }),
        )

        expect(page.locator("#cl-preview")).to_be_visible()
        expect(page.locator("#cl-save")).to_be_enabled()
        assert len(posts) == 2
        assert posts[1]["body"] == "Latest snapshot"
        assert posts[1]["revision"] == "revision-2"
        console_errors[:] = [err for err in console_errors if "status of 503" not in err]
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses, allow_statuses=(503,))
    finally:
        context.close()


def _job_payload_requires_full_jd() -> dict:
    """A local-projection lead whose full JD still needs review (plan 107)."""
    payload = _job_payload(False)
    payload["job"]["source_state"] = {
        **payload["job"]["source_state"],
        "full_jd_status": "missing",
        "requires_full_jd_review": True,
    }
    return payload


def _job_list_row(requires_full_jd: bool) -> dict:
    return {
        "id": JOB_ID,
        "title": "Senior Data Engineer",
        "company": "Acme GmbH",
        "status": "open",
        "starred": True,
        "source_state": {
            "full_jd_status": "missing" if requires_full_jd else "reviewed",
            "requires_full_jd_review": requires_full_jd,
        },
    }


def test_tailor_full_jd_preflight_blocks_score_and_hides_dead_url_field(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    score_bodies: list[dict] = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/ats-profiles":
                _json(route, _profiles_payload())
            elif path == "/api/cvs":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV"}]})
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [_job_list_row(True)], "count": 1})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, _job_payload_requires_full_jd())
            elif path == "/api/jobs/sources/status":
                _json(route, {"url_fetch": {"enabled": False}})
            elif path == "/api/ats-score" and req.method == "POST":
                score_bodies.append(json.loads(req.post_data or "{}"))
                _json(route, _ats_payload("semantic_fit"))
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/tailor?cv_id={CV_ID}")
        expect(page.locator("#tl-job")).to_be_visible()

        # F2: no operator allowlist -> the URL-fetch field is hidden and the
        # disabled note explains why (fail-open stays covered in the empty-
        # repository contract below).
        expect(page.locator("#tl-jd-url")).to_be_hidden()
        expect(page.locator("#tl-url-disabled")).to_be_visible()

        # F1: the option itself announces the review gap...
        option_label = page.locator('#tl-job option[value="' + JOB_ID + '"]').text_content()
        assert "(needs full JD)" in (option_label or "")

        # ...selecting it shows the inline warning with a way out...
        page.locator("#tl-job").select_option(JOB_ID)
        expect(page.locator("#tl-jd")).to_have_value(JD_TEXT)
        warning = page.locator("#tl-selected-job-context .tl-job-warning")
        expect(warning).to_be_visible()
        expect(warning.locator("a")).to_have_attribute("href", f"/jobs?id={JOB_ID}")

        # ...and Score pre-flights instead of dead-ending in a server 422.
        page.locator("#tl-score").click()
        expect(page.locator("#tl-feedback")).to_be_visible()
        expect(page.locator("#tl-feedback")).to_contain_text("full job description")
        expect(page.locator("#tl-feedback a")).to_have_attribute("href", f"/jobs?id={JOB_ID}")
        page.wait_for_timeout(300)
        assert score_bodies == []

        # A manual edit detaches the job and raw-text scoring stays open.
        page.locator("#tl-jd").fill("Manual override job description")
        page.locator("#tl-score").click()
        expect(page.locator("#tl-score-num")).to_have_text(str(PROFILE_SCORES["semantic_fit"]))
        assert score_bodies[-1]["jd_text"] == "Manual override job description"
        assert "job_id" not in score_bodies[-1]
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_tailor_empty_repository_hint_and_url_field_fail_open(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/ats-profiles":
                _json(route, _profiles_payload())
            elif path == "/api/cvs":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV"}]})
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            elif path == "/api/jobs/sources/status":
                # No url_fetch capability key at all -> the client fails open.
                _json(route, {"sources": {}})
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/tailor?cv_id={CV_ID}")
        expect(page.locator("#tl-job")).to_be_visible()

        # F3: an empty repository says so and routes the user to /jobs.
        hint = page.locator("#tl-job-empty")
        expect(hint).to_be_visible()
        expect(hint).to_contain_text("No saved jobs yet")
        expect(hint.locator("a")).to_have_attribute("href", "/jobs")

        # F2 fail-open: without a capability signal the field stays usable
        # (the server still enforces the allowlist).
        expect(page.locator("#tl-jd-url")).to_be_visible()
        expect(page.locator("#tl-url-disabled")).to_be_hidden()
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_cover_letters_full_jd_preflight_blocks_generation(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    generate_bodies: list[dict] = []
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/cvs":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV"}], "count": 1})
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [_job_list_row(True)], "count": 1})
            elif path == f"/api/jobs/{JOB_ID}" and req.method == "GET":
                _json(route, _job_payload_requires_full_jd())
            elif path == "/api/documents":
                _json(route, {"documents": [], "count": 0})
            elif path == "/api/cover-letter/generate" and req.method == "POST":
                generate_bodies.append(json.loads(req.post_data or "{}"))
                _json(route, {"id": LETTER_ID, "name": "x", "body": "y"}, status=201)
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, f"/cover-letters?job_id={JOB_ID}")
        expect(page.locator("#cl-job")).to_have_value(JOB_ID)
        expect(page.locator("#cl-jd")).to_have_value(JD_TEXT)

        warning = page.locator("#cl-job-warning")
        expect(warning).to_be_visible()
        expect(warning.locator("a")).to_have_attribute("href", f"/jobs?id={JOB_ID}")

        option_label = page.locator('#cl-job option[value="' + JOB_ID + '"]').text_content()
        assert "(needs full JD)" in (option_label or "")

        page.locator("#cl-generate").click()
        expect(page.locator("#cl-feedback")).to_be_visible()
        expect(page.locator("#cl-feedback")).to_contain_text("full job description")
        page.wait_for_timeout(300)
        assert generate_bodies == []
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()


def test_cover_letters_empty_repository_hint_links_to_jobs(browser):
    context = browser.new_context(device_scale_factor=1)
    page = context.new_page()
    try:
        console_errors, page_errors, request_failures, bad_responses = _prepare_authenticated_page(page, Viewport(1440, 900))

        def handle_api(route):
            req = route.request
            path = urlparse(req.url).path
            if path == "/api/cvs":
                _json(route, {"cvs": [{"id": CV_ID, "name": "Source CV"}], "count": 1})
            elif path == "/api/jobs" and req.method == "GET":
                _json(route, {"jobs": [], "count": 0})
            elif path == "/api/documents":
                _json(route, {"documents": [], "count": 0})
            else:
                route.fallback()

        page.route("**/api/**", handle_api)
        _goto_authenticated(page, "/cover-letters")
        expect(page.locator("#cl-job")).to_be_visible()
        hint = page.locator("#cl-job-empty")
        expect(hint).to_be_visible()
        expect(hint).to_contain_text("No saved jobs yet")
        expect(hint.locator("a")).to_have_attribute("href", "/jobs")
        assert_clean_console(console_errors, page_errors)
        assert_clean_network(request_failures, bad_responses)
    finally:
        context.close()
