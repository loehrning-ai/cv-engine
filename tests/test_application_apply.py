from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest


def _id() -> str:
    return str(uuid.uuid4())


def _confirmations() -> dict:
    return {
        "confirm": True,
        "confirmations": {
            "reviewed_packet_confirmed": True,
            "profile_answers_complete": True,
            "legal_answers_confirmed": True,
            "visa_answers_confirmed": True,
        },
    }


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _seeded_pdf_rows_have_backing_objects(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module.apply_svc.pdf_store,
        "r2_object_exists",
        lambda key: isinstance(key, str) and bool(key.strip()),
    )


def _seed_application(fake_supabase, *, source_url: str = "https://jobs.example.com/apply") -> dict:
    job_id = _id()
    source_cv_id = _id()
    tailored_cv_id = _id()
    cover_letter_id = _id()
    packet_id = _id()
    jd_text = "Title: Senior Data Engineer\nCompany: Acme GmbH\nBuild data systems."
    source_name = "Tim Loehr"
    source_yaml = f"name: {source_name}\nexperience:\n  - role: Data Engineer\n"
    tailored_yaml = "name: Tim Loehr\nexperience:\n  - role: Senior Data Engineer\n"
    letter_yaml = (
        "company: Acme GmbH\n"
        "role: Senior Data Engineer\n"
        f"source_cv_id: {source_cv_id}\n"
    )
    fake_supabase.tables.setdefault("saved_jobs", []).append(
        {
            "id": job_id,
            "owner_id": fake_supabase.owner_id,
            "title": "Senior Data Engineer",
            "company": "Acme GmbH",
            "status": "saved",
            "jd_text": jd_text,
            "jd_json": {"title": "Senior Data Engineer", "company": "Acme GmbH"},
            "source_url": source_url,
            "source_metadata": {},
            "verification_status": "user_provided",
            "content_hash": _hash(jd_text),
            "updated_at": 10,
        }
    )
    docs = fake_supabase.tables.setdefault("documents", [])
    docs.append(
        {
            "id": source_cv_id,
            "owner_id": fake_supabase.owner_id,
            "type": "cv",
            "name": "Source CV",
            "job_id": None,
            "yaml_blob": source_yaml,
            "deleted_at": None,
            "updated_at": 9,
        }
    )
    docs.append(
        {
            "id": tailored_cv_id,
            "owner_id": fake_supabase.owner_id,
            "type": "cv",
            "name": "Tailored CV",
            "job_id": job_id,
            "yaml_blob": tailored_yaml,
            "deleted_at": None,
            "updated_at": 11,
        }
    )
    docs.append(
        {
            "id": cover_letter_id,
            "owner_id": fake_supabase.owner_id,
            "type": "cover_letter",
            "name": "Cover Letter",
            "job_id": job_id,
            "yaml_blob": letter_yaml,
            "deleted_at": None,
            "updated_at": 12,
        }
    )
    fake_supabase.tables.setdefault("application_packets", []).append(
        {
            "id": packet_id,
            "owner_id": fake_supabase.owner_id,
            "job_id": job_id,
            "source_cv_id": source_cv_id,
            "tailored_cv_id": tailored_cv_id,
            "cover_letter_id": cover_letter_id,
            "status": "packet_created",
            "review_status": "reviewed",
            "source_cv_hash": _hash(source_yaml),
            "job_content_hash": _hash(jd_text),
            "tailored_cv_hash": _hash(tailored_yaml),
            "cover_letter_hash": _hash(letter_yaml),
            "cover_letter_review_status": "confirmed",
            "cover_letter_review_confirmed_at": "2026-07-02T12:00:00+00:00",
            "cover_letter_review_id": "review-fixture",
            "prompt_version": "jobs_packet.v1",
            "score_system_id": "semantic_fit",
            "score_snapshot": {"score": 91},
            "packet_version": 1,
            "profile_answers_complete": True,
            "legal_answers_confirmed": True,
            "visa_answers_confirmed": True,
            "updated_at": 13,
        }
    )
    import cover_letter_service as cl_service
    import pdf_store

    tailored_pdf_hash = pdf_store.persisted_cv_content_hash(tailored_yaml)
    letter_blob = cl_service.normalized_blob_for_row(docs[-1])
    letter_cache_key_text = cl_service.serialize_blob(letter_blob) + "\x00" + source_name
    letter_pdf_hash = pdf_store.cover_letter_content_hash(letter_cache_key_text)
    artifacts = fake_supabase.tables.setdefault("pdf_artifacts", [])
    artifacts.append(
        {
            "id": _id(),
            "owner_id": fake_supabase.owner_id,
            "document_id": tailored_cv_id,
            "r2_key": f"{fake_supabase.owner_id}/{tailored_cv_id}/{tailored_pdf_hash}.pdf",
            "content_hash": tailored_pdf_hash,
            "stale": False,
            "page_count": 1,
            "updated_at": 14,
        }
    )
    artifacts.append(
        {
            "id": _id(),
            "owner_id": fake_supabase.owner_id,
            "document_id": cover_letter_id,
            "r2_key": f"{fake_supabase.owner_id}/{cover_letter_id}/{letter_pdf_hash}.pdf",
            "content_hash": letter_pdf_hash,
            "stale": False,
            "page_count": 1,
            "updated_at": 15,
        }
    )
    return {
        "job_id": job_id,
        "source_cv_id": source_cv_id,
        "tailored_cv_id": tailored_cv_id,
        "cover_letter_id": cover_letter_id,
        "packet_id": packet_id,
    }


