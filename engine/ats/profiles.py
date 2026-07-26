"""Transparent ATS score systems.

These are simulated lenses over CV Copilot's deterministic/rubric scoring. They
are not vendor integrations and must never imply certification by a real ATS
provider.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


DEFAULT_PROFILE = "semantic_fit"
DISCLAIMER = "Transparent CV Copilot simulation; not certified by or backed by an ATS vendor."


ATS_PROFILES: dict[str, dict[str, Any]] = {
    "keyword_screen": {
        "id": "keyword_screen",
        "label": "Keyword screen",
        "purpose": "Checks whether the CV contains the explicit role terms and must-have keywords from the job description.",
        "best_for": "Fast first-pass screening and keyword coverage cleanup before applying.",
        "method": "deterministic_keyword_coverage",
        "description": "Deterministic keyword and must-have coverage simulation.",
        "explanation": "Scores extracted job keywords against the saved CV. No AI rubric is required.",
        "rubric_preferred": False,
        "disclaimer": DISCLAIMER,
        "simulation": True,
        "vendor_backed": False,
    },
    "strict_requirements": {
        "id": "strict_requirements",
        "label": "Strict requirements",
        "purpose": "Promotes missing required terms and hard requirements to the headline score.",
        "best_for": "Roles with explicit must-have requirements where gaps should block submission.",
        "method": "must_have_coverage",
        "description": "Conservative simulation that uses must-have coverage as the headline score when required terms exist.",
        "explanation": "If the job has required terms, the score follows required-term coverage instead of broader semantic fit.",
        "rubric_preferred": False,
        "disclaimer": DISCLAIMER,
        "simulation": True,
        "vendor_backed": False,
    },
    "semantic_fit": {
        "id": "semantic_fit",
        "label": "Semantic fit",
        "purpose": "Balances keyword evidence with role-fit rubric categories when an AI key is available.",
        "best_for": "Final review when the CV should read as a strong match, not only a keyword match.",
        "method": "rubric_preferred",
        "description": "Rubric-preferred job-fit simulation; deterministic keyword scoring is used when no AI key is available.",
        "explanation": "Uses the CV Copilot rubric for evidence quality and role fit, then falls back to deterministic scoring when needed.",
        "rubric_preferred": True,
        "disclaimer": DISCLAIMER,
        "simulation": True,
        "vendor_backed": False,
    },
}

LEGACY_PROFILE_MAP = {
    "balanced": "semantic_fit",
    "recruiter": "semantic_fit",
    "semantic_recruiter": "semantic_fit",
    "keyword": "keyword_screen",
    "strict": "strict_requirements",
    "enterprise_parser": "strict_requirements",
}


def available_profile_ids() -> list[str]:
    return list(ATS_PROFILES.keys())


def public_profile(profile_id: str) -> dict[str, Any]:
    return dict(ATS_PROFILES[normalize_profile(profile_id)])


def public_profiles() -> list[dict[str, Any]]:
    return [public_profile(profile_id) for profile_id in available_profile_ids()]


def rubric_preferred(profile_id: str) -> bool:
    return bool(ATS_PROFILES[normalize_profile(profile_id)].get("rubric_preferred"))


def normalize_profile(profile_id: object | None) -> str:
    profile = str(profile_id or DEFAULT_PROFILE).strip().lower()
    profile = LEGACY_PROFILE_MAP.get(profile, profile)
    if profile not in ATS_PROFILES:
        raise ValueError(profile)
    return profile


def normalize_profile_list(values: object | None) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("compare_profiles")
    normalized: list[str] = []
    for value in values:
        profile = normalize_profile(value)
        if profile not in normalized:
            normalized.append(profile)
    return normalized


def strict_payload(det_payload: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(det_payload)
    required_terms = [
        item for item in payload.get("matched", [])
        if isinstance(item, dict) and item.get("importance") == "required"
    ]
    required_terms.extend(
        item for item in payload.get("gaps", [])
        if isinstance(item, dict) and item.get("importance") == "required"
    )
    if required_terms:
        payload["score"] = payload.get("tier1_coverage")
        payload["note"] = "Strict requirements uses must-have coverage as the headline score."
    else:
        payload["note"] = "Strict requirements found no required terms; using deterministic keyword score."
    payload["source"] = "deterministic"
    return payload


def select_payload(
    profile_id: str,
    *,
    deterministic: dict[str, Any],
    rubric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile_id = normalize_profile(profile_id)
    if profile_id == "strict_requirements":
        return strict_payload(deterministic)
    if profile_id == "keyword_screen":
        return deepcopy(deterministic)
    if rubric_preferred(profile_id):
        return deepcopy(rubric or deterministic)
    return deepcopy(rubric or deterministic)


def attach_profile_metadata(
    payload: dict[str, Any],
    *,
    profile_id: str,
    job_id: str | None = None,
    computed_at: str | None = None,
) -> dict[str, Any]:
    result = deepcopy(payload)
    profile_id = normalize_profile(profile_id)
    profile = public_profile(profile_id)
    result["profile"] = profile
    result["summary"] = {
        "scorer": "onepager_ats_fit",
        "label": "CV Copilot ATS fit",
        "source": result.get("source", "deterministic"),
        "score": result.get("score"),
        "job": {"id": job_id} if job_id else None,
        "computed_at": computed_at,
        "profile": profile,
        "score_system": profile,
        "simulation": True,
        "vendor_backed": False,
        "must_have": {
            "coverage": result.get("tier1_coverage"),
            "target": result.get("tier1_target"),
        },
        "keywords": {
            "matched": result.get("matched", []),
            "gaps": result.get("gaps", []),
            "extracted_count": result.get("extracted_keyword_count"),
        },
        "rubric": {
            "categories": result.get("categories", {}),
            "strengths": result.get("strengths", []),
            "suggestions": result.get("suggestions", []),
            "bonus": result.get("bonus", 0),
            "deductions": result.get("deductions", 0),
        },
        "fallback": {
            "active": result.get("source") != "rubric",
            "note": result.get("note") or result.get("notes") or "",
        },
    }
    return result
