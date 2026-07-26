"""Cover-letter HTML + PDF renderer (Stage 5).

This is a NEW, SEPARATE renderer — it is NOT the CV path. It reuses the
brand typography (the @font-face declarations + font stacks from
``engine/render/css_base``) so a cover letter looks like it belongs with the
CV, but lays the page out as a business LETTER:

    sender block  →  date  →  recipient block  →  salutation
    →  body paragraphs  →  sign-off (closing + name)

Crucially, **multi-page is allowed**. There is NO 1-page bookmark-tree gate
here — that invariant lives only in ``engine/build.py`` for the CV and is
left completely untouched. The cover-letter ceiling is the server-side WORD
CAP (see :mod:`engine.cover_letter.guards`), not a page count, so a long
letter renders to as many pages as it needs.

The public surface:
  * :func:`render_cover_letter_html` — full HTML document (testable without
    WeasyPrint).
  * :func:`render_cover_letter_pdf` — bytes of the rendered PDF (imports
    WeasyPrint lazily so the module loads in bare environments).
"""
from __future__ import annotations

import json
import re
from typing import Any

# Reuse the engine's font stacks + @font-face block. We import the private
# builder only to lift the typography; the CV layout CSS is intentionally NOT
# reused (a letter is not a CV).
try:  # engine/ is on sys.path in every entrypoint.
    from render import css_base, resource_policy
except ImportError:  # pragma: no cover - fallback for unusual import roots
    from engine.render import css_base, resource_policy


def _esc(value: Any) -> str:
    """HTML-escape a scalar (mirrors templates._esc — kept local, no coupling)."""
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _font_stack(font: str) -> str:
    """The same family stack the CV uses for ``font`` (serif|sans|mono)."""
    f = (font or "serif").strip().lower()
    if f == "sans":
        return (
            "'Inter', -apple-system, BlinkMacSystemFont, "
            "'Helvetica Neue', Arial, sans-serif"
        )
    if f == "mono":
        return "ui-monospace, 'JetBrains Mono', 'Menlo', 'Consolas', monospace"
    return (
        "'Source Serif 4', 'EB Garamond', 'Hoefler Text', "
        "Georgia, 'Times New Roman', serif"
    )


def _font_face_block(base_url: str) -> str:
    """Extract just the @font-face declarations from the CV's CSS builder.

    We call ``css_base.build_css`` (the single source of the font files) and
    slice off everything from the first ``:root`` block onward — that's the
    CV-specific layout. What remains is the shared @font-face block, so the
    letter and the CV always load identical font binaries from the same paths.
    """
    full = css_base.build_css(base_url, accent="#111111", font="serif")
    marker = ":root"
    idx = full.find(marker)
    return full[:idx] if idx != -1 else full


# Only a literal hex color may reach the CSS sink — anything else falls back
# to the default so a hostile ``accent`` can't break out of the declaration.
# Valid CSS hex lengths are exactly 3/4/6/8 (#rgb/#rgba/#rrggbb/#rrggbbaa).
_ACCENT_RE = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_ACCENT_DEFAULT = "#111111"


def _safe_accent(value: Any) -> str:
    """Return ``value`` iff it is a plain hex color (#rgb/#rgba/#rrggbb/#rrggbbaa)."""
    accent = str(value or "").strip()
    if _ACCENT_RE.fullmatch(accent):
        return accent
    return _ACCENT_DEFAULT


def _letter_css(base_url: str, *, accent: str, font: str) -> str:
    """Letter-layout CSS: shared @font-face + a clean A4 business-letter sheet.

    No 1-page constraint — ``.page`` grows and WeasyPrint paginates naturally.
    Both interpolated values are safe: ``accent`` is hex-validated by the
    caller (:func:`_safe_accent`) and ``_font_stack`` only ever returns one of
    three constant, pre-quoted stacks.
    """
    font_stack = _font_stack(font)
    return (
        _font_face_block(base_url)
        + f"""
:root {{
  --accent: {accent};
  --ink:    #111111;
  --body:   #1a1a1a;
  --muted:  #555555;
  --paper:  #ffffff;
  --font:   {font_stack};
}}

* {{ box-sizing: border-box; }}

@page {{
  size: A4;
  /* Generous letter margins; pages flow naturally (multi-page OK). */
  margin: 24mm 22mm;
}}

html, body {{
  margin: 0;
  padding: 0;
  background: var(--paper);
  color: var(--body);
  font-family: var(--font);
  font-size: 10.5pt;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
  overflow-wrap: anywhere;
  word-break: normal;
}}

.letter {{
  max-width: 100%;
  overflow-wrap: anywhere;
}}

.sender {{
  text-align: right;
  color: var(--muted);
  font-size: 9.5pt;
  line-height: 1.4;
  margin-bottom: 14mm;
}}
.sender .sender-name {{
  color: var(--ink);
  font-weight: 600;
  font-size: 11pt;
}}

.letter-date {{
  color: var(--muted);
  margin-bottom: 8mm;
}}

.recipient {{
  color: var(--ink);
  line-height: 1.4;
  margin-bottom: 8mm;
  white-space: pre-line;
  overflow-wrap: anywhere;
}}
.recipient .company {{ font-weight: 600; }}

.subject {{
  font-weight: 600;
  color: var(--ink);
  margin-bottom: 6mm;
  overflow-wrap: anywhere;
}}

.salutation {{ margin-bottom: 5mm; }}

.body p {{
  margin: 0 0 4mm;
  text-align: justify;
  hyphens: auto;
  -webkit-hyphens: auto;
  overflow-wrap: anywhere;
  orphans: 2;
  widows: 2;
}}

.signoff {{
  margin-top: 8mm;
  break-inside: avoid;
}}
.signoff .closing {{ margin-bottom: 12mm; }}
.signoff .signer-name {{ color: var(--ink); font-weight: 600; }}

/* A thin accent rule under the signer's name ties it to the CV's accent. */
.signoff .signer-name {{
  border-top: 0.6pt solid var(--accent);
  padding-top: 2mm;
  display: inline-block;
}}

.attachments {{
  break-inside: avoid;
  margin-top: 10mm;
  color: var(--muted);
  font-size: 9.5pt;
}}
.attachments .attachments-label {{ color: var(--ink); font-weight: 600; }}
"""
    )


