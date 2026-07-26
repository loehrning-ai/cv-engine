"""PDF storage + delivery layer (Stage 8) — Cloudflare R2, content-hash cached.

R2 is an **optional caching/delivery layer**, never a hard dependency. The
graceful-fallback contract is the whole point of this module:

  * **R2 unconfigured** (the dev/test default — no creds): the build routes
    render the PDF and stream the bytes back exactly as before Stage 8. Nothing
    in this module is required for the app to work.
  * **R2 configured** (all four ``R2_*`` env vars set): a rendered PDF is
    uploaded to R2 under a content-addressed key and served via a short-lived
    signed URL. An unchanged document is NOT re-rendered — its non-stale
    artifact row's signed URL is returned straight from the cache.

Three responsibilities live here, each small and independently testable:

  1. **Storage abstraction** (:func:`r2_enabled`, :func:`put_pdf`,
     :func:`presigned_get`) — a boto3 S3 client pointed at the R2 endpoint,
     built lazily so importing this module never fails when unconfigured.
  2. **Concurrency bound** (:data:`RENDER_SEMAPHORE`) — a module-level
     ``threading.Semaphore(2)`` wrapping every WeasyPrint render (CV, cover
     letter, and the tailor service's 1-page-gate re-render) so concurrent
     renders can't exhaust memory in the always-on container.
  3. **Content-hash cache + generate-on-change** (:func:`build_cv_pdf`,
     :func:`build_cover_letter_pdf`, :func:`delete_document_artifacts`) —
     compute a stable ``content_hash`` over the inputs; reuse a non-stale
     ``pdf_artifacts`` row on a hit; otherwise render, remove superseded remote
     objects and rows, upload, and insert one fresh row. Document mutations
     remove prior artifacts before changing the document so derived personal
     data does not persist after it has been invalidated.

The CV's 1-page gate is unchanged: :func:`build_cv_pdf` calls the consolidated
``render_cv_pdf`` service and surfaces its overflow verdict untouched — a
2-page CV is never uploaded or cached.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any

import auth as auth_mod
from r2_security import validate_r2_account_id

# engine/ is on sys.path in every entrypoint (server boot, tests).
try:
    from render.pdf_service import RenderedPdf, render_cv_pdf
except ImportError:  # pragma: no cover - unusual import roots
    from engine.render.pdf_service import RenderedPdf, render_cv_pdf


# ---------------------------------------------------------------------------
# Concurrency bound — every WeasyPrint render goes through this semaphore.
# WeasyPrint holds a non-trivial amount of memory per render; the container is
# always-on (not serverless), so two concurrent renders is a deliberate ceiling
# that keeps memory bounded under a burst. Both the CV and the cover-letter
# render wrappers below acquire it, as does the tailor service's gate re-render
# (it calls render_cv via this module). Tune in ONE place if we scale the box.
# ---------------------------------------------------------------------------
_MAX_CONCURRENT_RENDERS = 2
RENDER_SEMAPHORE = threading.Semaphore(_MAX_CONCURRENT_RENDERS)
RENDER_CACHE_VERSION = "cv-render-2026-07-24-resource-policy-v3"


def _render_owner_id(explicit_owner_id: str | None = None) -> str | None:
    """Resolve the verified owner used by the engine photo policy.

    Cache-aware builds pass the owner explicitly. Direct-stream and tailor
    renders run inside an authenticated Flask request, so they inherit the
    verified ``g.owner_id``. CLI/test callers outside an app context remain
    ownerless and may use only the engine's safe repository-relative policy.
    """
    if explicit_owner_id not in {None, ""}:
        return str(explicit_owner_id)
    try:
        owner = getattr(auth_mod.auth_g(), "owner_id", None)
    except RuntimeError:
        return None
    return str(owner) if owner else None


def render_cv(
    yaml_text: str,
    *,
    density: str | None = None,
    template: str | None = None,
    paper: str | None = None,
    owner_id: str | None = None,
) -> RenderedPdf:
    """Render a CV through the consolidated 1-page service, semaphore-bounded.

    Thin wrapper so EVERY CV render (the build route AND the tailor service's
    1-page-gate re-render) is bounded by the shared semaphore. The 1-page gate
    and overflow verdict are byte-identical to ``render_cv_pdf`` — this only
    adds the concurrency bound. Raises whatever ``render_cv_pdf`` raises
    (``CvYamlError`` / ``RuntimeError``).
    """
    effective_owner = _render_owner_id(owner_id)
    with RENDER_SEMAPHORE:
        return render_cv_pdf(
            yaml_text,
            density=density,
            template=template,
            paper=paper,
            owner_id=effective_owner,
        )


def render_cover_letter(doc: dict[str, Any], *, base_url: str = "") -> bytes:
    """Render a cover letter to PDF bytes, semaphore-bounded (multi-page OK).

    Imports the letter renderer lazily (it imports WeasyPrint lazily too) so
    this module still loads in bare environments. No page-count gate — the CV's
    1-page invariant does not apply to letters.
    """
    try:
        from cover_letter.renderer import render_cover_letter_pdf
    except ImportError:  # pragma: no cover - unusual import roots
        from engine.cover_letter.renderer import render_cover_letter_pdf
    with RENDER_SEMAPHORE:
        return render_cover_letter_pdf(doc, base_url=base_url)


# ---------------------------------------------------------------------------
# Storage abstraction — Cloudflare R2 over the boto3 S3 client.
# ---------------------------------------------------------------------------
class StorageError(RuntimeError):
    """Raised when an R2 or artifact-metadata operation fails."""


def _r2_config() -> dict[str, str]:
    """Read the four R2 settings from the environment (re-read each call).

    Re-reading (rather than snapshotting at import) lets tests toggle R2 on/off
    via ``monkeypatch.setenv`` without reimporting the module.
    """
    return {
        "account_id": os.environ.get("R2_ACCOUNT_ID", "").strip(),
        "access_key_id": os.environ.get("R2_ACCESS_KEY_ID", "").strip(),
        "secret_access_key": os.environ.get("R2_SECRET_ACCESS_KEY", "").strip(),
        "bucket": os.environ.get("R2_BUCKET", "").strip(),
    }


def r2_enabled() -> bool:
    """True iff ALL four ``R2_*`` env vars are present.

    When this is False the build routes fall back to streaming PDF bytes
    directly — the app is fully functional with R2 unconfigured.
    """
    cfg = _r2_config()
    return all(cfg.values())


# The boto3 client is process-global and built lazily on first use so that
# importing this module never requires boto3 or any R2 config. Tests inject a
# fake by setting ``pdf_store._S3_CLIENT`` directly (see test_stage8_pdf_store).
_S3_CLIENT: Any | None = None


def _client() -> Any:
    """Return the lazily-built boto3 S3 client pointed at the R2 endpoint.

    Built on first call (never at import), so an unconfigured/boto3-less env can
    still import this module. R2's S3 API lives at
    ``https://{account}.r2.cloudflarestorage.com`` with region ``auto``.
    """
    global _S3_CLIENT
    cfg = _r2_config()
    if not all(cfg.values()):
        raise StorageError("R2 is not configured (missing R2_* env vars)")
    try:
        account_id = validate_r2_account_id(cfg["account_id"])
    except ValueError as exc:
        raise StorageError(str(exc)) from exc
    if _S3_CLIENT is not None:
        return _S3_CLIENT
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - boto3 is a hard dep in prod
        raise StorageError("boto3 is not installed") from exc
    endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    _S3_CLIENT = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=cfg["access_key_id"],
        aws_secret_access_key=cfg["secret_access_key"],
        region_name="auto",
        # SigV4 is required for R2; keep the signature explicit.
        config=Config(signature_version="s3v4"),
    )
    return _S3_CLIENT


def _pdf_disposition(filename: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename or "document.pdf").strip(".-_")
    if not safe.endswith(".pdf"):
        safe = "document.pdf"
    return f'inline; filename="{safe}"'


def put_pdf(key: str, data: bytes, filename: str = "document.pdf") -> None:
    """Upload PDF ``data`` to R2 under ``key`` (application/pdf).

    Raises :class:`StorageError` on any client/transport failure so the caller
    can degrade to a direct stream rather than 500.
    """
    try:
        cfg = _r2_config()
        _client().put_object(
            Bucket=cfg["bucket"],
            Key=key,
            Body=data,
            ContentType="application/pdf",
            ContentDisposition=_pdf_disposition(filename),
        )
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001 — wrap transport/client errors
        raise StorageError(f"R2 put failed: {type(exc).__name__}") from exc


def delete_pdf(key: str) -> None:
    """Delete one R2 object using idempotent S3 ``DeleteObject`` semantics."""
    if not isinstance(key, str) or not key.strip():
        raise StorageError("R2 delete refused an empty object key")
    try:
        cfg = _r2_config()
        _client().delete_object(Bucket=cfg["bucket"], Key=key)
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001 — wrap transport/client errors
        raise StorageError(f"R2 delete failed: {type(exc).__name__}") from exc


def presigned_get(key: str, ttl: int = 3600, filename: str = "document.pdf") -> str:
    """Return a short-lived signed GET URL for ``key`` (default 1 hour).

    The browser fetches the PDF directly from R2 with this URL — the bytes never
    transit the app a second time. Raises :class:`StorageError` on failure.
    """
    try:
        cfg = _r2_config()
        return _client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": cfg["bucket"],
                "Key": key,
                "ResponseContentDisposition": _pdf_disposition(filename),
            },
            ExpiresIn=int(ttl),
        )
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"R2 presign failed: {type(exc).__name__}") from exc


def r2_object_exists(key: str) -> bool:
    """True only when the configured R2 bucket contains ``key``.

    External-apply readiness is intentionally stricter than the normal build
    cache path: a database row alone is not proof that an application packet has
    a persisted PDF. Any missing config, missing key, or storage error fails
    closed.
    """
    if not key:
        return False
    try:
        cfg = _r2_config()
        _client().head_object(Bucket=cfg["bucket"], Key=key)
        return True
    except Exception:  # noqa: BLE001 - readiness must fail closed
        return False


# ---------------------------------------------------------------------------
# Content-hash cache + generate-on-change.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BuiltPdf:
    """Outcome of a cache-aware build when R2 is enabled.

    ``url`` is the signed GET URL; ``cached`` is True when a non-stale artifact
    was reused (no re-render); ``page_count`` mirrors the artifact row.
    """

    url: str
    cached: bool
    page_count: int


def content_hash(
    yaml_blob: str,
    accent: str,
    font: str,
    density: str,
    template: str = "classic",
    paper: str = "white",
) -> str:
    """Stable SHA-256 over the render inputs.

    Two builds with the SAME yaml + theme/template/paper inputs produce the SAME hash, so an
    unchanged document hits the cache and is never re-rendered. A NUL separator
    keeps the fields unambiguous (no field can contain ``\\x00``).
    """
    parts = (
        RENDER_CACHE_VERSION,
        yaml_blob or "",
        accent or "",
        font or "",
        density or "",
        template or "classic",
        paper or "white",
    )
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _artifact_table():
    """The RLS-scoped ``pdf_artifacts`` table handle for the current user."""
    return auth_mod.user_supabase().table("pdf_artifacts")


def _canonical_uuid(value: object, *, field: str) -> str:
    """Return a canonical UUID or fail before constructing an object key."""
    try:
        canonical = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise StorageError(f"invalid {field}") from exc
    if str(value) != canonical:
        raise StorageError(f"non-canonical {field}")
    return canonical


def _canonical_hash(value: object) -> str:
    text = str(value)
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise StorageError("invalid PDF content hash")
    return text


def _verified_owner_id() -> str:
    """Return only the request identity populated by verified JWT auth."""
    try:
        owner_id = getattr(auth_mod.auth_g(), "owner_id", None)
    except RuntimeError as exc:
        raise StorageError("verified owner context is unavailable") from exc
    return _canonical_uuid(owner_id, field="owner id")


def _r2_key(owner_id: str, document_id: str, chash: str) -> str:
    """Canonical content-addressed key: ``{owner}/{document}/{hash}.pdf``."""
    owner = _canonical_uuid(owner_id, field="owner id")
    document = _canonical_uuid(document_id, field="document id")
    digest = _canonical_hash(chash)
    return f"{owner}/{document}/{digest}.pdf"


def _validated_row_key(row: dict, verified_owner_id: str) -> str:
    """Derive a row's only permitted key and require an exact match."""
    expected = _r2_key(
        verified_owner_id,
        row.get("document_id"),
        row.get("content_hash"),
    )
    if row.get("r2_key") != expected:
        raise StorageError("PDF artifact key is not canonical for the verified owner")
    return expected


