"""Stage 6 tests — CV tailoring (duplicate-then-tailor) + deterministic ATS.

Covers the feature's safety contract and its determinism:

  * **duplicate-then-tailor**: tailoring creates a NEW cv document and leaves
    the source CV byte-identical (the headline guarantee — the original is
    never modified);
  * **anti-fabrication**: a tailored CV that introduces a skill/employer NOT in
    the source CV is BLOCKED (422 ``unverified_claims``) and NO document is
    persisted (rolled back);
  * **1-page gate**: an over-long tailored CV is caught by the EXISTING engine
    gate (409 ``overflow`` with the offending heading) and persists nothing; a
    normal tailored CV passes and stays one page;
  * **ATS score is deterministic**: a known CV + JD yields a STABLE score and a
    correct gap list (exact numbers/keywords asserted) — a pure function, no
    network, no LLM;
  * **cross-tenant**: user B cannot tailor / ATS-score user A's CV (404) — the
    real-JWT pattern, gated on env so it skips cleanly.

Every LLM call is MOCKED (ANTHROPIC_API_KEY is blank in CI). The DB-backed
routes use the in-memory ``fake_supabase`` fixture from conftest (whose
``rpc('consume_managed_credit')`` faithfully decrements + audits). The pure
ATS + applier tests need no fixtures at all.
"""
from __future__ import annotations

import json
import base64
import os
import sys
import uuid as _uuid
from pathlib import Path

import pytest
import yaml as _yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

from ats.score import score_cv_against_jd  # noqa: E402
from tailor.apply import apply_suggestions  # noqa: E402

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"

# A canonical CV used as the SOLE source of facts/evidence in every test.
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
            "bullets": [
                "Cut pipeline latency by 40% across the ETL stack.",
                "Built data models for analytics in Airflow.",
            ],
            "stack": "Python, Spark, Airflow",
        },
        {
            "role": "Data Scientist",
            "company": "Red Bull",
            "location": "Salzburg",
            "start": "2021",
            "end": "2023",
            "bullets": ["Built ML models for anomaly detection in Python."],
        },
    ],
    "skills": [{"label": "Data", "items": "Python, SQL, PyTorch, Spark"}],
}

JD_TEXT = (
    "Title: Senior Data Engineer\n"
    "Company: Acme GmbH\n"
    "You must have experience with Spark and Airflow. 5+ years required.\n"
    "Kubernetes and dbt are nice to have.\n"
    "Responsibilities: build ETL pipelines and data models."
)


# ===========================================================================
#  1. Deterministic ATS scorer — pure, offline, exact numbers.
# ===========================================================================
class TestAtsScorerDeterministic:
    def test_known_cv_and_jd_yield_stable_score_and_gaps(self):
        """The headline ATS assertion: exact score + exact gap list, twice."""
        res = score_cv_against_jd(GROUNDED_CV, JD_TEXT)
        # Spark + Airflow (required, weight 3 each) + ETL (nice_to_have, 1) are
        # backed; Kubernetes (nice_to_have, 1) is not. 4 keywords extracted
        # (company/title/section-headers filtered out).
        assert res.extracted_keyword_count == 4
        # score = round(100 * (3+3+1) / (3+3+1+1)) = round(87.5) = 88.
        assert res.score == 88
        # Both must-haves are covered → 100% tier-1 coverage.
        assert res.tier1_coverage == 100
        assert res.tier1_target == 70

        matched_terms = {m.term for m in res.matched}
        assert {"Spark", "Airflow", "ETL"} <= matched_terms
        # Every match cites real CV evidence.
        assert all(m.evidence for m in res.matched)

        # The gap list is exactly the one unmet keyword.
        gap_terms = [g.term for g in res.gaps]
        assert gap_terms == ["Kubernetes"]
        assert res.gaps[0].importance == "nice_to_have"
        # No company name / job title / section header leaked into the gaps.
        assert "Acme GmbH" not in gap_terms
        assert "Senior Data Engineer" not in gap_terms
        assert "Responsibilities" not in gap_terms

    def test_score_is_repeatable(self):
        a = score_cv_against_jd(GROUNDED_CV, JD_TEXT).to_json()
        b = score_cv_against_jd(GROUNDED_CV, JD_TEXT).to_json()
        assert a == b  # byte-for-byte stable — no randomness, no network

    def test_required_keyword_absent_appears_in_gaps_not_invented(self):
        """A must-have the CV can't back lands in gaps (apply-vs-upskill)."""
        jd = (
            "Title: Platform Engineer\n"
            "You must have experience with Kubernetes and Terraform. Required.\n"
            "Go is required."
        )
        res = score_cv_against_jd(GROUNDED_CV, jd)
        gap_terms = {g.term for g in res.gaps}
        # The CV has none of these → all are honest gaps, never fabricated in.
        assert "Kubernetes" in gap_terms
        assert "Terraform" in gap_terms
        assert all(g.importance == "required" for g in res.gaps if g.term in {"Kubernetes", "Terraform", "Go"})
        # And the score reflects the poor match.
        assert res.score is not None and res.score < 50

    def test_thin_jd_returns_none_score(self):
        res = score_cv_against_jd(GROUNDED_CV, "We are hiring great people.")
        assert res.score is None
        assert "thin" in res.notes.lower() or "too" in res.notes.lower()

    def test_alias_matches_k8s(self):
        """k8s in the JD is backed by 'Kubernetes' in the CV (alias-aware)."""
        cv = {"skills": [{"label": "Infra", "items": "Kubernetes, Docker, AWS"}]}
        jd = "You must have K8s and AWS experience. Required. Plus Docker."
        res = score_cv_against_jd(cv, jd)
        assert {m.term for m in res.matched} >= {"K8s", "AWS", "Docker"}
        assert res.gaps == []

    def test_substring_does_not_false_match(self):
        """'Java' (JD) must NOT match 'JavaScript' (CV) — whole-word only."""
        cv = {"skills": [{"label": "Web", "items": "JavaScript, TypeScript, React"}]}
        jd = "You must have Java and Spring experience. Required. React a plus."
        res = score_cv_against_jd(cv, jd)
        gap_terms = {g.term for g in res.gaps}
        assert "Java" in gap_terms  # not satisfied by JavaScript
        assert "Spring" in gap_terms
        assert any(m.term == "React" for m in res.matched)


