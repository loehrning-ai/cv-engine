"""HTML builders for the CV.

Output structure (in the order rendered):
  <main class="page density-{mode}">
    <header class="header" id="sec-header"> name + contact (+ optional photo)
    <section id="sec-experience"> Experience entries
    <section id="sec-education">  Education entries
    <section id="sec-skills">     Skills grid
    <section id="sec-projects">   Compact rows
    <section id="sec-leadership"> Compact rows
    <section id="sec-others">     Compact rows
  </main>

Slugged section IDs (``sec-{key}``) drive the editor outline-jump. They
match YAML top-level keys so they're stable across edits.
"""
from __future__ import annotations

import re
from typing import Any

from . import template_registry
from .markdown_inline import inline as md
from .markdown_inline import _is_safe_url


def _esc(s: Any) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _safe_href(value: Any) -> str:
    href = str(value or "").strip()
    return href if href and _is_safe_url(href) else ""


def _normalize_scholarly_url(value: Any) -> str:
    """Turn a bare DOI into a resolvable https URL.

    A publication ``doi``/``url`` is often a bare identifier ("10.1234/foo") or
    a ``doi:10.1234/foo`` string. Left as-is these render as a BROKEN relative
    link (``href="10.1234/foo"`` resolves against the page origin) or are
    dropped entirely (``_safe_href`` strips the ``doi:`` scheme). Normalise a
    bare/prefixed DOI to ``https://doi.org/<doi>``; anything that already has an
    http(s) scheme (or isn't a DOI) is returned unchanged for _safe_href to vet.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low.startswith(("http://", "https://")):
        return raw
    if low.startswith("doi:"):
        raw = raw[4:].strip()
    if raw.startswith("10.") and "/" in raw:  # DOIs are "10.<registrant>/<suffix>"
        return "https://doi.org/" + raw
    return raw


def _section_id(slug: str) -> str:
    """Return a safe, deterministic id suffix for outline/preview anchors."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(slug or "").strip()).strip("-")
    return cleaned or "custom-section"


def _date_range(start: str, end: str) -> str:
    s, e = _esc(start), _esc(end)
    if s and e:
        return f"{s} to {e}"
    return s or e


def _contact_line(contact: list[dict[str, Any]], with_links: bool = True, layout: str = "inline") -> str:
    """Build the centered contact line: icon + label, " · " separated.

    Each item renders as ``<a><svg/>label</a>`` (or a bare label) — purely
    inline so it doesn't affect the line-box height. The " · " separators
    are inline ``<span>``s with side margin.
    """
    parts: list[str] = []
    for c in contact:
        label = _esc(c.get("label", ""))
        href = c.get("href")
        icon = c.get("icon_svg", "")
        inner = f"{icon}{label}" if icon else label
        safe_href = _safe_href(href)
        if with_links and safe_href:
            item = f'<a href="{_esc(safe_href)}">{inner}</a>'
        else:
            item = inner
        parts.append(f'<span class="contact-item">{item}</span>')
    sep = "" if layout in {"stack", "grid"} else '<span class="sep">·</span>'
    return sep.join(parts)


def _header(cv: dict[str, Any]) -> str:
    name = _esc(cv["name"])
    contact_layout = _layout_value(cv.get("contact_layout"), {"inline", "stack", "grid"}, "inline")
    header_layout = _layout_value(cv.get("header_layout"), {"center", "left", "split"}, "center")
    photo_layout = _layout_value(cv.get("photo_layout"), {"right", "left", "square", "hidden"}, "right")
    contact = _contact_line(cv.get("contact", []), with_links=True, layout=contact_layout)
    photo = cv.get("photo")
    classes = ["header", f"header-layout-{header_layout}", f"contact-layout-{contact_layout}"]
    if photo and photo_layout != "hidden":
        classes.extend(["has-photo", f"photo-layout-{photo_layout}"])
        photo_html = f'<img class="photo" src="{_esc(photo)}" alt="">'
        text_html = f"""<div class="header-text">
    <h1 class="name">{name}</h1>
    <div class="contact">{contact}</div>
  </div>"""
        body = f"{photo_html}\n  {text_html}" if photo_layout == "left" else f"{text_html}\n  {photo_html}"
        return f"""\
<header class="{' '.join(classes)}" id="sec-header">
  {body}
</header>"""
    return f"""\
<header class="{' '.join(classes)}" id="sec-header">
  <h1 class="name">{name}</h1>
  <div class="contact">{contact}</div>
</header>"""