def _find_fresh_artifact(
    document_id: str,
    chash: str,
    owner_id: str,
) -> dict | None:
    """A non-stale artifact for ``document_id`` whose hash matches, or None.

    RLS scopes the SELECT to ``owner_id = auth.uid()`` so only the caller's
    artifacts are visible. The query also requires the exact key derived from
    the verified owner, document, and content hash; a writable metadata row can
    never redirect a presign operation to another tenant's object.
    """
    verified_owner = _verified_owner_id()
    if _canonical_uuid(owner_id, field="owner id") != verified_owner:
        raise StorageError("artifact owner does not match the verified request")
    expected_key = _r2_key(verified_owner, document_id, chash)
    resp = (
        _artifact_table()
        .select("id, r2_key, document_id, content_hash, page_count, stale")
        .eq("document_id", document_id)
        .eq("content_hash", chash)
        .eq("r2_key", expected_key)
        .eq("stale", False)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return None
    _validated_row_key(rows[0], verified_owner)
    return rows[0]


def _artifact_rows(document_id: str | None = None) -> list[dict]:
    """Return RLS-scoped artifact identities for one document or the owner."""
    try:
        query = _artifact_table().select(
            "id, r2_key, document_id, content_hash"
        )
        if document_id is not None:
            query = query.eq("document_id", document_id)
        return list(query.execute().data or [])
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001 — normalize database transport errors
        raise StorageError(
            f"PDF artifact lookup failed: {type(exc).__name__}"
        ) from exc


def _delete_artifact_rows(rows: list[dict], verified_owner_id: str) -> int:
    """Delete each remote object before its corresponding metadata row.

    A failure stops immediately. Rows already completed remain safely removed;
    the owning document is untouched and a retry is idempotent. If object
    deletion commits but the row deletion response is lost, the retained row
    is retried against S3's idempotent DeleteObject operation.
    """
    deleted = 0
    for row in rows:
        artifact_id = row.get("id")
        if not artifact_id:
            raise StorageError("PDF artifact metadata is incomplete")
        r2_key = _validated_row_key(row, verified_owner_id)
        delete_pdf(r2_key)
        try:
            _artifact_table().delete().eq("id", artifact_id).execute()
        except Exception as exc:  # noqa: BLE001 — normalize database transport errors
            raise StorageError(
                f"PDF artifact row deletion failed: {type(exc).__name__}"
            ) from exc
        deleted += 1
    return deleted


def delete_document_artifacts(document_id: str) -> int:
    """Delete every cached PDF for one owned document, object first.

    An unowned or already-clean document yields zero rows. When no rows exist,
    R2 need not be configured and this is an idempotent no-op.
    """
    verified_owner = _verified_owner_id()
    document = _canonical_uuid(document_id, field="document id")
    return _delete_artifact_rows(
        _artifact_rows(document),
        verified_owner,
    )


def delete_owner_artifacts() -> int:
    """Delete every cached PDF visible to the authenticated owner."""
    verified_owner = _verified_owner_id()
    return _delete_artifact_rows(_artifact_rows(), verified_owner)


def mark_document_stale(document_id: str) -> int:
    """Compatibility alias for strict object-and-row invalidation."""
    return delete_document_artifacts(document_id)


def _insert_artifact(document_id: str, r2_key: str, chash: str, page_count: int) -> None:
    """Insert a fresh (non-stale) artifact row for ``document_id``.

    ``owner_id`` defaults to ``auth.uid()`` in the schema (RLS WITH CHECK), so
    it is never set from here.
    """
    try:
        _artifact_table().insert(
            {
                "document_id": document_id,
                "r2_key": r2_key,
                "content_hash": chash,
                "page_count": int(page_count),
                "stale": False,
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 — normalize database transport errors
        raise StorageError(
            f"PDF artifact insert failed: {type(exc).__name__}"
        ) from exc


def build_cv_pdf(
    *,
    document_id: str,
    owner_id: str,
    yaml_blob: str,
    accent: str = "",
    font: str = "",
    density: str = "normal",
    template: str = "classic",
    paper: str = "white",
) -> tuple[RenderedPdf, BuiltPdf | None]:
    """Cache-aware CV build (R2 path). Returns ``(rendered, built)``.

    Contract:
      * Computes ``content_hash`` over (yaml, accent, font, density, template, paper).
      * On a non-stale cache HIT → returns ``(RenderedPdf(cached marker), built)``
        WITHOUT re-rendering: ``rendered.pdf_bytes`` is None, ``built.cached`` is
        True, and the signed URL points at the existing object.
      * On a MISS → renders via the semaphore-bounded 1-page service. If
        it overflows, returns ``(rendered_overflow, None)`` and uploads NOTHING
        (a 2-page CV is never cached). Otherwise deletes prior objects and
        rows, uploads the bytes, inserts a fresh row, and returns
        ``(rendered, built)`` with ``built.cached=False``.

    The caller must have already confirmed ``r2_enabled()``; the fallback
    (direct-stream) path does NOT use this function.
    """
    verified_owner = _verified_owner_id()
    if _canonical_uuid(owner_id, field="owner id") != verified_owner:
        raise StorageError("artifact owner does not match the verified request")
    document_id = _canonical_uuid(document_id, field="document id")
    chash = content_hash(yaml_blob, accent, font, density, template, paper)
    hit = _find_fresh_artifact(document_id, chash, verified_owner)
    if hit is not None:
        url = presigned_get(hit["r2_key"], filename="cv.pdf")
        rendered = RenderedPdf(
            overflowed=False,
            page_count=hit.get("page_count") or 1,
            pdf_bytes=None,
            density=density,
        )
        return rendered, BuiltPdf(url=url, cached=True, page_count=rendered.page_count)

    # Miss/stale → render (semaphore-bounded). render_cv may raise CvYamlError /
    # RuntimeError, which the route maps to 400/503.
    rendered = render_cv(
        yaml_blob,
        density=density,
        template=template,
        paper=paper,
        owner_id=owner_id,
    )
    if rendered.overflowed:
        # Never upload or cache a 2-page CV — surface the overflow verdict.
        return rendered, None

    key = _r2_key(verified_owner, document_id, chash)
    # Fail closed before upload: superseded personal-data objects and metadata
    # must be gone before a replacement can become authoritative.
    delete_document_artifacts(document_id)
    put_pdf(key, rendered.pdf_bytes or b"", filename="cv.pdf")
    try:
        _insert_artifact(document_id, key, chash, rendered.page_count)
    except Exception:
        # The upload is not reachable from application metadata if INSERT
        # fails. Remove it immediately rather than orphaning personal data.
        delete_pdf(key)
        raise
    url = presigned_get(key, filename="cv.pdf")
    built = BuiltPdf(url=url, cached=False, page_count=rendered.page_count)
    return rendered, built


def build_cover_letter_pdf(
    *,
    document_id: str,
    owner_id: str,
    cache_key_text: str,
    render_doc: dict[str, Any],
    base_url: str = "",
) -> BuiltPdf:
    """Cache-aware cover-letter build (R2 path). Multi-page allowed.

    ``cache_key_text`` is the stable text the hash is taken over (the stored
    letter blob, which captures body/recipient/company/etc.); ``render_doc`` is
    the renderer's input shape. On a non-stale hit the existing object's signed
    URL is returned WITHOUT re-rendering; otherwise the letter is rendered
    (semaphore-bounded), prior artifacts deleted, the new PDF uploaded, a
    fresh row inserted, and the signed URL returned.

    Raises whatever the renderer raises (the route maps a render failure to
    500). The caller must have confirmed ``r2_enabled()``.
    """
    verified_owner = _verified_owner_id()
    if _canonical_uuid(owner_id, field="owner id") != verified_owner:
        raise StorageError("artifact owner does not match the verified request")
    document_id = _canonical_uuid(document_id, field="document id")
    chash = content_hash(cache_key_text, "", "", "letter")
    hit = _find_fresh_artifact(document_id, chash, verified_owner)
    if hit is not None:
        return BuiltPdf(
            url=presigned_get(hit["r2_key"], filename="cover-letter.pdf"),
            cached=True,
            page_count=hit.get("page_count") or 1,
        )

    pdf_bytes = render_cover_letter(render_doc, base_url=base_url)
    page_count = _count_pdf_pages(pdf_bytes)
    key = _r2_key(verified_owner, document_id, chash)
    delete_document_artifacts(document_id)
    put_pdf(key, pdf_bytes, filename="cover-letter.pdf")
    try:
        _insert_artifact(document_id, key, chash, page_count)
    except Exception:
        delete_pdf(key)
        raise
    return BuiltPdf(url=presigned_get(key, filename="cover-letter.pdf"), cached=False, page_count=page_count)


def _count_pdf_pages(pdf_bytes: bytes) -> int:
    """Best-effort page count of a rendered PDF (for the artifact row).

    Uses pypdf (already a dependency); a parse hiccup must never break a build,
    so we fall back to 1. Cover letters are multi-page-allowed, so this is
    informational metadata, not a gate.
    """
    try:
        import io

        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:  # noqa: BLE001 — page count is informational only
        return 1
