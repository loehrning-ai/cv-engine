"""Claude-powered CV extraction.

Takes any pasted text (resume in plain text, Markdown, LinkedIn export,
etc.) and uses Claude to map it into our YAML schema. Falls back to the
heuristic plain-text parser when no API key is available.

The system prompt is designed for prompt caching (Anthropic's
``cache_control: {"type": "ephemeral"}``) — it stays static across calls
so subsequent extractions are ~90% cheaper on the cached portion.

API key resolution (Stage 4): every public function takes an optional
``api_key`` kwarg. The gateway passes the *resolved* key — either the user's
decrypted BYO key or, on the managed-credit path, the operator key — so the
provider call is made with the right account. When ``api_key`` is omitted
(CLI / local dev) we fall back to ``os.environ['ANTHROPIC_API_KEY']`` so the
single-user shell flow is unchanged. The actual SDK call goes through the
provider-agnostic :class:`~engine.llm.client.LLMClient`.
"""
from __future__ import annotations

import os
import re
from typing import Any

import yaml

from . import importers

try:
    # engine/ is on sys.path in every entrypoint (CLI, server, tests).
    from llm.client import (
        canonical_provider,
        demo_mode_enabled,
        make_client,
        ollama_health,
        provider_spec,
        resolve_model,
    )
except ImportError:  # pragma: no cover - fallback for unusual import roots
    from engine.llm.client import (
        canonical_provider,
        demo_mode_enabled,
        make_client,
        ollama_health,
        provider_spec,
        resolve_model,
    )


def _resolve_api_key(api_key: str | None) -> str | None:
    """The explicit per-request key, else the operator key from the env.

    The env fallback is the single-user CLI path, which is Anthropic-only —
    Gemini is BYO-only, so a Gemini call ALWAYS arrives with an explicit
    ``api_key`` (resolved by the gateway) and never falls through to the env.
    """
    return api_key or os.environ.get("ANTHROPIC_API_KEY") or None


def _provider_sdk_installed(provider: str) -> bool:
    """Whether the SDK for ``provider`` is importable (provider-aware check)."""
    p = canonical_provider(provider)
    spec = provider_spec(p)
    if spec.validation == "openai_compatible":
        return True
    if spec.auth_type == "none":
        return p == "ollama" and bool(ollama_health().get("available"))
    try:
        if p == "gemini":
            from google import genai  # noqa: F401
        else:
            import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


# Per-shape YAML schema fragments — used by build_system_prompt() to
# construct the schema description Claude sees. Adding a section to
# DEFAULT_SECTIONS automatically extends the prompt.
_SHAPE_SCHEMAS: dict[str, str] = {
    "experience": (
        "  - role: <string>              # e.g. \"Senior Software Engineer\"\n"
        "    company: <string>\n"
        "    location: <string>\n"
        "    start: <string>             # MM/YYYY or YYYY format preferred\n"
        "    end: <string>               # MM/YYYY, YYYY, or \"Present\"\n"
        "    bullets:\n"
        "      - <one impact-driven sentence per bullet, no leading dash>\n"
        "    stack: <optional comma-separated technologies>"
    ),
    "education": (
        "  - degree: <string>            # e.g. \"M.Sc. Computer Science (Minor: Data Science)\"\n"
        "    school: <string>\n"
        "    location: <string>\n"
        "    start: <string>\n"
        "    end: <string>\n"
        "    note: <optional one-line note, e.g. \"Graduated with distinction\">"
    ),
    "skills": (
        "  - { label: <category>, items: <comma-separated items> }"
    ),
    "compact": (
        "  - { title: <string>, date: <string>, desc: <one-line description> }"
    ),
    "publication": (
        "  - title: <string>\n"
        "    authors: <string — comma-separated, you first if applicable>\n"
        "    venue: <journal / conference / preprint server>\n"
        "    date: <YYYY or MM/YYYY>\n"
        "    doi: <optional DOI>\n"
        "    url: <optional URL>"
    ),
}


