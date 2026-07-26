"""Parse free-text job descriptions into a keyed JSON structure (Stage 5).

The grounded cover-letter prompt receives the JD as a structured object, not
a raw blob: a keyed shape lets the model (and our guards) reason about the
title, company, and requirement keywords separately, and keeps the prompt
stable across pastes.

This parser is intentionally DETERMINISTIC and dependency-light — pure
regex/heuristics over the text, no LLM call. That keeps it cheap, testable
offline (the LLM is mocked in tests), and free of its own fabrication risk:
it only ever extracts spans that literally appear in the input.

The shape it returns:

    {
      "title":        str,            # best-effort role title, "" if unknown
      "company":      str,            # best-effort employer, "" if unknown
      "language":     "en" | "de" | "unknown",
      "requirements": [str, ...],     # requirement/qualification lines
      "keywords":     [str, ...],     # recurring capitalized terms / tools
      "raw":          str,            # the (size-capped) original text
    }

``title``/``company`` may be supplied by the caller (e.g. from a saved_jobs
row) via :func:`parse_jd`'s ``title``/``company`` kwargs; an explicit value
always wins over the heuristic guess.
"""
from __future__ import annotations

import re
from typing import Any

# Hard cap on the JD text we keep / send. A JD over this is almost certainly
# pasted boilerplate; trimming protects the prompt budget and the row size.
MAX_JD_CHARS = 20_000

# Prompt-injection / data-hygiene sanitization (Stage 10). The JD is UNTRUSTED
# text that flows verbatim into LLM prompts (the ATS rubric, the cover-letter and
# tailor calls) via this parser's ``raw`` / ``requirements`` / ``keywords``
# output. The prompts already treat it as data (guardrail G3), but a JD can carry
# invisible control / format characters that don't render yet survive into the
# prompt and can be used to smuggle instructions (e.g. bidi overrides that hide a
# "ignore your instructions" run, or zero-width joiners that split a refusal
# trigger). We strip those at THIS single boundary — the one chokepoint every JD
# consumer shares — so the prompt only ever sees legible text.
#
# What we strip:
#   * C0 control chars (U+0000-U+001F) EXCEPT tab/newline/carriage-return, which
#     are legitimate JD structure the line-based parser relies on;
#   * the DEL + C1 control range (U+007F-U+009F);
#   * Unicode format/invisible chars browsers ignore but a model still reads:
#     zero-width space/joiner (U+200B-U+200D), the bidi marks/overrides
#     (U+200E-U+200F, U+202A-U+202E, U+2066-U+2069), word-joiner/invisible-math
#     (U+2060-U+2064), and the BOM/zero-width-no-break-space (U+FEFF).
# Visible text, ordinary whitespace, and all printable Unicode are untouched, so
# a legitimate non-Latin JD is unharmed.
_JD_CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    r"\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


def _strip_control_chars(text: str) -> str:
    """Remove injection-prone control / invisible-format chars from JD text.

    Pure + deterministic. Keeps tab/newline/carriage-return (the only C0 chars
    the line parser needs) and every printable character; see the module
    constant above for the exact ranges removed and why.
    """
    return _JD_CONTROL_CHARS.sub("", text)

# Lines that look like a requirement / qualification we want to surface.
_REQ_HINT = re.compile(
    r"\b(require[ds]?|must have|responsib|qualif|you (?:have|bring|will)|"
    r"experience (?:with|in)|proficien|familiar with|years? of|"
    r"erforderlich|vorausgesetzt|du (?:hast|bringst)|kenntnisse)\b",
    re.IGNORECASE,
)

# Label/value separator: a colon (tight or spaced) OR a hyphen/dash only when
# whitespace-delimited. A bare hyphen in a compound word ("Role-based …",
# "Company-wide …") must not act as a label separator, or _guess_title/company
# would mistake a requirement line for the role/employer (and, downstream, that
# wrong value would feed the grounding corpus).
_LABEL_SEP = r"(?:\s*:\s*|\s+[-–—]\s+)"
# A "Title:" / "Role:" / "Position:" style explicit label.
_TITLE_LABEL = re.compile(
    r"^\s*(?:job\s+title|title|role|position|stelle|position)" + _LABEL_SEP + r"(.+?)\s*$",
    re.IGNORECASE,
)
_COMPANY_LABEL = re.compile(
    r"^\s*(?:company|employer|organi[sz]ation|firma|unternehmen|arbeitgeber)" + _LABEL_SEP + r"(.+?)\s*$",
    re.IGNORECASE,
)

