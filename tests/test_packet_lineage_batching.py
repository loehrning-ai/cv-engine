"""Bounded-query contracts for high-cardinality document lineage reads."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

import jobs_service  # noqa: E402


def test_packet_lineage_batches_every_in_filter_and_bulk_loads_hashes(
    fake_supabase,
    monkeypatch,
):
    document_ids = [str(uuid.uuid4()) for _ in range(205)]
    job_ids = [str(uuid.uuid4()) for _ in document_ids]
    documents = fake_supabase.tables.setdefault("documents", [])
    jobs = fake_supabase.tables.setdefault("saved_jobs", [])
    packets = fake_supabase.tables.setdefault("application_packets", [])

    for index, (document_id, job_id) in enumerate(zip(document_ids, job_ids, strict=True)):
        yaml_blob = f"name: Candidate {index}\n"
        jd_text = f"Role {index}\n"
        documents.append(
            {
                "id": document_id,
                "owner_id": fake_supabase.owner_id,
                "type": "cv",
                "yaml_blob": yaml_blob,
                "deleted_at": None,
            }
        )
        jobs.append(
            {
                "id": job_id,
                "owner_id": fake_supabase.owner_id,
                "title": f"Role {index}",
                "company": "Example",
                "jd_text": jd_text,
            }
        )
        packets.append(
            {
                "id": str(uuid.uuid4()),
                "owner_id": fake_supabase.owner_id,
                "job_id": job_id,
                "source_cv_id": document_id,
                "tailored_cv_id": None,
                "cover_letter_id": None,
                "source_cv_hash": jobs_service._content_hash(yaml_blob),
                "job_content_hash": jobs_service._content_hash(jd_text),
                "status": "review_applied",
                "packet_version": 1,
                "updated_at": index,
            }
        )

    query_type = type(fake_supabase.table("application_packets").select("*"))
    original_in = query_type.in_
    original_eq = query_type.eq
    in_calls: list[tuple[str, str, tuple[str, ...]]] = []
    document_id_eq_calls: list[str] = []

    def recording_in(query, column, values, *args, **kwargs):
        in_calls.append((query._t.name, column, tuple(values)))
        return original_in(query, column, values, *args, **kwargs)

    def recording_eq(query, column, value):
        if query._t.name == "documents" and column == "id":
            document_id_eq_calls.append(str(value))
        return original_eq(query, column, value)

    monkeypatch.setattr(query_type, "in_", recording_in)
    monkeypatch.setattr(query_type, "eq", recording_eq)
    monkeypatch.setattr(jobs_service, "_sb", lambda: fake_supabase)
    monkeypatch.setattr(jobs_service.local_demo_store, "active", lambda: False)

    result = jobs_service.packet_lineage_for_documents(document_ids)

    assert set(result) == set(document_ids)
    assert all(len(values) <= jobs_service.PACKET_LINEAGE_BATCH_SIZE for _, _, values in in_calls)
    assert len([call for call in in_calls if call[:2] == ("application_packets", "source_cv_id")]) == 3
    assert len([call for call in in_calls if call[:2] == ("application_packets", "tailored_cv_id")]) == 3
    assert len([call for call in in_calls if call[:2] == ("application_packets", "cover_letter_id")]) == 3
    assert len([call for call in in_calls if call[:2] == ("saved_jobs", "id")]) == 3
    assert len([call for call in in_calls if call[:2] == ("documents", "id")]) == 3
    assert document_id_eq_calls == []
