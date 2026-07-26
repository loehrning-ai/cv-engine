"""Inline neutral SVG symbols for CV contact links.

Network names remain text labels. No third-party brand marks are redistributed.
Social profiles use Lucide's neutral external-link glyph; mail, phone, web,
and generic links use the corresponding Lucide 1.26.0 glyph. See
`LICENSES/icon-assets.lock.json` and the bundled Lucide license.
"""
from __future__ import annotations

# Exact inner SVG geometry extracted from lucide-static 1.26.0. Styling is
# applied on the outer SVG element in `svg_for`.
_EXTERNAL_LINK = (
    '<path d="M15 3h6v6"/>'
    '<path d="M10 14 21 3"/>'
    '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>'
)

_GENERIC_OUTLINE: dict[str, str] = {
    "mail": (
        '<path d="m22 7-8.991 5.727a2 2 0 0 1-2.009 0L2 7"/>'
        '<rect x="2" y="4" width="20" height="16" rx="2"/>'
    ),
    "phone": (
        '<path d="M13.832 16.568a1 1 0 0 0 1.213-.303l.355-.465A2 2 0 0 1 17 15h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2A18 18 0 0 1 2 4a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v3a2 2 0 0 1-.8 1.6l-.468.351a1 1 0 0 0-.292 1.233 14 14 0 0 0 6.392 6.384"/>'
    ),
    "web": (
        '<circle cx="12" cy="12" r="10"/>'
        '<path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/>'
        '<path d="M2 12h20"/>'
    ),
    "link": (
        '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>'
        '<path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'
    ),
}

_SOCIAL_NETWORKS = frozenset(
    {
        "linkedin",
        "github",
        "gitlab",
        "x",
        "mastodon",
        "bluesky",
        "instagram",
        "youtube",
        "telegram",
        "whatsapp",
        "reddit",
        "stackoverflow",
        "leetcode",
        "orcid",
        "googlescholar",
        "researchgate",
        "imdb",
    }
)

# Friendly aliases — accept several spellings of the network name.
ALIASES: dict[str, str] = {
    "linked-in": "linkedin",
    "li": "linkedin",
    "gh": "github",
    "twitter": "x",
    "scholar": "googlescholar",
    "google scholar": "googlescholar",
    "google-scholar": "googlescholar",
    "research-gate": "researchgate",
    "stack-overflow": "stackoverflow",
    "stack overflow": "stackoverflow",
    "stackexchange": "stackoverflow",
    "leet-code": "leetcode",
    "ig": "instagram",
    "yt": "youtube",
    "tg": "telegram",
    "wa": "whatsapp",
    "bsky": "bluesky",
    "email": "mail",
    "e-mail": "mail",
    "tel": "phone",
    "website": "web",
    "homepage": "web",
    "url": "link",
}


# URL templates used when only a username/handle is provided
URL_TEMPLATES: dict[str, str] = {
    "linkedin":     "https://www.linkedin.com/in/{}",
    "github":       "https://github.com/{}",
    "gitlab":       "https://gitlab.com/{}",
    "x":            "https://x.com/{}",
    "twitter":      "https://x.com/{}",
    "mastodon":     "https://{}",            # mastodon usernames include the host
    "bluesky":      "https://bsky.app/profile/{}",
    "instagram":    "https://www.instagram.com/{}",
    "youtube":      "https://www.youtube.com/@{}",
    "telegram":     "https://t.me/{}",
    "whatsapp":     "https://wa.me/{}",
    "reddit":       "https://www.reddit.com/user/{}",
    "stackoverflow":"https://stackoverflow.com/users/{}",
    "leetcode":     "https://leetcode.com/u/{}",
    "orcid":        "https://orcid.org/{}",
    "googlescholar":"https://scholar.google.com/citations?user={}",
    "researchgate": "https://www.researchgate.net/profile/{}",
    "imdb":         "https://www.imdb.com/name/{}",
    "mail":         "mailto:{}",
    "phone":        "tel:{}",
    "web":          "https://{}",
    "link":         "https://{}",
}


SUPPORTED_NETWORKS = sorted(_SOCIAL_NETWORKS | set(_GENERIC_OUTLINE))


# All social networks use the same neutral ink because the rendered symbols are
# navigational, not brand marks. Web and generic link symbols use the project
# accent. `BRAND_COLOR` remains as a compatibility alias.
ICON_COLOR: dict[str, str] = {key: "#374151" for key in _SOCIAL_NETWORKS}
ICON_COLOR.update(
    {
        "mail": "#374151",
        "phone": "#374151",
        "web": "#0866FF",
        "link": "#0866FF",
    }
)
BRAND_COLOR = ICON_COLOR


def color_for(name: str) -> str | None:
    """Return the configured icon colour for a known network, else None."""
    key = normalise(name)
    return ICON_COLOR.get(key) if key else None


def normalise(name: str) -> str | None:
    """Map a user-supplied network string to a canonical icon key, or None."""
    if not name:
        return None
    key = name.strip().lower().replace("_", "")
    key = ALIASES.get(key, key)
    if key in _SOCIAL_NETWORKS or key in _GENERIC_OUTLINE:
        return key
    # Allow space/dash variants
    flat = key.replace(" ", "").replace("-", "")
    return flat if flat in _SOCIAL_NETWORKS or flat in _GENERIC_OUTLINE else None


def svg_for(name: str, size: int = 12, *, mono: bool = False) -> str:
    """Return an inline SVG string for the named icon, or empty if unknown.

    Args:
        name: Network identifier (canonical or alias).
        size: Icon size in pixels (or pt, since we use the same number for both).
        mono: If True, fill with ``currentColor`` for a monochrome look.
            Default False — the icon uses its configured neutral or project accent colour.
    """
    key = normalise(name)
    if key is None:
        return ""
    color = "currentColor" if mono else (ICON_COLOR.get(key) or "currentColor")
    body = _EXTERNAL_LINK if key in _SOCIAL_NETWORKS else _GENERIC_OUTLINE[key]
    return (
        f'<svg class="brand-icon" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
        f'stroke="{color}" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{body}</svg>'
    )


def url_for(network: str, username: str | None = None, href: str | None = None) -> str:
    """Resolve the final URL for a contact entry.

    Priority: explicit ``href`` > template applied to ``username`` > empty.
    """
    if href:
        return href
    if not username:
        return ""
    key = normalise(network) or network.strip().lower()
    template = URL_TEMPLATES.get(key)
    return template.format(username) if template else username


def label_for(network: str, username: str, label: str | None = None) -> str:
    """Pick the human-readable label for a contact entry.

    Priority: explicit ``label`` > username (or stripped variant) > network.
    """
    if label:
        return label
    if username:
        key = normalise(network) or ""
        # For mail/phone/web, show the actual address; for socials, show the
        # handle (more recognisable than the full URL).
        if key in ("mail", "phone"):
            return username
        return username
    return network
