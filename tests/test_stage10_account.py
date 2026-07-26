"""Stage 10 tests — account & data self-service (the GDPR primitives).

Two tiers, matching the rest of the suite:

  * UNIT (in-memory ``fake_supabase``): profile read/update, the credit-tamper
    re-check (managed_credits_remaining cannot be written via /api/account/
    profile), the JSON export contents + the hard guarantee that NO key
    ciphertext leaks into it, and the soft-delete model (request_account_
    deletion soft-deletes documents AND they vanish from /api/cvs etc.). Also
    the route gating: /privacy is auth-exempt, /account is a servable shell.

  * REAL-JWT (skipped unless provisioned): cross-tenant isolation — B cannot
    export or delete A's account — using real user JWTs (TEST_A_TOKEN/
    TEST_B_TOKEN). An isolated-project-only service-role check validates the
    D+30 claim plus Auth hard-delete cascade. All env-gated so they skip
    cleanly offline and never mutate a shared production project.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"

# A realistic-looking encrypted key row so we can prove the export excludes the
# secret columns. The values are arbitrary base64 — the point is the COLUMN
# NAMES (ciphertext/nonce/wrapped_dek/dek_nonce) and their content must never
# appear in the export.
_SECRET_CIPHERTEXT = base64.b64encode(
    b"synthetic ciphertext fixture"
).decode("ascii")
_SECRET_WRAPPED = base64.b64encode(
    b"synthetic wrapped key fixture"
).decode("ascii")


# ---------------------------------------------------------------------------
# Fake-supabase seeding helpers (mirror the Stage 4 test helpers).
# ---------------------------------------------------------------------------
def _seed_profile(store, owner_id: str, *, credits: int = 3, **extra) -> dict:
    row = {
        "id": owner_id,
        "email": "owner@onepager.test",
        "display_name": None,
        "default_language": "en",
        "managed_credits_remaining": credits,
        "deletion_requested_at": None,
        **extra,
    }
    store.tables.setdefault("profiles", []).append(row)
    return row


def _seed_document(store, owner_id: str, *, doc_type="cv", name="My CV", blob="name: X\n") -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "owner_id": owner_id,
        "type": doc_type,
        "name": name,
        "yaml_blob": blob,
        "deleted_at": None,
    }
    store.tables.setdefault("documents", []).append(row)
    return row


def _seed_saved_job(store, owner_id: str, *, title="Data Engineer", **extra) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "owner_id": owner_id,
        "title": title,
        "company": "Acme",
        "jd_text": "Build pipelines.",
        "status": "saved",
        **extra,
    }
    store.tables.setdefault("saved_jobs", []).append(row)
    return row


def _seed_import_run(store, owner_id: str, **extra) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "owner_id": owner_id,
        "provider": "linkedin",
        "query": "data engineer",
        "location": "Berlin",
        "status": "completed",
        "started_at": "2026-07-01T10:00:00Z",
        "finished_at": "2026-07-01T10:01:00Z",
        "requested_limit": 10,
        "imported_count": 1,
        "skipped_count": 0,
        "error": None,
        "metadata": {
            "crawl_policy": "manual_review_only",
            "filters": {"country": "DE", "headers": "Bearer eyJhbGciOiJIUzI1NiJ9", "cookie": "li_at=secret-cookie"},
            "source_registry": {
                "linkedin": {
                    "id": "linkedin",
                    "label": "LinkedIn",
                    "terms_basis": "manual_review_required",
                    "supports_preview": True,
                    "supports_fetch": False,
                    "requires_review": True,
                    "authorization": "Bearer secret-token",
                }
            },
            "session_token": "run-secret",
        },
        **extra,
    }
    store.tables.setdefault("job_import_runs", []).append(row)
    return row


def _seed_import_result(store, owner_id: str, run_id: str, **extra) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "owner_id": owner_id,
        "import_run_id": run_id,
        "source_provider": "linkedin",
        "source_type": "manual_paste",
        "source_external_id": "safe-123",
        "title": "Imported Data Engineer",
        "company": "Acme",
        "jd_text": "Build pipelines.",
        "jd_json": {"title": "Imported Data Engineer"},
        "source_url": "https://www.linkedin.com/jobs/view/123",
        "source_metadata": {
            "terms_basis": "manual_review_required",
            "manual_verification_required": True,
            "crawl_policy": "manual_review_only",
            "origin": "local_preview",
            "site": "linkedin",
            "date_posted": "2026-07-01",
            "first_seen": "2026-07-01T10:00:00Z",
            "normalized_role": "data_engineer",
            "source_db": "sanitized-local-index",
            "li_at": "secret-cookie",
            "authorization": "Bearer secret-token",
        },
        "source_country": "DE",
        "source_region": "Berlin",
        "source_location": "Berlin, Germany",
        "remote_mode": "hybrid",
        "salary_text": "EUR 80k",
        "seniority": "senior",
        "verification_status": "needs_review",
        "content_hash": "sha256:abc",
        "last_seen_at": "2026-07-01T10:00:00Z",
        "expires_at": "2026-08-01",
        "selected": False,
        "saved_job_id": None,
        "created_at": "2026-07-01T10:00:00Z",
        "updated_at": "2026-07-01T10:00:00Z",
        **extra,
    }
    store.tables.setdefault("job_import_results", []).append(row)
    return row


def _seed_application_packet(
    store,
    owner_id: str,
    *,
    job_id: str | None = None,
    source_cv_id: str | None = None,
    tailored_cv_id: str | None = None,
    cover_letter_id: str | None = None,
    **extra,
) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "owner_id": owner_id,
        "job_id": job_id,
        "source_cv_id": source_cv_id,
        "tailored_cv_id": tailored_cv_id,
        "cover_letter_id": cover_letter_id,
        "status": "packet_created",
        "review_status": "reviewed",
        "provider": "anthropic",
        "model": "claude-test",
        "prompt_version": "jobs_packet.v1",
        "score_system_id": "semantic_fit",
        "score_snapshot": {"score": 88, "matched": [{"term": "Spark"}]},
        "source_cv_hash": "sha256:source",
        "job_content_hash": "sha256:job",
        "tailored_cv_hash": "sha256:tailored",
        "cover_letter_hash": "sha256:letter",
        "cover_letter_review_status": "confirmed",
        "cover_letter_review_confirmed_at": "2026-07-02T12:00:00Z",
        "cover_letter_review_id": "review-export",
        "input_hash": "sha256:legacy-input",
        "output_hash": "sha256:legacy-output",
        "packet_version": 1,
        "profile_answers_complete": True,
        "legal_answers_confirmed": True,
        "visa_answers_confirmed": True,
        "archived_at": None,
        "created_at": "2026-07-01T10:00:00Z",
        "updated_at": "2026-07-01T10:01:00Z",
        **extra,
    }
    store.tables.setdefault("application_packets", []).append(row)
    return row


def _seed_llm_key(store, owner_id: str) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "owner_id": owner_id,
        "provider": "anthropic",
        "ciphertext": _SECRET_CIPHERTEXT,
        "nonce": "Tk9OQ0U=",
        "wrapped_dek": _SECRET_WRAPPED,
        "dek_nonce": "REVLTk9OQ0U=",
        "last4": "WXYZ",
    }
    store.tables.setdefault("user_llm_keys", []).append(row)
    return row


# ===========================================================================
#  1. Route gating: /privacy auth-exempt, /account a servable shell.
# ===========================================================================
class TestRouteGating:
    def test_privacy_is_auth_exempt(self, client, server_module):
        """No bearer token, real auth guard active — /privacy still serves."""
        res = client.get("/privacy")
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_data(as_text=True)
        assert "Privacy Policy" in body
        # Names the controller contact + the key processors. The address is
        # operator-configurable (ONEPAGER_PRIVACY_CONTACT) because a self-hoster
        # is their own data controller, so assert the token was substituted with
        # a deliverable mailto rather than pinning a hardcoded literal.
        contact = server_module.PRIVACY_CONTACT
        assert "@" in contact, "privacy contact must be an email address"
        assert "__PRIVACY_CONTACT__" not in body, "contact token left unsubstituted"
        assert f'href="mailto:{contact}"' in body
        for proc in ("Supabase", "Anthropic", "Cloudflare", "Resend", "PostHog"):
            assert proc in body, f"privacy page must name processor {proc}"
        assert "Self-hosting template" in body
        assert "only when configured" in body
        assert "regions actually selected" in body
        assert "will be finalised before public launch" not in body
        assert "EU — Frankfurt" not in body

    def test_account_shell_servable(self, client):
        """/account is exempt at the HTML-shell level (gated client-side)."""
        res = client.get("/account")
        assert res.status_code == 200
        body = res.get_data(as_text=True)
        assert "Settings &amp; account" in body
        assert 'id="settings"' in body

    def test_settings_alias_serves_account_shell(self, client):
        """/settings is a deliberate alias for the Account-owned settings UI."""
        res = client.get("/settings")
        assert res.status_code == 200
        body = res.get_data(as_text=True)
        assert "App theme controls the editor chrome only" in body
        assert "CV Appearance remains document-specific" in body

    def test_settings_shell_has_safe_job_sources_card(self, client):
        res = client.get("/settings")
        assert res.status_code == 200
        body = res.get_data(as_text=True)
        assert 'id="job-sources"' in body
        assert 'id="ac-job-source-status"' in body
        for selector_id in (
            "ac-job-profile-family",
            "ac-job-profile-titles",
            "ac-job-profile-mode",
            "ac-job-profile-cities",
            "ac-job-profile-min-years",
            "ac-job-profile-ranking",
        ):
            assert f'id="{selector_id}"' in body
        assert "No blind LinkedIn fetch" in body
        assert "No stored LinkedIn sessions" in body
        assert "No stealth automation" in body
        job_sources = body.split('id="job-sources"', 1)[1].split("<!-- Profile", 1)[0]
        assert "password" not in job_sources.lower()
        assert "li_at" not in job_sources.lower()
        assert 'type="password"' not in job_sources
        assert 'type="file"' not in job_sources

    def test_account_api_requires_auth(self, client):
        """But every /api/account/* route needs a real bearer token → 401."""
        for path, method in (
            ("/api/account/profile", "get"),
            ("/api/account/export", "get"),
            ("/api/account/delete", "post"),
            ("/api/account/cancel-delete", "post"),
        ):
            res = getattr(client, method)(path)
            assert res.status_code == 401, f"{method} {path} -> {res.status_code}"


# ===========================================================================
#  2. Profile read/update — and the credit-tamper re-check.
# ===========================================================================
class TestProfile:
    def test_get_profile(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=2, display_name="Alex")
        res = client.get("/api/account/profile")
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["display_name"] == "Alex"
        assert body["default_language"] == "en"
        assert body["managed_credits_remaining"] == 2

    def test_update_profile_safe_fields(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post(
            "/api/account/profile",
            json={"display_name": "New Name", "default_language": "de"},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["display_name"] == "New Name"
        assert body["default_language"] == "de"
        # Persisted on the row.
        prof = next(r for r in fake_supabase.tables["profiles"] if r["id"] == FAKE_OWNER)
        assert prof["display_name"] == "New Name"
        assert prof["default_language"] == "de"

    def test_update_cannot_change_managed_credits(self, client, fake_supabase):
        """Credit-tamper re-check: a managed_credits_remaining key in the body
        is ignored — the balance is untouched (server-side ignore; the DB
        column grant is the real enforcement, covered by the Stage 4 real test)."""
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post(
            "/api/account/profile",
            json={"display_name": "x", "managed_credits_remaining": 999},
        )
        assert res.status_code == 200
        assert res.get_json()["managed_credits_remaining"] == 3
        prof = next(r for r in fake_supabase.tables["profiles"] if r["id"] == FAKE_OWNER)
        assert prof["managed_credits_remaining"] == 3, "credit self-grant leaked via profile update"

    def test_update_rejects_bad_language(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post("/api/account/profile", json={"default_language": "zz"})
        assert res.status_code == 400

    def test_update_rejects_empty_payload(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post("/api/account/profile", json={})
        assert res.status_code == 400


# ===========================================================================
#  3. Export — contains docs+jobs+profile, and NEVER key secret material.
# ===========================================================================
class TestExport:
    def test_export_contains_all_owned_data(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=1, display_name="Owner")
        _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="CV One",
                       blob="name: CV-ONE-MARKER\n")
        _seed_document(fake_supabase, FAKE_OWNER, doc_type="cover_letter",
                       name="Letter", blob="body: LETTER-MARKER\n")
        _seed_saved_job(fake_supabase, FAKE_OWNER, title="JOB-MARKER")

        res = client.get("/api/account/export")
        assert res.status_code == 200, res.get_data(as_text=True)
        # Content-Disposition makes it a download.
        cd = res.headers.get("Content-Disposition", "")
        assert "attachment" in cd and "onepager-export" in cd
        data = json.loads(res.get_data(as_text=True))

        assert data["profile"]["display_name"] == "Owner"
        assert len(data["documents"]) == 2
        names = {d["name"] for d in data["documents"]}
        assert names == {"CV One", "Letter"}
        # Full yaml_blob is included (portability).
        blobs = " ".join(d.get("yaml_blob", "") for d in data["documents"])
        assert "CV-ONE-MARKER" in blobs and "LETTER-MARKER" in blobs
        assert len(data["saved_jobs"]) == 1
        assert data["saved_jobs"][0]["title"] == "JOB-MARKER"
        assert data["application_packets"] == []

    def test_export_redacts_source_url_credentials_query_and_fragments(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=1)
        _seed_saved_job(
            fake_supabase,
            FAKE_OWNER,
            title="Secret URL Job",
            source_url=(
                "https://" + "user" + ":" + "pass"
                + "@jobs.example.com/apply?li_at=secret#token"
            ),
            source_external_id="li_at=secret-external-id",
            source_metadata={
                "source_links": [
                    {
                        "label": "Apply",
                        "url": (
                            "https://" + "user" + ":" + "pass"
                            + "@jobs.example.com/apply?token=secret#frag"
                        ),
                        "href": "https://jobs.example.com/role?li_at=secret",
                    },
                    {
                        "label": "Secret path",
                        "url": "https://jobs.example.com/apply/token/secret",
                    },
                ],
            },
        )
        run = _seed_import_run(fake_supabase, FAKE_OWNER)
        _seed_import_result(
            fake_supabase,
            FAKE_OWNER,
            run["id"],
            source_url="https://www.linkedin.com/jobs/view/123?li_at=secret",
            source_external_id="session_token=secret-import-id",
        )

        res = client.get("/api/account/export")

        assert res.status_code == 200
        raw = res.get_data(as_text=True)
        data = json.loads(raw)
        assert data["saved_jobs"][0]["source_url"] == "https://jobs.example.com/apply"
        assert data["saved_jobs"][0]["source_external_id"] is None
        assert data["saved_jobs"][0]["source_metadata"]["source_links"][0]["url"] == "https://jobs.example.com/apply"
        assert data["saved_jobs"][0]["source_metadata"]["source_links"][0]["href"] == "https://jobs.example.com/role"
        assert data["saved_jobs"][0]["source_metadata"]["source_links"][1]["url"] == "https://jobs.example.com"
        assert data["job_import_results"][0]["source_url"] == "https://www.linkedin.com/jobs/view/123"
        assert data["job_import_results"][0]["source_external_id"] is None
        assert "li_at=secret" not in raw
        assert "secret-external-id" not in raw
        assert "secret-import-id" not in raw
        assert "user:pass" not in raw
        assert "#token" not in raw

    @pytest.mark.parametrize(
        "source_url",
        [
            "http://127.0.0.1:8000/apply",
            "http://2130706433/apply",
            "http://0177.0.0.1/apply",
            "ftp://example.com/file",
            "javascript:alert(1)",
        ],
    )
    def test_export_drops_private_numeric_and_non_http_source_urls(self, client, fake_supabase, source_url):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=1)
        _seed_saved_job(fake_supabase, FAKE_OWNER, title="Poisoned URL Job", source_url=source_url)

        res = client.get("/api/account/export")

        assert res.status_code == 200
        raw = res.get_data(as_text=True)
        data = json.loads(raw)
        assert data["saved_jobs"][0]["source_url"] is None
        assert source_url.lower() not in raw.lower()

    def test_export_excludes_key_ciphertext(self, client, fake_supabase):
        """The hard one: a stored key's secret columns must NOT appear in the
        export — only a {provider, last4} hint."""
        _seed_profile(fake_supabase, FAKE_OWNER, credits=0)
        _seed_llm_key(fake_supabase, FAKE_OWNER)

        res = client.get("/api/account/export")
        assert res.status_code == 200
        raw = res.get_data(as_text=True)
        data = json.loads(raw)

        # The key hint is present and safe.
        assert len(data["llm_keys"]) == 1
        hint = data["llm_keys"][0]
        assert hint["provider"] == "anthropic"
        assert hint["last4"] == "WXYZ"
        # The encrypted field NAMES are absent from the hint dict.
        for secret_field in ("ciphertext", "nonce", "wrapped_dek", "dek_nonce"):
            assert secret_field not in hint, f"export leaked key field {secret_field!r}"
        # And the secret VALUES appear nowhere in the whole serialized export.
        assert _SECRET_CIPHERTEXT not in raw
        assert _SECRET_WRAPPED not in raw

    def test_export_excludes_soft_deleted_documents(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=1)
        live = _seed_document(fake_supabase, FAKE_OWNER, name="Live", blob="name: LIVE\n")
        gone = _seed_document(fake_supabase, FAKE_OWNER, name="Gone", blob="name: GONE\n")
        gone["deleted_at"] = 12345  # soft-deleted

        data = json.loads(client.get("/api/account/export").get_data(as_text=True))
        names = {d["name"] for d in data["documents"]}
        assert names == {"Live"}
        assert live["name"] in names

    def test_export_includes_sanitized_job_import_provenance(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=1)
        run = _seed_import_run(fake_supabase, FAKE_OWNER)
        result = _seed_import_result(fake_supabase, FAKE_OWNER, run["id"], source_external_id="session_token=secret-import-id")
        job = _seed_saved_job(fake_supabase, FAKE_OWNER, title="Saved Import")
        job.update(
            {
                "import_run_id": run["id"],
                "field": "Data",
                "deadline": "2026-08-01",
                "confidence": 82,
                "why_fit": "Strong fit",
                "source_provider": "linkedin",
                "source_type": "manual_paste",
                "source_fetched_at": "2026-07-01T10:00:00Z",
                "source_metadata": {
                    "terms_basis": "manual_review_required",
                    "manual_verification_required": True,
                    "origin": "local_preview",
                    "priority_rank": 1,
                    "rank_bucket": "Apply first",
                    "full_jd_status": "missing",
                    "source_artifact": "/tmp/top-tier-jobs.html",
                    "source_links": [{"label": "Apply", "url": "https://jobs.example.com/1"}],
                    "li_at": "secret-cookie",
                    "cookie": "li_at=secret-cookie",
                },
                "source_country": "li_at=secret-country",
                "source_region": "session_token=secret-region",
                "source_location": "Authorization: Bearer location-secret",
                "remote_mode": "cookie=remote-secret",
                "salary_text": "Authorization: Bearer salary-secret",
                "seniority": "password=seniority-secret",
                "verification_status": "needs_review",
                "source_external_id": "li_at=secret-external-id",
                "content_hash": "sha256:abc",
                "experience_requirements": {"total_years_min": 5.0, "evidence": ["5+ years"]},
                "last_seen_at": "2026-07-01T10:00:00Z",
                "expires_at": "2026-08-01",
                "authorization": "Bearer secret-token",
                "session_token": "row-secret",
            }
        )

        res = client.get("/api/account/export")

        assert res.status_code == 200
        raw = res.get_data(as_text=True)
        data = json.loads(raw)
        assert data["job_import_runs"][0]["id"] == run["id"]
        assert data["job_import_results"][0]["id"] == result["id"]
        saved = data["saved_jobs"][0]
        assert saved["source_provider"] == "linkedin"
        assert saved["field"] == "Data"
        assert saved["deadline"] == "2026-08-01"
        assert saved["confidence"] == 82
        assert saved["why_fit"] == "Strong fit"
        assert saved["source_country"] is None
        assert saved["source_region"] is None
        assert saved["source_location"] is None
        assert saved["remote_mode"] is None
        assert saved["salary_text"] is None
        assert saved["seniority"] is None
        assert saved["source_external_id"] is None
        assert saved["source_metadata"] == {
            "terms_basis": "manual_review_required",
            "manual_verification_required": True,
            "origin": "local_preview",
            "priority_rank": 1,
            "rank_bucket": "Apply first",
            "full_jd_status": "missing",
            "source_artifact": "top-tier-jobs.html",
            "source_links": [{"label": "Apply", "url": "https://jobs.example.com/1"}],
        }
        assert saved["experience_requirements"]["total_years_min"] == 5.0
        assert data["job_import_runs"][0]["metadata"]["filters"] == {"country": "DE"}
        assert data["job_import_runs"][0]["metadata"]["source_registry"][0]["id"] == "linkedin"
        assert data["job_import_results"][0]["source_external_id"] is None
        assert data["job_import_results"][0]["source_metadata"]["source_db"] == "sanitized-local-index"
        for forbidden in (
            "li_at",
            "secret-cookie",
            "Bearer secret-token",
            "Bearer eyJhbGciOiJIUzI1NiJ9",
            "session_token",
            "authorization",
            "run-secret",
            "row-secret",
            "salary-secret",
            "location-secret",
            "secret-external-id",
        ):
            assert forbidden not in raw

    def test_export_includes_sanitized_application_packets(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=1)
        source = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="Source CV")
        tailored = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="Tailored CV")
        letter = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cover_letter", name="Letter")
        job = _seed_saved_job(fake_supabase, FAKE_OWNER, title="Packet Job")
        packet = _seed_application_packet(
            fake_supabase,
            FAKE_OWNER,
            job_id=job["id"],
            source_cv_id=source["id"],
            tailored_cv_id=tailored["id"],
            cover_letter_id=letter["id"],
            score_snapshot={
                "score": 91,
                "matched": [{"term": "Spark"}],
                "raw_prompt": "RAW-PROMPT-MARKER",
                "notes": "review token REVIEW-TOKEN-MARKER",
                "authorization": "Bearer packet-secret",
            },
            raw_prompt="RAW-PROMPT-MARKER",
            review_token="REVIEW-TOKEN-MARKER",
            api_key="SECRET-PACKET-KEY",
            yaml_blob="body: packet duplicate must never export\n",
        )

        res = client.get("/api/account/export")

        assert res.status_code == 200
        raw = res.get_data(as_text=True)
        data = json.loads(raw)
        assert len(data["application_packets"]) == 1
        exported = data["application_packets"][0]
        assert exported["id"] == packet["id"]
        assert exported["job_id"] == job["id"]
        assert exported["source_cv_id"] == source["id"]
        assert exported["tailored_cv_id"] == tailored["id"]
        assert exported["cover_letter_id"] == letter["id"]
        assert exported["prompt_version"] == "jobs_packet.v1"
        assert exported["score_system_id"] == "semantic_fit"
        assert exported["score_snapshot"] == {"score": 91, "matched": [{"term": "Spark"}]}
        assert exported["source_cv_hash"] == "sha256:source"
        assert exported["tailored_cv_hash"] == "sha256:tailored"
        assert exported["cover_letter_review_status"] == "confirmed"
        assert exported["cover_letter_review_confirmed_at"] == "2026-07-02T12:00:00Z"
        assert exported["cover_letter_review_id"] == "review-export"
        assert exported["profile_answers_complete"] is True
        assert exported["legal_answers_confirmed"] is True
        assert exported["visa_answers_confirmed"] is True
        packet_raw = json.dumps(exported)
        for field in ("yaml_blob", "raw_prompt", "review_token", "api_key"):
            assert field not in exported
            assert field not in packet_raw
        for forbidden in (
            "RAW-PROMPT-MARKER",
            "REVIEW-TOKEN-MARKER",
            "Bearer packet-secret",
            "SECRET-PACKET-KEY",
            "packet duplicate must never export",
        ):
            assert forbidden not in raw

    def test_export_excludes_archived_and_soft_deleted_document_packets(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=1)
        source = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="Source CV")
        tailored = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="Tailored CV")
        letter = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cover_letter", name="Letter")
        job = _seed_saved_job(fake_supabase, FAKE_OWNER, title="Packet Job")
        live = _seed_application_packet(
            fake_supabase,
            FAKE_OWNER,
            job_id=job["id"],
            source_cv_id=source["id"],
            tailored_cv_id=tailored["id"],
            cover_letter_id=letter["id"],
        )
        _seed_application_packet(
            fake_supabase,
            FAKE_OWNER,
            job_id=job["id"],
            source_cv_id=source["id"],
            tailored_cv_id=tailored["id"],
            cover_letter_id=letter["id"],
            archived_at="2026-07-02T12:00:00Z",
        )
        status_archived = _seed_application_packet(
            fake_supabase,
            FAKE_OWNER,
            job_id=job["id"],
            source_cv_id=source["id"],
            tailored_cv_id=tailored["id"],
            cover_letter_id=letter["id"],
            status="archived",
        )
        gone_source = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="Gone Source")
        gone_source["deleted_at"] = 12345
        gone_tailored = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="Gone Tailored")
        gone_tailored["deleted_at"] = 12345
        gone_letter = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cover_letter", name="Gone Letter")
        gone_letter["deleted_at"] = 12345
        hidden_source = _seed_application_packet(
            fake_supabase,
            FAKE_OWNER,
            job_id=job["id"],
            source_cv_id=gone_source["id"],
            tailored_cv_id=tailored["id"],
            cover_letter_id=letter["id"],
        )
        hidden_tailored = _seed_application_packet(
            fake_supabase,
            FAKE_OWNER,
            job_id=job["id"],
            source_cv_id=source["id"],
            tailored_cv_id=gone_tailored["id"],
            cover_letter_id=letter["id"],
        )
        hidden_letter = _seed_application_packet(
            fake_supabase,
            FAKE_OWNER,
            job_id=job["id"],
            source_cv_id=source["id"],
            tailored_cv_id=tailored["id"],
            cover_letter_id=gone_letter["id"],
        )

        res = client.get("/api/account/export")

        assert res.status_code == 200
        data = json.loads(res.get_data(as_text=True))
        assert [row["id"] for row in data["application_packets"]] == [live["id"]]
        exported_ids = {row["id"] for row in data["application_packets"]}
        assert status_archived["id"] not in exported_ids
        assert hidden_source["id"] not in exported_ids
        assert hidden_tailored["id"] not in exported_ids
        assert hidden_letter["id"] not in exported_ids

    def test_application_packet_migration_has_narrow_rls_schema(self):
        sql = (REPO / "supabase" / "migrations" / "0012_application_packets.sql").read_text()
        lowered = sql.lower()
        assert "create table if not exists public.application_packets" in sql
        assert "alter table public.application_packets enable row level security" in sql
        assert "application_packets_select_own" in sql
        assert "application_packets_insert_own" in sql
        assert "application_packets_update_own" in sql
        assert 'drop policy if exists "application_packets_delete_own"' in sql
        assert 'create policy "application_packets_delete_own"' not in sql
        assert "revoke delete on public.application_packets from authenticated" in sql
        assert "grant select on public.application_packets to authenticated" in sql
        assert "grant select, insert on public.application_packets" not in sql
        assert "revoke insert on public.application_packets from authenticated" in sql
        assert "revoke update on public.application_packets from authenticated" in sql
        assert "grant update (" not in sql
        assert "references public.saved_jobs(id) on delete set null" in sql
        assert "references public.documents(id) on delete set null" in sql
        for policy in (
            "application_packets_select_own",
            "application_packets_insert_own",
            "application_packets_update_own",
        ):
            block = sql.split(f'create policy "{policy}"', 1)[1].split(";", 1)[0]
            assert "and p.deletion_requested_at is not null" in block
        for column in (
            "job_id",
            "source_cv_id",
            "tailored_cv_id",
            "cover_letter_id",
            "score_snapshot",
            "source_cv_hash",
            "job_content_hash",
            "tailored_cv_hash",
            "cover_letter_hash",
            "score_system_id",
            "packet_version",
            "profile_answers_complete",
            "legal_answers_confirmed",
            "visa_answers_confirmed",
        ):
            assert column in sql
        for forbidden in (
            "yaml_blob",
            "cover_letter_body",
            "raw_prompt",
            "review_token",
            "ciphertext",
            "wrapped_dek",
            "nonce",
        ):
            assert forbidden not in lowered

    def test_application_packet_readiness_lineage_migration_exports_review_evidence(self):
        sql = (REPO / "supabase" / "migrations" / "0015_application_packet_readiness_lineage.sql").read_text()

        for column in (
            "cover_letter_review_status",
            "cover_letter_review_confirmed_at",
            "cover_letter_review_id",
        ):
            assert column in sql
        assert "application_packets_cover_letter_review_status_check" in sql
        assert "cover_letter_review_status in ('pending', 'confirmed')" in sql
        assert "revoke update on public.application_packets from authenticated" in sql
        assert "grant update (" not in sql


# ===========================================================================
#  4. Erasure — request soft-deletes docs + stamps profile; docs vanish.
# ===========================================================================
class TestErasure:
    def test_request_deletion_requires_server_confirmation(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=2)

        res = client.post("/api/account/delete")

        assert res.status_code == 400
        assert res.get_json()["code"] == "confirm_required"

    def test_request_deletion_soft_deletes_and_stamps(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=2)
        cv = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="A")
        cl = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cover_letter", name="B")

        res = client.post("/api/account/delete", json={"confirm": "DELETE"})
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["deletion_requested"] is True
        assert body["grace_days"] == 30
        assert body["requested_at"] is not None

        # Profile stamped, documents soft-deleted.
        prof = next(r for r in fake_supabase.tables["profiles"] if r["id"] == FAKE_OWNER)
        assert prof["deletion_requested_at"] is not None
        assert cv["deleted_at"] is not None
        assert cl["deleted_at"] is not None

    def test_soft_deleted_docs_vanish_from_lists(self, client, fake_supabase):
        """After deletion request, the user's soft-deleted documents disappear
        from every normal list/get endpoint.

        The first-run CV template is a one-time onboarding seed. Account
        deletion sets a profile deletion flag and soft-deletes documents, so
        /api/cvs must not create a replacement CV while deletion is pending.
        /api/cover-letters also goes to a clean 0.
        """
        _seed_profile(fake_supabase, FAKE_OWNER, credits=2)
        cv = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="CV-GONE-MARKER")
        _seed_document(fake_supabase, FAKE_OWNER, doc_type="cover_letter", name="B")

        # Sanity: visible before deletion.
        before = client.get("/api/cvs").get_json()
        assert any(c["id"] == cv["id"] for c in before["cvs"])
        assert client.get("/api/cover-letters").get_json()["count"] == 1

        client.post("/api/account/delete", json={"confirm": "DELETE"})

        # The deleted CV is gone from the list and no replacement template is
        # seeded during pending account deletion.
        after = client.get("/api/cvs").get_json()
        assert after["cvs"] == []
        assert after["count"] == 0
        assert not any(c["id"] == cv["id"] for c in after["cvs"]), "soft-deleted CV still listed"
        assert not any(c["name"] == "CV-GONE-MARKER" for c in after["cvs"])
        # Reading the deleted CV by id is a 404 (excluded by deleted_at filter).
        got = client.get(f"/api/cv?id={cv['id']}")
        assert got.status_code == 404
        # Cover letters (no auto-seed) go to a clean zero.
        assert client.get("/api/cover-letters").get_json()["count"] == 0

    def test_pending_deletion_blocks_profile_and_key_mutations(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=2)
        client.post("/api/account/delete", json={"confirm": "DELETE"})

        profile = client.post("/api/account/profile", json={"display_name": "Late edit"})
        key_delete = client.delete("/api/key?provider=anthropic")

        assert profile.status_code == 409
        assert key_delete.status_code == 409

    def test_pending_deletion_hides_jobs_and_import_history_without_hard_delete(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=2)
        saved = _seed_saved_job(fake_supabase, FAKE_OWNER)
        run = _seed_import_run(fake_supabase, FAKE_OWNER)
        result = _seed_import_result(fake_supabase, FAKE_OWNER, run["id"])

        client.post("/api/account/delete", json={"confirm": "DELETE"})

        jobs = client.get("/api/jobs").get_json()
        runs = client.get("/api/jobs/import-runs").get_json()
        results = client.get(f"/api/jobs/import-runs/{run['id']}/results").get_json()
        assert jobs == {"jobs": [], "count": 0}
        assert runs == {"runs": [], "deletion_pending": True}
        assert results == {"results": [], "deletion_pending": True}
        assert saved in fake_supabase.tables["saved_jobs"]
        assert run in fake_supabase.tables["job_import_runs"]
        assert result in fake_supabase.tables["job_import_results"]

    def test_pending_deletion_export_hides_documents_jobs_and_import_history(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=2)
        _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="Hidden CV")
        _seed_saved_job(fake_supabase, FAKE_OWNER, title="Hidden Job")
        run = _seed_import_run(fake_supabase, FAKE_OWNER)
        _seed_import_result(fake_supabase, FAKE_OWNER, run["id"])

        client.post("/api/account/delete", json={"confirm": "DELETE"})
        res = client.get("/api/account/export")

        assert res.status_code == 200
        data = json.loads(res.get_data(as_text=True))
        assert data["deletion_pending"] is True
        assert data["documents"] == []
        assert data["saved_jobs"] == []
        assert data["job_import_runs"] == []
        assert data["job_import_results"] == []
        assert data["application_packets"] == []

    def test_pending_deletion_export_hides_application_packets_without_hard_delete(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=2)
        job = _seed_saved_job(fake_supabase, FAKE_OWNER, title="Hidden Packet Job")
        source = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="Hidden Source")
        packet = _seed_application_packet(
            fake_supabase,
            FAKE_OWNER,
            job_id=job["id"],
            source_cv_id=source["id"],
        )

        client.post("/api/account/delete", json={"confirm": "DELETE"})
        res = client.get("/api/account/export")

        assert res.status_code == 200
        data = json.loads(res.get_data(as_text=True))
        assert data["deletion_pending"] is True
        assert data["application_packets"] == []
        assert packet in fake_supabase.tables["application_packets"]

    def test_cancel_restores_documents(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=2)
        cv = _seed_document(fake_supabase, FAKE_OWNER, doc_type="cv", name="A")

        client.post("/api/account/delete", json={"confirm": "DELETE"})
        # Reading the deleted CV by id is a 404 while pending. (We avoid hitting
        # /api/cvs here so the auto-seed doesn't muddy the restore assertion.)
        assert client.get(f"/api/cv?id={cv['id']}").status_code == 404

        res = client.post("/api/account/cancel-delete")
        assert res.status_code == 200
        assert res.get_json()["cancelled"] is True
        # The original document is restored (readable by id again), flag cleared.
        assert client.get(f"/api/cv?id={cv['id']}").status_code == 200
        prof = next(r for r in fake_supabase.tables["profiles"] if r["id"] == FAKE_OWNER)
        assert prof["deletion_requested_at"] is None


# ===========================================================================
#  5. REAL-JWT cross-tenant + purge cascade (skipped unless provisioned).
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
class TestCrossTenantReal:
    def _client(self):
        import importlib

        server = importlib.import_module("server")
        server.app.config.update(TESTING=True)
        return server.app.test_client()

    def test_b_export_never_contains_a_data(self):
        """B's export must contain only B's own rows — never A's."""
        client = self._client()
        # A creates a uniquely-marked CV.
        marker = "CROSS-TENANT-A-MARKER"
        client.post(
            "/api/cv",
            json={"content": f"name: {marker}\ncontact:\n  - network: mail\n    username: a@x.io\n"
                              "experience:\n  - role: R\n    company: C\n    location: L\n"
                              "    start: '2024'\n    end: Present\n    bullets:\n      - Did it.\n"
                              "    stack: Python\n"},
            headers={"Authorization": f"Bearer {A_TOKEN}"},
        )
        # B exports — A's marker must not be anywhere in it.
        res = client.get("/api/account/export", headers={"Authorization": f"Bearer {B_TOKEN}"})
        assert res.status_code == 200, res.get_data(as_text=True)
        assert marker not in res.get_data(as_text=True), "RLS leak: B's export contained A's data"

    def test_b_delete_only_affects_b(self):
        """B requesting deletion soft-deletes only B's docs; A's stay visible."""
        client = self._client()
        a_marker = "A-SURVIVES-MARKER"
        client.post(
            "/api/cv",
            json={"content": f"name: {a_marker}\ncontact:\n  - network: mail\n    username: a@x.io\n"
                             "experience:\n  - role: R\n    company: C\n    location: L\n"
                             "    start: '2024'\n    end: Present\n    bullets:\n      - Did it.\n"
                             "    stack: Python\n"},
            headers={"Authorization": f"Bearer {A_TOKEN}"},
        )
        # B requests deletion.
        res = client.post(
            "/api/account/delete",
            json={"confirm": "DELETE"},
            headers={"Authorization": f"Bearer {B_TOKEN}"},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        try:
            # A's CV is still there.
            got = client.get("/api/cv", headers={"Authorization": f"Bearer {A_TOKEN}"})
            assert got.status_code == 200
            assert a_marker in got.get_data(as_text=True), "B's deletion wrongly affected A"
        finally:
            # Restore B so the shared test account isn't left scheduled-for-deletion.
            client.post("/api/account/cancel-delete", headers={"Authorization": f"Bearer {B_TOKEN}"})


_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
_SUPA_URL = os.environ.get("SUPABASE_URL")
_HAVE_SERVICE = bool(
    _SERVICE_KEY
    and _SUPA_URL
    and os.environ.get("TEST_ERASURE_PROJECT_ISOLATED") == "1"
)


@pytest.mark.skipif(
    not _HAVE_SERVICE,
    reason=(
        "isolated erasure-project gate not set — requires SUPABASE_URL, "
        "SUPABASE_SERVICE_ROLE_KEY, and TEST_ERASURE_PROJECT_ISOLATED=1"
    ),
)
class TestClaimCascadeReal:
    def test_claim_then_admin_hard_delete_cascades_rows(self):
        """The service role claims a D+30 throwaway, then Auth deletion cascades.

        Fully self-contained: provisions a throwaway confirmed user via the
        admin API, seeds a document as that user, marks deletion in the past,
        claims it, hard-deletes it through the same Auth Admin API used by the
        worker, then asserts owned rows are gone."""
        from supabase import create_client

        admin = create_client(_SUPA_URL, _SERVICE_KEY)
        email = f"purge-test-{uuid.uuid4().hex[:12]}@onepager.test"
        password = "Test-" + uuid.uuid4().hex
        created = admin.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True}
        )
        user_id = created.user.id
        deleted = False
        try:
            # The signup trigger creates the profile row; seed a document and
            # stamp a request far beyond the fixed D+30 deadline.
            admin.table("documents").insert(
                {"owner_id": user_id, "type": "cv", "name": "Doomed", "yaml_blob": "name: X\n"}
            ).execute()
            admin.table("profiles").update(
                {"deletion_requested_at": "2000-01-01T00:00:00Z"}
            ).eq("id", user_id).execute()

            # Sanity: rows exist before the purge.
            docs_before = admin.table("documents").select("id").eq("owner_id", user_id).execute().data
            assert docs_before, "seed document should exist before purge"

            assert (
                admin.rpc(
                    "account_deletion_worker_contract_version",
                    {},
                ).execute().data
                == 2
            )
            claims = admin.rpc(
                "claim_accounts_for_deletion",
                {"p_batch_size": 100, "p_lease_minutes": 30},
            ).execute().data
            claim = next(
                row for row in (claims or []) if row.get("owner_id") == user_id
            )
            assert admin.rpc(
                "account_deletion_claim_is_current",
                {
                    "p_owner_id": user_id,
                    "p_claim_token": claim["claim_token"],
                },
            ).execute().data is True

            admin.auth.admin.delete_user(user_id, should_soft_delete=False)
            deleted = True

            # The auth user is gone (admin lookup raises / returns falsy).
            users = admin.table("profiles").select("id").eq("id", user_id).execute().data
            assert users == [], "profile row should be cascade-deleted with the user"
            docs_after = admin.table("documents").select("id").eq("owner_id", user_id).execute().data
            assert docs_after == [], "documents should be cascade-deleted with the user"
        finally:
            if not deleted:
                # Best-effort cleanup if the claim/delete path did not finish.
                try:
                    admin.auth.admin.delete_user(user_id)
                except Exception:
                    pass
