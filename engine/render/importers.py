"""Import CV data from external formats into this tool's YAML shape.

Two paths are supported:

  * ``from_rendercv(yaml_str)`` — deterministic conversion from a
    ``rendercv`` YAML file (https://docs.rendercv.com). Maps fields into
    our schema, attaches brand-icon hints to social networks.
  * ``from_plain_text(text)`` — heuristic parse of an arbitrary resume
    text (pasted from a Word/PDF resume). Best-effort: detects section
    headings (EXPERIENCE / EDUCATION / SKILLS / PROJECTS), date ranges,
    bullet points. The output is meant to be reviewed and cleaned up.

Both return a dict in our schema, ready to be ``yaml.safe_dump``'d into
``content/cv.yaml``.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from .yaml_guard import reject_aliased_graphs as _reject_aliased_graphs


# ---- rendercv import --------------------------------------------------------

# Map rendercv social-network names → our canonical keys (already lower-cased
# in brand_icons.normalise — but keeping a copy here keeps the importer
# self-contained).
RENDERCV_NETWORK_MAP: dict[str, str] = {
    "linkedin":     "linkedin",
    "github":       "github",
    "gitlab":       "gitlab",
    "imdb":         "imdb",
    "instagram":    "instagram",
    "orcid":        "orcid",
    "mastodon":     "mastodon",
    "stackoverflow":"stackoverflow",
    "researchgate": "researchgate",
    "youtube":      "youtube",
    "googlescholar":"googlescholar",
    "google scholar":"googlescholar",
    "telegram":     "telegram",
    "whatsapp":     "whatsapp",
    "leetcode":     "leetcode",
    "x":            "x",
    "twitter":      "x",
    "bluesky":      "bluesky",
    "reddit":       "reddit",
}


def from_rendercv(yaml_str: str) -> dict[str, Any]:
    """Convert a rendercv YAML to our schema.

    Args:
        yaml_str: Raw rendercv YAML (top-level ``cv:`` key required).

    Returns:
        Dict in our schema (``name``, ``contact``, ``experience``,
        ``education``, ``skills``, ``projects``, ``leadership``, ``others``).
    """
    raw = yaml.safe_load(yaml_str)
    _reject_aliased_graphs(raw)
    if not isinstance(raw, dict):
        raise ValueError("Top-level YAML must be a mapping.")

    cv = raw.get("cv") or raw  # support both "cv:" wrapped and flat formats
    if not isinstance(cv, dict):
        raise ValueError("Could not find 'cv:' block in the YAML.")

    out: dict[str, Any] = {
        "name": str(cv.get("name") or "").strip() or "Your Name",
        "accent": "#111111",
        "font": "serif",
    }

    contact: list[dict[str, Any]] = []
    if cv.get("email"):
        contact.append({"network": "mail", "username": str(cv["email"])})
    if cv.get("phone"):
        contact.append({"network": "phone", "username": str(cv["phone"])})
    if cv.get("website"):
        url = str(cv["website"])
        label = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
        contact.append({"network": "web", "username": url, "label": label})
    for sn in cv.get("social_networks") or []:
        if not isinstance(sn, dict):
            continue
        name = str(sn.get("network") or "").strip().lower()
        username = str(sn.get("username") or "").strip()
        if not username:
            continue
        canonical = RENDERCV_NETWORK_MAP.get(name) or RENDERCV_NETWORK_MAP.get(name.replace(" ", ""))
        item: dict[str, Any] = {"username": username}
        if canonical:
            item["network"] = canonical
            item["label"] = _NETWORK_LABEL.get(canonical, canonical.title())
        else:
            item["label"] = sn.get("network") or username
        contact.append(item)
    if not contact:
        contact.append({"label": "you@example.com", "href": "mailto:you@example.com"})
    out["contact"] = contact

    # rendercv organises sections under ``cv.sections``; we iterate the
    # registry and map by ``rendercv_aliases``.
    from .sections import DEFAULT_SECTIONS

    sections = cv.get("sections") or {}
    if not isinstance(sections, dict):
        raise ValueError("'sections' must be a mapping of section name to entries.")
    for s in DEFAULT_SECTIONS:
        aliases = list(s.rendercv_aliases) or [s.key]
        if s.shape == "experience":
            out[s.key] = _convert_experience(sections, aliases)
        elif s.shape == "education":
            out[s.key] = _convert_education(sections, aliases)
        elif s.shape == "skills":
            out[s.key] = _convert_skills(sections, aliases)
        elif s.shape == "publication":
            out[s.key] = _convert_publications(sections, aliases)
        else:  # compact and any unknown shapes
            out[s.key] = _convert_compact(sections, aliases)

    return _drop_empty(out)


def _section(sections: dict[str, Any], names: list[str]) -> list[Any]:
    for n in names:
        if n in sections and sections[n]:
            return sections[n]
        if n.replace("_", " ") in sections and sections[n.replace("_", " ")]:
            return sections[n.replace("_", " ")]
    return []


def _convert_experience(sections: dict[str, Any], aliases: list[str] | None = None) -> list[dict[str, Any]]:
    items = _section(sections, aliases or ["experience", "work_experience", "professional_experience"])
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        start, end = _split_dates(it.get("date"), it.get("start_date"), it.get("end_date"))
        bullets = list(it.get("highlights") or it.get("bullets") or [])
        out.append(
            {
                "role": it.get("position") or it.get("title") or it.get("role") or "Role",
                "company": it.get("company") or it.get("institution") or it.get("organisation") or "Company",
                "location": it.get("location") or "",
                "start": start,
                "end": end,
                "bullets": [str(b) for b in bullets if b],
            }
        )
    return out


def _convert_education(sections: dict[str, Any], aliases: list[str] | None = None) -> list[dict[str, Any]]:
    items = _section(sections, aliases or ["education"])
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        start, end = _split_dates(it.get("date"), it.get("start_date"), it.get("end_date"))
        degree_parts: list[str] = []
        for k in ("degree", "study_type", "area"):
            v = it.get(k)
            if v:
                degree_parts.append(str(v))
        out.append(
            {
                "degree": " — ".join(dict.fromkeys(degree_parts)) or "Degree",
                "school": it.get("institution") or it.get("school") or "Institution",
                "location": it.get("location") or "",
                "start": start,
                "end": end,
                "note": it.get("note") or (it.get("highlights") or [""])[0],
            }
        )
    return out


def _convert_skills(sections: dict[str, Any], aliases: list[str] | None = None) -> list[dict[str, str]]:
    items = _section(sections, aliases or ["skills", "technical_skills"])
    out: list[dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        label = it.get("label") or it.get("category") or it.get("name") or "Skills"
        details = it.get("details") or it.get("items")
        if isinstance(details, list):
            details = ", ".join(str(d) for d in details if d)
        out.append({"label": str(label), "items": str(details or "")})
    return out


def _convert_publications(sections: dict[str, Any], names: list[str]) -> list[dict[str, str]]:
    """Convert rendercv-style publication entries to our schema.

    rendercv's PublicationEntry uses ``title``, ``authors``, ``doi``,
    ``journal`` (which maps to our ``venue``), and ``date``. We accept
    both that shape and the more generic ``{title, date, desc}`` shape
    that the heuristic parser produces.
    """
    items = _section(sections, names)
    out: list[dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        authors = it.get("authors")
        if isinstance(authors, list):
            authors = ", ".join(str(a) for a in authors if a)
        out.append({
            "title":   str(it.get("title") or it.get("name") or "Untitled"),
            "authors": str(authors or ""),
            "venue":   str(it.get("venue") or it.get("journal") or ""),
            "date":    str(it.get("date") or _join_dates(it.get("start_date"), it.get("end_date"))),
            "doi":     str(it.get("doi") or ""),
            "url":     str(it.get("url") or ""),
        })
    return out


def _convert_compact(sections: dict[str, Any], names: list[str]) -> list[dict[str, str]]:
    items = _section(sections, names)
    out: list[dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            # OneLineEntry-style: bare string
            if isinstance(it, str) and it.strip():
                out.append({"title": it.strip(), "date": "", "desc": ""})
            continue
        title = (
            it.get("name")
            or it.get("title")
            or it.get("label")
            or it.get("position")
            or "Item"
        )
        date_str = it.get("date") or _join_dates(it.get("start_date"), it.get("end_date"))
        desc = it.get("summary") or ""
        if not desc:
            highlights = it.get("highlights") or it.get("details") or []
            if highlights:
                desc = "; ".join(str(h) for h in highlights if h)
        out.append({"title": str(title), "date": str(date_str or ""), "desc": str(desc)})
    return out


def _split_dates(date_str: Any, start: Any, end: Any) -> tuple[str, str]:
    if start or end:
        return (str(start or ""), str(end or "Present"))
    if not date_str:
        return ("", "")
    s = str(date_str).strip()
    # rendercv uses "2022-03 to 2024-06" or "2022 to present"
    m = re.split(r"\s+(?:to|–|—|-)\s+", s, maxsplit=1)
    if len(m) == 2:
        return (m[0].strip(), m[1].strip())
    return (s, "")


def _join_dates(start: Any, end: Any) -> str:
    if start and end:
        return f"{start} – {end}"
    return str(start or end or "")


def _drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# ---- plain-text heuristic parser --------------------------------------------

# Headings we'll snap to (case-insensitive whole-line match).
# Derived from the section registry — adding a SectionDef with a
# ``text_header_pattern`` automatically wires it up here.
def _build_header_patterns() -> dict[str, "re.Pattern[str]"]:
    from .sections import DEFAULT_SECTIONS

    out: dict[str, re.Pattern[str]] = {}
    for s in DEFAULT_SECTIONS:
        if s.text_header_pattern:
            out[s.key] = re.compile(s.text_header_pattern, re.I)
    return out


_HEADER_PATTERNS = _build_header_patterns()

_DATE_RANGE = re.compile(
    r"\b(?P<start>(?:\d{1,2}[/.-])?\d{4})\s*(?:[-–—]|to)\s*"
    r"(?P<end>(?:\d{1,2}[/.-])?\d{4}|present|current|now)\b",
    re.I,
)

# Pretty labels for known networks — restore proper casing.
_NETWORK_LABEL: dict[str, str] = {
    "linkedin":      "LinkedIn",
    "github":        "GitHub",
    "gitlab":        "GitLab",
    "x":             "X",
    "bluesky":       "Bluesky",
    "googlescholar": "Scholar",
    "orcid":         "ORCID",
    "stackoverflow": "Stack Overflow",
    "leetcode":      "LeetCode",
    "mastodon":      "Mastodon",
    "youtube":       "YouTube",
    "instagram":     "Instagram",
    "telegram":      "Telegram",
    "whatsapp":      "WhatsApp",
    "reddit":        "Reddit",
    "imdb":          "IMDb",
    "researchgate":  "ResearchGate",
}


def from_plain_text(text: str) -> dict[str, Any]:
    """Heuristic parse of a plain-text resume.

    The output is intentionally conservative — we err on the side of not
    inventing fields we can't see. The user will need to clean up.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    name = _guess_name(lines)
    contact = _guess_contact(lines)
    sections = _split_sections(lines)

    out: dict[str, Any] = {
        "name": name,
        "accent": "#111111",
        "font": "serif",
        "contact": contact,
    }
    # Iterate the registry — adding a section in sections.py wires
    # plain-text parsing automatically (provided the SectionDef sets a
    # text_header_pattern + sensible shape).
    from .sections import DEFAULT_SECTIONS

    for s in DEFAULT_SECTIONS:
        chunk = sections.get(s.key, [])
        if s.shape == "experience":
            out[s.key] = _parse_experience(chunk)
        elif s.shape == "education":
            out[s.key] = _parse_education(chunk)
        elif s.shape == "skills":
            out[s.key] = _parse_skills(chunk)
        elif s.shape == "publication":
            out[s.key] = _parse_publications(chunk)
        else:  # compact / unknown
            out[s.key] = _parse_compact(chunk)
    return _drop_empty(out)


