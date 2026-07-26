"""Load and validate the CV YAML source.

Schema (see ``content/cv.yaml`` for an example):

    name:        str (required)
    contact:     list[dict] (required) — entries accept either:
                   {label: str, href?: str}                   # plain
                   {network: str, username: str, label?, href?}  # branded
    accent:      str hex colour (optional, default ``#111111``)
    font:        "serif" | "sans" | "mono"  (optional, default ``serif``)
    paper:       "white" | "creme" | "soft-gray" | "blue-tint"
                 (optional, default ``white``)
    photo:       str relative path to image (optional)

Sections (defined in ``engine/render/sections.py``):
    experience, education, skills, projects, leadership, others, …

# 🤖 ADD-A-SECTION-HERE
#
# This file derives its section list from ``sections.DEFAULT_SECTIONS``.
# To add a section, edit ``engine/render/sections.py`` — nothing here
# needs to change.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import brand_icons, css_base, resource_policy, template_registry
from .sections import DEFAULT_SECTIONS
from .yaml_guard import reject_aliased_graphs


REQUIRED_TOP = ("name", "contact")
SUPPORTED_FONTS = ("serif", "sans", "mono")
SUPPORTED_DENSITIES = ("tight", "normal", "airy")
SUPPORTED_TEMPLATES = template_registry.available_template_ids()
# Section keys, derived. Used for outline ordering.
SECTION_KEYS: tuple[str, ...] = tuple(s.key for s in DEFAULT_SECTIONS)


def load(
    path: str | Path,
    *,
    strict_required: bool = True,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """Load and validate a CV YAML file.

    ``strict_required=False`` skips the per-item required-field check. The
    editor uses this so half-finished form entries (e.g. a freshly added,
    still-empty role) can autosave; the renderers skip such incomplete
    items instead of erroring. The CLI build keeps the default strict
    behaviour so authoring mistakes fail the build.
    """
    src = Path(path).read_text(encoding="utf-8")
    cv = yaml.safe_load(src) or {}
    # Alias-bomb guard: a ~450-byte CV YAML with an anchor/alias chain
    # otherwise explodes into megabytes of shared structure at render time
    # (the editor's POST /api/cv path calls this with strict_required=False).
    reject_aliased_graphs(cv)
    # Use a CONSTANT display label in validation errors, never the real file
    # name: the editor validates CV YAML via a random temp file and returns
    # ``str(exc)`` in the 400 body — even the basename (tmpXXXX.yaml) leaks
    # server internals to the client.
    path = "cv.yaml"
    if not isinstance(cv, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")

    for key in REQUIRED_TOP:
        if key not in cv or cv[key] in (None, "", []):
            raise ValueError(f"{path}: missing required field {key!r}")

    # At least one substantive section must be present. We treat the first
    # three registered sections (typically experience/education/skills) as
    # the "spine" — the CV is degenerate without at least one of them.
    spine = [s.key for s in DEFAULT_SECTIONS[:3]]
    if not any(cv.get(k) for k in spine):
        raise ValueError(
            f"{path}: at least one of {'/'.join(spine)} must be non-empty"
        )

    if not isinstance(cv["contact"], list):
        raise ValueError(f"{path}: contact must be a list of entries")
    cv["contact"] = [
        _normalise_contact(c, i, path) for i, c in enumerate(cv["contact"])
    ]

    # Per-section required-field validation, driven entirely by the
    # registry. Adding a new section in sections.py automatically wires
    # validation here.
    for section in DEFAULT_SECTIONS:
        items = cv.get(section.key) or []
        if not isinstance(items, list):
            raise ValueError(
                f"{path}: {section.key!r} must be a list (got {type(items).__name__})"
            )
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(
                    f"{path}: {section.key}[{i}] must be a mapping (got {type(item).__name__})"
                )
            for field_name in section.required_fields:
                if not strict_required:
                    break
                value = item.get(field_name)
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise ValueError(
                        f"{path}: {section.key}[{i}] missing required field {field_name!r}"
                    )
            # Per-shape extra checks (kept tight — the registry handles
            # the bulk of validation via required_fields).
            if section.shape == "experience" and "bullets" in item:
                if not isinstance(item["bullets"], list):
                    raise ValueError(
                        f"{path}: {section.key}[{i}].bullets must be a list"
                    )

    cv.setdefault("accent", "#111111")
    # Malformed accents used to persist silently in YAML and only get
    # sanitized much later in css_base._safe_accent (plan 107, F13). Keep the
    # forgiving behavior (no hard fail — an old CV must still build) but
    # normalize at load and say so.
    accent = str(cv.get("accent") or "").strip()
    safe_accent = css_base._safe_accent(accent)
    if safe_accent != accent:
        import sys

        print(
            f"{path}: accent={cv.get('accent')!r} is not a hex colour — using {safe_accent}",
            file=sys.stderr,
        )
    cv["accent"] = safe_accent
    # str(...) coercion (like accent above): a non-string font/density (a list,
    # dict, or number slipped into the YAML) must surface the FRIENDLY
    # "not supported" ValueError, not a raw AttributeError from .strip().
    font = str(cv.get("font") or "serif").strip().lower()
    if font not in SUPPORTED_FONTS:
        raise ValueError(
            f"{path}: font={font!r} not supported. Use one of {SUPPORTED_FONTS}."
        )
    cv["font"] = font
    density = str(cv.get("density") or "normal").strip().lower()
    if density not in SUPPORTED_DENSITIES:
        raise ValueError(
            f"{path}: density={density!r} not supported. Use one of {SUPPORTED_DENSITIES}."
        )
    cv["density"] = density
    template = cv.get("template", "classic")
    normalized_template = template_registry.normalize_template(template)
    if isinstance(template, str) and template.strip() and template.strip().lower() not in SUPPORTED_TEMPLATES:
        raise ValueError(
            f"{path}: template={template!r} not supported. Use one of {SUPPORTED_TEMPLATES}."
        )
    cv["template"] = normalized_template
    cv["paper"] = css_base.normalize_paper(cv.get("paper"))
    if "photo" in cv:
        cv["photo"] = _validate_photo(cv.get("photo"), path, owner_id=owner_id)
    return cv


def _validate_photo(
    photo: Any,
    path: str | Path,
    *,
    owner_id: str | None = None,
) -> Any:
    """Validate every photo source before it can reach a renderer.

    The editor gateway validates photos on SAVE (tools/editor/server.py
    ``_validate_cv_photo_value``), but the engine path (CLI build, preview of
    a hand-edited YAML) also needs to enforce the boundary. Inline ``data:``
    images must be bounded raster data. File-backed images must be canonical,
    repository-relative raster paths. When ``owner_id`` is supplied, only that
    exact owner's uploaded ``design/photos/<owner>/photo.*`` path is accepted.
    """
    if photo is None or photo == "":
        return photo
    if not isinstance(photo, str):
        raise ValueError(f"{path}: photo: photo source must be a string")
    value = photo.strip()
    if not value:
        return ""
    try:
        import image_guard  # tools/editor is on sys.path in the app/tests
    except ImportError:  # CLI path: only engine/ is on sys.path
        import sys

        editor_dir = Path(__file__).resolve().parents[2] / "tools" / "editor"
        if not editor_dir.is_dir():
            raise ValueError(
                f"{path}: photo validation is unavailable — the editor's "
                "image_guard module was not found (engine running standalone "
                "without tools/editor)."
            ) from None
        # append (not insert(0)): a permanent front-of-path entry would
        # shadow same-named modules process-wide for the rest of the run.
        sys.path.append(str(editor_dir))
        import image_guard
    if image_guard.is_data_uri(value):
        try:
            image_guard.validate_data_uri_image(value)
        except image_guard.ImageRejected as exc:
            raise ValueError(f"{path}: photo: {exc}") from exc
        return value
    try:
        return resource_policy.validate_relative_photo_source(
            value, owner_id=owner_id
        )
    except resource_policy.ResourceBlocked as exc:
        raise ValueError(f"{path}: photo: {exc}") from exc


def _normalise_contact(c: Any, idx: int, path: str | Path) -> dict[str, Any]:
    """Coerce a contact entry into ``{network, label, href, icon_svg}`` shape.

    Accepts:
      - ``{label, href?}``                          legacy shape
      - ``{network, username, label?, href?}``      rendercv-style shape
      - ``{network, label, href?}``                 minimal with explicit label
    """
    if not isinstance(c, dict):
        raise ValueError(f"{path}: contact[{idx}] must be a mapping")

    network = c.get("network") or c.get("kind") or ""
    username = c.get("username") or c.get("user") or ""
    label = c.get("label")
    href = c.get("href")

    if not (label or username or href):
        raise ValueError(
            f"{path}: contact[{idx}] needs at least 'label', 'username', or 'href'"
        )

    canonical = brand_icons.normalise(network) if network else None
    icon_svg = brand_icons.svg_for(network) if network else ""
    final_href = (
        brand_icons.url_for(network, username, href)
        if (network or href)
        else (href or "")
    )
    final_label = (
        brand_icons.label_for(network, username, label)
        if network
        else (label or username or final_href)
    )

    return {
        "network": canonical or network or "",
        "label": final_label,
        "href": final_href,
        "icon_svg": icon_svg,
    }


_OUTLINE_RESERVED_TOP_LEVEL = frozenset(
    {
        "name", "contact", "accent", "font", "density", "template", "photo",
        "section_order", "optional_sections", "contact_layout", "header_layout", "photo_layout",
    }
)


def section_outline(cv: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Return outline entries (slug + label) in render order.

    Always includes a "Header" entry first, then the configured sections,
    then any custom top-level keys the user added directly to the YAML
    (so a freshly typed ``awards:`` section appears in the sidebar
    immediately on save).
    """
    out = [{"slug": "header", "label": "Header"}]
    registered = {s.key for s in DEFAULT_SECTIONS}
    for section in DEFAULT_SECTIONS:
        out.append({"slug": section.key, "label": section.label})
    if cv:
        # Import lazily to avoid a circular dependency.
        from .templates import humanize_key

        for key, value in cv.items():
            if key in registered or key in _OUTLINE_RESERVED_TOP_LEVEL:
                continue
            # A case-variant of a registered/reserved key (``Experience:``
            # next to ``experience:``) would show a duplicate outline entry —
            # the canonical key wins. A lone ``Experience:`` (no canonical
            # key in the doc) still appears, preserving prior behaviour.
            canonical = key.lower()
            if canonical != key and (
                canonical in cv or canonical in _OUTLINE_RESERVED_TOP_LEVEL
            ):
                continue
            if not isinstance(value, list):
                continue
            out.append({"slug": key, "label": humanize_key(key), "custom": True})
    return out