# ===========================================================================
#  2. Deterministic suggestion applier — immutable, defensive.
# ===========================================================================
class TestApplySuggestions:
    def test_original_model_not_mutated(self):
        import copy

        original = copy.deepcopy(GROUNDED_CV)
        suggestions = [
            {"id": "r", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": "Reduced ETL pipeline latency by 40%."},
        ]
        new_cv, applied, _skipped = apply_suggestions(GROUNDED_CV, suggestions)
        assert GROUNDED_CV == original  # input untouched (immutability)
        assert applied == ["r"]
        assert new_cv["experience"][0]["bullets"][0] == "Reduced ETL pipeline latency by 40%."

    def test_stale_before_rewrite_is_skipped(self):
        suggestions = [
            {"id": "r", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Different stale bullet.",
             "after": "Reduced ETL pipeline latency by 40%."},
        ]
        new_cv, applied, skipped = apply_suggestions(GROUNDED_CV, suggestions)
        assert applied == []
        assert skipped == ["r: not applicable"]
        assert new_cv["experience"][0]["bullets"][0] == "Cut pipeline latency by 40% across the ETL stack."

    def test_missing_before_rewrite_is_skipped(self):
        suggestions = [
            {"id": "r", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "after": "Reduced ETL pipeline latency by 40%."},
        ]
        new_cv, applied, skipped = apply_suggestions(GROUNDED_CV, suggestions)
        assert applied == []
        assert skipped == ["r: not applicable"]
        assert new_cv["experience"][0]["bullets"][0] == "Cut pipeline latency by 40% across the ETL stack."

    def test_reorder_bullets_is_a_permutation(self):
        sugs = [{"id": "ro", "op": "reorder_bullets", "target": "experience", "index": 0, "order": [1, 0]}]
        new_cv, applied, _ = apply_suggestions(GROUNDED_CV, sugs)
        assert applied == ["ro"]
        assert new_cv["experience"][0]["bullets"][0] == "Built data models for analytics in Airflow."

    def test_malformed_ops_skipped_not_crashing(self):
        sugs = [
            {"id": "bad-idx", "op": "rewrite_bullet", "target": "experience", "index": 99, "bullet_index": 0, "after": "x"},
            {"id": "bad-order", "op": "reorder_bullets", "target": "experience", "index": 0, "order": [0, 0]},
            {"id": "unknown", "op": "teleport", "target": "experience"},
            {"id": "touch-name", "op": "rewrite_summary", "target": "name", "after": "Hacker"},
        ]
        new_cv, applied, skipped = apply_suggestions(GROUNDED_CV, sugs)
        assert applied == []
        assert len(skipped) == 4
        assert new_cv["name"] == "Tim Loehr"  # protected key untouched

    def test_summary_block_is_advisory_not_an_unanchored_mutation(self):
        new_cv, applied, _ = apply_suggestions(
            GROUNDED_CV, [], summary={"recommended": True, "text": "Data engineer with Spark depth."}
        )
        assert "summary" not in new_cv
        assert applied == []
        new_cv2, applied2, _ = apply_suggestions(
            GROUNDED_CV, [], summary={"recommended": False, "text": "ignored"}
        )
        assert "summary" not in new_cv2
        assert applied2 == []


# ===========================================================================
#  Mock helpers for the route-level tests.
# ===========================================================================
def _structured_response(suggestions=None, summary=None, warnings=None) -> str:
    """A valid structured tailoring JSON the mock LLM returns."""
    return json.dumps(
        {
            "fit": {"score": 80, "verdict": "Strong match", "target_role": "Senior Data Engineer"},
            "keywords": {"present": [], "missing": [], "inferable": []},
            "suggestions": suggestions or [],
            "summary": summary or {"recommended": False, "text": "", "rationale": ""},
            "warnings": warnings or [],
        }
    )


def _mock_llm(monkeypatch, raw_text: str, seen: dict | None = None):
    """Patch tailor_service.make_client so .complete returns ``raw_text``."""
    import tailor_service as ts

    class _FakeClient:
        def __init__(self, api_key):
            if seen is not None:
                seen["api_key"] = api_key

        def complete(self, *, system, user, model=None, max_tokens=1024, cache_system=True):
            if seen is not None:
                seen["system"] = system
                seen["user"] = user
                seen["model"] = model
                seen["max_tokens"] = max_tokens
            return raw_text

    monkeypatch.setattr(ts, "make_client", lambda provider, api_key: _FakeClient(api_key))


def _seed_cv(store, name="My CV", cv_model=None):
    """Insert a CV document into the fake store; return (id, yaml_blob)."""
    blob = _yaml.safe_dump(cv_model or GROUNDED_CV, sort_keys=False, allow_unicode=True)
    rows = store.tables.setdefault("documents", [])
    doc_id = str(_uuid.uuid4())
    rows.append(
        {
            "id": doc_id,
            "owner_id": store.owner_id,
            "type": "cv",
            "name": name,
            "yaml_blob": blob,
            "accent": "#1f6f43",
            "font": "sans",
            "deleted_at": None,
            "updated_at": 1,
        }
    )
    return doc_id, blob


def _seed_profile(store, owner_id, *, credits):
    store.tables.setdefault("profiles", []).append(
        {"id": owner_id, "managed_credits_remaining": credits}
    )


def _seed_job(store, *, jd_text=JD_TEXT):
    job_id = str(_uuid.uuid4())
    store.tables.setdefault("saved_jobs", []).append(
        {
            "id": job_id,
            "owner_id": store.owner_id,
            "title": "Senior Data Engineer",
            "company": "Acme GmbH",
            "status": "saved",
            "jd_text": jd_text,
            "jd_json": {"title": "Senior Data Engineer", "company": "Acme GmbH"},
            "updated_at": 2,
        }
    )
    return job_id


def _cv_docs(store):
    return [d for d in store.tables.get("documents", []) if d["type"] == "cv"]


# ===========================================================================
#  3. Duplicate-then-tailor — original preserved, new doc created.
# ===========================================================================
class TestDuplicateThenTailor:
    def test_review_token_secret_fails_closed_without_secret_outside_demo(self, server_module, monkeypatch):
        """Default/production mode must not sign review tokens with a public fallback."""
        monkeypatch.delenv("ONEPAGER_REVIEW_TOKEN_SECRET", raising=False)
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)

        with pytest.raises(server_module.tailor_svc.TailorError) as excinfo:
            server_module.tailor_svc._review_token_secret()

        assert excinfo.value.status == 503
        assert excinfo.value.payload["code"] == "review_secret_missing"

    def test_review_token_secret_allows_local_demo_fallback(self, server_module, monkeypatch):
        """Explicit demo mode keeps the offline local review/apply flow usable."""
        monkeypatch.delenv("ONEPAGER_REVIEW_TOKEN_SECRET", raising=False)
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")

        assert server_module.tailor_svc._review_token_secret() == b"onepager-local-demo-tailor-review-token"

    def test_review_returns_diff_token_without_creating_copy(self, server_module, client, fake_supabase, monkeypatch):
        """Review is explicit: it produces a signed apply token and no document."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {"id": "tighten", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": "Cut ETL pipeline latency 40% with Spark and Airflow."},
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, original_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor/review", json={"jd_text": JD_TEXT})
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["review_token"]
        assert body["review_id"]
        assert body["source_cv_id"] == cv_id
        assert len(body["source_hash"]) == 64
        assert body["source_revision"] == "1"
        assert len(body["jd_hash"]) == 64
        assert len(body["tailored_hash"]) == 64
        assert body["provider"] == "anthropic"
        assert body["created_at"]
        assert body["diff"]["changed"] is True
        assert any("Cut ETL pipeline latency" in line for line in body["diff"]["lines"])
        token_body = body["review_token"].split(".", 1)[0]
        token_claims = json.loads(base64.urlsafe_b64decode(token_body + "=" * (-len(token_body) % 4)))
        serialized_claims = json.dumps(token_claims)
        assert "tailored_yaml" not in serialized_claims
        assert "Cut ETL pipeline latency" not in serialized_claims
        assert "experience:" not in serialized_claims
        assert token_claims["review_id"] == body["review_id"]
        assert token_claims["tailored_hash"] == body["tailored_hash"]
        review_rows = fake_supabase.tables["tailor_review_states"]
        assert len(review_rows) == 1
        assert review_rows[0]["id"] == body["review_id"]
        assert review_rows[0]["state_digest"] == token_claims["state_digest"]
        assert "state_json" not in review_rows[0]
        ciphertext = review_rows[0]["state_ciphertext"]
        assert ciphertext.startswith("v1:")
        serialized_row = json.dumps(review_rows[0], sort_keys=True)
        assert "tailored_yaml" not in serialized_row
        assert "experience:" not in serialized_row
        assert "Cut ETL pipeline latency" not in serialized_row
        decrypted = server_module.tailor_svc._decrypt_review_state(
            ciphertext,
            review_id=body["review_id"],
            digest=token_claims["state_digest"],
        )
        assert "tailored_yaml" in decrypted
        assert "Cut ETL pipeline latency" in decrypted["tailored_yaml"]
        docs = _cv_docs(fake_supabase)
        assert len(docs) == 1
        assert docs[0]["yaml_blob"] == original_blob

    def test_tampered_review_ciphertext_rejects_apply_without_copy(self, server_module, client, fake_supabase, monkeypatch):
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {"id": "tighten", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": "Cut ETL pipeline latency 40% with Spark and Airflow."},
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, original_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor/review", json={"jd_text": JD_TEXT})
        assert res.status_code == 200, res.get_data(as_text=True)
        review = res.get_json()
        review_rows = fake_supabase.tables["tailor_review_states"]
        review_rows[0]["state_ciphertext"] = review_rows[0]["state_ciphertext"][:-2] + "xx"

        apply_res = client.post(f"/api/cv/{cv_id}/tailor/apply", json={"review_token": review["review_token"]})

        assert apply_res.status_code == 400, apply_res.get_data(as_text=True)
        assert apply_res.get_json()["code"] == "invalid_review_token"
        docs = _cv_docs(fake_supabase)
        assert len(docs) == 1
        assert docs[0]["yaml_blob"] == original_blob

    def test_apply_review_creates_copy_and_preserves_original(self, server_module, client, fake_supabase, monkeypatch):
        """A reviewed token becomes exactly one new CV when explicitly applied."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {"id": "tighten", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": "Cut ETL pipeline latency 40% with Spark and Airflow."},
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, original_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        review = client.post(f"/api/cv/{cv_id}/tailor/review", json={"jd_text": JD_TEXT}).get_json()

        res = client.post(
            f"/api/cv/{cv_id}/tailor/apply",
            json={"review_token": review["review_token"]},
        )

        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        new_id = body["tailored_cv_id"]
        assert new_id != cv_id
        assert body["source_cv_id"] == cv_id
        assert body["review_id"] == review["review_id"]
        assert body["source_hash"] == review["source_hash"]
        assert body["source_revision"] == review["source_revision"]
        assert body["jd_hash"] == review["jd_hash"]
        assert body["tailored_hash"] == review["tailored_hash"]
        assert body["job_id"] is None
        docs = _cv_docs(fake_supabase)
        assert len(docs) == 2
        source = next(d for d in docs if d["id"] == cv_id)
        tailored = next(d for d in docs if d["id"] == new_id)
        assert source["yaml_blob"] == original_blob
        assert "Cut ETL pipeline latency 40%" in tailored["yaml_blob"]

    def test_job_linked_review_apply_preserves_selected_job_lineage(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Tailor review/apply keeps selected-job lineage and preserves the source CV."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {"id": "tighten", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": "Cut ETL pipeline latency 40% with Spark and Airflow."},
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, original_blob = _seed_cv(fake_supabase)
        job_id = _seed_job(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        review_res = client.post(
            f"/api/cv/{cv_id}/tailor/review",
            json={"job_id": job_id, "profile": "semantic_fit"},
        )
        review = review_res.get_json()

        assert review_res.status_code == 200, review_res.get_data(as_text=True)
        assert review["job_id"] == job_id
        assert review["source_cv_id"] == cv_id
        assert len(review["source_hash"]) == 64
        assert len(review["jd_hash"]) == 64
        assert len(review["tailored_hash"]) == 64
        assert len(_cv_docs(fake_supabase)) == 1

        applied = client.post(
            f"/api/cv/{cv_id}/tailor/apply",
            json={"review_token": review["review_token"]},
        )

        assert applied.status_code == 201, applied.get_data(as_text=True)
        body = applied.get_json()
        assert body["job_id"] == job_id
        assert body["review_id"] == review["review_id"]
        assert body["source_hash"] == review["source_hash"]
        assert body["jd_hash"] == review["jd_hash"]
        assert body["tailored_hash"] == review["tailored_hash"]
        docs = _cv_docs(fake_supabase)
        source = next(d for d in docs if d["id"] == cv_id)
        tailored = next(d for d in docs if d["id"] == body["tailored_cv_id"])
        assert source["yaml_blob"] == original_blob
        assert source.get("job_id") is None
        assert tailored["job_id"] == job_id
        assert "Cut ETL pipeline latency 40%" in tailored["yaml_blob"]

    def test_job_linked_apply_rejects_changed_job_description(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(monkeypatch, _structured_response([]))
        cv_id, _original_blob = _seed_cv(fake_supabase)
        job_id = _seed_job(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        review = client.post(
            f"/api/cv/{cv_id}/tailor/review",
            json={"job_id": job_id, "profile": "semantic_fit"},
        ).get_json()
        job = next(row for row in fake_supabase.tables["saved_jobs"] if row["id"] == job_id)
        job["jd_text"] = "Changed saved job description."

        res = client.post(f"/api/cv/{cv_id}/tailor/apply", json={"review_token": review["review_token"]})

        assert res.status_code == 409, res.get_data(as_text=True)
        assert res.get_json()["code"] == "job_description_changed"
        assert len(_cv_docs(fake_supabase)) == 1

    def test_apply_review_rejects_changed_source_revision(self, server_module, client, fake_supabase, monkeypatch):
        """The apply token is bound to the source hash and revision."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(monkeypatch, _structured_response([]))
        cv_id, original_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        review = client.post(f"/api/cv/{cv_id}/tailor/review", json={"jd_text": JD_TEXT}).get_json()
        source = next(d for d in _cv_docs(fake_supabase) if d["id"] == cv_id)
        source["yaml_blob"] = original_blob + "\n# user edit after review\n"
        source["updated_at"] = "newer-revision"

        res = client.post(
            f"/api/cv/{cv_id}/tailor/apply",
            json={"review_token": review["review_token"]},
        )

        assert res.status_code == 409, res.get_data(as_text=True)
        assert res.get_json()["code"] == "source_changed"
        assert len(_cv_docs(fake_supabase)) == 1

    def test_apply_review_token_is_single_use(self, server_module, client, fake_supabase, monkeypatch):
        """Double-click/retry on the same reviewed token must not duplicate CVs."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(monkeypatch, _structured_response([]))
        cv_id, _original_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        review = client.post(f"/api/cv/{cv_id}/tailor/review", json={"jd_text": JD_TEXT}).get_json()

        first = client.post(f"/api/cv/{cv_id}/tailor/apply", json={"review_token": review["review_token"]})
        second = client.post(f"/api/cv/{cv_id}/tailor/apply", json={"review_token": review["review_token"]})

        assert first.status_code == 201, first.get_data(as_text=True)
        assert second.status_code == 409, second.get_data(as_text=True)
        assert second.get_json()["code"] == "duplicate_review_apply"
        assert len(_cv_docs(fake_supabase)) == 2

    def test_tailor_review_state_migration_is_rls_scoped(self):
        sql = Path("supabase/migrations/0016_tailor_review_states.sql").read_text(encoding="utf-8")

        assert "create table if not exists public.tailor_review_states" in sql
        assert "state_ciphertext text not null" in sql
        assert "tailor_review_states_ciphertext_size_check" in sql
        assert "char_length(state_ciphertext) between 32 and 250000" in sql
        assert "state_json" not in sql
        assert "alter table public.tailor_review_states enable row level security" in sql
        for policy in (
            "tailor_review_states_select_own",
            "tailor_review_states_insert_own",
            "tailor_review_states_update_own",
        ):
            assert policy in sql
            block = sql.split(f'create policy "{policy}"', 1)[1].split(";", 1)[0]
            assert "and p.deletion_requested_at is not null" in block
        select_grant = sql.split("grant select (", 1)[1].split(")", 1)[0]
        assert "state_ciphertext" not in select_grant
        assert "state_json" not in select_grant
        assert "grant select, insert on public.tailor_review_states" not in sql
        assert "revoke select on public.tailor_review_states from authenticated" in sql
        assert "revoke insert on public.tailor_review_states from authenticated" in sql
        assert "revoke update on public.tailor_review_states from authenticated" in sql
        assert "grant update" not in sql
        assert "revoke delete on public.tailor_review_states from authenticated" in sql

    def test_tailor_creates_copy_and_preserves_original(self, server_module, client, fake_supabase, monkeypatch):
        """The headline guarantee: tailoring DUPLICATES (a new cv doc appears)
        and the SOURCE CV's yaml_blob is byte-identical afterwards."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        seen: dict = {}
        # A grounded reorder/rewrite that only re-uses CV facts.
        suggestions = [
            {"id": "reorder", "op": "reorder_bullets", "target": "experience", "index": 0, "order": [1, 0]},
            {"id": "rewrite", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Built data models for analytics in Airflow.",
             "after": "Built analytics data models in Airflow."},
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions), seen)
        cv_id, original_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        assert len(_cv_docs(fake_supabase)) == 1  # only the source exists

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT})
        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        new_id = body["tailored_cv_id"]
        assert new_id != cv_id

        docs = _cv_docs(fake_supabase)
        assert len(docs) == 2, "tailoring must create a NEW cv document"

        # The ORIGINAL is byte-identical (never modified).
        source = next(d for d in docs if d["id"] == cv_id)
        assert source["yaml_blob"] == original_blob, "source CV must be unchanged"
        assert source["name"] == "My CV"

        # The new doc is named "… — tailored for {company}".
        tailored = next(d for d in docs if d["id"] == new_id)
        assert "tailored for Acme GmbH" in (tailored["name"] or "")

        # The cheaper Haiku tier was used for the call (runtime model choice).
        assert seen["model"] == "claude-haiku-4-5"
        assert seen["max_tokens"] == 2000
        # The grounded prompt actually carried the CV + JD.
        assert "Meta" in seen["user"] and "Senior Data Engineer" in seen["user"]

        # The response carries the deterministic ATS score + gaps.
        assert body["ats"]["score"] == 88
        assert [g["term"] for g in body["gaps"]] == ["Kubernetes"]
        assert body["ats"]["experience_fit"]["required_years"] == 5.0
        assert body["ats"]["experience_fit"]["candidate_years"] >= 3.0
        assert body["used_managed_credit"] is True

    def test_review_ats_scores_tailored_artifact_not_stale_source(self, server_module, client, fake_supabase, monkeypatch):
        """Review ATS evidence is computed after applying reviewed edits."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        cv_model = {
            "name": "Tim Loehr",
            "contact": [{"network": "mail", "username": "tim@example.com"}],
            "experience": [
                {
                    "role": "Platform Engineer",
                    "company": "Meta",
                    "start": "2020",
                    "end": "2024",
                    "bullets": ["Built Kubernetes platforms in Python."],
                    "stack": "Kubernetes, Python",
                }
            ],
            "skills": [{"label": "Core", "items": "Kubernetes, Python"}],
        }
        jd_text = (
            "Title: Platform Engineer\n"
            "Company: InfraCo\n"
            "You must have Kubernetes, Terraform, and Docker. Required. Python is helpful."
        )
        source_score = score_cv_against_jd(cv_model, jd_text).score
        suggestions = [
            {"id": "retag", "op": "retag_stack", "target": "experience", "index": 0,
             "before": "Kubernetes, Python", "after": "Python"},
            {
                "id": "rewrite",
                "op": "rewrite_bullet",
                "target": "experience",
                "index": 0,
                "bullet_index": 0,
                "before": "Built Kubernetes platforms in Python.",
                "after": "Built platforms in Python.",
            },
            {"id": "skills", "op": "add_emphasis", "target": "skills", "index": 0, "field": "items",
             "before": "Kubernetes, Python", "after": "Python"},
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, _original_blob = _seed_cv(fake_supabase, cv_model=cv_model)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor/review", json={"jd_text": jd_text})

        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert source_score is not None and source_score > body["ats"]["score"]
        assert "Kubernetes" in [g["term"] for g in body["gaps"]]
        assert body["ats"]["experience_fit"]["candidate_years"] == 5.0

    def test_tailor_returns_selected_profile_and_comparison_metadata(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(monkeypatch, _structured_response([]))
        cv_id, _original_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post(
            f"/api/cv/{cv_id}/tailor",
            json={"jd_text": JD_TEXT, "profile": "strict", "compare_profiles": ["recruiter"]},
        )

        assert res.status_code == 201, res.get_data(as_text=True)
        ats = res.get_json()["ats"]
        assert ats["profile"]["id"] == "strict_requirements"
        assert ats["score"] == ats["tier1_coverage"]
        assert set(ats["profile_results"]) == {"semantic_fit"}
        assert ats["profile_results"]["semantic_fit"]["profile"]["id"] == "semantic_fit"

    def test_tailor_with_no_suggestions_still_duplicates_one_page(self, server_module, client, fake_supabase, monkeypatch):
        """An empty suggestion set still duplicates the (unchanged) CV, and it
        passes the 1-page gate because it's identical to the 1-page source."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(monkeypatch, _structured_response([]))
        cv_id, original_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT})
        assert res.status_code == 201, res.get_data(as_text=True)
        assert len(_cv_docs(fake_supabase)) == 2
        # Original still byte-identical.
        source = next(d for d in _cv_docs(fake_supabase) if d["id"] == cv_id)
        assert source["yaml_blob"] == original_blob


# ===========================================================================
#  4. Anti-fabrication guard — a non-source claim BLOCKS, nothing persists.
# ===========================================================================
class TestAntiFabrication:
    def test_invented_employer_blocks_and_does_not_persist(self, server_module, client, fake_supabase, monkeypatch):
        """The LLM rewrites a bullet to name an employer NOT in the source CV.
        The deterministic cross-reference rejects it (422 unverified_claims)
        and NO tailored document is persisted (rolled back)."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        # A rewrite that injects "Globex" (an employer absent from the CV).
        suggestions = [
            {"id": "fab", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": "At Globex Industries I cut latency by 40%."},
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, original_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT})
        assert res.status_code == 422, res.get_data(as_text=True)
        body = res.get_json()
        assert body["error"] == "unverified_claims"
        assert "Globex" in body["items"]

        # NOTHING was persisted — only the original CV remains, byte-identical.
        docs = _cv_docs(fake_supabase)
        assert len(docs) == 1
        assert docs[0]["yaml_blob"] == original_blob

    def test_invented_skill_blocks(self, server_module, client, fake_supabase, monkeypatch):
        """An add_emphasis that injects a skill (Kubernetes) the CV never names
        is rejected — surfacing a gap is fine, asserting it is not."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {"id": "fake-skill", "op": "add_emphasis", "target": "skills", "index": 0,
             "field": "items", "before": "Python, SQL, PyTorch, Spark",
             "after": "Python, SQL, PyTorch, Spark, Kubernetes"},
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, _blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT})
        assert res.status_code == 422
        assert "Kubernetes" in res.get_json()["items"]
        assert len(_cv_docs(fake_supabase)) == 1  # not persisted


# ===========================================================================
#  5. 1-page gate — an over-long tailored CV is caught (409 overflow).
# ===========================================================================
class TestOnePageGate:
    def test_overlong_tailored_cv_overflows_and_blocks(self, server_module, client, fake_supabase, monkeypatch):
        """The LLM rewrites a bullet into a huge (still-grounded) block that
        spills the CV onto page 2. The EXISTING engine gate catches it: 409
        overflow with the offending heading, and nothing is persisted."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        # Build a giant bullet entirely out of CV-grounded tokens (so it passes
        # the anti-fabrication guard) but is long enough to overflow one page.
        grounded_words = "Python Spark Airflow ETL data models latency analytics Meta "
        huge = (grounded_words * 400).strip()
        suggestions = [
            {"id": "blow-up", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": huge},
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, original_blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT})
        assert res.status_code == 409, res.get_data(as_text=True)
        body = res.get_json()
        assert body["error"] == "overflow"
        assert body["pages"] >= 2
        assert "heading" in body
        assert "Trim a bullet" in body["hint"]

        # Nothing persisted — the source survives byte-identical.
        docs = _cv_docs(fake_supabase)
        assert len(docs) == 1
        assert docs[0]["yaml_blob"] == original_blob

    def test_normal_tailored_cv_stays_one_page(self, server_module, client, fake_supabase, monkeypatch):
        """A reasonable rewrite passes the gate and is saved as a 1-page CV."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {"id": "tighten", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": "Cut ETL pipeline latency 40% with Spark and Airflow."},
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, _blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT})
        assert res.status_code == 201, res.get_data(as_text=True)
        # The tailored doc renders to exactly one page via /api/build. Pin the
        # direct-stream fallback (R2 off) so this asserts the %PDF byte contract
        # regardless of an ambient .env with R2 creds (Stage 8's R2 path is
        # covered separately in test_stage8_pdf_store.py).
        for _v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
            monkeypatch.delenv(_v, raising=False)
        new_id = res.get_json()["tailored_cv_id"]
        build = client.post("/api/build", json={"id": new_id})
        assert build.status_code == 200
        assert build.get_data().startswith(b"%PDF")


# ===========================================================================
#  6. Standalone ATS-score route — no LLM, no document created.
# ===========================================================================
class TestAtsScoreRoute:
    def test_score_route_returns_deterministic_result(self, server_module, client, fake_supabase, monkeypatch):
        cv_id, _blob = _seed_cv(fake_supabase)
        res = client.post("/api/ats-score", json={"cv_id": cv_id, "jd_text": JD_TEXT})
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["score"] == 88
        assert [g["term"] for g in body["gaps"]] == ["Kubernetes"]
        assert body["experience_fit"]["required_years"] == 5.0
        assert "confidence" not in body["experience_fit"]
        # No document was created — scoring is read-only.
        assert len(_cv_docs(fake_supabase)) == 1

    def test_score_route_rubric_result_keeps_experience_fit(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(
            monkeypatch,
            json.dumps(
                {
                    "score": 75,
                    "categories": {
                        "open_source": {"score": 40, "max": 100, "evidence": [], "reasoning": "r"},
                        "self_projects": {"score": 50, "max": 100, "evidence": [], "reasoning": "r"},
                        "production": {
                            "score": 85,
                            "max": 100,
                            "evidence": ["Data Engineer at Meta"],
                            "reasoning": "r",
                        },
                        "technical_skills": {
                            "score": 75,
                            "max": 100,
                            "evidence": ["Python, Spark, Airflow"],
                            "reasoning": "r",
                        },
                    },
                    "strengths": ["Strong production experience at Meta"],
                    "gaps": ["No Kubernetes shown"],
                    "suggestions": ["Surface the Airflow work from your Meta role"],
                    "bonus": 0,
                    "deductions": 0,
                }
            ),
        )
        cv_id, _blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=1)

        res = client.post("/api/ats-score", json={"cv_id": cv_id, "jd_text": JD_TEXT})

        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["source"] == "rubric"
        assert body["experience_fit"]["required_years"] == 5.0
        assert body["experience_fit"]["candidate_years"] >= 3.0

    def test_score_route_unknown_cv_is_404(self, server_module, client, fake_supabase):
        res = client.post("/api/ats-score", json={"cv_id": str(_uuid.uuid4()), "jd_text": JD_TEXT})
        assert res.status_code == 404

    def test_score_route_missing_jd_is_400(self, server_module, client, fake_supabase):
        cv_id, _blob = _seed_cv(fake_supabase)
        res = client.post("/api/ats-score", json={"cv_id": cv_id})
        assert res.status_code == 400

    def test_score_route_rejects_linkedin_jd_url_before_fetch(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import jobs_service as js

        def _boom(*_a, **_k):
            raise AssertionError("LinkedIn JD URLs must not be fetched")

        monkeypatch.setenv("ONEPAGER_JOB_URL_ALLOWLIST", "linkedin.com,www.linkedin.com")
        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _boom)
        cv_id, _blob = _seed_cv(fake_supabase)

        res = client.post("/api/ats-score", json={"cv_id": cv_id, "jd_url": "https://www.linkedin.com/jobs/view/123"})

        assert res.status_code == 400
        assert res.get_json()["code"] == "linkedin_manual_required"

    def test_score_route_allowlisted_jd_url_uses_jobs_url_policy(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import jobs_service as js

        called = []

        def _fake_fetch(url, *, timeout=8.0, max_bytes=64 * 1024):
            called.append((url, timeout, max_bytes))
            return JD_TEXT.encode("utf-8")

        monkeypatch.setenv("ONEPAGER_JOB_URL_ALLOWLIST", "jobs.example.com")
        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _fake_fetch)
        cv_id, _blob = _seed_cv(fake_supabase)

        res = client.post("/api/ats-score", json={"cv_id": cv_id, "jd_url": "https://jobs.example.com/posting/123"})

        assert res.status_code == 200, res.get_data(as_text=True)
        assert called == [("https://jobs.example.com/posting/123", 8, js.MAX_JD_BYTES)]


# ===========================================================================
#  7. Route input validation + credit/key path parity.
# ===========================================================================
class TestRouteValidation:
    def test_tailor_invalid_cv_id_is_400(self, server_module, client, fake_supabase):
        res = client.post("/api/cv/not-a-uuid/tailor", json={"jd_text": JD_TEXT})
        assert res.status_code == 400

    def test_tailor_missing_jd_is_400(self, server_module, client, fake_supabase):
        cv_id, _blob = _seed_cv(fake_supabase)
        res = client.post(f"/api/cv/{cv_id}/tailor", json={})
        assert res.status_code == 400

    def test_tailor_review_rejects_linkedin_jd_url_before_fetch(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import jobs_service as js

        def _boom(*_a, **_k):
            raise AssertionError("LinkedIn JD URLs must not be fetched")

        monkeypatch.setenv("ONEPAGER_JOB_URL_ALLOWLIST", "linkedin.com,www.linkedin.com")
        monkeypatch.setattr(js.ssrf, "safe_fetch_url", _boom)
        cv_id, _blob = _seed_cv(fake_supabase)

        res = client.post(
            f"/api/cv/{cv_id}/tailor/review",
            json={"jd_url": "https://www.linkedin.com/jobs/view/123"},
        )

        assert res.status_code == 400
        assert res.get_json()["code"] == "linkedin_manual_required"

    def test_tailor_unknown_cv_is_404(self, server_module, client, fake_supabase, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(monkeypatch, _structured_response([]))
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post(f"/api/cv/{_uuid.uuid4()}/tailor", json={"jd_text": JD_TEXT})
        assert res.status_code == 404

    def test_tailor_out_of_credits_needs_key(self, server_module, client, fake_supabase, monkeypatch):
        """No BYO key + 0 credits → 402 needs_key (Stage-4 path parity)."""
        pytest.importorskip("weasyprint")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _mock_llm(monkeypatch, _structured_response([]))
        cv_id, _blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=0)
        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT})
        assert res.status_code == 402
        assert res.get_json()["code"] == "needs_key"

    def test_tailor_malformed_llm_json_is_502(self, server_module, client, fake_supabase, monkeypatch):
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(monkeypatch, "this is not json at all")
        cv_id, _blob = _seed_cv(fake_supabase)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": JD_TEXT})
        assert res.status_code == 502
        # Nothing persisted on a malformed model response.
        assert len(_cv_docs(fake_supabase)) == 1


# ===========================================================================
#  8. Cross-tenant — user B cannot tailor / score user A's CV (404).
#  Runs against the REAL Supabase project with real user JWTs; SKIPS cleanly
#  when the provisioning env vars are absent (same pattern as Stage 2/4/5).
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
class TestTailorRLS:
    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_b_cannot_tailor_or_score_a_cv(self):
        """A creates a cv document; B must get 404 on tailor + ats-score."""
        import importlib

        from supabase import create_client

        server = importlib.import_module("server")
        server.app.config.update(TESTING=True)
        client = server.app.test_client()

        url = os.environ["SUPABASE_URL"]
        pub = os.environ["SUPABASE_PUBLISHABLE_KEY"]

        sb_a = create_client(url, pub)
        sb_a.postgrest.auth(A_TOKEN)
        created = (
            sb_a.table("documents")
            .insert(
                {
                    "type": "cv",
                    "name": "A-secret-cv",
                    "yaml_blob": _yaml.safe_dump(GROUNDED_CV, sort_keys=False),
                }
            )
            .execute()
            .data
        )
        assert created, "A should be able to create a CV"
        a_cv_id = created[0]["id"]

        try:
            # B tries to ATS-score A's CV → RLS hides it → 404 (no LLM needed).
            r = client.post(
                "/api/ats-score",
                json={"cv_id": a_cv_id, "jd_text": JD_TEXT},
                headers=self._auth(B_TOKEN),
            )
            assert r.status_code == 404, r.get_data(as_text=True)

            # B tries to tailor A's CV → 404, and A's CV is untouched.
            t = client.post(
                f"/api/cv/{a_cv_id}/tailor",
                json={"jd_text": JD_TEXT},
                headers=self._auth(B_TOKEN),
            )
            assert t.status_code == 404

            still = sb_a.table("documents").select("id").eq("id", a_cv_id).execute().data
            assert still, "RLS leak: A's CV vanished"
            # B created no tailored copy under A's ownership.
            b_docs = (
                sb_a.table("documents").select("id").eq("owner_id", B_ID).execute().data
            )
            assert not any(d for d in (b_docs or [])), "RLS leak: B created a doc"
        finally:
            sb_a.rpc(
                "delete_document_safely",
                {"p_document_id": a_cv_id, "p_document_type": "cv"},
            ).execute()