def _parse_publications(lines: list[str]) -> list[dict[str, str]]:
    """Heuristic parse of publication-style lines.

    Plain-text resumes typically write each publication on one line:
        "**Authors** (Year). Title. Venue. doi:..."
    We try to extract title + date and stuff the rest into ``authors``;
    a real Claude pass will do better.
    """
    out: list[dict[str, str]] = []
    for line in lines:
        s = line.strip(" •-*\t")
        if not s:
            continue
        date_m = re.search(r"\b\d{4}\b", s)
        date = date_m.group(0) if date_m else ""
        title = s
        out.append({"title": title, "authors": "", "venue": "", "date": date, "doi": "", "url": ""})
    return out


def _guess_name(lines: list[str]) -> str:
    for line in lines[:5]:
        s = line.strip()
        if 2 <= len(s.split()) <= 5 and not any(c in s for c in "@/:"):
            if s.replace(" ", "").replace("-", "").replace(".", "").isalpha():
                return s
    return "Your Name"


def _guess_contact(lines: list[str]) -> list[dict[str, Any]]:
    contact: list[dict[str, Any]] = []
    seen_handles: set[tuple[str, str]] = set()  # (network, username)
    # Cap the blob before regexing: the email/website patterns backtrack
    # quadratically on a long unbroken \w run with no match (a single 30KB
    # line cost ~10s). 5000 chars is far beyond any real contact block.
    blob = "\n".join(lines[:12])[:5000]

    # Email
    email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", blob)
    if email_m:
        contact.append({"network": "mail", "username": email_m.group()})
        seen_handles.add(("mail", email_m.group()))
    # Strip emails from the blob so the website regex won't match the
    # email's domain (e.g. "email.com" inside "jane@email.com").
    scrubbed = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", " ", blob)

    # Phone — accept +/digits/spaces/dashes/dots/parens; require a leading +
    # or 8+ digits to reduce false positives on year ranges like "2019–2022".
    m = re.search(r"(\+\d[\d \-./()]{6,}|\b\d{3}[ \-./]\d{3}[ \-./]\d{4}\b)", scrubbed)
    if m:
        digits = re.sub(r"[^\d+]", "", m.group())
        if 9 <= len(digits) <= 16:
            contact.append({"network": "phone", "username": digits, "label": m.group().strip()})
            seen_handles.add(("phone", digits))
            scrubbed = scrubbed.replace(m.group(), " ", 1)

    # Social networks — match patterns in order; each network can claim its
    # own username independently of others.
    patterns = {
        "linkedin":      r"linkedin\.com/in/([\w\-]+)",
        "github":        r"github\.com/([\w\-]+)",
        "gitlab":        r"gitlab\.com/([\w\-]+)",
        "x":             r"(?:x\.com|twitter\.com)/([\w\-]+)",
        "bluesky":       r"bsky\.app/profile/([\w\-.]+)",
        "googlescholar": r"scholar\.google\.com/citations\?user=([\w\-]+)",
        "orcid":         r"orcid\.org/([\d\-]+)",
        "stackoverflow": r"stackoverflow\.com/users/([\w\-]+)",
        "leetcode":      r"leetcode\.com/(?:u/)?([\w\-]+)",
        "mastodon":      r"@([\w\-]+)@([\w.\-]+)",
    }
    used_spans: list[tuple[int, int]] = []
    for net, pat in patterns.items():
        m = re.search(pat, scrubbed, re.I)
        if not m:
            continue
        username = m.group(1) if net != "mastodon" else f"{m.group(1)}@{m.group(2)}"
        if (net, username) in seen_handles:
            continue
        contact.append({"network": net, "username": username, "label": _NETWORK_LABEL.get(net, net.title())})
        seen_handles.add((net, username))
        used_spans.append(m.span())

    # Strip matched social URLs so the website regex won't catch them.
    for s, e in sorted(used_spans, reverse=True):
        scrubbed = scrubbed[:s] + " " + scrubbed[e:]

    # Bare personal site — last, picks something like "janedoe.com".
    m = re.search(r"\b(?:https?://)?([\w\-]+\.(?:dev|me|com|io|app|page|xyz|to))\b", scrubbed)
    if m:
        host = m.group(1)
        url = m.group(0) if m.group(0).startswith("http") else f"https://{host}"
        if ("web", url) not in seen_handles and host not in {"linkedin.com", "github.com"}:
            contact.append({"network": "web", "username": url, "label": host})

    return contact or [{"label": "you@example.com", "href": "mailto:you@example.com"}]


