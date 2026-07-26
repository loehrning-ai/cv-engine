"""Stage 5 tests — structured cover letter + why_fit grounding (plan 002).

These lock the Stage-5 Gate-1 decisions on top of the existing Stage-5 suite
(``test_stage5_cover_letter.py``, which stays green):

  * The cover letter is a STRUCTURED business letter: the renderer lays out the
    recipient, subject, salutation, body, closing/signoff and an attachments
    (enclosures) block, and every structured field shows in the rendered HTML.
  * The body stays ESCAPED — NOT routed through ``markdown_inline``. A body the
    model accidentally writes with Markdown ``**bold**`` (and an em-dash) still
    PASSES the guards AND the rendered HTML shows NO literal ``**`` and NO
    ``<strong>`` tag (the escaped-body decision).
  * A generated structured letter passes ``run_guards`` and renders a valid
    PDF; a short (<=300-word) letter is 1 page (multi-page is still allowed).
  * The catalog's ``why_fit`` flows into ``company_context`` in the reviewed
    job packet path — null-safe (None when the column is absent pre-Stage-6).

Every LLM call is MOCKED (no API key). The renderer/guards are pure + offline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

from cover_letter import guards, jd  # noqa: E402
from cover_letter.renderer import (  # noqa: E402
    render_cover_letter_html,
)

# Reuse the canonical grounded CV + JD from the original Stage-5 suite so the
# grounding facts (Meta, Spark, Airflow, 40%, ETL) line up with the guards.
from test_stage5_cover_letter import (  # noqa: E402
    GROUNDED_CV,
    JD_TEXT,
)

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"


# A grounded body that uses Markdown **bold** + an em-dash. Every proper noun /
# number is present in GROUNDED_CV or the JD, so the guards must PASS — the only
# thing under test is that the body survives escaping with no literal ``**``.
MARKDOWN_BODY = (
    "At **Meta** I cut pipeline latency by **40%** across the ETL stack "
    "using Spark and Airflow — the exact stack this Senior Data Engineer "
    "role calls for.\n\n"
    "At Red Bull I built ML models for anomaly detection in *Python*. I bring "
    "the SQL and PyTorch depth your data-modeling work needs."
)


def _structured_doc(body: str) -> dict:
    """A fully-populated structured cover-letter render doc."""
    return {
        "sender_name": "Tim Loehr",
        "sender_contact": ["Warsaw, Poland", "tim@example.com"],
        "date": "22 June 2026",
        "recipient": "Dr. Anna Weber",
        "company": "Acme GmbH",
        "role": "Senior Data Engineer",
        "subject": "Application for Senior Data Engineer",
        "salutation": "Dear Dr. Weber,",
        "body": body,
        "closing": "Best regards,",
        "attachments": ["CV"],
        "signer_name": "Tim Loehr",
        "language": "en",
        "font": "sans",
        "accent": "#1f6f43",
    }


# ===========================================================================
#  1. Structured business-letter layout — every field renders.
# ===========================================================================
class TestStructuredLetterRenders:
    def test_all_structured_fields_appear_in_html(self):
        html = render_cover_letter_html(_structured_doc(MARKDOWN_BODY))
        # Sender block + date.
        assert "Tim Loehr" in html
        assert "Warsaw, Poland" in html
        assert "22 June 2026" in html
        # Recipient + company.
        assert "Dr. Anna Weber" in html
        assert "Acme GmbH" in html
        # Subject line (label-able; here the explicit override).
        assert "Application for Senior Data Engineer" in html
        # Salutation override.
        assert "Dear Dr. Weber," in html
        # Closing / signoff.
        assert "Best regards," in html
        # Attachments / enclosures block.
        assert "Enclosures" in html
        assert ">CV<" in html or "CV" in html

    def test_attachments_block_is_language_aware(self):
        doc = _structured_doc(MARKDOWN_BODY)
        doc["language"] = "de"
        doc["attachments"] = ["Lebenslauf"]
        html = render_cover_letter_html(doc)
        assert "Anlagen" in html
        assert "Lebenslauf" in html
        assert "Enclosures" not in html

    def test_defaults_when_envelope_absent(self):
        """With only a body + role + language, the renderer still derives a
        subject, salutation and closing (the body is never bare)."""
        html = render_cover_letter_html(
            {"body": "At Meta I cut latency by 40%.", "role": "Data Engineer", "language": "en"}
        )
        assert "Application for Data Engineer" in html  # derived subject
        assert "Dear Hiring Manager," in html  # derived salutation
        assert "Best regards," in html  # derived closing


# ===========================================================================
#  1b. Grounding regression (plan 110 Stage 1) — the JD's own employer/role and
#      a named recipient may be named in the letter; JD *requirements* still
#      cannot ground a candidate *claim* (the plan-109 boundary holds).
# ===========================================================================
class TestGroundingContextRegression:
    def test_labeled_company_grounds_even_when_saved_job_metadata_overrides(self):
        """Regression: a saved job supplies short display metadata (company
        "Co", title "Seeded") that overrides parse_jd's structured fields, but
        the richer "Company: Acme GmbH" / "Title: Senior Data Engineer" LABELS
        in the JD text must still ground a letter that names them. Before the
        fix this raised GroundingError(['Senior','Acme','GmbH','Acme GmbH'])."""
        jd_json = jd.parse_jd(JD_TEXT, title="Seeded", company="Co")
        # The override wins for the structured fields (prompt shape)…
        assert jd_json["company"] == "Co"
        assert jd_json["title"] == "Seeded"
        body = (
            "I am applying for the Senior Data Engineer role at Acme GmbH. "
            "At Meta I cut pipeline latency by 40% across the ETL stack using "
            "Spark and Airflow."
        )
        # …but the labeled values in the JD text still ground the letter.
        res = guards.run_guards(body, GROUNDED_CV, jd_context=jd_json)
        assert res.text  # not blocked

    def test_named_recipient_is_grounded_via_recipient_slot(self):
        """A letter addressed to a named hiring contact must not be blocked —
        the recipient threads into the JD context's recipient slot (mirrors
        cover_letter_service). Without it, the name is an unverified proper
        noun and IS blocked (proves the recipient slot is what grounds it)."""
        jd_json = jd.parse_jd(JD_TEXT)
        body = (
            "Dear Dr. Anna Weber,\n\nAt Meta I cut pipeline latency by 40% "
            "across the ETL stack using Spark and Airflow."
        )
        # With the recipient grounded → passes.
        res = guards.run_guards(
            body, GROUNDED_CV, jd_context={**jd_json, "recipient": "Dr. Anna Weber"}
        )
        assert res.text
        # Without it → the invented name is caught.
        with pytest.raises(guards.GroundingError) as exc:
            guards.run_guards(body, GROUNDED_CV, jd_context=jd_json)
        assert any("Weber" in item for item in exc.value.items)

    def test_compound_word_requirement_line_cannot_pose_as_a_label(self):
        """Plan 110 Stage 8 (adversarial): a JD REQUIREMENT line beginning with a
        compound word — 'Role-based …', 'Company-wide …' — must NOT be mined as a
        'Role'/'Company' LABEL and feed its skill/number tokens into the grounding
        corpus. The label separator now requires a colon or a whitespace-delimited
        dash, so a bare hyphen inside a word is not a separator. Regression: this
        exact input let JD-only Kubernetes/Terraform/40% claims through run_guards."""
        jd_json = jd.parse_jd(
            "Senior Platform Engineer at Initech\n"
            "Role-based access control with Kubernetes and Terraform required\n"
            "Company-wide impact: improve deploy speed by 40%\n",
            title="Platform Engineer", company="Initech",
        )
        body = (
            "At Meta I ran production Kubernetes clusters, wrote Terraform for the "
            "platform, and improved deploy speed by 40%."
        )
        with pytest.raises(guards.GroundingError) as exc:
            guards.run_guards(body, GROUNDED_CV, jd_context=jd_json)
        joined = " ".join(exc.value.items)
        assert "Kubernetes" in joined or "Terraform" in joined

    def test_whitespace_delimited_hyphen_label_still_grounds(self):
        """The tightened separator must still accept a legitimate ' - ' label so
        'Company - Acme Corp' grounds a letter naming Acme Corp."""
        jd_json = jd.parse_jd("Company - Acme Corp\nData Engineer role\n")
        assert jd_json["company"] == "Acme Corp"
        body = "At Meta I cut latency by 40%. I would love to bring this to Acme Corp."
        res = guards.run_guards(body, GROUNDED_CV, jd_context=jd_json)
        assert res.text  # Acme Corp is grounded via the hyphen label

    def test_jd_requirement_still_cannot_ground_a_candidate_claim(self):
        """The plan-109 boundary MUST hold: a skill that appears only in the
        JD's *requirements* (not the CV, not a title/company label) cannot
        ground the candidate claiming it. This proves Stage 1 did not
        over-loosen the guard into grounding-by-requirement."""
        jd_k = (
            "Title: Senior Data Engineer\n"
            "Company: Acme GmbH\n"
            "You must have deep Kubernetes and Terraform experience."
        )
        jd_json = jd.parse_jd(jd_k)
        body = (
            "At Meta I ran production Kubernetes clusters and wrote Terraform "
            "for the whole platform."  # Kubernetes/Terraform are JD-only
        )
        with pytest.raises(guards.GroundingError) as exc:
            guards.run_guards(body, GROUNDED_CV, jd_context=jd_json)
        offenders = " ".join(exc.value.items)
        assert "Kubernetes" in offenders or "Terraform" in offenders


# ===========================================================================
#  2. The escaped-body decision — Markdown is stripped, never rendered.
# ===========================================================================
class TestEscapedBody:
    def test_markdown_body_passes_guards(self):
        """A grounded body that happens to contain **bold** + em-dash passes
        the full guard chain (the markers don't introduce ungrounded facts)."""
        jd_json = jd.parse_jd(JD_TEXT)
        res = guards.run_guards(MARKDOWN_BODY, GROUNDED_CV, jd_context=jd_json)
        assert res.text  # not blocked
        assert res.word_count > 0

    def test_rendered_html_has_no_literal_double_star(self):
        """Locks the escaped-body decision: **bold** in the body renders as
        plain text — NO literal ``**`` and NO ``<strong>`` tag in the HTML."""
        html = render_cover_letter_html(_structured_doc(MARKDOWN_BODY))
        # The whole document must not show a literal Markdown bold marker.
        assert "**" not in html
        # And we did NOT route the body through markdown_inline → no emphasis tag
        # produced FROM THE BODY. (No <strong>/<em> anywhere in this doc.)
        assert "<strong>" not in html
        assert "<em>" not in html
        # The inner words survive the strip (the content is intact).
        assert "Meta" in html
        assert "40%" in html
        assert "Python" in html
        # The em-dash is preserved through escaping.
        assert "—" in html

    def test_body_is_html_escaped(self):
        """A body with HTML metacharacters is escaped (XSS-safe), not injected."""
        doc = _structured_doc("At Meta I built <script>alert(1)</script> & more.")
        html = render_cover_letter_html(doc)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp;" in html

    def test_json_array_body_tolerance(self):
        """A model that returns a JSON array of paragraphs still renders as
        separate <p> blocks (cheap robustness shim)."""
        doc = _structured_doc('["First paragraph about Meta.", "Second about Spark."]')
        html = render_cover_letter_html(doc)
        assert "First paragraph about Meta." in html
        assert "Second about Spark." in html
        assert html.count("<p>") >= 2


# ===========================================================================
#  3. A structured letter renders a valid PDF (1 page for a short body).
# ===========================================================================
class TestStructuredLetterPdf:
    def test_short_letter_is_one_page_pdf(self):
        pytest.importorskip("weasyprint")
        from weasyprint import HTML

        from cover_letter.renderer import render_cover_letter_pdf

        # The grounded markdown body is well under 300 words → a single page.
        assert guards.word_count(MARKDOWN_BODY) <= 300
        doc = _structured_doc(MARKDOWN_BODY)
        pdf = render_cover_letter_pdf(doc, base_url=f"file://{REPO}")
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 1000

        # Count pages directly via WeasyPrint's rendered document.
        html = render_cover_letter_html(doc, base_url=f"file://{REPO}")
        pages = HTML(string=html, base_url=f"file://{REPO}").render().pages
        assert len(pages) == 1, f"short letter should be 1 page, got {len(pages)}"

    def test_long_letter_multipage_allowed(self):
        """The cover-letter renderer has NO 1-page gate (only the CV does)."""
        pytest.importorskip("weasyprint")
        from cover_letter.renderer import render_cover_letter_pdf

        long_body = "\n\n".join(
            f"Paragraph {i} about Meta and Spark and Airflow at scale." for i in range(80)
        )
        doc = _structured_doc(long_body)
        pdf = render_cover_letter_pdf(doc, base_url=f"file://{REPO}")
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 1000


# ===========================================================================
#  4. why_fit flows into company_context (null-safe).
# ===========================================================================
def _seed_job(store, *, why_fit=None, jd_text=JD_TEXT):
    """Insert a saved_jobs row into the fake store; return its id.

    ``why_fit`` is included only when given, mirroring the pre-Stage-6 schema
    where the column doesn't exist yet (so ``job.get('why_fit')`` is None).
    """
    import uuid as _uuid

    rows = store.tables.setdefault("saved_jobs", [])
    job_id = str(_uuid.uuid4())
    row = {
        "id": job_id,
        "owner_id": store.owner_id,
        "title": "Senior Data Engineer",
        "company": "Acme GmbH",
        "jd_text": jd_text,
        "jd_json": jd.parse_jd(jd_text),
        "status": "saved",
        "deleted_at": None,
    }
    if why_fit is not None:
        row["why_fit"] = why_fit
    rows.append(row)
    return job_id


class TestWhyFitWiring:
    def test_why_fit_flows_into_company_context(
        self, server_module, fake_supabase, monkeypatch
    ):
        """apply_packet_review passes the job's why_fit into the cover-letter
        generator's company_context (the catalog grounding signal)."""
        import cover_letter_service as cl_service
        import jobs_service
        import tailor_service as tailor_svc

        seen: dict = {}

        def _fake_apply_tailor(*, cv_id, review_token, now_iso, expected_jd_text=None, **_kw):
            return {
                "tailored_cv_id": "tailored-1",
                "ats": {"score": 0.9, "profile": {"id": "semantic_fit"}},
                "source_hash": "source-hash",
                "jd_hash": "jd-hash",
                "tailored_hash": "tailored-hash",
                "review_id": "review-1",
            }

        def _fake_generate(*, cv_id, jd_text, tone="auto", company_context="", **kw):
            seen["company_context"] = company_context
            return {"id": "cover-1"}

        monkeypatch.setattr(tailor_svc, "apply_tailor_review", _fake_apply_tailor)
        monkeypatch.setattr(cl_service, "generate_cover_letter", _fake_generate)
        monkeypatch.setattr(
            jobs_service,
            "_ensure_pending_packet_row",
            lambda **_kw: {"id": "packet-1", "cover_letter_hash": "d" * 64},
        )

        why = "This candidate's ETL work at Meta maps directly to this role."
        job_id = _seed_job(fake_supabase, why_fit=why)
        out = jobs_service.apply_packet_review(
            job_id=job_id,
            source_cv_id="src-cv",
            review_token="review-token",
            now_iso="2026-06-22T00:00:00Z",
        )
        assert out["cover_letter_id"] == "cover-1"
        assert seen["company_context"] == why

    def test_why_fit_absent_is_null_safe(self, server_module, fake_supabase, monkeypatch):
        """Pre-Stage-6 (no why_fit column): job.get('why_fit') is None →
        company_context is an empty string, never a crash."""
        import cover_letter_service as cl_service
        import jobs_service
        import tailor_service as tailor_svc

        seen: dict = {}

        monkeypatch.setattr(
            tailor_svc,
            "apply_tailor_review",
            lambda *, cv_id, review_token, now_iso, expected_jd_text=None, **_kw: {
                "tailored_cv_id": "t",
                "ats": None,
                "source_hash": "source-hash",
                "jd_hash": "jd-hash",
                "tailored_hash": "tailored-hash",
                "review_id": "review-2",
            },
        )

        def _fake_generate(*, cv_id, jd_text, tone="auto", company_context="", **kw):
            seen["company_context"] = company_context
            return {"id": "cover-2"}

        monkeypatch.setattr(cl_service, "generate_cover_letter", _fake_generate)
        monkeypatch.setattr(
            jobs_service,
            "_ensure_pending_packet_row",
            lambda **_kw: {"id": "packet-2", "cover_letter_hash": "d" * 64},
        )

        job_id = _seed_job(fake_supabase, why_fit=None)  # column absent
        jobs_service.apply_packet_review(
            job_id=job_id,
            source_cv_id="src-cv",
            review_token="review-token",
            now_iso="2026-06-22T00:00:00Z",
        )
        assert seen["company_context"] == ""


# ===========================================================================
#  5. The service still writes the envelope fields into the stored blob.
# ===========================================================================
class TestBlobEnvelope:
    def test_serialize_roundtrip_carries_envelope(self):
        import cover_letter_service as cl_service

        blob = cl_service.serialize_blob(
            {
                "recipient": "Dr. Weber",
                "company": "Acme GmbH",
                "role": "Senior Data Engineer",
                "body": "At Meta I cut latency by 40%.",
                "language": "en",
                "subject": "Application for Senior Data Engineer",
                "salutation": None,
                "closing": None,
                "attachments": ["CV"],
                "source_cv_id": "cv-1",
                "job_id": "job-1",
            }
        )
        parsed = cl_service.parse_blob(blob)
        assert parsed["subject"] == "Application for Senior Data Engineer"
        assert parsed["attachments"] == ["CV"]
        assert parsed["recipient"] == "Dr. Weber"

    def test_build_render_doc_threads_envelope(self):
        import cover_letter_service as cl_service

        doc = cl_service.build_render_doc(
            {
                "recipient": "Dr. Weber",
                "company": "Acme GmbH",
                "role": "Senior Data Engineer",
                "body": "At Meta I cut latency by 40%.",
                "language": "en",
                "subject": "Application for Senior Data Engineer",
                "attachments": ["CV"],
            },
            name="Acme application",
            sender_name="Tim Loehr",
        )
        assert doc["subject"] == "Application for Senior Data Engineer"
        assert doc["attachments"] == ["CV"]
        assert doc["sender_name"] == "Tim Loehr"
        assert doc["signer_name"] == "Tim Loehr"

    def test_generate_writes_envelope_into_blob(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """End-to-end: a generated letter stores subject + attachments in the
        blob so the rendered PDF is a complete business letter."""
        import uuid as _uuid

        import yaml as _yaml

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")

        import cover_letter_service as cl

        class _FakeClient:
            def __init__(self, api_key):
                pass

            def complete(self, *, system, user, model=None, max_tokens=1024, cache_system=True):
                return (
                    "At Meta I cut pipeline latency by 40% with Spark and Airflow "
                    "across the ETL stack."
                )

        monkeypatch.setattr(cl, "make_client", lambda provider, api_key: _FakeClient(api_key))

        # Seed a CV + credits.
        docs = fake_supabase.tables.setdefault("documents", [])
        cv_id = str(_uuid.uuid4())
        docs.append(
            {
                "id": cv_id,
                "owner_id": fake_supabase.owner_id,
                "type": "cv",
                "name": "My CV",
                "yaml_blob": _yaml.safe_dump(GROUNDED_CV, sort_keys=False),
                "deleted_at": None,
                "updated_at": 1,
            }
        )
        fake_supabase.tables.setdefault("profiles", []).append(
            {"id": FAKE_OWNER, "managed_credits_remaining": 3}
        )

        res = client.post("/api/cover-letter/generate", json={"cv_id": cv_id, "jd_text": JD_TEXT})
        assert res.status_code == 201, res.get_data(as_text=True)

        stored = [d for d in docs if d["type"] == "cover_letter"]
        assert len(stored) == 1
        blob = cl.parse_blob(stored[0]["yaml_blob"])
        # Subject auto-derived from the JD role; CV travels as an enclosure.
        assert blob["subject"] == "Application for Senior Data Engineer"
        assert blob["attachments"] == ["CV"]
