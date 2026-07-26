"""Cover-letter generation, guards, and rendering (Stage 5).

This package is the product's core AI value: turn a candidate's structured
CV plus a pasted job description into an application-ready, *grounded* cover
letter — one that never fabricates a fact about the candidate.

It is deliberately kept SEPARATE from the CV engine:

  * ``engine/build.py`` and its 1-page-or-fail invariant are untouched.
  * The cover-letter renderer (:mod:`engine.cover_letter.renderer`) is a
    NEW, multi-page-allowed letter layout. There is NO page-count gate here.
  * The validator is a server-side WORD CAP (~400 words, hard ceiling 450),
    not a page count.

The public surface:

  * :func:`prompts.build_cover_letter_messages` — the grounded system prompt
    + user template, assembled from the CV JSON and the parsed JD JSON.
  * :func:`jd.parse_jd` — parse free JD text into a keyed JSON structure.
  * :func:`guards.run_guards` — the POST-GEN guards: cliché scan,
    anti-fabrication proper-noun/number cross-reference, and the word cap.
  * :func:`renderer.render_cover_letter` — letter HTML + PDF bytes.
"""
from __future__ import annotations

from .guards import (
    ClicheFinding,
    GroundingError,
    GuardResult,
    WordCapError,
    cliche_scan,
    cross_reference_facts,
    enforce_word_cap,
    run_guards,
    word_count,
)
from .jd import parse_jd
from .prompts import (
    COVER_LETTER_MAX_TOKENS,
    COVER_LETTER_SYSTEM_PROMPT,
    build_cover_letter_messages,
)
from .renderer import render_cover_letter_html, render_cover_letter_pdf

__all__ = [
    "ClicheFinding",
    "GroundingError",
    "GuardResult",
    "WordCapError",
    "cliche_scan",
    "cross_reference_facts",
    "enforce_word_cap",
    "run_guards",
    "word_count",
    "parse_jd",
    "COVER_LETTER_MAX_TOKENS",
    "COVER_LETTER_SYSTEM_PROMPT",
    "build_cover_letter_messages",
    "render_cover_letter_html",
    "render_cover_letter_pdf",
]
