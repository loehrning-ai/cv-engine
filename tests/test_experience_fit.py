from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from ats.experience import (  # noqa: E402
    build_experience_fit,
    candidate_experience,
    extract_experience_requirements,
)


def test_candidate_experience_merges_overlapping_months():
    cv = {
        "experience": [
            {"company": "A", "role": "Engineer", "start": "01/2020", "end": "12/2021"},
            {"company": "B", "role": "Engineer", "start": "07/2021", "end": "06/2022"},
            {"company": "C", "role": "Engineer", "start": "07/2022", "end": "Present"},
        ]
    }
    result = candidate_experience(cv, as_of="2024-12")
    assert result.total_months == 60
    assert result.total_years == 5.0
    assert len(result.merged_intervals) == 1


def test_candidate_experience_flags_year_only_and_invalid_roles():
    cv = {
        "experience": [
            {"company": "A", "role": "Engineer", "start": "2021", "end": "2023"},
            {"company": "B", "role": "Engineer", "start": "", "end": "2022"},
            {"company": "C", "role": "Engineer", "start": "2025-02", "end": "2024-01"},
        ]
    }
    result = candidate_experience(cv, as_of="2024-12")
    assert result.total_months == 36
    assert result.uncertain is True
    assert any("year-only date" in note for interval in result.intervals for note in interval.notes)
    assert any("skipped" in note for note in result.notes)


def test_skill_specific_years_count_only_dated_role_evidence():
    cv = {
        "experience": [
            {"company": "A", "role": "Data Engineer", "start": "01/2020", "end": "12/2021", "stack": "Python, Spark"},
            {"company": "B", "role": "Backend Engineer", "start": "01/2022", "end": "12/2023", "stack": "Go, Postgres"},
        ],
        "skills": [{"label": "Data", "items": "Python"}],
    }
    result = candidate_experience(cv, as_of="2024-12", skill="Python")
    assert result.total_months == 24
    assert result.total_years == 2.0


def test_required_years_extracts_total_skill_and_seniority_without_confidence():
    payload = extract_experience_requirements(
        "Senior Data Engineer\nMust have 5+ years required. Salary CHF 150k.\n3+ years of Python preferred."
    )
    assert payload["total_years_min"] == 5.0
    assert payload["seniority_label"] == "senior"
    assert payload["skill_years"][0]["years"] == 3.0
    assert payload["skill_years"][0]["skill"].lower().startswith("python")
    assert all("150" not in item for item in payload["evidence"])


def test_experience_fit_payload_is_additive_and_explainable():
    cv = {"experience": [{"company": "A", "role": "Data Engineer", "start": "01/2020", "end": "12/2023"}]}
    fit = build_experience_fit(cv, "Must have at least 5 years of experience.", as_of="2024-12")
    assert fit["required_years"] == 5.0
    assert fit["candidate_years"] == 4.0
    assert fit["missing_years"] == 1.0
    assert fit["fit"] == "near"
    assert fit["evidence"]["requirements"]
    assert fit["evidence"]["candidate"]
    contribution = fit["ranking_contribution"]
    assert contribution["component"] == "experience_years"
    assert contribution["bucket"] == "near"
    assert contribution["score"] == 75.0
    assert contribution["weighted_score"] == 13.5
    assert "candidate years" in contribution["reason"]


def test_skill_specific_fit_uses_matching_role_evidence_not_total_years():
    cv = {
        "experience": [
            {"company": "A", "role": "Data Engineer", "start": "01/2020", "end": "12/2023", "stack": "SQL"},
            {"company": "B", "role": "ML Engineer", "start": "01/2023", "end": "12/2023", "stack": "Python"},
        ]
    }
    fit = build_experience_fit(cv, "Must have 3 years of experience with Python.", as_of="2024-12")

    assert fit["candidate_years"] == 4.0
    assert fit["skill_fit"][0]["skill"] == "Python"
    assert fit["skill_fit"][0]["candidate_years"] == 1.0
    assert fit["skill_fit"][0]["missing_years"] == 2.0
    assert fit["skill_fit"][0]["fit"] == "below"
    contribution = fit["ranking_contribution"]
    assert contribution["bucket"] == "below"
    assert contribution["score"] == 45.0
    assert contribution["weighted_score"] == 8.1
    assert "skill-specific" in contribution["reason"]


def test_total_years_contribution_meets_requirement():
    cv = {"experience": [{"company": "A", "role": "Engineer", "start": "01/2018", "end": "12/2023"}]}
    fit = build_experience_fit(cv, "Must have 5 years of experience.", as_of="2024-12")

    assert fit["fit"] == "meets"
    assert fit["ranking_contribution"]["score"] == 100.0
    assert fit["ranking_contribution"]["weighted_score"] == 18.0


def test_uncertain_and_undated_roles_dampen_ranking_contribution():
    cv = {
        "experience": [
            {"company": "A", "role": "Engineer", "start": "2020", "end": "2024"},
            {"company": "B", "role": "Engineer", "start": "", "end": "2023"},
        ]
    }
    fit = build_experience_fit(cv, "Must have 4 years of experience.", as_of="2024-12")

    assert fit["uncertain"] is True
    assert fit["ranking_contribution"]["bucket"] == "meets"
    assert fit["ranking_contribution"]["score"] == 85.0
    assert "uncertain" in fit["ranking_contribution"]["reason"].lower()


def test_insufficient_years_reduce_ranking_contribution():
    cv = {"experience": [{"company": "A", "role": "Engineer", "start": "01/2022", "end": "12/2023"}]}
    fit = build_experience_fit(cv, "Required: 6 years of experience.", as_of="2024-12")

    assert fit["fit"] == "below"
    assert fit["missing_years"] == 4.0
    assert fit["ranking_contribution"]["score"] == 0.0
    assert fit["ranking_contribution"]["weighted_score"] == 0.0


def test_salary_and_currency_text_near_years_is_not_treated_as_requirement():
    payload = extract_experience_requirements(
        "Salary EUR 120k. Compensation USD 160k. Required: 4 years of data engineering."
    )

    assert payload["total_years_min"] == 4.0
    assert all("120" not in item and "160" not in item for item in payload["evidence"])
