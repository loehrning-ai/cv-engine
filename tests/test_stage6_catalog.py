"""Stage 6 (plan 002) tests — per-user jobs CATALOG + cross-tenant job_id close.

The MOST security-critical stage. Covers the feature's contract:

  * **catalog metadata**: a job saves with field / deadline / confidence /
    why_fit; the new 5-value status enum + transitions work; the metadata is
    sanitized (confidence → int 0-100 else None, deadline → ISO date else None,
    why_fit length-capped) — malformed advisory values drop, never 500;
  * **has_cover (RLS-safe)**: derived per-user via a grouped, RLS-scoped count
    over the caller's OWN cover-letter documents — user B's has_cover NEVER
    reflects user A's covers; cover_id targets the most-recent cover;
  * **(SECURITY MUST-FIX) cross-tenant job_id is BLOCKED** — at the APP layer
    (`assert_job_owned` → a tailor / cover-letter / generate request stamped with
    a job the caller can't see → 404, nothing written) AND, in the env-gated
    real-JWT class, at the DB layer (the migration-0007 documents WITH CHECK
    rejects an INSERT stamping another tenant's job_id, asserted via the supabase
    client as user B).

The app-layer + validation tests use the in-memory ``fake_supabase`` fixture
from conftest (no network, every LLM call mocked). The DB-layer + route
cross-tenant matrix runs against the REAL Supabase project with real user JWTs
and SKIPS cleanly when the provisioning env vars are absent — same pattern as
Stage 2/4/5/7.
"""
from __future__ import annotations

import os
import sys
import uuid as _uuid
from pathlib import Path

import pytest
import yaml as _yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"

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
        }
    ],
    "skills": [{"label": "Data", "items": "Python, SQL, PyTorch, Spark"}],
}

JD_TEXT = (
    "Title: Senior Data Engineer\n"
    "Company: Acme GmbH\n"
    "You must have experience with Spark and Airflow. 5+ years required.\n"
    "Responsibilities: build ETL pipelines and data models."
)

CLEAN_LETTER = (
    "At Meta I cut pipeline latency by 40% across the ETL stack using Spark "
    "and Airflow, the exact stack this Senior Data Engineer role calls for.\n\n"
    "I would welcome the chance to bring this to Acme GmbH."
)


# ===========================================================================
#  Mock helpers (LLM is never called for real).
# ===========================================================================
def _structured_response() -> str:
    import json

    return json.dumps(
        {
            "fit": {"score": 80, "verdict": "Strong", "target_role": "Senior Data Engineer"},
            "keywords": {"present": [], "missing": [], "inferable": []},
            "suggestions": [],
            "summary": {"recommended": False, "text": "", "rationale": ""},
            "warnings": [],
        }
    )


def _mock_tailor_llm(monkeypatch, raw_text: str):
    import tailor_service as ts

    class _FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key

        def complete(self, *, system, user, model=None, max_tokens=1024, cache_system=True):
            return raw_text

    monkeypatch.setattr(ts, "make_client", lambda provider, api_key: _FakeClient(api_key))


def _mock_letter_llm(monkeypatch, letter_text: str):
    import cover_letter_service as cl

    class _FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key

        def complete(self, *, system, user, model=None, max_tokens=1024, cache_system=True):
            return letter_text

    monkeypatch.setattr(cl, "make_client", lambda provider, api_key: _FakeClient(api_key))


def _seed_cv(store, name="My CV"):
    blob = _yaml.safe_dump(GROUNDED_CV, sort_keys=False, allow_unicode=True)
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
    return doc_id


def _seed_profile(store, owner_id, *, credits=3):
    store.tables.setdefault("profiles", []).append(
        {"id": owner_id, "managed_credits_remaining": credits}
    )


def _seed_job(store, *, owner_id=None, **fields):
    """Insert a saved_jobs row directly into the fake store; return its id."""
    rows = store.tables.setdefault("saved_jobs", [])
    jid = str(_uuid.uuid4())
    row = {
        "id": jid,
        "owner_id": owner_id or store.owner_id,
        "title": "Seeded",
        "company": "Co",
        "jd_text": JD_TEXT,
        "status": "saved",
        "deleted_at": None,
        "updated_at": 1,
    }
    row.update(fields)
    rows.append(row)
    return jid


