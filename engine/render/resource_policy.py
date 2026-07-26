"""Fail-closed resource policy for WeasyPrint renders.

The renderer consumes user-controlled CV YAML.  WeasyPrint's default fetcher
supports network and arbitrary ``file:`` URLs, so handing it an unbounded
``<img src>`` turns PDF generation into an SSRF/local-file-read primitive.

This module keeps the allowed resource set deliberately small:

* ``data:`` resources (the CV photo is validated as a bounded raster first);
* the repository's exact, versioned font files; and
* the one exact, already-validated repository-relative photo path in the CV.

Everything else, including every network scheme and arbitrary local file, is
rejected before WeasyPrint's default fetcher is invoked.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Callable, Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname


REPO_ROOT = Path(__file__).resolve().parents[2]

_FONT_RELATIVE_PATHS = frozenset(
    {
        "design/fonts/Inter-Light.ttf",
        "design/fonts/Inter-Regular.ttf",
        "design/fonts/Inter-Medium.ttf",
        "design/fonts/Inter-SemiBold.ttf",
        "design/fonts/Inter-Bold.ttf",
        "design/fonts/SourceSerif4-Regular.ttf",
        "design/fonts/SourceSerif4-It.ttf",
        "design/fonts/SourceSerif4-Semibold.ttf",
        "design/fonts/SourceSerif4-SemiboldIt.ttf",
        "design/fonts/SourceSerif4-Bold.ttf",
    }
)
_RASTER_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_OWNER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class ResourceBlocked(ValueError):
    """Raised before a disallowed resource can reach WeasyPrint's fetcher."""


def repo_base_url(repo_root: Path | None = None) -> str:
    """Canonical ``file:`` base URL for controlled repository resources."""
    return (repo_root or REPO_ROOT).resolve().as_uri()


def is_data_resource(value: str) -> bool:
    """Return whether ``value`` uses the case-insensitive ``data:`` scheme."""
    return isinstance(value, str) and value.lstrip()[:5].lower() == "data:"


def _validated_owner(owner_id: str | None) -> str | None:
    if owner_id is None or owner_id == "":
        return None
    if not isinstance(owner_id, str) or not _OWNER_RE.fullmatch(owner_id):
        raise ResourceBlocked("authenticated owner id is not a safe path segment")
    return owner_id


def validate_relative_photo_source(
    source: str,
    *,
    owner_id: str | None = None,
) -> str:
    """Validate and normalize a non-``data:`` CV photo source.

    Without an authenticated owner, local CLI use may reference one safe,
    repository-relative raster path.  With an owner, the only accepted shape
    is ``design/photos/<exact-owner>/photo.(png|jpg|jpeg|webp)``.
    """
    if not isinstance(source, str):
        raise ResourceBlocked("photo source must be a string")
    value = source.strip()
    if not value:
        raise ResourceBlocked("photo source is empty")
    if "\x00" in value:
        raise ResourceBlocked("photo source contains a NUL byte")
    if "\\" in value or _WINDOWS_ABSOLUTE_RE.match(value):
        raise ResourceBlocked("photo source must use a repository-relative POSIX path")
    if value.startswith(("/", "//", "~")):
        raise ResourceBlocked("absolute photo paths are not allowed")

    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        raise ResourceBlocked("photo URL schemes are not allowed")
    if parsed.query or parsed.fragment:
        raise ResourceBlocked("photo query strings and fragments are not allowed")

    decoded = unquote(parsed.path)
    if decoded != parsed.path:
        # Canonical paths only.  Rejecting encoded separators/dot segments
        # avoids a second decoder interpreting a different resource later.
        raise ResourceBlocked("percent-encoded photo paths are not allowed")

    relative = PurePosixPath(decoded)
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ResourceBlocked("photo path traversal is not allowed")
    if relative.is_absolute():
        raise ResourceBlocked("absolute photo paths are not allowed")
    if relative.suffix.lower() not in _RASTER_SUFFIXES:
        raise ResourceBlocked("photo must be PNG, JPEG, or WebP")

    owner = _validated_owner(owner_id)
    if owner is not None:
        if (
            len(parts) != 4
            or parts[:3] != ("design", "photos", owner)
            or parts[3] not in {"photo.png", "photo.jpg", "photo.jpeg", "photo.webp"}
        ):
            raise ResourceBlocked(
                "photo must be the authenticated owner's exact uploaded photo path"
            )
    return relative.as_posix()


