"""Post-generation guards for cover letters (Stage 5).

The grounded prompt makes fabrication unlikely; these guards make a shipped
fabrication *impossible*. They run AFTER the LLM returns and BEFORE the letter
is stored:

  1. :func:`enforce_word_cap` — a hard server-side word ceiling (~400 target,
     450 hard cap). This is the cover-letter equivalent of the CV's 1-page
     gate (but it is NOT a page count — the letter renderer is multi-page OK).

  2. :func:`cliche_scan` — flags the ~15 banned AI-cliché phrases ("passionate
     about", "results-driven", "I am writing to express", …) and strips them,
     returning both the cleaned text and the list of findings so the UI can
     say "we removed 2 clichés".

  3. :func:`cross_reference_facts` — the anti-fabrication backstop. Every
     proper noun and number in the generated letter MUST be grounded in the
     source CV (employers, dates, metrics). Any token not found in the CV
     fact corpus → the save is blocked and the offending items are returned
     (``GroundingError`` / ``{error: "unverified_claims", items: [...]}``),
     so a hallucinated employer or invented metric never reaches storage.

:func:`run_guards` chains them in the order the route uses and returns a
:class:`GuardResult` (cleaned text + cliché findings + word count). It raises
:class:`WordCapError` or :class:`GroundingError` on a blocking failure.

Everything here is pure, deterministic, and offline — no provider call, no
network — so it is cheap and fully unit-testable.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Target / hard word ceilings. The prompt asks for 250-400 words; we allow a
# little slack to the hard cap so a slightly-long-but-good letter isn't lost,
# but block runaway output.
WORD_TARGET = 400
WORD_HARD_CAP = 450


# ---------------------------------------------------------------------------
# Errors. Each maps to a route response (see server.py).
# ---------------------------------------------------------------------------
class WordCapError(ValueError):
    """Raised when a letter exceeds :data:`WORD_HARD_CAP` words."""

    def __init__(self, count: int) -> None:
        super().__init__(
            f"Cover letter is {count} words — over the {WORD_HARD_CAP}-word ceiling. "
            "Tighten it or regenerate."
        )
        self.count = count


class GroundingError(ValueError):
    """Raised when the letter contains claims not grounded in the CV.

    ``items`` is the list of offending proper nouns / numbers — surfaced to
    the user so they can see exactly which claims couldn't be verified.
    """

    def __init__(self, items: list[str]) -> None:
        super().__init__(
            "Couldn't verify these claims against your CV: " + ", ".join(items)
        )
        self.items = items


# ---------------------------------------------------------------------------
# Cliché scan.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClicheFinding:
    """One banned phrase found in the letter, with a rewrite hint."""

    phrase: str
    suggestion: str


# The reject phrases (dossier §1 rule C, plus the common offenders). Each maps
# to a short inline rewrite suggestion the UI can surface. Matching is
# case-insensitive and whitespace-tolerant.
_CLICHES: tuple[tuple[str, str], ...] = (
    ("I am writing to express my interest", "Open with a concrete, role-relevant fact."),
    ("I am writing to express my", "Open with a concrete, role-relevant fact."),
    ("I am excited to apply", "State a specific reason this role fits."),
    ("I believe I would be a great fit", "Show the fit with evidence instead of claiming it."),
    ("passionate about", "Replace with a concrete thing you did."),
    ("results-driven", "Cite a result instead of asserting you are driven by them."),
    ("proven track record", "Name the specific track record."),
    ("team player", "Give a concrete example of collaboration."),
    ("think outside the box", "Describe the actual creative solution."),
    ("hit the ground running", "Say what you'd do in week one, specifically."),
    ("wealth of experience", "State the experience plainly."),
    ("wear many hats", "List the actual responsibilities."),
    ("fast-paced environment", "Drop the boilerplate; describe the real context."),
    ("synergy", "Use a plain word — describe the actual benefit."),
    ("dynamic", "Replace with a specific, concrete descriptor."),
    ("self-starter", "Show initiative with an example."),
    ("go-getter", "Show initiative with an example."),
    ("detail-oriented", "Demonstrate the rigor with a concrete example."),
)


def _cliche_pattern(phrase: str) -> re.Pattern[str]:
    """Whitespace-tolerant, case-insensitive matcher for a cliché phrase."""
    parts = [re.escape(w) for w in phrase.split()]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


# Precompile once.
_CLICHE_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (_cliche_pattern(phrase), phrase, suggestion) for phrase, suggestion in _CLICHES
)


def cliche_scan(text: str, *, strip: bool = True) -> tuple[str, list[ClicheFinding]]:
    """Flag (and optionally strip) banned AI-cliché phrases.

    Returns ``(cleaned_text, findings)``. When ``strip`` is True each matched
    phrase is removed and any doubled spaces / dangling punctuation left
    behind are tidied, so the cleaned letter reads naturally. The findings
    list lets the UI report "we removed N clichés" with rewrite hints.
    """
    findings: list[ClicheFinding] = []
    cleaned = text
    for pattern, phrase, suggestion in _CLICHE_PATTERNS:
        if pattern.search(cleaned):
            findings.append(ClicheFinding(phrase=phrase, suggestion=suggestion))
            if strip:
                removal = _cliche_removal_pattern(pattern)
                cleaned = removal.sub(lambda m: _strip_match(m, cleaned), cleaned)
    if strip and findings:
        cleaned = _tidy_whitespace(cleaned)
    return cleaned, findings


def _cliche_removal_pattern(pattern: re.Pattern[str]) -> re.Pattern[str]:
    """Wrap a cliché matcher so a directly-preceding ``a``/``an`` is consumed too.

    Stripping a cliché ADJECTIVE can leave its indefinite article disagreeing
    with the word that now follows it ("a results-driven engineer" → "a
    engineer"). Consuming the article with the phrase lets :func:`_strip_match`
    re-emit it repaired ("an engineer").
    """
    return re.compile(
        r"(?:\b(?P<article>a|an)\s+)?(?:" + pattern.pattern + r")", re.IGNORECASE
    )


# Indefinite-article exceptions: words whose first LETTER misleads the naive
# vowel/consonant a/an rule (silent h → "an"; "yoo"/"wun" onsets → "a").
_AN_WORDS = re.compile(r"^(?:honest|hour|heir|honor)", re.IGNORECASE)
_A_WORDS = re.compile(r"^(?:uni|use|user|eu|one|once)", re.IGNORECASE)


def _strip_match(match: re.Match[str], text: str) -> str:
    """Replacement for one stripped cliché (repair a consumed a/an article).

    The repair must never make the text worse than leaving the author's
    article alone, so we only change it when the choice is certain:
    ``an`` before the well-known silent-h words, ``a`` before the
    "yoo"/"wun"-sounding exceptions, and a plain vowel/consonant default
    otherwise. When no word follows to judge against, the original article
    is re-emitted unchanged.
    """
    article = match.group("article")
    if not article:
        return ""
    nxt = re.match(r"\s*([A-Za-z]+)", text[match.end() :])
    if not nxt:
        return article + " "
    word = nxt.group(1)
    if _AN_WORDS.match(word):
        want_an = True
    elif _A_WORDS.match(word):
        want_an = False
    else:
        want_an = word[0].lower() in "aeiou"
    if want_an == (article.lower() == "an"):
        return article + " "
    fixed = "an" if want_an else "a"
    if article[0].isupper():
        fixed = fixed.capitalize()
    return fixed + " "


def _tidy_whitespace(text: str) -> str:
    """Collapse the gaps a strip leaves behind without reflowing paragraphs."""
    # Tidy each line so paragraph breaks (blank lines) survive.
    out_lines: list[str] = []
    for line in text.splitlines():
        s = re.sub(r"[ \t]{2,}", " ", line)
        s = re.sub(r"\s+([,.;:!?])", r"\1", s)  # space before punctuation
        s = re.sub(r"([,;:])\s*([,.;:])", r"\2", s)  # doubled punctuation
        s = re.sub(r"^[ \t]*[,.;:]+[ \t]*", "", s)  # leading orphan punctuation
        out_lines.append(s.strip())
    text = "\n".join(out_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Word cap.
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"\b[\w'-]+\b", re.UNICODE)

# CJK scripts don't delimit words with spaces, so a run of Han/Kana/Hangul
# codepoints is ONE ``\w+`` match above — a 10,000-character CJK letter counted
# as a single "word" and sailed under the cap. Count each CJK codepoint as one
# word instead (Unified Ideographs + Ext-A + Ext-B + Compat, Kana,
# Hangul syllables + Hangul Jamo).
_CJK_CHAR_RE = re.compile(
    "[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯ᄀ-ᇿ\U00020000-\U0002a6df]"
)


def word_count(text: str) -> int:
    """Count words (Unicode-aware) the way the cap is enforced."""
    text = text or ""
    cjk = len(_CJK_CHAR_RE.findall(text))
    latin = len(_WORD_RE.findall(_CJK_CHAR_RE.sub(" ", text)))
    return latin + cjk


def enforce_word_cap(text: str, *, hard_cap: int = WORD_HARD_CAP) -> int:
    """Raise :class:`WordCapError` if ``text`` exceeds ``hard_cap`` words.

    Returns the word count on success. We reject (rather than silently
    truncate) so a runaway letter surfaces clearly to the user instead of
    shipping a mid-sentence cut.
    """
    count = word_count(text)
    if count > hard_cap:
        raise WordCapError(count)
    return count


# ---------------------------------------------------------------------------
# Normalization (anti-evasion). The grounding check is only as strong as the
# matching: an attacker can dodge a naive comparison with lowercase nouns,
# Unicode homoglyphs (Cyrillic/Greek lookalikes), or spelled-out numbers. We
# fold all of these out of BOTH the generated text and the CV/JD corpus before
# comparing so the same fabrication is caught however it is written.
# ---------------------------------------------------------------------------

# Common confusable characters → their Latin lookalike. Sourced from the
# Unicode confusables data (the high-frequency Cyrillic/Greek/fullwidth set an
# attacker actually reaches for). NFKC handles fullwidth/ligatures; this table
# handles the cross-script homoglyphs NFKC leaves alone (e.g. Cyrillic "а"
# U+0430 vs Latin "a" U+0061 are distinct codepoints NFKC keeps separate).
_HOMOGLYPHS: dict[str, str] = {
    # Cyrillic → Latin
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    "к": "k", "м": "m", "т": "t", "н": "h", "в": "b", "і": "i", "ј": "j",
    "ѕ": "s", "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "Х": "X",
    "У": "Y", "К": "K", "М": "M", "Т": "T", "Н": "H", "В": "B", "І": "I",
    "Ј": "J", "Ѕ": "S", "Г": "F", "г": "r",
    # Greek → Latin
    "ο": "o", "Ο": "O", "α": "a", "Α": "A", "ε": "e", "Ε": "E", "ρ": "p",
    "Ρ": "P", "τ": "t", "Τ": "T", "υ": "u", "Υ": "Y", "χ": "x", "Χ": "X",
    "ι": "i", "Ι": "I", "κ": "k", "Κ": "K", "ν": "v", "Ν": "N", "μ": "u",
    "Μ": "M", "Β": "B", "Η": "H", "Ζ": "Z", "Ξ": "E",
}
_HOMOGLYPH_TABLE = {ord(k): v for k, v in _HOMOGLYPHS.items()}


def _fold(s: str) -> str:
    """NFKC + homoglyph-fold + casefold a string for evasion-resistant match."""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_HOMOGLYPH_TABLE)
    return s.casefold()


def _norm_token(raw: str) -> str:
    """Fold a single token and strip surrounding punctuation for matching.

    Used on BOTH corpus tokens and candidate words so "Stack." in the CV
    grounds "stack" in the letter, and a homoglyph "Glοbex" folds to "globex".
    """
    return _fold(raw).strip(".,;:!?()[]{}'\"`«»…-/")


# Spelled-out number words → value, so "two hundred percent" / "fifty" are
# checked just like "200%" / "50". A small deterministic parser (no NLP dep).
_NUM_WORDS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUM_SCALES: dict[str, int] = {"hundred": 100, "thousand": 1000,
                               "million": 1_000_000, "billion": 1_000_000_000}


def _word_alt(words: list[str]) -> str:
    """A regex alternation that matches whole words only, longest-first.

    Two anti-prefix protections: (a) the alternatives are sorted LONGEST-FIRST
    so ``ninety`` is tried before ``nine`` (Python ``re`` is leftmost-eager, so
    without this it would match the shorter ``nine`` prefix of ``ninety``);
    (b) each alternative is wrapped in ``\\b`` so a number word can never match
    a PREFIX of a longer word (``nine`` must not match inside ``ninety`` /
    ``ninefold``). Together these fix the spelled-number prefix bug where
    "ninety percent" parsed as 9 with the "percent" unit left unconsumed.
    """
    ordered = sorted({w for w in words}, key=len, reverse=True)
    return "|".join(rf"\b{re.escape(w)}\b" for w in ordered)


_NUM_TOKEN_ALT = _word_alt(list(_NUM_WORDS) + list(_NUM_SCALES) + ["and"])
_NUM_WORD_RE = re.compile(r"(?:" + _NUM_TOKEN_ALT + r")", re.IGNORECASE)
# A spelled number optionally followed by a unit word ("percent", "x", …).
# Each number word is \b-anchored (see _word_alt) so a longer word ("ninety")
# is never split into a shorter prefix ("nine"); the unit is then re-checked
# against the FULL match so "ninety percent" → 90% (not a bare, discarded 9).
_SPELLED_NUMBER_RE = re.compile(
    r"(?P<words>(?:" + _NUM_TOKEN_ALT + r")"
    + r"(?:[\s-]+(?:" + _NUM_TOKEN_ALT + r"))*)"
    + r"\s*(?P<unit>percent|%|x|k|m|b)?",
    re.IGNORECASE,
)


def _value_of_number_words(words: str) -> int | None:
    """Parse a run of number words into an integer (e.g. "two hundred" → 200).

    Handles the common ``units/tens (scale)*`` grammar enough to catch metric
    fabrications. Returns ``None`` if nothing parseable is present.
    """
    tokens = [t for t in re.split(r"[\s-]+", words.lower()) if t and t != "and"]
    if not tokens:
        return None
    total = 0
    current = 0
    saw = False
    for tok in tokens:
        if tok in _NUM_WORDS:
            current += _NUM_WORDS[tok]
            saw = True
        elif tok in _NUM_SCALES:
            scale = _NUM_SCALES[tok]
            current = (current or 1) * scale
            if scale >= 1000:
                total += current
                current = 0
            saw = True
        else:
            return None
    if not saw:
        return None
    return total + current


def _spelled_numbers(text: str) -> list[tuple[str, str]]:
    """Yield ``(normalized_numeric, raw_phrase)`` for spelled numbers in text.

    The normalized form mirrors :func:`_normalize_number` ("200%", "50") so a
    spelled number can be compared against the digit-based corpus set.
    """
    out: list[tuple[str, str]] = []
    for m in _SPELLED_NUMBER_RE.finditer(text):
        words = m.group("words")
        value = _value_of_number_words(words)
        if value is None:
            continue
        unit = (m.group("unit") or "").lower()
        if unit == "percent":
            unit = "%"
        norm = f"{value}{unit}"
        raw = m.group(0).strip()
        out.append((norm, raw))
    return out


# ---------------------------------------------------------------------------
# Anti-fabrication cross-reference.
# ---------------------------------------------------------------------------
# A number token: digit runs, optionally with separators / a percent / unit
# letters (e.g. "40%", "1.2M", "3,000", "10x", "2024"). We normalize before
# comparing so "40%" in the letter matches "40 %" or "40 percent" in the CV.
_NUMBER_RE = re.compile(r"\d[\d.,]*\s?(?:%|percent|k|m|b|x|\+)?", re.IGNORECASE)

# A proper-noun token: a capitalized word (incl. internal caps/digits like
# "PyTorch", "GPT-4", "AWS"), allowing dotted/hyphenated forms. We then drop
# sentence-initial common words via a stop-list.
_PROPER_RE = re.compile(r"\b[A-Z][\w&.+/-]*(?:\s+[A-Z][\w&.+/-]*)*\b")

# Common words that are capitalized only because they start a sentence or are
# generic — NOT proper nouns we need to ground. Lower-cased for comparison.
_PROPER_STOP = frozenset(
    {
        "i", "i'm", "i've", "i'd", "i'll", "the", "a", "an", "and", "or", "but",
        "as", "at", "by", "for", "from", "in", "into", "of", "on", "to", "with",
        "my", "your", "our", "their", "his", "her", "its", "this", "that",
        "these", "those", "we", "you", "they", "he", "she", "it", "is", "are",
        "was", "were", "be", "been", "being", "have", "has", "had", "do", "does",
        "did", "will", "would", "can", "could", "should", "may", "might", "must",
        "dear", "sincerely", "regards", "best", "thank", "thanks", "hello", "hi",
        "during", "while", "when", "where", "which", "who", "what", "why", "how",
        "after", "before", "since", "until", "because", "although", "though",
        "however", "therefore", "moreover", "additionally", "furthermore",
        "team", "role", "company", "position", "experience", "skills", "work",
        "project", "projects", "years", "year", "months", "month",
        # Common sentence-initial adverbs / connectives (capitalized only
        # because they open a sentence — not proper nouns).
        "earlier", "later", "today", "currently", "recently", "previously",
        "finally", "first", "second", "third", "next", "now", "then", "here",
        "there", "together", "throughout", "across", "beyond", "within",
        "indeed", "specifically", "notably", "particularly", "meanwhile",
        # German salutation / connective leads.
        "sehr", "geehrte", "geehrter", "mit", "freundlichen", "grüßen",
        "ich", "wir", "sie", "und", "oder", "als", "bei", "für", "von", "der",
        "die", "das", "den", "dem", "ein", "eine", "einen", "meine", "mein",
    }
)


def _normalize_number(token: str) -> str:
    """Canonicalize a number token for comparison.

    Strips spaces, lower-cases unit letters, maps "percent" → "%", drops
    grouping commas so "3,000" == "3000". Keeps the leading digits + a unit.
    NFKC-folds first so fullwidth digits (e.g. "４０") normalize to ASCII.
    """
    t = unicodedata.normalize("NFKC", token).strip().lower().replace(" ", "")
    t = t.replace("percent", "%")
    # Remove grouping commas/dots used as thousands separators but keep a
    # decimal point: simplest robust rule — drop commas entirely, keep dots.
    t = t.replace(",", "")
    return t


def _cv_fact_corpus(cv: dict) -> str:
    """Flatten every string value in the CV into one searchable text blob.

    Recurses dicts/lists so employers, titles, dates, bullets, metrics,
    skills, project descriptions — every fact the candidate actually wrote —
    end up in the corpus. Numbers and proper nouns in the letter are checked
    against this. Keys are ignored; only values count (a key named "company"
    is not evidence — its value is).
    """
    parts: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            parts.append(node)
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


# The label/value separator is a colon (tight or spaced: "Title:", "Title :")
# OR a hyphen/dash ONLY when whitespace-delimited ("Company - Acme"). A bare
# hyphen inside a compound word must NOT act as a separator, or a JD REQUIREMENT
# line ("Role-based access control with Kubernetes…", "Company-wide impact: 40%")
# would masquerade as a "Role"/"Company" label and feed its skill/number tokens
# into the grounding corpus — re-opening the plan-109 hole (a JD requirement can
# never ground a candidate claim; only a labelled title/company/employer can).
_JD_LABEL_SEP = r"(?:\s*:\s*|\s+[-–—]\s+)"
_JD_LABEL_RE = re.compile(
    r"^\s*(?:job\s+title|title|role|position|stelle|position|company|employer|"
    r"organi[sz]ation|firma|unternehmen|arbeitgeber)" + _JD_LABEL_SEP + r"(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _jd_application_context_corpus(jd_context: object) -> str:
    """Return only employer/role context from a JD, never candidate requirements.

    The JD can ground the employer and role the candidate is applying to. It
    cannot ground candidate skills, years, metrics, employers, or
    responsibilities; those must trace to the CV only.
    """
    parts: list[str] = []
    if isinstance(jd_context, dict):
        for key in ("title", "role", "position", "company", "employer", "organization", "recipient"):
            value = jd_context.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        # Also mine the ORIGINAL JD text for LABELED employer/role values. When
        # a saved job supplies short display metadata (e.g. company "Co", title
        # "Seeded") it overrides the structured field via parse_jd's kwargs, so
        # a richer "Company: Acme GmbH" / "Title: Senior Data Engineer" in the
        # posting would otherwise be lost and a letter naming it wrongly blocked.
        # Only explicitly LABELED values are taken (via _JD_LABEL_RE), never
        # requirement/skill lines — so this can never ground a candidate claim
        # (preserves the plan-109 boundary: the JD grounds only who/what you
        # apply to, never your own skills/years/metrics).
        raw = jd_context.get("raw") or jd_context.get("jd_text")
        if isinstance(raw, str):
            for match in _JD_LABEL_RE.finditer(raw):
                value = match.group(1).strip()
                # Defense-in-depth on top of the strict separator: a genuine
                # title/company is short; a requirement sentence that somehow
                # matched is long, so cap the mined value so it can never smuggle
                # a run of skill/number tokens into the corpus.
                if value and len(value) <= 80:
                    parts.append(value)
    elif isinstance(jd_context, str):
        for match in _JD_LABEL_RE.finditer(jd_context):
            value = match.group(1).strip()
            if value:
                parts.append(value)
    return "\n".join(parts)


def _normalized_corpus_tokens(corpus: str) -> set[str]:
    """Folded word tokens of the CV corpus, for proper-noun membership.

    Each token is NFKC + homoglyph-folded + casefolded and stripped of
    surrounding punctuation, so "Stack." grounds "stack" and a homoglyph in
    the letter can never sneak past a token that is plainly present in the CV.
    """
    out: set[str] = set()
    for w in re.findall(r"[\w&.+/-]+", corpus):
        n = _norm_token(w)
        if n:
            out.add(n)
    return out


def _normalized_corpus_numbers(corpus: str) -> set[str]:
    """Normalized number tokens present anywhere in the CV corpus.

    Includes both digit-based numbers and any spelled-out numbers, so a CV
    that writes "fifty" grounds a letter that writes "50" and vice versa.
    """
    nums = {_normalize_number(m.group(0)) for m in _NUMBER_RE.finditer(corpus)}
    nums |= {norm for norm, _raw in _spelled_numbers(corpus)}
    return nums


def _candidate_proper_nouns(text: str) -> list[str]:
    """Proper-noun phrases in the letter, minus generic/sentence-initial words.

    A multi-word capitalized phrase (e.g. "Globex Industries") is also split
    into its component words so a partially-grounded phrase still surfaces the
    ungrounded part. The capitalization-driven match catches the normal case
    AND a homoglyph-disguised capitalized fabrication ("Glοbex"); the lowercase
    bypass is handled separately by :func:`_lowercase_fabrication_runs`.
    """
    found: list[str] = []
    seen: set[str] = set()
    for m in _PROPER_RE.finditer(text):
        phrase = m.group(0).strip()
        for word in phrase.split():
            w = word.strip(".,;:!?()[]\"'")
            if not w:
                continue
            key = _norm_token(w)  # NFKC + homoglyph + casefold for matching
            if not key or key in _PROPER_STOP:
                continue
            if not re.search(r"[^\W\d_]", w, re.UNICODE):  # has a letter
                continue
            if key in seen:
                continue
            seen.add(key)
            found.append(w)
    return found


# A "word-like" token used by the lowercase-fabrication scan: letters (any
# script) plus internal marks/digits/hyphens, so "globex" and "GPT-4" count.
_WORDLIKE_RE = re.compile(r"[^\W\d_][\w&./'-]*", re.UNICODE)
# Boundaries that end a run (sentence/clause punctuation, newlines).
_RUN_BREAK_RE = re.compile(r"[.,;:!?()\[\]{}\"“”«»\n\r]|\s—\s|\s-\s")

# Common English prose words that legitimately appear in cover letters. Used
# ONLY to break lowercase-fabrication runs so honest bigrams like "depth too"
# or "revenue increase" aren't mistaken for a hidden entity. This is a
# false-positive suppressor — never put a plausible ORG-name token here
# (e.g. "industries", "solutions", "technologies", "labs"); those must stay
# flaggable. (Capitalized proper nouns are handled by a separate path and are
# unaffected by this list.)
_COMMON_PROSE = frozenset(
    {
        "bring", "bringing", "brought", "drove", "drive", "driving", "driven",
        "led", "lead", "leading", "built", "build", "building", "shipped",
        "ship", "shipping", "delivered", "deliver", "delivering", "grew",
        "grow", "growing", "growth", "improved", "improve", "improving",
        "increase", "increased", "increasing", "revenue", "results", "result",
        "record", "records", "depth", "welcome", "chance", "exact", "using",
        "use", "used", "thrilled", "excited", "keen", "eager", "able", "help",
        "helped", "helping", "make", "made", "making", "took", "take", "taking",
        "gave", "give", "giving", "went", "going", "gone", "came", "come",
        "coming", "saw", "see", "seen", "seeing", "known", "knew", "know",
        "knowing", "want", "wanted", "need", "needed", "needs", "look",
        "looking", "find", "found", "finding", "work", "worked", "working",
        "role", "roles", "team", "teams", "company", "companies", "value",
        "values", "scale", "scaling", "scaled", "impact", "impactful",
        "great", "strong", "solid", "deep", "broad", "wide", "fast", "quick",
        "real", "true", "good", "best", "better", "more", "most", "much",
        "many", "some", "such", "very", "well", "also", "too", "both", "each",
        "every", "across", "around", "along", "among", "within", "without",
        "behind", "beyond", "above", "below", "under", "over", "between",
        "gain", "gains", "gained", "boost", "boosted", "cut", "cutting",
        "reduce", "reduced", "reducing", "raise", "raised", "raising",
        "owned", "own", "owning", "managed", "manage", "managing", "ran",
        "run", "running", "drove", "track", "tracking", "tracked", "record",
        "year", "years", "month", "months", "day", "days", "week", "weeks",
        "time", "times", "data", "model", "models", "system", "systems",
        "tool", "tools", "code", "stack", "pipeline", "pipelines", "platform",
        "platforms", "product", "products", "service", "services", "feature",
        "features", "user", "users", "customer", "customers", "client",
        "clients", "market", "markets", "business", "process", "processes",
        "would", "could", "should", "will", "shall", "may", "might", "must",
        "engineer", "engineers", "engineering", "scientist", "scientists",
        "developer", "developers", "analyst", "analysts", "manager",
        "managers", "designer", "designers", "lead", "leads", "senior",
        "junior", "staff", "principal", "director", "officer",
    }
)


def _lowercase_fabrication_runs(text: str, corpus_words: set[str]) -> list[str]:
    """Catch a fabricated entity written lowercase (e.g. "globex industries").

    Lowercasing defeats the capitalization-based proper-noun extractor, so an
    attacker can hide a hallucinated employer by not capitalizing it. We catch
    it via a conservative signal that does NOT flag honest prose: a run of
    **two or more consecutive** word-like tokens where EVERY token is

      * not a stop / generic word, and
      * not grounded in the CV/JD corpus (after the same fold), and
      * at least 3 chars (skip stray short function words).

    A grounded word anywhere in the run breaks it, so ordinary phrases like
    "data engineer" or "ETL stack" (whose words ARE in the CV) never trip it,
    while a wholly-foreign multi-word name surfaces as an offender. Single
    ungrounded words are intentionally NOT flagged here (too noisy — that path
    is reserved for capitalized proper nouns).
    """
    offenders: list[str] = []
    seen: set[str] = set()
    # Split the text into segments at clause boundaries; scan each segment.
    for segment in _RUN_BREAK_RE.split(text):
        run: list[str] = []
        for m in _WORDLIKE_RE.finditer(segment):
            tok = m.group(0)
            key = _norm_token(tok)
            grounded = key in corpus_words
            # Number-words / units are handled by the numeric grounding path,
            # not here — treat them as run-breakers so "forty percent" (which
            # may be grounded as "40%") is never flagged as a lexical entity.
            is_number_word = key in _NUM_WORDS or key in _NUM_SCALES or key in {
                "percent", "and",
            }
            generic = (
                key in _PROPER_STOP
                or key in _COMMON_PROSE
                or len(key) < 3
                or is_number_word
            )
            if grounded or generic:
                _flush_run(run, offenders, seen)
                run = []
            else:
                run.append(tok)
        _flush_run(run, offenders, seen)
    return offenders


def _flush_run(run: list[str], offenders: list[str], seen: set[str]) -> None:
    """Emit a >=2-token ungrounded run as one offender phrase (deduped)."""
    if len(run) < 2:
        return
    phrase = " ".join(run)
    key = _fold(phrase)
    if key not in seen:
        seen.add(key)
        offenders.append(phrase)


# A comma-separated SKILL/STACK entry, e.g. "Python, Spark, Airflow,
# kubernetes". The lowercase-run scan (above) treats commas as run-breakers,
# so a LONE lowercase ungrounded skill in such a list ("…, kubernetes") is
# never a >=2-token run and slips through; the capitalized proper-noun path
# also misses it (it is lowercase). This pass closes that hole by checking each
# comma token in a skill-list line INDIVIDUALLY, regardless of run length.
#
# A "skill-list line" is a comma-delimited line whose segments are all SHORT,
# skill-like tokens (1-2 words, no sentence punctuation) — so ordinary prose
# that merely contains a comma ("models, pipelines, and dashboards I built")
# is NOT treated as a skill list and never trips this stricter, single-token
# check.
_SKILL_TOKEN_RE = re.compile(r"^[\w&.+#/'-]+(?:\s+[\w&.+#/'-]+){0,2}$", re.UNICODE)


def _is_skill_list_line(line: str) -> bool:
    """True iff ``line`` looks like a comma-separated skill/stack list.

    Requires >=2 non-empty comma segments where EVERY segment is a short
    skill-like token (matches :data:`_SKILL_TOKEN_RE`). This is deliberately
    narrow so it only fires on real skill rows ("Python, Spark, Airflow"),
    never on prose sentences that happen to use commas.
    """
    segments = [seg.strip() for seg in line.split(",")]
    segments = [seg for seg in segments if seg]
    if len(segments) < 2:
        return False
    return all(_SKILL_TOKEN_RE.match(seg) for seg in segments)


def _comma_list_skill_fabrications(text: str, corpus_words: set[str]) -> list[str]:
    """Flag a SINGLE ungrounded skill token inside a comma-separated list.

    For each line that :func:`_is_skill_list_line` recognizes as a skill/stack
    row, every comma token is normalized (NFKC + homoglyph + casefold, same as
    the corpus) and reported individually if it is ungrounded and not a generic
    / stop / number word. Unlike :func:`_lowercase_fabrication_runs` this fires
    on a lone token (run length 1) — that is exactly the evasion it closes
    ("Python, Spark, Airflow, kubernetes" where only "kubernetes" is foreign).
    """
    offenders: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if not _is_skill_list_line(line):
            continue
        for seg in line.split(","):
            tok = seg.strip()
            if not tok:
                continue
            key = _norm_token(tok)
            if not key or key in seen:
                continue
            grounded = key in corpus_words
            is_number_word = (
                key in _NUM_WORDS or key in _NUM_SCALES or key in {"percent", "and"}
            )
            generic = (
                key in _PROPER_STOP
                or key in _COMMON_PROSE
                or len(key) < 2
                or is_number_word
            )
            if grounded or generic:
                continue
            # A multi-word skill ("data engineering") whose words are all
            # grounded individually is not a fabrication — only flag when the
            # token as a whole is ungrounded AND its words aren't all grounded.
            words = [w for w in re.split(r"\s+", tok) if w]
            if len(words) > 1 and all(
                _norm_token(w) in corpus_words for w in words if _norm_token(w)
            ):
                continue
            seen.add(key)
            offenders.append(tok)
    return offenders


# Prepositions that introduce an EMPLOYER / ORG in a cover letter ("at Datadog",
# "for Stripe", "with Acme", "@stripe"). A lone ungrounded token right after one
# of these is a likely fabricated employer even when written lowercase (which
# defeats the capitalization-based proper-noun path). Lower-cased for matching.
_ORG_PREPOSITIONS = frozenset({"at", "for", "with", "@"})

# Common prepositional-OBJECT words that legitimately follow at/for/with in
# ordinary prose and must NOT be mistaken for a fabricated org. This is a
# FALSE-POSITIVE suppressor for the lone-org-after-preposition check ONLY; it
# is deliberately small and conservative. Never add a plausible ORG/brand name
# here. Capitalized proper nouns are handled by a separate path and unaffected.
_PREP_OBJECT_COMMON = frozenset(
    {
        "example", "examples", "instance", "instances", "least", "most",
        "once", "twice", "hand", "large", "ease", "care", "scratch", "speed",
        "scope", "length", "depth", "breadth", "rigor", "rigour", "pride",
        "passion", "confidence", "detail", "details", "focus", "intent",
        "purpose", "reason", "reasons", "course", "regard", "regards",
        "respect", "sure", "free", "good", "home", "work", "scale", "present",
        "future", "moment", "granted", "stake", "play", "hire", "hiring",
        "years", "year", "months", "month", "days", "day", "weeks", "week",
        "time", "times", "now", "good", "best", "real", "true", "myself",
        "yourself", "yourselves", "ourselves", "themselves", "everyone",
        "anyone", "someone", "people", "clients", "customers", "users",
        "teams", "companies", "businesses", "results", "impact", "growth",
        "revenue", "efficiency", "quality", "speed", "accuracy", "reliability",
        "consistency", "clarity", "precision", "scale", "context", "feedback",
    }
)


def _lone_org_after_preposition(text: str, corpus_words: set[str]) -> list[str]:
    """Flag a lone ungrounded token directly after an employer/org preposition.

    Residual closed: a single ALL-LOWERCASE ungrounded employer ("at datadog",
    "for terraform") slips past BOTH the capitalization-based proper-noun path
    (it's lowercase) and the >=2-token lowercase-run scan (it's a lone token).
    This narrow check flags a token that:

      (a) immediately follows ``at`` / ``for`` / ``with`` / ``@`` (case-insens.),
      (b) is >= 4 letters,
      (c) is NOT a stop / common-prose / common-prepositional-object word, and
      (d) is ungrounded in the CV/JD corpus (same fold as everywhere).

    This catches a lowercase fabricated employer in the exact position a letter
    names one, WITHOUT flagging ordinary lowercase tech terms elsewhere (only
    the post-preposition slot is checked) or grounded mentions ("at Meta",
    "with Python" — both grounded, so (d) fails). Deliberately conservative: a
    grounded OR common word in the slot is never flagged. A capitalized org in
    this slot is already caught by the proper-noun path and de-duped by the
    shared ``seen`` set in :func:`cross_reference_facts`.
    """
    offenders: list[str] = []
    seen: set[str] = set()
    # Tokenize into (lowercased-for-prep-test, original) pairs. Treat a bare "@"
    # glued to a handle ("@stripe") as preposition+token by splitting it out.
    tokens: list[str] = []
    for m in re.finditer(r"@|[^\W_][\w&.+/'-]*", text, re.UNICODE):
        tok = m.group(0)
        tokens.append(tok)
    for i in range(len(tokens) - 1):
        prev = tokens[i].lower().strip(".,;:!?()[]{}'\"`")
        if prev not in _ORG_PREPOSITIONS:
            continue
        cand = tokens[i + 1]
        key = _norm_token(cand)
        if not key or key in seen:
            continue
        # (b) >= 4 LETTERS (count letters, not total chars — "GPT-4" has 3).
        letter_count = sum(1 for ch in key if ch.isalpha())
        if letter_count < 4:
            continue
        # (c) not a generic / stop / prose / prepositional-object / number word.
        is_number_word = key in _NUM_WORDS or key in _NUM_SCALES or key in {
            "percent", "and",
        }
        if (
            key in _PROPER_STOP
            or key in _COMMON_PROSE
            or key in _PREP_OBJECT_COMMON
            or is_number_word
        ):
            continue
        # (d) ungrounded in the corpus.
        if key in corpus_words:
            continue
        seen.add(key)
        offenders.append(cand)
    return offenders


def cross_reference_facts(
    text: str, cv: dict, *, jd_context: object = None
) -> list[str]:
    """Return the proper nouns / numbers in ``text`` NOT grounded in CV/context.

    An empty list means every checkable claim traces to the candidate's CV or
    to the narrow application context from the job description. A non-empty list is the set of offending items the
    route uses to BLOCK the save (``unverified_claims``). Comparison is
    case-insensitive for proper nouns and normalized for numbers (40% ==
    "40 percent" == "40 %").

    ``jd_context`` is deliberately reduced to employer/role labels. The full JD
    requirements are never grounding for candidate facts, so a generated letter
    cannot claim a skill or years of experience merely because the posting asked
    for it.

    Conservative on the grounding side (a token is accepted if any CV/context
    word matches) so honest letters aren't blocked, but strict that a token
    appearing NOWHERE is reported — that's the hallucination signal.
    """
    corpus = _cv_fact_corpus(cv)
    if jd_context is not None:
        corpus = corpus + "\n" + _jd_application_context_corpus(jd_context)
    corpus_words = _normalized_corpus_tokens(corpus)
    corpus_numbers = _normalized_corpus_numbers(corpus)
    corpus_digit_runs = {re.sub(r"[^\d]", "", c) for c in corpus_numbers}

    offenders: list[str] = []
    seen: set[str] = set()

    # 1) Proper nouns (capitalized — incl. homoglyph-disguised, via the fold).
    for noun in _candidate_proper_nouns(text):
        key = _norm_token(noun)
        if key and key not in corpus_words and key not in seen:
            offenders.append(noun)
            seen.add(key)

    # 1b) Lowercase fabrications the capitalization scan misses ("globex
    #     industries"). Conservative: only wholly-ungrounded multi-word runs.
    for phrase in _lowercase_fabrication_runs(text, corpus_words):
        key = _fold(phrase)
        if key not in seen:
            offenders.append(phrase)
            seen.add(key)

    # 1c) A SINGLE ungrounded skill token in a comma-separated skill/stack list
    #     ("Python, Spark, Airflow, kubernetes"). The run scan breaks on commas
    #     so a lone lowercase fabrication there is never a run; this checks each
    #     comma token individually in recognized skill-list lines.
    for tok in _comma_list_skill_fabrications(text, corpus_words):
        key = _norm_token(tok)
        if key and key not in seen:
            offenders.append(tok)
            seen.add(key)

    # 1d) A LONE lowercase ungrounded token in the employer slot right after
    #     at/for/with/@ ("at datadog", "for terraform"). Closes the residual
    #     where a single lowercase fabricated employer dodges both the
    #     capitalization path and the >=2-token lowercase-run scan. Narrow +
    #     low-false-positive (only the post-preposition slot; grounded/common
    #     words excluded). See _lone_org_after_preposition for the criteria.
    for tok in _lone_org_after_preposition(text, corpus_words):
        key = _norm_token(tok)
        if key and key not in seen:
            offenders.append(tok)
            seen.add(key)

    # 2) Numbers (skip trivial small integers that are usually structural, not
    #    candidate metrics — e.g. "one role", "3 paragraphs". A bare 1-digit
    #    integer with no unit is too noisy to ground reliably and is rarely a
    #    fabricated *credential*; anything with a unit or 2+ digits is checked).
    def _check_number(raw: str, norm: str) -> None:
        if not norm:
            return
        digits_only = re.sub(r"[^\d]", "", norm)
        has_unit = bool(re.search(r"[%kxmb+]", norm))
        if len(digits_only) < 2 and not has_unit:
            return  # trivial small integer, not a checkable credential
        if norm in corpus_numbers:
            return
        # Also accept if the digit-run alone appears (e.g. letter "40%" vs CV "40").
        if digits_only and digits_only in corpus_digit_runs:
            return
        key = f"#{norm}"
        if key not in seen:
            offenders.append(raw)
            seen.add(key)

    for m in _NUMBER_RE.finditer(text):
        raw = m.group(0).strip()
        _check_number(raw, _normalize_number(raw))

    # 2b) Spelled-out numbers ("two hundred percent", "fifty") — normalized to
    #     the same numeric form and checked against the corpus.
    for norm, raw in _spelled_numbers(text):
        _check_number(raw, norm)

    return offenders


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-(?:ant|proj|live|test)?[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\b(?:li_at|session_token|JSESSIONID)\s*[:=]\s*[^,\s;]{8,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{12,}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def unsafe_generated_content(text: str) -> list[str]:
    """Return credential-shaped snippets that must never be persisted."""
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text or ""):
            raw = match.group(0)
            redacted = raw[:12] + "..." if len(raw) > 15 else raw
            key = _fold(redacted)
            if key not in seen:
                seen.add(key)
                found.append(redacted)
    return found


# ---------------------------------------------------------------------------
# The orchestrated guard chain.
# ---------------------------------------------------------------------------
@dataclass
class GuardResult:
    """Outcome of a successful guard run (no blocking failure)."""

    text: str
    cliches_removed: list[ClicheFinding] = field(default_factory=list)
    word_count: int = 0


def run_guards(
    raw_letter: str,
    cv: dict,
    *,
    jd_context: object = None,
    strip_cliches: bool = True,
) -> GuardResult:
    """Run the full post-generation guard chain on a generated letter.

    Order:
      1. Cliché scan (+ strip): clean obvious AI filler first.
      2. Anti-fabrication cross-reference: BLOCK if any proper noun / number
         in the cleaned letter is grounded in NEITHER the CV nor the JD.
      3. Word cap: BLOCK if the cleaned letter exceeds the hard ceiling.

    ``jd_context`` (the parsed JD dict / raw text) lets the candidate name the
    employer and role they're applying to without tripping the guard — only a
    token grounded in neither source is a fabrication. See
    :func:`cross_reference_facts`.

    Returns a :class:`GuardResult` (cleaned text + cliché findings + word
    count) on success. Raises :class:`GroundingError` or :class:`WordCapError`
    on a blocking failure — the route maps these to ``unverified_claims`` /
    word-cap responses and does NOT store the letter.
    """
    cleaned, cliches = cliche_scan(raw_letter or "", strip=strip_cliches)

    unsafe = unsafe_generated_content(cleaned)
    if unsafe:
        raise GroundingError(unsafe)

    offenders = cross_reference_facts(cleaned, cv, jd_context=jd_context)
    if offenders:
        raise GroundingError(offenders)

    count = enforce_word_cap(cleaned)

    return GuardResult(text=cleaned.strip(), cliches_removed=cliches, word_count=count)
