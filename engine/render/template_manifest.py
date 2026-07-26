"""External template manifest guardrails.

This module validates metadata for manually reviewed external templates. It
does not fetch remote code and it does not execute template assets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


COMPATIBILITY_VERSION = "1"
ALLOWED_RENDERERS = {"standard", "rail"}
ALLOWED_STYLESHEETS = {"cv"}
ALLOWED_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
ALLOWED_LAYOUT_FAMILIES = {"single_column", "two_column", "rail", "compact"}
ALLOWED_ATS_PRIORITIES = {"high", "medium", "low"}
ALLOWED_CUSTOMIZATION_AXES = {"accent", "font", "density", "paper", "section_order"}
ALLOWED_PAPER_SIZES = {"A4"}
EXECUTABLE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".sh", ".bash", ".zsh", ".fish", ".ps1"}
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,48}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class TemplateManifest:
    id: str
    label: str
    description: str
    renderer: str
    stylesheet: str
    thumbnail: str
    source_url: str
    commit: str
    license: str
    compatibility_version: str
    assets: tuple[dict[str, str], ...]
    layout_family: str
    column_count: int
    inspiration_source: str
    ats_priority: str
    customization_axes: tuple[str, ...]
    paper_sizes: tuple[str, ...]
    import_export_notes: str


def _text(data: dict[str, Any], key: str, *, required: bool = True) -> str:
    value = data.get(key)
    if value is None:
        if required:
            raise ValueError(f"missing {key}")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"missing {key}")
    return value


def _safe_relative_path(path: str, *, field: str) -> str:
    clean = path.strip().replace("\\", "/")
    if not clean or clean.startswith("/") or "://" in clean:
        raise ValueError(f"{field} must be a relative path")
    parts = [part for part in clean.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError(f"{field} must not traverse directories")
    suffix = "." + parts[-1].rsplit(".", 1)[-1].lower() if "." in parts[-1] else ""
    if suffix in EXECUTABLE_SUFFIXES:
        raise ValueError(f"{field} must not be executable code")
    return "/".join(parts)


def _choice(data: dict[str, Any], key: str, allowed: set[str], default: str) -> str:
    value = _text(data, key, required=False) or default
    if value not in allowed:
        raise ValueError(f"{key} is not approved")
    return value


def _column_count(data: dict[str, Any]) -> int:
    value = data.get("column_count", 1)
    if isinstance(value, bool) or value not in (1, 2):
        raise ValueError("column_count must be 1 or 2")
    return int(value)


def _string_list(data: dict[str, Any], key: str, allowed: set[str], default: tuple[str, ...]) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or not value:
        raise ValueError(f"{key} must be a non-empty list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{key} entries must be strings")
        clean = item.strip()
        if clean not in allowed:
            raise ValueError(f"{key} entry is not approved")
        if clean not in out:
            out.append(clean)
    return tuple(out)


def validate_pinned_github_raw_url(url: str, commit: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com":
        raise ValueError("source_url must use https://raw.githubusercontent.com")
    if not _SHA_RE.match(commit):
        raise ValueError("commit must be a pinned 40-character lowercase SHA")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        raise ValueError("source_url must include owner/repo/commit/path")
    if parts[2] != commit:
        raise ValueError("source_url commit segment must match commit")
    return url


def _validate_assets(value: Any) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("assets must be a list")
    out: list[dict[str, str]] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"assets[{i}] must be an object")
        path = _safe_relative_path(_text(item, "path"), field=f"assets[{i}].path")
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if suffix not in ALLOWED_ASSET_SUFFIXES:
            raise ValueError(f"assets[{i}].path has unsupported suffix")
        digest = _text(item, "sha256")
        if not _HASH_RE.match(digest):
            raise ValueError(f"assets[{i}].sha256 must be sha256:<64 hex>")
        out.append({"path": path, "sha256": digest})
    return tuple(out)


def validate_manifest(data: dict[str, Any], *, known_ids: set[str] | None = None) -> TemplateManifest:
    if not isinstance(data, dict):
        raise ValueError("manifest must be an object")
    template_id = _text(data, "id")
    if not _ID_RE.match(template_id):
        raise ValueError("id must be a slug")
    if known_ids and template_id in known_ids:
        raise ValueError("id duplicates an existing template")
    renderer = _text(data, "renderer")
    if renderer not in ALLOWED_RENDERERS:
        raise ValueError("renderer is not approved")
    stylesheet = _text(data, "stylesheet")
    if stylesheet not in ALLOWED_STYLESHEETS:
        raise ValueError("stylesheet is not approved")
    commit = _text(data, "commit").lower()
    source_url = validate_pinned_github_raw_url(_text(data, "source_url"), commit)
    license_id = _text(data, "license")
    compatibility = _text(data, "compatibility_version")
    if compatibility != COMPATIBILITY_VERSION:
        raise ValueError("compatibility_version is unsupported")
    thumbnail = _safe_relative_path(_text(data, "thumbnail"), field="thumbnail")
    thumb_suffix = "." + thumbnail.rsplit(".", 1)[-1].lower() if "." in thumbnail else ""
    if thumb_suffix not in ALLOWED_ASSET_SUFFIXES:
        raise ValueError("thumbnail has unsupported suffix")
    inspiration_source = _text(data, "inspiration_source", required=False) or "Reviewed external manifest metadata"
    if "://" in inspiration_source:
        raise ValueError("inspiration_source must not be a URL")
    import_export_notes = _text(data, "import_export_notes", required=False) or "External import remains metadata-only."
    return TemplateManifest(
        id=template_id,
        label=_text(data, "label"),
        description=_text(data, "description"),
        renderer=renderer,
        stylesheet=stylesheet,
        thumbnail=thumbnail,
        source_url=source_url,
        commit=commit,
        license=license_id,
        compatibility_version=compatibility,
        assets=_validate_assets(data.get("assets")),
        layout_family=_choice(data, "layout_family", ALLOWED_LAYOUT_FAMILIES, "single_column"),
        column_count=_column_count(data),
        inspiration_source=inspiration_source,
        ats_priority=_choice(data, "ats_priority", ALLOWED_ATS_PRIORITIES, "high"),
        customization_axes=_string_list(
            data,
            "customization_axes",
            ALLOWED_CUSTOMIZATION_AXES,
            ("accent", "font", "density", "paper", "section_order"),
        ),
        paper_sizes=_string_list(data, "paper_sizes", ALLOWED_PAPER_SIZES, ("A4",)),
        import_export_notes=import_export_notes,
    )
