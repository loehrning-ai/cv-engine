"""Grounded CV-to-job tailoring (Stage 6).

Given a candidate's CV and a target job description, the tailoring flow:

  1. duplicates the CV (the ORIGINAL is never touched — duplicate-then-tailor);
  2. runs ONE structured LLM call (:mod:`engine.tailor.prompts`) that returns
     typed, machine-applyable suggestions backed by the candidate's GENUINE
     bullets — it never rewrites the whole CV and never invents facts;
  3. applies those suggestions DETERMINISTICALLY (:mod:`engine.tailor.apply`)
     onto a copy of the model;
  4. is followed (in the service layer) by the two deterministic guards: the
     anti-fabrication cross-reference and the engine's 1-page gate.

The tailoring LLM call defaults to a cheaper model tier
(:data:`CV_TAILOR_MODEL`) for cost — a RUNTIME model choice on the call, not a
change to the extraction engine's default model.
"""
from __future__ import annotations

from .apply import apply_suggestions
from .prompts import (
    CV_TAILOR_MAX_TOKENS,
    CV_TAILOR_SYSTEM_PROMPT,
    build_tailor_messages,
)

# The cheaper default tier for tailoring (cost). Overridable per call; this is
# a runtime model choice, NOT a change to ai_extract's DEFAULT_MODEL. Kept
# byte-identical to the model llm_gateway already uses for its cheap probe.
CV_TAILOR_MODEL = "claude-haiku-4-5"

__all__ = [
    "apply_suggestions",
    "build_tailor_messages",
    "CV_TAILOR_MAX_TOKENS",
    "CV_TAILOR_SYSTEM_PROMPT",
    "CV_TAILOR_MODEL",
]
