"""Deterministic experience-years extraction for jobs and CVs.

This module is intentionally small, offline, and conservative. It extracts
literal year requirements from job descriptions, derives candidate years from
dated CV experience intervals, merges overlaps, and returns evidence payloads.
It does not modify ATS scores.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
import re
from typing import Any


_YEAR_MONTH = re.compile(r"^\s*(?:(?P<month>0?[1-9]|1[0-2])[/.-](?P<year>\d{4})|(?P<iso_year>\d{4})-(?P<iso_month>0?[1-9]|1[0-2]))\s*$")
_YEAR_ONLY = re.compile(r"^\s*(?P<year>\d{4})\s*$")
_PRESENT = re.compile(r"^\s*(present|current|now|heute|aktuell)\s*$", re.IGNORECASE)
_YEAR_REQ = re.compile(
    r"(?P<prefix>\b(?:at\s+least|minimum|min\.?|mindestens|required|must\s+have|"
    r"requires?|requirement|preferred|ideally|up\s+to)?\b[\s:-]*)"
    r"(?P<years>\d{1,2})(?:\s*\+)?\s*(?:years?|yrs?|jahre?n?)"
    r"(?:\s+(?:"
    # Connected skill: "4 years of Go", "3 years experience with Python".
    r"(?:of|with|in|experience\s+(?:with|in)|erfahrung\s+(?:mit|in))\s+"
    r"(?P<skill>[A-Za-z][A-Za-z0-9+.#/-]*(?:\s+[A-Za-z][A-Za-z0-9+.#/-]*){0,3})"
    # Bare skill, no connector: "4 years Go experience required". Only trusted
    # when the run ends in "experience"/"erfahrung" — the existing trailing-cue
    # strip removes it — so "4 years experience required" yields NO skill and
    # "18 years old" never invents one.
    r"|(?P<bare_skill>[A-Za-z][A-Za-z0-9+.#/-]*(?:\s+[A-Za-z][A-Za-z0-9+.#/-]*){0,2}"
    r"\s+(?:experience|erfahrung))(?=\s|$)"
    r"))?",
    re.IGNORECASE,
)
_SENIORITY = (
    ("staff", 8),
    ("principal", 8),
    ("lead", 6),
    ("senior", 5),
    ("sr.", 5),
    ("mid", 3),
    ("junior", 1),
)
_FALSE_YEAR_CONTEXT = re.compile(
    r"\b(salary|comp|eur|usd|chf|pln|date|posted|contract|befristet|renewable)\b|[€$]",
    re.IGNORECASE,
)
_SKILL_LEADIN = re.compile(r"^(?:experience|erfahrung)\s+(?:with|in|mit)\s+", re.IGNORECASE)
_SKILL_TRAILING_CUE = re.compile(
    r"\s+\b(required|preferred|ideally|minimum|min\.?|must|bonus|nice|wünschenswert|erforderlich)\b.*$",
    re.IGNORECASE,
)
# A trailing "experience"/"erfahrung" the skill group can swallow (e.g. "4
# years of Go experience required" captures "Go experience") — it is a cue
# word, not part of the skill name. English "experience" is matched
# case-sensitively: a capitalized "Experience" is part of the skill itself
# ("User Experience", "Customer Experience"). German "Erfahrung" stays
# case-insensitive because German capitalizes nouns ("Kubernetes Erfahrung").
_SKILL_TRAILING_EXP = re.compile(r"\s+(?:experience|(?i:erfahrung))$")
RANKING_CONTRIBUTION_WEIGHT = 0.18


@dataclass(frozen=True)
class MonthInterval:
    start: int
    end: int
    label: str
    uncertain: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExperienceRequirement:
    years: float
    skill: str = ""
    importance: str = "required"
    evidence: str = ""
    uncertain: bool = False


@dataclass(frozen=True)
class CandidateExperience:
    total_months: int
    total_years: float
    intervals: list[MonthInterval] = field(default_factory=list)
    merged_intervals: list[MonthInterval] = field(default_factory=list)
    uncertain: bool = False
    notes: list[str] = field(default_factory=list)
    as_of: str = ""

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["intervals"] = [asdict(i) for i in self.intervals]
        payload["merged_intervals"] = [asdict(i) for i in self.merged_intervals]
        return payload


def _month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def _month_label(index: int) -> str:
    year, month0 = divmod(index, 12)
    return f"{year:04d}-{month0 + 1:02d}"


def _parse_as_of(value: str | date | None = None) -> int:
    if isinstance(value, date):
        return _month_index(value.year, value.month)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if re.match(r"^\d{4}-\d{2}$", raw):
            year, month = raw.split("-", 1)
            return _month_index(int(year), int(month))
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return _month_index(parsed.year, parsed.month)
        except ValueError:
            pass
    today = datetime.now(timezone.utc).date()
    return _month_index(today.year, today.month)


def _parse_date(value: object, *, is_end: bool, as_of_index: int) -> tuple[int | None, bool, str]:
    if not isinstance(value, str) or not value.strip():
        return None, True, "missing date"
    raw = value.strip()
    if _PRESENT.match(raw):
        return as_of_index + (1 if is_end else 0), False, "present normalized to as_of"
    m = _YEAR_MONTH.match(raw)
    if m:
        year = int(m.group("year") or m.group("iso_year"))
        month = int(m.group("month") or m.group("iso_month"))
        idx = _month_index(year, month)
        return idx + (1 if is_end else 0), False, ""
    m = _YEAR_ONLY.match(raw)
    if m:
        year = int(m.group("year"))
        idx = _month_index(year, 12 if is_end else 1)
        return idx + (1 if is_end else 0), True, "year-only date"
    return None, True, f"unparsed date: {raw}"


def _role_text(role: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("role", "company", "stack", "location", "summary"):
        value = role.get(key)
        if isinstance(value, str):
            parts.append(value)
    for bullet in role.get("bullets") or []:
        if isinstance(bullet, str):
            parts.append(bullet)
    return "\n".join(parts)


def dated_experience_intervals(
    cv: dict[str, Any],
    *,
    as_of: str | date | None = None,
    skill: str | None = None,
) -> tuple[list[MonthInterval], list[str]]:
    as_of_index = _parse_as_of(as_of)
    notes: list[str] = []
    intervals: list[MonthInterval] = []
    skill_low = (skill or "").strip().lower()
    for role in cv.get("experience") or []:
        if not isinstance(role, dict):
            continue
        text = _role_text(role)
        if skill_low and skill_low not in text.lower():
            continue
        start, start_uncertain, start_note = _parse_date(role.get("start"), is_end=False, as_of_index=as_of_index)
        end, end_uncertain, end_note = _parse_date(role.get("end"), is_end=True, as_of_index=as_of_index)
        label = " - ".join(part for part in (role.get("company"), role.get("role")) if isinstance(part, str) and part)
        role_notes = [note for note in (start_note, end_note) if note]
        if start is None or end is None:
            notes.append(f"{label or 'experience'} skipped: {', '.join(role_notes) or 'missing date'}")
            continue
        if end <= start:
            notes.append(f"{label or 'experience'} skipped: end before start")
            continue
        if start > as_of_index + 1:
            notes.append(f"{label or 'experience'} skipped: future start")
            continue
        intervals.append(MonthInterval(start=start, end=end, label=label or "experience", uncertain=start_uncertain or end_uncertain, notes=tuple(role_notes)))
    return intervals, notes


def merge_intervals(intervals: list[MonthInterval]) -> list[MonthInterval]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda i: (i.start, i.end))
    merged: list[MonthInterval] = [ordered[0]]
    for item in ordered[1:]:
        prev = merged[-1]
        if item.start <= prev.end:
            merged[-1] = MonthInterval(
                start=prev.start,
                end=max(prev.end, item.end),
                label=f"{prev.label}; {item.label}",
                uncertain=prev.uncertain or item.uncertain,
                notes=tuple([*prev.notes, *item.notes]),
            )
        else:
            merged.append(item)
    return merged


def candidate_experience(cv: dict[str, Any], *, as_of: str | date | None = None, skill: str | None = None) -> CandidateExperience:
    as_of_index = _parse_as_of(as_of)
    intervals, notes = dated_experience_intervals(cv, as_of=as_of, skill=skill)
    merged = merge_intervals(intervals)
    total_months = sum(max(0, item.end - item.start) for item in merged)
    uncertain = any(item.uncertain for item in intervals) or bool(notes)
    return CandidateExperience(
        total_months=total_months,
        total_years=round(total_months / 12, 1),
        intervals=intervals,
        merged_intervals=merged,
        uncertain=uncertain,
        notes=notes,
        as_of=_month_label(as_of_index),
    )


def extract_experience_requirements(jd_text: str, *, jd_json: dict[str, Any] | None = None) -> dict[str, Any]:
    text = jd_text or (jd_json or {}).get("raw") or ""
    requirements: list[ExperienceRequirement] = []
    for line in text.splitlines() or [text]:
        for match in _YEAR_REQ.finditer(line):
            # False-year context (salary, contract duration, …) may sit just
            # AFTER the matched span ("2 years contract position", "2 Jahre
            # befristet"), so check the match plus the rest of its sentence —
            # not the whole line, which would suppress genuine requirements
            # sharing a line with salary text.
            tail = re.split(r"[.!?]", line[match.end():], maxsplit=1)[0]
            if _FALSE_YEAR_CONTEXT.search(match.group(0) + tail):
                continue
            years = float(match.group("years"))
            prefix = (match.group("prefix") or "").lower()
            evidence = match.group(0).strip()
            if "up to" in prefix:
                requirements.append(ExperienceRequirement(years=years, importance="maximum", evidence=evidence, uncertain=True))
                continue
            importance = "preferred" if re.search(r"preferred|ideally|bonus|nice|wünschenswert", line, re.IGNORECASE) else "required"
            skill = (match.group("skill") or match.group("bare_skill") or "").strip(" .,:;")
            skill = _SKILL_TRAILING_CUE.sub("", _SKILL_LEADIN.sub("", skill)).strip(" .,:;")
            skill = _SKILL_TRAILING_EXP.sub("", skill).strip(" .,:;")
            if skill.lower() in {"experience", "erfahrung"}:
                skill = ""
            requirements.append(ExperienceRequirement(years=years, skill=skill, importance=importance, evidence=evidence))

    seniority_label = ""
    inferred_years: float | None = None
    low = text.lower()
    for label, years in _SENIORITY:
        if re.search(r"\b" + re.escape(label) + r"\b", low):
            seniority_label = label
            inferred_years = float(years)
            break

    required = [r for r in requirements if r.importance == "required"]
    total_years = max((r.years for r in required), default=None)
    if total_years is None and inferred_years is not None:
        total_years = inferred_years
    skill_years = [asdict(r) for r in requirements if r.skill]
    evidence = [r.evidence for r in requirements[:8]]
    if seniority_label and inferred_years is not None:
        evidence.append(f"seniority label: {seniority_label}")
    return {
        "total_years_min": total_years,
        "skill_years": skill_years,
        "seniority_label": seniority_label,
        "evidence": evidence,
        "uncertain": any(r.uncertain for r in requirements) or (total_years is None and bool(seniority_label)),
    }


def build_experience_fit(
    cv: dict[str, Any],
    jd_text: str,
    *,
    jd_json: dict[str, Any] | None = None,
    as_of: str | date | None = None,
) -> dict[str, Any]:
    req = extract_experience_requirements(jd_text, jd_json=jd_json)
    cand = candidate_experience(cv, as_of=as_of)
    skill_fit: list[dict[str, Any]] = []
    for item in req.get("skill_years") or []:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("skill") or "").strip()
        years = item.get("years")
        if not skill or not isinstance(years, (int, float)):
            continue
        skill_cand = candidate_experience(cv, as_of=as_of, skill=skill)
        missing_skill = max(0.0, round(float(years) - skill_cand.total_years, 1))
        skill_fit.append(
            {
                "skill": skill,
                "required_years": float(years),
                "candidate_years": skill_cand.total_years,
                "missing_years": missing_skill,
                "fit": "meets" if missing_skill <= 0 else ("near" if missing_skill <= 1 else "below"),
                "importance": item.get("importance") or "required",
                "evidence": item.get("evidence") or "",
                "uncertain": bool(skill_cand.uncertain or item.get("uncertain")),
                "candidate": [
                    {"start": _month_label(i.start), "end": _month_label(i.end), "label": i.label, "uncertain": i.uncertain}
                    for i in skill_cand.merged_intervals
                ],
                "notes": skill_cand.notes,
            }
        )
    required = req.get("total_years_min")
    candidate = cand.total_years
    missing = None
    fit = "unknown"
    if isinstance(required, (int, float)):
        missing = max(0.0, round(float(required) - candidate, 1))
        if missing <= 0:
            fit = "meets"
        elif missing <= 1:
            fit = "near"
        else:
            fit = "below"
    contribution = _ranking_contribution(
        required=required,
        candidate=candidate,
        missing=missing,
        fit=fit,
        uncertain=bool(req.get("uncertain")) or cand.uncertain,
        requirement_evidence=req.get("evidence", []),
        candidate_labels=[f"{_month_label(i.start)}..{_month_label(i.end)} {i.label}" for i in cand.merged_intervals],
        skill_fit=skill_fit,
    )
    return {
        "required_years": required,
        "candidate_years": candidate,
        "missing_years": missing,
        "fit": fit,
        "uncertain": bool(req.get("uncertain")) or cand.uncertain,
        "ranking_contribution": contribution,
        "evidence": {
            "requirements": req.get("evidence", []),
            "candidate": [
                {"start": _month_label(i.start), "end": _month_label(i.end), "label": i.label, "uncertain": i.uncertain}
                for i in cand.merged_intervals
            ],
            "notes": cand.notes,
        },
        "experience_requirements": req,
        "candidate_experience": cand.to_json(),
        "skill_fit": skill_fit,
    }


def _ranking_contribution(
    *,
    required: object,
    candidate: float,
    missing: float | None,
    fit: str,
    uncertain: bool,
    requirement_evidence: list[Any],
    candidate_labels: list[str],
    skill_fit: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a separate ranking component without mutating ATS confidence."""
    if not isinstance(required, (int, float)):
        score = 0.0
        bucket = "unknown"
        reason = "No explicit total-years requirement was detected."
    else:
        req_float = float(required)
        miss_float = float(missing or 0.0)
        if miss_float <= 0:
            score = 100.0
            bucket = "meets"
        elif miss_float <= 1:
            score = max(65.0, 100.0 - miss_float * 25.0)
            bucket = "near"
        else:
            score = max(0.0, 60.0 - miss_float * 15.0)
            bucket = "below"
        reason = f"{candidate:.1f} candidate years vs {req_float:.1f} required years."

    required_skill = [row for row in skill_fit if row.get("importance") == "required"]
    skill_below = [row for row in required_skill if row.get("fit") == "below"]
    if skill_below:
        score = min(score, 45.0)
        bucket = "below"
        reason += " Required skill-specific years are below the posting threshold."

    if uncertain and bucket != "unknown":
        score = round(score * 0.85, 1)
        reason += " Date evidence is uncertain."

    weighted = round(score * RANKING_CONTRIBUTION_WEIGHT, 1)
    return {
        "component": "experience_years",
        "score": round(score, 1),
        "weight": RANKING_CONTRIBUTION_WEIGHT,
        "weighted_score": weighted,
        "bucket": bucket or fit or "unknown",
        "missing_years": missing,
        "uncertain": bool(uncertain),
        "reason": reason.strip(),
        "evidence": [str(item) for item in [*requirement_evidence[:4], *candidate_labels[:4]] if str(item).strip()],
    }