def build_system_prompt() -> str:
    """Build the Claude system prompt from the section registry.

    The schema block lists every registered section with its per-shape
    YAML fragment. The prompt is wrapped in cache_control:ephemeral by
    the caller so the cache key matches across calls — but it changes
    when DEFAULT_SECTIONS changes (which is rare; intentional).
    """
    from .sections import DEFAULT_SECTIONS

    def _field_comment(section) -> str:
        fields = getattr(section, "fields", ()) or ()
        if not fields:
            return ""
        required = [f.name for f in fields if f.required]
        optional = [f.name for f in fields if not f.required]
        parts = []
        if required:
            parts.append("required fields: " + ", ".join(required))
        if optional:
            parts.append("optional fields: " + ", ".join(optional))
        return "   # " + "; ".join(parts)

    sections_yaml = "\n\n".join(
        f"{s.key}:" +
        (f"   # {s.ai_hint}" if s.ai_hint else _field_comment(s)) +
        "\n" + _SHAPE_SCHEMAS.get(s.shape, _SHAPE_SCHEMAS["compact"])
        for s in DEFAULT_SECTIONS
    )

    return (
        "You are a CV/resume data extractor.\n\n"
        "You will be given the text of a resume (any format — plain text, "
        "Markdown, PDF dump, LinkedIn export). Your job is to extract "
        "structured data into the EXACT schema below and return it as a "
        "single YAML document. Output ONLY the YAML — no prose, no code "
        "fences, no commentary.\n\n"
        "# Schema\n\n"
        "```yaml\n"
        "name: <string, required — full name>\n"
        "accent: \"#111111\"               # hex; default to #111111\n"
        "font: serif                     # serif | sans | mono — default serif\n\n"
        "contact:                        # 1+ entries; use brand-network shape when known\n"
        "  - { network: <network>, username: <handle or value>, label?: <display text>, href?: <override URL> }\n"
        "  # OR (when no known network applies)\n"
        "  - { label: <text>, href: <url> }\n\n"
        f"{sections_yaml}\n"
        "```\n\n"
        "# Network values (use these exactly when applicable)\n\n"
        "mail, phone, web, linkedin, github, gitlab, x, mastodon, bluesky, "
        "instagram, youtube, telegram, whatsapp, reddit, stackoverflow, "
        "leetcode, orcid, googlescholar, researchgate, imdb\n\n"
        "For known networks, set `username` to the handle (e.g. `alex-hartman` "
        "for linkedin, not the full URL). The renderer auto-resolves the URL.\n\n"
        "# Rules\n\n"
        "1. Output ONLY YAML. The first character of your response is the "
        "first character of the YAML document.\n"
        "2. NEVER invent facts. If the source doesn't mention something, "
        "omit it.\n"
        "3. Bullets should be short, impact-focused (verb + what + why/impact). "
        "Don't include the leading \"•\" or \"-\".\n"
        "4. For contact entries, prefer `network` + `username` over plain "
        "`{label, href}` whenever a known network applies.\n"
        "5. If a section has no entries, omit it entirely (don't emit empty "
        "arrays).\n"
        "6. Order experience and education NEWEST FIRST.\n"
        "7. Render mathematical/special characters (–, &, %) as Unicode, not "
        "HTML entities."
    )


# Eagerly built at import time so it can be reused (matches cache-control
# semantics — same string on every call until DEFAULT_SECTIONS changes).
SYSTEM_PROMPT: str = build_system_prompt()


# Some files arrive as PDF or DOCX bytes. We extract text first and then
# pass the text to Claude. Keep these helpers pure-Python and dependency-light.

# A crafted DOCX is a zip bomb: ~800KB of compressed data can decompress to
# hundreds of MB of XML. Cap the decompressed document.xml size (a real
# resume is a few hundred KB of XML at most) and the extracted text length
# (it is parsed and sent to the LLM, whose context is finite anyway).
_MAX_DECOMPRESSED_XML = 32 * 1024 * 1024  # 32 MB
_MAX_EXTRACTED_CHARS = 1_000_000  # 1M chars of text is far past any real CV


def extract_text_from_pdf(data: bytes) -> str:
    """Best-effort PDF text extraction via pypdf."""
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    chunks: list[str] = []
    total = 0
    for page in reader.pages:
        try:
            chunk = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — page may be image-only
            chunk = ""
        chunks.append(chunk)
        total += len(chunk)
        if total >= _MAX_EXTRACTED_CHARS:
            break
    return "\n\n".join(chunks).strip()[:_MAX_EXTRACTED_CHARS]