def _layout_value(value: Any, allowed: set[str], default: str) -> str:
    if not isinstance(value, str):
        return default
    cleaned = value.strip().lower().replace("-", "_")
    return cleaned if cleaned in allowed else default


def _experience_item(item: dict[str, Any]) -> str:
    company = _esc(item.get("company", ""))
    location = _esc(item.get("location", ""))
    role = _esc(item.get("role", "") or item.get("title", ""))
    dates = _date_range(item.get("start", ""), item.get("end", ""))
    # Prune empty / whitespace-only bullets so an in-progress ``bullets: ['']``
    # (an unfilled row saved from the form) never renders a stray empty <li>
    # dot in the preview or the printed PDF.
    bullets = [b for b in (item.get("bullets") or []) if str(b).strip()]
    bullet_html = "".join(f"<li>{md(b)}</li>" for b in bullets)
    bullets_block = f'<ul class="bullets">{bullet_html}</ul>' if bullets else ""
    stack = item.get("stack")
    stack_block = f'<div class="stack">{md(stack)}</div>' if stack else ""
    return f"""\
<div class="item">
  <div class="item-head"><div class="left">{company}</div><div class="right">{location}</div></div>
  <div class="item-sub"><div class="left">{role}</div><div class="right">{dates}</div></div>
  {bullets_block}
  {stack_block}
</div>"""


def _education_item(item: dict[str, Any]) -> str:
    school = _esc(item.get("school", ""))
    location = _esc(item.get("location", ""))
    degree = _esc(item.get("degree", "") or item.get("title", ""))
    dates = _date_range(item.get("start", ""), item.get("end", ""))
    note = item.get("note")
    note_block = (
        f'<ul class="bullets"><li>{md(note)}</li></ul>' if note else ""
    )
    return f"""\
<div class="item">
  <div class="item-head"><div class="left">{school}</div><div class="right">{location}</div></div>
  <div class="item-sub"><div class="left">{degree}</div><div class="right">{dates}</div></div>
  {note_block}
</div>"""


def _skills_grid(items: list[dict[str, str]]) -> str:
    rows: list[str] = []
    for s in items:
        rows.append(f"<dt>{_esc(s.get('label', ''))}</dt><dd>{md(s.get('items', ''))}</dd>")
    return f'<dl class="skills-grid">{"".join(rows)}</dl>'


def _compact_row(item: dict[str, Any] | Any) -> str:
    if not isinstance(item, dict):
        item = {"title": str(item)}
    raw_title = item.get("title") or item.get("name") or item.get("label") or item.get("items") or item.get("desc") or ""
    title = md(raw_title)
    desc = item.get("desc") if item.get("desc") != raw_title else None
    desc_html = f" · {md(desc)}" if desc else ""
    date = _esc(item.get("date", ""))
    return f"""\
<div class="row">
  <div class="left"><b>{title}</b>{desc_html}</div>
  <div class="right">{date}</div>
</div>"""


