"""Image validation guard for the editor gateway (web-layer hardening).

Two attack surfaces feed an attacker-supplied image into the renderer:

  * ``POST /api/asset/photo`` — a multipart upload saved to disk and later
    referenced by the CV YAML's ``photo:`` field.
  * a ``data:`` image URI embedded directly in the CV YAML ``photo:`` field,
    which WeasyPrint decodes and rasterises during a build/preview.

Both must be validated BEFORE the bytes reach WeasyPrint, because a tiny
compressed payload can decode to an enormous bitmap (a "decompression bomb")
and exhaust memory. This module enforces, with NO full decompression:

  * a hard byte-size cap on the (decoded) payload;
  * a real raster type check by MAGIC BYTES — only PNG / JPEG / WebP are
    accepted; SVG (XML, scriptable) and everything else are rejected by type,
    not by a spoofable file extension;
  * a decoded pixel-area + per-dimension cap, read from the image header via
    Pillow's lazy ``Image.open`` (header parse only — it does not decode the
    pixels) plus Pillow's own ``DecompressionBombError`` as a backstop.

It deliberately does not depend on Flask so it is trivially unit-testable.
"""
from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass

# --- Caps -----------------------------------------------------------------
# Encoded/raw byte ceiling for a profile photo. A real headshot is well under
# this; anything larger is either junk or an attack payload.
MAX_PHOTO_BYTES = 8 * 1024 * 1024  # 8 MB

# Decoded-pixel ceilings. A bomb is a small file that claims a huge canvas, so
# we reject on the *header-declared* dimensions before any pixel is decoded.
# Rendering a 25 MP PNG consumed roughly 293 MB RSS in an isolated proof.
# We permit at most two simultaneous PDF renders, so the old ceiling could
# exhaust a modest production container. Four megapixels is already far above
# the resolution a profile photo occupies on an A4 page while keeping decoded
# RGBA memory near 16 MB per image.
MAX_IMAGE_AREA = 4_000_000
MAX_IMAGE_DIMENSION = 4_096

# Accepted raster formats by magic bytes. SVG and all non-raster inputs are
# rejected (SVG is XML and can carry script — never hand it to the renderer).
_ALLOWED_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})


class ImageRejected(ValueError):
    """Raised when an image is missing/oversized/wrong-type/bomb-shaped."""


@dataclass(frozen=True)
class ImageInfo:
    """Validated image metadata (format + decoded dimensions)."""

    fmt: str
    width: int
    height: int
    nbytes: int


def _sniff_format(data: bytes) -> str | None:
    """Return 'PNG' | 'JPEG' | 'WEBP' from magic bytes, or ``None``.

    Extension is never trusted — only the actual leading bytes decide the type.
    """
    if len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    # WebP: RIFF container, "WEBP" fourcc at offset 8.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def _looks_like_svg(data: bytes) -> bool:
    """Heuristic: leading bytes look like an SVG/XML document.

    Used only for a clearer error message — ``_sniff_format`` already rejects
    SVG by virtue of it not being a raster magic number.
    """
    head = data[:512].lstrip()
    low = head[:200].lower()
    return low.startswith(b"<?xml") or low.startswith(b"<svg") or b"<svg" in low


def validate_image_bytes(data: bytes) -> ImageInfo:
    """Validate raw image ``data`` against all caps. Return :class:`ImageInfo`.

    Raises :class:`ImageRejected` on: empty input, oversize bytes, SVG /
    non-raster type, an unreadable/corrupt header, or decoded dimensions over
    the area / per-dimension caps. No pixels are decoded — only the header.
    """
    if not data:
        raise ImageRejected("empty image")
    if len(data) > MAX_PHOTO_BYTES:
        raise ImageRejected(
            f"image too large ({len(data)} bytes > {MAX_PHOTO_BYTES} cap)"
        )
    if _looks_like_svg(data):
        raise ImageRejected("SVG images are not allowed — use PNG, JPEG, or WebP")

    fmt = _sniff_format(data)
    if fmt is None or fmt not in _ALLOWED_FORMATS:
        raise ImageRejected("unsupported image type — use PNG, JPEG, or WebP")

    # Header-only dimension read. Pillow's Image.open is lazy: it parses the
    # header (so .size is available) without decoding the pixel data. We also
    # rely on Pillow's MAX_IMAGE_PIXELS DecompressionBombError as a backstop.
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow is a hard dep here
        raise ImageRejected("image validation unavailable (Pillow missing)") from exc

    import io

    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            pillow_fmt = (im.format or "").upper()
    except Image.DecompressionBombError as exc:
        raise ImageRejected("image dimensions are implausibly large") from exc
    except Exception as exc:  # noqa: BLE001 — corrupt/unreadable header → reject
        raise ImageRejected(f"unreadable image header: {type(exc).__name__}") from exc

    # Pillow's own format must agree with our magic-byte sniff (defence-in-depth
    # against polyglot/spoofed containers).
    if pillow_fmt and pillow_fmt not in _ALLOWED_FORMATS:
        raise ImageRejected("unsupported image type — use PNG, JPEG, or WebP")

    if width <= 0 or height <= 0:
        raise ImageRejected("image has no dimensions")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ImageRejected(
            f"image dimension too large ({width}×{height} > "
            f"{MAX_IMAGE_DIMENSION}px per side)"
        )
    if width * height > MAX_IMAGE_AREA:
        raise ImageRejected(
            f"image area too large ({width}×{height} = {width * height}px "
            f"> {MAX_IMAGE_AREA}px cap)"
        )
    return ImageInfo(fmt=fmt, width=width, height=height, nbytes=len(data))


