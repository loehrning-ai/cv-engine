"""In-house ATS keyword-gap scorer (Stage 6).

This is the deterministic, offline, LLM-free half of the tailoring feature. It
answers two questions a job-seeker actually has:

  1. *How well does my CV already cover what this job screens for?* → a 0-100
     ``match_score`` weighted by requirement importance.
  2. *Which requirements does my CV show NO evidence for?* → a ``gaps`` list,
     so the user decides apply-vs-upskill instead of fabricating a skill.

It deliberately uses NO paid ATS feed and NO model call. Keyword extraction
reuses :func:`engine.cover_letter.jd.parse_jd`'s output (``keywords`` +
``requirements``); coverage is exact-phrase / token-overlap against the CV's
flattened fact corpus (the same corpus the Stage-5 anti-fabrication guard
builds, so "evidence" here and "grounding" there agree).

Why deterministic matters: the score must be stable and explainable (the same
CV + JD always yields the same number and the same gap list), and it must be
fully unit-testable without a network or an API key. A model-scored ATS number
would be neither.

Scoring (mirrors the dossier §3 weighting, computed in-house here):
  * each keyword is weighted by importance — ``required`` = 3, ``preferred``
    = 2, ``nice_to_have`` = 1 — inferred from the JD phrasing around it;
  * ``score = round(100 * covered_weight / total_weight)``;
  * a keyword is *covered* when its term (or a normalized alias) appears in the
    CV corpus as a whole word / phrase.

The public surface is one pure function, :func:`score_cv_against_jd`, returning
an :class:`AtsResult`. Everything is a frozen dataclass so callers can't mutate
a result in place.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

try:  # engine/ is on sys.path in every entrypoint.
    from cover_letter.jd import parse_jd
except ImportError:  # pragma: no cover - unusual import roots
    from engine.cover_letter.jd import parse_jd


# Importance weights (dossier §3). Kept as a module constant so the single
# place that defines "how much each tier counts" is obvious and tweakable.
WEIGHTS: dict[str, int] = {"required": 3, "preferred": 2, "nice_to_have": 1}

# A JD must yield at least this many extractable keywords to score reliably;
# below it we return ``None`` rather than a misleading number on a thin JD.
MIN_KEYWORDS_TO_SCORE = 3

# Tier-1 (must-have) coverage target the UI frames "are you a fit?" around.
TIER1_TARGET_PCT = 70

# Section-header / label / boilerplate tokens that ``parse_jd`` surfaces as
# "keywords" because they are capitalized, but which an ATS does NOT key on
# (dossier §3: do not extract the job title, company name, or boilerplate).
# Compared case-insensitively against the whole keyword phrase.
_NON_KEYWORDS = frozenset(
    {
        "title", "role", "position", "company", "employer", "organization",
        "responsibilities", "requirements", "qualifications", "duties",
        "about", "about us", "about the role", "the role", "the team",
        "benefits", "perks", "salary", "location", "summary", "overview",
        "what you", "what we", "who you are", "your profile", "your role",
        "nice to have", "must have", "must haves", "tasks", "your tasks",
        "we offer", "you will", "you have", "you bring", "apply now",
        "job description", "job title", "full time", "part time", "remote",
        # German section headers.
        "aufgaben", "anforderungen", "profil", "ihr profil", "dein profil",
        "wir bieten", "deine aufgaben", "über uns", "stelle", "unternehmen",
        "arbeitgeber", "voraussetzungen", "benefits", "standort",
    }
)


# ---------------------------------------------------------------------------
# Importance inference — read the phrasing AROUND a keyword in the JD.
# ---------------------------------------------------------------------------
_REQUIRED_HINTS = re.compile(
    r"\b(must[- ]?have|must|required|requirement|essential|mandatory|"
    r"\d+\+?\s*years?|erforderlich|vorausgesetzt|zwingend|notwendig)\b",
    re.IGNORECASE,
)
_PREFERRED_HINTS = re.compile(
    r"\b(preferred|a plus|bonus|advantage|ideally|desirable|"
    r"von vorteil|wünschenswert|idealerweise)\b",
    re.IGNORECASE,
)
# "Nice to have" is its own (lowest) tier, distinct from "preferred".
_NICE_HINTS = re.compile(
    r"\b(nice[- ]?to[- ]?have)\b",
    re.IGNORECASE,
)


def _importance_for(term: str, jd_text: str, requirement_lines: list[str]) -> str:
    """Classify a keyword's importance from the JD phrasing near it.

    Heuristic, deterministic, and conservative:
      * a term that appears on a line carrying a "must/required/X+ years"
        cue → ``required``;
      * a term near a "preferred/nice-to-have/a plus" cue → ``preferred``;
      * a term that shows up in the parsed requirement lines at all → at least
        ``preferred`` (it's listed under requirements/qualifications);
      * otherwise ``nice_to_have``.
    The required cue wins over preferred when both appear on a line.
    """
    term_re = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)

    on_required_line = False
    on_preferred_line = False
    on_nice_line = False
    in_requirements = False
    for line in requirement_lines:
        if not term_re.search(line):
            continue
        in_requirements = True
        if _NICE_HINTS.search(line):
            on_nice_line = True
        if _REQUIRED_HINTS.search(line):
            on_required_line = True
        if _PREFERRED_HINTS.search(line):
            on_preferred_line = True

    # Also scan the raw JD for a sentence/line mentioning the term with a cue,
    # in case the term wasn't on a captured requirement line.
    for line in jd_text.splitlines():
        if not term_re.search(line):
            continue
        if _NICE_HINTS.search(line):
            on_nice_line = True
        if _REQUIRED_HINTS.search(line):
            on_required_line = True
        elif _PREFERRED_HINTS.search(line):
            on_preferred_line = True

    # An explicit "nice to have" cue is the weakest signal but it is explicit —
    # honor it over a generic "required" only when no stronger cue co-occurs on
    # a requirement line. Required wins when both appear; otherwise: required >
    # preferred > nice_to_have. A bare appearance in the requirements block
    # (no cue) defaults to nice_to_have so section noise isn't over-weighted.
    if on_required_line:
        return "required"
    if on_preferred_line:
        return "preferred"
    if on_nice_line:
        return "nice_to_have"
    if in_requirements:
        return "nice_to_have"
    return "nice_to_have"


# ---------------------------------------------------------------------------
# Keyword normalization + alias expansion (deterministic, no model).
# ---------------------------------------------------------------------------
# Canonical alias map: surface-form → set of equivalent forms to also match in
# the CV. Kept small and high-signal (the common DE/data-engineering stack
# variants). Matching is case-insensitive throughout.
_ALIASES: dict[str, tuple[str, ...]] = {
    "kubernetes": ("k8s",),
    "machine learning": ("ml",),
    "javascript": ("js",),
    "typescript": ("ts",),
    "google cloud": ("gcp", "google cloud platform"),
    "amazon web services": ("aws",),
    "continuous integration": ("ci", "ci/cd"),
    "postgresql": ("postgres",),
    "natural language processing": ("nlp",),
    "extract transform load": ("etl",),
}
# Reverse direction too (k8s in JD → match "kubernetes" in CV).
_REVERSE_ALIASES: dict[str, tuple[str, ...]] = {}
for _canon, _variants in _ALIASES.items():
    for _v in _variants:
        _REVERSE_ALIASES.setdefault(_v.lower(), tuple())
        _REVERSE_ALIASES[_v.lower()] = _REVERSE_ALIASES[_v.lower()] + (_canon,)


def _aliases_for(term: str) -> list[str]:
    """All forms to look for in the CV for ``term`` (itself + known aliases)."""
    low = term.lower()
    forms = {low}
    forms.update(a.lower() for a in _ALIASES.get(low, ()))
    forms.update(a.lower() for a in _REVERSE_ALIASES.get(low, ()))
    return sorted(forms)


# ---------------------------------------------------------------------------
# CV fact corpus + coverage test.
# ---------------------------------------------------------------------------
def _cv_corpus(cv: dict[str, Any]) -> str:
    """Flatten every string/number value in the CV into one searchable blob.

    Identical in spirit to the Stage-5 guard's corpus (so a term counts as
    "evidence" here exactly when it would count as "grounded" there). Keys are
    ignored — only the values the candidate actually wrote are evidence.
    """
    parts: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            parts.append(str(node))
        elif isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(cv or {})
    return "\n".join(parts)


def _term_in_corpus(term: str, corpus_lower: str) -> tuple[bool, str]:
    """Whole-word/phrase membership test for ``term`` (+ aliases) in the CV.

    Returns ``(found, matched_form)``. A multi-word term must appear as a
    contiguous phrase (whitespace-tolerant); a single token must appear as a
    whole word so "Java" never matches inside "JavaScript". Special chars in
    tech tokens (C++, C#, .NET, CI/CD, Node.js) are escaped and bounded by
    non-word edges rather than ``\\b`` (which misbehaves around ``+``/``#``).
    """
    for form in _aliases_for(term):
        words = form.split()
        if len(words) > 1:
            pattern = r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"
        else:
            tok = re.escape(form)
            # Edge-bounded so "go" doesn't match "google" but "c++"/"c#" work.
            pattern = r"(?<![\w+#.])" + tok + r"(?![\w+#.])"
        if re.search(pattern, corpus_lower, re.IGNORECASE):
            return True, form
    return False, ""


# ---------------------------------------------------------------------------
# Result types (frozen — immutable, JSON-serializable).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class KeywordMatch:
    """One JD keyword and whether the CV backs it."""

    term: str
    importance: str          # required | preferred | nice_to_have
    matched: bool
    evidence: str = ""       # the matched form when ``matched`` is True


@dataclass(frozen=True)
class Gap:
    """A JD requirement with NO CV evidence (apply-vs-upskill signal)."""

    term: str
    importance: str


@dataclass(frozen=True)
class AtsResult:
    """The full deterministic ATS analysis for a CV against a JD."""

    score: int | None                       # 0-100, or None if JD too thin
    tier1_coverage: int                      # % of required keywords covered
    tier1_target: int                        # the target % the UI compares to
    matched: list[KeywordMatch] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    extracted_keyword_count: int = 0
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        """A plain dict for a JSON response (route-friendly)."""
        return {
            "score": self.score,
            "tier1_coverage": self.tier1_coverage,
            "tier1_target": self.tier1_target,
            "matched": [asdict(m) for m in self.matched],
            "gaps": [asdict(g) for g in self.gaps],
            "extracted_keyword_count": self.extracted_keyword_count,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# The public scorer.
# ---------------------------------------------------------------------------
# Connective / boilerplate words that the JD parser may glue onto a real tech
# token when capitalized phrases straddle a sentence boundary (e.g.
# "Required. Plus Docker" → we want "Docker"). Used to split + clean phrases.
_PHRASE_NOISE = frozenset(
    {
        "required", "preferred", "plus", "bonus", "advantage", "experience",
        "expertise", "knowledge", "and", "or", "with", "in", "of", "the", "a",
        "an", "you", "your", "we", "our", "must", "have", "is", "are", "will",
        "ideally", "desirable", "nice", "to", "years", "year",
        # German modal / cue words — without these a line like "Muss Python
        # können" surfaces "Muss Python" (or a bare "Erforderlich") as a gap.
        "muss", "müssen", "haben", "erforderlich", "wünschenswert",
        "kenntnisse", "erfahrung", "können", "vorausgesetzt", "zwingend",
        "notwendig", "idealerweise", "und", "oder", "mit", "für", "von",
    }
)

# True tech tokens often carry these characters; we keep multi-word phrases
# only when EVERY word looks like a real term (no sentence terminator inside).
_SENTENCE_SPLIT = re.compile(r"[.!?;,]\s+|\s+[.!?;]\s*")


def _clean_subtokens(phrase: str) -> list[str]:
    """Split a parser keyword phrase into clean component terms.

    The reused ``parse_jd`` can merge adjacent capitalized words across a
    sentence boundary ("Terraform. Required", "Required. Plus Docker"). This
    splits on sentence punctuation, then drops connective/boilerplate words,
    yielding the real tokens ("Terraform", "Docker"). A genuine multi-word
    phrase whose words are ALL non-noise (e.g. "Apache Spark", "Google Cloud")
    is kept whole AND its salient last word is also surfaced as a fallback.
    """
    # First split on sentence-ish punctuation so "Terraform. Required" → two.
    segments = [s.strip() for s in _SENTENCE_SPLIT.split(phrase) if s.strip()]
    if not segments:
        segments = [phrase.strip()]

    out: list[str] = []
    for seg in segments:
        words = seg.split()
        kept = [w for w in words if w.lower().strip(".,;:") not in _PHRASE_NOISE]
        if not kept:
            continue
        if len(kept) == len(words):
            # A clean phrase (no noise words) — keep it whole.
            out.append(seg)
        else:
            # Mixed: emit each non-noise word as its own token.
            out.extend(kept)
    return out or [phrase.strip()]


def _collect_keywords(jd_json: dict[str, Any]) -> list[str]:
    """De-duplicated, noise-filtered keyword candidates from the parsed JD.

    Pulls the parser's ranked ``keywords``, splits parser-merged phrases into
    clean component terms (:func:`_clean_subtokens`), de-duplicates
    case-insensitively while preserving first-seen order (so the score is
    stable), and DROPS:
      * section-header / boilerplate tokens (:data:`_NON_KEYWORDS`);
      * the resolved company name and the job title (an ATS keys on skills, not
        on the employer or the role label — dossier §3).
    The result is the set of skills/tools/qualifications worth scoring.
    """
    seen: set[str] = set()
    out: list[str] = []

    drop = set(_NON_KEYWORDS)
    company = (jd_json.get("company") or "").strip().lower()
    title = (jd_json.get("title") or "").strip().lower()
    if company:
        drop.add(company)
    if title:
        drop.add(title)

    def _add(term: str) -> None:
        t = term.strip().strip(".,;:")
        if len(t) < 2:
            return
        low = t.lower()
        if low in seen or low in drop or low in _PHRASE_NOISE:
            return
        # Drop a multi-word phrase that merely echoes the title/company (e.g.
        # the parser surfacing "Senior Data" out of "Senior Data Engineer").
        # Exact title/company equality is already handled by ``drop`` above;
        # the substring rule must NOT apply to single tokens, or a real skill
        # that appears in the title ("Python" in "Senior Python Developer")
        # would silently vanish from the score.
        if title and len(low.split()) > 1 and (low in title or title in low):
            return
        if company and len(low.split()) > 1 and (low in company or company in low):
            return
        seen.add(low)
        out.append(t)

    for kw in jd_json.get("keywords", []):
        for sub in _clean_subtokens(kw):
            _add(sub)
    return out


def score_cv_against_jd(
    cv: dict[str, Any],
    jd_text: str,
    *,
    jd_json: dict[str, Any] | None = None,
) -> AtsResult:
    """Deterministically score ``cv`` against the job description ``jd_text``.

    Args:
        cv: the candidate's canonical CV model (dict). Its flattened string
            values are the evidence corpus.
        jd_text: the raw job description text. Parsed via ``parse_jd`` unless
            ``jd_json`` is supplied (the caller may pass an already-parsed JD
            to avoid double work — both paths give the same result).
        jd_json: optional pre-parsed JD (from :func:`parse_jd`).

    Returns:
        An :class:`AtsResult`. ``score`` is ``None`` when the JD is too thin to
        score (< :data:`MIN_KEYWORDS_TO_SCORE` keywords); otherwise a 0-100
        weighted-coverage integer. ``gaps`` lists every keyword with no CV
        evidence (sorted required → preferred → nice_to_have, then JD order).

    Raises:
        ValueError: if ``jd_text`` is empty/whitespace and no ``jd_json`` is
            supplied (propagated from :func:`parse_jd`; callers surface it as
            a 400). Only a genuinely EMPTY JD raises — a non-empty but thin JD
            returns ``score=None`` as documented above.

    Pure and offline: no LLM call, no network. The same inputs always produce
    the same output — that is the whole point of the in-house scorer.
    """
    parsed = jd_json if jd_json is not None else parse_jd(jd_text)
    jd_raw = parsed.get("raw") or jd_text or ""
    requirement_lines = list(parsed.get("requirements", []))

    keywords = _collect_keywords(parsed)
    corpus = _cv_corpus(cv)
    corpus_lower = corpus.lower()

    matched: list[KeywordMatch] = []
    gaps: list[Gap] = []
    covered_weight = 0
    total_weight = 0
    tier1_total = 0
    tier1_covered = 0

    for term in keywords:
        importance = _importance_for(term, jd_raw, requirement_lines)
        weight = WEIGHTS.get(importance, 1)
        total_weight += weight
        if importance == "required":
            tier1_total += 1

        found, form = _term_in_corpus(term, corpus_lower)
        if found:
            covered_weight += weight
            matched.append(
                KeywordMatch(term=term, importance=importance, matched=True, evidence=form)
            )
            if importance == "required":
                tier1_covered += 1
        else:
            gaps.append(Gap(term=term, importance=importance))

    extracted = len(keywords)

    # Stable ordering: required first, then preferred, then nice_to_have; JD
    # order preserved within a tier (we appended in JD order already).
    _rank = {"required": 0, "preferred": 1, "nice_to_have": 2}
    matched = sorted(matched, key=lambda m: _rank.get(m.importance, 3))
    gaps = sorted(gaps, key=lambda g: _rank.get(g.importance, 3))

    if extracted < MIN_KEYWORDS_TO_SCORE:
        return AtsResult(
            score=None,
            tier1_coverage=0,
            tier1_target=TIER1_TARGET_PCT,
            matched=matched,
            gaps=gaps,
            extracted_keyword_count=extracted,
            notes=(
                "The job description was too thin to score reliably "
                f"(found {extracted} keyword(s); need at least "
                f"{MIN_KEYWORDS_TO_SCORE})."
            ),
        )

    score = round(100 * covered_weight / total_weight) if total_weight else 0
    tier1_coverage = round(100 * tier1_covered / tier1_total) if tier1_total else 100

    return AtsResult(
        score=score,
        tier1_coverage=tier1_coverage,
        tier1_target=TIER1_TARGET_PCT,
        matched=matched,
        gaps=gaps,
        extracted_keyword_count=extracted,
        notes="",
    )
