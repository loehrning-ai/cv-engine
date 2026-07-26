"""Tiny inline-markdown processor for CV bullet text.

Handles, in order: HTML escape, ``[label](url)`` links, backtick code,
``**bold**``, ``*italic*``. No lists; bullets are already lists in YAML.

Security: the link rule WHITELISTS the URL scheme. Only ``http://``,
``https://``, ``mailto:`` and scheme-less relative/anchor URLs are turned
into live ``<a>`` tags; anything else (``javascript:``, ``data:``,
``vbscript:``, ``file:``, …) is rendered as the escaped label text with no
anchor, so a malicious ``[x](javascript:alert(1))`` can never become a live
XSS sink in the served preview / CV HTML.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

# Schemes we allow to become a live <a href>. Everything else (javascript,
# data, vbscript, file, blob, about, …) is dropped to plain text.
_ALLOWED_SCHEMES = frozenset({"http", "https", "mailto"})

# Matches a URL scheme prefix: ``scheme:`` at the very start (per RFC 3986 a
# scheme is ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )). We use this only to
# decide whether a scheme is present and, if so, whether it is allow-listed.
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")

# Numeric HTML entities (decimal ``&#106;`` or hex ``&#x6a;``) used to obfuscate
# a scheme like ``j&#x61;vascript:``. We decode these during normalization.
_NUMERIC_ENTITY_RE = re.compile(r"&#(x[0-9a-fA-F]+|[0-9]+);?")


def _decode_numeric_entities(s: str) -> str:
    """Decode decimal/hex numeric HTML entities (obfuscation vector)."""

    def _sub(m: re.Match[str]) -> str:
        body = m.group(1)
        try:
            code = int(body[1:], 16) if body[0] in "xX" else int(body)
        except ValueError:  # pragma: no cover - regex guarantees digits
            return m.group(0)
        if 0 <= code <= 0x10FFFF:
            return chr(code)
        return m.group(0)

    return _NUMERIC_ENTITY_RE.sub(_sub, s)


def _percent_decode_until_stable(s: str, *, max_passes: int = 5) -> str:
    """Repeatedly ``urllib.parse.unquote`` until the string stops changing.

    A single decode neutralizes ``%6a`` → ``j`` (so ``%6aavascript:`` becomes
    ``javascript:``); the loop additionally catches DOUBLE-encoding
    (``%256a`` → ``%6a`` → ``j``), which a browser will likewise collapse if
    the value is ever re-fed through a decode. Bounded passes guard against a
    pathological input; in practice 2 passes are enough.
    """
    for _ in range(max_passes):
        decoded = unquote(s)
        if decoded == s:
            return s
        s = decoded
    return s


def _strip_format_chars(s: str) -> str:
    """Remove Unicode format/control chars browsers ignore inside a scheme.

    Covers the C0/C1 control ranges (0x00-0x1F, 0x7F-0x9F) AND every Unicode
    "format" char (category ``Cf`` — soft hyphen U+00AD, zero-width
    space/joiner U+200B-U+200D, BOM U+FEFF, bidi controls, …). These are the
    invisible characters an attacker injects mid-scheme (``java­script:``,
    ``ja​vascript:``) to dodge a literal scheme match while the browser
    still parses the scheme as live.
    """
    out: list[str] = []
    for ch in s:
        code = ord(ch)
        if code <= 0x1F or 0x7F <= code <= 0x9F:
            continue  # C0 / C1 control chars
        if unicodedata.category(ch) == "Cf":
            continue  # Unicode format chars (soft hyphen, zero-width, BOM, …)
        out.append(ch)
    return "".join(out)


def _normalize_url(raw: str) -> str:
    """Canonicalize a URL for scheme inspection.

    Undoes the global HTML-escape this module applies first (so ``&amp;``
    entity obfuscation is visible again), decodes numeric HTML entities,
    percent-decodes (repeatedly, to catch ``%6a`` and double-encoded
    ``%256a``), strips ASCII whitespace + control chars AND Unicode format
    chars (soft hyphen / zero-width, used to split ``java<U+00AD>script:``),
    and lower-cases. The result is ONLY used to validate the scheme — the
    original (escaped) URL is what gets emitted into the href.
    """
    s = raw
    # Reverse the escapes inline() applies before the link pass, so entity
    # obfuscation (&amp;#x6a; / &lt; …) is decoded for inspection.
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = _decode_numeric_entities(s)
    # Percent-decode BEFORE the scheme check so `%6aavascript:` → `javascript:`
    # (and double-encoded `%256a…` collapses too). The browser performs the
    # same decode on the href, so a percent-hidden scheme is still live.
    s = _percent_decode_until_stable(s)
    # Numeric entities can hide inside the now-decoded text too; decode again so
    # a `%26%23x6a%3b` (percent-encoded `&#x6a;`) is also caught.
    s = _decode_numeric_entities(s)
    # Strip Unicode format chars (soft hyphen / zero-width / BOM) the browser
    # ignores inside a scheme — they must not survive into the comparison.
    s = _strip_format_chars(s)
    # NFKC-fold so compatibility forms collapse to their ASCII canonical before
    # the scheme regex runs: a FULLWIDTH scheme `ｊａｖａｓｃｒｉｐｔ：` (or a mixed
    # `javaｓcript:`) folds to `javascript:` and is then caught as a disallowed
    # scheme. Without this the fullwidth letters miss the ASCII `[a-zA-Z]`
    # scheme pattern and the URL slips through as a "scheme-less" (safe) link.
    # Defense-in-depth — no current browser executes a fullwidth scheme, but a
    # future/embedded engine might, and folding here closes the gap cheaply.
    s = unicodedata.normalize("NFKC", s)
    # Strip every ASCII control char (0x00-0x1F incl. tab/newline/CR) and
    # the space — browsers ignore these inside a scheme, so we must too.
    s = re.sub(r"[\x00-\x20]+", "", s)
    return s.lower()


def _is_safe_url(raw: str) -> bool:
    """True if ``raw`` is an allow-listed scheme OR a relative/anchor URL."""
    norm = _normalize_url(raw)
    if not norm:
        return False
    m = _SCHEME_RE.match(norm)
    if m:
        return m.group(1) in _ALLOWED_SCHEMES
    # No scheme: relative path, anchor, query, or protocol-relative. All of
    # these are safe (they inherit the page's origin/scheme). Protocol-relative
    # ``//host`` is fine — it cannot carry a javascript: payload.
    return True


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _render_link(m: re.Match[str]) -> str:
    """Render one ``[label](url)`` match, dropping disallowed schemes.

    The label is already HTML-escaped (the whole string was escaped before
    the link pass). On a disallowed scheme we keep the escaped label as plain
    text with NO anchor. On an allowed scheme we additionally escape the href
    (defence in depth — it was escaped once already, but double-escaping a
    safe http/https/mailto URL is harmless and closes the attribute).
    """
    label = m.group(1)
    url = m.group(2)
    if not _is_safe_url(url):
        return label  # already escaped upstream; render as inert text
    href = url.replace('"', "&quot;")
    return f'<a href="{href}">{label}</a>'


def inline(text: str) -> str:
    s = (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _render_link, s)
    s = re.sub(r"`([^`]+)`", r'<code class="inline-code">\1</code>', s)
    s = re.sub(r"\*\*\*([^*]+)\*\*\*", r"<strong><em>\1</em></strong>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    return s
