"""Stage 5 tests — grounded cover-letter generation + guards + renderer.

Covers the product's core AI value and its safety contract:

  * the grounded prompt is assembled with the CV + parsed JD; tone respected;
  * ANTI-FABRICATION: a letter naming an employer NOT in the CV ("Globex
    Industries") + a fabricated metric is BLOCKED before save, with the
    offending items reported (the headline guard);
  * the cliché scan flags/strips the reject phrases;
  * the word cap is enforced (over-long letter → rejected);
  * a clean grounded letter is stored as a ``cover_letter`` document and uses
    the Stage 4 credit/key path (3 managed then needs_key);
  * cover letters are RLS-isolated (user B cannot read/delete user A's) —
    real-JWT tests gated on env, skipping cleanly when unset;
  * the letter renders to a non-empty PDF (multi-page allowed).

Every LLM call is MOCKED (ANTHROPIC_API_KEY is blank in CI). The DB-backed
routes use the in-memory ``fake_supabase`` fixture from conftest (whose
``rpc('consume_managed_credit')`` faithfully decrements + audits).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

from cover_letter import guards, jd, prompts  # noqa: E402
from cover_letter.guards import GroundingError, WordCapError  # noqa: E402

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"
SAMPLE_KEY = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJ"

# A canonical CV used as the SOLE source of candidate facts in every test.
GROUNDED_CV = {
    "name": "Tim Loehr",
    "accent": "#1f6f43",
    "font": "sans",
    "contact": [{"network": "mail", "username": "tim@example.com"}],
    "experience": [
        {
            "role": "Data Engineer",
            "company": "Meta",
            "location": "Warsaw",
            "start": "2023",
            "end": "Present",
            "bullets": ["Cut pipeline latency by 40% across the ETL stack."],
            "stack": "Python, Spark, Airflow",
        },
        {
            "role": "Data Scientist",
            "company": "Red Bull",
            "location": "Salzburg",
            "start": "2021",
            "end": "2023",
            "bullets": ["Built ML models for anomaly detection."],
        },
    ],
    "skills": [{"label": "Data", "items": "Python, SQL, PyTorch, Spark"}],
}

JD_TEXT = (
    "Title: Senior Data Engineer\n"
    "Company: Acme GmbH\n"
    "You must have experience with Spark and Airflow. 5+ years required.\n"
    "Responsibilities: build ETL pipelines and data models."
)

# A clean letter that only states facts present in GROUNDED_CV.
CLEAN_LETTER = (
    "At Meta I cut pipeline latency by 40% across the ETL stack using Spark "
    "and Airflow, the exact stack this Senior Data Engineer role calls for.\n\n"
    "Earlier, at Red Bull, I built ML models for anomaly detection in Python. "
    "I bring the SQL and PyTorch depth your data-modeling work needs.\n\n"
    "I would welcome the chance to bring this to Acme GmbH."
)

# A letter that hallucinates an employer not in the CV + a fabricated metric.
FABRICATED_LETTER = (
    "At Globex Industries I drove a 250% revenue increase and led a team of "
    "80 engineers, delivering record results for the company.\n\n"
    "I would be thrilled to bring this track record to your team."
)


# ===========================================================================
#  1. Grounded prompt assembly + JD parsing.
# ===========================================================================
class TestPromptAssembly:
    def test_jd_parses_into_keyed_json(self):
        parsed = jd.parse_jd(JD_TEXT)
        assert parsed["title"] == "Senior Data Engineer"
        assert parsed["company"] == "Acme GmbH"
        assert parsed["language"] == "en"
        # Spark/Airflow surface as keywords; requirements captured.
        assert any("Spark" in k for k in parsed["keywords"])
        assert parsed["requirements"], "should capture requirement lines"

    def test_empty_jd_raises(self):
        with pytest.raises(ValueError):
            jd.parse_jd("   ")

    def test_prompt_contains_cv_and_jd_and_tone(self):
        parsed = jd.parse_jd(JD_TEXT)
        system, user = prompts.build_cover_letter_messages(
            cv=GROUNDED_CV, jd_json=parsed, tone="formal", recipient="Dr. Jane Doe"
        )
        # System prompt carries the global guardrails + cover-letter task.
        assert "Global guardrails" in system
        assert "NEVER invent facts" in system
        assert "You write cover letters" in system
        # User message embeds the CV JSON (a real fact) + the parsed JD + tone.
        assert "Meta" in user and "Red Bull" in user
        assert "Senior Data Engineer" in user  # from parsed JD
        assert "formal" in user
        assert "Dr. Jane Doe" in user
        # The CV is wrapped as untrusted data.
        assert "<cv_json>" in user and "<job_description>" in user


# ===========================================================================
#  2. Guards — cliché scan, anti-fabrication, word cap (unit level).
# ===========================================================================
class TestGuardsUnit:
    def test_clean_letter_passes(self):
        # The JD is grounding context too: the candidate legitimately names the
        # employer (Acme GmbH) and role (Senior Data Engineer) from the posting.
        jd_json = jd.parse_jd(JD_TEXT)
        res = guards.run_guards(CLEAN_LETTER, GROUNDED_CV, jd_context=jd_json)
        assert res.text  # non-empty
        assert res.cliches_removed == []
        assert res.word_count > 0

    def test_fabricated_employer_and_metric_blocked(self):
        """The headline guard: a hallucinated employer + invented metric is
        caught and reported — even with the real JD as grounding context, an
        employer in NEITHER the CV nor the JD ("Globex Industries") is blocked."""
        jd_json = jd.parse_jd(JD_TEXT)
        with pytest.raises(GroundingError) as exc:
            guards.run_guards(FABRICATED_LETTER, GROUNDED_CV, jd_context=jd_json)
        items = exc.value.items
        # The fabricated employer and the invented metrics are all flagged.
        assert "Globex" in items
        assert any(i in items for i in ("250%", "250"))
        assert any(i == "80" for i in items)

    def test_fabrication_blocked_without_jd_context_too(self):
        """CV-only grounding (no JD) still catches the hallucinated employer."""
        with pytest.raises(GroundingError) as exc:
            guards.run_guards(FABRICATED_LETTER, GROUNDED_CV)
        assert "Globex" in exc.value.items

    def test_cliche_scan_flags_and_strips_reject_phrases(self):
        letter = (
            "I am writing to express my interest in this role. I am passionate "
            "about data and a results-driven team player who can hit the ground "
            "running at Meta."
        )
        cleaned, findings = guards.cliche_scan(letter)
        phrases = {f.phrase for f in findings}
        assert "I am writing to express my interest" in phrases
        assert "passionate about" in phrases
        assert "results-driven" in phrases
        assert "team player" in phrases
        assert "hit the ground running" in phrases
        # The stripped phrases are gone from the cleaned text.
        for p in ("passionate about", "results-driven", "team player"):
            assert p not in cleaned.lower()
        # Each finding carries a rewrite suggestion.
        assert all(f.suggestion for f in findings)

    def test_word_cap_enforced(self):
        over = " ".join(["word"] * (guards.WORD_HARD_CAP + 20)) + " Meta"
        with pytest.raises(WordCapError) as exc:
            guards.enforce_word_cap(over)
        assert exc.value.count > guards.WORD_HARD_CAP

    def test_grounding_accepts_normalized_numbers(self):
        # CV says "40%"; a letter saying "40 percent" must still be grounded.
        letter = "At Meta I improved latency by 40 percent."
        assert guards.cross_reference_facts(letter, GROUNDED_CV) == []

    def test_jd_requirements_do_not_ground_candidate_claims(self):
        jd_json = jd.parse_jd(
            "Title: Platform Engineer\n"
            "Company: Acme GmbH\n"
            "Requirements: Kubernetes and 5+ years of Go experience."
        )
        letter = "I bring 5+ years of Kubernetes and Go experience to Acme GmbH."
        with pytest.raises(GroundingError) as exc:
            guards.run_guards(letter, GROUNDED_CV, jd_context=jd_json)
        items = {str(item).lower() for item in exc.value.items}
        assert any("kubernetes" in item for item in items)
        assert any("5+" in item or item == "5" for item in items)

    def test_generated_secret_shape_is_blocked(self):
        letter = "At Meta I used Spark. Token sk-ant-thisShouldNeverPersist1234567890"
        with pytest.raises(GroundingError) as exc:
            guards.run_guards(letter, GROUNDED_CV)
        assert any("sk-ant" in item for item in exc.value.items)


# ===========================================================================
#  3. Generation endpoint — happy path, guards, credit/key path.
# ===========================================================================
def _mock_llm(server_module, monkeypatch, letter_text: str, seen: dict | None = None):
    """Patch the service's make_client so .complete returns ``letter_text``.

    Records the (system, user) it was called with into ``seen`` if provided,
    plus the api_key — so tests can assert grounding + the resolved key.
    """
    import cover_letter_service as cl

    class _FakeClient:
        def __init__(self, api_key):
            if seen is not None:
                seen["api_key"] = api_key

        def complete(self, *, system, user, model=None, max_tokens=1024, cache_system=True):
            if seen is not None:
                seen["system"] = system
                seen["user"] = user
                seen["max_tokens"] = max_tokens
            return letter_text

    monkeypatch.setattr(cl, "make_client", lambda provider, api_key: _FakeClient(api_key))


def _seed_cv(store, name="My CV", yaml_blob=None):
    """Insert a CV document into the fake store; return its id."""
    import yaml as _yaml

    blob = yaml_blob if yaml_blob is not None else _yaml.safe_dump(GROUNDED_CV, sort_keys=False)
    rows = store.tables.setdefault("documents", [])
    import uuid as _uuid

    doc_id = str(_uuid.uuid4())
    rows.append(
        {
            "id": doc_id,
            "owner_id": store.owner_id,
            "type": "cv",
            "name": name,
            "yaml_blob": blob,
            "deleted_at": None,
            "updated_at": 1,
        }
    )
    return doc_id


def _seed_profile(store, owner_id, *, credits):
    store.tables.setdefault("profiles", []).append(
        {"id": owner_id, "managed_credits_remaining": credits}
    )


class TestGenerateEndpoint:
    def test_grounded_letter_generated_and_stored(self, server_module, client, fake_supabase, monkeypatch):
        """A clean grounded letter passes guards, is stored as a cover_letter
        document, and goes through the managed-credit path."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        seen: dict = {}
        _mock_llm(server_module, monkeypatch, CLEAN_LETTER, seen)
        cv_id = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": cv_id, "jd_text": JD_TEXT, "tone": "formal"},
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        assert body["body"].strip()
        assert body["used_managed_credit"] is True
        assert body["role"] == "Senior Data Engineer"
        assert body["company"] == "Acme GmbH"

        # The grounded prompt actually carried the CV + parsed JD + tone.
        assert "Meta" in seen["user"] and "Senior Data Engineer" in seen["user"]
        assert "formal" in seen["user"]
        assert seen["max_tokens"] == prompts.COVER_LETTER_MAX_TOKENS

        # Stored as a cover_letter document.
        docs = [d for d in fake_supabase.tables.get("documents", []) if d["type"] == "cover_letter"]
        assert len(docs) == 1
        # A managed credit was spent + audited.
        assert fake_supabase.tables["profiles"][0]["managed_credits_remaining"] == 2

    def test_fabricated_employer_blocks_save(self, server_module, client, fake_supabase, monkeypatch):
        """End-to-end anti-fabrication: a hallucinated employer never ships.

        The route returns 422 unverified_claims with the offending items and
        NO cover_letter document is stored."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(server_module, monkeypatch, FABRICATED_LETTER)
        cv_id = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": cv_id, "jd_text": JD_TEXT},
        )
        assert res.status_code == 422, res.get_data(as_text=True)
        body = res.get_json()
        assert body["error"] == "unverified_claims"
        assert "Globex" in body["items"]
        # NOTHING was stored — the hallucinated letter is blocked.
        docs = [d for d in fake_supabase.tables.get("documents", []) if d["type"] == "cover_letter"]
        assert docs == []

    def test_cliches_reported_in_response(self, server_module, client, fake_supabase, monkeypatch):
        """A grounded-but-cliché-laden letter is cleaned and the removals are
        surfaced to the UI."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        cliche_letter = (
            "I am passionate about data engineering and bring a proven track "
            "record. At Meta I cut pipeline latency by 40% with Spark and Airflow."
        )
        _mock_llm(server_module, monkeypatch, cliche_letter)
        cv_id = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post("/api/cover-letter/generate", json={"cv_id": cv_id, "jd_text": JD_TEXT})
        assert res.status_code == 201, res.get_data(as_text=True)
        removed = {c["phrase"] for c in res.get_json()["cliches_removed"]}
        assert "passionate about" in removed
        assert "proven track record" in removed

    def test_over_long_letter_rejected(self, server_module, client, fake_supabase, monkeypatch):
        """An over-long (grounded) letter is rejected by the word cap."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        # Grounded filler ("Meta" appears in CV) repeated past the hard cap.
        long_letter = "Meta " * (guards.WORD_HARD_CAP + 30)
        _mock_llm(server_module, monkeypatch, long_letter)
        cv_id = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post("/api/cover-letter/generate", json={"cv_id": cv_id, "jd_text": JD_TEXT})
        assert res.status_code == 422
        assert res.get_json()["code"] == "word_cap"

    def test_missing_cv_id_is_400(self, server_module, client, fake_supabase):
        res = client.post("/api/cover-letter/generate", json={"jd_text": JD_TEXT})
        assert res.status_code == 400

    def test_missing_jd_is_400(self, server_module, client, fake_supabase, monkeypatch):
        cv_id = _seed_cv(fake_supabase)
        res = client.post("/api/cover-letter/generate", json={"cv_id": cv_id, "jd_text": "  "})
        assert res.status_code == 400

    def test_unknown_cv_is_404(self, server_module, client, fake_supabase, monkeypatch):
        import uuid as _uuid

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(server_module, monkeypatch, CLEAN_LETTER)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": str(_uuid.uuid4()), "jd_text": JD_TEXT},
        )
        assert res.status_code == 404


# ===========================================================================
#  4. Credit/key path parity with Stage 4 — 3 managed, then needs_key.
# ===========================================================================
class TestCreditKeyPath:
    def test_three_managed_then_needs_key(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        seen: dict = {}
        _mock_llm(server_module, monkeypatch, CLEAN_LETTER, seen)
        cv_id = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        for _ in range(3):
            res = client.post("/api/cover-letter/generate", json={"cv_id": cv_id, "jd_text": JD_TEXT})
            assert res.status_code == 201, res.get_data(as_text=True)
            # The managed path resolved the operator key into .complete().
            assert seen["api_key"]

        assert fake_supabase.tables["profiles"][0]["managed_credits_remaining"] == 0
        # 4th → 402 needs_key.
        res = client.post("/api/cover-letter/generate", json={"cv_id": cv_id, "jd_text": JD_TEXT})
        assert res.status_code == 402
        assert res.get_json()["code"] == "needs_key"

    def test_byo_key_used_and_logged(self, server_module, client, fake_supabase, monkeypatch):
        import llm_gateway

        # No operator key → proves BYO path is independent.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(llm_gateway, "validate_provider_key", lambda p, k: None)
        cv_id = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=0)
        # Store a BYO key (validate mocked).
        if not os.environ.get("ONEPAGER_KEK"):
            pytest.skip("ONEPAGER_KEK not set — BYO encrypt/decrypt needs it")
        client.post("/api/key", json={"provider": "anthropic", "key": SAMPLE_KEY})

        seen: dict = {}
        _mock_llm(server_module, monkeypatch, CLEAN_LETTER, seen)
        res = client.post("/api/cover-letter/generate", json={"cv_id": cv_id, "jd_text": JD_TEXT})
        assert res.status_code == 201, res.get_data(as_text=True)
        assert res.get_json()["used_managed_credit"] is False
        # The decrypted BYO key reached the client.
        assert seen["api_key"] == SAMPLE_KEY
        # A BYO usage_events row was logged for the generation.
        byo = [
            e
            for e in fake_supabase.tables.get("usage_events", [])
            if e.get("used_managed_credit") is False and e.get("kind") == "cover_letter_generate"
        ]
        assert len(byo) == 1


# ===========================================================================
#  5. CRUD + render (list / read / edit / delete / PDF).
# ===========================================================================
class TestCoverLetterCrudAndRender:
    def _generate_one(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(server_module, monkeypatch, CLEAN_LETTER)
        cv_id = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post("/api/cover-letter/generate", json={"cv_id": cv_id, "jd_text": JD_TEXT})
        assert res.status_code == 201, res.get_data(as_text=True)
        return res.get_json()["id"]

    def test_list_read_edit_delete(self, server_module, client, fake_supabase, monkeypatch):
        cl_id = self._generate_one(server_module, client, fake_supabase, monkeypatch)

        # List shows it.
        lst = client.get("/api/cover-letters").get_json()
        assert lst["count"] == 1
        assert lst["cover_letters"][0]["id"] == cl_id

        # Read returns the blob.
        got = client.get(f"/api/cover-letter?id={cl_id}").get_json()
        assert got["id"] == cl_id
        assert got["body"]
        assert got["role"] == "Senior Data Engineer"

        # Edit the body (editable after generation).
        edited = "At Meta I cut latency by 40% with Spark."
        save = client.post("/api/cover-letter", json={"id": cl_id, "body": edited})
        assert save.status_code == 200
        again = client.get(f"/api/cover-letter?id={cl_id}").get_json()
        assert again["body"] == edited

        # Rename.
        rn = client.post(f"/api/cover-letter/{cl_id}/rename", json={"name": "Acme application"})
        assert rn.status_code == 200
        assert rn.get_json()["name"] == "Acme application"

        # Delete.
        d = client.delete(f"/api/cover-letter/{cl_id}")
        assert d.status_code == 200
        assert client.get("/api/cover-letters").get_json()["count"] == 0

    def test_edit_structured_envelope_and_preview(self, server_module, client, fake_supabase, monkeypatch):
        cl_id = self._generate_one(server_module, client, fake_supabase, monkeypatch)

        save = client.post("/api/cover-letter", json={
            "id": cl_id,
            "body": "At Meta I cut latency by 40% with Spark.",
            "subject": "Application for Senior Data Engineer",
            "salutation": "Dear Hiring Team,",
            "closing": "Best regards,",
            "attachments": ["CV", "Portfolio"],
        })
        assert save.status_code == 200, save.get_data(as_text=True)
        got = client.get(f"/api/cover-letter?id={cl_id}").get_json()
        assert got["subject"] == "Application for Senior Data Engineer"
        assert got["salutation"] == "Dear Hiring Team,"
        assert got["closing"] == "Best regards,"
        assert got["attachments"] == ["CV", "Portfolio"]

        preview = client.get(f"/api/cover-letter/{cl_id}/preview")
        assert preview.status_code == 200, preview.get_data(as_text=True)
        html = preview.get_data(as_text=True)
        assert "Application for Senior Data Engineer" in html
        assert "Dear Hiring Team" in html
        assert "Portfolio" in html
        assert "<script" not in html.lower()

    def test_direct_read_and_preview_ignore_stale_blob_job_id(self, server_module, client, fake_supabase, monkeypatch):
        cl_id = self._generate_one(server_module, client, fake_supabase, monkeypatch)
        stale_job = "11111111-1111-4111-8111-111111111111"
        row = next(d for d in fake_supabase.tables["documents"] if d["id"] == cl_id)
        row["yaml_blob"] = row["yaml_blob"] + "\njob_id: " + stale_job + "\n"
        row["job_id"] = None

        got = client.get(f"/api/cover-letter?id={cl_id}").get_json()
        assert got["job_id"] is None
        preview = client.get(f"/api/cover-letter/{cl_id}/preview")
        assert preview.status_code == 200

    def test_edit_over_long_body_rejected(self, server_module, client, fake_supabase, monkeypatch):
        cl_id = self._generate_one(server_module, client, fake_supabase, monkeypatch)
        over = "word " * (guards.WORD_HARD_CAP + 30)
        res = client.post("/api/cover-letter", json={"id": cl_id, "body": over})
        assert res.status_code == 422
        assert res.get_json()["code"] == "word_cap"

    def test_build_returns_pdf_bytes(self, server_module, client, fake_supabase, monkeypatch):
        pytest.importorskip("weasyprint")
        # Pin the direct-stream fallback: this test asserts the pre-Stage-8 byte
        # contract, so R2 must be off (an ambient .env with R2 creds would route
        # to a signed URL + hit real R2). Stage 8's R2 path is covered separately.
        for _v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
            monkeypatch.delenv(_v, raising=False)
        cl_id = self._generate_one(server_module, client, fake_supabase, monkeypatch)
        res = client.get(f"/api/cover-letter/{cl_id}/build")
        assert res.status_code == 200, res.get_data(as_text=True)
        assert res.headers["Content-Type"] == "application/pdf"
        data = res.get_data()
        assert data.startswith(b"%PDF"), "expected PDF bytes"
        assert len(data) > 1000

    def test_renderer_direct_pdf_multipage_ok(self):
        """The renderer itself yields non-empty PDF bytes (multi-page allowed —
        no 1-page gate)."""
        pytest.importorskip("weasyprint")
        from cover_letter.renderer import render_cover_letter_pdf

        long_body = "\n\n".join(
            [f"Paragraph {i} about Meta and Spark and Airflow." for i in range(60)]
        )
        pdf = render_cover_letter_pdf(
            {"sender_name": "Tim Loehr", "body": long_body, "font": "sans", "accent": "#1f6f43"},
            base_url=f"file://{REPO}",
        )
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 1000


class TestCoverLetterFrontendLibrary:
    def test_library_rows_show_storage_and_linkage_metadata(self):
        js = (REPO / "tools" / "editor" / "static" / "cover-letters.js").read_text()
        css = (REPO / "tools" / "editor" / "static" / "cover-letters.css").read_text()
        assert "row.dataset.docId = doc.id || \"\"" in js
        assert "row.dataset.docType = doc.type || \"\"" in js
        assert "row.dataset.jobId = doc.job_id" in js
        assert 't("lib.storage.saved", "Saved in Documents")' in js
        assert 't("lib.storage.linked_job", "Linked to this job")' in js
        assert 't("lib.storage.standalone_letter", "Standalone letter")' in js
        assert ".cl-lib-meta-chip" in css
        html = (REPO / "tools" / "editor" / "static" / "cover-letters.html").read_text()
        assert "cl-envelope-grid" not in html
        assert "cl-edit-subject" not in html
        assert "cl-edit-attachments" not in html
        assert "cl-letter-meta" in html
        assert 'id="cl-body"' in html
        assert "cl-preview" in html
        assert "/preview?t=" in js
        assert "envelopePayload()" not in js
        assert "JSON.stringify({ id: letterId, body: body })" in js


# ===========================================================================
#  6. RLS isolation — user B cannot read/delete user A's cover letters.
#  Runs against the REAL Supabase project with real user JWTs; SKIPS cleanly
#  when the provisioning env vars are absent (same pattern as Stage 2/4).
# ===========================================================================
A_TOKEN = os.environ.get("TEST_A_TOKEN")
A_ID = os.environ.get("TEST_A_ID")
B_TOKEN = os.environ.get("TEST_B_TOKEN")
B_ID = os.environ.get("TEST_B_ID")
_HAVE_USERS = all([A_TOKEN, A_ID, B_TOKEN, B_ID])


@pytest.mark.skipif(
    not _HAVE_USERS,
    reason="TEST_A_TOKEN/TEST_B_TOKEN (+ ids) not set — provision real users first",
)
class TestCoverLetterRLS:
    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_b_cannot_read_or_delete_a_cover_letter(self):
        """A creates a cover_letter document; B must get 404 on read + delete."""
        import importlib

        from supabase import create_client

        server = importlib.import_module("server")
        server.app.config.update(TESTING=True)
        client = server.app.test_client()

        url = os.environ["SUPABASE_URL"]
        pub = os.environ["SUPABASE_PUBLISHABLE_KEY"]

        # A inserts a cover_letter row directly under their JWT (RLS path).
        sb_a = create_client(url, pub)
        sb_a.postgrest.auth(A_TOKEN)
        created = (
            sb_a.table("documents")
            .insert(
                {
                    "type": "cover_letter",
                    "name": "A-secret-letter",
                    "yaml_blob": "body: secret\n",
                }
            )
            .execute()
            .data
        )
        assert created, "A should be able to create a cover letter"
        a_doc_id = created[0]["id"]

        try:
            # B reads A's cover letter by id → RLS hides it → 404.
            r = client.get(f"/api/cover-letter?id={a_doc_id}", headers=self._auth(B_TOKEN))
            assert r.status_code == 404, r.get_data(as_text=True)

            # B tries to delete A's cover letter → 404, and A's row survives.
            d = client.delete(f"/api/cover-letter/{a_doc_id}", headers=self._auth(B_TOKEN))
            assert d.status_code == 404

            still = (
                sb_a.table("documents").select("id").eq("id", a_doc_id).execute().data
            )
            assert still, "RLS leak: B deleted A's cover letter"
        finally:
            # Cleanup: A removes its own row.
            sb_a.rpc(
                "delete_document_safely",
                {"p_document_id": a_doc_id, "p_document_type": "cover_letter"},
            ).execute()
