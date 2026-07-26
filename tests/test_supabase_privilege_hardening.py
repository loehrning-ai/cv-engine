from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent
MIGRATION = (
    REPO
    / "supabase"
    / "migrations"
    / "0020_data_api_privilege_hardening.sql"
)
INDEX_MIGRATION = (
    REPO
    / "supabase"
    / "migrations"
    / "0021_foreign_key_indexes.sql"
)
CLAIM_MIGRATION = (
    REPO
    / "supabase"
    / "migrations"
    / "0019_account_deletion_worker_claim.sql"
)
CLAIM_FIX_MIGRATION = (
    REPO
    / "supabase"
    / "migrations"
    / "0022_fix_account_deletion_claim_conflict.sql"
)
SECURITY_CONTRACT_MIGRATION = (
    REPO
    / "supabase"
    / "migrations"
    / "0023_security_contract_hardening.sql"
)
TENANT_RELATIONSHIP_MIGRATION = (
    REPO
    / "supabase"
    / "migrations"
    / "0024_tenant_relationship_constraints.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_browser_roles_lose_broad_table_and_sequence_privileges() -> None:
    sql = _sql()
    assert (
        "revoke all privileges on all tables in schema public "
        "from anon, authenticated"
    ) in sql
    assert (
        "revoke all privileges on all sequences in schema public "
        "from anon, authenticated"
    ) in sql
    assert "revoke all privileges (%s)" in sql


def test_truncate_is_never_regranted_to_a_browser_role() -> None:
    sql = _sql()
    assert "grant truncate" not in sql
    assert "grant all" not in sql


def test_anon_receives_no_public_data_api_grant() -> None:
    grants = [
        line.strip()
        for line in _sql().splitlines()
        if line.strip().startswith("grant ")
    ]
    assert all(" to anon" not in line for line in grants)


def test_worker_rpcs_remain_service_role_only() -> None:
    sql = _sql()
    assert "grant execute on all functions in schema public to service_role" in sql
    for function in (
        "claim_accounts_for_deletion",
        "account_deletion_claim_is_current",
        "account_deletion_worker_ready",
    ):
        assert f"grant execute on function public.{function}" not in sql


def test_future_objects_fail_closed() -> None:
    sql = _sql()
    assert "alter default privileges for role postgres in schema public" in sql
    assert "revoke execute on functions from public" in sql


def test_every_workflow_foreign_key_gets_a_covering_index() -> None:
    sql = INDEX_MIGRATION.read_text(encoding="utf-8").lower()
    for index_name in (
        "application_apply_attempts_job_id_idx",
        "application_apply_attempts_packet_id_idx",
        "application_packets_cover_letter_id_idx",
        "application_packets_job_id_idx",
        "application_packets_source_cv_id_idx",
        "application_packets_tailored_cv_id_idx",
        "job_import_results_import_run_id_idx",
        "job_import_results_saved_job_id_idx",
        "saved_jobs_import_run_id_idx",
        "tailor_review_states_cv_id_idx",
        "tailor_review_states_job_id_idx",
    ):
        assert f"create index if not exists {index_name}" in sql


def test_claim_rpc_uses_unambiguous_conflict_constraint() -> None:
    for migration in (CLAIM_MIGRATION, CLAIM_FIX_MIGRATION):
        sql = migration.read_text(encoding="utf-8").lower()
        assert "on conflict (owner_id)" not in sql
        assert (
            "on conflict on constraint account_deletion_claims_pkey "
            "do update"
        ) in sql


def test_every_migration_has_a_numeric_unique_version() -> None:
    migration_dir = REPO / "supabase" / "migrations"
    names = sorted(path.name for path in migration_dir.glob("*.sql"))
    assert names
    assert all(
        re.fullmatch(r"[0-9]+_[a-z0-9_]+\.sql", name)
        for name in names
    )
    versions = [name.split("_", 1)[0] for name in names]
    assert len(versions) == len(set(versions))


def test_final_document_policy_hides_deleted_and_pending_rows() -> None:
    sql = SECURITY_CONTRACT_MIGRATION.read_text(encoding="utf-8").lower()
    policy = sql.split(
        'create policy "documents_select_own"',
        1,
    )[1].split(");", 1)[0]
    assert "owner_id = (select auth.uid())" in policy
    assert "deleted_at is null" in policy
    assert "deletion_requested_at is not null" in policy


def test_immutable_audit_tables_are_rpc_only() -> None:
    sql = SECURITY_CONTRACT_MIGRATION.read_text(encoding="utf-8").lower()
    assert "drop policy if exists \"usage_events_insert_own\"" in sql
    assert "revoke insert on table public.usage_events from authenticated" in sql
    assert "create or replace function public.record_byo_usage(" in sql
    assert "drop policy if exists \"application_apply_attempts_insert_own\"" in sql
    assert (
        "revoke insert on table public.application_apply_attempts "
        "from authenticated"
    ) in sql
    assert (
        "create or replace function "
        "public.record_application_apply_attempt("
    ) in sql
    assert "submitted,\n    automation_performed" in sql
    assert "false,\n    false" in sql

    gateway = (
        REPO / "tools" / "editor" / "llm_gateway.py"
    ).read_text(encoding="utf-8")
    apply_service = (
        REPO / "tools" / "editor" / "apply_service.py"
    ).read_text(encoding="utf-8")
    assert 'rpc(\n            "record_byo_usage"' in gateway
    assert '"record_application_apply_attempt"' in apply_service
    assert '.table("usage_events").insert(' not in gateway
    assert ".table(ATTEMPT_TABLE).insert(" not in apply_service


def test_authenticated_definer_inputs_and_deletion_state_are_bounded() -> None:
    sql = SECURITY_CONTRACT_MIGRATION.read_text(encoding="utf-8").lower()
    for kind in (
        "cv_tailor",
        "ats_score",
        "cover_letter_generate",
        "polish_bullet",
        "polish_section",
        "extract",
    ):
        assert f"'{kind}'" in sql
    assert "and deletion_requested_at is null" in sql
    assert "octet_length(coalesce(p_yaml_blob, '')) > 524288" in sql
    assert "char_length(normalized_name) > 200" in sql
    assert "^#[0-9a-fa-f]{6}$" in sql
    assert "('serif', 'sans', 'mono')" in sql
    assert "('tight', 'normal', 'airy')" in sql


def test_worker_contract_is_versioned_and_null_controls_fail_closed() -> None:
    sql = SECURITY_CONTRACT_MIGRATION.read_text(encoding="utf-8").lower()
    assert "p_batch_size is null" in sql
    assert "p_lease_minutes is null" in sql
    assert (
        "create or replace function "
        "public.account_deletion_worker_contract_version()"
    ) in sql
    assert "select 2;" in sql
    assert "drop function if exists public.account_deletion_worker_ready()" in sql
    assert (
        "grant execute on function "
        "public.account_deletion_worker_contract_version()"
    ) in sql
    assert "to service_role" in sql


def test_all_denormalized_links_have_same_owner_foreign_keys() -> None:
    sql = TENANT_RELATIONSHIP_MIGRATION.read_text(
        encoding="utf-8"
    ).lower()
    for constraint in (
        "documents_job_owner_fkey",
        "pdf_artifacts_document_owner_fkey",
        "saved_jobs_import_run_owner_fkey",
        "job_import_results_run_owner_fkey",
        "job_import_results_saved_job_owner_fkey",
        "application_packets_job_owner_fkey",
        "application_packets_source_cv_owner_fkey",
        "application_packets_tailored_cv_owner_fkey",
        "application_packets_cover_letter_owner_fkey",
        "application_apply_attempts_job_owner_fkey",
        "application_apply_attempts_packet_owner_fkey",
        "tailor_review_states_cv_owner_fkey",
        "tailor_review_states_job_owner_fkey",
    ):
        assert f"constraint {constraint}" in sql
        assert f"validate constraint {constraint}" in sql
    assert "cross-owner relationship detected; migration aborted" in sql


def test_fresh_artifact_unique_index_reconciles_existing_duplicates() -> None:
    sql = (
        REPO
        / "supabase"
        / "migrations"
        / "0008_first_run_template.sql"
    ).read_text(encoding="utf-8").lower()
    reconciliation = sql.index("with ranked_fresh_artifacts as")
    unique_index = sql.index(
        "create unique index if not exists "
        "pdf_artifacts_one_fresh_content_idx"
    )
    assert reconciliation < unique_index
    assert "row_number() over" in sql
    assert "set stale = true" in sql
