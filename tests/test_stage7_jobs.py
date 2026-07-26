"""Stage 7 tests — saved-jobs workflow (CRUD + linkage + one-click chain).

Covers the feature's contract:

  * **job CRUD**: create by paste AND by URL (the URL path is mocked so no
    network is touched, and we assert the SSRF guard is on that path), the JD is
    parsed + stored, status toggles ('saved'⇄'applied'), delete works;
  * **document↔job linkage**: a tailored CV and a cover letter created with a
    ``job_id`` show up grouped under ``GET /api/jobs/<id>``;
  * **one-click chain**: mocking BOTH LLM calls, ``POST /api/jobs/<id>/generate``
    creates a tailored CV AND a cover letter and links BOTH to the job; and if
    the cover-letter anti-fabrication guard trips, the endpoint reports the
    failure (422) WITHOUT leaving an orphaned, half-linked tailored CV (the CV
    is rolled back — the job ends with neither);
  * **SSRF**: a ``jd_url`` pointing at a private address is blocked (400) before
    any fetch — proving the guard is on the create-by-URL path;
  * **cross-tenant**: user B cannot read / update / delete / generate-against
    user A's job (404) — the real-JWT pattern, gated on env so it skips cleanly
    without TEST_* tokens.

Every LLM call is MOCKED (ANTHROPIC_API_KEY is blank in CI). DB-backed routes
use the in-memory ``fake_supabase`` fixture from conftest (whose
``rpc('consume_managed_credit')`` faithfully decrements + audits). The one-click
chain mocks ``tailor_service.make_client`` (structured JSON) and
``cover_letter_service.make_client`` (letter prose) independently.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid as _uuid
from datetime import date
from pathlib import Path

import pytest
import yaml as _yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"

# The canonical CV used as the SOLE source of facts/evidence in every test
# (identical shape to the Stage 5/6 fixtures so grounding behaves the same).
GROUNDED_CV = {
    "name": "Tim Loehr",
    "accent": "#1f6f43",
    "font": "sans",
    "contact": [{"network": "mail", "username": "tim@example.com"}],
    "experience": [
        {
            "role": "Data Engineer",
            "company": "Meta",
            "location": "Warsaw",
            "start": "2023",
            "end": "Present",
            "bullets": ["Cut pipeline latency by 40% across the ETL stack."],
            "stack": "Python, Spark, Airflow",
        },
        {
            "role": "Data Scientist",
            "company": "Red Bull",
            "location": "Salzburg",
            "start": "2021",
            "end": "2023",
            "bullets": ["Built ML models for anomaly detection in Python."],
        },
    ],
    "skills": [{"label": "Data", "items": "Python, SQL, PyTorch, Spark"}],
}

JD_TEXT = (
    "Title: Senior Data Engineer\n"
    "Company: Acme GmbH\n"
    "You must have experience with Spark and Airflow. 5+ years required.\n"
    "Kubernetes and dbt are nice to have.\n"
    "Responsibilities: build ETL pipelines and data models."
)

# A clean cover letter that only states facts present in GROUNDED_CV (passes the
# anti-fabrication guard; the JD's Acme GmbH / Senior Data Engineer are allowed
# as grounding context).
CLEAN_LETTER = (
    "At Meta I cut pipeline latency by 40% across the ETL stack using Spark "
    "and Airflow, the exact stack this Senior Data Engineer role calls for.\n\n"
    "Earlier, at Red Bull, I built ML models for anomaly detection in Python. "
    "I bring the SQL and PyTorch depth your data-modeling work needs.\n\n"
    "I would welcome the chance to bring this to Acme GmbH."
)

# A letter that hallucinates an employer absent from the CV + the JD — trips the
# anti-fabrication guard (used for the orphan-prevention test).
FABRICATED_LETTER = (
    "At Globex Industries I drove a 250% revenue increase and led a team of "
    "80 engineers, delivering record results for the company.\n\n"
    "I would be thrilled to bring this track record to your team."
)


# ===========================================================================
#  Mock helpers.
# ===========================================================================
def _structured_response(suggestions=None, summary=None, warnings=None) -> str:
    """A valid structured CV-tailoring JSON the mock tailoring LLM returns."""
    return json.dumps(
        {
            "fit": {"score": 80, "verdict": "Strong match", "target_role": "Senior Data Engineer"},
            "keywords": {"present": [], "missing": [], "inferable": []},
            "suggestions": suggestions or [],
            "summary": summary or {"recommended": False, "text": "", "rationale": ""},
            "warnings": warnings or [],
        }
    )


def _mock_tailor_llm(monkeypatch, raw_text: str):
    """Patch tailor_service.make_client so .complete returns ``raw_text``."""
    import tailor_service as ts

    class _FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key

        def complete(self, *, system, user, model=None, max_tokens=1024, cache_system=True):
            return raw_text

    monkeypatch.setattr(ts, "make_client", lambda provider, api_key: _FakeClient(api_key))


def _mock_letter_llm(monkeypatch, letter_text: str):
    """Patch cover_letter_service.make_client so .complete returns the letter."""
    import cover_letter_service as cl

    class _FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key

        def complete(self, *, system, user, model=None, max_tokens=1024, cache_system=True):
            return letter_text

    monkeypatch.setattr(cl, "make_client", lambda provider, api_key: _FakeClient(api_key))


def _seed_cv(store, name="My CV", cv_model=None):
    """Insert a CV document into the fake store; return (id, yaml_blob)."""
    blob = _yaml.safe_dump(cv_model or GROUNDED_CV, sort_keys=False, allow_unicode=True)
    rows = store.tables.setdefault("documents", [])
    doc_id = str(_uuid.uuid4())
    rows.append(
        {
            "id": doc_id,
            "owner_id": store.owner_id,
            "type": "cv",
            "name": name,
            "yaml_blob": blob,
            "accent": "#1f6f43",
            "font": "sans",
            "deleted_at": None,
            "updated_at": 1,
        }
    )
    return doc_id, blob


def _seed_profile(store, owner_id, *, credits):
    store.tables.setdefault("profiles", []).append(
        {"id": owner_id, "managed_credits_remaining": credits}
    )


def _jobs(store):
    return store.tables.get("saved_jobs", [])


def _docs(store):
    return [d for d in store.tables.get("documents", []) if not d.get("deleted_at")]


def _cv_docs(store):
    return [d for d in _docs(store) if d["type"] == "cv"]


def _letter_docs(store):
    return [d for d in _docs(store) if d["type"] == "cover_letter"]


def _make_job(client, **body):
    """POST /api/jobs and return the parsed response."""
    return client.post("/api/jobs", json=body)


def _review_and_apply_packet(client, job_id: str, source_cv_id: str) -> tuple[dict, dict]:
    review = client.post(f"/api/jobs/{job_id}/review", json={"source_cv_id": source_cv_id})
    assert review.status_code == 200, review.get_data(as_text=True)
    applied = client.post(
        f"/api/jobs/{job_id}/apply-review",
        json={
            "source_cv_id": source_cv_id,
            "review_token": review.get_json()["review_token"],
        },
    )
    assert applied.status_code == 201, applied.get_data(as_text=True)
    applied_body = applied.get_json()
    confirmed = client.post(
        f"/api/jobs/{job_id}/packets/{applied_body['packet_id']}/confirm-cover-letter-review",
        json={"cover_letter_hash": applied_body["cover_letter_hash"]},
    )
    assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
    return review.get_json(), applied.get_json()


# ===========================================================================
#  1. Job CRUD by paste — create, list, detail, status toggle, delete.
# ===========================================================================
class TestJobCrudPaste:
    def test_create_by_paste_parses_and_stores_jd(self, server_module, client, fake_supabase):
        res = _make_job(client, jd_text=JD_TEXT)
        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        # The deterministic parser filled title + company from the JD labels.
        assert body["title"] == "Senior Data Engineer"
        assert body["company"] == "Acme GmbH"
        assert body["status"] == "saved"
        assert "id" in body

        # The row stored both the raw JD text AND the parsed keyed JSON.
        rows = _jobs(fake_supabase)
        assert len(rows) == 1
        row = rows[0]
        assert row["jd_text"] == JD_TEXT
        assert isinstance(row["jd_json"], dict)
        assert row["jd_json"]["title"] == "Senior Data Engineer"
        assert any("Spark" in k for k in row["jd_json"]["keywords"])

    def test_create_by_paste_sanitizes_reference_source_url(
        self, server_module, client, fake_supabase
    ):
        raw_url = (
            "https://" + "user" + ":" + "pass"
            + "@www.linkedin.com/jobs/view/123?li_at=secret-cookie#session"
        )
        res = _make_job(client, jd_text=JD_TEXT, jd_url=raw_url)

        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        row = _jobs(fake_supabase)[0]
        assert row["source_url"] == "https://www.linkedin.com/jobs/view/123"
        assert body["source_url"] == "https://www.linkedin.com/jobs/view/123"
        detail = client.get(f"/api/jobs/{body['id']}").get_json()["job"]
        listed = client.get("/api/jobs").get_json()["jobs"][0]
        assert detail["source_url"] == "https://www.linkedin.com/jobs/view/123"
        assert listed["source_url"] == "https://www.linkedin.com/jobs/view/123"
        raw = json.dumps({"body": body, "detail": detail, "listed": listed}).lower()
        for forbidden in ("li_at", "secret-cookie", "user:pass", "session"):
            assert forbidden not in raw

    def test_create_by_paste_drops_non_http_source_url(
        self, server_module, client, fake_supabase
    ):
        res = _make_job(client, jd_text=JD_TEXT, jd_url="javascript:alert(document.cookie)")

        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        row = _jobs(fake_supabase)[0]
        assert row["source_url"] in (None, "")
        assert body["source_url"] == ""

    @pytest.mark.parametrize(
        "raw_url",
        [
            "http://2130706433/apply",
            "http://0177.0.0.1/apply",
            "http://0x7f.0.0.1/apply",
            "http://127.1/apply",
        ],
    )
    def test_create_by_paste_drops_numeric_private_source_hosts(
        self, server_module, client, fake_supabase, raw_url
    ):
        res = _make_job(client, jd_text=JD_TEXT, jd_url=raw_url)

        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        row = _jobs(fake_supabase)[0]
        assert row["source_url"] in (None, "")
        assert body["source_url"] == ""

    def test_create_by_paste_strips_secret_shaped_source_url_path(
        self, server_module, client, fake_supabase
    ):
        res = _make_job(
            client,
            jd_text=JD_TEXT,
            jd_url="https://jobs.example.com/apply/token/secret",
        )

        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        row = _jobs(fake_supabase)[0]
        assert row["source_url"] == "https://jobs.example.com"
        assert body["source_url"] == "https://jobs.example.com"

    def test_explicit_title_company_override_parse(self, server_module, client, fake_supabase):
        res = _make_job(client, jd_text=JD_TEXT, title="Custom Role", company="Custom Co")
        assert res.status_code == 201
        body = res.get_json()
        assert body["title"] == "Custom Role"
        assert body["company"] == "Custom Co"

    def test_list_jobs_newest_first(self, server_module, client, fake_supabase):
        _make_job(client, jd_text="Title: First\nCompany: A\nResponsibilities: work.")
        _make_job(client, jd_text="Title: Second\nCompany: B\nResponsibilities: work.")
        res = client.get("/api/jobs")
        assert res.status_code == 200
        body = res.get_json()
        assert body["count"] == 2
        # Newest first (the fake stamps strictly increasing created_at).
        assert body["jobs"][0]["title"] == "Second"
        assert body["jobs"][1]["title"] == "First"
        # The compact list shape omits the bulky JD payload.
        assert "jd_text" not in body["jobs"][0]

    def test_detail_returns_full_job(self, server_module, client, fake_supabase):
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        res = client.get(f"/api/jobs/{jid}")
        assert res.status_code == 200
        body = res.get_json()
        assert body["job"]["jd_text"] == JD_TEXT
        assert body["job"]["jd_json"]["company"] == "Acme GmbH"
        # No documents linked yet.
        assert body["cvs"] == []
        assert body["cover_letters"] == []

    def test_status_toggle_saved_to_applied(self, server_module, client, fake_supabase):
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        res = client.post(f"/api/jobs/{jid}/status", json={"status": "applied"})
        assert res.status_code == 200
        assert res.get_json()["status"] == "applied"
        # Persisted on the row + reflected in the list.
        assert _jobs(fake_supabase)[0]["status"] == "applied"
        back = client.post(f"/api/jobs/{jid}/status", json={"status": "saved"})
        assert back.get_json()["status"] == "saved"

    def test_starred_job_persists_as_safe_metadata(self, server_module, client, fake_supabase):
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        _jobs(fake_supabase)[0]["source_metadata"] = {
            "origin": "local_jobs_db_read_only",
            "site": "linkedin",
            "fit_note": "high signal",
            "source_links": [{"url": "https://jobs.example.com/role", "text": "Role"}],
            "session_token": "must-not-survive",
        }

        res = client.patch(f"/api/jobs/{jid}", json={"starred": True})

        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        row = _jobs(fake_supabase)[0]
        assert body["starred"] is True
        assert body["source_metadata"]["starred"] is True
        assert body["source_metadata"]["origin"] == "local_jobs_db_read_only"
        assert body["source_metadata"]["site"] == "linkedin"
        assert body["source_metadata"]["fit_note"] == "high signal"
        assert body["source_metadata"]["source_links"][0]["url"] == "https://jobs.example.com/role"
        assert "session_token" not in body["source_metadata"]
        assert row["source_metadata"]["starred"] is True
        assert row["source_metadata"]["origin"] == "local_jobs_db_read_only"
        assert row["source_metadata"]["site"] == "linkedin"
        assert row["source_metadata"]["fit_note"] == "high signal"
        assert row["source_metadata"]["source_links"][0]["url"] == "https://jobs.example.com/role"
        assert "session_token" not in row["source_metadata"]

        listed = client.get("/api/jobs").get_json()["jobs"][0]
        detail = client.get(f"/api/jobs/{jid}").get_json()["job"]
        assert listed["starred"] is True
        assert detail["starred"] is True

        off = client.patch(f"/api/jobs/{jid}", json={"starred": False})
        assert off.status_code == 200
        assert off.get_json()["starred"] is False
        assert _jobs(fake_supabase)[0]["source_metadata"]["starred"] is False

        bad = client.patch(f"/api/jobs/{jid}", json={"starred": "yes"})
        assert bad.status_code == 400
        assert bad.get_json()["code"] == "bad_starred"
        assert _jobs(fake_supabase)[0]["source_metadata"]["starred"] is False

    def test_status_rejects_garbage(self, server_module, client, fake_supabase):
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        res = client.post(f"/api/jobs/{jid}/status", json={"status": "ghosted"})
        assert res.status_code == 400
        assert res.get_json()["code"] == "bad_status"

    def test_delete_job(self, server_module, client, fake_supabase):
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        res = client.delete(f"/api/jobs/{jid}")
        assert res.status_code == 200
        assert res.get_json()["deleted"] == jid
        assert _jobs(fake_supabase) == []
        # Gone now → 404 on detail.
        assert client.get(f"/api/jobs/{jid}").status_code == 404

    def test_unknown_job_is_404(self, server_module, client, fake_supabase):
        rid = str(_uuid.uuid4())
        assert client.get(f"/api/jobs/{rid}").status_code == 404
        assert client.delete(f"/api/jobs/{rid}").status_code == 404
        assert client.post(f"/api/jobs/{rid}/status", json={"status": "saved"}).status_code == 404

    def test_create_without_jd_is_400(self, server_module, client, fake_supabase):
        res = _make_job(client)
        assert res.status_code == 400
        assert res.get_json()["code"] == "no_jd"

    def test_invalid_job_id_is_400(self, server_module, client, fake_supabase):
        assert client.get("/api/jobs/not-a-uuid").status_code == 400
        assert client.delete("/api/jobs/not-a-uuid").status_code == 400


# ===========================================================================
#  2. Create by URL — SSRF-guarded, no network. The guard is on the path.
# ===========================================================================
class TestJobCrudUrl:
    def test_create_by_url_uses_ssrf_guard_then_fetches(self, server_module, client, fake_supabase, monkeypatch):
        """A jd_url is fetched via ssrf.safe_fetch_url (mocked — no network).
        We assert the guarded fetcher was the code path and its bytes were
        parsed + stored, and that source_url is recorded."""
        import jobs_service as js

        called = {}

        def _fake_fetch(url, *, timeout=8.0, max_bytes=64 * 1024):
            called["url"] = url
            return JD_TEXT.encode("utf-8")

        # Patch the SSRF-guarded fetcher the service calls (proves it's on the
        # path); the real guard is exercised separately below.
        monkeypatch.setenv("ONEPAGER_JOB_URL_ALLOWLIST", "jobs.example.com")
        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _fake_fetch)

        credentialed_url = (
            "https://" + "user" + ":" + "pass"
            + "@jobs.example.com/posting/123?li_at=secret-cookie#session"
        )
        res = _make_job(client, jd_url=credentialed_url)
        assert res.status_code == 201, res.get_data(as_text=True)
        assert called["url"] == "https://jobs.example.com/posting/123"
        body = res.get_json()
        assert body["title"] == "Senior Data Engineer"
        # The fetched JD text was parsed + stored, and the source URL recorded.
        row = _jobs(fake_supabase)[0]
        assert row["jd_text"] == JD_TEXT
        assert row["source_url"] == "https://jobs.example.com/posting/123"
        assert body["source_url"] == "https://jobs.example.com/posting/123"

    def test_url_import_requires_explicit_source_allowlist(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.delenv("ONEPAGER_JOB_URL_ALLOWLIST", raising=False)
        res = _make_job(client, jd_url="https://jobs.example.com/posting/123")
        assert res.status_code == 400
        assert res.get_json()["code"] == "source_not_allowed"
        assert _jobs(fake_supabase) == []

    def test_paste_wins_over_url(self, server_module, client, fake_supabase, monkeypatch):
        """When both jd_text and jd_url are given, the paste is used (no fetch)."""
        import jobs_service as js

        def _boom(*a, **k):  # the fetcher must NOT be called when text is given
            raise AssertionError("safe_fetch_url should not be called when jd_text is present")

        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _boom)
        res = _make_job(client, jd_text=JD_TEXT, jd_url="https://example.com/x")
        assert res.status_code == 201
        assert _jobs(fake_supabase)[0]["jd_text"] == JD_TEXT

    def test_linkedin_url_without_paste_is_rejected_before_fetch(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import jobs_service as js

        def _boom(*_a, **_k):
            raise AssertionError("LinkedIn URLs must not be fetched")

        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _boom)
        res = _make_job(client, jd_url="https://www.linkedin.com/jobs/view/123")
        assert res.status_code == 400
        assert res.get_json()["code"] == "linkedin_manual_required"
        assert _jobs(fake_supabase) == []

    def test_linkedin_pasted_text_is_saved_as_manual_provenance(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import jobs_service as js

        def _boom(*_a, **_k):
            raise AssertionError("Pasted LinkedIn jobs must not be fetched")

        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _boom)
        res = _make_job(
            client,
            jd_text=JD_TEXT,
            jd_url="https://de.linkedin.com/jobs/view/123",
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        assert body["source_provider"] == "linkedin"
        assert body["source_type"] == "manual_paste"
        assert body["verification_status"] == "needs_review"
        row = _jobs(fake_supabase)[0]
        assert row["source_url"] == "https://de.linkedin.com/jobs/view/123"
        assert row["source_provider"] == "linkedin"
        assert row["source_type"] == "manual_paste"
        assert row["verification_status"] == "needs_review"
        assert row["source_metadata"]["terms_basis"] == "manual_user_supplied"
        assert row["source_metadata"]["manual_verification_required"] is True
        assert len(row["content_hash"]) == 64

    def test_linkedin_url_is_rejected_even_when_allowlisted(self, server_module, client, fake_supabase, monkeypatch):
        import jobs_service as js

        def _boom_fetch(*_a, **_k):
            raise AssertionError("allowlisted LinkedIn URLs must not be fetched")

        def _boom_rate(*_a, **_k):
            raise AssertionError("LinkedIn rejection must happen before URL allowlist/rate-limit path")

        monkeypatch.setenv("ONEPAGER_JOB_URL_ALLOWLIST", "linkedin.com,www.linkedin.com")
        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _boom_fetch)
        monkeypatch.setattr(js, "_enforce_source_rate_limit", _boom_rate)

        res = _make_job(client, jd_url="https://www.linkedin.com/jobs/view/123")
        assert res.status_code == 400
        assert res.get_json()["code"] == "linkedin_manual_required"
        assert _jobs(fake_supabase) == []

    def test_url_fetch_transport_failure_returns_fetch_failed(self, server_module, client, fake_supabase, monkeypatch):
        import jobs_service as js

        called = []

        def _fail_fetch(url, *, timeout=8.0, max_bytes=64 * 1024):
            called.append(url)
            raise TimeoutError("timed out")

        monkeypatch.setenv("ONEPAGER_JOB_URL_ALLOWLIST", "jobs.example.com")
        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _fail_fetch)

        res = _make_job(client, jd_url="https://jobs.example.com/posting/123")
        body = res.get_json()
        assert res.status_code == 400
        assert body["code"] == "fetch_failed"
        assert "TimeoutError" in body["error"]
        assert called == ["https://jobs.example.com/posting/123"]
        assert _jobs(fake_supabase) == []

    def test_url_import_rate_limit_exhaustion_returns_429_before_fetch(self, server_module, client, fake_supabase, monkeypatch):
        import jobs_service as js

        calls = []

        def _fake_fetch(url, *, timeout=8.0, max_bytes=64 * 1024):
            calls.append(url)
            return JD_TEXT.encode("utf-8")

        monkeypatch.setenv("ONEPAGER_JOB_URL_ALLOWLIST", "jobs.example.com")
        monkeypatch.setattr(js, "JOB_URL_RATE_LIMIT_PER_MINUTE", 1)
        monkeypatch.setattr(js, "_URL_FETCH_BUCKETS", {})
        monkeypatch.setattr(js.time, "time", lambda: 1_700_000_000.0)
        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _fake_fetch)

        first = _make_job(client, jd_url="https://jobs.example.com/posting/1")
        second = _make_job(client, jd_url="https://jobs.example.com/posting/2")

        assert first.status_code == 201, first.get_data(as_text=True)
        assert second.status_code == 429
        assert second.get_json()["code"] == "source_rate_limited"
        assert calls == ["https://jobs.example.com/posting/1"]
        assert len(_jobs(fake_supabase)) == 1

    def test_private_url_is_blocked_by_ssrf(self, server_module, client, fake_supabase):
        """A jd_url to a private/loopback address is blocked (400) BEFORE any
        fetch — exercising the REAL ssrf.assert_safe_url on the create path.
        No monkeypatch: this hits the genuine guard."""
        for bad in (
            "http://127.0.0.1:5567/api/cv",        # loopback (the app itself)
            "http://169.254.169.254/latest/meta",  # cloud metadata
            "http://10.0.0.5/posting",             # RFC-1918 private
            "ftp://example.com/posting",           # disallowed scheme
        ):
            res = _make_job(client, jd_url=bad)
            assert res.status_code == 400, f"{bad!r} should be blocked: {res.get_data(as_text=True)}"
        # Nothing was saved for any blocked URL.
        assert _jobs(fake_supabase) == []


# ===========================================================================
#  3. Document↔job linkage — tailored CV + cover letter show under the job.
# ===========================================================================
class TestLinkage:
    def test_tailor_with_job_id_links_under_job(self, server_module, client, fake_supabase, monkeypatch):
        """A CV tailored with job_id is stamped + appears in the job detail."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT, "job_id": jid})
        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        tailored_id = body["tailored_cv_id"]
        assert body["packet_lineage"]["job_id"] == jid
        assert body["packet_lineage"]["source_cv_id"] == cv_id
        assert body["packet_lineage"]["tailored_cv_id"] == tailored_id
        assert body["packet_lineage"]["document_role"] == "tailored_cv"
        assert body["packet_lineage"]["status"] == "stale"
        assert body["packet_lineage"]["stored_status"] == "review_applied"
        assert body["packet_lineage"]["source_cv_hash"]
        assert body["packet_lineage"]["job_content_hash"]
        assert body["packet_lineage"]["tailored_cv_hash"]
        assert body["packet_lineage"]["score_system_id"] == "semantic_fit"
        assert body["packet_lineage"]["prompt_version"] == "jobs_packet.v1"
        assert body["packet_lineage"]["packet_version"] == 1
        assert body["packet_lineage"]["review_id"] == body["review_id"]
        assert body["packet_lineage"]["cover_letter_review_status"] == "pending"
        assert body["packet_lineage"]["cover_letter_review_confirmed_at"] is None
        assert body["packet_lineage"]["stale_reasons"] == ["cover_letter_missing"]
        # The new doc carries the job_id.
        tailored = next(d for d in _cv_docs(fake_supabase) if d["id"] == tailored_id)
        assert tailored["job_id"] == jid

        detail = client.get(f"/api/jobs/{jid}").get_json()
        assert [c["id"] for c in detail["cvs"]] == [tailored_id]
        assert detail["cover_letters"] == []
        assert detail["packets"][0]["id"] == body["packet_lineage"]["packet_id"]
        assert detail["packets"][0]["status"] == "stale"
        assert detail["packets"][0]["stale_reasons"] == ["cover_letter_missing"]
        docs = {row["id"]: row for row in client.get("/api/documents").get_json()["documents"]}
        assert docs[tailored_id]["packet_lineage"]["packet_id"] == body["packet_lineage"]["packet_id"]

    def test_tailor_with_job_id_rolls_back_cv_when_lineage_insert_fails(self, server_module, client, fake_supabase, monkeypatch):
        """Legacy direct tailor must not leave a job-linked CV without packet lineage."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        def fail_lineage(**_kwargs):
            raise RuntimeError("lineage insert failed")

        monkeypatch.setattr(server_module.jobs_svc, "record_tailored_cv_packet_lineage", fail_lineage)

        with pytest.raises(RuntimeError, match="lineage insert failed"):
            client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT, "job_id": jid})

        assert [doc["id"] for doc in _cv_docs(fake_supabase)] == [cv_id]
        assert client.get(f"/api/jobs/{jid}").get_json()["cvs"] == []

    def test_cover_letter_with_job_id_links_under_job(self, server_module, client, fake_supabase, monkeypatch):
        """A cover letter generated with job_id appears under the job detail."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        res = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": cv_id, "jd_text": JD_TEXT, "job_id": jid},
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        letter_id = body["id"]
        assert body["packet_lineage"]["job_id"] == jid
        assert body["packet_lineage"]["source_cv_id"] == cv_id
        assert body["packet_lineage"]["cover_letter_id"] == letter_id
        assert body["packet_lineage"]["document_role"] == "cover_letter"
        assert body["packet_lineage"]["status"] == "stale"
        assert body["packet_lineage"]["stored_status"] == "review_applied"
        assert body["packet_lineage"]["source_cv_hash"]
        assert body["packet_lineage"]["job_content_hash"]
        assert body["packet_lineage"]["cover_letter_hash"]
        assert body["packet_lineage"]["score_system_id"] == "semantic_fit"
        assert body["packet_lineage"]["prompt_version"] == "jobs_packet.v1"
        assert body["packet_lineage"]["packet_version"] == 1
        assert body["packet_lineage"]["cover_letter_review_status"] == "pending"
        assert body["packet_lineage"]["cover_letter_review_confirmed_at"] is None
        assert body["packet_lineage"]["stale_reasons"] == ["tailored_cv_missing"]
        letter = next(d for d in _letter_docs(fake_supabase) if d["id"] == letter_id)
        # job_id is serialized into the cover-letter blob.
        assert jid in (letter["yaml_blob"] or "")

        detail = client.get(f"/api/jobs/{jid}").get_json()
        assert [c["id"] for c in detail["cover_letters"]] == [letter_id]
        assert detail["packets"][0]["id"] == body["packet_lineage"]["packet_id"]
        assert detail["packets"][0]["status"] == "stale"
        assert detail["packets"][0]["stale_reasons"] == ["tailored_cv_missing"]
        docs = {row["id"]: row for row in client.get("/api/documents").get_json()["documents"]}
        assert docs[letter_id]["packet_lineage"]["packet_id"] == body["packet_lineage"]["packet_id"]

    def test_cover_letter_job_id_only_uses_saved_job_description(self, server_module, client, fake_supabase, monkeypatch):
        """The repository picker can generate from job_id without posting JD text."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        res = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": cv_id, "job_id": jid},
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        assert body["packet_lineage"]["job_id"] == jid
        letter = next(d for d in _letter_docs(fake_supabase) if d["id"] == body["id"])
        assert letter["job_id"] == jid
        assert jid in (letter["yaml_blob"] or "")

    def test_partial_lineage_insert_failure_restores_existing_partial_row(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """A failed incompatible partial replacement cannot archive the previous partial row."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        tailored = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT, "job_id": jid})
        assert tailored.status_code == 201, tailored.get_data(as_text=True)
        packet_id = tailored.get_json()["packet_lineage"]["packet_id"]
        letter_id = str(_uuid.uuid4())
        fake_supabase.tables.setdefault("documents", []).append(
            {
                "id": letter_id,
                "owner_id": fake_supabase.owner_id,
                "type": "cover_letter",
                "name": "Manual Letter",
                "job_id": jid,
                "yaml_blob": "company: Acme GmbH\nrole: Senior Data Engineer\nbody: Manual\n",
                "deleted_at": None,
                "updated_at": 20,
            }
        )

        import jobs_service

        def _fail_insert(_sb, _payload):
            raise jobs_service.JobError(
                500,
                {"error": "Failed to save packet lineage.", "code": "packet_insert_failed"},
            )

        monkeypatch.setattr(jobs_service, "_insert_packet_payload", _fail_insert)
        with server_module.app.test_request_context(headers={"Authorization": "Bearer test-token"}):
            server_module.auth_mod.auth_g().owner_id = fake_supabase.owner_id
            server_module.auth_mod.auth_g().access_token = "test-token"
            with pytest.raises(jobs_service.JobError) as exc:
                jobs_service.record_cover_letter_packet_lineage(
                    job_id=jid,
                    source_cv_id=cv_id,
                    cover_letter_id=letter_id,
                    provider="anthropic",
                    model=None,
                    score_system_id="keyword_screen",
                    now_iso="2026-07-02T12:00:00+00:00",
                )

        assert exc.value.payload["code"] == "packet_insert_failed"
        row = fake_supabase.tables["application_packets"][0]
        assert row["id"] == packet_id
        assert row["status"] == "review_applied"
        assert row.get("archived_at") is None

    def test_tailor_without_job_id_still_works(self, server_module, client, fake_supabase, monkeypatch):
        """The Stage-6 path is unchanged when job_id is absent (no link)."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT})
        assert res.status_code == 201
        tailored_id = res.get_json()["tailored_cv_id"]
        tailored = next(d for d in _cv_docs(fake_supabase) if d["id"] == tailored_id)
        assert tailored.get("job_id") is None


# ===========================================================================
#  4. One-click chain — tailor a CV + write a cover letter, both linked.
# ===========================================================================
class TestOneClickChain:
    def test_generate_threads_provider_and_model(self, server_module, client, fake_supabase, monkeypatch):
        import jobs_service

        job_id = str(_uuid.uuid4())
        source_cv_id = str(_uuid.uuid4())
        seen = {}

        def _fake_generate_for_job(**kwargs):
            seen.update(kwargs)
            return {"tailored_cv_id": "cv-out", "cover_letter_id": "cl-out", "ats": None}

        monkeypatch.setattr(jobs_service, "generate_for_job", _fake_generate_for_job)

        res = client.post(
            f"/api/jobs/{job_id}/generate",
            json={
                "source_cv_id": source_cv_id,
                "provider": "gemini",
                "model": "gemini-2.5-pro",
                "profile": "keyword_screen",
                "force": True,
            },
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        assert seen["job_id"] == job_id
        assert seen["source_cv_id"] == source_cv_id
        assert seen["provider"] == "gemini"
        assert seen["model"] == "gemini-2.5-pro"
        assert seen["profile"] == "keyword_screen"
        assert seen["force"] is True

    def test_generate_requires_review_first_without_creating_documents(self, server_module, client, fake_supabase, monkeypatch):
        """POST /api/jobs/<id>/generate cannot complete a packet without review evidence."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        assert len(_cv_docs(fake_supabase)) == 1  # only the source CV
        assert len(_letter_docs(fake_supabase)) == 0

        res = client.post(f"/api/jobs/{jid}/generate", json={"source_cv_id": cv_id})
        assert res.status_code == 409, res.get_data(as_text=True)
        assert res.get_json()["code"] == "review_required"
        assert len(_cv_docs(fake_supabase)) == 1
        assert len(_letter_docs(fake_supabase)) == 0
        assert fake_supabase.tables.get("application_packets", []) == []
        prof = next(p for p in fake_supabase.tables["profiles"] if p["id"] == FAKE_OWNER)
        assert prof["managed_credits_remaining"] == 3

    def test_generate_grounds_cover_letter_in_tailored_cv(self, server_module, client, fake_supabase, monkeypatch):
        """The packet letter uses the reviewed/tailored artifact, not source CV."""
        import jobs_service

        cv_id, _ = _seed_cv(fake_supabase)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        tailored_id = str(_uuid.uuid4())
        letter_id = str(_uuid.uuid4())
        seen: dict[str, object] = {}

        monkeypatch.setattr(
            jobs_service.tailor_svc,
            "apply_tailor_review",
            lambda **kwargs: {
                "tailored_cv_id": tailored_id,
                "ats": {"score": 91},
                "source_hash": "a" * 64,
                "jd_hash": "b" * 64,
                "tailored_hash": "c" * 64,
                "review_id": "review-grounding",
            },
        )

        def _fake_letter(**kwargs):
            seen.update(kwargs)
            return {"id": letter_id}

        monkeypatch.setattr(jobs_service.cl_service, "generate_cover_letter", _fake_letter)
        monkeypatch.setattr(
            jobs_service,
            "_ensure_pending_packet_row",
            lambda **kwargs: {
                "id": "packet-grounding",
                "cover_letter_hash": "d" * 64,
                "cover_letter_review_status": "pending",
            },
        )

        result = jobs_service.apply_packet_review(
            job_id=jid,
            source_cv_id=cv_id,
            review_token={"signed": "review"},
            now_iso="2026-07-02T12:00:00+00:00",
        )

        assert result["tailored_cv_id"] == tailored_id
        assert result["cover_letter_id"] == letter_id
        assert seen["cv_id"] == tailored_id

    def test_review_then_apply_creates_linked_packet(self, server_module, client, fake_supabase, monkeypatch):
        """The job workbench can review CV changes before creating the packet."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {"id": "tighten", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": "Cut ETL pipeline latency 40% with Spark and Airflow."},
        ]
        _mock_tailor_llm(monkeypatch, _structured_response(suggestions))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, source_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        review = client.post(f"/api/jobs/{jid}/review", json={"source_cv_id": cv_id})
        assert review.status_code == 200, review.get_data(as_text=True)
        review_body = review.get_json()
        assert review_body["review_token"]
        assert review_body["source_cv_id"] == cv_id
        assert review_body["job_id"] == jid
        assert len(review_body["source_hash"]) == 64
        assert len(review_body["jd_hash"]) == 64
        assert len(review_body["tailored_hash"]) == 64
        assert review_body["source_revision"]
        assert review_body["provider"] == "anthropic"
        assert review_body["created_at"]
        assert review_body["diff"]["changed"] is True
        assert len(_cv_docs(fake_supabase)) == 1
        assert _cv_docs(fake_supabase)[0]["yaml_blob"] == source_blob
        assert _letter_docs(fake_supabase) == []

        applied = client.post(
            f"/api/jobs/{jid}/apply-review",
            json={
                "source_cv_id": cv_id,
                "review_token": review_body["review_token"],
            },
        )
        assert applied.status_code == 201, applied.get_data(as_text=True)
        body = applied.get_json()
        assert body["tailored_cv_id"] != cv_id
        assert body["cover_letter_id"]
        assert body["packet_id"]
        assert len(body["cover_letter_hash"]) == 64
        assert body["cover_letter_review_status"] == "pending"
        assert body["requires_cover_letter_review"] is True
        assert body["review_id"] == review_body["review_id"]
        tailored = next(d for d in _cv_docs(fake_supabase) if d["id"] == body["tailored_cv_id"])
        letter = next(d for d in _letter_docs(fake_supabase) if d["id"] == body["cover_letter_id"])
        assert tailored["job_id"] == jid
        assert letter["job_id"] == jid
        assert "Cut ETL pipeline latency 40%" in tailored["yaml_blob"]
        packet = fake_supabase.tables["application_packets"][0]
        assert packet["source_cv_id"] == cv_id
        assert packet["tailored_cv_id"] == body["tailored_cv_id"]
        assert packet["cover_letter_id"] == body["cover_letter_id"]
        assert packet["status"] == "review_applied"
        assert packet["review_status"] == "reviewed"
        assert packet["prompt_version"] == "jobs_packet.v1"
        assert packet["score_system_id"] == "semantic_fit"
        assert packet["packet_version"] == 1
        assert packet["source_cv_hash"] == review_body["source_hash"]
        assert packet["job_content_hash"] == review_body["jd_hash"]
        assert packet["tailored_cv_hash"] == review_body["tailored_hash"]
        assert len(packet["cover_letter_hash"]) == 64
        assert packet["cover_letter_review_status"] == "pending"
        assert packet["cover_letter_review_confirmed_at"] is None
        assert packet["cover_letter_review_id"] == review_body["review_id"]

        confirmed = client.post(
            f"/api/jobs/{jid}/packets/{body['packet_id']}/confirm-cover-letter-review",
            json={"cover_letter_hash": body["cover_letter_hash"]},
        )
        assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
        packet = fake_supabase.tables["application_packets"][0]
        assert packet["status"] == "packet_created"
        assert packet["cover_letter_review_status"] == "confirmed"
        assert packet["cover_letter_review_confirmed_at"]
        detail_packet = client.get(f"/api/jobs/{jid}").get_json()["packets"][0]
        assert detail_packet["document_links"] == {
            "source_cv": f"/app?id={cv_id}",
            "tailored_cv": f"/app?id={body['tailored_cv_id']}",
            "cover_letter": f"/cover-letters?id={body['cover_letter_id']}",
        }
        assert detail_packet["cover_letter_review_status"] == "confirmed"

    def test_apply_review_creates_pending_packet_until_cover_letter_hash_confirmed(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Packet completion is blocked until the generated cover-letter hash is confirmed."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _source_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        review = client.post(f"/api/jobs/{jid}/review", json={"source_cv_id": cv_id})
        assert review.status_code == 200, review.get_data(as_text=True)
        applied = client.post(
            f"/api/jobs/{jid}/apply-review",
            json={"source_cv_id": cv_id, "review_token": review.get_json()["review_token"]},
        )

        assert applied.status_code == 201, applied.get_data(as_text=True)
        body = applied.get_json()
        assert body["requires_cover_letter_review"] is True
        assert body["cover_letter_review_status"] == "pending"
        assert len(_cv_docs(fake_supabase)) == 2
        assert len(_letter_docs(fake_supabase)) == 1
        packet = fake_supabase.tables["application_packets"][0]
        assert packet["status"] == "review_applied"
        assert packet["cover_letter_review_status"] == "pending"
        assert packet["cover_letter_review_confirmed_at"] is None

        missing_hash = client.post(
            f"/api/jobs/{jid}/packets/{body['packet_id']}/confirm-cover-letter-review",
            json={},
        )
        wrong_hash = client.post(
            f"/api/jobs/{jid}/packets/{body['packet_id']}/confirm-cover-letter-review",
            json={"cover_letter_hash": "0" * 64},
        )

        assert missing_hash.status_code == 400
        assert missing_hash.get_json()["code"] == "cover_letter_hash_required"
        assert wrong_hash.status_code == 409
        assert wrong_hash.get_json()["code"] == "cover_letter_hash_mismatch"
        assert fake_supabase.tables["application_packets"][0]["status"] == "review_applied"

    def test_apply_review_rejects_changed_job_description(self, server_module, client, fake_supabase, monkeypatch):
        """A reviewed packet cannot be applied after the job description changes."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _source_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        review = client.post(f"/api/jobs/{jid}/review", json={"source_cv_id": cv_id})
        assert review.status_code == 200, review.get_data(as_text=True)
        job_row = next(j for j in fake_supabase.tables["saved_jobs"] if j["id"] == jid)
        job_row["jd_text"] = "Changed job requirements after review."

        applied = client.post(
            f"/api/jobs/{jid}/apply-review",
            json={
                "source_cv_id": cv_id,
                "review_token": review.get_json()["review_token"],
            },
        )

        assert applied.status_code == 409
        assert applied.get_json()["code"] == "job_description_changed"
        assert len(_cv_docs(fake_supabase)) == 1
        assert _letter_docs(fake_supabase) == []
        assert fake_supabase.tables.get("application_packets", []) == []

    def test_apply_review_rejects_token_prepared_for_different_job_before_documents(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """A review token is bound to the job it was prepared for."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, source_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=5)
        first_job = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        second_job = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        review = client.post(f"/api/jobs/{first_job}/review", json={"source_cv_id": cv_id})
        assert review.status_code == 200, review.get_data(as_text=True)
        applied = client.post(
            f"/api/jobs/{second_job}/apply-review",
            json={
                "source_cv_id": cv_id,
                "review_token": review.get_json()["review_token"],
            },
        )

        assert applied.status_code == 409, applied.get_data(as_text=True)
        assert applied.get_json()["code"] == "review_job_mismatch"
        assert len(_cv_docs(fake_supabase)) == 1
        assert _cv_docs(fake_supabase)[0]["yaml_blob"] == source_blob
        assert _letter_docs(fake_supabase) == []
        assert fake_supabase.tables.get("application_packets", []) == []

    def test_review_is_idempotent_when_job_packet_exists(self, server_module, client, fake_supabase, monkeypatch):
        """Retrying review after packet completion returns the existing packet."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        _review_body, first_body = _review_and_apply_packet(client, jid, cv_id)
        second = client.post(f"/api/jobs/{jid}/review", json={"source_cv_id": cv_id})

        assert second.status_code == 200, second.get_data(as_text=True)
        assert second.get_json()["duplicate"] is True
        assert second.get_json()["tailored_cv_id"] == first_body["tailored_cv_id"]
        assert second.get_json()["cover_letter_id"] == first_body["cover_letter_id"]
        assert second.get_json()["packet_id"] == first_body["packet_id"]
        assert len(_cv_docs(fake_supabase)) == 2
        assert len(_letter_docs(fake_supabase)) == 1
        assert len(fake_supabase.tables["application_packets"]) == 1
        prof = next(p for p in fake_supabase.tables["profiles"] if p["id"] == FAKE_OWNER)
        assert prof["managed_credits_remaining"] == 1

    def test_review_applied_partial_row_with_linked_docs_does_not_short_circuit_review(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """A standalone Tailor+Cover Letter partial row is not a completed packet duplicate."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=5)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        tailored = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT, "job_id": jid})
        assert tailored.status_code == 201, tailored.get_data(as_text=True)
        tailored_id = tailored.get_json()["tailored_cv_id"]
        partial_id = tailored.get_json()["packet_lineage"]["packet_id"]
        partial_score = fake_supabase.tables["application_packets"][0]["score_snapshot"]
        assert partial_score["score"]

        letter = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": tailored_id, "jd_text": JD_TEXT, "job_id": jid},
        )
        assert letter.status_code == 201, letter.get_data(as_text=True)
        row = fake_supabase.tables["application_packets"][0]
        assert row["id"] == partial_id
        assert row["status"] == "review_applied"
        assert row["tailored_cv_id"] == tailored_id
        assert row["cover_letter_id"] == letter.get_json()["id"]
        assert row["cover_letter_review_status"] == "pending"
        assert row["score_snapshot"] == partial_score

        review = client.post(f"/api/jobs/{jid}/review", json={"source_cv_id": cv_id})

        assert review.status_code == 200, review.get_data(as_text=True)
        body = review.get_json()
        assert body.get("duplicate") is not True
        assert body["review_token"]
        assert body["source_cv_id"] == cv_id
        assert len(fake_supabase.tables["application_packets"]) == 1

    def test_archive_keeps_packet_visible_as_archived(self, server_module, client, fake_supabase, monkeypatch):
        """Archiving a packet changes its state without hiding its document lineage."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        _review_and_apply_packet(client, jid, cv_id)
        packet_id = fake_supabase.tables["application_packets"][0]["id"]

        archived = client.post(f"/api/jobs/{jid}/packets/{packet_id}/archive", json={})
        detail = client.get(f"/api/jobs/{jid}").get_json()
        packet = next(p for p in detail["packets"] if p["id"] == packet_id)

        assert archived.status_code == 200, archived.get_data(as_text=True)
        assert packet["status"] == "archived"
        assert packet["archived_at"]
        assert packet["document_links"]["source_cv"] == f"/app?id={cv_id}"
        assert len(detail["cvs"]) == 1
        assert len(detail["cover_letters"]) == 1

    def test_job_detail_derives_stale_packet_status_from_live_hashes(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Job detail cannot show packet_created after source CV or JD drift."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        _review_and_apply_packet(client, jid, cv_id)
        job_row = next(j for j in fake_supabase.tables["saved_jobs"] if j["id"] == jid)
        job_row["jd_text"] = "Title: Different role\nCompany: Acme GmbH\nDifferent requirements."
        source_row = next(d for d in fake_supabase.tables["documents"] if d["id"] == cv_id)
        source_row["yaml_blob"] = source_row["yaml_blob"] + "\nsummary: changed after packet\n"
        source_row["updated_at"] = "2026-07-02T13:00:00+00:00"

        detail = client.get(f"/api/jobs/{jid}").get_json()
        packet = detail["packets"][0]

        assert packet["status"] == "stale"
        assert packet["stored_status"] == "packet_created"
        assert set(packet["stale_reasons"]) == {"source_cv_stale", "job_description_stale"}

    @pytest.mark.parametrize(
        ("doc_type", "body_field", "expected_reason"),
        [
            ("cv", "tailored_cv_id", "tailored_cv_stale"),
            ("cover_letter", "cover_letter_id", "cover_letter_stale"),
        ],
    )
    def test_job_detail_derives_stale_packet_status_from_tailored_and_letter_hashes(
        self, server_module, client, fake_supabase, monkeypatch, doc_type, body_field, expected_reason
    ):
        """Job detail cannot disagree with apply readiness on tailored/letter drift."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        _review_and_apply_packet(client, jid, cv_id)
        packet_row = fake_supabase.tables["application_packets"][0]
        doc_id = packet_row[body_field]
        doc = next(d for d in fake_supabase.tables["documents"] if d["id"] == doc_id and d["type"] == doc_type)
        doc["yaml_blob"] = doc["yaml_blob"] + "\nchanged_after_packet: true\n"
        doc["updated_at"] = 999

        packet = client.get(f"/api/jobs/{jid}").get_json()["packets"][0]

        assert packet["status"] == "stale"
        assert packet["stored_status"] == "packet_created"
        assert packet["stale_reasons"] == [expected_reason]

    def test_document_catalog_cvs_and_cover_letter_detail_expose_packet_lineage(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Non-Jobs surfaces expose the same safe packet lineage as job detail."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        review_body, applied = _review_and_apply_packet(client, jid, cv_id)
        packet = client.get(f"/api/jobs/{jid}").get_json()["packets"][0]

        docs = client.get("/api/documents").get_json()["documents"]
        indexed = {row["id"]: row for row in docs}
        cvs = client.get("/api/cvs").get_json()["cvs"]
        cv_indexed = {row["id"]: row for row in cvs}
        letter = client.get(f"/api/cover-letter?id={applied['cover_letter_id']}").get_json()

        expected_fields = {
            "packet_id": packet["id"],
            "job_id": jid,
            "source_cv_id": cv_id,
            "tailored_cv_id": applied["tailored_cv_id"],
            "cover_letter_id": applied["cover_letter_id"],
            "source_cv_hash": packet["source_cv_hash"],
            "job_content_hash": packet["job_content_hash"],
            "tailored_cv_hash": packet["tailored_cv_hash"],
            "cover_letter_hash": packet["cover_letter_hash"],
            "score_system_id": "semantic_fit",
            "prompt_version": "jobs_packet.v1",
            "packet_version": 1,
            "review_id": review_body["review_id"],
            "cover_letter_review_status": "confirmed",
            "cover_letter_review_id": review_body["review_id"],
            "status": "packet_created",
            "document_role": "tailored_cv",
        }
        tailored_lineage = indexed[applied["tailored_cv_id"]]["packet_lineage"]
        for key, value in expected_fields.items():
            assert tailored_lineage[key] == value
        assert "yaml_blob" not in tailored_lineage
        assert "review_token" not in tailored_lineage
        assert cv_indexed[applied["tailored_cv_id"]]["packet_lineage"]["packet_id"] == packet["id"]

        letter_lineage = indexed[applied["cover_letter_id"]]["packet_lineage"]
        assert letter_lineage["document_role"] == "cover_letter"
        assert letter_lineage["packet_id"] == packet["id"]
        assert letter["packet_lineage"]["packet_id"] == packet["id"]
        assert letter["packet_lineage"]["cover_letter_review_confirmed_at"]

        save = client.post(
            "/api/cover-letter",
            json={"id": applied["cover_letter_id"], "body": letter["body"] + "\nEdited after packet."},
        )
        assert save.status_code == 200, save.get_data(as_text=True)
        assert save.get_json()["packet_lineage"]["status"] == "stale"
        assert save.get_json()["packet_lineage"]["stale_reasons"] == ["cover_letter_stale"]

    def test_job_detail_packet_score_snapshot_is_sanitized(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        _review_and_apply_packet(client, jid, cv_id)
        fake_supabase.tables["application_packets"][0]["score_snapshot"] = {
            "score": 91,
            "matched": [{"term": "Spark"}],
            "raw_prompt": "RAW-PROMPT-MARKER",
            "notes": "review token REVIEW-TOKEN-MARKER",
            "authorization": "Bearer packet-secret",
        }

        packet = client.get(f"/api/jobs/{jid}").get_json()["packets"][0]
        raw = json.dumps(packet)

        assert packet["score_snapshot"]["score"] == 91
        assert packet["score_snapshot"]["matched"] == [{"term": "Spark"}]
        for forbidden in ("RAW-PROMPT-MARKER", "REVIEW-TOKEN-MARKER", "Bearer packet-secret", "authorization"):
            assert forbidden not in raw

    def test_source_metadata_nested_links_strip_credentials_query_and_fragment(self, server_module):
        import jobs_service

        metadata = jobs_service._safe_source_metadata({
            "source_links": [{
                "label": "Apply",
                "url": (
                    "https://" + "user" + ":" + "pass"
                    + "@jobs.example.com/apply?token=secret#frag"
                ),
                "href": "https://jobs.example.com/role?li_at=secret",
            }],
        })
        link = metadata["source_links"][0]

        assert link["url"] == "https://jobs.example.com/apply"
        assert link["href"] == "https://jobs.example.com/role"
        assert "token" not in json.dumps(metadata).lower()
        assert "li_at" not in json.dumps(metadata).lower()

    def test_source_url_query_parameters_are_marked_redacted(self, server_module):
        import jobs_service

        assert jobs_service._safe_source_url("https://jobs.example.com/apply?token=secret") == "https://jobs.example.com/apply"
        assert jobs_service._source_url_redacted("https://jobs.example.com/apply?token=secret") is True
        assert jobs_service._safe_source_url("https://jobs.example.com/apply/token/secret") == "https://jobs.example.com"
        assert jobs_service._source_url_redacted("https://jobs.example.com/apply/token/secret") is True

    def test_applied_job_status_propagates_to_document_packet_lineage(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        _, applied = _review_and_apply_packet(client, jid, cv_id)

        status = client.post(f"/api/jobs/{jid}/status", json={"status": "applied"})
        detail = client.get(f"/api/jobs/{jid}").get_json()
        docs = {row["id"]: row for row in client.get("/api/documents").get_json()["documents"]}

        assert status.status_code == 200
        assert detail["packets"][0]["status"] == "applied_external"
        assert docs[applied["tailored_cv_id"]]["packet_lineage"]["status"] == "applied_external"
        assert docs[applied["cover_letter_id"]]["packet_lineage"]["status"] == "applied_external"

    def test_regenerate_prepares_review_and_apply_creates_next_packet_version(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Regenerate starts a fresh review and only creates v2 after apply confirmation."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=5)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        _, first = _review_and_apply_packet(client, jid, cv_id)
        packet_id = first["packet_id"]

        regenerate = client.post(f"/api/jobs/{jid}/packets/{packet_id}/regenerate", json={})
        regen_body = regenerate.get_json()

        assert regenerate.status_code == 201, regenerate.get_data(as_text=True)
        assert regen_body["review_token"]
        assert regen_body["packet_mode"] == "regenerate"
        assert regen_body["replaces_packet_id"] == packet_id
        assert regen_body["source_cv_id"] == cv_id
        assert len(fake_supabase.tables["application_packets"]) == 1

        second = client.post(
            f"/api/jobs/{jid}/apply-review",
            json={
                "source_cv_id": cv_id,
                "review_token": regen_body["review_token"],
            },
        )
        assert second.status_code == 201, second.get_data(as_text=True)
        pending_detail = client.get(f"/api/jobs/{jid}").get_json()
        pending_packets = {p["id"]: p for p in pending_detail["packets"]}
        assert pending_packets[packet_id]["status"] == "packet_created"
        assert pending_packets[second.get_json()["packet_id"]]["status"] == "review_applied"

        confirmed = client.post(
            f"/api/jobs/{jid}/packets/{second.get_json()['packet_id']}/confirm-cover-letter-review",
            json={"cover_letter_hash": second.get_json()["cover_letter_hash"]},
        )
        assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
        detail = client.get(f"/api/jobs/{jid}").get_json()
        packets = {p["id"]: p for p in detail["packets"]}
        new_packet = packets[second.get_json()["packet_id"]]
        old_packet = packets[packet_id]

        assert new_packet["packet_version"] == 2
        assert new_packet["status"] == "packet_created"
        assert old_packet["status"] == "archived"
        assert old_packet["archived_at"]
        source_cv = next(row for row in client.get("/api/cvs").get_json()["cvs"] if row["id"] == cv_id)
        assert source_cv["packet_lineage"]["packet_id"] == new_packet["id"]
        assert source_cv["packet_lineage"]["packet_version"] == 2
        assert source_cv["packet_lineage"]["document_role"] == "source_cv"
        assert source_cv["packet_lineage"]["status"] == "packet_created"
        assert len(detail["cvs"]) == 2
        assert len(detail["cover_letters"]) == 2

    def test_packet_replacement_insert_failure_restores_previous_packet(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """A failed replacement cannot strand the previous packet as archived."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, source_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=5)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        _, first = _review_and_apply_packet(client, jid, cv_id)
        packet_id = first["packet_id"]
        regenerate = client.post(f"/api/jobs/{jid}/packets/{packet_id}/regenerate", json={})
        assert regenerate.status_code == 201, regenerate.get_data(as_text=True)

        import jobs_service

        def _fail_insert(_sb, _payload):
            raise jobs_service.JobError(
                500,
                {"error": "Failed to save application packet.", "code": "packet_insert_failed"},
            )

        monkeypatch.setattr(jobs_service, "_insert_packet_payload", _fail_insert)
        failed = client.post(
            f"/api/jobs/{jid}/apply-review",
            json={
                "source_cv_id": cv_id,
                "review_token": regenerate.get_json()["review_token"],
            },
        )
        packet = fake_supabase.tables["application_packets"][0]

        assert failed.status_code == 500
        assert failed.get_json()["code"] == "packet_insert_failed"
        assert len(fake_supabase.tables["application_packets"]) == 1
        assert packet["id"] == packet_id
        assert packet["status"] == "packet_created"
        assert packet.get("archived_at") is None
        cvs = _cv_docs(fake_supabase)
        assert len(cvs) == 2
        assert next(d for d in cvs if d["id"] == cv_id)["yaml_blob"] == source_blob
        assert len(_letter_docs(fake_supabase)) == 1
        retry_review = client.post(f"/api/jobs/{jid}/review", json={"source_cv_id": cv_id})
        assert retry_review.status_code == 200, retry_review.get_data(as_text=True)
        assert retry_review.get_json()["duplicate"] is True
        assert retry_review.get_json()["packet_id"] == packet_id

    def test_letter_guard_trip_leaves_no_orphan(self, server_module, client, fake_supabase, monkeypatch):
        """The CV tailors fine, but the cover-letter step fabricates an employer
        ('Globex Industries') → 422. The chain must NOT leave the job with a
        tailored CV and no letter: the CV is rolled back, so the job ends with
        NEITHER document and the source CV is untouched."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))   # CV: clean, saves
        _mock_letter_llm(monkeypatch, FABRICATED_LETTER)          # letter: blocked
        cv_id, source_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        review = client.post(f"/api/jobs/{jid}/review", json={"source_cv_id": cv_id})
        assert review.status_code == 200, review.get_data(as_text=True)
        res = client.post(
            f"/api/jobs/{jid}/apply-review",
            json={
                "source_cv_id": cv_id,
                "review_token": review.get_json()["review_token"],
            },
        )
        # The letter's anti-fabrication guard surfaces as a 422 with the offender.
        assert res.status_code == 422, res.get_data(as_text=True)
        body = res.get_json()
        assert body["error"] == "unverified_claims"
        assert "Globex" in body["items"]

        # NO orphan: the tailored CV was rolled back; only the source remains.
        cvs = _cv_docs(fake_supabase)
        assert len(cvs) == 1, "the half-created tailored CV must be rolled back"
        assert cvs[0]["id"] == cv_id
        assert cvs[0]["yaml_blob"] == source_blob  # source untouched
        assert len(_letter_docs(fake_supabase)) == 0

        # The job's detail shows neither a CV nor a letter linked.
        detail = client.get(f"/api/jobs/{jid}").get_json()
        assert detail["cvs"] == []
        assert detail["cover_letters"] == []
        assert fake_supabase.tables.get("application_packets", []) == []

    def test_apply_review_retries_same_token_after_cover_letter_rollback(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """A cover-letter guard trip releases the review claim for one retry."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, FABRICATED_LETTER)
        cv_id, source_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=4)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        review = client.post(f"/api/jobs/{jid}/review", json={"source_cv_id": cv_id})
        assert review.status_code == 200, review.get_data(as_text=True)
        token = review.get_json()["review_token"]
        payload = {
            "source_cv_id": cv_id,
            "review_token": token,
        }
        first = client.post(f"/api/jobs/{jid}/apply-review", json=payload)
        assert first.status_code == 422, first.get_data(as_text=True)
        assert len(_cv_docs(fake_supabase)) == 1
        assert _letter_docs(fake_supabase) == []
        assert fake_supabase.tables.get("application_packets", []) == []

        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        second = client.post(f"/api/jobs/{jid}/apply-review", json=payload)

        assert second.status_code == 201, second.get_data(as_text=True)
        confirmed = client.post(
            f"/api/jobs/{jid}/packets/{second.get_json()['packet_id']}/confirm-cover-letter-review",
            json={"cover_letter_hash": second.get_json()["cover_letter_hash"]},
        )
        assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
        assert len(_cv_docs(fake_supabase)) == 2
        assert next(d for d in _cv_docs(fake_supabase) if d["id"] == cv_id)["yaml_blob"] == source_blob
        assert len(_letter_docs(fake_supabase)) == 1
        assert len(fake_supabase.tables["application_packets"]) == 1
        assert fake_supabase.tables["application_packets"][0]["status"] == "packet_created"

    def test_packet_insert_failure_rolls_back_cv_and_letter(self, server_module, client, fake_supabase, monkeypatch):
        """If packet persistence fails after document creation, no job-linked orphan documents remain."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, source_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        import jobs_service

        def _fail_packet_insert(**_kwargs):
            raise jobs_service.JobError(
                500,
                {"error": "Failed to save application packet.", "code": "packet_insert_failed"},
            )

        monkeypatch.setattr(jobs_service, "_ensure_pending_packet_row", _fail_packet_insert)
        review = client.post(f"/api/jobs/{jid}/review", json={"source_cv_id": cv_id})
        assert review.status_code == 200, review.get_data(as_text=True)

        res = client.post(
            f"/api/jobs/{jid}/apply-review",
            json={
                "source_cv_id": cv_id,
                "review_token": review.get_json()["review_token"],
            },
        )

        assert res.status_code == 500
        assert res.get_json()["code"] == "packet_insert_failed"
        cvs = _cv_docs(fake_supabase)
        assert len(cvs) == 1
        assert cvs[0]["id"] == cv_id
        assert cvs[0]["yaml_blob"] == source_blob
        assert _letter_docs(fake_supabase) == []
        assert fake_supabase.tables.get("application_packets", []) == []
        detail = client.get(f"/api/jobs/{jid}").get_json()
        assert detail["cvs"] == []
        assert detail["cover_letters"] == []

    def test_apply_review_retries_same_token_after_packet_insert_rollback(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """A packet persistence failure releases the review claim for one retry."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, source_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=4)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]

        import jobs_service

        original_ensure = jobs_service._ensure_pending_packet_row
        calls = {"count": 0}

        def _fail_once(**kwargs):
            if calls["count"] == 0:
                calls["count"] += 1
                raise jobs_service.JobError(
                    500,
                    {"error": "Failed to save application packet.", "code": "packet_insert_failed"},
                )
            return original_ensure(**kwargs)

        monkeypatch.setattr(jobs_service, "_ensure_pending_packet_row", _fail_once)
        review = client.post(f"/api/jobs/{jid}/review", json={"source_cv_id": cv_id})
        assert review.status_code == 200, review.get_data(as_text=True)
        payload = {
            "source_cv_id": cv_id,
            "review_token": review.get_json()["review_token"],
        }

        first = client.post(f"/api/jobs/{jid}/apply-review", json=payload)
        assert first.status_code == 500
        assert first.get_json()["code"] == "packet_insert_failed"
        assert len(_cv_docs(fake_supabase)) == 1
        assert next(d for d in _cv_docs(fake_supabase) if d["id"] == cv_id)["yaml_blob"] == source_blob
        assert _letter_docs(fake_supabase) == []
        assert fake_supabase.tables.get("application_packets", []) == []

        second = client.post(f"/api/jobs/{jid}/apply-review", json=payload)
        assert second.status_code == 201, second.get_data(as_text=True)
        confirmed = client.post(
            f"/api/jobs/{jid}/packets/{second.get_json()['packet_id']}/confirm-cover-letter-review",
            json={"cover_letter_hash": second.get_json()["cover_letter_hash"]},
        )
        assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
        assert len(_cv_docs(fake_supabase)) == 2
        assert len(_letter_docs(fake_supabase)) == 1
        assert len(fake_supabase.tables["application_packets"]) == 1
        assert fake_supabase.tables["application_packets"][0]["status"] == "packet_created"

    def test_generate_unknown_job_is_404(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id, _ = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post(f"/api/jobs/{_uuid.uuid4()}/generate", json={"source_cv_id": cv_id})
        assert res.status_code == 404

    def test_generate_bad_source_cv_is_400(self, server_module, client, fake_supabase):
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        res = client.post(f"/api/jobs/{jid}/generate", json={"source_cv_id": "not-a-uuid"})
        assert res.status_code == 400

    def test_generate_unknown_source_cv_is_404_no_orphan(self, server_module, client, fake_supabase, monkeypatch):
        """A valid-shaped but non-existent source CV → the tailor step 404s; no
        document is created (the chain never reaches the letter step)."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response([]))
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        jid = _make_job(client, jd_text=JD_TEXT).get_json()["id"]
        res = client.post(f"/api/jobs/{jid}/generate", json={"source_cv_id": str(_uuid.uuid4())})
        assert res.status_code == 404
        assert _cv_docs(fake_supabase) == []
        assert _letter_docs(fake_supabase) == []


# ===========================================================================
#  5. The /jobs page is served and auth-exempt (loads pre-session).
# ===========================================================================
def _write_local_jobs_db(
    path: Path,
    *,
    last_seen: str = "2026-06-16",
    description: str = "Build Spark and Airflow pipelines.\nContact recruiter@example.com or +49 30 12345678.",
    job_url: str = "https://www.linkedin.com/jobs/view/1",
) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            create table companies (
              id integer primary key autoincrement,
              name text not null,
              name_normalized text not null unique,
              first_seen text not null,
              last_updated text not null
            );
            create table jobs (
              fingerprint text primary key,
              id text,
              site text not null,
              job_url text not null,
              title text not null,
              company_id integer,
              location text,
              date_posted text,
              job_level text,
              is_remote integer,
              work_from_home_type text,
              description text,
              emails text,
              first_seen text not null,
              last_seen text not null,
              normalized_role text
            );
            create table scrape_runs (
              id integer primary key,
              started_at text not null,
              status text not null
            );
            create table job_salaries (
              id integer primary key autoincrement,
              fingerprint text not null,
              interval text,
              min_amount real,
              max_amount real,
              currency text,
              observed_at text not null
            );
            """
        )
        conn.execute(
            "insert into companies (id, name, name_normalized, first_seen, last_updated) values (1, 'Acme GmbH', 'acme gmbh', '2026-06-01', '2026-06-16')"
        )
        conn.execute("insert into scrape_runs (id, started_at, status) values (1, '2026-06-16T10:00:00+00:00', 'completed')")
        conn.execute(
            """
            insert into jobs (
              fingerprint, id, site, job_url, title, company_id, location,
              date_posted, job_level, is_remote, work_from_home_type,
              description, emails, first_seen, last_seen, normalized_role
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fp-1",
                "external-1",
                "linkedin",
                job_url,
                "Senior Data Engineer",
                1,
                "Berlin, Germany",
                "2026-06-15",
                "senior",
                1,
                "remote",
                description,
                "recruiter@example.com",
                "2026-06-15",
                last_seen,
                "data_engineer",
            ),
        )
        conn.execute(
            "insert into job_salaries (fingerprint, interval, min_amount, max_amount, currency, observed_at) values ('fp-1', 'yearly', 90000, 110000, 'EUR', '2026-06-16')"
        )
        conn.commit()
    finally:
        conn.close()


def _insert_local_job(
    path: Path,
    *,
    fingerprint: str,
    title: str,
    site: str = "linkedin",
    location: str = "Berlin, Germany",
    last_seen: str = "2026-06-16",
    normalized_role: str = "data_engineer",
    description: str = "Build pipelines.",
) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            insert into jobs (
              fingerprint, id, site, job_url, title, company_id, location,
              date_posted, job_level, is_remote, work_from_home_type,
              description, emails, first_seen, last_seen, normalized_role
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                f"external-{fingerprint}",
                site,
                f"https://example.com/jobs/{fingerprint}",
                title,
                1,
                location,
                "2026-06-15",
                "senior",
                1,
                "remote",
                description,
                "",
                "2026-06-15",
                last_seen,
                normalized_role,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestJobsPage:
    def test_jobs_page_served(self, server_module, client):
        res = client.get("/jobs")
        assert res.status_code == 200
        # Stage 6 (plan 002) renamed the page to the "Jobs catalog" grid.
        body = res.get_data()
        assert b"Jobs catalog" in body
        assert b"Application packet" in body

    def test_jobs_frontend_supports_packet_deep_links_and_metadata(self):
        js = (REPO / "tools" / "editor" / "static" / "jobs.js").read_text()
        assert 'new URLSearchParams(window.location.search || "").get("id")' in js
        assert 'row.dataset.docId = doc.id || ""' in js
        assert 'row.dataset.docType = kind' in js
        assert 'row.dataset.jobId = state.jobId' in js
        assert 't("lib.storage.saved", "Saved in Documents")' in js
        assert 't("lib.storage.linked_job", "Linked to this job")' in js
        assert '"/api/jobs/" + encodeURIComponent(jid) + "/review"' in js
        assert '"/api/jobs/" + encodeURIComponent(jid) + "/apply-review"' in js
        html = (REPO / "tools" / "editor" / "static" / "jobs.html").read_text()
        assert "LinkedIn" in html and "approved adapter" in html
        assert "jb-review-panel" in html
        assert "jb-local-search" in html
        assert "Search local DB" in html
        assert "JOB_SOURCE_DEFAULTS_KEY" in js
        assert "/api/jobs/local-search" in js
        assert "applyJobSourceDefaults()" in js
        assert "blind-fetch LinkedIn" in js
        for selector in (
            "jb-search-mode",
            "jb-filter-family",
            "jb-filter-city",
            "jb-filter-min-years",
        ):
            assert selector in html
        for token in (
            "searchMode",
            "job.job_family",
            "job.source_city",
            "experienceFit",
            "minYears",
            't("jobs.years"',
        ):
            assert token in js

    def test_linkedin_adapter_endpoint_fails_closed(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.delenv("LINKEDIN_APPROVED_ADAPTER_ENABLED", raising=False)
        monkeypatch.delenv("LINKEDIN_PARTNER_CONFIG_ID", raising=False)

        res = client.post(
            "/api/jobs/import-runs",
            json={"provider": "linkedin", "query": "data engineer"},
        )
        assert res.status_code == 400
        assert res.get_json()["code"] == "linkedin_adapter_disabled"

    def test_enabled_linkedin_adapter_is_still_not_implemented_without_fetch(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import jobs_service as js

        def _boom(*_a, **_k):
            raise AssertionError("Approved adapter placeholder must not fetch")

        monkeypatch.setenv("LINKEDIN_APPROVED_ADAPTER_ENABLED", "true")
        monkeypatch.setenv("LINKEDIN_PARTNER_CONFIG_ID", "partner-contract-1")
        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _boom)
        res = client.post("/api/jobs/import-runs", json={"provider": "linkedin", "query": "data engineer"})
        assert res.status_code == 501
        assert res.get_json()["code"] == "adapter_not_implemented"

    def test_linkedin_policy_doc_forbids_cookies_and_stealth_scraping(self):
        policy = (REPO / "docs" / "job-ingestion-policy.md").read_text()
        assert "No blind LinkedIn scraping" in policy
        assert "No stored LinkedIn cookies" in policy
        assert "stealth automation" in policy
        env = (REPO / ".env.example").read_text()
        assert "LINKEDIN_APPROVED_ADAPTER_ENABLED" in env
        assert "LINKEDIN_PARTNER_CONFIG_ID" in env

    def test_job_source_status_reports_local_db_without_secrets(self, server_module, client, fake_supabase, tmp_path, monkeypatch):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path)
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))
        monkeypatch.delenv("LINKEDIN_APPROVED_ADAPTER_ENABLED", raising=False)
        monkeypatch.delenv("LINKEDIN_PARTNER_CONFIG_ID", raising=False)

        res = client.get("/api/jobs/sources/status")
        assert res.status_code == 200
        body = res.get_json()
        linkedin = body["sources"]["linkedin"]
        assert linkedin["local_db_configured"] is True
        assert linkedin["local_db_available"] is True
        assert linkedin["row_count"] == 1
        assert linkedin["latest_seen_at"] == "2026-06-16"
        assert linkedin["stale"] is True
        assert "no_cookies" in linkedin["warnings"]
        assert body["policy"]["no_blind_linkedin_fetch"] is True
        assert "session" not in json.dumps(body).lower()
        assert "li_at" not in json.dumps(body).lower()

    def test_local_jobs_search_returns_sanitized_stale_projection(self, server_module, client, fake_supabase, tmp_path, monkeypatch):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path)
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        res = client.post(
            "/api/jobs/local-search",
            json={
                "query": "data engineer",
                "filters": {"source": "linkedin", "region": "Berlin", "remote_mode": "remote", "seniority": "senior"},
                "limit": 5,
            },
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["count"] == 1
        row = body["results"][0]
        assert row["source_provider"] == "linkedin"
        assert row["source_type"] == "csv_import"
        assert row["verification_status"] == "stale"
        assert row["source_external_id"] == "fp-1"
        assert row["source_metadata"]["origin"] == "local_jobs_db_read_only"
        assert row["source_metadata"]["site"] == "linkedin"
        assert "recruiter@example.com" not in row["jd_text"]
        assert "+49 30" not in row["jd_text"]
        assert "emails" not in json.dumps(row).lower()
        assert row["search_criteria"]["schema"] == "onepager.job_search_criteria.v1"
        assert row["job_family"] == "Data Engineer"
        assert row["source_city"] == "Berlin"

    def test_local_jobs_strict_search_requires_title_family_and_location(
        self, server_module, client, fake_supabase, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path)
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        strict = client.post(
            "/api/jobs/local-search",
            json={
                "query": "Data Engineer",
                "filters": {
                    "source": "linkedin",
                    "search_mode": "strict",
                    "family": "Data / ML",
                    "country": "DE",
                    "city": "Berlin",
                    "verification": "stale",
                    "min_years": "5",
                },
                "limit": 5,
            },
        )
        assert strict.status_code == 200, strict.get_data(as_text=True)
        body = strict.get_json()
        assert body["count"] == 1
        row = body["results"][0]
        assert row["search_criteria"]["search_mode"] == "strict"
        assert row["search_criteria"]["countries"] == ["DE"]
        assert row["search_criteria"]["cities"] == ["Berlin"]
        assert row["broader_match"] is False
        assert "normalized title match" in row["match_reasons"]
        assert "city/location match" in row["match_reasons"]

        company_only = client.post(
            "/api/jobs/local-search",
            json={"query": "Acme", "filters": {"source": "linkedin", "search_mode": "strict"}, "limit": 5},
        )
        assert company_only.status_code == 200
        assert company_only.get_json()["count"] == 0

    def test_local_jobs_strict_search_rejects_substring_false_positives(
        self, server_module, client, fake_supabase, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "update jobs set title = 'Engineering Manager', normalized_role = 'engineering_manager', location = 'New York, US' where fingerprint = 'fp-1'"
            )
            conn.commit()
        finally:
            conn.close()
        _insert_local_job(
            db_path,
            fingerprint="fp-database",
            title="Database Engineer",
            normalized_role="database_engineer",
            description="Own database administration and storage operations.",
        )
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        title = client.post(
            "/api/jobs/local-search",
            json={"query": "Engineer", "filters": {"source": "linkedin", "search_mode": "strict"}, "limit": 10},
        )
        assert title.status_code == 200, title.get_data(as_text=True)
        assert "fp-1" not in [row["id"] for row in title.get_json()["results"]]

        family = client.post(
            "/api/jobs/local-search",
            json={
                "query": "Data",
                "filters": {"source": "linkedin", "search_mode": "strict", "family": "Data / ML"},
                "limit": 10,
            },
        )
        assert family.status_code == 200, family.get_data(as_text=True)
        assert "fp-database" not in [row["id"] for row in family.get_json()["results"]]

        city = client.post(
            "/api/jobs/local-search",
            json={
                "query": "Engineering Manager",
                "filters": {"source": "linkedin", "search_mode": "strict", "city": "York"},
                "limit": 10,
            },
        )
        assert city.status_code == 200, city.get_data(as_text=True)
        assert "fp-1" not in [row["id"] for row in city.get_json()["results"]]

    def test_search_criteria_sanitizer_preserves_legitimate_key_words(self):
        import jobs_service

        criteria = jobs_service._safe_search_criteria({
            "title": "Key Account Manager",
            "titles": ["Key Account Manager", "Secret Clearance Analyst", "Session Replay Product Manager", "Cookie Consent Engineer"],
            "countries": ["Turkey"],
            "cities": ["Key West"],
            "source": "linkedin",
            "search_mode": "strict",
        })

        assert criteria["title"] == "Key Account Manager"
        assert criteria["titles"] == ["Key Account Manager", "Secret Clearance Analyst", "Session Replay Product Manager", "Cookie Consent Engineer"]
        assert criteria["countries"] == ["Turkey"]
        assert criteria["cities"] == ["Key West"]

        criteria = jobs_service._safe_search_criteria({
            "ranking": {"title": 0.5, "key": 0.7, "secret_key": 0.8, "token": 0.9}
        })
        assert criteria["ranking"] == {"title": 0.5}

    def test_local_jobs_flexible_search_explains_broader_description_matches(
        self, server_module, client, fake_supabase, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path)
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        strict = client.post(
            "/api/jobs/local-search",
            json={"query": "Spark", "filters": {"source": "linkedin", "search_mode": "strict"}, "limit": 5},
        )
        assert strict.status_code == 200
        assert strict.get_json()["count"] == 0

        flexible = client.post(
            "/api/jobs/local-search",
            json={"query": "Spark", "filters": {"source": "linkedin", "search_mode": "flexible"}, "limit": 5},
        )
        assert flexible.status_code == 200, flexible.get_data(as_text=True)
        row = flexible.get_json()["results"][0]
        assert row["broader_match"] is True
        assert "broader description match" in row["match_reasons"]

        company = client.post(
            "/api/jobs/local-search",
            json={"query": "Acme", "filters": {"source": "linkedin", "search_mode": "flexible"}, "limit": 5},
        )
        assert company.status_code == 200, company.get_data(as_text=True)
        company_row = company.get_json()["results"][0]
        assert company_row["broader_match"] is True
        assert "broader company match" in company_row["match_reasons"]

    def test_local_jobs_strict_search_keeps_normalized_role_candidates_after_sql_window(
        self, server_module, client, fake_supabase, tmp_path, monkeypatch
    ):
        import jobs_service as js

        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path, last_seen="2026-06-01")
        monkeypatch.setattr(js, "LOCAL_JOBS_SEARCH_CANDIDATE_LIMIT", 25)
        for idx in range(60):
            _insert_local_job(
                db_path,
                fingerprint=f"fp-noise-{idx}",
                title=f"Operations Builder {idx}",
                location="Berlin, Germany",
                last_seen="2026-06-21",
                normalized_role="operations",
                description="Coordinate support operations.",
            )
        _insert_local_job(
            db_path,
            fingerprint="fp-2",
            title="Platform Builder",
            location="Berlin, Germany",
            last_seen="2026-06-20",
            normalized_role="data_engineer",
            description="Own reliable warehouse orchestration.",
        )
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        res = client.post(
            "/api/jobs/local-search",
            json={
                "query": "Data Engineer",
                "filters": {"source": "linkedin", "search_mode": "strict", "city": "Berlin", "country": "DE"},
                "limit": 1,
            },
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["count"] == 1
        assert body["results"][0]["id"] == "fp-2"
        assert "normalized title match" in body["results"][0]["match_reasons"]

    def test_local_jobs_strict_family_rejects_unrelated_data_roles(
        self, server_module, client, fake_supabase, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path)
        _insert_local_job(
            db_path,
            fingerprint="fp-entry",
            title="Data Entry Clerk",
            normalized_role="data_entry",
            description="Enter records and maintain back-office tables.",
        )
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        res = client.post(
            "/api/jobs/local-search",
            json={
                "query": "Data",
                "filters": {"source": "linkedin", "search_mode": "strict", "family": "Data / ML"},
                "limit": 10,
            },
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        ids = [row["id"] for row in res.get_json()["results"]]
        assert "fp-1" in ids
        assert "fp-entry" not in ids

    def test_local_jobs_country_filter_matches_short_code_locations(
        self, server_module, client, fake_supabase, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("update jobs set title = 'Operations Builder', normalized_role = 'operations' where fingerprint = 'fp-1'")
            conn.commit()
        finally:
            conn.close()
        _insert_local_job(db_path, fingerprint="fp-de", title="Data Engineer", location="Berlin, DE")
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        res = client.post(
            "/api/jobs/local-search",
            json={
                "query": "Data Engineer",
                "filters": {"source": "linkedin", "search_mode": "strict", "country": "DE", "city": "Berlin"},
                "limit": 5,
            },
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["count"] == 1
        assert body["results"][0]["id"] == "fp-de"

    def test_local_jobs_verification_filter_uses_row_staleness(
        self, server_module, client, fake_supabase, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path, last_seen="2000-01-01")
        _insert_local_job(db_path, fingerprint="fp-fresh", title="Operations Builder", last_seen=date.today().isoformat(), normalized_role="operations")
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        res = client.post(
            "/api/jobs/local-search",
            json={
                "query": "Data Engineer",
                "filters": {"source": "linkedin", "search_mode": "strict", "verification": "stale"},
                "limit": 5,
            },
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["count"] == 1
        assert body["results"][0]["id"] == "fp-1"
        assert body["results"][0]["verification_status"] == "stale"

    def test_local_jobs_ranking_weights_order_local_results(
        self, server_module, client, fake_supabase, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path, last_seen="2026-06-01")
        _insert_local_job(
            db_path,
            fingerprint="fp-fresh-title",
            title="Data Engineer",
            location="Berlin, Germany",
            last_seen=date.today().isoformat(),
            normalized_role="data_engineer",
            description="5 years required. Build reliable data pipelines.",
        )
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        res = client.post(
            "/api/jobs/local-search",
            json={
                "query": "Data Engineer",
                "filters": {
                    "source": "linkedin",
                    "search_mode": "strict",
                    "city": "Berlin",
                    "country": "DE",
                    "min_years": "5",
                    "ranking": {"title": 0.35, "location": 0.25, "freshness": 0.2, "experience": 0.2},
                },
                "limit": 5,
            },
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        ids = [row["id"] for row in res.get_json()["results"]]
        assert ids[0] == "fp-fresh-title"

    def test_local_jobs_source_other_matches_unknown_local_sites(
        self, server_module, client, fake_supabase, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path)
        _insert_local_job(db_path, fingerprint="fp-2", title="Data Engineer", site="greenhouse")
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        res = client.post(
            "/api/jobs/local-search",
            json={"query": "Data Engineer", "filters": {"source": "other", "search_mode": "strict"}, "limit": 5},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["count"] == 1
        assert body["results"][0]["source_provider"] == "other"

    def test_reviewed_shortlist_parser_preserves_fixture_leads_and_multi_link_card(self):
        import jobs_service as js

        artifact = REPO / "tests" / "fixtures" / "top-tier-jobs.html"
        leads = js.parse_reviewed_leads_html(artifact.read_text(encoding="utf-8"), source_artifact=str(artifact))

        assert len(leads) == 3
        assert sum(len(lead["source_metadata"]["source_links"]) for lead in leads) == 4
        assert leads[0]["source_metadata"]["rank_bucket"] == "Apply first"
        assert leads[0]["source_metadata"]["priority_rank"] == 1
        assert leads[0]["source_metadata"]["rank_badge"] == "Best"
        scientist = next(lead for lead in leads if lead["title"] == "AI Scientist")
        assert scientist["company"] == "Mistral AI"
        assert scientist["source_metadata"]["rank_bucket"] == "Frontier stretch"
        assert len(scientist["source_metadata"]["source_links"]) == 2
        assert scientist["source_metadata"]["verified_at"] == "2026-06-25"
        assert scientist["source_metadata"]["full_jd_status"] == "missing"
        assert scientist["verification_status"] == "needs_review"

    def test_reviewed_shortlist_import_preserves_metadata_and_blocks_generation(
        self, server_module, client, fake_supabase
    ):
        import jobs_service as js

        artifact = REPO / "tests" / "fixtures" / "top-tier-jobs.html"
        leads = js.parse_reviewed_leads_html(artifact.read_text(encoding="utf-8"), source_artifact=str(artifact))
        res = client.post(
            "/api/jobs/import-runs",
            json={
                "provider": "manual",
                "query": "curated shortlist",
                "location": "Switzerland, Warsaw, Berlin",
                "postings": leads[:1],
            },
        )
        assert res.status_code == 202, res.get_data(as_text=True)
        body = res.get_json()
        result = body["results"][0]
        assert result["source_metadata"]["priority_rank"] == 1
        assert result["source_metadata"]["rank_bucket"] == "Apply first"
        assert result["source_metadata"]["full_jd_status"] == "missing"

        saved = client.post(
            f"/api/jobs/import-runs/{body['id']}/save-selected",
            json={"result_ids": [result["id"]]},
        )
        assert saved.status_code == 200, saved.get_data(as_text=True)
        job = _jobs(fake_supabase)[0]
        assert job["source_metadata"]["fit_note"]
        assert job["source_metadata"]["source_artifact"] == "top-tier-jobs.html"
        assert job["source_metadata"]["full_jd_status"] == "missing"

        cv_id, _ = _seed_cv(fake_supabase)
        blocked = client.post(f"/api/jobs/{job['id']}/review", json={"source_cv_id": cv_id})
        assert blocked.status_code == 422
        assert blocked.get_json()["code"] == "full_jd_required"

        revalidated = client.post(
            f"/api/jobs/{job['id']}/source/revalidate",
            json={"full_jd_text": JD_TEXT},
        )
        assert revalidated.status_code == 200, revalidated.get_data(as_text=True)
        body = revalidated.get_json()
        assert body["source_metadata"]["full_jd_status"] == "reviewed"
        assert body["source_state"]["requires_full_jd_review"] is False
        assert body["verification_status"] == "user_provided"
        assert body["jd_text"] == JD_TEXT
        assert len(body["content_hash"]) == 64
        assert body["experience_fit"]["experience_requirements"]

    def test_local_jobs_search_honors_freshness_filter(self, server_module, client, fake_supabase, tmp_path, monkeypatch):
        fresh_db = tmp_path / "fresh-jobs.db"
        _write_local_jobs_db(fresh_db, last_seen=date.today().isoformat())
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(fresh_db))

        fresh = client.post(
            "/api/jobs/local-search",
            json={"query": "data engineer", "filters": {"source": "linkedin", "freshness": "fresh"}, "limit": 5},
        )
        assert fresh.status_code == 200, fresh.get_data(as_text=True)
        assert fresh.get_json()["count"] == 1

        stale_only = client.post(
            "/api/jobs/local-search",
            json={"query": "data engineer", "filters": {"source": "linkedin", "freshness": "stale"}, "limit": 5},
        )
        assert stale_only.status_code == 200
        assert stale_only.get_json()["count"] == 0

        stale_db = tmp_path / "stale-jobs.db"
        _write_local_jobs_db(stale_db, last_seen="2000-01-01")
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(stale_db))
        stale = client.post(
            "/api/jobs/local-search",
            json={"query": "data engineer", "filters": {"source": "linkedin", "freshness": "stale"}, "limit": 5},
        )
        assert stale.status_code == 200
        assert stale.get_json()["count"] == 1

    def test_local_jobs_search_never_fetches_linkedin_source_urls(self, server_module, client, fake_supabase, tmp_path, monkeypatch):
        import jobs_service as js

        def _boom(*_a, **_k):
            raise AssertionError("local jobs search must be read-only and never fetch source URLs")

        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path)
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))
        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _boom)

        res = client.post(
            "/api/jobs/local-search",
            json={"query": "data engineer", "filters": {"source": "linkedin"}, "limit": 5},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        assert res.get_json()["count"] == 1

    def test_local_jobs_search_redacts_token_shaped_description_and_url(self, server_module, client, fake_supabase, tmp_path, monkeypatch):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(
            db_path,
            description=(
                "Build Spark and Airflow pipelines.\n"
                "Internal token li_at=secret-cookie and session_token=abc123 should never leak.\n"
                "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9"
            ),
            job_url=(
                "https://" + "user" + ":" + "pass"
                + "@www.linkedin.com/jobs/view/1"
                + "?li_at=secret-cookie&session_token=abc123"
            ),
        )
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        res = client.post(
            "/api/jobs/local-search",
            json={"query": "data engineer", "filters": {"source": "linkedin"}, "limit": 5},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        row = body["results"][0]
        assert row["source_url"] == "https://www.linkedin.com/jobs/view/1"
        raw = json.dumps(body).lower()
        for forbidden in ("li_at", "secret-cookie", "session_token", "authorization", "bearer", "user:pass"):
            assert forbidden not in raw

    def test_local_jobs_search_sanitizes_secret_shaped_scalar_provenance(self, server_module, client, fake_supabase, tmp_path, monkeypatch):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                update jobs
                   set location = ?,
                       job_level = ?,
                       work_from_home_type = ?,
                       normalized_role = ?,
                       title = ?,
                       site = ?
                 where fingerprint = 'fp-1'
                """,
                (
                    "Authorization: Bearer location-secret, session_token=secret-region",
                    "password=seniority-secret",
                    "cookie=remote-secret",
                    "li_at=role-secret",
                    "Senior Data Engineer",
                    "li_at=site-secret",
                ),
            )
            conn.execute(
                "update job_salaries set currency = ? where fingerprint = 'fp-1'",
                ("Authorization: Bearer salary-secret",),
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))

        res = client.post(
            "/api/jobs/local-search",
            json={
                "query": "data engineer",
                "filters": {
                    "source": "all",
                },
                "limit": 5,
            },
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["count"] == 1
        row = body["results"][0]
        assert row["source_country"] is None
        assert row["source_city"] is None
        assert row["source_region"] is None
        assert row["source_location"] is None
        assert row["remote_mode"] is None
        assert row["salary_text"] is None
        assert row["seniority"] is None
        assert row["job_family"] == ""
        assert row["source_metadata"]["site"] == "local"
        raw = json.dumps(body).lower()
        for forbidden in (
            "li_at",
            "site-secret",
            "secret-country",
            "session_token",
            "secret-region",
            "authorization",
            "bearer",
            "location-secret",
            "cookie=remote-secret",
            "salary-secret",
            "password=seniority-secret",
            "role-secret",
        ):
            assert forbidden not in raw

        poisoned_criteria = client.post(
            "/api/jobs/local-search",
            json={
                "query": "data engineer",
                "filters": {
                    "source": "all",
                    "country": "li_at=secret-country",
                    "region": "session_token=secret-region",
                    "remote_mode": "cookie=remote-secret",
                    "seniority": "password=seniority-secret",
                    "job_family": "authorization=Bearer family-secret",
                    "rank_bucket": "session_token=rank-secret",
                },
                "limit": 5,
            },
        )
        assert poisoned_criteria.status_code == 200, poisoned_criteria.get_data(as_text=True)
        criteria_body = poisoned_criteria.get_json()
        assert criteria_body["search_criteria"]["remote_mode"] == ""
        assert criteria_body["search_criteria"]["seniority"] == ""
        assert criteria_body["search_criteria"]["job_family"] == ""
        assert criteria_body["search_criteria"]["rank_bucket"] == ""
        criteria_raw = json.dumps(criteria_body).lower()
        for forbidden in (
            "li_at",
            "secret-country",
            "session_token",
            "secret-region",
            "authorization",
            "bearer",
            "cookie=remote-secret",
            "password=seniority-secret",
            "family-secret",
            "rank-secret",
        ):
            assert forbidden not in criteria_raw

    def test_local_jobs_projection_can_be_saved_as_review_import_run(self, server_module, client, fake_supabase, tmp_path, monkeypatch):
        db_path = tmp_path / "jobs.db"
        _write_local_jobs_db(db_path)
        monkeypatch.setenv("ONEPAGER_LOCAL_JOBS_DB", str(db_path))
        search = client.post(
            "/api/jobs/local-search",
            json={"query": "data engineer", "filters": {"source": "linkedin", "country": "DE"}, "limit": 5},
        ).get_json()

        res = client.post(
            "/api/jobs/import-runs",
            json={
                "provider": "manual",
                "query": "data engineer",
                "filters": {"source": "linkedin"},
                "postings": search["results"],
            },
        )
        assert res.status_code == 202, res.get_data(as_text=True)
        body = res.get_json()
        result = body["results"][0]
        assert result["source_provider"] == "linkedin"
        assert result["source_type"] == "csv_import"
        assert result["verification_status"] == "stale"
        assert result["job_family"] == "Data Engineer"
        assert result["source_city"] == "Berlin"
        assert result["source_country"] == "DE"
        assert result["search_criteria"]["schema"] == "onepager.job_search_criteria.v1"
        assert "normalized title match" in result["match_reasons"]

    def test_import_run_api_sanitizes_metadata_filters_and_registry(self, server_module, client, fake_supabase):
        res = client.post(
            "/api/jobs/import-runs",
            json={
                "provider": "linkedin",
                "query": "session_token=query-secret",
                "location": "Authorization: Bearer location-secret",
                "filters": {
                    "country": "DE",
                    "headers": {"authorization": "Bearer eyJhbGciOiJIUzI1NiJ9"},
                    "cookie": "li_at=secret-cookie",
                    "session_token": "abc123",
                },
                "postings": [
                    {
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "jd_text": JD_TEXT,
                        "source_url": "https://www.linkedin.com/jobs/view/1",
                    }
                ],
            },
        )
        assert res.status_code == 202, res.get_data(as_text=True)
        body = res.get_json()
        assert body["query"] is None
        assert body["location"] is None
        assert body["metadata"]["filters"] == {"country": "DE"}
        assert body["metadata"]["source_registry"]["id"] == "linkedin"
        assert "approved_adapter_env" not in json.dumps(body["metadata"])
        listed = client.get("/api/jobs/import-runs")
        assert listed.status_code == 200
        raw = listed.get_data(as_text=True)
        assert "approved_adapter_env" not in raw
        for forbidden in ("li_at", "secret-cookie", "authorization", "bearer", "session_token", "query-secret", "location-secret"):
            assert forbidden not in raw.lower()

    def test_import_posting_metadata_is_sanitized_before_result_and_saved_job(self, server_module, client, fake_supabase):
        malicious = {
            "origin": "local_jobs_db_read_only",
            "site": "linkedin",
            "terms_basis": "weakened_client_value",
            "manual_verification_required": False,
            "crawl_policy": "client_weakened_policy",
            "normalized_role": "data_engineer",
            "first_seen": "2026-06-01",
            "source_db": "/home/example/databases/jobs.db",
            "li_at": "secret-cookie",
            "authorization": "Bearer eyJhbGciOiJIUzI1NiJ9",
            "headers": {"cookie": "li_at=secret-cookie", "Authorization": "Bearer eyJ"},
            "session_token": "abc123",
        }
        res = client.post(
            "/api/jobs/import-runs",
            json={
                "provider": "linkedin",
                "query": "data engineer",
                "postings": [
                    {
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "jd_text": JD_TEXT,
                        "source_url": "https://www.linkedin.com/jobs/view/1",
                        "source_external_id": "li-1",
                        "source_metadata": malicious,
                        "country": "li_at=secret-country",
                        "region": "session_token=secret-region",
                        "source_location": "Authorization: Bearer location-secret",
                        "remote_mode": "cookie=remote-secret",
                        "salary_text": "Authorization: Bearer salary-secret",
                        "seniority": "password=seniority-secret",
                    }
                ],
            },
        )
        assert res.status_code == 202, res.get_data(as_text=True)
        body = res.get_json()
        run_id = body["id"]
        result_id = body["results"][0]["id"]
        result_meta = body["results"][0]["source_metadata"]
        assert result_meta["origin"] == "local_jobs_db_read_only"
        assert result_meta["site"] == "linkedin"
        assert result_meta["normalized_role"] == "data_engineer"
        assert result_meta["terms_basis"] == "manual_user_supplied"
        assert result_meta["manual_verification_required"] is True
        assert result_meta["crawl_policy"] == "manual_user_supplied_no_fetch"
        assert "source_db" not in result_meta
        serialized_result = json.dumps(body["results"][0]).lower()
        for forbidden in ("li_at", "cookie", "session", "authorization", "bearer", "salary-secret", "location-secret", "/users/private-owner"):
            assert forbidden not in serialized_result

        stored_result = fake_supabase.tables["job_import_results"][0]
        stored_serialized = json.dumps(stored_result).lower()
        for forbidden in ("li_at", "cookie", "session", "authorization", "bearer", "salary-secret", "location-secret", "/users/private-owner"):
            assert forbidden not in stored_serialized

        saved = client.post(f"/api/jobs/import-runs/{run_id}/save-selected", json={"result_ids": [result_id]})
        assert saved.status_code == 200, saved.get_data(as_text=True)
        job = _jobs(fake_supabase)[0]
        assert job["source_metadata"]["origin"] == "local_jobs_db_read_only"
        assert "source_db" not in job["source_metadata"]
        stored_job_serialized = json.dumps(job).lower()
        for forbidden in ("li_at", "cookie", "session", "authorization", "bearer", "salary-secret", "location-secret", "/users/private-owner"):
            assert forbidden not in stored_job_serialized

    def test_import_posting_search_criteria_and_external_id_are_sanitized_before_storage(
        self, server_module, client, fake_supabase
    ):
        res = client.post(
            "/api/jobs/import-runs",
            json={
                "provider": "manual",
                "query": "data engineer",
                "postings": [
                    {
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "jd_text": JD_TEXT,
                        "source_url": "http://0177.0.0.1/internal",
                        "source_external_id": "sk-proj-secret-api-key-1234567890abcdef",
                        "search_criteria": {
                            "schema": "onepager.job_search_criteria.v1",
                            "title": "AIzaSySecretGoogleApiKey1234567890",
                            "titles": ["Data Engineer", "session_token=secret", "sk-ant-api03-secret1234567890"],
                            "countries": ["DE", "li_at=secret-cookie"],
                            "cities": ["Berlin", "ya29.oauthSecretToken1234567890"],
                            "search_mode": "strict",
                            "remote_mode": "remote",
                            "source": "linkedin",
                            "freshness": "fresh",
                            "verification": "stale",
                            "headers": {"authorization": "Bearer secret"},
                        },
                        "match_reasons": ["normalized title match", "authorization=Bearer secret", "sk-ant-api03-secret1234567890"],
                        "job_family": "Data",
                        "source_city": "Berlin",
                    }
                ],
            },
        )

        assert res.status_code == 202, res.get_data(as_text=True)
        row = fake_supabase.tables["job_import_results"][0]
        serialized = json.dumps(row).lower()
        assert row["source_url"] == ""
        assert row["source_external_id"] != "sk-proj-secret-api-key-1234567890abcdef"
        assert row["source_metadata"]["search_criteria"]["schema"] == "onepager.job_search_criteria.v1"
        assert row["source_metadata"]["search_criteria"]["titles"] == ["Data Engineer"]
        assert row["source_metadata"]["search_criteria"]["countries"] == ["DE"]
        assert row["source_metadata"]["search_criteria"]["cities"] == ["Berlin"]
        assert row["source_metadata"]["match_reasons"] == ["normalized title match"]
        for forbidden in ("li_at", "secret-cookie", "session_token", "authorization", "bearer eyj", "sk-proj", "sk-ant", "aizasy", "ya29."):
            assert forbidden not in serialized

    def test_public_job_projections_sanitize_persisted_source_identifiers_and_criteria(self):
        import jobs_service

        row = {
            "id": str(_uuid.uuid4()),
            "title": "Senior Data Engineer",
            "company": "Acme GmbH",
            "jd_text": JD_TEXT,
            "source_provider": "linkedin",
            "source_type": "csv_import",
            "source_external_id": "sk-ant-api03-secret1234567890",
            "source_country": "DE",
            "source_city": "authorization=Bearer city",
            "source_location": "Berlin",
            "job_family": "Data",
            "match_reasons": ["normalized title match", "authorization=Bearer secret"],
            "search_criteria": {
                "schema": "onepager.job_search_criteria.v1",
                "title": "sk-proj-secret-api-key-1234567890abcdef",
                "titles": ["Data Engineer", "session_token=secret", "AIzaSySecretGoogleApiKey1234567890"],
                "countries": ["DE", "li_at=secret-cookie"],
                "cities": ["Berlin", "ya29.oauthSecretToken1234567890"],
                "search_mode": "strict",
                "source": "linkedin",
            },
        }

        result = jobs_service._public_import_result(row)
        job = jobs_service._public_job(row)
        for projected in (result, job):
            serialized = json.dumps(projected).lower()
            assert projected["source_external_id"] is None
            assert projected["search_criteria"]["title"] == ""
            assert projected["source_city"] is None
            assert projected["search_criteria"]["titles"] == ["Data Engineer"]
            assert projected["search_criteria"]["countries"] == ["DE"]
            assert projected["search_criteria"]["cities"] == ["Berlin"]
            assert projected["match_reasons"] == ["normalized title match"]
            for forbidden in ("li_at", "secret-session-cookie", "session_token", "authorization", "bearer secret", "sk-ant", "sk-proj", "aizasy", "ya29."):
                assert forbidden not in serialized

    def test_job_import_migration_has_rls_grants_and_lineage_columns(self):
        sql = (REPO / "supabase" / "migrations" / "0010_job_import_provenance.sql").read_text()
        assert "create table if not exists public.job_import_runs" in sql
        assert "create table if not exists public.job_import_results" in sql
        assert "alter table public.job_import_runs enable row level security" in sql
        assert "alter table public.job_import_results enable row level security" in sql
        assert "job_import_runs_select_own" in sql
        assert "job_import_results_select_own" in sql
        assert 'drop policy if exists "job_import_runs_delete_own"' in sql
        assert 'drop policy if exists "job_import_results_delete_own"' in sql
        assert 'create policy "job_import_runs_delete_own"' in sql
        assert 'create policy "job_import_results_delete_own"' in sql
        assert "grant select, insert, delete on public.job_import_runs to authenticated" in sql
        assert "grant select, insert, delete on public.job_import_results to authenticated" in sql
        assert "grant update (verification_status, selected, saved_job_id, updated_at)" in sql
        for policy in (
            "job_import_runs_select_own",
            "job_import_results_select_own",
        ):
            block = sql.split(f'create policy "{policy}"', 1)[1].split("drop policy", 1)[0]
            assert "using (owner_id = (select auth.uid()))" in block
            assert "p.deletion_requested_at is not null" not in block
        for column in (
            "import_run_id",
            "source_provider",
            "source_type",
            "source_external_id",
            "verification_status",
            "content_hash",
        ):
            assert f"add column if not exists {column}" in sql
        assert "where jir.id = saved_jobs.import_run_id" in sql
        assert "and jir.owner_id = (select auth.uid())" in sql

    def test_job_import_rls_delete_hardening_is_forward_migration(self):
        sql = (REPO / "supabase" / "migrations" / "0018_job_import_rls_delete_hardening.sql").read_text()
        for policy in (
            "job_import_runs_select_own",
            "job_import_results_select_own",
        ):
            block = sql.split(f'create policy "{policy}"', 1)[1].split("drop policy", 1)[0]
            assert "p.deletion_requested_at is not null" in block
        assert 'drop policy if exists "job_import_runs_delete_own"' in sql
        assert 'drop policy if exists "job_import_results_delete_own"' in sql
        assert 'create policy "job_import_runs_delete_own"' not in sql
        assert 'create policy "job_import_results_delete_own"' not in sql
        assert "revoke delete on public.job_import_runs from authenticated" in sql
        assert "revoke delete on public.job_import_results from authenticated" in sql
        assert 'drop policy if exists "job_import_runs_insert_own"' in sql
        assert 'drop policy if exists "job_import_runs_update_own"' in sql
        assert "revoke insert on public.job_import_runs from authenticated" in sql
        assert "revoke update on public.job_import_runs from authenticated" in sql

    def test_job_import_trust_hardening_migration_removes_browser_trust_claims(self):
        sql = (REPO / "supabase" / "migrations" / "0017_job_import_trust_hardening.sql").read_text()
        assert "update public.job_import_results" in sql
        assert "source_provider = 'other'" in sql
        assert "source_type = 'csv_import'" in sql
        assert "verification_status = 'needs_review'" in sql
        assert "revoke insert on public.job_import_results from authenticated" in sql
        assert "revoke update on public.job_import_results from authenticated" in sql
        assert "grant select on public.job_import_results to authenticated" in sql
        assert "grant select, insert on public.job_import_results" not in sql
        results_provider_check = sql.split("job_import_results_source_provider_check", 2)[2].split("alter table", 1)[0]
        assert "approved_adapter" not in results_provider_check
        results_type_check = sql.split("job_import_results_source_type_check", 2)[2].split("alter table", 1)[0]
        assert "approved_adapter" not in results_type_check
        results_verification_check = sql.split("job_import_results_verification_status_check", 2)[2].split("alter table", 1)[0]
        assert "verified" not in results_verification_check
        saved_provider_check = sql.split("saved_jobs_source_provider_check", 2)[2].split("alter table", 1)[0]
        assert "approved_adapter" not in saved_provider_check
        saved_type_check = sql.split("saved_jobs_source_type_check", 2)[2].split("alter table", 1)[0]
        assert "approved_adapter" not in saved_type_check
        saved_verification_check = sql.split("saved_jobs_verification_status_check", 2)[2].split("revoke insert", 1)[0]
        assert "verified" not in saved_verification_check

    def test_job_experience_requirements_migration_adds_required_columns(self):
        sql = (REPO / "supabase" / "migrations" / "0014_job_experience_requirements.sql").read_text()

        assert "alter table public.saved_jobs" in sql
        assert "add column if not exists experience_requirements jsonb not null default '{}'::jsonb" in sql
        assert "alter table public.job_import_results" in sql
        saved_jobs_grant = sql.split(
            "grant update (title, company, jd_text, jd_json, source_url, source_metadata,",
            1,
        )[1]
        assert "source_provider, source_type, source_external_id" not in saved_jobs_grant.split("on public.saved_jobs", 1)[0]
        for field in ("content_hash", "last_seen_at", "expires_at", "experience_requirements"):
            assert field in saved_jobs_grant.split("on public.saved_jobs", 1)[0]
        assert "verification_status" not in saved_jobs_grant.split("on public.saved_jobs", 1)[0]
        assert "revoke update on public.job_import_results from authenticated" in sql
        assert "grant update (verification_status, selected, saved_job_id, updated_at)" not in sql

    def test_manual_import_preview_save_selected_and_mark_stale(self, server_module, client, fake_supabase):
        res = client.post(
            "/api/jobs/import-runs",
            json={
                "provider": "manual",
                "query": "data engineer",
                "location": "Berlin",
                "filters": {"country": "DE", "region": "Berlin", "remote_mode": "hybrid", "seniority": "senior"},
                "postings": [
                    {
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "jd_text": JD_TEXT,
                        "source_external_id": "acme-1",
                    }
                ],
            },
        )
        assert res.status_code == 202, res.get_data(as_text=True)
        body = res.get_json()
        run_id = body["id"]
        result_id = body["results"][0]["id"]
        assert body["metadata"]["crawl_policy"] == "manual_user_supplied_no_fetch"
        assert body["results"][0]["source_country"] == "DE"
        assert body["results"][0]["verification_status"] == "user_provided"

        preview = client.get(f"/api/jobs/import-runs/{run_id}/results")
        assert preview.status_code == 200
        assert [r["id"] for r in preview.get_json()["results"]] == [result_id]

        saved = client.post(
            f"/api/jobs/import-runs/{run_id}/save-selected",
            json={"result_ids": [result_id]},
        )
        assert saved.status_code == 200, saved.get_data(as_text=True)
        assert saved.get_json()["saved_count"] == 1
        job = _jobs(fake_supabase)[0]
        assert job["import_run_id"] == run_id
        assert job["source_provider"] == "manual"
        assert job["source_external_id"] == "acme-1"

        stale = client.post(f"/api/jobs/import-runs/{run_id}/mark-stale", json={"result_ids": [result_id]})
        assert stale.status_code == 200
        assert stale.get_json()["stale_count"] == 1

    def test_client_import_cannot_claim_approved_adapter_or_verified_trust(
        self, server_module, client, fake_supabase
    ):
        res = client.post(
            "/api/jobs/import-runs",
            json={
                "provider": "manual",
                "query": "data engineer",
                "postings": [
                    {
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "jd_text": JD_TEXT,
                        "source_external_id": "client-claimed-adapter",
                        "source_provider": "approved_adapter",
                        "source_type": "csv_import",
                        "verification_status": "verified",
                    }
                ],
            },
        )
        assert res.status_code == 202, res.get_data(as_text=True)
        body = res.get_json()
        result = body["results"][0]
        assert result["source_provider"] == "other"
        assert result["source_type"] == "csv_import"
        assert result["verification_status"] == "needs_review"

        saved = client.post(
            f"/api/jobs/import-runs/{body['id']}/save-selected",
            json={"result_ids": [result["id"]]},
        )
        assert saved.status_code == 200, saved.get_data(as_text=True)
        job = _jobs(fake_supabase)[0]
        assert job["source_provider"] == "other"
        assert job["verification_status"] == "needs_review"

    def test_save_selected_revalidates_persisted_import_result_trust(
        self, server_module, client, fake_supabase
    ):
        res = client.post(
            "/api/jobs/import-runs",
            json={
                "provider": "manual",
                "query": "data engineer",
                "postings": [
                    {
                        "title": "Senior Data Engineer",
                        "company": "Acme GmbH",
                        "jd_text": JD_TEXT,
                        "source_external_id": "persisted-claim",
                        "source_type": "csv_import",
                    }
                ],
            },
        )
        assert res.status_code == 202, res.get_data(as_text=True)
        body = res.get_json()
        stored_result = fake_supabase.tables["job_import_results"][0]
        stored_result["source_provider"] = "approved_adapter"
        stored_result["verification_status"] = "verified"

        saved = client.post(
            f"/api/jobs/import-runs/{body['id']}/save-selected",
            json={"result_ids": [body["results"][0]["id"]]},
        )
        assert saved.status_code == 200, saved.get_data(as_text=True)
        job = _jobs(fake_supabase)[0]
        assert job["source_provider"] == "other"
        assert job["verification_status"] == "needs_review"

    def test_demo_import_preview_degrades_when_import_tables_are_missing(
        self, server_module, client, bypass_auth, monkeypatch
    ):
        import jobs_service as js

        class MissingImportSchema:
            def table(self, _name):
                raise RuntimeError("PGRST205 Could not find the table 'public.job_import_runs' in the schema cache")

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "true")
        monkeypatch.setattr(js.auth_mod, "user_supabase", lambda: MissingImportSchema())
        js._DEMO_IMPORT_RUNS.clear()
        js._DEMO_IMPORT_RESULTS.clear()

        res = client.post(
            "/api/jobs/import-runs",
            json={
                "provider": "linkedin",
                "filters": {"country": "DE", "region": "Berlin", "remote_mode": "hybrid", "seniority": "senior"},
                "postings": [{"title": "QA Engineer", "company": "Example GmbH", "jd_text": JD_TEXT}],
            },
        )
        assert res.status_code == 202, res.get_data(as_text=True)
        body = res.get_json()
        assert body["demo_degraded"] is True
        run_id = body["id"]
        result_id = body["results"][0]["id"]
        assert body["results"][0]["verification_status"] == "needs_review"

        listed = client.get(f"/api/jobs/import-runs/{run_id}/results")
        assert listed.status_code == 200
        assert listed.get_json()["results"][0]["id"] == result_id

        saved = client.post(f"/api/jobs/import-runs/{run_id}/save-selected", json={"result_ids": [result_id]})
        assert saved.status_code == 200
        assert saved.get_json()["saved_count"] == 1

        stale = client.post(f"/api/jobs/import-runs/{run_id}/mark-stale", json={"result_ids": [result_id]})
        assert stale.status_code == 200
        assert stale.get_json()["stale_count"] == 1

    def test_linkedin_manual_import_preview_requires_text_and_needs_review(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        def _boom(*_a, **_k):
            raise AssertionError("LinkedIn manual preview must not fetch")

        import jobs_service as js

        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _boom)
        missing = client.post(
            "/api/jobs/import-runs",
            json={"provider": "linkedin", "postings": [{"source_url": "https://linkedin.com/jobs/view/1"}]},
        )
        assert missing.status_code == 400
        assert missing.get_json()["code"] == "linkedin_manual_required"

        res = client.post(
            "/api/jobs/import-runs",
            json={
                "provider": "linkedin",
                "postings": [{"jd_text": JD_TEXT, "source_url": "https://linkedin.com/jobs/view/1"}],
            },
        )
        assert res.status_code == 202, res.get_data(as_text=True)
        result = res.get_json()["results"][0]
        assert result["source_provider"] == "linkedin"
        assert result["verification_status"] == "needs_review"


# ===========================================================================
#  6. Cross-tenant — user B cannot touch user A's job (404).
#  Runs against the REAL Supabase project with real user JWTs; SKIPS cleanly
#  when the provisioning env vars are absent (same pattern as Stage 2/4/5/6).
# ===========================================================================
A_TOKEN = os.environ.get("TEST_A_TOKEN")
A_ID = os.environ.get("TEST_A_ID")
B_TOKEN = os.environ.get("TEST_B_TOKEN")
B_ID = os.environ.get("TEST_B_ID")
_HAVE_USERS = all([A_TOKEN, A_ID, B_TOKEN, B_ID])


@pytest.mark.skipif(
    not _HAVE_USERS,
    reason="TEST_A_TOKEN/TEST_B_TOKEN (+ ids) not set — provision real users first",
)
class TestJobsRLS:
    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_b_cannot_read_update_delete_or_generate_a_job(self):
        """A creates a saved job; B must get 404 on read / status / delete /
        generate, and A's job must survive untouched."""
        import importlib

        from supabase import create_client

        server = importlib.import_module("server")
        server.app.config.update(TESTING=True)
        client = server.app.test_client()

        url = os.environ["SUPABASE_URL"]
        pub = os.environ["SUPABASE_PUBLISHABLE_KEY"]

        # A creates a job directly under its own JWT (RLS owner default).
        sb_a = create_client(url, pub)
        sb_a.postgrest.auth(A_TOKEN)
        created = (
            sb_a.table("saved_jobs")
            .insert({"title": "A-secret-job", "company": "ACorp", "jd_text": JD_TEXT, "status": "saved"})
            .execute()
            .data
        )
        assert created, "A should be able to create a job"
        a_job_id = created[0]["id"]

        # A also needs a CV for the generate attempt to even reach the job check.
        a_cv = (
            sb_a.table("documents")
            .insert({"type": "cv", "name": "A-cv", "yaml_blob": _yaml.safe_dump(GROUNDED_CV, sort_keys=False)})
            .execute()
            .data
        )
        a_cv_id = a_cv[0]["id"]

        try:
            # B: read A's job → 404 (RLS hides it).
            r = client.get(f"/api/jobs/{a_job_id}", headers=self._auth(B_TOKEN))
            assert r.status_code == 404, r.get_data(as_text=True)

            # B: flip A's status → 404, and A's status is unchanged.
            s = client.post(
                f"/api/jobs/{a_job_id}/status",
                json={"status": "applied"},
                headers=self._auth(B_TOKEN),
            )
            assert s.status_code == 404

            # B: generate against A's job → 404 (job invisible).
            g = client.post(
                f"/api/jobs/{a_job_id}/generate",
                json={"source_cv_id": a_cv_id},
                headers=self._auth(B_TOKEN),
            )
            assert g.status_code == 404

            # B: delete A's job → 404, and A's job survives.
            d = client.delete(f"/api/jobs/{a_job_id}", headers=self._auth(B_TOKEN))
            assert d.status_code == 404

            still = sb_a.table("saved_jobs").select("id, status").eq("id", a_job_id).execute().data
            assert still, "RLS leak: A's job vanished"
            assert still[0]["status"] == "saved", "RLS leak: B changed A's status"
            # B created no document under A's ownership.
            b_docs = sb_a.table("documents").select("id").eq("owner_id", B_ID).execute().data
            assert not (b_docs or []), "RLS leak: B created a doc against A's job"
        finally:
            sb_a.table("documents").delete().eq("id", a_cv_id).execute()
            sb_a.table("saved_jobs").delete().eq("id", a_job_id).execute()