# German stop-words that flag the JD as German when several appear.
_DE_MARKERS = re.compile(
    r"\b(und|oder|für|mit|wir|sie|deine?|unsere?n?|kenntnisse|erfahrung|"
    r"aufgaben|anforderungen|bewerbung|gehalt|mitarbeiter)\b",
    re.IGNORECASE,
)
_EN_MARKERS = re.compile(
    r"\b(and|or|the|with|you|we|your|our|experience|requirements|"
    r"responsibilities|skills|team|role)\b",
    re.IGNORECASE,
)

# A capitalized multi-word noun phrase or an all-caps/Camel tech token —
# the kind of recurring term an ATS keys on (e.g. "Apache Spark", "Kubernetes").
_KEYWORD_TOKEN = re.compile(
    r"\b([A-Z][A-Za-z0-9+.#]*(?:\s+[A-Z][A-Za-z0-9+.#]*){0,2})\b"
)
# Requirement cues that mark a line as a requirement, never a role title —
# used by the _guess_title fallback so a JD pasted starting at its
# "Requirements:" section doesn't yield "Must have Python" as the title.
_TITLE_REQ_CUE = re.compile(
    r"\b(must|required|erforderlich|muss|müssen|sollen|kenntnisse|erfahrung|"
    r"\d+\+?\s*(?:years|jahre))\b",
    re.IGNORECASE,
)
# Whole-line requirement-section headers ("Anforderungen:", "Requirements", …).
# Once one of these opens the JD, everything below is a requirements list, so
# the _guess_title fallback must stop rather than grab the next bullet.
_REQ_SECTION_HEADER = re.compile(
    r"^\s*(?:anforderungen|qualifikationen|voraussetzungen|profil|ihr profil|"
    r"requirements|qualifications)\s*:?\s*$",
    re.IGNORECASE,
)
# "<Role title> at <Company>" — a common JD headline shape ("Senior Python
# Developer at Acme"). Only short, sentence-free lines with a capitalized
# candidate, so prose ("... at scale", "team at work") never yields a company.
_TITLE_AT_COMPANY = re.compile(
    r"^[^.!?:]{3,80}?\s+\bat\s+(?P<company>[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,2})\s*$"
)
# Tokens that are not useful keywords even though they're capitalized.
_KEYWORD_STOP = frozenset(
    {
        "we", "you", "your", "our", "the", "and", "or", "a", "an", "to", "of",
        "in", "for", "with", "as", "is", "are", "will", "this", "that", "job",
        "role", "company", "team", "work", "about", "what", "who", "why",
        "monday", "tuesday", "wednesday", "thursday", "friday",
    }
)


def _detect_language(text: str) -> str:
    """Best-effort EN/DE detection from marker-word frequency."""
    de = len(_DE_MARKERS.findall(text))
    en = len(_EN_MARKERS.findall(text))
    if de == 0 and en == 0:
        return "unknown"
    if de > en:
        return "de"
    if en > de:
        return "en"
    return "unknown"


def _guess_title(lines: list[str]) -> str:
    """Heuristic role title: an explicit label, else a short early heading."""
    for line in lines[:25]:
        m = _TITLE_LABEL.match(line)
        if m:
            return m.group(1).strip()
    # Fallback: the first short, title-ish line (<= 80 chars, has a letter,
    # not a sentence/bullet, not a requirement line) near the top. A JD pasted
    # starting at its "Requirements:" section must not guess "Must have
    # Python" as the title — a wrong title poisons the ATS keyword filter
    # downstream (real skills echoing it get dropped).
    for line in lines[:8]:
        s = line.strip(" \t-•*#")
        if _REQ_SECTION_HEADER.match(s):
            # A requirement-section header: nothing below it is a title.
            break
        if 3 <= len(s) <= 80 and re.search(r"[A-Za-zÀ-ÿ]", s) and not s.endswith(
            (".", ":", "!")
        ):
            if _TITLE_REQ_CUE.search(s):
                continue
            return s
    return ""


