"""Database boundary for evidence-backed application status."""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
MIGRATION = REPO / "supabase" / "migrations" / "20260801180500_evidence_backed_job_status.sql"


def test_migration_blocks_new_applied_claims_without_rewriting_history():
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "update public.saved_jobs" not in sql
    assert "delete from public.saved_jobs" not in sql
    assert "before insert or update of status on public.saved_jobs" in sql
    assert "if new.status = 'applied'" in sql
    assert "old.status is distinct from 'applied'" in sql
    assert "errcode = '23514'" in sql
    assert "security definer" not in sql
    assert "set search_path = ''" in sql
    assert (
        "revoke all on function public.reject_evidence_free_applied_job_status()"
        in sql
    )


def test_application_mutable_status_enum_excludes_applied():
    import sys

    sys.path.insert(0, str(REPO / "tools" / "editor"))
    import jobs_service

    assert "applied" not in jobs_service.JOB_STATUSES
    assert jobs_service.JOB_STATUSES == ("saved", "open", "speculative", "closed")


def test_jobs_ui_never_offers_applied_as_mutable_state():
    html = (REPO / "tools" / "editor" / "static" / "jobs.html").read_text(
        encoding="utf-8"
    )
    script = (REPO / "tools" / "editor" / "static" / "jobs.js").read_text(
        encoding="utf-8"
    )
    i18n = (REPO / "tools" / "editor" / "i18n.py").read_text(encoding="utf-8")

    assert 'value="applied"' not in html
    assert 'data-filter="applied"' not in html
    assert "Unverified legacy application note" in script
    assert "Unverified legacy application note" in i18n
    assert "option[data-legacy-status]" in script