def _lexical_repo_path(repo_root: Path, relative: str) -> Path:
    """Return a lexical repository path without accepting aliases/traversal."""
    candidate = repo_root.joinpath(*PurePosixPath(relative).parts)
    try:
        candidate.relative_to(repo_root)
    except ValueError as exc:  # defensive; validation already rejects traversal
        raise ResourceBlocked("resource escapes the repository") from exc
    return candidate


def _assert_controlled_file(path: Path, repo_root: Path) -> None:
    """Reject missing files, symlinks, and files resolving outside the repo."""
    try:
        relative = path.relative_to(repo_root)
    except ValueError as exc:
        raise ResourceBlocked("local resource is outside the repository") from exc

    cursor = repo_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ResourceBlocked("symlinked render resources are not allowed")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo_root)
    except (FileNotFoundError, ValueError) as exc:
        raise ResourceBlocked("render resource is missing or outside the repository") from exc
    if not resolved.is_file():
        raise ResourceBlocked("render resource is not a regular file")


def make_url_fetcher(
    *,
    photo_source: str | None = None,
    owner_id: str | None = None,
    repo_root: Path | None = None,
) -> Callable[[str], Any]:
    """Build a deny-by-default WeasyPrint ``url_fetcher``.

    ``photo_source`` must already have passed the data-image guard when it is a
    ``data:`` URI.  Non-data values are validated again here so callers cannot
    accidentally widen the file allowlist.
    """
    root = (repo_root or REPO_ROOT).resolve()
    allowed_files = {
        _lexical_repo_path(root, relative) for relative in _FONT_RELATIVE_PATHS
    }
    allowed_data_resource: str | None = None
    if photo_source and is_data_resource(photo_source):
        # ``content.load`` has already decoded and bounded this raster. Bind the
        # fetch permission to that exact value so unrelated data payloads cannot
        # be introduced by a future HTML/CSS sink.
        allowed_data_resource = photo_source
    if photo_source and not is_data_resource(photo_source):
        relative_photo = validate_relative_photo_source(
            photo_source, owner_id=owner_id
        )
        allowed_files.add(_lexical_repo_path(root, relative_photo))

    native_fetcher = None

    def secure_url_fetcher(url: str) -> Any:
        nonlocal native_fetcher
        parsed = urlsplit(str(url))
        scheme = parsed.scheme.lower()
        if scheme == "data":
            if allowed_data_resource is None or str(url) != allowed_data_resource:
                raise ResourceBlocked("data resource is not the validated photo")
            if native_fetcher is None:
                from weasyprint import URLFetcher

                native_fetcher = URLFetcher(
                    allowed_protocols=("data", "file"),
                    allow_redirects=False,
                    fail_on_errors=True,
                )
            return native_fetcher(url)
        if scheme != "file":
            raise ResourceBlocked("network and non-file render resources are blocked")
        if parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
            raise ResourceBlocked("non-canonical file resource is blocked")

        raw_path = unquote(parsed.path)
        if "%" in raw_path and unquote(raw_path) != raw_path:
            raise ResourceBlocked("multiply encoded file resource is blocked")
        local_path = Path(url2pathname(raw_path))
        # Compare the lexical absolute path, not only its resolved target.  An
        # alias/symlink path to an allowed font/photo is still unauthorized.
        if not local_path.is_absolute() or local_path not in allowed_files:
            raise ResourceBlocked("local render resource is not allowlisted")
        _assert_controlled_file(local_path, root)

        if native_fetcher is None:
            from weasyprint import URLFetcher

            native_fetcher = URLFetcher(
                allowed_protocols=("data", "file"),
                allow_redirects=False,
                fail_on_errors=True,
            )
        return native_fetcher(url)

    # WeasyPrint uses this marker to make fetch failures fatal rather than
    # logging and silently omitting the blocked resource.
    secure_url_fetcher._fail_on_errors = True  # type: ignore[attr-defined]
    return secure_url_fetcher


def allowed_font_paths(repo_root: Path | None = None) -> frozenset[Path]:
    """Expose the immutable font allowlist for focused policy tests."""
    root = (repo_root or REPO_ROOT).resolve()
    return frozenset(_lexical_repo_path(root, rel) for rel in _FONT_RELATIVE_PATHS)