# Inline-markdown markers the model is told NOT to emit (the prompt forbids
# Markdown). If one slips through anyway, we strip the MARKERS to plain text so
# the letter never shows a literal ``**`` — but we deliberately do NOT render
# them as HTML emphasis: the cover-letter body is ESCAPED prose, not Markdown
# (the Gate-1 decision; the body goes through ``_esc``, never ``markdown_inline``).
_MD_BOLD_ITALIC = re.compile(r"\*\*\*([^*]+?)\*\*\*")  # ***x*** → x
_MD_BOLD = re.compile(r"\*\*([^*]+?)\*\*")  # **x** → x
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")  # *x* → x
_MD_CODE = re.compile(r"`([^`]+?)`")  # `x` → x
_MD_LINK = re.compile(r"\[([^\]]+?)\]\((?:[^)\s]+)\)")  # [label](url) → label
_MD_STRAY_STARS = re.compile(r"\*+")  # any leftover run of asterisks → removed


def _strip_inline_markdown(text: str) -> str:
    """Reduce stray inline Markdown to its plain text (no HTML, no markers).

    Removes the emphasis/code/link MARKERS while keeping the inner words, so a
    body the model accidentally bolds (``**40%**``) renders as plain ``40%`` —
    never as a literal ``**`` and never as a ``<strong>`` tag. This keeps the
    body a clean ESCAPED prose string (the renderer's contract) rather than
    routing it through the Markdown processor.
    """
    s = _MD_LINK.sub(r"\1", text)
    s = _MD_BOLD_ITALIC.sub(r"\1", s)
    s = _MD_BOLD.sub(r"\1", s)
    s = _MD_ITALIC.sub(r"\1", s)
    s = _MD_CODE.sub(r"\1", s)
    s = _MD_STRAY_STARS.sub("", s)  # drop any unmatched ``**`` / ``*`` runs
    return s


def _body_blocks(body: Any) -> list[str]:
    """Normalize a body into a list of paragraph strings (JSON-array tolerant).

    The body contract is a single blank-line-separated string. As cheap
    insurance against model variability we also accept a JSON array of
    paragraphs (``["para", "para"]``) — either passed directly as a list or as
    a JSON string — and stray leading bullet/quote chars are trimmed.
    """
    text: str
    if isinstance(body, (list, tuple)):
        text = "\n\n".join(str(b) for b in body)
    else:
        text = str(body or "")
        stripped = text.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                text = "\n\n".join(str(b) for b in parsed)
    text = text.strip()
    if not text:
        return []
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    return blocks or [text]


def _paragraphs_html(body: Any) -> str:
    """Split letter body prose into <p> paragraphs (blank-line separated).

    The body stays ESCAPED (``_esc``) — it is NOT routed through the Markdown
    processor. Any stray inline Markdown markers the model emits are reduced to
    plain text first (see :func:`_strip_inline_markdown`) so the letter never
    shows a literal ``**``.
    """
    blocks = _body_blocks(body)
    if not blocks:
        return "<p></p>"
    out: list[str] = []
    for block in blocks:
        joined = " ".join(line.strip(" \t-•*>") for line in block.splitlines() if line.strip())
        joined = _strip_inline_markdown(joined)
        out.append(f"<p>{_esc(joined)}</p>")
    return "\n".join(out)


def _recipient_html(doc: dict[str, Any]) -> str:
    """Recipient block from {recipient, company} — omitted if both blank."""
    recipient = (doc.get("recipient") or "").strip()
    company = (doc.get("company") or "").strip()
    if not recipient and not company:
        return ""
    lines: list[str] = []
    if recipient:
        lines.append(_esc(recipient))
    if company:
        lines.append(f'<span class="company">{_esc(company)}</span>')
    return '<div class="recipient">' + "\n".join(lines) + "</div>"