def _jobs(store):
    return store.tables.get("saved_jobs", [])


def _docs(store):
    return [d for d in store.tables.get("documents", []) if not d.get("deleted_at")]


def _seed_cover(store, *, job_id, owner_id=None, name="Cover"):
    """Insert a cover_letter document linked to job_id; return its id."""
    rows = store.tables.setdefault("documents", [])
    cid = str(_uuid.uuid4())
    rows.append(
        {
            "id": cid,
            "owner_id": owner_id or store.owner_id,
            "type": "cover_letter",
            "name": name,
            "yaml_blob": _yaml.safe_dump({"job_id": job_id, "body": "x"}),
            "job_id": job_id,
            "deleted_at": None,
            "updated_at": 2,
        }
    )
    return cid


# ===========================================================================
#  1. Catalog metadata — new fields persist + come back on the list/detail.
# ===========================================================================
class TestCatalogMetadata:
    def test_create_with_metadata_persists(self, server_module, client, fake_supabase):
        res = client.post(
            "/api/jobs",
            json={
                "jd_text": JD_TEXT,
                "field": "Data / ML",
                "deadline": "2026-09-01",
                "confidence": 82,
                "why_fit": "Spark + Airflow is exactly my stack.",
            },
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        assert body["field"] == "Data / ML"
        assert body["deadline"] == "2026-09-01"
        assert body["confidence"] == 82
        assert body["why_fit"] == "Spark + Airflow is exactly my stack."
        assert body["has_cover"] is False
        assert body["cover_id"] is None
        # Stored on the row.
        row = _jobs(fake_supabase)[0]
        assert row["field"] == "Data / ML"
        assert row["confidence"] == 82

    def test_list_returns_metadata_and_has_cover(self, server_module, client, fake_supabase):
        client.post("/api/jobs", json={"jd_text": JD_TEXT, "field": "ML", "confidence": 50})
        res = client.get("/api/jobs")
        assert res.status_code == 200
        job = res.get_json()["jobs"][0]
        assert job["field"] == "ML"
        assert job["confidence"] == 50
        assert "has_cover" in job and "cover_id" in job

    def test_detail_includes_metadata(self, server_module, client, fake_supabase):
        jid = client.post(
            "/api/jobs", json={"jd_text": JD_TEXT, "field": "ML", "deadline": "2026-08-15"}
        ).get_json()["id"]
        body = client.get(f"/api/jobs/{jid}").get_json()
        assert body["job"]["field"] == "ML"
        assert body["job"]["deadline"] == "2026-08-15"
        assert body["job"]["has_cover"] is False


# ===========================================================================
#  2. Validation — confidence / deadline / why_fit coerce, never 500.
# ===========================================================================
class TestValidation:
    @pytest.mark.parametrize(
        "raw,expected",
        [(0, 0), (100, 100), (55, 55), (150, None), (-5, None), ("70", 70), ("abc", None), (None, None)],
    )
    def test_confidence_coerces_to_0_100(self, server_module, client, fake_supabase, raw, expected):
        body = client.post("/api/jobs", json={"jd_text": JD_TEXT, "confidence": raw}).get_json()
        assert body["confidence"] == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [("2026-09-01", "2026-09-01"), ("not-a-date", None), ("2026-13-99", None), ("", None)],
    )
    def test_deadline_validates_iso(self, server_module, client, fake_supabase, raw, expected):
        body = client.post("/api/jobs", json={"jd_text": JD_TEXT, "deadline": raw}).get_json()
        assert body["deadline"] == expected

    def test_why_fit_is_length_capped(self, server_module, client, fake_supabase):
        long = "x" * 5000
        body = client.post("/api/jobs", json={"jd_text": JD_TEXT, "why_fit": long}).get_json()
        assert len(body["why_fit"]) == 2000  # MAX_WHY_FIT_LEN

    def test_garbage_metadata_does_not_500(self, server_module, client, fake_supabase):
        # field as a number, confidence as a list, deadline as a dict — all drop.
        res = client.post(
            "/api/jobs",
            json={"jd_text": JD_TEXT, "field": 123, "confidence": [1], "deadline": {"a": 1}},
        )
        assert res.status_code == 201
        body = res.get_json()
        assert body["field"] == "" and body["confidence"] is None and body["deadline"] is None


