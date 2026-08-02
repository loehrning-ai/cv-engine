from __future__ import annotations

from pathlib import Path
import uuid

import pytest


REPO = Path(__file__).resolve().parent.parent
MIGRATION = (
    REPO
    / "supabase"
    / "migrations"
    / "20260801145140_preserve_application_history_deletes.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def _function_sql(name: str, following: str) -> str:
    sql = _sql()
    return sql.split(f"function public.{name}", 1)[1].split(
        f"function public.{following}", 1
    )[0]


def test_authenticated_direct_deletes_are_revoked() -> None:
    sql = _sql()
    assert "drop policy if exists \"saved_jobs_delete_own\"" in sql
    assert "drop policy if exists \"documents_delete_own\"" in sql
    assert "revoke delete on table public.saved_jobs from authenticated" in sql
    assert "revoke delete on table public.documents from authenticated" in sql
    assert "grant delete" not in sql


def test_safe_delete_rpcs_are_narrow_and_tenant_derived() -> None:
    sql = _sql()
    for signature in (
        "public.delete_saved_job_safely(uuid)",
        "public.delete_document_safely(uuid, text)",
    ):
        assert f"revoke execute on function {signature}" in sql
        assert f"grant execute on function {signature}" in sql
    for signature in (
        "public.complete_pdf_artifact_cleanup(uuid, uuid, uuid)",
        "public.record_pdf_artifact_cleanup_failure(uuid, uuid, uuid)",
    ):
        assert f"revoke execute on function {signature}" in sql
        assert f"grant execute on function {signature}" in sql
    assert sql.count("security definer") >= 4
    assert sql.count("set search_path = ''") >= 4
    assert "caller_id uuid := auth.uid()" in sql


def test_job_delete_lock_precedes_every_history_check_and_delete() -> None:
    body = _function_sql("delete_saved_job_safely", "delete_document_safely")
    lock_at = body.index("for update")
    delete_at = body.index("delete from public.saved_jobs")
    assert lock_at < delete_at
    for relation in (
        "from public.documents",
        "from public.application_packets",
        "from public.application_apply_attempts",
        "from public.tailor_review_states",
    ):
        relation_at = body.index(relation, lock_at)
        assert lock_at < relation_at < delete_at
    assert "j.owner_id = caller_id" in body
    assert "job_has_application_history" in body


def test_document_delete_lock_precedes_packet_and_review_checks() -> None:
    sql = _sql()
    body = sql.split("function public.delete_document_safely", 1)[1]
    lock_at = body.index("for update")
    delete_at = body.index("delete from public.documents")
    assert lock_at < body.index("from public.application_packets", lock_at) < delete_at
    assert lock_at < body.index("from public.tailor_review_states", lock_at) < delete_at
    for column in ("source_cv_id", "tailored_cv_id", "cover_letter_id"):
        assert f"p.{column} = locked_document_id" in body
    assert "d.owner_id = caller_id" in body
    assert "document_has_application_history" in body


def test_document_delete_returns_only_locked_canonical_artifact_evidence() -> None:
    sql = _sql()
    body = sql.split("function public.delete_document_safely", 1)[1]
    child_lock_at = body.index("from public.pdf_artifacts")
    canonical_at = body.index("document_artifact_invalid")
    snapshot_at = body.index("into artifact_snapshot")
    delete_at = body.index("delete from public.documents")
    assert child_lock_at < canonical_at < snapshot_at < delete_at
    assert "p.content_hash !~ '^[0-9a-f]{64}$'" in body
    assert "caller_id::text || '/' || locked_document_id::text" in body
    assert "from public.pdf_artifacts" in body
    assert "for update" in body[child_lock_at:canonical_at]
    assert "insert into public.pdf_artifact_cleanup_queue" in body
    assert "'artifacts', artifact_snapshot" in body


def test_cleanup_queue_is_durable_and_not_directly_mutable() -> None:
    sql = _sql()
    assert "create table if not exists public.pdf_artifact_cleanup_queue" in sql
    assert "alter table public.pdf_artifact_cleanup_queue enable row level security" in sql
    assert "revoke all on table public.pdf_artifact_cleanup_queue" in sql
    assert "grant select on table public.pdf_artifact_cleanup_queue" not in sql
    assert "grant insert" not in sql
    assert "grant update" not in sql
    assert "grant delete" not in sql


def _owned_row(store, *, row_id: str, **values) -> dict:
    return {"id": row_id, "owner_id": store.owner_id, **values}


@pytest.mark.parametrize(
    ("table", "child"),
    (
        ("documents", {"type": "cv", "job_id": "__job__", "deleted_at": None}),
        ("application_packets", {"job_id": "__job__"}),
        ("application_apply_attempts", {"job_id": "__job__", "packet_id": None}),
        ("tailor_review_states", {"job_id": "__job__", "cv_id": None}),
    ),
)
def test_job_endpoint_rejects_each_history_relationship(
    client, fake_supabase, table: str, child: dict
) -> None:
    job_id = str(uuid.uuid4())
    fake_supabase.tables.setdefault("saved_jobs", []).append(
        _owned_row(fake_supabase, row_id=job_id, title="Protected", status="saved")
    )
    protected = {
        key: job_id if value == "__job__" else value for key, value in child.items()
    }
    fake_supabase.tables.setdefault(table, []).append(
        _owned_row(fake_supabase, row_id=str(uuid.uuid4()), **protected)
    )

    response = client.delete(f"/api/jobs/{job_id}")

    assert response.status_code == 409
    assert response.get_json()["code"] == "job_has_application_history"
    assert any(row.get("id") == job_id for row in fake_supabase.tables["saved_jobs"])


def test_import_provenance_alone_does_not_block_job_deletion(
    client, fake_supabase
) -> None:
    """Import provenance is not application history.

    `job_import_results.saved_job_id` is ON DELETE SET NULL, so the import
    record survives a job delete intact. Blocking on it would make every job
    saved from an import permanently undeletable while protecting nothing —
    and because the in-memory fake and the SQL RPC are maintained separately,
    only a test can keep the two from drifting apart again.
    """
    job_id = str(uuid.uuid4())
    fake_supabase.tables.setdefault("saved_jobs", []).append(
        _owned_row(fake_supabase, row_id=job_id, title="Imported", status="saved")
    )
    fake_supabase.tables.setdefault("job_import_results", []).append(
        _owned_row(
            fake_supabase,
            row_id=str(uuid.uuid4()),
            import_run_id=str(uuid.uuid4()),
            saved_job_id=job_id,
            jd_text="jd body",
        )
    )

    response = client.delete(f"/api/jobs/{job_id}")

    assert response.status_code == 200, response.get_json()
    assert not any(
        row.get("id") == job_id for row in fake_supabase.tables["saved_jobs"]
    )
    # The provenance row itself must survive, with its link cleared.
    imports = fake_supabase.tables["job_import_results"]
    assert len(imports) == 1
    assert imports[0]["saved_job_id"] is None


@pytest.mark.parametrize(
    ("yaml_blob", "expected_status", "label"),
    (
        ("source_cv_id: {cv}\nbody: Dear team\n", 409, "legacy letter without a signer"),
        (
            "source_cv_id: {cv}\nsender_name: Jordan Avery\nbody: Dear team\n",
            200,
            "letter carrying a persisted signer",
        ),
        ("source_cv_id: 11111111-1111-4111-8111-111111111111\n", 200, "unrelated letter"),
    ),
)
def test_source_cv_delete_respects_legacy_cover_letter_signers(
    client, fake_supabase, yaml_blob: str, expected_status: int, label: str
) -> None:
    """A legacy letter's signer must survive its source CV.

    Letters saved before sender_name was persisted resolve their signer from
    the source CV at render time, so deleting that CV would strip the
    signature permanently. The production RPC blocks it; the in-memory fake
    did not, which left the whole blocked path unexercised and only divergent
    in production.
    """
    cv_id = str(uuid.uuid4())
    fake_supabase.tables.setdefault("documents", []).extend(
        [
            _owned_row(
                fake_supabase,
                row_id=cv_id,
                type="cv",
                name="Source CV",
                deleted_at=None,
                yaml_blob="name: Jordan Avery\n",
            ),
            _owned_row(
                fake_supabase,
                row_id=str(uuid.uuid4()),
                type="cover_letter",
                name="Letter",
                deleted_at=None,
                yaml_blob=yaml_blob.format(cv=cv_id),
            ),
        ]
    )

    response = client.delete(f"/api/cv/{cv_id}")

    assert response.status_code == expected_status, (label, response.get_json())
    if expected_status == 409:
        assert response.get_json()["code"] == "document_has_application_history"


def test_job_delete_rpc_does_not_treat_import_provenance_as_history() -> None:
    """Pin the SQL side of the contract above.

    Text-level, but it is the only in-CI signal that the migration still agrees
    with the fake; the executable proof lives in tools/db/replay_migrations.sh.
    """
    body = _function_sql("delete_saved_job_safely", "delete_document_safely")
    # Strip `--` comments: the rationale for the omission names the very table
    # this asserts is absent from the executable clause.
    code = "\n".join(
        line.split("--", 1)[0] for line in body.splitlines()
    )
    blocked = code[: code.index("job_has_application_history")]
    assert "job_import_results" not in blocked, (
        "delete_saved_job_safely must not block on import provenance — its FK is "
        "ON DELETE SET NULL, so the import record survives the delete"
    )


@pytest.mark.parametrize(
    ("doc_type", "packet_column"),
    (
        ("cv", "source_cv_id"),
        ("cv", "tailored_cv_id"),
        ("cover_letter", "cover_letter_id"),
    ),
)
def test_document_endpoint_rejects_each_packet_role(
    client,
    fake_supabase,
    server_module,
    monkeypatch,
    doc_type: str,
    packet_column: str,
) -> None:
    doc_id = str(uuid.uuid4())
    fake_supabase.tables.setdefault("documents", []).append(
        _owned_row(
            fake_supabase,
            row_id=doc_id,
            type=doc_type,
            name="Protected document",
            yaml_blob="name: Protected\n" if doc_type == "cv" else "body: Protected\n",
            deleted_at=None,
        )
    )
    fake_supabase.tables.setdefault("application_packets", []).append(
        _owned_row(
            fake_supabase,
            row_id=str(uuid.uuid4()),
            **{packet_column: doc_id},
        )
    )
    monkeypatch.setattr(
        server_module,
        "_delete_detached_document_pdf_artifacts",
        lambda _rows: pytest.fail("protected document attempted PDF cleanup"),
    )
    endpoint = f"/api/cv/{doc_id}" if doc_type == "cv" else f"/api/cover-letter/{doc_id}"

    response = client.delete(endpoint)

    assert response.status_code == 409
    assert response.get_json()["code"] == "document_has_application_history"
    assert any(row.get("id") == doc_id for row in fake_supabase.tables["documents"])


def test_cv_delete_rejects_tailor_review_history(
    client, fake_supabase, server_module, monkeypatch
) -> None:
    doc_id = str(uuid.uuid4())
    fake_supabase.tables.setdefault("documents", []).append(
        _owned_row(
            fake_supabase,
            row_id=doc_id,
            type="cv",
            name="Reviewed CV",
            yaml_blob="name: Reviewed\n",
            deleted_at=None,
        )
    )
    fake_supabase.tables.setdefault("tailor_review_states", []).append(
        _owned_row(
            fake_supabase,
            row_id=str(uuid.uuid4()),
            cv_id=doc_id,
            job_id=None,
        )
    )
    monkeypatch.setattr(
        server_module,
        "_delete_detached_document_pdf_artifacts",
        lambda _rows: pytest.fail("protected document attempted PDF cleanup"),
    )

    response = client.delete(f"/api/cv/{doc_id}")

    assert response.status_code == 409
    assert response.get_json()["code"] == "document_has_application_history"
    assert any(row.get("id") == doc_id for row in fake_supabase.tables["documents"])