def _publication_item(item: dict[str, Any]) -> str:
    """Two-line publication entry: title/date on top, authors/venue on row 2.

    Required: title.
    Optional: authors, venue, date, doi, url.

    Renders as a compact "row + sub" combo, similar to experience but
    much terser — academics cram many of these onto a page.
    """
    title = _esc(item.get("title") or item.get("name") or item.get("venue") or "Publication")
    date = _esc(item.get("date", ""))
    authors = item.get("authors")
    venue = item.get("venue")
    url = _normalize_scholarly_url(item.get("url") or item.get("doi"))
    secondary_parts: list[str] = []
    if authors:
        secondary_parts.append(md(authors))
    if venue:
        secondary_parts.append(md(venue))
    secondary = " · ".join(secondary_parts)
    safe_url = _safe_href(url)
    title_html = f'<a href="{_esc(safe_url)}">{title}</a>' if safe_url else f"<b>{title}</b>"
    sub_html = f'<div class="pub-sub">{secondary}</div>' if secondary else ""
    return f"""\
<div class="item publication-item">
  <div class="item-head">
    <div class="left">{title_html}</div>
    <div class="right">{date}</div>
  </div>
  {sub_html}
</div>"""


def _skills_section(items: list[dict[str, str]]) -> str:
    """Wrap _skills_grid so it conforms to the per-item RENDERERS shape API.

    All other shapes render one item at a time; skills renders the whole
    list as a single <dl>. We collapse that here so the dispatcher can
    treat all shapes uniformly.
    """
    return _skills_grid(items)


# ──────────────────────────────────────────────────────────────────────
#  RENDERERS — shape → render function. Used by render_body() to
#  dispatch each section to the right HTML builder.
#
#  # 🤖 ADD-A-SHAPE-HERE
#  To add a new visual shape: write a render function below, register it
#  here, and add a matching JS renderer in
#  ``tools/editor/static/form.js:SHAPE_RENDERERS``.
# ──────────────────────────────────────────────────────────────────────

# Per-item renderers (called once per entry in the section's list).
_PER_ITEM_RENDERERS: dict[str, callable] = {
    "experience":  _experience_item,
    "education":   _education_item,
    "compact":     _compact_row,
    "publication": _publication_item,
}

# Whole-list renderers (called once with the full items list).
_WHOLE_LIST_RENDERERS: dict[str, callable] = {
    "skills": _skills_section,
}


def _section(slug: str, title: str, body_html: str) -> str:
    safe_slug = _section_id(slug)
    return f"""\
<section id="sec-{safe_slug}">
  <h2 class="section-title">{_esc(title)}</h2>
  {body_html}
</section>"""


# Top-level YAML keys that are NOT sections. Anything else with a list
# value gets rendered as a custom section.
_RESERVED_TOP_LEVEL = frozenset({
    "name", "contact", "accent", "font", "density", "template", "paper", "photo",
    "section_order", "optional_sections", "contact_layout", "header_layout", "photo_layout",
})


def detect_shape(items: list[Any]) -> str:
    """Infer the best render shape for a custom section's items.

    Used when the user adds a new section to ``cv.yaml`` without
    registering it in ``sections.py``. Looks at the first item's keys
    and picks the closest shape; falls back to ``compact``.
    """
    if not items or not isinstance(items[0], dict):
        return "compact"
    keys = set(items[0].keys())
    if {"role", "company"} <= keys:
        return "experience"
    if {"degree", "school"} <= keys:
        return "education"
    if {"label", "items"} <= keys and len(keys) <= 3:
        return "skills"
    if "authors" in keys or {"venue", "title"} <= keys:
        return "publication"
    return "compact"


def humanize_key(key: str) -> str:
    """Turn a YAML key like ``speaking_engagements`` into ``Speaking Engagements``."""
    cleaned = key.replace("_", " ").replace("-", " ").strip()
    if not cleaned:
        return key
    # Title-case words, but keep small words (and, of, the) lowercase
    # except the first word.
    SMALL = {"and", "of", "the", "to", "in", "on", "at", "for"}
    parts = cleaned.split()
    out = [parts[0].capitalize()]
    for w in parts[1:]:
        out.append(w.lower() if w.lower() in SMALL else w.capitalize())
    return " ".join(out)


