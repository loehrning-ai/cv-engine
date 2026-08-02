"""QA-regression tests — service-layer fixes (Flask routes + services).

Each class maps to one QA-verified finding and pins the fixed behavior:

  1. /api/extract returns a JSON 400 (not Werkzeug's HTML 500) on a corrupt
     PDF/DOCX upload.
  2. ``None`` in cover-letter attachments is dropped, never stringified into
     "Enclosures: CV, None".
  3. /api/cover-letter/generate caps tone/recipient/company_context (413) and
     rejects bidi/zero-width control chars at the route boundary.
  4. The cover-letter word cap counts each CJK codepoint as one word — a
     10,000-char CJK letter no longer passes the 450-word ceiling.
  5. Cliché stripping repairs a/an agreement ("a results-driven engineer" →
     "an engineer", not "a engineer").
  6. The cover-letter renderer only accepts a hex ``accent`` (CSS-injection
     hardening on direct calls).
  7. A malformed port in ``jd_url`` is a 400 (bad_source_url), never a 500 —
     in both jobs_service._safe_source_url and ssrf.safe_fetch_url.
  8. The local-JD PII sanitizer no longer wipes a single-line description:
     inline redaction already neutralizes the address.
  9. is_linkedin_url matches trailing-dot hosts ("www.linkedin.com.").
 10. 401 responses carry a static "invalid token" — the internal exception
     class name is logged server-side only.
 11. save-selected rejects a provided list that contains no valid string ids
     (400), matching the non-list rejection.

LLM calls are mocked where a route needs one; DB-backed routes use the
in-memory ``fake_supabase`` fixture from conftest.
"""
from __future__ import annotations

import io
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

from cover_letter import guards  # noqa: E402
from cover_letter.renderer import render_cover_letter_html  # noqa: E402

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"

# A CV + JD + letter that pass the anti-fabrication guards (same shape as
# tests/test_stage5_cover_letter.py, kept minimal).
GROUNDED_CV = {
    "name": "Tim Loehr",
    "experience": [
        {
            "role": "Data Engineer",
            "company": "Meta",
            "start": "2023",
            "end": "Present",
            "bullets": ["Cut pipeline latency by 40% across the ETL stack."],
            "stack": "Python, Spark, Airflow",
        }
    ],
    "skills": [{"label": "Data", "items": "Python, SQL, Spark"}],
}

JD_TEXT = (
    "Title: Senior Data Engineer\n"
    "Company: Acme GmbH\n"
    "You must have experience with Spark and Airflow. 5+ years required.\n"
)

CLEAN_LETTER = (
    "At Meta I cut pipeline latency by 40% across the ETL stack using Spark "
    "and Airflow, the exact stack this Senior Data Engineer role calls for.\n\n"
    "I would welcome the chance to bring this to Acme GmbH."
)


def _mock_llm(monkeypatch, letter_text: str):
    """Patch the service's make_client so .complete returns ``letter_text``."""
    import cover_letter_service as cl

    class _FakeClient:
        def __init__(self, api_key):
            self.provider = "stub"

        def complete(self, *, system, user, model=None, max_tokens=1024, cache_system=True):
            return letter_text

    monkeypatch.setattr(cl, "make_client", lambda provider, api_key: _FakeClient(api_key))


def _seed_cv(store) -> str:
    """Insert a CV document into the fake store; return its id."""
    import yaml as _yaml

    doc_id = str(uuid.uuid4())
    store.tables.setdefault("documents", []).append(
        {
            "id": doc_id,
            "owner_id": store.owner_id,
            "type": "cv",
            "name": "My CV",
            "yaml_blob": _yaml.safe_dump(GROUNDED_CV, sort_keys=False),
            "deleted_at": None,
            "updated_at": 1,
        }
    )
    return doc_id


