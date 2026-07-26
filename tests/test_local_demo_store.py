"""Plan 107 Stage 2 — local demo jobs storage + honest storage errors.

F14: in the shipped local demo configuration (``ONEPAGER_DEMO_MODE`` on, no
reachable Supabase) every jobs WRITE used to 500 with Flask's HTML error page
because ``auth.user_supabase()`` raised (or connected to a dead host) outside
any JSON error handler. These tests pin the two-part fix:

  * demo-owner requests route job storage to the in-memory
    ``local_demo_store`` (full CRUD + import save-selected roundtrip works
    through the REAL ``/auth/demo-login`` token path), and
  * a storage-configuration failure on any other path returns the JSON
    ``storage_unavailable`` contract instead of HTML (AuthError handler).

F17: ``create_job`` stamps ``source_metadata.full_jd_status = "reviewed"`` at
write time because the jobs LIST select excludes ``jd_text`` — without the
stamp, ``_source_state``'s presence fallback calls manual jobs "missing" in
list/card shapes while the detail shape says "reviewed".

F2: ``/api/jobs/sources/status`` exposes a boolean ``url_fetch.enabled``
capability (never the allowlist contents) so Tailor can hide its otherwise
dead URL-fetch field.
"""
from __future__ import annotations

import time
import uuid

import pytest

JD = (
    "Senior Data Engineer role. Build ETL pipelines with Python and SQL, own "
    "Airflow DAGs, ship dbt models, 5+ years experience, Spark, Kafka, AWS. "
) * 4


@pytest.fixture
def demo_client(server_module, monkeypatch):
    """A test client running the REAL local-demo auth + storage path."""
    import local_demo_store

    monkeypatch.setenv("ONEPAGER_DEMO_MODE", "true")
    local_demo_store.reset()
    server_module.app.config.update(TESTING=True)
    client = server_module.app.test_client()
    token = client.post("/auth/demo-login").get_json()["access_token"]
    yield client, {"Authorization": f"Bearer {token}"}
    local_demo_store.reset()


