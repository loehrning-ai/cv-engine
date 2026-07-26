"""LLM-driven ATS rubric (Stage 4).

This is the model-scored half of the ATS feature. Where
:func:`engine.ats.score.score_cv_against_jd` deterministically answers "which
must-have keywords does the CV cover?", :func:`score_cv_with_rubric` asks the
richer hiring question — "how well does THIS candidate fit THIS job?" — and
returns a structured rubric: a 0-100 fit score, four job-relative category
sub-scores with grounded evidence, plus strengths, gaps, and honest
(reframe-only) suggestions.

Design (mirrors :mod:`engine.cover_letter`):

  * Prompts live in :mod:`engine.ats.prompts` — a static, cache-friendly system
    prompt (global guardrails + fairness + task) and a per-call user message.
  * The call goes through the provider-agnostic :class:`engine.llm.client
    .LLMClient` with ``cache_system=True``, exactly like the cover-letter and
    tailor calls.
  * The deterministic scorer is fed in as a SIGNAL (so the model can cross-check
    its reasoning against a stable baseline) and remains the FALLBACK: every
    failure mode here is a typed, recoverable :class:`AtsRubricError`, and the
    function returns ``(None, error)`` so the caller degrades to the
    deterministic score instead of hard-failing. The CV/JD never change — the
    rubric is read-only, so the 1-page gate is untouched.
  * Anti-fabrication grounding (Global G9): suggestions are filtered against the
    CV fact corpus before the result is returned, so a model that nudges the
    user to add a non-existent skill can't reach the UI.

Everything is a frozen dataclass so a result can't be mutated in place. The
parse/validate helpers are pure and fully unit-testable with a mocked client.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

try:  # engine/ is on sys.path in every entrypoint.
    from ats.prompts import (
        CATEGORY_KEYS,
        CATEGORY_WEIGHTS,
        build_ats_rubric_system_prompt,
        build_ats_rubric_user_message,
    )
    from ats.score import AtsResult, _cv_corpus, score_cv_against_jd
    from cover_letter.jd import parse_jd
    from llm.client import LLMClient
except ImportError:  # pragma: no cover - unusual import roots
    from engine.ats.prompts import (  # type: ignore
        CATEGORY_KEYS,
        CATEGORY_WEIGHTS,
        build_ats_rubric_system_prompt,
        build_ats_rubric_user_message,
    )
    from engine.ats.score import (  # type: ignore
        AtsResult,
        _cv_corpus,
        score_cv_against_jd,
    )
    from engine.cover_letter.jd import parse_jd  # type: ignore
    from engine.llm.client import LLMClient  # type: ignore


# ===========================================================================
#  Result types (frozen — immutable, JSON-serializable).
# ===========================================================================
@dataclass(frozen=True)
class CategoryScore:
    """One remapped category's score (0-100) with grounded CV evidence."""

    category: str            # one of CATEGORY_KEYS
    score: int               # 0-100
    max: int = 100           # fixed ceiling (kept for the UI gauge math)
    evidence: list[str] = field(default_factory=list)  # quotes from the CV
    reasoning: str = ""      # one short sentence, optional


@dataclass(frozen=True)
class RubricResult:
    """The full LLM-driven ATS analysis for a CV against a JD."""

    score: int                                   # 0-100 weighted final
    categories: dict[str, CategoryScore]         # keyed by CATEGORY_KEYS
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    bonus: int = 0                               # 0 or 5
    deductions: int = 0                          # 0 or 5 (non-negative magnitude)
    note: str = ""                               # provenance / fallback reason

    def to_json(self) -> dict[str, Any]:
        """A plain dict for a JSON response (route-friendly)."""
        return {
            "score": self.score,
            "categories": {k: asdict(v) for k, v in self.categories.items()},
            "strengths": list(self.strengths),
            "gaps": list(self.gaps),
            "suggestions": list(self.suggestions),
            "bonus": self.bonus,
            "deductions": self.deductions,
            "note": self.note,
        }


# ===========================================================================
#  Typed, recoverable errors. Each carries an HTTP status the route MAY use,
#  but the contract is: on ANY of these the caller falls back to the
#  deterministic score — the rubric is never load-bearing.
# ===========================================================================
class AtsRubricError(Exception):
    """A recoverable ATS-rubric failure → graceful deterministic fallback.

    ``reason`` is a short, user-safe sentence (no key/provider details — see
    Global G4); ``http_status`` is a hint for the route if it chooses to surface
    one. The default path is to swallow the error and return the deterministic
    score, so this is informational, never fatal.
    """

    def __init__(self, reason: str, http_status: int = 502) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