def extract_text_from_docx(data: bytes) -> str:
    """DOCX is a zip — pull the body text from word/document.xml."""
    import io
    import zipfile

    from defusedxml import ElementTree as ET
    from defusedxml.common import DefusedXmlException

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        try:
            info = zf.getinfo("word/document.xml")
        except KeyError as exc:
            raise ValueError("Not a valid .docx file (missing word/document.xml)") from exc
        # Zip-bomb guard: reject on the header-declared size first (cheap),
        # then stream with a hard read cap in case the header lies.
        if info.file_size > _MAX_DECOMPRESSED_XML:
            raise ValueError(
                f"DOCX document.xml is too large ({info.file_size} bytes decompressed)"
            )
        with zf.open(info) as fh:
            xml = fh.read(_MAX_DECOMPRESSED_XML + 1)
        if len(xml) > _MAX_DECOMPRESSED_XML:
            raise ValueError("DOCX document.xml exceeds the decompressed-size cap")

    try:
        root = ET.fromstring(xml)
    except DefusedXmlException as exc:
        raise ValueError("DOCX document.xml contains forbidden XML constructs") from exc
    except ET.ParseError as exc:
        raise ValueError("DOCX document.xml is malformed") from exc
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    total = 0
    for p in root.iter(f"{ns}p"):
        runs = [t.text or "" for t in p.iter(f"{ns}t")]
        text = "".join(runs).strip()
        if text:
            paragraphs.append(text)
            total += len(text)
            if total >= _MAX_EXTRACTED_CHARS:
                break
    return "\n\n".join(paragraphs)[:_MAX_EXTRACTED_CHARS]


def is_available(*, api_key: str | None = None, provider: str = "anthropic") -> bool:
    """Whether ``provider``'s API can be called (key resolvable, SDK installed).

    ``api_key`` is the explicit per-request key (BYO or operator); when omitted
    we check the operator key in the environment, preserving the single-user CLI
    behaviour (Anthropic-only). The SDK check is provider-aware so a Gemini
    request doesn't require the ``anthropic`` package and vice-versa.
    """
    # DEV-ONLY demo mode (defaults OFF): the offline StubLLMClient needs no key
    # and no SDK, so extraction can use the deterministic stub instead of the
    # heuristic fallback. Production (flag off) keeps the exact checks below.
    if demo_mode_enabled():
        return True
    p = canonical_provider(provider)
    spec = provider_spec(p)
    if spec.auth_type == "none":
        return _provider_sdk_installed(p)
    if not _resolve_api_key(api_key):
        return False
    return _provider_sdk_installed(p)


def extract(
    text: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    provider: str = "anthropic",
) -> str:
    """Run the selected provider over ``text`` and return raw YAML.

    Raises ``RuntimeError`` if the API isn't available; the caller should
    fall back to ``importers.from_plain_text``.
    """
    prov = canonical_provider(provider)
    spec = provider_spec(prov)
    key = "" if spec.auth_type == "none" else _resolve_api_key(api_key)
    if not is_available(api_key=key, provider=provider):
        raise RuntimeError("No usable API key / provider SDK for extraction.")

    client = make_client(prov, key or "")
    raw = client.complete(
        system=SYSTEM_PROMPT,
        user=text,
        model=resolve_model(prov, model),
        max_tokens=4096,
        cache_system=True,
    )
    return _strip_code_fences(raw)


def extract_to_dict(
    text: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    provider: str = "anthropic",
) -> dict[str, Any]:
    """Run the provider and parse the result back into our cv dict shape.

    Falls back to the heuristic parser on any failure (network error, key
    missing, parse error). The fallback path is documented in the response
    so the UI can show "AI extraction unavailable, used heuristic fallback".
    """
    try:
        raw = extract(text, api_key=api_key, model=model, provider=provider)
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            raise ValueError("AI returned non-mapping YAML.")
        return parsed
    except Exception:
        # Fall back to heuristic — the user still gets *something*.
        return importers.from_plain_text(text)