class TestLocalDemoJobsStore:
    def test_demo_job_write_read_star_status_delete_roundtrip(self, demo_client):
        client, headers = demo_client

        created = client.post(
            "/api/jobs",
            json={"title": "Probe A", "company": "ProbeCorp", "jd_text": JD},
            headers=headers,
        )
        assert created.status_code == 201, created.get_data(as_text=True)[:200]
        job = created.get_json()
        job_id = job["id"]

        listed = client.get("/api/jobs", headers=headers).get_json()
        assert listed["count"] == 1
        assert listed["jobs"][0]["id"] == job_id

        starred = client.patch(f"/api/jobs/{job_id}", json={"starred": True}, headers=headers)
        assert starred.status_code == 200
        assert starred.get_json()["starred"] is True

        bad = client.patch(f"/api/jobs/{job_id}", json={"starred": "nope"}, headers=headers)
        assert bad.status_code == 400
        assert bad.get_json()["code"] == "bad_starred"

        status = client.post(f"/api/jobs/{job_id}/status", json={"status": "applied"}, headers=headers)
        assert status.status_code == 200

        detail = client.get(f"/api/jobs/{job_id}", headers=headers).get_json()
        detail_job = detail.get("job") or detail
        assert detail_job["starred"] is True
        assert detail_job["status"] == "applied"

        deleted = client.delete(f"/api/jobs/{job_id}", headers=headers)
        assert deleted.status_code == 200
        assert client.get("/api/jobs", headers=headers).get_json()["count"] == 0

    def test_demo_import_run_save_selected_persists_job(self, demo_client, monkeypatch):
        client, headers = demo_client
        posting = {
            "title": "Imported Engineer",
            "company": "ImportCorp",
            "jd_text": "Truncated preview text only — full JD pending review.",
            "location": "Warsaw, Poland",
        }
        run = client.post(
            "/api/jobs/import-runs",
            json={"provider": "manual", "query": "engineer", "postings": [posting]},
            headers=headers,
        )
        assert run.status_code == 202, run.get_data(as_text=True)[:200]
        body = run.get_json()
        assert body.get("results"), body
        saved = client.post(
            f"/api/jobs/import-runs/{body['id']}/save-selected",
            json={"result_ids": [body["results"][0]["id"]]},
            headers=headers,
        )
        assert saved.status_code == 200, saved.get_data(as_text=True)[:200]
        jobs = client.get("/api/jobs", headers=headers).get_json()["jobs"]
        assert any(j["title"] == "Imported Engineer" for j in jobs)

    def test_demo_job_linked_cover_letter_generation_uses_local_cv_store(self, demo_client):
        client, headers = demo_client
        cv_yaml = """
name: Tim Loehr
contact:
  - network: mail
    username: tim@example.com
experience:
  - role: Data Engineer
    company: Meta
    start: 2025
    end: Present
    bullets:
      - Built data systems and pipelines.
skills:
  - label: Data
    items: Python, SQL, Spark
"""
        saved_cv = client.post("/api/cv", json={"content": cv_yaml}, headers=headers)
        assert saved_cv.status_code == 200, saved_cv.get_data(as_text=True)[:200]
        cv_id = saved_cv.get_json()["id"]
        created_job = client.post(
            "/api/jobs",
            json={
                "title": "Research Engineer, Data Infrastructure",
                "company": "Mistral AI",
                "jd_text": (
                    "Research Engineer, Data Infrastructure role at Mistral AI. "
                    "Build Python, SQL, Spark, Kubernetes, and cloud data systems. "
                    "4+ years experience."
                ),
            },
            headers=headers,
        )
        assert created_job.status_code == 201, created_job.get_data(as_text=True)[:200]
        job_id = created_job.get_json()["id"]

        generated = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": cv_id, "job_id": job_id, "tone": "direct"},
            headers=headers,
        )
        assert generated.status_code == 201, generated.get_data(as_text=True)[:200]
        body = generated.get_json()
        assert body["id"]
        assert body["company"] == "Mistral AI"

        detail = client.get(f"/api/jobs/{job_id}", headers=headers)
        assert detail.status_code == 200
        assert any(row["id"] == body["id"] for row in detail.get_json()["cover_letters"])

    def test_demo_store_never_activates_for_non_demo_owner(self, server_module, client, bypass_auth):
        import local_demo_store

        with server_module.app.test_request_context("/api/jobs"):
            from flask import g

            g.owner_id = bypass_auth
            assert local_demo_store.active() is False

    def test_demo_cover_letter_documents_use_local_store(self, demo_client, server_module):
        import auth as auth_mod
        import cover_letter_service as cl_service
        import local_demo_store
        from flask import g

        with server_module.app.test_request_context("/api/cover-letter"):
            g.owner_id = auth_mod.LOCAL_DEMO_OWNER_ID
            row = cl_service.insert_document("cover_letter", "Demo Letter", "body: Hello\n")
            assert row["id"]
            assert cl_service.document_by_id(row["id"], "cover_letter")["name"] == "Demo Letter"
            assert cl_service.update_document_blob(row["id"], "cover_letter", "body: Updated\n", now_iso="2026-07-07T10:00:00Z")
            assert cl_service.rename_document(row["id"], "cover_letter", "Renamed", now_iso="2026-07-07T10:01:00Z")
            listed = cl_service.list_documents("cover_letter")
            assert listed[0]["name"] == "Renamed"
            assert cl_service.delete_document(row["id"], "cover_letter")
            assert local_demo_store.db().tables["documents"] == []

    def test_demo_tailor_review_state_uses_local_store(self, demo_client, server_module, monkeypatch):
        import auth as auth_mod
        import local_demo_store
        import tailor_service as tailor_svc
        from flask import g

        monkeypatch.delenv("ONEPAGER_REVIEW_TOKEN_SECRET", raising=False)
        with server_module.app.test_request_context("/api/cv/demo/tailor/review"):
            g.owner_id = auth_mod.LOCAL_DEMO_OWNER_ID
            review_id = str(uuid.uuid4())
            state = {
                "v": 1,
                "review_id": review_id,
                "owner_id": auth_mod.LOCAL_DEMO_OWNER_ID,
                "cv_id": str(uuid.uuid4()),
                "job_id": None,
                "source_hash": "a" * 64,
                "source_revision": "1",
                "jd_hash": "b" * 64,
                "tailored_hash": "c" * 64,
                "tailored_name": "Demo tailored",
                "tailored_yaml": "name: Demo\n",
                "applied": [],
                "warnings": [],
                "ats": {},
                "provider": "stub",
                "model": "stub",
                "used_managed_credit": False,
                "expires_at": int(time.time()) + 600,
            }
            token = tailor_svc._sign_review_state(state)
            loaded = tailor_svc._verify_review_token(token)
            assert loaded["tailored_yaml"] == "name: Demo\n"
            assert len(local_demo_store.db().tables["tailor_review_states"]) == 1
            tailor_svc._claim_review_apply(review_id, digest=loaded["_review_state_digest"], now_iso="2026-07-07T10:00:00Z")
            assert local_demo_store.db().tables["tailor_review_states"][0]["applied_at"] == "2026-07-07T10:00:00Z"
            tailor_svc.release_review_apply(review_id)
            assert local_demo_store.db().tables["tailor_review_states"][0]["applied_at"] is None

    def test_demo_apply_attempts_and_packet_confirmations_use_local_store(self, demo_client, server_module):
        import apply_service
        import auth as auth_mod
        import local_demo_store
        from flask import g

        with server_module.app.test_request_context("/api/jobs/demo/apply/open"):
            g.owner_id = auth_mod.LOCAL_DEMO_OWNER_ID
            packet_id = str(uuid.uuid4())
            local_demo_store.db().tables.setdefault("application_packets", []).append(
                {
                    "id": packet_id,
                    "owner_id": auth_mod.LOCAL_DEMO_OWNER_ID,
                    "profile_answers_complete": False,
                    "legal_answers_confirmed": False,
                    "visa_answers_confirmed": False,
                }
            )
            attempt = apply_service._record_attempt(
                job_id=str(uuid.uuid4()),
                packet_id=packet_id,
                action="manual_open",
                status="blocked",
                reason_code="probe",
                target={"url": "https://jobs.example.com/apply", "host": "jobs.example.com", "classification": "unknown"},
                readiness={"ready": False, "blockers": [{"code": "probe"}]},
                confirmations={},
                now_iso="2026-07-07T10:00:00Z",
            )
            assert attempt["status"] == "blocked"
            apply_service._persist_packet_confirmations(
                packet_id,
                {
                    "profile_answers_complete": True,
                    "legal_answers_confirmed": True,
                    "visa_answers_confirmed": True,
                },
                now_iso="2026-07-07T10:01:00Z",
            )
            packet = local_demo_store.db().tables["application_packets"][0]
            assert packet["profile_answers_complete"] is True
            assert packet["legal_answers_confirmed"] is True
            assert packet["visa_answers_confirmed"] is True