class LLMTimeoutError(AtsRubricError):
    """The LLM call timed out."""

    def __init__(self) -> None:
        super().__init__("ATS rubric timed out; using the deterministic score.", 502)


class LLMRateLimitError(AtsRubricError):
    """The provider rate-limited the call (HTTP 429)."""

    def __init__(self) -> None:
        super().__init__("ATS is briefly rate-limited; using the deterministic score.", 429)


class LLMProviderMismatchError(AtsRubricError):
    """An auth/provider-key problem (HTTP 401/403) — treated as internal.

    We deliberately do NOT leak key-validation detail to the client (Global G4);
    the message stays generic and the route falls back to deterministic.
    """

    def __init__(self) -> None:
        super().__init__("ATS provider unavailable; using the deterministic score.", 502)


class LLMEmptyError(AtsRubricError):
    """The LLM returned an empty string (e.g. a safety-blocked Gemini turn)."""

    def __init__(self) -> None:
        super().__init__("ATS returned an empty result; using the deterministic score.", 502)


class LLMJSONDecodeError(AtsRubricError):
    """The LLM output could not be parsed as the expected JSON object."""

    def __init__(self) -> None:
        super().__init__("ATS returned malformed JSON; using the deterministic score.", 502)


# ===========================================================================
#  Parsing — pull the JSON object out of the model's reply, defensively.
# ===========================================================================
def _parse_ats_rubric_json(raw: str) -> dict[str, Any]:
    """Parse the rubric JSON from a raw model reply (first ``{`` … last ``}``).

    The task prompt forbids preamble / code fences, but models occasionally add
    a ```` ```json ```` fence or a stray sentence anyway. We locate the outer
    object by the first ``{`` and last ``}`` (robust to a leading fence and to
    trailing prose) and ``json.loads`` that slice.

    Raises:
        LLMEmptyError: the reply is empty / whitespace.
        LLMJSONDecodeError: no ``{...}`` slice, or it isn't valid JSON, or the
            top-level value isn't an object.
    """
    if not raw or not raw.strip():
        raise LLMEmptyError()

    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMJSONDecodeError()

    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError) as exc:  # json.JSONDecodeError is a ValueError
        raise LLMJSONDecodeError() from exc

    if not isinstance(parsed, dict):
        raise LLMJSONDecodeError()
    return parsed


# ===========================================================================
#  Validation / coercion — turn a parsed dict into a safe RubricResult.
#  Defensive on EVERY field: missing → safe default, out-of-range → clamped,
#  wrong type → coerced or dropped. A model that returns a partial or sloppy
#  object still yields a coherent, in-bounds result rather than crashing.
# ===========================================================================
def _coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort int coercion (handles ``"75"`` / ``75.0`` / junk → default)."""
    if isinstance(value, bool):  # bool is an int subclass — treat as junk here.
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (ValueError, TypeError):
            return default
    return default


def _clamp(value: int, low: int = 0, high: int = 100) -> int:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


def _string_list(value: Any, *, cap: int = 5) -> list[str]:
    """Coerce ``value`` into a capped list of non-empty trimmed strings."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            s = item.strip()
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            s = str(item)
        else:
            continue
        if s:
            out.append(s)
        if len(out) >= cap:
            break
    return out


def _coerce_category(name: str, data: Any) -> CategoryScore:
    """Coerce one category sub-object into a safe :class:`CategoryScore`.

    A missing category defaults to a neutral 50 with no evidence — neutral
    rather than 0 so a model that simply omits a category doesn't tank the
    final score; a model that genuinely scores it low will say so explicitly.
    """
    block = data if isinstance(data, dict) else {}
    score = _clamp(_coerce_int(block.get("score"), 50))
    reasoning = block.get("reasoning")
    reasoning = reasoning.strip() if isinstance(reasoning, str) else ""
    return CategoryScore(
        category=name,
        score=score,
        max=100,
        evidence=_string_list(block.get("evidence"), cap=5),
        reasoning=reasoning,
    )


def _recompute_score(
    categories: dict[str, CategoryScore], bonus: int, deductions: int
) -> int:
    """Recompute the final score from category weights + bonus − deductions.

    We do NOT trust the model's own ``score`` field as the source of truth:
    recomputing it from the (clamped) category scores and the documented
    :data:`engine.ats.prompts.CATEGORY_WEIGHTS` guarantees the headline number
    is internally consistent with the breakdown the UI shows, and removes any
    arithmetic the model got wrong. Result is clamped to [0, 100].
    """
    weighted = sum(
        categories[key].score * CATEGORY_WEIGHTS[key] for key in CATEGORY_KEYS
    )
    return _clamp(round(weighted) + bonus - deductions)


