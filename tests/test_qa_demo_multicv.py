"""QA round 2 — demo-mode multi-CV writes, GDPR export, and apply-body 500s.

Round-2 review findings against the local demo shell (``ONEPAGER_DEMO_MODE``
on, no reachable Supabase, in-memory ``local_demo_store``):

  1. [P1] Multi-CV write ops (POST /api/cvs, rename, duplicate, delete,
     DELETE /api/cv/<id>) called ``auth.user_supabase()`` directly → HTML 500
     for the demo owner. They now route through the same demo seam
     ``account_service._sb`` uses, and the seeded primary demo CV lives in the
     demo store's ``documents`` table so created CVs and the primary CV share
     one model. Foreign/nonexistent ids → 404, never 500.
  2. [P2] GDPR export omitted the seeded primary demo CV (it lived in a
     separate route-layer dict). The export now materializes it into the demo
     documents table first, so ``documents`` covers ALL demo documents with
     their full ``yaml_blob``.
  3. [P3] An explicit foreign CV id silently served the bundled demo CV on
     read/preview/build. Explicit ids that don't resolve now 404 like
     production; the no-id bundled-CV fallback still works.
  4. [P3] Demo account-deletion dead-end: cancel-delete always returned
     ``{cancelled: false}`` so the pending panel's Cancel button could never
     dismiss it. Demo cancel now returns ``cancelled: True`` (the pending
     state is purely client-side in the demo shell — nothing is ever written).
  5. [P2] Apply action routes 500'd on truthy non-dict JSON bodies
     (``['x']``, ``true``, …) via ``payload.get`` → AttributeError. Non-dict
     bodies now coerce to ``{}`` (fixed for both demo and production).

The demo tests run through the REAL ``/auth/demo-login`` token path; the
production-parity tests use the in-memory ``fake_supabase`` fixture.
"""
from __future__ import annotations

import uuid

import pytest

JD = (
    "Senior Data Engineer role. Build ETL pipelines with Python and SQL, own "
    "Airflow DAGs, ship dbt models, 5+ years experience, Spark, Kafka, AWS. "
) * 4

