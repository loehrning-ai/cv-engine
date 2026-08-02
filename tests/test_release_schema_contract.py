from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent
MIGRATION = (
    REPO
    / "supabase"
    / "migrations"
    / "20260801180820_release_schema_contract_probe.sql"
)

CONTRACT_ID = "onepager-20260801170000-v1"
AUTHENTICATED_RUNTIME_SIGNATURES = (
    "public.cancel_account_deletion()",
    "public.consume_managed_credit(text)",
    "public.delete_document_safely(uuid,text)",
    "public.delete_saved_job_safely(uuid)",
    "public.insert_document_if_account_active(text,text,text,uuid,text,text,text,text,text)",
    "public.mark_first_cv_seen()",
    "public.record_byo_usage(text,text)",
    "public.request_account_deletion_with_artifact_detach()",
    "public.seed_first_cv_template(text,text,text,text,text)",
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def _function_body() -> str:
    sql = _sql()
    return sql.split("as $$", 1)[1].split("$$;", 1)[0]


def test_release_contract_is_forward_only_and_exactly_versioned() -> None:
    assert MIGRATION.name.startswith("20260801180820_")
    assert "20260801180820" > "20260801170000"
    sql = _sql()
    assert f"'{CONTRACT_ID}'::text" in sql
    assert re.search(r"\n\s*1,\n\s*worker_contract\.version,", sql)
    assert "worker_contract.version = 2" in sql


def test_release_contract_has_a_bounded_read_only_response() -> None:
    sql = _sql()
    returns = sql.split("returns table (", 1)[1].split(")\nlanguage sql", 1)[0]
    assert [line.strip().rstrip(",") for line in returns.splitlines() if line.strip()] == [
        "contract_id text",
        "contract_version integer",
        "worker_contract_version integer",
        "worker_contract_ok boolean",
        "authenticated_rpc_execute_ok boolean",
        "evidence_backed_job_status_ok boolean",
        "compatible boolean",
    ]
    assert "\nlanguage sql\nstable\nsecurity definer\nset search_path = ''" in sql

    body = _function_body()
    for write_statement in (
        r"\binsert\s+into\b",
        r"\bupdate\s+[a-z\"]",
        r"\bdelete\s+from\b",
        r"\btruncate\b",
        r"\balter\s+(?:table|function|role)\b",
        r"\bcreate\s+(?:table|function|role)\b",
        r"\bdrop\s+(?:table|function|role)\b",
        r"\bgrant\b",
        r"\brevoke\b",
    ):
        assert re.search(write_statement, body) is None
    assert "auth.uid" not in body
    assert "auth.jwt" not in body
    assert "request.jwt" not in body
    assert "current_setting" not in body


def test_release_contract_checks_every_exact_user_rpc_regprocedure() -> None:
    sql = _sql()
    values_block = sql.split(
        "authenticated_runtime_rpcs(signature) as (",
        1,
    )[1].split("),\n  authenticated_runtime_contract", 1)[0]
    found = tuple(re.findall(r"\('([^']+)'\)", values_block))
    assert found == AUTHENTICATED_RUNTIME_SIGNATURES
    assert "pg_catalog.to_regprocedure(signature) is not null" in sql
    assert "pg_catalog.has_function_privilege(" in sql
    assert "'authenticated'" in sql
    assert "'execute'" in sql
    assert "pg_catalog.bool_and(" in sql


def test_release_contract_proves_exact_worker_contract_version() -> None:
    sql = _sql()
    assert (
        "select public.account_deletion_worker_contract_version() as version"
        in sql
    )
    assert sql.count("worker_contract.version = 2") >= 2
    assert "and authenticated_runtime_contract.execute_ok" in sql


def test_release_contract_proves_evidence_backed_job_status_trigger() -> None:
    sql = _sql()
    assert "from pg_catalog.pg_trigger as trigger" in sql
    assert (
        "trigger.tgname = "
        "'reject_evidence_free_applied_job_status_on_write'"
    ) in sql
    assert "pg_catalog.to_regclass('public.saved_jobs')" in sql
    assert (
        "pg_catalog.to_regprocedure(\n"
        "          'public.reject_evidence_free_applied_job_status()'"
    ) in sql
    assert "trigger.tgenabled in ('o', 'a')" in sql
    assert "and not trigger.tgisinternal" in sql
    assert "and evidence_backed_job_status_contract.enabled" in sql


def test_release_contract_is_callable_only_by_service_role() -> None:
    sql = _sql()
    signature = "function public.release_schema_contract()"
    assert f"revoke execute on {signature}" in sql
    assert "from public, anon, authenticated, service_role;" in sql
    assert f"grant execute on {signature}\n  to service_role;" in sql
    assert f"grant execute on {signature}\n  to authenticated" not in sql
    assert f"grant execute on {signature}\n  to anon" not in sql
    assert f"grant execute on {signature}\n  to public" not in sql