_FENCE_RE = re.compile(r"^```(?:yaml|ya?ml)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.IGNORECASE)


def _strip_code_fences(text: str) -> str:
    """Remove any accidental ```yaml fences Claude might add."""
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


# ──────────────────────────────────────────────────────────────────────
#  POLISH — tighten a bullet or rewrite an entire section.
#
#  These prompts are short and intentionally cheap. Each call is
#  independent (no shared cache prefix between polish and extract — the
#  use-cases are too different to share).
# ──────────────────────────────────────────────────────────────────────

POLISH_BULLET_PROMPT = """You rewrite resume bullet points so they're tighter, more impact-focused, and FAANG-style.

Rules:
1. Output ONE rewritten bullet. No bullet character, no quotes, no preamble, no commentary.
2. Verb-led. Start with a strong action verb in past tense (or present tense for current roles).
3. Numbers > adjectives. Prefer "reduced X by 40%" over "significantly improved X".
4. Aim for 100-180 characters. Trim adverbs and filler.
5. Keep the original facts. Don't invent metrics, scope, or technologies.
6. If the bullet is already tight (< 100 chars and verb-led with numbers), return it unchanged.
7. NEVER use Markdown formatting (**, _, `) — plain prose only."""


POLISH_SECTION_PROMPT = """You rewrite resume bullet points for a single role so they're tighter and more impact-focused. FAANG/YC convention.

Rules:
1. Output one bullet per line. No bullet character, no quotes, no preamble, no commentary, no numbering.
2. Same number of bullets as the input — keep the structure.
3. Verb-led. Numbers > adjectives. 100-180 characters per bullet.
4. Don't invent facts. Keep scope, tools, and metrics from the originals.
5. Preserve the role's voice (technical vs. managerial vs. research).
6. Output PLAIN prose. No Markdown formatting (**, _, `)."""


def polish_bullet(
    text: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    provider: str = "anthropic",
) -> str:
    """Rewrite a single resume bullet via the provider. Raises on no key/SDK."""
    prov = canonical_provider(provider)
    spec = provider_spec(prov)
    key = "" if spec.auth_type == "none" else _resolve_api_key(api_key)
    if not is_available(api_key=key, provider=provider):
        raise RuntimeError("No usable API key / provider SDK for polish.")

    client = make_client(prov, key or "")
    raw = client.complete(
        system=POLISH_BULLET_PROMPT,
        user=text,
        model=resolve_model(prov, model),
        max_tokens=400,
        cache_system=False,  # short, varied prompt — not worth a cache write
    )
    # Strip any leading bullet character or quotes the model might emit.
    raw = re.sub(r"^[•\-*]\s*", "", raw).strip()
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1].strip()
    return raw


def polish_section(
    bullets: list[str],
    context: str = "",
    *,
    api_key: str | None = None,
    model: str | None = None,
    provider: str = "anthropic",
) -> list[str]:
    """Rewrite a whole role's bullets at once. Returns the new bullets in
    the same order; empty input → empty output.
    """
    if not bullets:
        return []
    prov = canonical_provider(provider)
    spec = provider_spec(prov)
    key = "" if spec.auth_type == "none" else _resolve_api_key(api_key)
    if not is_available(api_key=key, provider=provider):
        raise RuntimeError("No usable API key / provider SDK for polish.")

    user_msg = (context.strip() + "\n\n" if context.strip() else "") + "\n".join(
        f"- {b}" for b in bullets
    )
    client = make_client(prov, key or "")
    raw = client.complete(
        system=POLISH_SECTION_PROMPT,
        user=user_msg,
        model=resolve_model(prov, model),
        max_tokens=1200,
        cache_system=False,
    )
    out: list[str] = []
    for line in raw.splitlines():
        line = re.sub(r"^[•\-*]\s*", "", line).strip()
        if line:
            out.append(line)
    # Match the original count if possible — if Claude returned more or
    # fewer, just trim/pad with the originals.
    if len(out) > len(bullets):
        out = out[: len(bullets)]
    while len(out) < len(bullets):
        out.append(bullets[len(out)])
    return out