CV_YAML = """
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


def _create_cv(client, headers, name="Second CV") -> str:
    res = client.post("/api/cvs", json={"name": name}, headers=headers)
    assert res.status_code == 201, res.get_data(as_text=True)[:200]
    return res.get_json()["id"]


class TestDemoMultiCvCrud:
    """Finding 1: multi-CV write ops must not 500 in the demo shell."""

    def test_create_cv_returns_201_and_appears_in_list(self, demo_client, server_module):
        client, headers = demo_client
        new_id = _create_cv(client, headers)
        assert uuid.UUID(new_id)  # well-formed document id

        listed = client.get("/api/cvs", headers=headers).get_json()
        ids = [cv["id"] for cv in listed["cvs"]]
        assert listed["count"] == 2
        assert server_module.LOCAL_DEMO_CV_ID in ids  # seeded primary survives
        assert new_id in ids

    def test_rename_created_cv(self, demo_client):
        client, headers = demo_client
        new_id = _create_cv(client, headers)

        res = client.post(f"/api/cv/{new_id}/rename", json={"name": "Renamed CV"}, headers=headers)

        assert res.status_code == 200, res.get_data(as_text=True)[:200]
        assert res.get_json() == {"id": new_id, "name": "Renamed CV"}
        names = [cv["name"] for cv in client.get("/api/cvs", headers=headers).get_json()["cvs"]]
        assert "Renamed CV" in names

    def test_duplicate_created_cv(self, demo_client):
        client, headers = demo_client
        new_id = _create_cv(client, headers)

        res = client.post(f"/api/cv/{new_id}/duplicate", headers=headers)

        assert res.status_code == 201, res.get_data(as_text=True)[:200]
        body = res.get_json()
        assert body["id"] != new_id
        assert body["name"] == "Copy of Second CV"
        assert client.get("/api/cvs", headers=headers).get_json()["count"] == 3

    def test_delete_created_cv(self, demo_client, server_module):
        client, headers = demo_client
        new_id = _create_cv(client, headers)

        res = client.delete(f"/api/cv/{new_id}", headers=headers)

        assert res.status_code == 200, res.get_data(as_text=True)[:200]
        assert res.get_json() == {"deleted": new_id}
        ids = [cv["id"] for cv in client.get("/api/cvs", headers=headers).get_json()["cvs"]]
        assert new_id not in ids
        assert server_module.LOCAL_DEMO_CV_ID in ids

    def test_demo_mutations_never_reach_derived_pdf_storage(
        self, demo_client, monkeypatch
    ):
        """The fail-closed artifact cleanup is exempt in the demo shell.

        Demo documents live in server-process memory and never reach Supabase
        or R2, so no ``pdf_artifacts`` row and no stored object can exist for
        the demo owner. Sending the demo's ``alg:none`` token at a real project
        would fail every demo mutation closed over an artifact that cannot
        exist. Any call into ``pdf_store`` from a demo mutation is the bug this
        pins.
        """
        import pdf_store

        def _never(*_args, **_kwargs):
            raise AssertionError("demo mutation reached derived PDF storage")

        # Tripwire every pdf_store entry point a demo mutation could actually
        # reach. `delete_owner_artifacts` used to be pinned here but had no
        # call sites left, so that half of the guard asserted nothing while
        # looking like coverage.
        for entry_point in (
            "delete_document_artifacts",
            "delete_detached_artifacts",
            "delete_owner_cv_artifacts",
            "retry_pending_artifact_cleanup",
            "pending_artifact_cleanup_count",
            "update_document_with_artifact_detach",
        ):
            assert hasattr(pdf_store, entry_point), entry_point
            monkeypatch.setattr(pdf_store, entry_point, _never)
        client, headers = demo_client
        new_id = _create_cv(client, headers)

        saved = client.post(
            "/api/cv", json={"id": new_id, "content": CV_YAML}, headers=headers
        )
        deleted = client.delete(f"/api/cv/{new_id}", headers=headers)
        erased = client.post(
            "/api/account/delete", json={"confirm": "DELETE"}, headers=headers
        )

        assert saved.status_code == 200, saved.get_data(as_text=True)[:200]
        assert deleted.status_code == 200, deleted.get_data(as_text=True)[:200]
        assert erased.status_code == 200, erased.get_data(as_text=True)[:200]

    def test_save_created_cv_yaml_roundtrip(self, demo_client):
        """A CV created via POST /api/cvs must accept edits — the save path
        uses the same demo seam, otherwise the first edit after "New CV" 500s."""
        client, headers = demo_client
        new_id = _create_cv(client, headers)

        saved = client.post(f"/api/cv?id={new_id}", json={"content": CV_YAML}, headers=headers)
        assert saved.status_code == 200, saved.get_data(as_text=True)[:200]
        assert saved.get_json()["saved"] is True

        loaded = client.get(f"/api/cv?id={new_id}", headers=headers)
        assert loaded.status_code == 200
        assert "Tim Loehr" in loaded.get_data(as_text=True)

    def test_primary_demo_cv_rename_and_duplicate(self, demo_client, server_module):
        """The seeded primary CV is a first-class document: rename/duplicate/
        delete of ``LOCAL_DEMO_CV_ID`` work like any created demo CV."""
        client, headers = demo_client
        primary = server_module.LOCAL_DEMO_CV_ID

        renamed = client.post(f"/api/cv/{primary}/rename", json={"name": "My Demo CV"}, headers=headers)
        assert renamed.status_code == 200, renamed.get_data(as_text=True)[:200]

        duplicated = client.post(f"/api/cv/{primary}/duplicate", headers=headers)
        assert duplicated.status_code == 201, duplicated.get_data(as_text=True)[:200]
        assert duplicated.get_json()["name"] == "Copy of My Demo CV"

        deleted = client.delete(f"/api/cv/{primary}", headers=headers)
        assert deleted.status_code == 200, deleted.get_data(as_text=True)[:200]

    def test_foreign_ids_404_never_500(self, demo_client):
        """Findings 1+2: a foreign/nonexistent id must 404 (production
        semantics), not 500 — on every multi-CV write op."""
        client, headers = demo_client
        foreign = str(uuid.uuid4())

        rename = client.post(f"/api/cv/{foreign}/rename", json={"name": "X"}, headers=headers)
        duplicate = client.post(f"/api/cv/{foreign}/duplicate", headers=headers)
        delete = client.delete(f"/api/cv/{foreign}", headers=headers)

        for res in (rename, duplicate, delete):
            assert res.status_code == 404, res.get_data(as_text=True)[:200]
            assert res.content_type.startswith("application/json")


class TestDemoForeignIdReads:
    """Finding 3: an explicit foreign id must not silently serve the demo CV."""

    def test_get_cv_foreign_id_404s(self, demo_client):
        client, headers = demo_client
        res = client.get(f"/api/cv?id={uuid.uuid4()}", headers=headers)
        assert res.status_code == 404

    def test_get_cv_no_id_fallback_still_works(self, demo_client):
        client, headers = demo_client
        res = client.get("/api/cv", headers=headers)
        assert res.status_code == 200
        assert "experience:" in res.get_data(as_text=True)

    def test_get_cv_primary_demo_id_still_works(self, demo_client, server_module):
        client, headers = demo_client
        res = client.get(f"/api/cv?id={server_module.LOCAL_DEMO_CV_ID}", headers=headers)
        assert res.status_code == 200
        assert "experience:" in res.get_data(as_text=True)

    def test_preview_foreign_id_404s(self, demo_client):
        client, headers = demo_client
        res = client.get(f"/api/preview?id={uuid.uuid4()}", headers=headers)
        assert res.status_code == 404

    def test_build_foreign_id_404s(self, demo_client):
        client, headers = demo_client
        res = client.post("/api/build", json={"id": str(uuid.uuid4())}, headers=headers)
        assert res.status_code == 404

    def test_build_no_id_fallback_still_works(self, demo_client):
        pytest.importorskip("weasyprint")
        client, headers = demo_client
        res = client.post("/api/build", json={}, headers=headers)
        assert res.status_code == 200, res.get_data(as_text=True)[:200]


class TestDemoGdprExport:
    """Finding 2: the export must cover ALL demo documents, primary included."""

    def test_export_includes_primary_demo_cv(self, demo_client, server_module):
        client, headers = demo_client
        res = client.get("/api/account/export", headers=headers)
        assert res.status_code == 200, res.get_data(as_text=True)[:200]
        documents = res.get_json()["documents"]
        primary = next((d for d in documents if d.get("id") == server_module.LOCAL_DEMO_CV_ID), None)
        assert primary is not None, f"primary demo CV missing from export: {documents!r}"
        assert "experience:" in (primary.get("yaml_blob") or "")

    def test_export_includes_created_cvs_with_full_blob(self, demo_client, server_module):
        client, headers = demo_client
        new_id = _create_cv(client, headers)

        documents = client.get("/api/account/export", headers=headers).get_json()["documents"]
        ids = {d.get("id") for d in documents}
        assert server_module.LOCAL_DEMO_CV_ID in ids
        assert new_id in ids
        created = next(d for d in documents if d.get("id") == new_id)
        assert "John Doe" in (created.get("yaml_blob") or "")  # full skeleton blob


class TestDemoAccountDeletion:
    """Finding 4: the demo pending-deletion panel must be cancellable."""

    def test_demo_delete_then_cancel_dismisses_pending_state(self, demo_client):
        client, headers = demo_client

        deleted = client.post("/api/account/delete", json={"confirm": "DELETE"}, headers=headers)
        assert deleted.status_code == 200, deleted.get_data(as_text=True)[:200]
        assert deleted.get_json()["deletion_requested"] is True

        cancelled = client.post("/api/account/cancel-delete", headers=headers)
        assert cancelled.status_code == 200, cancelled.get_data(as_text=True)[:200]
        assert cancelled.get_json()["cancelled"] is True


class TestDemoCoverLetterRevisionParity:
    def test_save_uses_cas_preserves_lineage_and_allows_idempotent_retry(
        self, demo_client, server_module, monkeypatch
    ):
        import auth as auth_mod
        import cover_letter_service as cl_service
        import local_demo_store

        client, headers = demo_client
        doc_id = str(uuid.uuid4())
        initial_revision = "2026-08-01T10:00:00+00:00"
        local_demo_store.db().tables.setdefault("documents", []).append(
            {
                "id": doc_id,
                "owner_id": auth_mod.LOCAL_DEMO_OWNER_ID,
                "type": "cover_letter",
                "name": "Demo letter",
                "yaml_blob": cl_service.serialize_blob({"body": "Original body."}),
                "job_id": None,
                "deleted_at": None,
                "created_at": initial_revision,
                "updated_at": initial_revision,
            }
        )
        lineage = {
            "packet_id": str(uuid.uuid4()),
            "document_role": "cover_letter",
            "status": "stale",
            "stale_reasons": ["cover_letter_stale"],
        }
        monkeypatch.setattr(
            server_module.jobs_svc,
            "packet_lineage_for_document",
            lambda requested_id: lineage if requested_id == doc_id else None,
        )

        saved = client.post(
            "/api/cover-letter",
            json={"id": doc_id, "body": "First committed edit.", "revision": initial_revision},
            headers=headers,
        )
        assert saved.status_code == 200, saved.get_data(as_text=True)[:300]
        current_revision = saved.get_json()["updated_at"]
        assert current_revision != initial_revision
        assert saved.get_json()["packet_lineage"] == lineage

        stale = client.post(
            "/api/cover-letter",
            json={"id": doc_id, "body": "Conflicting stale edit.", "revision": initial_revision},
            headers=headers,
        )
        assert stale.status_code == 409
        assert stale.get_json()["code"] == "revision_conflict"
        stored = next(
            row for row in local_demo_store.db().tables["documents"] if row["id"] == doc_id
        )
        assert cl_service.parse_blob(stored["yaml_blob"])["body"] == "First committed edit."

        retry = client.post(
            "/api/cover-letter",
            json={"id": doc_id, "body": "First committed edit.", "revision": initial_revision},
            headers=headers,
        )
        assert retry.status_code == 200
        assert retry.get_json()["updated_at"] == current_revision
        assert retry.get_json()["packet_lineage"] == lineage


class TestApplyNonDictJsonBody:
    """Finding 5: truthy non-dict JSON bodies must not 500 the apply routes."""

    def test_demo_apply_open_with_array_body_is_not_500(self, demo_client, monkeypatch):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        client, headers = demo_client
        job = client.post(
            "/api/jobs",
            json={"title": "Probe", "company": "ProbeCorp", "jd_text": JD},
            headers=headers,
        )
        assert job.status_code == 201, job.get_data(as_text=True)[:200]
        job_id = job.get_json()["id"]

        res = client.post(
            f"/api/jobs/{job_id}/apply/open",
            data='["x"]',
            content_type="application/json",
            headers=headers,
        )
        assert res.status_code != 500
        assert res.content_type.startswith("application/json")

    def test_demo_apply_dry_run_with_scalar_body_is_not_500(self, demo_client, monkeypatch):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        client, headers = demo_client
        job_id = client.post(
            "/api/jobs",
            json={"title": "Probe", "company": "ProbeCorp", "jd_text": JD},
            headers=headers,
        ).get_json()["id"]

        res = client.post(
            f"/api/jobs/{job_id}/apply/dry-run",
            data="true",
            content_type="application/json",
            headers=headers,
        )
        assert res.status_code != 500
        assert res.content_type.startswith("application/json")

    def test_production_apply_routes_with_non_dict_body_behave_like_empty_object(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Pre-existing in production too: a non-dict body coerces to ``{}``
        and follows the same JSON client-error path as an empty object, never
        an AttributeError 500."""
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        job_id = str(uuid.uuid4())
        fake_supabase.tables.setdefault("saved_jobs", []).append(
            {
                "id": job_id,
                "title": "Senior Data Engineer",
                "company": "Acme GmbH",
                "jd_text": JD,
                "source_url": "https://jobs.example.com/apply",
                "source_metadata": {},
                "updated_at": 10,
            }
        )

        for route in ("open", "dry-run"):
            res = client.post(
                f"/api/jobs/{job_id}/apply/{route}",
                data='["x"]',
                content_type="application/json",
            )
            # Same client-error path as an empty object (the exact code depends
            # on readiness blockers) — the regression was an AttributeError 500.
            assert res.status_code in (400, 409), res.get_data(as_text=True)[:200]
            assert res.get_json()["code"]
