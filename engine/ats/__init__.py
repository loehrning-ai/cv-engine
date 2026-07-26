"""ATS scoring — deterministic keyword coverage + an LLM fit rubric.

Two complementary scorers:

  * **Deterministic** (:func:`score_cv_against_jd`, Stage 6): offline, LLM-free,
    fully unit-testable. Returns a 0-100 weighted keyword-coverage score plus a
    gap list of JD requirements the CV shows no evidence for. Stable and
    explainable — the same CV + JD always yields the same number. It is the
    no-key fallback and an input SIGNAL to the rubric below.

  * **LLM rubric** (:func:`score_cv_with_rubric`, Stage 4): asks "how well does
    THIS candidate fit THIS job?", scoring four job-relative categories
    (open-source, self-projects, production, technical-skills) with grounded
    evidence, strengths, gaps, and reframe-only suggestions. Every failure is a
    typed :class:`AtsRubricError` and the function returns ``(None, error)`` so
    the caller degrades to the deterministic score — the rubric is never
    load-bearing, and it never mutates the CV (the 1-page gate is untouched).
"""
from __future__ import annotations

from .rubric import (  # noqa: F401
    AtsRubricError,
    CategoryScore,
    LLMEmptyError,
    LLMJSONDecodeError,
    LLMProviderMismatchError,
    LLMRateLimitError,
    LLMTimeoutError,
    RubricResult,
    build_ats_rubric_system_prompt,
    build_ats_rubric_user_message,
    ground_rubric_suggestions,
    score_cv_with_rubric,
)
from .score import (  # noqa: F401
    AtsResult,
    Gap,
    KeywordMatch,
    TIER1_TARGET_PCT,
    WEIGHTS,
    score_cv_against_jd,
)

__all__ = [
    # Deterministic scorer (fallback + signal).
    "AtsResult",
    "Gap",
    "KeywordMatch",
    "TIER1_TARGET_PCT",
    "WEIGHTS",
    "score_cv_against_jd",
    # LLM rubric.
    "AtsRubricError",
    "CategoryScore",
    "LLMEmptyError",
    "LLMJSONDecodeError",
    "LLMProviderMismatchError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "RubricResult",
    "build_ats_rubric_system_prompt",
    "build_ats_rubric_user_message",
    "ground_rubric_suggestions",
    "score_cv_with_rubric",
]
