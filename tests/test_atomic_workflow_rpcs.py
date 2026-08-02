"""Focused contracts for atomic job and application-packet workflow RPCs."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
OWNER_ID = "00000000-0000-0000-0000-000000000001"
JD_TEXT = (
    "Title: Senior Data Engineer\n"
    "Company: Acme GmbH\n"
    "Responsibilities: Build reliable Python and SQL data pipelines.\n"
    "Requirements: Five years of data engineering experience."
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sql_function_block(sql: str, function_name: str) -> str:
    marker = f"create or replace function public.{function_name}"
    return sql.split(marker, 1)[1].split("$$;", 1)[0]


def _seed_job(store, *, jd_text: str = JD_TEXT) -> dict:
    job = {
        "id": str(uuid.uuid4()),
        "owner_id": store.owner_id,
        "title": "Senior Data Engineer",
        "company": "Acme GmbH",
        "jd_text": jd_text,
        "jd_json": {},
        "source_metadata": {"full_jd_status": "reviewed"},
        "verification_status": "user_provided",
        "status": "saved",
        "created_at": "2026-08-01T09:00:00+00:00",
        "updated_at": "2026-08-01T09:00:00+00:00",
    }
    store.tables.setdefault("saved_jobs", []).append(job)
    return job


def _seed_document(
    store,
    *,
    document_type: str,
    yaml_blob: str,
    job_id: str | None = None,
) -> dict:
    document = {
        "id": str(uuid.uuid4()),
        "owner_id": store.owner_id,
        "type": document_type,
        "name": f"Atomic {document_type}",
        "yaml_blob": yaml_blob,
        "job_id": job_id,
        "deleted_at": None,
        "created_at": "2026-08-01T09:00:00+00:00",
        "updated_at": "2026-08-01T09:00:00+00:00",
    }
    store.tables.setdefault("documents", []).append(document)
    return document


def _seed_pending_packet(store) -> tuple[dict, dict, dict, dict, dict]:
    job = _seed_job(store)
    source = _seed_document(store, document_type="cv", yaml_blob="name: Source\n")
    tailored = _seed_document(
        store,
        document_type="cv",
        yaml_blob="name: Tailored\n",
        job_id=job["id"],
    )
    letter = _seed_document(
        store,
        document_type="cover_letter",
        yaml_blob="company: Acme GmbH\nbody: Grounded evidence.\n",
        job_id=job["id"],
    )
    packet = {
        "id": str(uuid.uuid4()),
        "owner_id": store.owner_id,
        "job_id": job["id"],
        "source_cv_id": source["id"],
        "tailored_cv_id": tailored["id"],
        "cover_letter_id": letter["id"],
        "status": "review_applied",
        "review_status": "reviewed",
        "provider": "anthropic",
        "model": "test-model",
        "prompt_version": "jobs_packet.v1",
        "score_system_id": "semantic_fit",
        "score_snapshot": {"score": 91},
        "source_cv_hash": _digest(source["yaml_blob"]),
        "job_content_hash": _digest(job["jd_text"]),
        "tailored_cv_hash": _digest(tailored["yaml_blob"]),
        "cover_letter_hash": _digest(letter["yaml_blob"]),
        "input_hash": _digest(source["yaml_blob"]),
        "output_hash": _digest(tailored["yaml_blob"]),
        "packet_version": 1,
        "cover_letter_review_status": "pending",
        "cover_letter_review_confirmed_at": None,
        "cover_letter_review_id": "cover-review-1",
        "archived_at": None,
        "created_at": "2026-08-01T09:05:00+00:00",
        "updated_at": "2026-08-01T09:05:00+00:00",
    }
    store.tables.setdefault("application_packets", []).append(packet)
    return job, source, tailored, letter, packet


def test_import_result_save_is_idempotent_and_repairs_provenance_link(
    client,
    fake_supabase,
):
    imported = client.post(
        "/api/jobs/import-runs",
        json={
            "provider": "manual",
            "query": "data engineer",
            "postings": [
                {
                    "title": "Senior Data Engineer",
                    "company": "Acme GmbH",
                    "jd_text": JD_TEXT,
                    "source_external_id": "acme-atomic-1",
                }
            ],
        },
    )
    assert imported.status_code == 202, imported.get_data(as_text=True)
    run = imported.get_json()
    result_id = run["results"][0]["id"]
    endpoint = f"/api/jobs/import-runs/{run['id']}/save-selected"

    first = client.post(endpoint, json={"result_ids": [result_id]})

    assert first.status_code == 200, first.get_data(as_text=True)
    assert first.get_json()["saved_count"] == 1
    assert len(fake_supabase.tables["saved_jobs"]) == 1
    job_id = fake_supabase.tables["saved_jobs"][0]["id"]
    result = fake_supabase.tables["job_import_results"][0]
    assert result["selected"] is True
    assert result["saved_job_id"] == job_id

    # Simulate stale provenance from an interrupted older flow. The atomic RPC
    # finds the existing identity and repairs the result link without a second job.
    result["selected"] = False
    result["saved_job_id"] = None
    repaired = client.post(endpoint, json={"result_ids": [result_id]})
    retried = client.post(endpoint, json={"result_ids": [result_id]})

    assert repaired.status_code == 200, repaired.get_data(as_text=True)
    assert repaired.get_json() == {
        "saved": [],
        "saved_count": 0,
        "skipped": [
            {"job_id": job_id, "reason": "duplicate", "result_id": result_id}
        ],
        "skipped_count": 1,
    }
    assert retried.status_code == 200, retried.get_data(as_text=True)
    assert retried.get_json()["skipped"][0]["job_id"] == job_id
    assert len(fake_supabase.tables["saved_jobs"]) == 1
    assert result["selected"] is True
    assert result["saved_job_id"] == job_id


def test_star_and_revalidation_merge_current_metadata(client, fake_supabase):
    created = client.post("/api/jobs", json={"jd_text": JD_TEXT})
    assert created.status_code == 201, created.get_data(as_text=True)
    job_id = created.get_json()["id"]
    row = fake_supabase.tables["saved_jobs"][0]
    row["source_metadata"] = {
        "origin": "local_jobs_db_read_only",
        "full_jd_status": "reviewed",
        "ranking": {"bucket": "strong", "score": 94},
        "source_links": [
            {"url": "https://jobs.example.test/apply?li_at=secret-cookie"}
        ],
        "starred": False,
        "verified_at": "2026-07-31",
        "session_token": "must-not-survive",
        "unknown_extension": "must-not-survive",
    }

    starred = client.patch(f"/api/jobs/{job_id}", json={"starred": True})

    assert starred.status_code == 200, starred.get_data(as_text=True)
    assert row["source_metadata"]["starred"] is True
    assert row["source_metadata"]["ranking"] == {"bucket": "strong", "score": 94}
    assert row["source_metadata"]["verified_at"] == "2026-07-31"
    assert "source_links" not in row["source_metadata"]
    assert "session_token" not in row["source_metadata"]
    assert "unknown_extension" not in row["source_metadata"]

    replacement_jd = JD_TEXT + "\nPreferred: Apache Airflow."
    revalidated = client.post(
        f"/api/jobs/{job_id}/source/revalidate",
        json={"full_jd_text": replacement_jd},
    )

    assert revalidated.status_code == 200, revalidated.get_data(as_text=True)
    assert row["jd_text"] == replacement_jd
    assert row["source_metadata"]["starred"] is True
    assert row["source_metadata"]["ranking"] == {"bucket": "strong", "score": 94}
    assert row["source_metadata"]["revalidation"] == "manual_review"
    assert row["source_metadata"]["full_jd_status"] == "reviewed"
    assert revalidated.get_json()["starred"] is True


def test_cover_confirmation_rejects_document_drift_inside_atomic_boundary(
    client,
    fake_supabase,
):
    job, _source, _tailored, letter, packet = _seed_pending_packet(fake_supabase)
    expected_hash = packet["cover_letter_hash"]
    original_letter = letter["yaml_blob"]

    # The UI-reviewed hash is still the stored packet hash, but the document
    # changed before the durable transition. Confirmation must not advance.
    letter["yaml_blob"] = original_letter + "body: Changed after review.\n"
    rejected = client.post(
        f"/api/jobs/{job['id']}/packets/{packet['id']}/confirm-cover-letter-review",
        json={"cover_letter_hash": expected_hash},
    )

    assert rejected.status_code == 409, rejected.get_data(as_text=True)
    assert rejected.get_json() == {
        "code": "packet_stale",
        "error": "The packet changed before cover-letter review was confirmed.",
        "stale_reasons": ["cover_letter_stale"],
    }
    assert packet["status"] == "review_applied"
    assert packet["cover_letter_review_status"] == "pending"
    assert packet["cover_letter_review_confirmed_at"] is None

    letter["yaml_blob"] = original_letter
    confirmed = client.post(
        f"/api/jobs/{job['id']}/packets/{packet['id']}/confirm-cover-letter-review",
        json={"cover_letter_hash": expected_hash},
    )
    confirmed_at = packet["cover_letter_review_confirmed_at"]
    retried = client.post(
        f"/api/jobs/{job['id']}/packets/{packet['id']}/confirm-cover-letter-review",
        json={"cover_letter_hash": expected_hash},
    )

    assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
    assert retried.status_code == 200, retried.get_data(as_text=True)
    assert packet["status"] == "packet_created"
    assert packet["cover_letter_review_status"] == "confirmed"
    assert packet["cover_letter_review_confirmed_at"] == confirmed_at


def test_standalone_lineage_merges_without_losing_existing_artifact(
    server_module,
    fake_supabase,
):
    import jobs_service

    job = _seed_job(fake_supabase)
    source = _seed_document(fake_supabase, document_type="cv", yaml_blob="name: Source\n")
    tailored = _seed_document(
        fake_supabase,
        document_type="cv",
        yaml_blob="name: Tailored\n",
        job_id=job["id"],
    )
    letter = _seed_document(
        fake_supabase,
        document_type="cover_letter",
        yaml_blob="company: Acme GmbH\nbody: Evidence.\n",
        job_id=job["id"],
    )
    score_snapshot = {"score": 88, "verdict": "strong"}

    with server_module.app.test_request_context(
        headers={"Authorization": "Bearer test-token"}
    ):
        server_module.auth_mod.auth_g().owner_id = OWNER_ID
        server_module.auth_mod.auth_g().access_token = "test-token"
        tailored_lineage = jobs_service.record_tailored_cv_packet_lineage(
            job_id=job["id"],
            source_cv_id=source["id"],
            tailored_cv_id=tailored["id"],
            provider="anthropic",
            model="test-model",
            ats=score_snapshot,
            score_system_id="semantic_fit",
            review_id="tailor-review-1",
            now_iso="2026-08-01T10:00:00+00:00",
        )
        letter_lineage = jobs_service.record_cover_letter_packet_lineage(
            job_id=job["id"],
            source_cv_id=source["id"],
            tailored_cv_id=tailored["id"],
            cover_letter_id=letter["id"],
            provider="anthropic",
            model="test-model",
            ats=None,
            score_system_id="semantic_fit",
            review_id="cover-review-1",
            now_iso="2026-08-01T10:01:00+00:00",
        )

    packets = fake_supabase.tables["application_packets"]
    assert len(packets) == 1
    packet = packets[0]
    assert tailored_lineage["id"] == letter_lineage["id"] == packet["id"]
    assert packet["tailored_cv_id"] == tailored["id"]
    assert packet["cover_letter_id"] == letter["id"]
    assert packet["tailored_cv_hash"] == _digest(tailored["yaml_blob"])
    assert packet["cover_letter_hash"] == _digest(letter["yaml_blob"])
    assert packet["score_snapshot"] == score_snapshot
    assert packet["packet_version"] == 1
    assert packet["status"] == "review_applied"
    assert packet["archived_at"] is None


def test_standalone_lineage_rejects_locked_input_drift_without_packet_mutation(
    server_module,
    fake_supabase,
    monkeypatch,
):
    import jobs_service

    job = _seed_job(fake_supabase)
    source = _seed_document(fake_supabase, document_type="cv", yaml_blob="name: Source\n")
    tailored = _seed_document(
        fake_supabase,
        document_type="cv",
        yaml_blob="name: Tailored\n",
        job_id=job["id"],
    )
    letter = _seed_document(
        fake_supabase,
        document_type="cover_letter",
        yaml_blob="company: Acme GmbH\nbody: Evidence.\n",
        job_id=job["id"],
    )

    with server_module.app.test_request_context(
        headers={"Authorization": "Bearer test-token"}
    ):
        server_module.auth_mod.auth_g().owner_id = OWNER_ID
        server_module.auth_mod.auth_g().access_token = "test-token"
        jobs_service.record_tailored_cv_packet_lineage(
            job_id=job["id"],
            source_cv_id=source["id"],
            tailored_cv_id=tailored["id"],
            provider="anthropic",
            model="test-model",
            ats={"score": 88},
            score_system_id="semantic_fit",
            review_id="tailor-review-1",
            now_iso="2026-08-01T10:00:00+00:00",
        )
        before = json.loads(
            json.dumps(fake_supabase.tables["application_packets"])
        )
        merge = fake_supabase._merge_incomplete_application_packet

        def _drift_before_locked_merge(params):
            tailored["yaml_blob"] += "summary: Changed after service hashing.\n"
            return merge(params)

        monkeypatch.setattr(
            fake_supabase,
            "_merge_incomplete_application_packet",
            _drift_before_locked_merge,
        )
        with pytest.raises(jobs_service.JobError) as exc:
            jobs_service.record_cover_letter_packet_lineage(
                job_id=job["id"],
                source_cv_id=source["id"],
                tailored_cv_id=tailored["id"],
                cover_letter_id=letter["id"],
                provider="anthropic",
                model="test-model",
                ats=None,
                score_system_id="semantic_fit",
                review_id="cover-review-1",
                now_iso="2026-08-01T10:01:00+00:00",
            )

    assert exc.value.status == 409
    assert exc.value.payload == {
        "error": "Application inputs changed before packet lineage was stored.",
        "code": "packet_input_changed",
    }
    assert fake_supabase.tables["application_packets"] == before


def test_job_workflow_atomicity_migration_is_server_only_and_locked():
    sql = (
        ROOT
        / "supabase"
        / "migrations"
        / "20260801160000_job_workflow_atomicity.sql"
    ).read_text(encoding="utf-8")

    for function_name in (
        "save_import_result_as_job",
        "set_saved_job_star",
        "revalidate_saved_job_source",
    ):
        assert f"create or replace function public.{function_name}" in sql
        assert f"revoke execute on function public.{function_name}" in sql
        assert f"grant execute on function public.{function_name}" in sql
        block = _sql_function_block(sql, function_name)
        assert "security definer" in block
        assert "set search_path = ''" in block
        assert "set lock_timeout = '2s'" in block
    assert "pg_catalog.pg_advisory_xact_lock" in sql
    assert "from public.job_import_results as i" in sql
    assert "for update;" in sql
    assert "set selected = true" in sql
    assert "saved_job_id = created_job.id" in sql
    assert "create or replace function public.sanitize_saved_job_source_metadata" in sql
    assert "public.sanitize_saved_job_source_metadata(j.source_metadata)" in sql
    assert "before insert or update of source_metadata on public.saved_jobs" in sql
    assert "revoke update (source_metadata) on table public.saved_jobs from authenticated" in sql
    assert "'source_links', 'job_family', 'source_city', 'search_criteria'" in sql
    assert "session[_-]?token" in sql
    assert "private[_-]?key" in sql
    assert (
        "revoke execute on function public.sanitize_saved_job_source_metadata(jsonb)"
        in sql
    )
    assert (
        "revoke execute on function public.sanitize_saved_job_source_metadata_trigger()"
        in sql
    )
    assert "from public, anon, authenticated;" in sql
    assert "grant execute on function public.save_import_result_as_job" in sql
    assert ") to service_role;" in sql
    assert ") from public, anon, authenticated;" in sql


def test_application_packet_atomicity_migration_hashes_and_locks_lineage():
    sql = (
        ROOT
        / "supabase"
        / "migrations"
        / "20260801170000_application_packet_atomicity.sql"
    ).read_text(encoding="utf-8")

    for function_name in (
        "confirm_application_packet_cover_review",
        "merge_incomplete_application_packet",
    ):
        assert f"create or replace function public.{function_name}" in sql
        assert f"revoke execute on function public.{function_name}" in sql
        assert f"grant execute on function public.{function_name}" in sql
        block = _sql_function_block(sql, function_name)
        assert "security definer" in block
        assert "set search_path = ''" in block
        assert "set lock_timeout = '2s'" in block
    assert sql.count("pg_catalog.sha256") >= 4
    assert "'code', 'packet_stale'" in sql
    assert "cover_letter_stale" in sql
    assert "for update;" in sql
    assert "pg_catalog.pg_advisory_xact_lock" in sql
    assert "coalesce(max(p.packet_version), 0) + 1" in sql
    assert "score_snapshot_present" in sql
    merge_block = _sql_function_block(sql, "merge_incomplete_application_packet")
    assert merge_block.count("pg_catalog.sha256") >= 4
    assert "'code', 'packet_input_changed'" in merge_block
    assert merge_block.index("from public.saved_jobs as j") < merge_block.index(
        "pg_catalog.pg_advisory_xact_lock"
    )
    assert merge_block.index("pg_catalog.pg_advisory_xact_lock") < merge_block.index(
        "from public.application_packets as p"
    )
    assert merge_block.index("from public.application_packets as p") < merge_block.index(
        "from public.documents as d"
    )
    assert ") to service_role;" in sql
    assert ") from public, anon, authenticated;" in sql