# ===========================================================================
#  1. /api/extract — corrupt upload is a JSON 400, never an HTML 500.
# ===========================================================================
class TestExtractCorruptUpload:
    def test_corrupt_pdf_is_400_json(self, client, fake_supabase):
        res = client.post(
            "/api/extract",
            data={"file": (io.BytesIO(b"this is not a pdf at all"), "bad.pdf")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 400, res.get_data(as_text=True)
        assert res.is_json
        error = res.get_json()["error"]
        assert "Could not read file" in error
        assert "PDF" in error

    def test_corrupt_docx_is_400_json(self, client, fake_supabase):
        res = client.post(
            "/api/extract",
            data={"file": (io.BytesIO(b"not a zip either"), "bad.docx")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 400, res.get_data(as_text=True)
        assert res.is_json
        assert "DOCX" in res.get_json()["error"]


# ===========================================================================
#  2. None in attachments is dropped (never stringified to "None").
# ===========================================================================
class TestAttachmentsNoneDropped:
    def test_build_render_doc_drops_none(self):
        import cover_letter_service as cl_service

        doc = cl_service.build_render_doc(
            {"attachments": ["CV", None, 123], "language": "en"}, name="x"
        )
        assert doc["attachments"] == ["CV", "123"]

    def test_renderer_omits_none_from_enclosures(self):
        html = render_cover_letter_html(
            {"body": "hello", "attachments": ["CV", None], "language": "en"}
        )
        assert "Enclosures:" in html
        assert "None" not in html

    def test_edit_route_drops_none(self, client, fake_supabase):
        import cover_letter_service as cl_service

        doc_id = str(uuid.uuid4())
        fake_supabase.tables.setdefault("documents", []).append(
            {
                "id": doc_id,
                "owner_id": FAKE_OWNER,
                "type": "cover_letter",
                "name": "Letter",
                "yaml_blob": cl_service.serialize_blob({"body": "Original body."}),
                "job_id": None,
                "deleted_at": None,
                "updated_at": 1,
            }
        )
        res = client.post(
            f"/api/cover-letter?id={doc_id}",
            json={"body": "Edited body.", "attachments": ["CV", None, "Transcript"]},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        row = next(r for r in fake_supabase.tables["documents"] if r["id"] == doc_id)
        blob = cl_service.parse_blob(row["yaml_blob"])
        assert blob["attachments"] == ["CV", "Transcript"]


# ===========================================================================
#  3. Generate route — tone/recipient/company_context capped + control chars.
# ===========================================================================
class TestGenerateFieldHygiene:
    def _post(self, client, **overrides):
        payload = {"cv_id": str(uuid.uuid4()), "jd_text": JD_TEXT}
        payload.update(overrides)
        return client.post("/api/cover-letter/generate", json=payload)

    def test_oversized_recipient_is_413(self, client, fake_supabase):
        res = self._post(client, recipient="r" * 501)
        assert res.status_code == 413
        assert res.get_json()["max_chars"] == 500

    def test_oversized_company_context_is_413(self, client, fake_supabase):
        res = self._post(client, company_context="c" * 2001)
        assert res.status_code == 413
        assert res.get_json()["max_chars"] == 2000

    def test_oversized_tone_is_413(self, client, fake_supabase):
        res = self._post(client, tone="t" * 201)
        assert res.status_code == 413
        assert res.get_json()["max_chars"] == 200

    def test_control_chars_rejected_from_recipient(
        self, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(monkeypatch, CLEAN_LETTER)
        cv_id = _seed_cv(fake_supabase)
        fake_supabase.tables.setdefault("profiles", []).append(
            {"id": FAKE_OWNER, "managed_credits_remaining": 3}
        )
        res = client.post(
            "/api/cover-letter/generate",
            json={
                "cv_id": cv_id,
                "jd_text": JD_TEXT,
                # Bidi override (U+202E) + zero-width space (U+200B) must not
                # survive into the stored letter / prompt.
                "recipient": "Jane \u202eDoe\u200b",
            },
        )
        assert res.status_code == 400, res.get_data(as_text=True)
        assert res.get_json()["code"] == "invalid_cover_envelope_control"
        assert res.get_json()["field"] == "recipient"
        assert not [
            row for row in fake_supabase.tables.get("documents", [])
            if row.get("type") == "cover_letter"
        ]

    @pytest.mark.parametrize("field", ["tone", "recipient", "company_context"])
    def test_non_string_generate_field_is_400(self, client, fake_supabase, field):
        res = self._post(client, **{field: {"nested": "value"}})
        assert res.status_code == 400, res.get_data(as_text=True)
        assert res.get_json()["code"] == "invalid_cover_envelope_type"
        assert res.get_json()["field"] == field

    def test_parser_derived_role_is_bounded_before_ai_call(self, client, fake_supabase):
        cv_id = _seed_cv(fake_supabase)
        res = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": cv_id, "jd_text": "Title: " + ("R" * 501)},
        )
        assert res.status_code == 413, res.get_data(as_text=True)
        assert res.get_json()["code"] == "cover_envelope_field_too_large"
        assert res.get_json()["field"] == "role"


class TestCoverEnvelopeIntegrity:
    @staticmethod
    def _seed_letter(fake_supabase) -> tuple[str, dict]:
        import cover_letter_service as cl_service

        doc_id = str(uuid.uuid4())
        row = {
            "id": doc_id,
            "owner_id": FAKE_OWNER,
            "type": "cover_letter",
            "name": "Integrity letter",
            "yaml_blob": cl_service.serialize_blob({"body": "Original body."}),
            "job_id": None,
            "deleted_at": None,
            "updated_at": 1,
        }
        fake_supabase.tables.setdefault("documents", []).append(row)
        return doc_id, row

    @pytest.mark.parametrize(
        ("field", "max_chars"),
        [
            ("recipient", 500),
            ("company", 500),
            ("role", 500),
            ("tone", 200),
            ("body", 32_000),
            ("language", 16),
            ("source_cv_id", 128),
            ("job_id", 128),
            ("subject", 500),
            ("salutation", 500),
            ("closing", 500),
        ],
    )
    def test_every_persisted_text_field_has_a_hard_limit(self, field, max_chars):
        import cover_letter_service as cl_service

        with pytest.raises(cl_service.CoverLetterError) as exc_info:
            cl_service.serialize_blob({field: "x" * (max_chars + 1)})
        assert exc_info.value.status == 413
        assert exc_info.value.payload["code"] == "cover_envelope_field_too_large"
        assert exc_info.value.payload["field"] == field
        assert exc_info.value.payload["max_chars"] == max_chars

    @pytest.mark.parametrize(
        ("attachments", "code"),
        [
            (["CV"] * 21, "cover_envelope_attachment_count"),
            (["x" * 201], "cover_envelope_field_too_large"),
            (["CV", 7], "invalid_cover_envelope_type"),
            (["CV\nInjected"], "invalid_cover_envelope_control"),
        ],
    )
    def test_attachment_shape_count_and_item_bounds(self, attachments, code):
        import cover_letter_service as cl_service

        with pytest.raises(cl_service.CoverLetterError) as exc_info:
            cl_service.serialize_blob({"body": "Body", "attachments": attachments})
        assert exc_info.value.payload["code"] == code
        assert exc_info.value.payload["field"] == "attachments"

    def test_serialized_envelope_has_a_utf8_byte_cap(self):
        import cover_letter_service as cl_service

        with pytest.raises(cl_service.CoverLetterError) as exc_info:
            cl_service.serialize_blob({"body": "😀" * 20_000})
        assert exc_info.value.status == 413
        assert exc_info.value.payload["code"] == "cover_envelope_too_large"
        assert exc_info.value.payload["max_bytes"] == 64 * 1024

    def test_update_rejects_control_chars_without_mutating_row(self, client, fake_supabase):
        doc_id, row = self._seed_letter(fake_supabase)
        before = row["yaml_blob"]
        res = client.post(
            "/api/cover-letter",
            json={"id": doc_id, "body": "Edited body.", "subject": "Safe\u202eHidden"},
        )
        assert res.status_code == 400, res.get_data(as_text=True)
        assert res.get_json()["code"] == "invalid_cover_envelope_control"
        assert res.get_json()["field"] == "subject"
        assert row["yaml_blob"] == before

    def test_update_rejects_invalid_envelope_types(self, client, fake_supabase):
        doc_id, row = self._seed_letter(fake_supabase)
        before = row["yaml_blob"]
        res = client.post(
            "/api/cover-letter",
            json={"id": doc_id, "body": "Edited body.", "company": ["Acme"]},
        )
        assert res.status_code == 400, res.get_data(as_text=True)
        assert res.get_json()["code"] == "invalid_cover_envelope_type"
        assert res.get_json()["field"] == "company"
        assert row["yaml_blob"] == before

    def test_update_normalizes_newline_attachment_text(self, client, fake_supabase):
        import cover_letter_service as cl_service

        doc_id, row = self._seed_letter(fake_supabase)
        res = client.post(
            "/api/cover-letter",
            json={"id": doc_id, "body": "Edited body.", "attachments": " CV \n Portfolio "},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        assert cl_service.parse_blob(row["yaml_blob"])["attachments"] == ["CV", "Portfolio"]


# ===========================================================================
#  4. Word cap — CJK codepoints count as individual words.
# ===========================================================================
class TestCjkWordCount:
    def test_cjk_run_counts_each_codepoint(self):
        assert guards.word_count("中" * 2000) == 2000

    def test_mixed_cjk_latin(self):
        assert guards.word_count("hello 世界 test") == 4

    def test_latin_count_unchanged(self):
        assert guards.word_count("I cut latency by 40% across the ETL stack.") == 9

    def test_cjk_letter_blocked_by_cap(self):
        with pytest.raises(guards.WordCapError):
            guards.enforce_word_cap("中" * (guards.WORD_HARD_CAP + 1))

    def test_empty_inputs(self):
        assert guards.word_count("") == 0
        assert guards.word_count(None) == 0


# ===========================================================================
#  5. Cliché stripping repairs a/an agreement.
# ===========================================================================
class TestClicheArticleRepair:
    def test_a_becomes_an_before_vowel(self):
        cleaned, findings = guards.cliche_scan("I am a results-driven engineer.")
        assert cleaned == "I am an engineer."
        assert {f.phrase for f in findings} == {"results-driven"}

    def test_an_becomes_a_before_consonant(self):
        cleaned, _ = guards.cliche_scan("She is an results-driven manager.")
        assert cleaned == "She is a manager."

    def test_sentence_initial_article_capitalized(self):
        cleaned, _ = guards.cliche_scan("A results-driven approach cut latency.")
        assert cleaned == "An approach cut latency."

    def test_no_article_no_repair(self):
        cleaned, _ = guards.cliche_scan("He is results-driven.")
        assert cleaned == "He is."


# ===========================================================================
#  6. Renderer — accent must be a hex color (CSS-injection hardening).
# ===========================================================================
class TestRendererAccentHardening:
    def test_malicious_accent_falls_back(self):
        html = render_cover_letter_html(
            {"body": "hello", "accent": "#111}body{display:none}/*"}
        )
        assert "display:none" not in html
        assert "--accent: #111111;" in html

    def test_valid_accent_preserved(self):
        html = render_cover_letter_html({"body": "hello", "accent": "#1f6f43"})
        assert "--accent: #1f6f43;" in html

    def test_short_hex_and_alpha_accepted(self):
        assert "--accent: #abc;" in render_cover_letter_html({"body": "x", "accent": "#abc"})
        assert "--accent: #aabbccdd;" in render_cover_letter_html(
            {"body": "x", "accent": "#aabbccdd"}
        )

    def test_empty_accent_defaults(self):
        assert "--accent: #111111;" in render_cover_letter_html({"body": "x", "accent": ""})


# ===========================================================================
#  7. Malformed port in jd_url — 400, never a 500.
# ===========================================================================
class TestMalformedPort:
    def test_safe_source_url_rejects_alpha_port(self):
        import jobs_service

        assert jobs_service._safe_source_url("http://example.com:abc/j") == ""

    def test_safe_source_url_rejects_out_of_range_port(self):
        import jobs_service

        assert jobs_service._safe_source_url("http://example.com:99999/j") == ""

    def test_safe_source_url_keeps_valid_port(self):
        import jobs_service

        assert jobs_service._safe_source_url("http://example.com:8080/j") == (
            "http://example.com:8080/j"
        )

    def test_ssrf_blocks_invalid_port(self):
        import ssrf

        with pytest.raises(ssrf.SSRFBlocked):
            ssrf.safe_fetch_url("http://example.com:abc/j")

    def test_post_jobs_malformed_port_is_400(self, client, fake_supabase):
        res = client.post("/api/jobs", json={"jd_url": "http://example.com:abc/j"})
        assert res.status_code == 400, res.get_data(as_text=True)
        assert res.get_json()["code"] == "bad_source_url"


# ===========================================================================
#  8. PII sanitizer keeps a single-line description (inline redaction).
# ===========================================================================
class TestSanitizeLocalJobDescription:
    def test_single_line_description_survives(self):
        import jobs_service

        out = jobs_service._sanitize_local_job_description(
            "Build distributed systems in Go. Requirements: 5 years experience. "
            "Email jane@corp.com to apply."
        )
        assert "Build distributed systems in Go" in out
        assert "jane@corp.com" not in out
        assert "[email redacted]" in out

    def test_contact_line_still_dropped_when_other_lines_survive(self):
        import jobs_service

        out = jobs_service._sanitize_local_job_description(
            "Build distributed systems in Go.\nContact jane@corp.com for details."
        )
        assert "Build distributed systems in Go." in out
        assert "Contact jane@corp.com" not in out

    def test_non_string_is_empty(self):
        import jobs_service

        assert jobs_service._sanitize_local_job_description(None) == ""
        assert jobs_service._sanitize_local_job_description(42) == ""


# ===========================================================================
#  9. is_linkedin_url — trailing-dot hosts match.
# ===========================================================================
class TestIsLinkedinUrl:
    def test_trailing_dot_host_matches(self):
        import jobs_service

        assert jobs_service.is_linkedin_url("https://www.linkedin.com./jobs/123") is True

    def test_plain_host_still_matches(self):
        import jobs_service

        assert jobs_service.is_linkedin_url("https://www.linkedin.com/jobs/123") is True

    def test_lookalike_host_rejected(self):
        import jobs_service

        assert jobs_service.is_linkedin_url("https://www.linkedin.com.evil.com/jobs/1") is False


# ===========================================================================
# 10. 401 responses carry a static message (no internal class names).
# ===========================================================================
class TestAuthErrorMessage:
    def test_pyjwt_failure_is_static(self, monkeypatch):
        import auth as auth_mod

        monkeypatch.setattr(
            auth_mod, "SUPABASE_JWKS_URL", "https://auth.test/.well-known/jwks.json"
        )
        auth_mod._jwks_client.cache_clear()
        try:
            with pytest.raises(auth_mod.AuthError) as exc:
                auth_mod.verify_token("not-a-real-jwt")
            assert exc.value.message == "invalid token"
            assert exc.value.status == 401
        finally:
            auth_mod._jwks_client.cache_clear()

    def test_jwks_network_failure_is_static(self, monkeypatch):
        import auth as auth_mod

        class _Boom:
            def get_signing_key_from_jwt(self, token):
                raise ConnectionError("jwks unreachable")

        monkeypatch.setattr(auth_mod, "_jwks_client", lambda: _Boom())
        with pytest.raises(auth_mod.AuthError) as exc:
            auth_mod.verify_token("aaa.bbb.ccc")
        assert exc.value.message == "invalid token"
        assert "ConnectionError" not in exc.value.message

    def test_401_detail_is_static(self, client, monkeypatch):
        import auth as auth_mod

        monkeypatch.setattr(
            auth_mod, "SUPABASE_JWKS_URL", "https://auth.test/.well-known/jwks.json"
        )
        auth_mod._jwks_client.cache_clear()
        try:
            res = client.get("/api/cv", headers={"Authorization": "Bearer not-a-real-jwt"})
            assert res.status_code == 401
            body = res.get_json()
            assert body["error"] == "unauthorized"
            assert body["detail"] == "invalid token"
        finally:
            auth_mod._jwks_client.cache_clear()


# ===========================================================================
# 11. save-selected — a provided list with no valid string ids is a 400.
# ===========================================================================
class TestSaveSelectedValidation:
    def _seed_run(self, store) -> str:
        run_id = str(uuid.uuid4())
        store.tables.setdefault("job_import_runs", []).append(
            {"id": run_id, "owner_id": FAKE_OWNER, "provider": "manual"}
        )
        return run_id

    def test_non_string_ids_are_400(self, client, fake_supabase):
        run_id = self._seed_run(fake_supabase)
        res = client.post(
            f"/api/jobs/import-runs/{run_id}/save-selected",
            json={"result_ids": [123, {}, None]},
        )
        assert res.status_code == 400, res.get_data(as_text=True)
        assert res.get_json()["code"] == "no_selection"

    def test_non_list_is_400(self, client, fake_supabase):
        run_id = self._seed_run(fake_supabase)
        res = client.post(
            f"/api/jobs/import-runs/{run_id}/save-selected",
            json={"result_ids": "not-a-list"},
        )
        assert res.status_code == 400
        assert res.get_json()["code"] == "no_selection"

    def test_string_ids_still_accepted(self, client, fake_supabase):
        run_id = self._seed_run(fake_supabase)
        res = client.post(
            f"/api/jobs/import-runs/{run_id}/save-selected",
            json={"result_ids": ["no-such-result"]},
        )
        # Unknown-but-well-formed ids are skipped, not rejected as malformed.
        assert res.status_code == 200, res.get_data(as_text=True)
        assert res.get_json()["saved_count"] == 0
