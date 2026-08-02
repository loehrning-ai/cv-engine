"""Bounded pagination contracts for high-cardinality job workflow reads."""
from __future__ import annotations

import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

import jobs_service  # noqa: E402


def _uuid(index: int) -> str:
    return f"00000000-0000-0000-0000-{index:012d}"


def _record_ranges(fake_supabase, monkeypatch) -> list[tuple[str, int, int]]:
    query_type = type(fake_supabase.table("saved_jobs").select("*"))
    original_range = query_type.range
    calls: list[tuple[str, int, int]] = []

    def recording_range(query, start, end, *args, **kwargs):
        calls.append((query._t.name, int(start), int(end)))
        return original_range(query, start, end, *args, **kwargs)

    monkeypatch.setattr(query_type, "range", recording_range)
    return calls


def test_list_jobs_and_cover_lookup_read_every_bounded_page_in_stable_order(
    fake_supabase,
    monkeypatch,
):
    monkeypatch.setattr(jobs_service, "_sb", lambda: fake_supabase)
    monkeypatch.setattr(jobs_service, "HIGH_CARDINALITY_PAGE_SIZE", 2)
    ranges = _record_ranges(fake_supabase, monkeypatch)

    job_ids = [_uuid(index) for index in range(1, 6)]
    fake_supabase.tables["saved_jobs"] = [
        {
            "id": job_id,
            "owner_id": fake_supabase.owner_id,
            "title": f"Role {index}",
            "company": "Example",
            "status": "saved",
            "created_at": "2026-08-01T12:00:00+00:00",
        }
        for index, job_id in enumerate(job_ids, start=1)
    ]
    fake_supabase.tables["documents"] = [
        {
            "id": _uuid(100 + index),
            "owner_id": fake_supabase.owner_id,
            "type": "cover_letter",
            "job_id": job_ids[-1],
            "updated_at": "2026-08-01T13:00:00+00:00",
            "deleted_at": None,
        }
        for index in range(1, 6)
    ]

    rows = jobs_service.list_jobs()

    assert [row["id"] for row in rows] == list(reversed(job_ids))
    assert rows[0]["cover_id"] == _uuid(105)
    assert rows[0]["has_cover"] is True
    assert all(row["has_cover"] is False for row in rows[1:])
    assert [call[1:] for call in ranges if call[0] == "saved_jobs"] == [
        (0, 1),
        (2, 3),
        (4, 5),
    ]
    assert [call[1:] for call in ranges if call[0] == "documents"] == [
        (0, 1),
        (2, 3),
        (4, 5),
    ]


def test_linked_documents_and_packet_rows_page_with_unique_tie_breakers(
    fake_supabase,
    monkeypatch,
):
    monkeypatch.setattr(jobs_service, "_sb", lambda: fake_supabase)
    monkeypatch.setattr(jobs_service, "HIGH_CARDINALITY_PAGE_SIZE", 2)
    monkeypatch.setattr(jobs_service, "_public_packet", lambda row, **_kwargs: dict(row))
    ranges = _record_ranges(fake_supabase, monkeypatch)

    job_id = _uuid(1)
    document_ids = [_uuid(index) for index in range(10, 15)]
    packet_ids = [_uuid(index) for index in range(20, 25)]
    fake_supabase.tables["documents"] = [
        {
            "id": document_id,
            "owner_id": fake_supabase.owner_id,
            "type": "cv",
            "name": "Candidate",
            "yaml_blob": "name: Candidate\n",
            "job_id": job_id,
            "updated_at": "2026-08-01T13:00:00+00:00",
            "deleted_at": None,
        }
        for document_id in document_ids
    ]
    fake_supabase.tables["application_packets"] = [
        {
            "id": packet_id,
            "owner_id": fake_supabase.owner_id,
            "job_id": job_id,
            "updated_at": "2026-08-01T14:00:00+00:00",
        }
        for packet_id in packet_ids
    ]

    documents = jobs_service._linked_documents(job_id)
    packets = jobs_service._packet_rows(job_id)

    assert [row["id"] for row in documents] == list(reversed(document_ids))
    assert [row["id"] for row in packets] == list(reversed(packet_ids))
    assert [call[1:] for call in ranges if call[0] == "documents"] == [
        (0, 1),
        (2, 3),
        (4, 5),
    ]
    assert [call[1:] for call in ranges if call[0] == "application_packets"] == [
        (0, 1),
        (2, 3),
        (4, 5),
    ]


def test_packet_lineage_role_queries_page_and_resolve_ties_deterministically(
    fake_supabase,
    monkeypatch,
):
    monkeypatch.setattr(jobs_service, "_sb", lambda: fake_supabase)
    monkeypatch.setattr(jobs_service.local_demo_store, "active", lambda: False)
    monkeypatch.setattr(jobs_service, "HIGH_CARDINALITY_PAGE_SIZE", 2)
    ranges = _record_ranges(fake_supabase, monkeypatch)

    document_id = _uuid(1)
    job_id = _uuid(2)
    yaml_blob = "name: Candidate\n"
    jd_text = "Staff Engineer\n"
    fake_supabase.tables["documents"] = [
        {
            "id": document_id,
            "owner_id": fake_supabase.owner_id,
            "type": "cv",
            "yaml_blob": yaml_blob,
            "deleted_at": None,
        }
    ]
    fake_supabase.tables["saved_jobs"] = [
        {
            "id": job_id,
            "owner_id": fake_supabase.owner_id,
            "title": "Staff Engineer",
            "company": "Example",
            "jd_text": jd_text,
        }
    ]
    packet_ids = [_uuid(index) for index in range(10, 15)]
    fake_supabase.tables["application_packets"] = [
        {
            "id": packet_id,
            "owner_id": fake_supabase.owner_id,
            "job_id": job_id,
            "source_cv_id": document_id,
            "tailored_cv_id": None,
            "cover_letter_id": None,
            "source_cv_hash": jobs_service._content_hash(yaml_blob),
            "job_content_hash": jobs_service._content_hash(jd_text),
            "status": "review_applied",
            "packet_version": 1,
            "updated_at": "2026-08-01T14:00:00+00:00",
        }
        for packet_id in packet_ids
    ]

    lineage = jobs_service.packet_lineage_for_documents([document_id])

    assert lineage[document_id]["packet_id"] == packet_ids[-1]
    packet_ranges = [call[1:] for call in ranges if call[0] == "application_packets"]
    assert packet_ranges == [
        (0, 1),
        (2, 3),
        (4, 5),
        (0, 1),
        (0, 1),
    ]
    assert all((end - start + 1) <= 2 for start, end in packet_ranges)