def _validate_rubric_result(parsed: dict[str, Any]) -> RubricResult:
    """Validate + coerce a parsed rubric dict into a safe :class:`RubricResult`.

    Never raises on a well-formed-but-sloppy object: every field is defaulted /
    clamped / coerced. The final ``score`` is RECOMPUTED from the category
    weights (see :func:`_recompute_score`) rather than trusting the model's
    arithmetic. ``bonus`` and ``deductions`` are normalized to non-negative
    magnitudes in {0, 5} (the only values the prompt permits).
    """
    raw_cats = parsed.get("categories")
    raw_cats = raw_cats if isinstance(raw_cats, dict) else {}
    categories = {
        key: _coerce_category(key, raw_cats.get(key)) for key in CATEGORY_KEYS
    }

    # Bonus/deductions: clamp to the {0, 5} the prompt allows; a model that
    # emits a negative deduction or an out-of-band magnitude is normalized.
    bonus = 5 if _coerce_int(parsed.get("bonus"), 0) >= 5 else 0
    deductions = 5 if abs(_coerce_int(parsed.get("deductions"), 0)) >= 5 else 0

    score = _recompute_score(categories, bonus, deductions)

    return RubricResult(
        score=score,
        categories=categories,
        strengths=_string_list(parsed.get("strengths"), cap=5),
        gaps=_string_list(parsed.get("gaps"), cap=5),
        suggestions=_string_list(parsed.get("suggestions"), cap=5),
        bonus=bonus,
        deductions=deductions,
        note="rubric_source=llm",
    )


# ===========================================================================
#  Anti-fabrication grounding for suggestions (Global G9).
#  A suggestion may only ask the user to reframe/surface/quantify EXISTING
#  work. We drop any suggestion whose content-bearing tokens are mostly NOT
#  present in the CV corpus — that signature catches "add Kubernetes" /
#  "include your AWS certification" when the CV has neither, while keeping
#  honest "surface your Airflow work" / "quantify the latency win" (whose key
#  nouns ARE in the CV). Conservative on the keep side so we don't strip good
#  advice; gaps remain the place to name genuinely-missing skills.
# ===========================================================================
# Function/stop words that carry no grounding signal — excluded from the ratio.
_SUGGESTION_STOP = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at",
        "for", "with", "as", "by", "from", "into", "your", "you", "their",
        "this", "that", "these", "those", "add", "include", "surface", "show",
        "showcase", "highlight", "mention", "list", "reframe", "quantify",
        "metric", "metrics", "number", "numbers", "role", "roles", "work",
        "experience", "section", "bullet", "bullets", "consider", "could",
        "should", "would", "make", "more", "about", "where", "which", "when",
        "have", "has", "had", "are", "was", "were", "been", "being", "its",
        "it", "is", "be", "if", "so", "do", "did", "will", "can", "may",
    }
)