# ===========================================================================
#  3. Status enum + transitions (the widened 5-value set).
# ===========================================================================
class TestStatusEnum:
    def test_job_statuses_constant_is_five_values(self):
        import jobs_service as js

        assert js.JOB_STATUSES == ("saved", "open", "speculative", "applied", "closed")

    @pytest.mark.parametrize("status", ["open", "speculative", "applied", "closed", "saved"])
    def test_transition_to_each_status(self, server_module, client, fake_supabase, status):
        jid = client.post("/api/jobs", json={"jd_text": JD_TEXT}).get_json()["id"]
        res = client.post(f"/api/jobs/{jid}/status", json={"status": status})
        assert res.status_code == 200, res.get_data(as_text=True)
        assert res.get_json()["status"] == status
        assert _jobs(fake_supabase)[0]["status"] == status

    def test_garbage_status_rejected(self, server_module, client, fake_supabase):
        jid = client.post("/api/jobs", json={"jd_text": JD_TEXT}).get_json()["id"]
        res = client.post(f"/api/jobs/{jid}/status", json={"status": "ghosted"})
        assert res.status_code == 400
        assert res.get_json()["code"] == "bad_status"

    def test_patch_edits_metadata(self, server_module, client, fake_supabase):
        jid = client.post("/api/jobs", json={"jd_text": JD_TEXT}).get_json()["id"]
        res = client.patch(
            f"/api/jobs/{jid}",
            json={"field": "Platform", "confidence": 90, "deadline": "2026-10-01", "status": "open"},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["field"] == "Platform"
        assert body["confidence"] == 90
        assert body["deadline"] == "2026-10-01"
        assert body["status"] == "open"

    def test_patch_empty_is_400(self, server_module, client, fake_supabase):
        jid = client.post("/api/jobs", json={"jd_text": JD_TEXT}).get_json()["id"]
        assert client.patch(f"/api/jobs/{jid}", json={}).status_code == 400

    def test_patch_unknown_job_is_404(self, server_module, client, fake_supabase):
        assert client.patch(f"/api/jobs/{_uuid.uuid4()}", json={"field": "x"}).status_code == 404


# ===========================================================================
#  4. has_cover derivation is per-user correct (the RLS-safe count).
# ===========================================================================
class TestHasCover:
    def test_has_cover_true_with_linked_cover(self, server_module, client, fake_supabase):
        jid = _seed_job(fake_supabase)
        cid = _seed_cover(fake_supabase, job_id=jid)
        res = client.get("/api/jobs").get_json()
        job = next(j for j in res["jobs"] if j["id"] == jid)
        assert job["has_cover"] is True
        assert job["cover_id"] == cid

    def test_has_cover_false_without_cover(self, server_module, client, fake_supabase):
        jid = _seed_job(fake_supabase)
        job = next(j for j in client.get("/api/jobs").get_json()["jobs"] if j["id"] == jid)
        assert job["has_cover"] is False
        assert job["cover_id"] is None

    def test_deleted_cover_does_not_count(self, server_module, client, fake_supabase):
        jid = _seed_job(fake_supabase)
        cid = _seed_cover(fake_supabase, job_id=jid)
        # Soft-delete the cover → it must no longer count.
        next(d for d in fake_supabase.tables["documents"] if d["id"] == cid)["deleted_at"] = 9
        job = next(j for j in client.get("/api/jobs").get_json()["jobs"] if j["id"] == jid)
        assert job["has_cover"] is False

    def test_has_cover_is_grouped_not_n_plus_1(self, server_module, client, fake_supabase, monkeypatch):
        """The list derives has_cover with ONE documents query (no N+1)."""
        for _ in range(3):
            _seed_job(fake_supabase)
        # Count documents-table SELECTs while listing.
        real_table = fake_supabase.table
        seen = {"doc_selects": 0}

        def _counting_table(name):
            t = real_table(name)
            if name == "documents":
                seen["doc_selects"] += 1
            return t

        monkeypatch.setattr(fake_supabase, "table", _counting_table)
        client.get("/api/jobs")
        assert seen["doc_selects"] == 1, "has_cover must be one grouped query, not per-job"


# ===========================================================================
#  5. (SECURITY MUST-FIX) Cross-tenant job_id is BLOCKED at the APP layer.
#  A request stamped with a job the caller can't see → 404, nothing written.
#  Uses the fake store (the "invisible job" == not in this tenant's view).
# ===========================================================================
class TestCrossTenantAppLayer:
    def test_cover_letter_with_invisible_job_id_is_404_nothing_written(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER)
        # A job id this tenant does NOT own/see (never inserted into the store).
        foreign_job = str(_uuid.uuid4())
        res = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": cv_id, "jd_text": JD_TEXT, "job_id": foreign_job},
        )
        assert res.status_code == 404, res.get_data(as_text=True)
        assert res.get_json().get("code") == "job_not_found"
        # NOTHING was written — no cover-letter document, no credit spent.
        assert [d for d in _docs(fake_supabase) if d["type"] == "cover_letter"] == []
        prof = next(p for p in fake_supabase.tables["profiles"] if p["id"] == FAKE_OWNER)
        assert prof["managed_credits_remaining"] == 3  # untouched

    def test_tailor_with_invisible_job_id_is_404_nothing_written(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_tailor_llm(monkeypatch, _structured_response())
        cv_id = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER)
        foreign_job = str(_uuid.uuid4())
        before = len(_docs(fake_supabase))
        res = client.post(
            f"/api/cv/{cv_id}/tailor",
            json={"jd_text": JD_TEXT, "job_id": foreign_job},
        )
        assert res.status_code == 404, res.get_data(as_text=True)
        # No tailored CV was created.
        assert len(_docs(fake_supabase)) == before

    def test_owned_job_id_still_works(self, server_module, client, fake_supabase, monkeypatch):
        """Sanity: a job the caller DOES own stamps fine (no over-block)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_letter_llm(monkeypatch, CLEAN_LETTER)
        cv_id = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER)
        jid = _seed_job(fake_supabase)  # owned by this tenant
        res = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": cv_id, "jd_text": JD_TEXT, "job_id": jid},
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        letter = next(d for d in _docs(fake_supabase) if d["type"] == "cover_letter")
        assert letter["job_id"] == jid

    def test_assert_job_owned_helper(self, server_module, fake_supabase):
        """The helper: None ok; owned ok; unknown → JobError 404."""
        import jobs_service as js

        js.assert_job_owned(None)  # no-op
        jid = _seed_job(fake_supabase)
        js.assert_job_owned(jid)  # owned → ok
        with pytest.raises(js.JobError) as ei:
            js.assert_job_owned(str(_uuid.uuid4()))
        assert ei.value.status == 404


# ===========================================================================
#  6. Cross-tenant matrix against the REAL project + the DB WITH CHECK.
#  SKIPS cleanly without the provisioning env vars (same as Stage 2/4/5/7).
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
class TestCrossTenantDbLayer:
    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_b_cannot_stamp_a_job_id_app_and_db(self):
        """A creates a job. B must NOT be able to stamp A's job_id on a document:
        (1) via the app route → 404 (assert_job_owned), nothing written;
        (2) directly via the supabase client as B → the migration-0007 documents
            WITH CHECK rejects the INSERT (row-level security violation)."""
        import importlib

        from postgrest.exceptions import APIError
        from supabase import create_client

        server = importlib.import_module("server")
        server.app.config.update(TESTING=True)
        client = server.app.test_client()

        url = os.environ["SUPABASE_URL"]
        pub = os.environ["SUPABASE_PUBLISHABLE_KEY"]

        sb_a = create_client(url, pub)
        sb_a.postgrest.auth(A_TOKEN)
        sb_b = create_client(url, pub)
        sb_b.postgrest.auth(B_TOKEN)

        # A creates a job + owns a CV; B owns a CV too (for the app-route attempt).
        a_job_id = (
            sb_a.table("saved_jobs")
            .insert({"title": "A-secret-job", "company": "ACorp", "jd_text": JD_TEXT, "status": "open"})
            .execute()
            .data[0]["id"]
        )
        b_cv_id = (
            sb_b.table("documents")
            .insert({"type": "cv", "name": "B-cv", "yaml_blob": _yaml.safe_dump(GROUNDED_CV, sort_keys=False)})
            .execute()
            .data[0]["id"]
        )
        try:
            # (1) APP layer: B tries to generate a cover letter stamped with A's
            #     job → 404 at the ownership check; no document is written.
            r = client.post(
                "/api/cover-letter/generate",
                json={"cv_id": b_cv_id, "jd_text": JD_TEXT, "job_id": a_job_id},
                headers=self._auth(B_TOKEN),
            )
            assert r.status_code == 404, r.get_data(as_text=True)
            b_letters = (
                sb_b.table("documents").select("id").eq("type", "cover_letter").execute().data
            )
            assert not (b_letters or []), "app layer leak: B wrote a letter stamped with A's job"

            # (2) DB layer: B tries to INSERT a document stamped with A's job_id
            #     DIRECTLY (bypassing the app) → the WITH CHECK must reject it.
            blocked = False
            try:
                sb_b.table("documents").insert(
                    {"type": "cover_letter", "name": "stolen", "yaml_blob": "x", "job_id": a_job_id}
                ).execute()
            except APIError:
                blocked = True
            assert blocked, "DB leak: documents WITH CHECK allowed B to stamp A's job_id"

            # And nothing references A's job from B.
            stamped = sb_a.table("documents").select("id").eq("job_id", a_job_id).execute().data
            assert not (stamped or []), "DB leak: a document is stamped with A's job_id"

            # B CAN still insert an unlinked document (no over-block).
            ok = (
                sb_b.table("documents")
                .insert({"type": "cover_letter", "name": "B-own", "yaml_blob": "x", "job_id": None})
                .execute()
                .data
            )
            assert ok, "over-block: B cannot insert an unlinked document"
        finally:
            sb_b.table("documents").delete().eq("owner_id", B_ID).execute()
            sb_a.table("saved_jobs").delete().eq("id", a_job_id).execute()

    def test_b_has_cover_never_reflects_a(self):
        """A has a job + a cover letter for it (has_cover=True for A). B saves a
        job too; B's /api/jobs must show has_cover=False for B's job and must NOT
        see A's job at all (RLS)."""
        import importlib

        from supabase import create_client

        server = importlib.import_module("server")
        server.app.config.update(TESTING=True)
        client = server.app.test_client()

        url = os.environ["SUPABASE_URL"]
        pub = os.environ["SUPABASE_PUBLISHABLE_KEY"]
        sb_a = create_client(url, pub)
        sb_a.postgrest.auth(A_TOKEN)

        a_job_id = (
            sb_a.table("saved_jobs")
            .insert({"title": "A-covered", "company": "ACorp", "jd_text": JD_TEXT, "status": "open"})
            .execute()
            .data[0]["id"]
        )
        # A stamps a cover letter on A's own job (allowed — A owns it).
        a_cover_id = (
            sb_a.table("documents")
            .insert(
                {
                    "type": "cover_letter",
                    "name": "A-cover",
                    "yaml_blob": _yaml.safe_dump({"job_id": a_job_id, "body": "x"}),
                    "job_id": a_job_id,
                }
            )
            .execute()
            .data[0]["id"]
        )
        b_job_id = None
        try:
            # A's own list: the job is covered.
            a_list = client.get("/api/jobs", headers=self._auth(A_TOKEN)).get_json()
            a_row = next((j for j in a_list["jobs"] if j["id"] == a_job_id), None)
            assert a_row is not None and a_row["has_cover"] is True
            assert a_row["cover_id"] == a_cover_id

            # B saves a job; B's list must NOT contain A's job and B's job is
            # has_cover=False (A's cover can never leak into B's derivation).
            b_job_id = (
                client.post("/api/jobs", json={"jd_text": JD_TEXT}, headers=self._auth(B_TOKEN))
                .get_json()["id"]
            )
            b_list = client.get("/api/jobs", headers=self._auth(B_TOKEN)).get_json()
            ids = {j["id"] for j in b_list["jobs"]}
            assert a_job_id not in ids, "RLS leak: B saw A's job"
            b_row = next(j for j in b_list["jobs"] if j["id"] == b_job_id)
            assert b_row["has_cover"] is False, "has_cover leak: B reflected A's cover"
        finally:
            sb_a.table("documents").delete().eq("id", a_cover_id).execute()
            sb_a.table("saved_jobs").delete().eq("id", a_job_id).execute()
            if b_job_id:
                sb_a_admin = create_client(url, pub)
                sb_a_admin.postgrest.auth(B_TOKEN)
                sb_a_admin.table("saved_jobs").delete().eq("id", b_job_id).execute()
