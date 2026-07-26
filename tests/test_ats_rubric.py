"""Stage 4 tests — the LLM-driven ATS rubric (engine/ats/rubric.py).

Every test runs OFFLINE: the LLM is a ``unittest.mock.Mock`` or the dev-only
``StubLLMClient``, never a real provider. The suite asserts the rubric's
contract:

  * **JSON parsing** is robust to fences / preamble and fails cleanly (typed
    errors) on empty / malformed / non-object replies.
  * **Validation** defaults missing fields, clamps scores to [0, 100], caps
    evidence, normalizes bonus/deductions to {0, 5}, and RECOMPUTES the final
    score from the documented category weights (never trusting the model's
    arithmetic).
  * **Fallback**: a timeout / 429 / auth error / empty / malformed reply each
    returns ``(None, typed_error)`` so the caller degrades to the deterministic
    score — the rubric is never load-bearing.
  * **Fairness + cache**: the system prompt is static (byte-identical across
    calls) and carries the global (G1) + fairness (G7-G9) guardrails.
  * **Anti-fabrication**: a suggestion that nudges the user to add a skill the
    CV doesn't support is dropped; honest reframe suggestions survive.
  * **Demo mode**: the StubLLMClient serves a parseable, grounded rubric with
    no key and no network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from ats.prompts import (  # noqa: E402
    ATS_RUBRIC_MAX_TOKENS,
    CATEGORY_KEYS,
    CATEGORY_WEIGHTS,
    build_ats_rubric_system_prompt,
    build_ats_rubric_user_message,
)
from ats.rubric import (  # noqa: E402
    AtsRubricError,
    CategoryScore,
    LLMEmptyError,
    LLMJSONDecodeError,
    LLMProviderMismatchError,
    LLMRateLimitError,
    LLMTimeoutError,
    RubricResult,
    _parse_ats_rubric_json,
    _recompute_score,
    _validate_rubric_result,
    ground_rubric_suggestions,
    score_cv_with_rubric,
)
from ats.score import score_cv_against_jd  # noqa: E402
from cover_letter.jd import parse_jd  # noqa: E402
from llm.client import StubLLMClient  # noqa: E402


# A canonical, grounded CV reused as the SOLE source of evidence everywhere.
GROUNDED_CV = {
    "name": "Tim Loehr",
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
        }
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


def _full_rubric_json(**overrides) -> str:
    """A complete, well-formed rubric JSON string (override any top-level key)."""
    payload = {
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
    payload.update(overrides)
    return json.dumps(payload)


# ===========================================================================
#  1. JSON parsing.
# ===========================================================================
class TestRubricJSONParsing:
    def test_valid_object_parses(self):
        parsed = _parse_ats_rubric_json(_full_rubric_json())
        assert parsed["score"] == 75
        assert "production" in parsed["categories"]

    def test_empty_or_whitespace_raises_empty(self):
        with pytest.raises(LLMEmptyError):
            _parse_ats_rubric_json("")
        with pytest.raises(LLMEmptyError):
            _parse_ats_rubric_json("   \n\t  ")

    def test_malformed_json_raises_decode(self):
        with pytest.raises(LLMJSONDecodeError):
            _parse_ats_rubric_json("{not valid json")
        with pytest.raises(LLMJSONDecodeError):
            _parse_ats_rubric_json("no braces here at all")

    def test_non_object_top_level_raises_decode(self):
        # A JSON array is valid JSON but not the object we require.
        with pytest.raises(LLMJSONDecodeError):
            _parse_ats_rubric_json("[1, 2, 3]")

    def test_markdown_fence_and_preamble_are_stripped(self):
        raw = "Here is the result:\n```json\n" + _full_rubric_json() + "\n```\nDone."
        parsed = _parse_ats_rubric_json(raw)
        assert parsed["score"] == 75

    def test_typed_errors_are_ats_rubric_errors(self):
        # All parse errors are recoverable AtsRubricError subclasses (the caller
        # only needs to catch the base type to fall back to deterministic).
        assert issubclass(LLMEmptyError, AtsRubricError)
        assert issubclass(LLMJSONDecodeError, AtsRubricError)


# ===========================================================================
#  2. Validation / coercion.
# ===========================================================================
class TestRubricValidation:
    def test_well_formed_result_validates(self):
        result = _validate_rubric_result(json.loads(_full_rubric_json()))
        assert isinstance(result, RubricResult)
        assert set(result.categories) == set(CATEGORY_KEYS)
        assert result.categories["production"].score == 85
        assert result.categories["production"].evidence == ["Data Engineer at Meta"]

    def test_final_score_is_recomputed_from_weights(self):
        # The model's own "score" is IGNORED — the headline must equal the
        # weighted category sum so the breakdown and the number agree.
        result = _validate_rubric_result(json.loads(_full_rubric_json(score=999)))
        expected = round(
            40 * 0.15 + 50 * 0.15 + 85 * 0.50 + 75 * 0.20
        )  # = 73
        assert result.score == expected
        assert result.score != 999

    def test_score_recompute_helper_clamps(self):
        cats = {k: CategoryScore(category=k, score=100) for k in CATEGORY_KEYS}
        # All-100 + a bonus must clamp at 100, never exceed.
        assert _recompute_score(cats, bonus=5, deductions=0) == 100
        # All-0 minus a deduction must clamp at 0, never go negative.
        zeros = {k: CategoryScore(category=k, score=0) for k in CATEGORY_KEYS}
        assert _recompute_score(zeros, bonus=0, deductions=5) == 0

    def test_missing_fields_default_safely(self):
        result = _validate_rubric_result({})
        # Every category present with the neutral 50 default + empty evidence.
        assert set(result.categories) == set(CATEGORY_KEYS)
        assert all(c.score == 50 for c in result.categories.values())
        assert all(c.evidence == [] for c in result.categories.values())
        assert result.strengths == []
        assert result.gaps == []
        assert result.suggestions == []
        # 50 across all categories recomputes to 50.
        assert result.score == 50

    def test_category_score_clamped_to_0_100(self):
        parsed = json.loads(_full_rubric_json())
        parsed["categories"]["production"]["score"] = 150
        parsed["categories"]["open_source"]["score"] = -10
        result = _validate_rubric_result(parsed)
        assert result.categories["production"].score == 100
        assert result.categories["open_source"].score == 0

    def test_string_scores_are_coerced(self):
        parsed = json.loads(_full_rubric_json())
        parsed["categories"]["production"]["score"] = "85"
        result = _validate_rubric_result(parsed)
        assert result.categories["production"].score == 85

    def test_evidence_and_lists_capped_at_five(self):
        parsed = json.loads(_full_rubric_json())
        parsed["categories"]["production"]["evidence"] = [f"q{i}" for i in range(9)]
        parsed["strengths"] = [f"s{i}" for i in range(9)]
        result = _validate_rubric_result(parsed)
        assert len(result.categories["production"].evidence) == 5
        assert len(result.strengths) == 5

    def test_bonus_and_deductions_normalized_to_0_or_5(self):
        # Arbitrary magnitudes collapse to {0, 5}; a negative deduction → its
        # magnitude is used (never a negative number).
        r1 = _validate_rubric_result(json.loads(_full_rubric_json(bonus=3, deductions=-7)))
        assert r1.bonus == 0          # 3 < 5 → 0
        assert r1.deductions == 5     # |−7| ≥ 5 → 5
        r2 = _validate_rubric_result(json.loads(_full_rubric_json(bonus=5, deductions=0)))
        assert r2.bonus == 5
        assert r2.deductions == 0

    def test_to_json_is_serializable_and_shaped(self):
        result = _validate_rubric_result(json.loads(_full_rubric_json()))
        blob = result.to_json()
        # Round-trips through JSON and keeps the documented top-level keys.
        round_tripped = json.loads(json.dumps(blob))
        for key in ("score", "categories", "strengths", "gaps", "suggestions", "bonus", "deductions"):
            assert key in round_tripped
        assert set(round_tripped["categories"]) == set(CATEGORY_KEYS)
        # Category sub-objects expose score + evidence for the UI.
        assert "score" in round_tripped["categories"]["production"]
        assert "evidence" in round_tripped["categories"]["production"]


# ===========================================================================
#  3. The public scorer with a MOCKED LLM — happy path + every fallback.
# ===========================================================================
class TestRubricWithMockedLLM:
    def _client(self, *, returns=None, raises=None) -> Mock:
        c = Mock()
        if raises is not None:
            c.complete = Mock(side_effect=raises)
        else:
            c.complete = Mock(return_value=returns)
        return c

    def test_happy_path_returns_result_no_error(self):
        client = self._client(returns=_full_rubric_json())
        jd_json = parse_jd(JD_TEXT)
        det = score_cv_against_jd(GROUNDED_CV, JD_TEXT, jd_json=jd_json)

        result, error = score_cv_with_rubric(
            cv=GROUNDED_CV,
            jd_text=JD_TEXT,
            jd_json=jd_json,
            deterministic_ats=det,
            llm_client=client,
        )

        assert error is None
        assert isinstance(result, RubricResult)
        assert result.categories["production"].score == 85
        assert result.score == round(40 * 0.15 + 50 * 0.15 + 85 * 0.50 + 75 * 0.20)
        # The call used the cache-friendly path + the rubric token budget.
        _args, kwargs = client.complete.call_args
        assert kwargs["cache_system"] is True
        assert kwargs["max_tokens"] == ATS_RUBRIC_MAX_TOKENS

    def test_computes_deterministic_signal_when_not_supplied(self):
        # Omitting both jd_json and deterministic_ats must NOT crash — the
        # scorer parses + computes them internally and still succeeds.
        client = self._client(returns=_full_rubric_json())
        result, error = score_cv_with_rubric(
            cv=GROUNDED_CV,
            jd_text=JD_TEXT,
            llm_client=client,
        )
        assert error is None
        assert isinstance(result, RubricResult)

    def test_timeout_falls_back(self):
        client = self._client(raises=TimeoutError("call timed out"))
        result, error = score_cv_with_rubric(
            cv=GROUNDED_CV, jd_text=JD_TEXT, llm_client=client
        )
        assert result is None
        assert isinstance(error, LLMTimeoutError)
        assert error.http_status == 502
        assert "deterministic" in error.reason.lower()

    def test_rate_limit_falls_back(self):
        client = self._client(raises=RuntimeError("API error 429 Too Many Requests"))
        result, error = score_cv_with_rubric(
            cv=GROUNDED_CV, jd_text=JD_TEXT, llm_client=client
        )
        assert result is None
        assert isinstance(error, LLMRateLimitError)
        assert error.http_status == 429

    def test_auth_error_falls_back_without_leaking(self):
        client = self._client(raises=RuntimeError("401 Unauthorized: invalid x-api-key"))
        result, error = score_cv_with_rubric(
            cv=GROUNDED_CV, jd_text=JD_TEXT, llm_client=client
        )
        assert result is None
        assert isinstance(error, LLMProviderMismatchError)
        # Global G4: the user-facing reason must not echo key/auth specifics.
        assert "api-key" not in error.reason.lower()
        assert "401" not in error.reason

    def test_generic_exception_falls_back(self):
        client = self._client(raises=RuntimeError("connection reset by peer"))
        result, error = score_cv_with_rubric(
            cv=GROUNDED_CV, jd_text=JD_TEXT, llm_client=client
        )
        assert result is None
        assert isinstance(error, AtsRubricError)

    def test_empty_reply_falls_back(self):
        client = self._client(returns="")
        result, error = score_cv_with_rubric(
            cv=GROUNDED_CV, jd_text=JD_TEXT, llm_client=client
        )
        assert result is None
        assert isinstance(error, LLMEmptyError)

    def test_malformed_json_falls_back(self):
        client = self._client(returns="totally not json")
        result, error = score_cv_with_rubric(
            cv=GROUNDED_CV, jd_text=JD_TEXT, llm_client=client
        )
        assert result is None
        assert isinstance(error, LLMJSONDecodeError)

    def test_empty_jd_falls_back_with_400(self):
        client = self._client(returns=_full_rubric_json())
        result, error = score_cv_with_rubric(
            cv=GROUNDED_CV, jd_text="   ", llm_client=client
        )
        assert result is None
        assert isinstance(error, AtsRubricError)
        assert error.http_status == 400
        # The model was never called for an unscoreable JD.
        client.complete.assert_not_called()


# ===========================================================================
#  4. System prompt is static (cache-friendly) + carries the guardrails.
# ===========================================================================
class TestSystemPromptStaticAndFair:
    def test_system_prompt_is_byte_identical_across_calls(self):
        assert build_ats_rubric_system_prompt() == build_ats_rubric_system_prompt()

    def test_system_prompt_carries_global_and_fairness_guardrails(self):
        prompt = build_ats_rubric_system_prompt()
        # Global anti-fabrication (G1) reused from the cover-letter system.
        assert "G1." in prompt
        assert "NEVER invent facts" in prompt
        # Fairness block (G7-G9): ignore name/gender/institution; ground all.
        assert "G7." in prompt
        assert "G8." in prompt
        assert "G9." in prompt
        # Fairness text explicitly names the protected attributes to ignore.
        low = prompt.lower()
        assert "name" in low and "gender" in low
        assert "institution" in low or "prestige" in low
        assert "gpa" in low
        assert "location" in low
        # The four remapped categories are defined in the task.
        for key in CATEGORY_KEYS:
            assert key in prompt

    def test_user_message_includes_cv_jd_and_signal(self):
        jd_json = parse_jd(JD_TEXT)
        signal = {"score": 88, "tier1_coverage": 100, "extracted_keyword_count": 4}
        msg = build_ats_rubric_user_message(GROUNDED_CV, jd_json, signal)
        assert "cv_json" in msg
        assert "job_description" in msg
        assert "keyword_signal" in msg
        assert "88" in msg            # deterministic signal is embedded
        assert "Tim Loehr" in msg     # the CV is serialized in

    def test_category_weights_sum_to_one(self):
        # The weighted-average formula only yields a 0-100 score if the weights
        # form a convex combination — guard against an accidental edit.
        assert abs(sum(CATEGORY_WEIGHTS.values()) - 1.0) < 1e-9


# ===========================================================================
#  5. Anti-fabrication grounding of suggestions (Global G9).
# ===========================================================================
class TestSuggestionGrounding:
    def _result_with(self, suggestions: list[str]) -> RubricResult:
        parsed = json.loads(_full_rubric_json(suggestions=suggestions))
        return _validate_rubric_result(parsed)

    def test_grounded_suggestion_is_kept(self):
        # "Airflow" + "Meta" are both in the CV → the reframe survives.
        result = self._result_with(["Surface the Airflow work from your Meta role"])
        kept, dropped = ground_rubric_suggestions(GROUNDED_CV, result)
        assert "Surface the Airflow work from your Meta role" in kept.suggestions
        assert dropped == []

    def test_ungrounded_suggestion_is_dropped(self):
        # "Kubernetes" + "Terraform" appear NOWHERE in the CV → a "add these"
        # nudge would invite fabrication and must be filtered out.
        result = self._result_with(["Add Kubernetes and Terraform deployment experience"])
        kept, dropped = ground_rubric_suggestions(GROUNDED_CV, result)
        assert kept.suggestions == []
        assert len(dropped) == 1

    def test_mixed_suggestions_split_correctly(self):
        result = self._result_with(
            [
                "Surface the Airflow work from your Meta role",   # grounded
                "Add Kubernetes orchestration certifications",     # ungrounded
            ]
        )
        kept, dropped = ground_rubric_suggestions(GROUNDED_CV, result)
        assert len(kept.suggestions) == 1
        assert len(dropped) == 1
        assert "Airflow" in kept.suggestions[0]

    def test_grounding_does_not_mutate_input_or_other_fields(self):
        result = self._result_with(["Add Kubernetes experience"])
        before_score = result.score
        before_strengths = list(result.strengths)
        kept, _dropped = ground_rubric_suggestions(GROUNDED_CV, result)
        # Original is untouched (frozen dataclass) and the filtered copy keeps
        # its score, categories, strengths, and gaps intact.
        assert result.suggestions == ["Add Kubernetes experience"]
        assert kept.score == before_score
        assert kept.strengths == before_strengths
        assert set(kept.categories) == set(CATEGORY_KEYS)

    def test_public_scorer_drops_ungrounded_suggestions(self):
        # End-to-end through score_cv_with_rubric: an ungrounded suggestion from
        # the model never reaches the returned result.
        client = Mock()
        client.complete = Mock(
            return_value=_full_rubric_json(
                suggestions=["Add Kubernetes and AWS certifications you do not have"]
            )
        )
        result, error = score_cv_with_rubric(
            cv=GROUNDED_CV, jd_text=JD_TEXT, llm_client=client
        )
        assert error is None
        assert result.suggestions == []


# ===========================================================================
#  6. Demo-mode StubLLMClient serves a parseable, grounded rubric (no key).
# ===========================================================================
class TestDemoModeStub:
    def test_stub_returns_parseable_grounded_rubric(self):
        stub = StubLLMClient()
        result, error = score_cv_with_rubric(
            cv=GROUNDED_CV, jd_text=JD_TEXT, llm_client=stub
        )
        assert error is None
        assert isinstance(result, RubricResult)
        assert 0 <= result.score <= 100
        assert set(result.categories) == set(CATEGORY_KEYS)
        # Production evidence is grounded in the CV's most-recent role/company.
        evidence_blob = " ".join(result.categories["production"].evidence)
        assert "Meta" in evidence_blob

    def test_stub_score_in_bounds_for_thin_cv(self):
        thin_cv = {"name": "Nobody"}
        stub = StubLLMClient()
        result, error = score_cv_with_rubric(
            cv=thin_cv, jd_text=JD_TEXT, llm_client=stub
        )
        assert error is None
        assert 0 <= result.score <= 100
        # A thin CV yields no production evidence (no fabrication).
        assert result.categories["production"].evidence == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