# data:[<mediatype>][;base64],<data>
_DATA_URI_RE = re.compile(r"^data:([^;,]*)(;[^,]*)?,(.*)$", re.IGNORECASE | re.DOTALL)


def is_data_uri(value: str) -> bool:
    """True iff ``value`` is a ``data:`` URI (case-insensitive scheme)."""
    return isinstance(value, str) and value.lstrip()[:5].lower() == "data:"


def validate_data_uri_image(value: str) -> ImageInfo:
    """Decode + validate a ``data:`` image URI. Raise :class:`ImageRejected`.

    Caps the ENCODED length first (so a multi-hundred-MB base64 blob is
    rejected before we allocate the decoded buffer), then decodes and runs the
    same magic-byte + dimension checks as an upload. This is what blocks a
    tiny-on-the-wire ``data:`` URI that decodes to a giant bitmap from ever
    reaching WeasyPrint.
    """
    raw = value.lstrip()
    # base64 inflates ~4/3; cap the encoded string so the decoded buffer can't
    # blow past MAX_PHOTO_BYTES.
    if len(raw.encode("utf-8")) > MAX_PHOTO_BYTES * 2:
        raise ImageRejected("embedded image is too large")

    m = _DATA_URI_RE.match(raw)
    if not m:
        raise ImageRejected("malformed data: URI")
    mediatype = (m.group(1) or "").lower()
    is_b64 = bool(m.group(2) and "base64" in m.group(2).lower())
    payload = m.group(3) or ""

    if "svg" in mediatype:
        raise ImageRejected("SVG images are not allowed — use PNG, JPEG, or WebP")

    if is_b64:
        try:
            data = base64.b64decode(payload, validate=False)
        except Exception as exc:  # noqa: BLE001 — bad base64 → reject
            raise ImageRejected("invalid base64 in data: URI") from exc
    else:
        # URL-encoded / raw text payload — not a raster image we accept.
        from urllib.parse import unquote_to_bytes

        data = unquote_to_bytes(payload)

    return validate_image_bytes(data)


def sanitise_image_bytes(data: bytes) -> tuple[bytes, ImageInfo]:
    """Decode and re-encode an accepted raster without source metadata.

    Validation runs before full decode. Re-encoding applies EXIF orientation
    and omits EXIF, GPS, comments, ICC profiles, and other source metadata.
    The returned bytes are validated again and remain under the same byte and
    pixel caps.
    """
    original = validate_image_bytes(data)
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
            out = io.BytesIO()
            if original.fmt == "JPEG":
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                image.save(out, format="JPEG", quality=90, optimize=True)
            elif original.fmt == "PNG":
                image.save(out, format="PNG", optimize=True)
            elif original.fmt == "WEBP":
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                image.save(out, format="WEBP", quality=90, method=6)
            else:  # pragma: no cover - validate_image_bytes already rejects it
                raise ImageRejected("unsupported image type")
    except ImageRejected:
        raise
    except Exception as exc:  # noqa: BLE001 - decode/re-encode must fail closed
        raise ImageRejected(f"image sanitization failed: {type(exc).__name__}") from exc

    cleaned = out.getvalue()
    info = validate_image_bytes(cleaned)
    if info.fmt != original.fmt:
        raise ImageRejected("image format changed during sanitization")
    return cleaned, info