def _guess_company(lines: list[str]) -> str:
    """Heuristic employer name from an explicit ``Company:`` label, or from a
    "<Title> at <Company>" headline ("Senior Python Developer at Acme").

    Beyond those two shapes we never *guess* a company from prose — a wrong
    company name is worse than none, and the cover-letter prompt handles a
    missing company.
    """
    for line in lines[:30]:
        m = _COMPANY_LABEL.match(line)
        if m:
            return m.group(1).strip()
    # Headline shape: without it the company token leaks into the ATS keyword
    # list downstream and surfaces as a bogus skill gap ("Acme").
    for line in lines[:10]:
        m = _TITLE_AT_COMPANY.match(line.strip())
        if m:
            return m.group("company").strip()
    return ""


def _extract_requirements(lines: list[str]) -> list[str]:
    """Collect requirement/qualification-looking lines (de-duplicated, capped)."""
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        s = line.strip(" \t-•*●▪◦·")
        if not s or len(s) < 4:
            continue
        if not _REQ_HINT.search(s):
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:300])
        if len(out) >= 40:
            break
    return out


def _extract_keywords(text: str) -> list[str]:
    """Recurring capitalized terms / tech tokens, ranked by frequency.

    Matched per-line so a multi-word phrase never crosses a line break
    (e.g. a label at the end of one line and a word at the start of the next).
    """
    counts: dict[str, int] = {}
    for line in text.splitlines():
        for m in _KEYWORD_TOKEN.finditer(line):
            tok = m.group(1).strip()
            if len(tok) < 2:
                continue
            if tok.lower() in _KEYWORD_STOP:
                continue
            # Drop a multi-word phrase whose words are all stop-words anyway.
            words = tok.split()
            if all(w.lower() in _KEYWORD_STOP for w in words):
                continue
            counts[tok] = counts.get(tok, 0) + 1
    # Rank by frequency desc, then by first appearance (stable). Keep the top
    # terms — enough for ATS signal without flooding the prompt.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [tok for tok, _ in ranked[:30]]


def parse_jd(
    jd_text: str,
    *,
    title: str | None = None,
    company: str | None = None,
) -> dict[str, Any]:
    """Parse free JD text into a keyed JSON structure (see module docstring).

    Args:
        jd_text: the raw pasted job description.
        title: an explicit role title (e.g. from a saved_jobs row); overrides
            the heuristic guess when non-empty.
        company: an explicit employer name; overrides the heuristic guess.

    Raises:
        ValueError: if ``jd_text`` is empty/whitespace (or becomes empty after
            control-char sanitization) — a JD is required to ground the letter
            (callers surface this as a 400).
    """
    if not isinstance(jd_text, str) or not jd_text.strip():
        raise ValueError("job description text is empty")

    # Sanitize FIRST (Stage 10): strip injection-prone control / invisible-format
    # chars before anything reads the text, so the length cap counts visible
    # characters and every downstream consumer (parser + LLM prompts) sees clean
    # data. A JD that was nothing but control chars is now empty → treat it the
    # same as an empty paste (400, not a prompt of whitespace).
    sanitized = _strip_control_chars(jd_text)
    if not sanitized.strip():
        raise ValueError("job description text is empty")

    text = sanitized.strip()[:MAX_JD_CHARS]
    lines = [ln.rstrip() for ln in text.splitlines()]

    # Sanitize the caller-supplied title/company too — they come from saved_jobs
    # rows (imported/pasted data) and flow verbatim into the LLM prompt and the
    # grounding corpus, so they must pass the SAME control/invisible-char
    # boundary as jd_text (Stage 10); otherwise a bidi/zero-width smuggle in a
    # job's title/company would bypass the single chokepoint.
    resolved_title = _strip_control_chars(title or "").strip() or _guess_title(lines)
    resolved_company = _strip_control_chars(company or "").strip() or _guess_company(lines)

    return {
        "title": resolved_title,
        "company": resolved_company,
        "language": _detect_language(text),
        "requirements": _extract_requirements(lines),
        "keywords": _extract_keywords(text),
        "raw": text,
    }