class TestDemoKeyWriteSeam:
    """Follow-up: the BYO-key WRITE path (POST/DELETE /api/key) bypassed the demo
    seam, so a demo owner disconnecting a key (or connecting a valid one) hit the
    unreachable Supabase → httpx.ConnectError → HTML 500 (hard rule 5). The write
    functions now short-circuit for the demo owner."""

    def test_demo_delete_key_does_not_500(self, demo_client):
        client, headers = demo_client
        res = client.delete("/api/key?provider=anthropic", headers=headers)
        assert res.status_code == 200, res.get_data(as_text=True)[:200]
        assert res.get_json().get("deleted") is False  # nothing durably stored

    def test_demo_save_key_short_circuits_without_supabase(self, server_module):
        import local_demo_store
        import llm_gateway
        # Directly exercise the seam guard: with the demo store active, save_key
        # refuses with a typed 400 (keys are not stored in demo mode) without
        # touching user_supabase (which would 500).
        with server_module.app.test_request_context():
            from flask import g
            import auth as auth_mod
            g.owner_id = auth_mod.LOCAL_DEMO_OWNER_ID
            import os
            os.environ["ONEPAGER_DEMO_MODE"] = "true"
            assert local_demo_store.active() is True
            with pytest.raises(llm_gateway.LLMUnavailable) as excinfo:
                llm_gateway.save_key(auth_mod.LOCAL_DEMO_OWNER_ID, "anthropic", "sk-ant-demo-abcd1234")
            assert excinfo.value.status == 400
            assert excinfo.value.payload["code"] == "demo_mode_key_not_stored"