def _attachments_html(doc: dict[str, Any]) -> str:
    """Enclosures block ("Enclosures:" / "Anlagen:") — omitted when empty.

    ``attachments`` is a list of enclosure labels (e.g. ["CV"]). The label is
    language-aware (German letters say "Anlagen"); the values are escaped.
    """
    attachments = doc.get("attachments") or []
    if isinstance(attachments, str):
        attachments = [attachments]
    items = [str(a).strip() for a in attachments if a is not None and str(a).strip()]
    if not items:
        return ""
    german = (doc.get("language") or "").strip().lower() == "de"
    label = "Anlagen" if german else "Enclosures"
    return (
        '<div class="attachments">'
        + f'<span class="attachments-label">{_esc(label)}:</span> '
        + _esc(", ".join(items))
        + "</div>"
    )


def _salutation(doc: dict[str, Any]) -> str:
    """Salutation line. Uses an explicit override, else the recipient, else a
    neutral opener (EN/DE)."""
    override = (doc.get("salutation") or "").strip()
    if override:
        return _esc(override)
    recipient = (doc.get("recipient") or "").strip()
    language = (doc.get("language") or "").strip().lower()
    german = language == "de"
    if recipient:
        if german:
            return f"Sehr geehrte/r {_esc(recipient)},"
        return f"Dear {_esc(recipient)},"
    if german:
        return "Sehr geehrte Damen und Herren,"
    return "Dear Hiring Manager,"


def render_cover_letter_html(doc: dict[str, Any], *, base_url: str = "") -> str:
    """Render a cover-letter document dict to a full HTML page.

    ``doc`` keys (all optional except ``body``):
      sender_name, sender_contact (lines), date, recipient, company, role,
      subject, salutation, body (the generated/edited letter prose), closing,
      attachments (list of enclosure labels), signer_name, accent, font,
      language.

    The body is rendered ESCAPED (stray inline Markdown is reduced to plain
    text — never to HTML emphasis, never a literal ``**``). Multi-page is fine —
    the layout has no page-count gate.
    """
    accent = _safe_accent(doc.get("accent"))
    font = (doc.get("font") or "serif").strip().lower() or "serif"
    css = _letter_css(base_url, accent=accent, font=font)

    sender_name = (doc.get("sender_name") or "").strip()
    sender_contact = doc.get("sender_contact") or []
    if isinstance(sender_contact, str):
        sender_contact = [sender_contact]
    sender_lines = "".join(f"<div>{_esc(line)}</div>" for line in sender_contact if str(line).strip())
    sender_html = ""
    if sender_name or sender_lines:
        sender_html = (
            '<div class="sender">'
            + (f'<div class="sender-name">{_esc(sender_name)}</div>' if sender_name else "")
            + sender_lines
            + "</div>"
        )

    date = (doc.get("date") or "").strip()
    date_html = f'<div class="letter-date">{_esc(date)}</div>' if date else ""

    german = (doc.get("language") or "").lower() == "de"
    subject = (doc.get("subject") or "").strip()
    role = (doc.get("role") or "").strip()
    if not subject and role:
        subject = f"Bewerbung als {role}" if german else f"Application for {role}"
    # DIN-5008 German business letters label the subject line "Betreff:".
    # English letters keep the subject unlabelled (it stands on its own line), so
    # the subject TEXT is rendered verbatim — only the German chrome word is added.
    if subject and german and not subject.lower().startswith("betreff"):
        subject_label = f"Betreff: {subject}"
    else:
        subject_label = subject
    subject_html = f'<div class="subject">{_esc(subject_label)}</div>' if subject else ""

    closing = (doc.get("closing") or "").strip()
    if not closing:
        closing = (
            "Mit freundlichen Grüßen,"
            if (doc.get("language") or "").lower() == "de"
            else "Best regards,"
        )
    signer = (doc.get("signer_name") or sender_name or "").strip()
    signoff_html = (
        '<div class="signoff">'
        + f'<div class="closing">{_esc(closing)}</div>'
        + (f'<div class="signer-name">{_esc(signer)}</div>' if signer else "")
        + "</div>"
    )

    title = _esc(subject or role or "Cover Letter")
    return f"""<!DOCTYPE html>
<html lang="{_esc((doc.get('language') or 'en'))}">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<main class="letter">
{sender_html}
{date_html}
{_recipient_html(doc)}
{subject_html}
<div class="salutation">{_salutation(doc)}</div>
<div class="body">
{_paragraphs_html(doc.get('body') or '')}
</div>
{signoff_html}
{_attachments_html(doc)}
</main>
</body>
</html>"""


def render_cover_letter_pdf(doc: dict[str, Any], *, base_url: str = "") -> bytes:
    """Render a cover-letter document to PDF bytes (multi-page allowed).

    Imports WeasyPrint lazily so this module loads even where WeasyPrint is
    absent (e.g. import-time in a bare test env). There is deliberately NO
    page-count check — the CV's 1-page invariant does not apply to letters.
    """
    from weasyprint import HTML

    resolved_base = base_url or resource_policy.repo_base_url()
    html = render_cover_letter_html(doc, base_url=resolved_base)
    document = HTML(
        string=html,
        base_url=resolved_base,
        url_fetcher=resource_policy.make_url_fetcher(),
    ).render()
    return document.write_pdf()