def _render_sections(cv: dict[str, Any]) -> list[str]:
    from .sections import DEFAULT_SECTIONS

    parts: list[str] = []
    rendered_keys: set[str] = set()

    for section in _ordered_default_sections(cv, DEFAULT_SECTIONS):
        items = cv.get(section.key)
        rendered_keys.add(section.key)
        if not items:
            continue
        # Skip incomplete items (required fields blank/missing). Strict
        # validation (engine/build.py) rejects these before render; the
        # editor loads leniently so half-finished form entries autosave —
        # they simply don't appear in the preview until completed.
        if section.required_fields:
            items = [
                it
                for it in items
                if not isinstance(it, dict)
                or all(
                    it.get(f) is not None
                    and (not isinstance(it.get(f), str) or it.get(f).strip())
                    for f in section.required_fields
                )
            ]
            if not items:
                continue
        body_html = _render_section_body(section.shape, items)
        parts.append(_section(section.key, section.label, body_html))

    # Custom sections — anything else the user added.
    for key, items in cv.items():
        if key in rendered_keys or key in _RESERVED_TOP_LEVEL:
            continue
        # A case-variant of a registered/reserved key (``Experience:`` next
        # to ``experience:``) would render a duplicate heading — the
        # canonical key wins. A lone ``Experience:`` (no canonical key in
        # the doc) still renders, preserving prior behaviour.
        canonical = key.lower()
        if canonical != key and (
            canonical in cv or canonical in _RESERVED_TOP_LEVEL
        ):
            continue
        if not isinstance(items, list) or not items:
            continue
        shape = detect_shape(items)
        label = humanize_key(key)
        body_html = _render_section_body(shape, items)
        parts.append(_section(key, label, body_html))

    return parts


def render_body(cv: dict[str, Any], density: str = "normal", renderer: str = "standard") -> str:
    """Return the inner HTML of the page (everything inside <main>).

    Iterates the registered sections in order, then any custom top-level
    keys the user added directly to the YAML. Each section dispatches to
    the renderer matching its ``shape`` (inferred for custom keys).

    Skips sections with no items.
    """
    sections = _render_sections(cv)
    if renderer == "rail":
        return "\n".join([
            _header(cv),
            '<div class="layout-rail" data-renderer="rail">',
            *sections,
            "</div>",
        ])
    return "\n".join([_header(cv), *sections])


def _ordered_default_sections(cv: dict[str, Any], defaults: list[Any]) -> list[Any]:
    by_key = {section.key: section for section in defaults}
    raw_order = cv.get("section_order")
    if not isinstance(raw_order, list):
        return defaults
    ordered: list[Any] = []
    seen: set[str] = set()
    for item in raw_order:
        key = str(item).strip() if item is not None else ""
        if key in by_key and key not in seen:
            ordered.append(by_key[key])
            seen.add(key)
    ordered.extend(section for section in defaults if section.key not in seen)
    return ordered


def _render_section_body(shape: str, items: list[Any]) -> str:
    """Pick the renderer (whole-list vs per-item) and produce the body HTML."""
    whole_list = _WHOLE_LIST_RENDERERS.get(shape)
    if whole_list is not None:
        return whole_list(items)
    per_item = _PER_ITEM_RENDERERS.get(shape, _compact_row)
    return "".join(per_item(i) for i in items)


def render_document(cv: dict[str, Any], css: str, density: str = "normal") -> str:
    """Return a full HTML document ready for WeasyPrint or browser preview."""
    name = _esc(cv["name"])
    template = template_registry.normalize_template(cv.get("template"))
    spec = template_registry.template_spec(template)
    body = render_body(cv, density, renderer=spec.renderer)
    paper = _esc(str(cv.get("paper") or "white"))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{name} — CV</title>
<style>{css}</style>
</head>
<body class="template-{template} paper-{paper}">
<main class="page density-{density}" data-template="{template}" data-renderer="{spec.renderer}" data-paper="{paper}">
{body}
</main>
</body>
</html>"""