def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = "_preamble"
    sections[current] = []
    for line in lines:
        matched = None
        for name, pat in _HEADER_PATTERNS.items():
            if pat.match(line):
                matched = name
                break
        if matched:
            current = matched
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _parse_experience(lines: list[str]) -> list[dict[str, Any]]:
    return _parse_role_section(lines, role_keywords=("engineer", "scientist", "manager", "intern", "analyst", "lead", "director", "developer", "consultant"))


def _parse_education(lines: list[str]) -> list[dict[str, Any]]:
    items = _parse_role_section(lines, role_keywords=("b.sc", "m.sc", "ph.d", "phd", "bachelor", "master", "diploma", "exchange"))
    out: list[dict[str, Any]] = []
    for it in items:
        out.append(
            {
                "degree": it.get("role") or "Degree",
                "school": it.get("company") or "Institution",
                "location": it.get("location", ""),
                "start": it.get("start", ""),
                "end": it.get("end", ""),
            }
        )
    return out


def _parse_role_section(lines: list[str], role_keywords: tuple[str, ...]) -> list[dict[str, Any]]:
    """Group lines into entries around date-bearing header lines.

    Heuristic for splitting "Company — Role" headers:
      * If the head text contains a separator (" — ", " - ", " | ", or 2+
        spaces), assume "<Company> <sep> <Role>" — the FAANG/YC convention.
      * If only one part, treat it as the company and leave role empty.

    Tail text after the date range is treated as the location.
    """
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    bullets: list[str] = []

    def flush() -> None:
        nonlocal cur, bullets
        if cur:
            cur["bullets"] = [b for b in bullets if b]
            out.append(cur)
        cur = None
        bullets = []

    for line in lines:
        s = line.strip()
        if not s:
            continue
        date_m = _DATE_RANGE.search(s)
        is_header = date_m and not s.startswith(("•", "-", "*"))
        if is_header:
            flush()
            start = date_m.group("start")
            end = date_m.group("end")
            head = s[: date_m.start()].strip(" \t-—–|,")
            tail = s[date_m.end() :].strip(" \t-—–|,")
            # Split into up-to-three parts: company, role, location.
            parts = re.split(r"\s+[—–|]\s+|\s{2,}", head)
            parts = [p.strip() for p in parts if p.strip()]
            company = parts[0] if parts else ""
            role = parts[1] if len(parts) > 1 else ""
            location = parts[2] if len(parts) > 2 else (tail or "")
            cur = {
                "role": role or company or "Role",
                "company": company if role else "",
                "location": location,
                "start": start,
                "end": end,
            }
            bullets = []
        elif s.startswith(("•", "-", "*")):
            bullets.append(re.sub(r"^[•\-*]\s*", "", s))
        elif cur is not None:
            # Continuation line — append to last bullet if any.
            if bullets:
                bullets[-1] = (bullets[-1] + " " + s).strip()
            else:
                bullets.append(s)
    flush()
    return out


def _parse_skills(lines: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for line in lines:
        s = line.strip(" •-*\t")
        if not s:
            continue
        m = re.match(r"^([^:]{1,40}):\s*(.+)$", s)
        if m:
            out.append({"label": m.group(1).strip(), "items": m.group(2).strip()})
        elif "," in s and len(s.split(",")) >= 2:
            out.append({"label": "Skills", "items": s})
    return out


def _parse_compact(lines: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for line in lines:
        s = line.strip(" •-*\t")
        if not s:
            continue
        date_m = _DATE_RANGE.search(s) or re.search(r"\b\d{4}\b", s)
        date = ""
        if date_m:
            date = date_m.group(0)
            s = (s[: date_m.start()] + s[date_m.end() :]).strip(" \t-—–|,")
        if " — " in s:
            title, _, desc = s.partition(" — ")
        elif " - " in s:
            title, _, desc = s.partition(" - ")
        else:
            title, desc = s, ""
        out.append({"title": title.strip(), "date": date, "desc": desc.strip()})
    return out
