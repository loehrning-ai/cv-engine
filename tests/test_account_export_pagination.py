"""Account-export pagination contracts beyond PostgREST's default row cap."""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

import account_service  # noqa: E402


ROW_COUNT = 1_005
PAGE_SIZE = 500
TIED_TIMESTAMP = "2026-08-01T12:00:00+00:00"


def _uuid(index: int) -> str:
    return f"00000000-0000-0000-0000-{index:012d}"


def _expected_ranges() -> list[tuple[int, int]]:
    return [(0, 499), (500, 999), (1_000, 1_499)]


def test_export_pages_every_collection_past_1000_rows_without_truncation(
    fake_supabase,
    monkeypatch,
):
    monkeypatch.setattr(account_service, "_sb", lambda: fake_supabase)
    monkeypatch.setattr(account_service.local_demo_store, "active", lambda: False)
    monkeypatch.setattr(account_service, "ACCOUNT_EXPORT_PAGE_SIZE", PAGE_SIZE)
    monkeypatch.setattr(account_service, "_now_iso", lambda: TIED_TIMESTAMP)
    monkeypatch.setattr(
        account_service,
        "get_profile",
        lambda: {"id": fake_supabase.owner_id, "deletion_requested_at": None},
    )

    query_type = type(fake_supabase.table("documents").select("*"))
    original_range = query_type.range
    original_execute = query_type.execute
    range_calls: list[tuple[str, int, int]] = []

    def recording_range(query, start, end, *args, **kwargs):
        range_calls.append((query._t.name, int(start), int(end)))
        return original_range(query, start, end, *args, **kwargs)

    def server_capped_execute(query):
        response = original_execute(query)
        if query._op == "select" and query._range is None:
            response.data = response.data[:1_000]
        return response

    monkeypatch.setattr(query_type, "range", recording_range)
    monkeypatch.setattr(query_type, "execute", server_capped_execute)

    document_ids = [_uuid(index) for index in range(1, ROW_COUNT + 1)]
    job_ids = [_uuid(index) for index in range(2_001, 2_001 + ROW_COUNT)]
    run_ids = [_uuid(index) for index in range(4_001, 4_001 + ROW_COUNT)]
    result_ids = [_uuid(index) for index in range(6_001, 6_001 + ROW_COUNT)]
    packet_ids = [_uuid(index) for index in range(8_001, 8_001 + ROW_COUNT)]

    fake_supabase.tables["documents"] = [
        {
            "id": document_id,
            "owner_id": fake_supabase.owner_id,
            "type": "cv",
            "name": f"CV {index}",
            "yaml_blob": f"name: Candidate {index}\n",
            "deleted_at": None,
            "created_at": TIED_TIMESTAMP,
            "updated_at": TIED_TIMESTAMP,
        }
        for index, document_id in enumerate(document_ids)
    ]
    fake_supabase.tables["saved_jobs"] = [
        {
            "id": job_id,
            "owner_id": fake_supabase.owner_id,
            "title": f"Role {index}",
            "company": "Example",
            "status": "saved",
            "created_at": TIED_TIMESTAMP,
            "updated_at": TIED_TIMESTAMP,
        }
        for index, job_id in enumerate(job_ids)
    ]
    fake_supabase.tables["job_import_runs"] = [
        {
            "id": run_id,
            "owner_id": fake_supabase.owner_id,
            "provider": "fixture",
            "status": "completed",
            "started_at": TIED_TIMESTAMP,
        }
        for run_id in run_ids
    ]
    fake_supabase.tables["job_import_results"] = [
        {
            "id": result_id,
            "owner_id": fake_supabase.owner_id,
            "import_run_id": run_ids[index],
            "title": f"Imported role {index}",
            "company": "Example",
            "created_at": TIED_TIMESTAMP,
            "updated_at": TIED_TIMESTAMP,
        }
        for index, result_id in enumerate(result_ids)
    ]
    fake_supabase.tables["application_packets"] = [
        {
            "id": packet_id,
            "owner_id": fake_supabase.owner_id,
            "job_id": job_ids[index],
            "source_cv_id": document_ids[index],
            "tailored_cv_id": None,
            "cover_letter_id": None,
            "status": "packet_created",
            "archived_at": None,
            "created_at": TIED_TIMESTAMP,
            "updated_at": TIED_TIMESTAMP,
        }
        for index, packet_id in enumerate(packet_ids)
    ]
    fake_supabase.tables["user_llm_keys"] = [
        {
            "id": _uuid(10_001 + index),
            "owner_id": fake_supabase.owner_id,
            "provider": f"provider-{index:04d}",
            "last4": f"{index:04d}"[-4:],
            "created_at": TIED_TIMESTAMP,
            "updated_at": TIED_TIMESTAMP,
            "ciphertext": "must-not-export",
        }
        for index in range(ROW_COUNT)
    ]

    first = account_service.export_account_data()
    first_range_calls = list(range_calls)
    range_calls.clear()
    second = account_service.export_account_data()

    assert first == second
    assert [row["id"] for row in first["documents"]] == list(reversed(document_ids))
    assert [row["id"] for row in first["saved_jobs"]] == list(reversed(job_ids))
    assert [row["id"] for row in first["job_import_runs"]] == list(reversed(run_ids))
    assert [row["id"] for row in first["job_import_results"]] == list(reversed(result_ids))
    assert [row["id"] for row in first["application_packets"]] == list(reversed(packet_ids))
    assert [row["provider"] for row in first["llm_keys"]] == [
        f"provider-{index:04d}" for index in range(ROW_COUNT)
    ]
    assert all("ciphertext" not in row for row in first["llm_keys"])

    expected_tables = {
        "documents",
        "saved_jobs",
        "job_import_runs",
        "job_import_results",
        "application_packets",
        "user_llm_keys",
    }
    for calls in (first_range_calls, range_calls):
        by_table: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for table, start, end in calls:
            by_table[table].append((start, end))
        assert set(by_table) == expected_tables
        assert all(ranges == _expected_ranges() for ranges in by_table.values())