class TestStorageErrorContract:
    def test_unconfigured_storage_write_returns_json_not_html(
        self, server_module, client, bypass_auth, monkeypatch
    ):
        import auth as auth_mod
        import jobs_service as jobs_svc

        def _raise():
            raise auth_mod.AuthError(
                "supabase not configured: SUPABASE_URL/SUPABASE_PUBLISHABLE_KEY missing",
                status=500,
            )

        monkeypatch.setattr(jobs_svc.auth_mod, "user_supabase", _raise)
        res = client.post("/api/jobs", json={"title": "X", "jd_text": JD})
        assert res.status_code == 500
        assert res.content_type.startswith("application/json")
        assert res.get_json()["code"] == "storage_unavailable"


class TestFullJdStampAtCreate:
    def test_create_job_stamps_reviewed_full_jd_metadata(self, client, fake_supabase):
        res = client.post("/api/jobs", json={"title": "Manual", "jd_text": JD})
        assert res.status_code == 201
        row = fake_supabase.tables["saved_jobs"][0]
        assert row["source_metadata"]["full_jd_status"] == "reviewed"

    def test_list_shape_without_jd_text_still_reports_reviewed(self, server_module):
        import jobs_service as jobs_svc

        list_row = {"source_metadata": {"full_jd_status": "reviewed"}}
        state = jobs_svc._source_state(list_row)
        assert state["full_jd_status"] == "reviewed"
        assert state["requires_full_jd_review"] is False

        unstamped = jobs_svc._source_state({"source_metadata": {}})
        assert unstamped["full_jd_status"] == "missing"


class TestUrlFetchCapability:
    def test_sources_status_reports_url_fetch_disabled_by_default(
        self, client, bypass_auth, monkeypatch
    ):
        monkeypatch.delenv("ONEPAGER_JOB_URL_ALLOWLIST", raising=False)
        data = client.get("/api/jobs/sources/status").get_json()
        assert data["url_fetch"] == {"enabled": False}

    def test_sources_status_reports_url_fetch_enabled_with_allowlist(
        self, client, bypass_auth, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_JOB_URL_ALLOWLIST", "jobs.example.com")
        data = client.get("/api/jobs/sources/status").get_json()
        assert data["url_fetch"]["enabled"] is True
        # Capability only — the allowlist contents must never be exposed.
        assert "jobs.example.com" not in str(data)


class TestDemoScoreSourceCv:
    def test_ats_score_with_saved_job_and_demo_cv_returns_deterministic_score(self, demo_client):
        """F19: scoring a saved job against the seeded demo CV must not 500 —
        the service layer resolves the demo CV through the registered
        provider instead of querying an unreachable Supabase."""
        client, headers = demo_client
        job = client.post(
            "/api/jobs",
            json={"title": "Probe", "company": "Probe", "jd_text": JD},
            headers=headers,
        ).get_json()
        res = client.post(
            "/api/ats-score",
            json={
                "cv_id": "00000000-0000-4000-8000-000000000001",
                "job_id": job["id"],
                "profile": "keyword_screen",
            },
            headers=headers,
        )
        assert res.status_code == 200, res.get_data(as_text=True)[:200]
        body = res.get_json()
        assert isinstance(body.get("score"), int)
        assert body.get("source") == "deterministic"
