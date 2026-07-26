"""Regression tests for the QA-verified ATS engine fixes.

Covers, one test class per finding:

  * ``engine/ats/score.py`` — a single-token skill that appears in the JD
    title ("Python" in "Senior Python Developer") must NOT be dropped by the
    title-echo filter; German modal/cue words ("Muss", "Erforderlich",
    "Kenntnisse") must NOT surface as keyword gaps; the empty-JD contract
    (ValueError for empty, ``score=None`` for thin-but-nonempty) matches the
    docstring and the callers' 400 handling.
  * ``engine/cover_letter/jd.py`` — the ``_guess_title`` fallback skips
    requirement lines, so a JD pasted starting at "Requirements:" does not
    guess "Must have Python" as the title.
  * ``engine/ats/experience.py`` — "4 years of Go experience required"
    captures skill "Go" (not "Go experience"), and the false-year-context
    regex treats a literal "$" as salary context.

All tests run OFFLINE — the deterministic scorer/parser need no LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from ats.experience import (  # noqa: E402
    _FALSE_YEAR_CONTEXT,
    build_experience_fit,
    extract_experience_requirements,
)
from ats.score import score_cv_against_jd  # noqa: E402
from cover_letter.jd import parse_jd  # noqa: E402


# ---------------------------------------------------------------------------
# 1. score.py — the title-echo filter must not drop single-token skills.
# ---------------------------------------------------------------------------
class TestTitleEchoFilterKeepsSingleTokenSkills:
    JD = (
        "Senior Python Developer at Acme\n\n"
        "Requirements:\n"
        "- Must have Python\n"
        "- Must have Docker\n"
        "- Must have Kubernetes"
    )
    CV = {"skills": ["Python", "Docker", "Kubernetes"]}

    def test_skill_appearing_in_title_is_matched(self):
        """'Python' appears in the title AND is a real required skill."""
        res = score_cv_against_jd(self.CV, self.JD)
        matched_terms = {m.term for m in res.matched}
        assert "Python" in matched_terms
        assert "Python" not in {g.term for g in res.gaps}
        # All three required skills are now scored (>= MIN_KEYWORDS_TO_SCORE).
        assert res.score is not None

    def test_title_itself_is_still_dropped(self):
        """The original intent holds: the full title is not a keyword."""
        res = score_cv_against_jd(self.CV, self.JD)
        terms = {m.term for m in res.matched} | {g.term for g in res.gaps}
        assert "Senior Python Developer At Acme" not in terms

    def test_multi_word_title_echo_is_still_dropped(self):
        """A multi-word fragment of the title ('Senior Data') is still noise."""
        jd = (
            "Title: Senior Data Engineer\n"
            "You must have Spark and Airflow. Kubernetes is nice to have."
        )
        res = score_cv_against_jd({"skills": ["Spark", "Airflow"]}, jd)
        terms = {m.term for m in res.matched} | {g.term for g in res.gaps}
        assert "Senior Data" not in terms
        assert "Senior Data Engineer" not in terms

    def test_single_token_skill_inside_company_name_is_kept(self):
        """'Docker' must survive a company named 'Docker Inc'."""
        jd = (
            "Requirements:\n"
            "- Must have Docker\n"
            "- Must have Kubernetes\n"
            "- Must have Terraform"
        )
        jd_json = parse_jd(jd, title="Platform Engineer", company="Docker Inc")
        res = score_cv_against_jd(
            {"skills": ["Docker", "Kubernetes", "Terraform"]}, jd, jd_json=jd_json
        )
        assert "Docker" in {m.term for m in res.matched}


# ---------------------------------------------------------------------------
# 2. jd.py — _guess_title must skip requirement lines in its fallback.
# ---------------------------------------------------------------------------
class TestGuessTitleSkipsRequirementLines:
    def test_jd_starting_with_requirements_yields_no_title(self):
        jd = "Requirements:\n- Must have Python\n- Must have Docker"
        assert parse_jd(jd)["title"] == ""

    def test_bullet_free_requirement_cues_are_skipped_too(self):
        jd = "Must have 3+ years of Python\nWe are a lovely company."
        assert parse_jd(jd)["title"] == ""

    def test_german_requirement_cue_is_skipped(self):
        jd = "Erforderlich: Python Kenntnisse\nWir sind ein tolles Team."
        assert parse_jd(jd)["title"] == ""

    def test_genuine_heading_is_still_guessed(self):
        jd = "Senior Backend Engineer\nRequirements:\n- Must have Python"
        assert parse_jd(jd)["title"] == "Senior Backend Engineer"

    def test_title_less_jd_does_not_compound_keyword_drop(self):
        """End-to-end: no guessed title → 'Python' is scored, not dropped."""
        jd = (
            "Requirements:\n"
            "- Must have Python\n"
            "- Must have Docker\n"
            "- Must have Kubernetes"
        )
        res = score_cv_against_jd({"skills": ["Python", "Docker", "Kubernetes"]}, jd)
        assert res.score == 100
        assert res.gaps == []


# ---------------------------------------------------------------------------
# 3. experience.py — a trailing 'experience'/'erfahrung' is not part of the
#    captured skill name.
# ---------------------------------------------------------------------------
class TestSkillCaptureStripsTrailingExperience:
    CV_GO = {
        "experience": [
            {
                "role": "Go Developer",
                "company": "X",
                "start": "2021-01",
                "end": "2025-04",
                "stack": "Go, Kubernetes",
                "bullets": ["Built Go services"],
            }
        ]
    }

    def test_skill_is_go_not_go_experience(self):
        req = extract_experience_requirements("- 4 years of Go experience required")
        assert [r["skill"] for r in req["skill_years"]] == ["Go"]

    def test_candidate_years_counted_for_stripped_skill(self):
        fit = build_experience_fit(self.CV_GO, "- 4 years of Go experience required")
        assert len(fit["skill_fit"]) == 1
        row = fit["skill_fit"][0]
        assert row["skill"] == "Go"
        assert row["candidate_years"] >= 4.0
        assert row["fit"] == "meets"

    def test_trailing_experience_stripped_before_cue_word(self):
        req = extract_experience_requirements("- 3 years of Kubernetes experience preferred")
        assert [r["skill"] for r in req["skill_years"]] == ["Kubernetes"]

    def test_german_erfahrung_phrasing_captures_clean_skill(self):
        req = extract_experience_requirements("- 4 Jahre Erfahrung mit Python erforderlich")
        assert [r["skill"] for r in req["skill_years"]] == ["Python"]

    def test_mixed_language_trailing_erfahrung_stripped(self):
        req = extract_experience_requirements("- 4 years of Kubernetes Erfahrung")
        assert [r["skill"] for r in req["skill_years"]] == ["Kubernetes"]


# ---------------------------------------------------------------------------
# 4. score.py — German modal/cue words must not surface as keyword gaps.
# ---------------------------------------------------------------------------
class TestGermanCueWordsAreNotGaps:
    JD = (
        "Requirements:\n"
        "- Muss Python können\n"
        "- Erforderlich: Docker\n"
        "- Kenntnisse in Kubernetes wünschenswert"
    )
    CV = {"skills": ["Python", "Docker", "Kubernetes"]}

    def test_german_cues_produce_no_gaps(self):
        res = score_cv_against_jd(self.CV, self.JD)
        assert res.gaps == []
        assert {m.term for m in res.matched} == {"Python", "Docker", "Kubernetes"}
        assert res.score == 100

    def test_cue_word_alone_is_not_a_keyword(self):
        res = score_cv_against_jd(self.CV, self.JD)
        terms = {m.term for m in res.matched} | {g.term for g in res.gaps}
        assert "Erforderlich" not in terms
        assert "Muss Python" not in terms
        assert "Kenntnisse" not in terms


# ---------------------------------------------------------------------------
# 5. experience.py — '$' is false-year (salary) context, not a regex bug.
# ---------------------------------------------------------------------------
class TestFalseYearContextDollar:
    def test_dollar_amount_is_salary_context(self):
        assert _FALSE_YEAR_CONTEXT.search("pay $120k")

    def test_euro_amount_is_salary_context(self):
        assert _FALSE_YEAR_CONTEXT.search("pay €120k")

    def test_plain_years_line_is_not_filtered(self):
        assert not _FALSE_YEAR_CONTEXT.search("5 years of Python required")


# ---------------------------------------------------------------------------
# 6. score.py — empty JD raises (callers map it to a 400); a thin but
#    non-empty JD returns score=None, exactly as the docstring states.
# ---------------------------------------------------------------------------
class TestEmptyVsThinJdContract:
    def test_empty_jd_raises_valueerror(self):
        with pytest.raises(ValueError):
            score_cv_against_jd({"skills": ["Python"]}, "")

    def test_whitespace_jd_raises_valueerror(self):
        with pytest.raises(ValueError):
            score_cv_against_jd({"skills": ["Python"]}, "   \n  ")

    def test_thin_but_nonempty_jd_returns_none_score(self):
        res = score_cv_against_jd({"skills": ["Python"]}, "We are hiring great people.")
        assert res.score is None
        assert "thin" in res.notes.lower()