def _ground_suggestions(
    suggestions: list[str], corpus_words: set[str], *, min_ratio: float = 0.5
) -> tuple[list[str], list[str]]:
    """Split suggestions into (grounded_kept, dropped) against the CV corpus.

    A suggestion is KEPT when at least ``min_ratio`` of its content-bearing
    tokens (len > 3, not a stop word) appear in ``corpus_words``, OR when it has
    no content-bearing tokens at all (a purely structural nudge like "quantify
    your results" — harmless, no fabricated entity). Otherwise it is DROPPED as
    likely asking the user to add something the CV doesn't support.

    Returns ``(kept, dropped)`` so the caller can keep the clean list and report
    how many were filtered. Pure + deterministic.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for sugg in suggestions:
        tokens = [
            t.strip(".,;:!?()[]{}'\"`")
            for t in sugg.lower().split()
        ]
        content = [
            t for t in tokens if len(t) > 3 and t not in _SUGGESTION_STOP
        ]
        if not content:
            kept.append(sugg)
            continue
        grounded = sum(1 for t in content if t in corpus_words)
        if grounded / len(content) >= min_ratio:
            kept.append(sugg)
        else:
            dropped.append(sugg)
    return kept, dropped


def ground_rubric_suggestions(
    cv: dict[str, Any], result: RubricResult
) -> tuple[RubricResult, list[str]]:
    """Return a copy of ``result`` with ungrounded suggestions removed.

    Reuses :func:`engine.ats.score._cv_corpus` so "grounded" here means the
    same thing it does for the deterministic scorer and the cover-letter guard.
    Returns ``(filtered_result, dropped_suggestions)``; the CV is never mutated.
    """
    corpus_words = {
        w for w in _cv_corpus(cv).lower().replace("/", " ").split() if w
    }
    kept, dropped = _ground_suggestions(result.suggestions, corpus_words)
    if not dropped:
        return result, []
    fields = asdict(result)
    # asdict() deep-converts the nested CategoryScore dataclasses to dicts;
    # rebuild them so the returned result keeps its typed shape.
    fields["categories"] = result.categories
    fields["suggestions"] = kept
    return RubricResult(**fields), dropped


# ===========================================================================
#  The public entry point.
# ===========================================================================
def score_cv_with_rubric(
    *,
    cv: dict[str, Any],
    jd_text: str,
    jd_json: dict[str, Any] | None = None,
    deterministic_ats: AtsResult | None = None,
    llm_client: LLMClient,
    model: str | None = None,
) -> tuple[RubricResult | None, AtsRubricError | None]:
    """Score ``cv`` against the job ``jd_text`` with the LLM rubric.

    Args:
        cv: the candidate's canonical CV model (the SOLE source of candidate
            facts — Global G1).
        jd_text: the raw job description. Parsed via ``parse_jd`` unless
            ``jd_json`` is supplied.
        jd_json: optional pre-parsed JD (avoids re-parsing when the caller
            already has one).
        deterministic_ats: optional pre-computed deterministic
            :class:`engine.ats.score.AtsResult`, fed to the prompt as a signal.
            Computed here if not supplied.
        llm_client: a wired :class:`engine.llm.client.LLMClient` (the caller
            owns key resolution + provider selection).
        model: optional model id override (else the client's default).

    Returns:
        ``(RubricResult, None)`` on success, or ``(None, AtsRubricError)`` on
        any recoverable failure (timeout / 429 / auth / empty / malformed JSON /
        unparseable JD). The caller MUST fall back to the deterministic score on
        an error — the rubric is never load-bearing. Suggestions in a successful
        result are already grounded (ungrounded ones are dropped).

    Pure of side effects on the CV: the rubric is read-only, so the 1-page gate
    and stored documents are untouched.
    """
    # Parse the JD if not supplied. A thin/empty JD is a recoverable error.
    if jd_json is None:
        try:
            jd_json = parse_jd(jd_text)
        except ValueError:
            return None, AtsRubricError(
                "Job description was empty; using the deterministic score.", 400
            )

    # Compute the deterministic signal if not supplied.
    if deterministic_ats is None:
        deterministic_ats = score_cv_against_jd(cv, jd_text, jd_json=jd_json)

    system_prompt = build_ats_rubric_system_prompt()
    user_message = build_ats_rubric_user_message(
        cv,
        jd_json,
        {
            "score": deterministic_ats.score,
            "tier1_coverage": deterministic_ats.tier1_coverage,
            "extracted_keyword_count": deterministic_ats.extracted_keyword_count,
        },
    )

    # Call the model. Map known failure signatures to typed errors; anything
    # else becomes a generic recoverable AtsRubricError. We import ATS_RUBRIC
    # _MAX_TOKENS lazily-free (module constant) for the call budget.
    try:
        from ats.prompts import ATS_RUBRIC_MAX_TOKENS  # local import: cheap, avoids cycle noise
    except ImportError:  # pragma: no cover - unusual import roots
        from engine.ats.prompts import ATS_RUBRIC_MAX_TOKENS  # type: ignore

    try:
        raw = llm_client.complete(
            system=system_prompt,
            user=user_message,
            model=model,
            max_tokens=ATS_RUBRIC_MAX_TOKENS,
            cache_system=True,
        )
    except TimeoutError:
        return None, LLMTimeoutError()
    except Exception as exc:  # noqa: BLE001 — provider/timeout/refusal/network
        text = str(exc).lower()
        if "429" in text or "rate" in text:
            return None, LLMRateLimitError()
        if "401" in text or "403" in text or "permission" in text or "auth" in text:
            return None, LLMProviderMismatchError()
        if "timeout" in text or "timed out" in text:
            return None, LLMTimeoutError()
        return None, AtsRubricError(
            "ATS rubric call failed; using the deterministic score.", 502
        )

    # Parse + validate. Both raise typed AtsRubricError subclasses on failure.
    try:
        parsed = _parse_ats_rubric_json(raw)
        result = _validate_rubric_result(parsed)
    except AtsRubricError as exc:
        return None, exc
    except Exception:  # noqa: BLE001 — defensive: any coercion surprise → fallback
        return None, AtsRubricError(
            "ATS rubric result was invalid; using the deterministic score.", 502
        )

    # Ground the suggestions (drop any that nudge a fabrication).
    grounded, _dropped = ground_rubric_suggestions(cv, result)
    return grounded, None