class TestApplyHostClassifier:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/apply",
            "http://localhost:8000/apply",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.5/apply",
            "http://192.168.1.5/apply",
            "http://[::1]/apply",
            "http://[fc00::1]/apply",
            "http://[::ffff:127.0.0.1]/apply",
            "http://metadata.google.internal/computeMetadata/v1",
            "http://2130706433/apply",
            "http://0177.0.0.1/apply",
            "http://0x7f.0.0.1/apply",
            "http://127.1/apply",
        ],
    )
    def test_private_and_metadata_hosts_are_invalid(self, server_module, url):
        target = server_module.apply_svc.classify_apply_url(url)

        assert target["classification"] == "invalid"
        assert target["open_allowed"] is False
        assert target["dry_run_allowed"] is False
        assert target["reason_code"] == "invalid_application_url"

    def test_linkedin_is_manual_open_only(self, server_module, monkeypatch):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_SAFE_FIXTURE_HOSTS", "fixture.apply.test")
        target = server_module.apply_svc.classify_apply_url("https://www.linkedin.com/jobs/view/123")

        assert target["classification"] == "linkedin"
        assert target["method"] == "manual_open"
        assert target["open_allowed"] is True
        assert target["dry_run_allowed"] is False
        assert target["submit_allowed"] is False
        assert target["automation_allowed"] is False
        assert target["reason_code"] == "linkedin_manual_open_only"

    def test_unknown_is_manual_open_only(self, server_module):
        target = server_module.apply_svc.classify_apply_url("https://company.example/apply/123")

        assert target["classification"] == "unknown"
        assert target["method"] == "manual_open"
        assert target["open_allowed"] is True
        assert target["dry_run_allowed"] is False
        assert target["submit_allowed"] is False
        assert target["automation_allowed"] is False
        assert target["reason_code"] == "unknown_manual_open_only"

    def test_query_urls_are_not_silently_stripped_and_opened(self, server_module):
        target = server_module.apply_svc.classify_apply_url("https://jobs.example.com/apply?token=secret")

        assert target["classification"] == "invalid"
        assert target["open_allowed"] is False
        assert target["dry_run_allowed"] is False
        assert target["reason_code"] == "invalid_application_url"

    def test_classification_strips_secret_shaped_url_paths(self, server_module):
        target = server_module.apply_svc.classify_apply_url("https://jobs.example.com/apply/token/secret")

        assert target["classification"] == "invalid"
        assert target["open_allowed"] is False
        assert target["url"] == "https://jobs.example.com"

    def test_classification_preserves_legitimate_key_words_in_url_paths(self, server_module):
        target = server_module.apply_svc.classify_apply_url("https://jobs.example.com/apply/key-account-manager")

        assert target["classification"] == "unknown"
        assert target["open_allowed"] is True
        assert target["url"] == "https://jobs.example.com/apply/key-account-manager"

    def test_safe_fixture_host_is_explicit(self, server_module, monkeypatch):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_SAFE_FIXTURE_HOSTS", "fixture.apply.test")

        fixture = server_module.apply_svc.classify_apply_url("https://fixture.apply.test/apply")
        not_fixture = server_module.apply_svc.classify_apply_url("https://other-fixture.apply.test/apply")

        assert fixture["classification"] == "safe_fixture"
        assert fixture["dry_run_allowed"] is True
        assert not_fixture["dry_run_allowed"] is False

    def test_linkedin_cannot_be_enabled_as_fixture(self, server_module, monkeypatch):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_SAFE_FIXTURE_HOSTS", "www.linkedin.com,linkedin.com")

        target = server_module.apply_svc.classify_apply_url("https://www.linkedin.com/jobs/view/123")

        assert target["classification"] == "linkedin"
        assert target["dry_run_allowed"] is False
        assert target["reason_code"] == "linkedin_manual_open_only"

    def test_linkedin_shortlinks_cannot_be_enabled_as_fixtures(self, server_module, monkeypatch):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_SAFE_FIXTURE_HOSTS", "lnkd.in")

        target = server_module.apply_svc.classify_apply_url("https://lnkd.in/example")

        assert target["classification"] == "linkedin"
        assert target["dry_run_allowed"] is False
        assert target["reason_code"] == "linkedin_manual_open_only"

    def test_invalid_url_is_redacted_before_attempt_logging(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        seeded = _seed_application(
            fake_supabase,
            source_url=(
                "https://" + "user" + ":" + "pass"
                + "@fixture.apply.test/apply?li_at=secret"
            ),
        )

        res = client.post(f"/api/jobs/{seeded['job_id']}/apply/open", json=_confirmations())

        assert res.status_code == 409
        attempt = fake_supabase.tables["application_apply_attempts"][0]
        assert attempt["reason_code"] == "invalid_application_url"
        assert attempt["target_url"] == "https://fixture.apply.test/apply"
        assert "pass" not in attempt["target_url"]
        assert "li_at" not in attempt["target_url"]

    def test_known_ats_hosts_are_manual_open_only(self, server_module):
        urls = {
            "lever": "https://jobs.lever.co/acme/123",
            "greenhouse": "https://job-boards.greenhouse.io/acme/jobs/123",
            "workday": "https://acme.wd1.myworkdayjobs.com/jobs/job/123",
        }

        for label, url in urls.items():
            target = server_module.apply_svc.classify_apply_url(url)
            assert target["classification"] == "known_ats", label
            assert target["open_allowed"] is True
            assert target["dry_run_allowed"] is False
            assert target["submit_allowed"] is False
            assert target["reason_code"] == "known_ats_manual_open_only"

    def test_known_ats_hosts_cannot_be_enabled_as_fixtures(self, server_module, monkeypatch):
        monkeypatch.setenv(
            "ONEPAGER_EXTERNAL_APPLY_SAFE_FIXTURE_HOSTS",
            "jobs.lever.co,job-boards.greenhouse.io",
        )

        for url in (
            "https://jobs.lever.co/acme/123",
            "https://job-boards.greenhouse.io/acme/jobs/123",
        ):
            target = server_module.apply_svc.classify_apply_url(url)
            assert target["classification"] == "known_ats"
            assert target["dry_run_allowed"] is False
            assert target["reason_code"] == "known_ats_manual_open_only"


class TestApplyReadiness:
    def test_readiness_requires_reviewed_packet(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)
        fake_supabase.tables["application_packets"].clear()

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert [b["code"] for b in body["blockers"]] == ["reviewed_packet_missing"]
        assert body["actions"]["submit"] is False

    def test_readiness_blocks_private_application_url(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase, source_url="http://127.0.0.1:8000/apply")

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert body["target"]["open_allowed"] is False
        assert body["target"]["reason_code"] == "invalid_application_url"
        assert "missing_application_url" in {b["code"] for b in body["blockers"]}

    def test_readiness_uses_packet_and_linked_document_state(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is True
        assert body["packet"]["id"] == seeded["packet_id"]
        assert body["packet"]["review_status"] == "reviewed"
        assert body["packet"]["cover_letter_review_status"] == "confirmed"
        assert body["packet"]["cover_letter_review_confirmed_at"] == "2026-07-02T12:00:00+00:00"
        assert body["packet"]["cover_letter_review_id"] == "review-fixture"
        assert body["target"]["reason_code"] == "unknown_manual_open_only"
        assert body["packet"]["job_content_hash"]

    def test_readiness_ignores_archived_packets_when_selecting_default(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)
        archived = dict(fake_supabase.tables["application_packets"][0])
        archived.update({
            "id": _id(),
            "status": "archived",
            "archived_at": "2026-07-02T12:00:00+00:00",
            "updated_at": 99,
        })
        fake_supabase.tables["application_packets"].append(archived)

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is True
        assert body["packet"]["id"] == seeded["packet_id"]

    def test_readiness_blocks_explicit_archived_packet(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)
        packet = fake_supabase.tables["application_packets"][0]
        packet["status"] = "archived"
        packet["archived_at"] = "2026-07-02T12:00:00+00:00"

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness?packet_id={seeded['packet_id']}")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert "packet_archived" in {b["code"] for b in body["blockers"]}

    def test_readiness_blocks_stale_job_description_hash(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)
        fake_supabase.tables["saved_jobs"][0]["jd_text"] = "Changed job description."

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert "job_description_stale" in {b["code"] for b in body["blockers"]}

    def test_readiness_blocks_stale_source_cv_hash(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)
        source = next(d for d in fake_supabase.tables["documents"] if d["id"] == seeded["source_cv_id"])
        source["yaml_blob"] = "name: Changed\n"

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert "source_cv_stale" in {b["code"] for b in body["blockers"]}

    def test_readiness_blocks_stale_tailored_cv_hash(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)
        tailored = next(d for d in fake_supabase.tables["documents"] if d["id"] == seeded["tailored_cv_id"])
        tailored["yaml_blob"] = "name: Tim Loehr\nexperience:\n  - role: Changed\n"

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert "tailored_cv_stale" in {b["code"] for b in body["blockers"]}

    def test_readiness_blocks_stale_cover_letter_hash(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)
        letter = next(d for d in fake_supabase.tables["documents"] if d["id"] == seeded["cover_letter_id"])
        letter["yaml_blob"] = "company: Acme GmbH\nrole: Changed\n"

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert "cover_letter_stale" in {b["code"] for b in body["blockers"]}

    @pytest.mark.parametrize(
        "doc_key,expected_code",
        [
            ("source_cv_id", "source_cv_missing"),
            ("tailored_cv_id", "tailored_cv_missing"),
            ("cover_letter_id", "cover_letter_missing"),
        ],
    )
    def test_readiness_treats_soft_deleted_packet_documents_as_missing(
        self, server_module, client, fake_supabase, doc_key, expected_code
    ):
        seeded = _seed_application(fake_supabase)
        doc = next(d for d in fake_supabase.tables["documents"] if d["id"] == seeded[doc_key])
        doc["deleted_at"] = "2026-07-03T12:00:00+00:00"

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        codes = {b["code"] for b in body["blockers"]}
        assert body["ready"] is False
        assert expected_code in codes
        assert expected_code.replace("_missing", "_hash_missing") not in codes

    def test_readiness_blocks_incomplete_review_applied_packet(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)
        packet = fake_supabase.tables["application_packets"][0]
        packet["status"] = "review_applied"
        packet["review_status"] = "reviewed"
        packet["cover_letter_review_status"] = "confirmed"
        packet["cover_letter_review_confirmed_at"] = "2026-07-02T12:00:00+00:00"
        packet["cover_letter_review_id"] = "review-fixture"

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness?packet_id={seeded['packet_id']}")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert body["packet"]["id"] == seeded["packet_id"]
        assert "packet_incomplete" in {b["code"] for b in body["blockers"]}

    def test_readiness_requires_tailored_cv_pdf(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)
        fake_supabase.tables["pdf_artifacts"] = [
            row
            for row in fake_supabase.tables["pdf_artifacts"]
            if row["document_id"] != seeded["tailored_cv_id"]
        ]

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert "tailored_cv_pdf_missing" in {b["code"] for b in body["blockers"]}

    def test_readiness_requires_cover_letter_pdf(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)
        fake_supabase.tables["pdf_artifacts"] = [
            row
            for row in fake_supabase.tables["pdf_artifacts"]
            if row["document_id"] != seeded["cover_letter_id"]
        ]

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert "cover_letter_pdf_missing" in {b["code"] for b in body["blockers"]}

    def test_readiness_degrades_when_stored_cover_letter_fails_envelope_validation(
        self, server_module, client, fake_supabase
    ):
        """A legacy blob must produce a blocker, never a 400.

        Stored YAML is parsed leniently on read, but the PDF cache key re-runs
        the strict envelope validation added with the persisted envelope. A
        letter saved before those caps existed can carry an invisible format
        character (U+200B here), which makes `serialize_blob` raise
        CoverLetterError. If that escapes, GET .../apply/readiness returns 400
        and the whole Application Studio panel dies for that job.
        """
        seeded = _seed_application(fake_supabase)
        letter = next(
            row
            for row in fake_supabase.tables["documents"]
            if row["id"] == seeded["cover_letter_id"]
        )
        letter["yaml_blob"] = "body: |\n  Dear team,​ hello.\n"

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200, res.get_json()
        body = res.get_json()
        assert body["ready"] is False
        assert "cover_letter_pdf_missing" in {b["code"] for b in body["blockers"]}

    def test_readiness_accepts_multi_page_cover_letter_artifact(
        self, server_module, client, fake_supabase
    ):
        seeded = _seed_application(fake_supabase)
        artifact = next(
            row for row in fake_supabase.tables["pdf_artifacts"]
            if row["document_id"] == seeded["cover_letter_id"]
        )
        artifact["page_count"] = 2

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is True
        assert "cover_letter_pdf_missing" not in {b["code"] for b in body["blockers"]}

    def test_readiness_rejects_cross_document_pdf_object_key(
        self, server_module, client, fake_supabase
    ):
        seeded = _seed_application(fake_supabase)
        artifact = next(
            row for row in fake_supabase.tables["pdf_artifacts"]
            if row["document_id"] == seeded["tailored_cv_id"]
        )
        artifact["r2_key"] = (
            f"{fake_supabase.owner_id}/{seeded['source_cv_id']}/"
            f"{artifact['content_hash']}.pdf"
        )

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert "tailored_cv_pdf_missing" in {b["code"] for b in body["blockers"]}

    def test_readiness_rejects_cv_appearance_drift_without_yaml_drift(
        self, server_module, client, fake_supabase
    ):
        seeded = _seed_application(fake_supabase)
        tailored = next(
            row for row in fake_supabase.tables["documents"]
            if row["id"] == seeded["tailored_cv_id"]
        )
        tailored["accent"] = "#2563eb"

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        codes = {b["code"] for b in body["blockers"]}
        assert body["ready"] is False
        assert "tailored_cv_pdf_missing" in codes
        assert "tailored_cv_stale" not in codes

    def test_readiness_rejects_cover_pdf_after_sender_render_input_drift(
        self, server_module, client, fake_supabase
    ):
        seeded = _seed_application(fake_supabase)
        source = next(
            row for row in fake_supabase.tables["documents"]
            if row["id"] == seeded["source_cv_id"]
        )
        source["yaml_blob"] = source["yaml_blob"].replace("Tim Loehr", "Tina Loehr")
        fake_supabase.tables["application_packets"][0]["source_cv_hash"] = _hash(
            source["yaml_blob"]
        )

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        codes = {b["code"] for b in body["blockers"]}
        assert body["ready"] is False
        assert "cover_letter_pdf_missing" in codes
        assert "source_cv_stale" not in codes

    def test_readiness_rejects_multiple_fresh_artifacts_for_one_document(
        self, server_module, client, fake_supabase
    ):
        seeded = _seed_application(fake_supabase)
        conflicting_hash = "f" * 64
        fake_supabase.tables["pdf_artifacts"].append(
            {
                "id": _id(),
                "owner_id": fake_supabase.owner_id,
                "document_id": seeded["tailored_cv_id"],
                "r2_key": (
                    f"{fake_supabase.owner_id}/{seeded['tailored_cv_id']}/"
                    f"{conflicting_hash}.pdf"
                ),
                "content_hash": conflicting_hash,
                "stale": False,
                "page_count": 1,
            }
        )

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert "tailored_cv_pdf_missing" in {b["code"] for b in body["blockers"]}

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("r2_key", ""),
            ("content_hash", ""),
            ("content_hash", "f" * 64),
            ("page_count", 2),
        ],
    )
    def test_readiness_rejects_metadata_only_or_non_one_page_pdf_artifacts(
        self, server_module, client, fake_supabase, field, value
    ):
        seeded = _seed_application(fake_supabase)
        artifact = next(
            row for row in fake_supabase.tables["pdf_artifacts"]
            if row["document_id"] == seeded["tailored_cv_id"]
        )
        artifact[field] = value

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert "tailored_cv_pdf_missing" in {b["code"] for b in body["blockers"]}

    def test_readiness_rejects_pdf_metadata_without_backing_r2_object(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setattr(server_module.apply_svc.pdf_store, "r2_object_exists", lambda _key: False)
        seeded = _seed_application(fake_supabase)

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        codes = {b["code"] for b in body["blockers"]}
        assert "tailored_cv_pdf_missing" in codes
        assert "cover_letter_pdf_missing" in codes

    def test_readiness_requires_stored_profile_and_legal_answers(
        self, server_module, client, fake_supabase
    ):
        seeded = _seed_application(fake_supabase)
        packet = fake_supabase.tables["application_packets"][0]
        packet["profile_answers_complete"] = False
        packet["legal_answers_confirmed"] = False
        packet["visa_answers_confirmed"] = False

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert {
            "profile_answers_incomplete",
            "legal_answers_unconfirmed",
            "visa_answers_unconfirmed",
        }.issubset({b["code"] for b in body["blockers"]})

    def test_readiness_blocks_stale_packet_lineage(self, server_module, client, fake_supabase):
        seeded = _seed_application(fake_supabase)
        packet = fake_supabase.tables["application_packets"][0]
        packet["prompt_version"] = "jobs_packet.v0"
        packet["packet_version"] = 0
        packet["score_system_id"] = ""

        res = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness")

        assert res.status_code == 200
        body = res.get_json()
        assert body["ready"] is False
        assert {
            "packet_prompt_version_stale",
            "packet_version_stale",
            "score_system_missing",
        }.issubset({b["code"] for b in body["blockers"]})


class TestApplyActions:
    def test_default_disabled_logs_blocked_attempt(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.delenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", raising=False)
        monkeypatch.delenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", raising=False)
        seeded = _seed_application(fake_supabase)

        res = client.post(f"/api/jobs/{seeded['job_id']}/apply/open", json=_confirmations())

        assert res.status_code == 403
        body = res.get_json()
        assert body["code"] == "external_apply_disabled"
        attempts = fake_supabase.tables["application_apply_attempts"]
        assert len(attempts) == 1
        assert attempts[0]["status"] == "blocked"
        assert attempts[0]["reason_code"] == "external_apply_disabled"
        assert attempts[0]["submitted"] is False
        assert attempts[0]["automation_performed"] is False

    def test_kill_switch_fails_closed_when_enabled_without_explicit_zero(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.delenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", raising=False)
        seeded = _seed_application(fake_supabase)

        res = client.post(f"/api/jobs/{seeded['job_id']}/apply/open", json=_confirmations())

        assert res.status_code == 403
        assert res.get_json()["code"] == "external_apply_kill_switch"
        assert fake_supabase.tables["application_apply_attempts"][0]["reason_code"] == "external_apply_kill_switch"

    def test_confirmation_required_before_manual_open(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        seeded = _seed_application(fake_supabase, source_url="https://www.linkedin.com/jobs/view/123")

        res = client.post(f"/api/jobs/{seeded['job_id']}/apply/open", json={})

        assert res.status_code == 400
        body = res.get_json()
        assert body["code"] == "confirmation_required"
        assert set(body["missing_confirmations"]) == {
            "user_confirmed",
            "reviewed_packet_confirmed",
            "profile_answers_complete",
            "legal_answers_confirmed",
            "visa_answers_confirmed",
        }
        assert fake_supabase.tables["application_apply_attempts"][0]["reason_code"] == "confirmation_required"

    def test_linkedin_manual_open_never_automates(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        seeded = _seed_application(fake_supabase, source_url="https://www.linkedin.com/jobs/view/123")

        res = client.post(f"/api/jobs/{seeded['job_id']}/apply/open", json=_confirmations())

        assert res.status_code == 202
        body = res.get_json()
        assert body["status"] == "manual_open_ready"
        assert body["submitted"] is False
        assert body["automation_performed"] is False
        assert body["target"]["classification"] == "linkedin"
        assert body["target"]["dry_run_allowed"] is False
        assert body["attempt"]["status"] == "manual_open_ready"

        history = client.get(f"/api/jobs/{seeded['job_id']}/apply/readiness").get_json()["attempts"]
        assert len(history) == 1
        assert history[0]["id"] == body["attempt"]["id"]
        assert history[0]["status"] == "manual_open_ready"
        assert history[0]["submitted"] is False

    def test_manual_open_persists_explicit_packet_readiness_confirmations(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        seeded = _seed_application(fake_supabase)
        packet = fake_supabase.tables["application_packets"][0]
        packet["profile_answers_complete"] = False
        packet["legal_answers_confirmed"] = False
        packet["visa_answers_confirmed"] = False

        res = client.post(
            f"/api/jobs/{seeded['job_id']}/apply/open",
            json={**_confirmations(), "packet_id": seeded["packet_id"]},
        )

        assert res.status_code == 202, res.get_data(as_text=True)
        assert res.get_json()["status"] == "manual_open_ready"
        assert packet["profile_answers_complete"] is True
        assert packet["legal_answers_confirmed"] is True
        assert packet["visa_answers_confirmed"] is True
        assert fake_supabase.tables["application_apply_attempts"][0]["readiness"]["ready"] is True

    def test_manual_open_finalizer_rejects_target_race_without_success_evidence(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        seeded = _seed_application(fake_supabase)
        original = fake_supabase._finalize_application_apply_action

        def race_target(params):
            job = fake_supabase.tables["saved_jobs"][0]
            job["source_url"] = "https://changed.example/apply"
            # Preserve the revision deliberately: the finalizer must compare
            # the target inputs themselves, not trust a caller-writable clock.
            return original(params)

        monkeypatch.setattr(
            fake_supabase,
            "_finalize_application_apply_action",
            race_target,
        )

        res = client.post(
            f"/api/jobs/{seeded['job_id']}/apply/open",
            json=_confirmations(),
        )

        assert res.status_code == 409
        body = res.get_json()
        assert body["code"] == "apply_state_changed"
        assert body["reason_code"] == "target_changed"
        assert not any(
            row.get("status") == "manual_open_ready"
            for row in fake_supabase.tables["application_apply_attempts"]
        )

    def test_manual_open_finalizer_rejects_document_race_without_success_evidence(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        seeded = _seed_application(fake_supabase)
        original = fake_supabase._finalize_application_apply_action

        def race_document(params):
            tailored = next(
                row for row in fake_supabase.tables["documents"]
                if row["id"] == seeded["tailored_cv_id"]
            )
            tailored["accent"] = "#2563eb"
            return original(params)

        monkeypatch.setattr(
            fake_supabase,
            "_finalize_application_apply_action",
            race_document,
        )

        res = client.post(
            f"/api/jobs/{seeded['job_id']}/apply/open",
            json=_confirmations(),
        )

        assert res.status_code == 409
        body = res.get_json()
        assert body["code"] == "apply_state_changed"
        assert body["reason_code"] == "document_changed"
        assert not any(
            row.get("status") == "manual_open_ready"
            for row in fake_supabase.tables["application_apply_attempts"]
        )

    def test_manual_open_finalizer_transport_failure_is_fail_closed(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        seeded = _seed_application(fake_supabase)

        def fail_finalizer(_params):
            raise TimeoutError("database lock timeout")

        monkeypatch.setattr(
            fake_supabase,
            "_finalize_application_apply_action",
            fail_finalizer,
        )

        res = client.post(
            f"/api/jobs/{seeded['job_id']}/apply/open",
            json=_confirmations(),
        )

        assert res.status_code == 500
        assert res.get_json()["code"] == "apply_finalize_failed"
        assert fake_supabase.tables.get("application_apply_attempts", []) == []

    def test_dry_run_rejects_unknown_hosts(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_SAFE_FIXTURE_HOSTS", "fixture.apply.test")
        seeded = _seed_application(fake_supabase, source_url="https://company.example/apply")

        res = client.post(f"/api/jobs/{seeded['job_id']}/apply/dry-run", json=_confirmations())

        assert res.status_code == 403
        assert res.get_json()["code"] == "dry_run_not_allowed"
        assert fake_supabase.tables["application_apply_attempts"][0]["reason_code"] == "dry_run_not_allowed"

    def test_dry_run_fixture_returns_field_report_and_logs(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_SAFE_FIXTURE_HOSTS", "fixture.apply.test")
        seeded = _seed_application(fake_supabase, source_url="https://fixture.apply.test/apply")
        payload = {
            **_confirmations(),
            "fixture_fields": [
                {"name": "email", "label": "Email", "type": "email", "required": True},
                {"name": "resume", "label": "Resume", "type": "file", "required": True},
            ],
        }

        res = client.post(f"/api/jobs/{seeded['job_id']}/apply/dry-run", json=payload)

        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "dry_run_completed"
        assert body["submitted"] is False
        assert body["automation_performed"] is False
        assert [field["name"] for field in body["fields"]] == ["email", "resume"]
        attempt = fake_supabase.tables["application_apply_attempts"][0]
        assert attempt["status"] == "dry_run_completed"
        assert attempt["target_host"] == "fixture.apply.test"
        assert attempt["target_url"] == "https://fixture.apply.test/apply"
        assert attempt["metadata"]["field_count"] == 2

    def test_apply_actions_have_apply_specific_rate_limit(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        seeded = _seed_application(fake_supabase)
        monkeypatch.setattr(server_module.apply_svc, "APPLY_ACTION_RATE_LIMIT_PER_MINUTE", 1)
        server_module.apply_svc._APPLY_ACTION_BUCKETS.clear()

        first = client.post(f"/api/jobs/{seeded['job_id']}/apply/open", json=_confirmations())
        second = client.post(f"/api/jobs/{seeded['job_id']}/apply/open", json=_confirmations())

        assert first.status_code == 202
        assert second.status_code == 429
        assert second.get_json()["code"] == "external_apply_rate_limited"

    def test_manual_open_logs_tailored_cv_stale_blocked_attempt(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        seeded = _seed_application(fake_supabase)
        tailored = next(d for d in fake_supabase.tables["documents"] if d["id"] == seeded["tailored_cv_id"])
        tailored["yaml_blob"] = "name: Tim Loehr\nexperience:\n  - role: Changed\n"

        payload = {**_confirmations(), "packet_id": seeded["packet_id"]}
        res = client.post(f"/api/jobs/{seeded['job_id']}/apply/open", json=payload)

        assert res.status_code == 409
        assert res.get_json()["code"] == "apply_not_ready"
        attempt = fake_supabase.tables["application_apply_attempts"][0]
        assert attempt["packet_id"] == seeded["packet_id"]
        assert attempt["status"] == "blocked"
        assert attempt["reason_code"] == "tailored_cv_stale"

    def test_manual_open_logs_cover_letter_stale_blocked_attempt(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_ENABLED", "1")
        monkeypatch.setenv("ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH", "0")
        seeded = _seed_application(fake_supabase)
        letter = next(d for d in fake_supabase.tables["documents"] if d["id"] == seeded["cover_letter_id"])
        letter["yaml_blob"] = "company: Acme GmbH\nrole: Changed\n"

        payload = {**_confirmations(), "packet_id": seeded["packet_id"]}
        res = client.post(f"/api/jobs/{seeded['job_id']}/apply/open", json=payload)

        assert res.status_code == 409
        assert res.get_json()["code"] == "apply_not_ready"
        attempt = fake_supabase.tables["application_apply_attempts"][0]
        assert attempt["packet_id"] == seeded["packet_id"]
        assert attempt["status"] == "blocked"
        assert attempt["reason_code"] == "cover_letter_stale"


def test_apply_service_does_not_import_browser_or_network_automation():
    source = Path("tools/editor/apply_service.py").read_text(encoding="utf-8").lower()

    assert "selenium" not in source
    assert "webdriver" not in source
    assert "playwright" not in source
    assert "requests" not in source
    assert "httpx" not in source


def test_apply_attempt_audit_is_append_only_server_write_and_no_submit():
    sql = Path("supabase/migrations/0013_application_apply_attempts.sql").read_text(encoding="utf-8")
    lowered = sql.lower()
    hardening = Path(
        "supabase/migrations/0023_security_contract_hardening.sql"
    ).read_text(encoding="utf-8")
    hardening_lower = hardening.lower()
    latest = Path(
        "supabase/migrations/20260801145140_preserve_application_history_deletes.sql"
    ).read_text(encoding="utf-8").lower()

    assert "create table if not exists public.application_apply_attempts" in sql
    assert "alter table public.application_apply_attempts enable row level security" in sql
    assert "application_apply_attempts_select_own" in sql
    assert "application_apply_attempts_insert_own" in sql
    assert 'create policy "application_apply_attempts_update_own"' not in sql
    assert 'create policy "application_apply_attempts_delete_own"' not in sql
    assert "grant select, insert on public.application_apply_attempts to authenticated" in sql
    assert "revoke update on public.application_apply_attempts from authenticated" in sql
    assert "revoke delete on public.application_apply_attempts from authenticated" in sql
    assert "submitted = false and automation_performed = false" in lowered
    assert "references public.application_packets(id) on delete set null" in sql
    assert "ap.job_id is not distinct from application_apply_attempts.job_id" in sql
    assert "and p.deletion_requested_at is not null" in sql
    assert (
        'drop policy if exists "application_apply_attempts_insert_own"'
        in hardening_lower
    )
    assert (
        "revoke insert on table public.application_apply_attempts "
        "from authenticated"
        in hardening_lower
    )
    assert (
        "create or replace function "
        "public.record_application_apply_attempt("
        in hardening_lower
    )
    assert "submitted,\n    automation_performed" in hardening_lower
    assert "false,\n    false" in hardening_lower
    assert (
        "revoke execute on function public.record_application_apply_attempt("
        in latest
    )
    assert "from public, anon, authenticated" in latest
    apply_service = Path("tools/editor/apply_service.py").read_text(encoding="utf-8")
    assert "_server_sb()" in apply_service
    assert ".table(ATTEMPT_TABLE)" in apply_service
    assert '"record_application_apply_attempt"' not in apply_service


def test_successful_apply_evidence_uses_locked_atomic_finalizer():
    migration = Path(
        "supabase/migrations/20260801145140_preserve_application_history_deletes.sql"
    ).read_text(encoding="utf-8").lower()
    function = migration.split(
        "create or replace function public.finalize_application_apply_action(", 1
    )[1].split("revoke execute on function public.finalize_application_apply_action(", 1)[0]
    apply_service = Path("tools/editor/apply_service.py").read_text(encoding="utf-8")

    assert "security definer" in function
    assert "set search_path = ''" in function
    assert "set lock_timeout = '2s'" in function
    for relation in (
        "public.profiles",
        "public.saved_jobs",
        "public.application_packets",
        "public.documents",
        "public.pdf_artifacts",
    ):
        assert relation in function
    assert function.count("for share") >= 8
    assert "job_row.source_url" in function
    assert "job_row.jd_text" in function
    assert "packet_row.updated_at" in function
    assert "source_row.updated_at" in function
    assert "tailored_row.updated_at" in function
    assert "letter_row.updated_at" in function
    assert "tailored_artifact.content_hash" in function
    assert "letter_artifact.content_hash" in function
    assert "insert into public.application_apply_attempts" in function
    assert "statement_timestamp()" in function
    assert "submitted, automation_performed" in function
    assert "false, false" in function
    assert (
        "from public, anon, authenticated;\ngrant execute on function "
        "public.finalize_application_apply_action("
        in migration
    )
    assert ") to service_role;" in migration
    assert '"finalize_application_apply_action"' in apply_service
    assert "attempt, readiness = _record_successful_attempt(" in apply_service


def test_application_packet_migration_tracks_lineage_and_readiness():
    sql = Path("supabase/migrations/0012_application_packets.sql").read_text(encoding="utf-8")

    for column in (
        "score_system_id text not null default 'semantic_fit'",
        "profile_answers_complete boolean not null default false",
        "legal_answers_confirmed boolean not null default false",
        "visa_answers_confirmed boolean not null default false",
    ):
        assert column in sql
    assert "application_packets_score_system_check" in sql
    assert "application_packets_owner_job_source_active_uidx" in sql
    assert "application_packets_owner_job_source_lineage_version_uidx" in sql
    for column in (
        "source_cv_hash",
        "job_content_hash",
        "prompt_version",
        "score_system_id",
        "packet_version",
    ):
        assert column in sql.split("application_packets_owner_job_source_lineage_version_uidx", 1)[1]
    assert "grant select on public.application_packets to authenticated" in sql
    assert "grant select, insert on public.application_packets" not in sql
    assert "revoke insert on public.application_packets from authenticated" in sql
    assert "revoke update on public.application_packets from authenticated" in sql
    assert "grant update (" not in sql
    for policy in (
        "application_packets_select_own",
        "application_packets_insert_own",
        "application_packets_update_own",
    ):
        block = sql.split(f'create policy "{policy}"', 1)[1].split(";", 1)[0]
        assert "and p.deletion_requested_at is not null" in block
    assert "and d.job_id = application_packets.job_id" in sql


def test_application_packet_additive_migration_hardens_applied_databases():
    sql = Path("supabase/migrations/0015_application_packet_readiness_lineage.sql").read_text(encoding="utf-8")

    assert "add column if not exists score_system_id text" in sql
    assert "update public.application_packets" in sql
    assert "alter column score_system_id set not null" in sql
    assert "application_packets_owner_job_source_active_uidx" in sql
    assert "application_packets_owner_job_source_lineage_version_uidx" in sql
    assert "profile_answers_complete" in sql
    assert "legal_answers_confirmed" in sql
    assert "visa_answers_confirmed" in sql
    assert "revoke update on public.application_packets from authenticated" in sql
    assert "grant update (" not in sql
