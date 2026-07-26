"""Regression tests for the round-3 engine hardening batch.

Covers:
  * content/yaml_guard: the editor CV path (``content.load``) now rejects
    YAML alias bombs, sharing the guard with ``importers.from_rendercv``.
  * jd/score: a "<Title> at <Company>" headline no longer leaks the company
    into the ATS keyword list as a bogus skill gap.
  * jd: German requirement lines/headers no longer guessed as the job title.
  * experience: bare-connector skill extraction ("4 years Go experience
    required") yields a skill-fit entry; contract durations ("2 years
    contract", "2 Jahre befristet") are not year requirements.
  * content: validation errors use the constant label ``cv.yaml`` instead of
    the random temp-file basename.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from ats.experience import (  # noqa: E402
    build_experience_fit,
    extract_experience_requirements,
)
from ats.score import score_cv_against_jd  # noqa: E402
from cover_letter.jd import parse_jd  # noqa: E402
from render import content, importers  # noqa: E402


VALID_CV = """\
name: QA Person
contact:
  - label: mail
experience:
  - role: Engineer
    company: Acme
    location: Berlin
    start: '2020'
    end: Present
    bullets:
      - Did a thing.
"""


def _alias_bomb(depth: int = 6) -> str:
    """A ~450-byte CV YAML whose alias chain fans out 10x per level."""
    src = "name: A\ncontact:\n  - label: x\nl0: &a0 [x,x,x,x,x,x,x,x,x,x]\n"
    for i in range(1, depth):
        refs = ",".join([f"*a{i - 1}"] * 10)
        src += f"l{i}: &a{i} [{refs}]\n"
    src += (
        "experience:\n"
        "  - role: r\n"
        "    company: c\n"
        "    start: '2020-01'\n"
        f"    bullets: *a{depth - 1}\n"
    )
    return src


# ---------------------------------------------------------------------------
# 1 — content.load rejects YAML alias bombs (shared guard with importers)
# ---------------------------------------------------------------------------
class TestContentLoadRejectsAliasBombs:
    def test_editor_path_rejects_alias_bomb(self, tmp_path):
        p = tmp_path / "upload.yaml"
        p.write_text(_alias_bomb(), encoding="utf-8")
        with pytest.raises(ValueError, match="aliases/cyclic"):
            content.load(p, strict_required=False)

    def test_strict_path_rejects_alias_bomb(self, tmp_path):
        p = tmp_path / "upload.yaml"
        p.write_text(_alias_bomb(), encoding="utf-8")
        with pytest.raises(ValueError, match="aliases/cyclic"):
            content.load(p)

    def test_importers_still_reject_alias_bomb(self):
        with pytest.raises(ValueError, match="aliases/cyclic"):
            importers.from_rendercv("cv:\n  name: A\n  x: &a [1, 2]\n  y: *a\n")

    def test_anchor_free_cv_still_loads(self, tmp_path):
        p = tmp_path / "cv.yaml"
        p.write_text(VALID_CV, encoding="utf-8")
        cv = content.load(p)
        assert cv["name"] == "QA Person"


# ---------------------------------------------------------------------------
# 2 — "<Title> at <Company>" headline: company is not a skill gap
# ---------------------------------------------------------------------------
class TestCompanyHeadlineNotASkillGap:
    JD = (
        "Senior Python Developer at Acme\n\n"
        "Requirements:\n"
        "- Must have Python\n"
        "- Must have Docker\n"
        "- Must have Kubernetes"
    )
    CV = {"skills": ["Python", "Docker", "Kubernetes"]}

    def test_company_guessed_from_at_headline(self):
        assert parse_jd(self.JD)["company"] == "Acme"

    def test_company_not_scored_as_gap(self):
        # Previously scored 90 with gaps == ["Acme"]: _guess_company returned
        # "" for this title shape and the company token leaked into the
        # keyword list.
        res = score_cv_against_jd(self.CV, self.JD)
        assert res.score == 100
        assert res.gaps == []

    def test_prose_at_does_not_invent_company(self):
        jd = "Requirements:\n- Must have Python\n- Must have Docker\n- Must have Go"
        assert parse_jd(jd)["company"] == ""

    def test_explicit_company_label_still_wins(self):
        jd = "Company: Globex\nSenior Python Developer at Acme"
        assert parse_jd(jd)["company"] == "Globex"


# ---------------------------------------------------------------------------
# 3 — German requirement lines/headers are not job titles
# ---------------------------------------------------------------------------
class TestGermanRequirementLinesNotTitles:
    def test_kenntnisse_line_after_header_is_not_title(self):
        jd = "Anforderungen:\n- Muss Python können\n- Docker Kenntnisse"
        assert parse_jd(jd)["title"] == ""

    def test_requirement_header_stops_title_fallback(self):
        jd = "Requirements:\n- Docker\n- Kubernetes"
        assert parse_jd(jd)["title"] == ""

    def test_erfahrung_line_is_not_title(self):
        jd = "Profil:\n- Kubernetes Erfahrung"
        assert parse_jd(jd)["title"] == ""

    def test_genuine_title_still_guessed(self):
        jd = "Senior Backend Engineer\nWir sind ein tolles Team."
        assert parse_jd(jd)["title"] == "Senior Backend Engineer"

    def test_genuine_title_before_requirements_still_guessed(self):
        jd = "Senior Backend Engineer\nRequirements:\n- Must have Python"
        assert parse_jd(jd)["title"] == "Senior Backend Engineer"


# ---------------------------------------------------------------------------
# 4 — bare-connector skill extraction ("4 years Go experience required")
# ---------------------------------------------------------------------------
class TestBareConnectorSkillExtraction:
    CV_GO = {
        "experience": [
            {
                "role": "Go Developer",
                "company": "X",
                "start": "2021-01",
                "end": "2025-01",
                "stack": "Go, Kubernetes",
                "bullets": ["Built Go services"],
            }
        ]
    }

    def test_bare_skill_extracted(self):
        payload = extract_experience_requirements("4 years Go experience required")
        assert payload["skill_years"][0]["skill"] == "Go"
        assert payload["skill_years"][0]["years"] == 4.0

    def test_bare_skill_fit_entry_present(self):
        fit = build_experience_fit(
            self.CV_GO, "- 4 years Go experience required", as_of="2025-01"
        )
        assert fit["skill_fit"][0]["skill"] == "Go"
        assert fit["skill_fit"][0]["fit"] != "below"

    def test_bare_experience_alone_invents_no_skill(self):
        payload = extract_experience_requirements("4 years experience required")
        assert payload["skill_years"] == []
        assert payload["total_years_min"] == 4.0

    def test_age_mention_invents_no_skill(self):
        payload = extract_experience_requirements("Must be 18 years old")
        # No skill may be invented from the bare word after "years". (The
        # 18.0 total-years read is pre-existing behaviour, out of scope here.)
        assert payload["skill_years"] == []


# ---------------------------------------------------------------------------
# 5 — contract duration is not an experience requirement
# ---------------------------------------------------------------------------
class TestContractDurationNotExperienceRequirement:
    def test_english_contract_duration_ignored(self):
        payload = extract_experience_requirements(
            "$150k salary. 2 years contract position."
        )
        assert payload["total_years_min"] is None
        assert payload["skill_years"] == []

    def test_german_fixed_term_ignored(self):
        payload = extract_experience_requirements("Vertrag: 2 Jahre befristet")
        assert payload["total_years_min"] is None

    def test_renewable_contract_ignored(self):
        payload = extract_experience_requirements("2 years renewable contract")
        assert payload["total_years_min"] is None

    def test_genuine_requirement_on_salary_line_survives(self):
        payload = extract_experience_requirements(
            "Salary EUR 120k. Compensation USD 160k. Required: 4 years of data engineering."
        )
        assert payload["total_years_min"] == 4.0


# ---------------------------------------------------------------------------
# 6 — validation errors use the constant label, not the tmp basename
# ---------------------------------------------------------------------------
class TestValidationErrorConstantLabel:
    def test_random_tmp_basename_not_leaked(self, tmp_path):
        p = tmp_path / "tmpj68jq9y7.yaml"
        p.write_text("contact:\n  - label: x\n", encoding="utf-8")
        with pytest.raises(ValueError) as excinfo:
            content.load(p)
        msg = str(excinfo.value)
        assert "tmpj68jq9y7" not in msg
        assert str(tmp_path) not in msg
        assert "cv.yaml: missing required field 'name'" in msg

    def test_top_level_error_uses_constant_label(self, tmp_path):
        p = tmp_path / "whatever-name.yaml"
        p.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="cv.yaml: top-level YAML"):
            content.load(p)
