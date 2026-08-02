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
from datetime import datetime, timezone
from typing import Any

import yaml

import auth as auth_mod
from r2_security import validate_r2_account_id

# engine/ is on sys.path in every entrypoint (server boot, tests).
try:
    from render.pdf_service import RenderedPdf, render_cv_pdf
    from render import css_base, template_registry
except ImportError:  # pragma: no cover - unusual import roots
    from engine.render.pdf_service import RenderedPdf, render_cv_pdf
    from engine.render import css_base, template_registry


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


def _revision_evidence_matches(actual: object, expected: object) -> bool:
    """Compare timestamptz evidence by instant, not PostgREST string spelling."""
    if actual == expected:
        return True
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False

    def _parse(value: str) -> datetime | None:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    actual_instant = _parse(actual)
    expected_instant = _parse(expected)
    return actual_instant is not None and actual_instant == expected_instant


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


class ArtifactRevisionConflict(StorageError):
    """Raised when a document changes while its PDF is rendering."""


class ArtifactAccountUnavailable(StorageError):
    """Raised when the owner becomes unavailable during an artifact operation."""


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


def _r2_not_found(exc: Exception) -> bool:
    """Recognize an authoritative S3/R2 missing-object response."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        code = str(error.get("Code") if isinstance(error, dict) else "")
        metadata = response.get("ResponseMetadata")
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return True
    return getattr(exc, "status_code", None) == 404


def _r2_object_exists_strict(key: str) -> bool:
    """Return False only for an authoritative 404; raise on indeterminate HEAD."""
    if not key:
        raise StorageError("R2 HEAD refused an empty object key")
    try:
        cfg = _r2_config()
        _client().head_object(Bucket=cfg["bucket"], Key=key)
        return True
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize provider failures
        if _r2_not_found(exc):
            return False
        raise StorageError(f"R2 HEAD failed: {type(exc).__name__}") from exc


def r2_object_exists(key: str) -> bool:
    """Fail-closed object evidence for readiness and other boolean callers."""
    try:
        return _r2_object_exists_strict(key)
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


def persisted_cv_content_hash(
    yaml_blob: str,
    accent: str = "",
    font: str = "",
) -> str:
    """Return the canonical cache hash for a persisted CV document.

    ``/api/build`` permits explicit density/template/paper overrides for one
    render. External-apply readiness must not accept an artifact produced from
    such an ephemeral override as proof of the current document. Its canonical
    identity therefore uses the persisted YAML appearance with the same
    normalization/defaults as the build route, plus the mirrored accent/font
    document columns that are part of the existing cache contract.
    """
    if not isinstance(yaml_blob, str):
        raise StorageError("invalid persisted CV content")
    if not isinstance(accent, str) or not isinstance(font, str):
        raise StorageError("invalid persisted CV appearance")
    try:
        parsed = yaml.safe_load(yaml_blob)
    except Exception as exc:  # noqa: BLE001 - normalize parser failures
        raise StorageError("invalid persisted CV content") from exc
    if not isinstance(parsed, dict):
        raise StorageError("invalid persisted CV content")

    density_value = parsed.get("density")
    density = (
        (density_value or "normal").strip().lower()
        if isinstance(density_value, str)
        else "normal"
    )
    if density not in {"tight", "normal", "airy"}:
        density = "normal"

    template_value = parsed.get("template")
    template = template_registry.normalize_template(
        (template_value or "classic").strip().lower()
        if isinstance(template_value, str)
        else "classic"
    )

    paper_value = parsed.get("paper")
    paper = (
        (paper_value or "white").strip().lower()
        if isinstance(paper_value, str)
        else "white"
    )
    try:
        paper = css_base.normalize_paper(paper)
    except ValueError:
        paper = "white"

    return content_hash(yaml_blob, accent, font, density, template, paper)


def cover_letter_content_hash(cache_key_text: str) -> str:
    """Return the canonical cache hash for cover-letter renderer inputs."""
    if not isinstance(cache_key_text, str):
        raise StorageError("invalid cover-letter cache content")
    return content_hash(cache_key_text, "", "", "letter")


def _artifact_table():
    """The RLS-scoped ``pdf_artifacts`` table handle for the current user."""
    return auth_mod.user_supabase().table("pdf_artifacts")


def _server_sb():
    """Service-role client for server-authored artifact mutations."""
    return auth_mod.server_supabase()


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


def _r2_key(
    owner_id: str,
    document_id: str,
    chash: str,
    generation_id: object | None = None,
) -> str:
    """Return a canonical legacy or unique-generation object key."""
    owner = _canonical_uuid(owner_id, field="owner id")
    document = _canonical_uuid(document_id, field="document id")
    digest = _canonical_hash(chash)
    if generation_id is None:
        return f"{owner}/{document}/{digest}.pdf"
    generation = _canonical_uuid(generation_id, field="generation id")
    return f"{owner}/{document}/{digest}/{generation}.pdf"


def verified_artifact_key(
    document_id: str,
    chash: str,
    generation_id: object | None = None,
) -> str:
    """Canonical object key bound to the current verified request owner."""
    return _r2_key(_verified_owner_id(), document_id, chash, generation_id)


def _validated_row_key(row: dict, verified_owner_id: str) -> str:
    """Derive a row's only permitted key and require an exact match."""
    expected = _r2_key(
        verified_owner_id,
        row.get("document_id"),
        row.get("content_hash"),
        row.get("generation_id"),
    )
    if row.get("r2_key") != expected:
        raise StorageError("PDF artifact key is not canonical for the verified owner")
    return expected


def _resolve_current_artifact(
    *,
    document_id: str,
    document_revision: object,
    chash: str,
    owner_id: str,
    dependency_document_id: str | None = None,
    dependency_revision: object | None = None,
) -> dict | None:
    """Resolve a revision-bound cache hit through the service-role RPC."""
    verified_owner = _verified_owner_id()
    if _canonical_uuid(owner_id, field="owner id") != verified_owner:
        raise StorageError("artifact owner does not match the verified request")
    dependency = (
        _canonical_uuid(dependency_document_id, field="dependency document id")
        if dependency_document_id is not None
        else None
    )
    try:
        response = _server_sb().rpc(
            "resolve_pdf_artifact",
            {
                "p_owner_id": verified_owner,
                "p_document_id": document_id,
                "p_document_revision": document_revision,
                "p_content_hash": chash,
                "p_dependency_document_id": dependency,
                "p_dependency_revision": dependency_revision,
            },
        ).execute().data
    except Exception as exc:  # noqa: BLE001 - normalize database transport errors
        raise StorageError(
            f"PDF artifact resolution failed: {type(exc).__name__}"
        ) from exc
    result = response[0] if isinstance(response, list) and response else response
    if not isinstance(result, dict):
        raise StorageError("PDF artifact resolution returned invalid evidence")
    status = result.get("status")
    if status == "revision_conflict":
        raise ArtifactRevisionConflict("document changed during PDF cache lookup")
    if status == "miss":
        return None
    if status != "hit":
        if result.get("code") == "account_unavailable":
            raise ArtifactAccountUnavailable("account is unavailable")
        raise StorageError(str(result.get("code") or "PDF artifact resolution blocked"))
    if (
        result.get("document_id") != document_id
        or result.get("content_hash") != chash
    ):
        raise StorageError("PDF artifact resolution evidence mismatch")
    _validated_row_key(result, verified_owner)
    return result


def authorize_document_export(
    *,
    document_id: str,
    document_revision: object,
) -> None:
    """Authorize a direct stream at an active account/document revision."""
    owner = _verified_owner_id()
    document = _canonical_uuid(document_id, field="document id")
    try:
        response = _server_sb().rpc(
            "authorize_document_export",
            {
                "p_owner_id": owner,
                "p_document_id": document,
                "p_document_revision": document_revision,
            },
        ).execute().data
    except Exception as exc:  # noqa: BLE001 - normalize database transport errors
        raise StorageError(
            f"document export authorization failed: {type(exc).__name__}"
        ) from exc
    result = response[0] if isinstance(response, list) and response else response
    if not isinstance(result, dict):
        raise StorageError("document export authorization returned invalid evidence")
    if result.get("status") == "revision_conflict":
        raise ArtifactRevisionConflict("document changed before export")
    if result.get("status") != "authorized":
        if result.get("code") == "account_unavailable":
            raise ArtifactAccountUnavailable("account is unavailable")
        raise StorageError(str(result.get("code") or "document export blocked"))
    if (
        result.get("document_id") != document
        or not _revision_evidence_matches(
            result.get("document_revision"), document_revision
        )
    ):
        raise StorageError("document export authorization evidence mismatch")


def _evict_broken_artifact(
    *,
    document_id: str,
    document_revision: object,
    artifact: dict,
) -> str:
    """CAS-evict exactly one metadata row after its object failed HEAD."""
    owner = _verified_owner_id()
    artifact_id = _canonical_uuid(artifact.get("artifact_id"), field="artifact id")
    _validated_row_key(artifact, owner)
    try:
        response = _server_sb().rpc(
            "evict_broken_pdf_artifact",
            {
                "p_owner_id": owner,
                "p_document_id": document_id,
                "p_document_revision": document_revision,
                "p_artifact_id": artifact_id,
            },
        ).execute().data
    except Exception as exc:  # noqa: BLE001 - normalize database transport errors
        raise StorageError(
            f"broken PDF artifact eviction failed: {type(exc).__name__}"
        ) from exc
    result = response[0] if isinstance(response, list) and response else response
    if not isinstance(result, dict):
        raise StorageError("broken PDF artifact eviction returned invalid evidence")
    status = result.get("status")
    if status == "revision_conflict":
        raise ArtifactRevisionConflict("document changed during PDF cache repair")
    if status == "already_replaced":
        return status
    if status != "evicted":
        if result.get("code") == "account_unavailable":
            raise ArtifactAccountUnavailable("account is unavailable")
        raise StorageError(str(result.get("code") or "broken PDF eviction blocked"))
    cleanup = result.get("artifact")
    if not isinstance(cleanup, dict):
        raise StorageError("broken PDF eviction returned invalid cleanup evidence")
    delete_detached_artifacts([cleanup], strict=False)
    return status


def _artifact_rows(document_id: str | None = None) -> list[dict]:
    """Return RLS-scoped artifact identities for one document or the owner."""
    try:
        query = _artifact_table().select(
            "id, r2_key, document_id, content_hash, generation_id"
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


def delete_document_artifacts(document_id: str, *, force: bool = False) -> int:
    """Atomically detach then delete every cached PDF for one owned document."""
    verified_owner = _verified_owner_id()
    document = _canonical_uuid(document_id, field="document id")
    try:
        response = _server_sb().rpc(
            "detach_pdf_artifacts",
            {"p_owner_id": verified_owner, "p_document_id": document},
        ).execute().data
    except Exception as exc:  # noqa: BLE001 - normalize database transport errors
        raise StorageError(
            f"PDF artifact detach failed: {type(exc).__name__}"
        ) from exc
    result = response[0] if isinstance(response, list) and response else response
    if not isinstance(result, dict):
        raise StorageError("PDF artifact detach returned invalid evidence")
    status = result.get("status")
    if status == "not_found":
        return 0
    if status != "detached":
        raise StorageError(str(result.get("code") or "PDF artifact detach blocked"))
    rows = result.get("artifacts") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise StorageError("PDF artifact detach returned invalid rows")
    return delete_detached_artifacts(rows, force=force)


def delete_owner_cv_artifacts(*, force: bool = True) -> int:
    """Atomically detach every current CV artifact for the verified owner."""
    owner = _verified_owner_id()
    try:
        response = _server_sb().rpc(
            "detach_owner_cv_pdf_artifacts",
            {"p_owner_id": owner},
        ).execute().data
    except Exception as exc:  # noqa: BLE001 - normalize database transport errors
        raise StorageError(
            f"owner CV artifact detach failed: {type(exc).__name__}"
        ) from exc
    result = response[0] if isinstance(response, list) and response else response
    if not isinstance(result, dict):
        raise StorageError("owner CV artifact detach returned invalid evidence")
    if result.get("status") != "detached":
        if result.get("code") == "account_unavailable":
            raise ArtifactAccountUnavailable("account is unavailable")
        raise StorageError(str(result.get("code") or "owner CV artifact detach blocked"))
    rows = result.get("artifacts") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise StorageError("owner CV artifact detach returned invalid rows")
    return delete_detached_artifacts(rows, force=force)


def update_document_with_artifact_detach(
    *,
    document_id: str,
    document_type: str,
    expected_revision: object,
    yaml_blob: str,
    updated_at: object,
    accent: str | None = None,
    font: str | None = None,
    density: str | None = None,
    summary_company: str | None = None,
    summary_role: str | None = None,
) -> dict:
    """CAS-save render inputs and detach old PDF metadata in one transaction."""
    owner = _verified_owner_id()
    document = _canonical_uuid(document_id, field="document id")
    if document_type not in {"cv", "cover_letter"}:
        raise StorageError("unsupported document type")
    if expected_revision is None or updated_at is None or not isinstance(yaml_blob, str):
        raise StorageError("document save revision and content are required")
    try:
        response = _server_sb().rpc(
            "update_document_with_artifact_detach",
            {
                "p_owner_id": owner,
                "p_document_id": document,
                "p_document_type": document_type,
                "p_expected_revision": expected_revision,
                "p_yaml_blob": yaml_blob,
                "p_updated_at": updated_at,
                "p_accent": accent,
                "p_font": font,
                "p_density": density,
                "p_summary_company": summary_company,
                "p_summary_role": summary_role,
            },
        ).execute().data
    except Exception as exc:  # noqa: BLE001 - normalize database transport errors
        raise StorageError(f"document save failed: {type(exc).__name__}") from exc
    result = response[0] if isinstance(response, list) and response else response
    if not isinstance(result, dict):
        raise StorageError("document save returned invalid evidence")
    status = result.get("status")
    if status in {"revision_conflict", "not_found"}:
        return result
    if status != "saved":
        if result.get("code") == "account_unavailable":
            raise ArtifactAccountUnavailable("account is unavailable")
        raise StorageError(str(result.get("code") or "document save blocked"))
    if (
        result.get("document_id") != document
        or not _revision_evidence_matches(
            result.get("document_revision"), updated_at
        )
        or not isinstance(result.get("render_changed"), bool)
    ):
        raise StorageError("document save evidence mismatch")
    artifacts = result.get("artifacts") or []
    if not isinstance(artifacts, list) or any(not isinstance(row, dict) for row in artifacts):
        raise StorageError("document save cleanup evidence is invalid")
    return result


def _claim_cleanup_row(
    row: dict,
    *,
    verified_owner: str,
    force: bool,
) -> tuple[str, str] | None:
    """Claim one queue identity; return ``(claim_token, key)`` when deletable."""
    cleanup_id = _canonical_uuid(row.get("cleanup_id"), field="cleanup id")
    expected_key = _validated_row_key(row, verified_owner)
    claim_token = str(uuid.uuid4())
    try:
        response = _server_sb().rpc(
            "claim_pdf_artifact_cleanup",
            {
                "p_owner_id": verified_owner,
                "p_cleanup_id": cleanup_id,
                "p_claim_token": claim_token,
                "p_force": bool(force),
            },
        ).execute().data
    except Exception as exc:  # noqa: BLE001 - normalize database transport errors
        raise StorageError(
            f"PDF artifact cleanup claim failed: {type(exc).__name__}"
        ) from exc
    result = response[0] if isinstance(response, list) and response else response
    if not isinstance(result, dict):
        raise StorageError("PDF artifact cleanup claim returned invalid evidence")
    status = result.get("status")
    if status in {"active", "busy", "missing", "pending"}:
        return None
    if status != "claimed":
        raise StorageError(str(result.get("code") or "PDF artifact cleanup blocked"))
    if (
        result.get("cleanup_id") != cleanup_id
        or result.get("owner_id") != verified_owner
        or result.get("claim_token") != claim_token
        or result.get("document_id") != row.get("document_id")
        or result.get("content_hash") != row.get("content_hash")
        or result.get("generation_id") != row.get("generation_id")
        or result.get("r2_key") != expected_key
    ):
        raise StorageError("PDF artifact cleanup claim evidence mismatch")
    _validated_row_key(result, verified_owner)
    return claim_token, expected_key


def delete_detached_artifacts(
    rows: list[dict],
    *,
    strict: bool = True,
    force: bool = False,
) -> int:
    """Delete R2 objects captured by an atomic document-delete RPC.

    The database rows have already cascaded with the document. Validate every
    returned content-addressed identity against the authenticated owner before
    issuing any object deletion, then delete only those canonical keys.
    """
    if not rows:
        return 0
    verified_owner = _verified_owner_id()
    validated: list[tuple[str, dict]] = []
    for row in rows:
        cleanup_id = _canonical_uuid(row.get("cleanup_id"), field="cleanup id")
        _validated_row_key(row, verified_owner)
        validated.append((cleanup_id, row))
    deleted = 0
    failures = 0
    for cleanup_id, row in validated:
        try:
            claim = _claim_cleanup_row(
                row,
                verified_owner=verified_owner,
                force=force,
            )
        except Exception:
            failures += 1
            continue
        if claim is None:
            continue
        claim_token, key = claim
        try:
            delete_pdf(key)
        except Exception:
            try:
                _server_sb().rpc(
                    "record_pdf_artifact_cleanup_failure",
                    {
                        "p_owner_id": verified_owner,
                        "p_cleanup_id": cleanup_id,
                        "p_claim_token": claim_token,
                    },
                ).execute()
            except Exception:
                pass
            failures += 1
            continue
        try:
            # False is an idempotent success: another worker may have deleted
            # the same object and acknowledged the same queue row concurrently.
            _server_sb().rpc(
                "complete_pdf_artifact_cleanup",
                {
                    "p_owner_id": verified_owner,
                    "p_cleanup_id": cleanup_id,
                    "p_claim_token": claim_token,
                },
            ).execute()
        except Exception:
            failures += 1
            continue
        deleted += 1
    if failures and strict:
        raise StorageError(f"PDF artifact cleanup incomplete ({failures} failed)")
    return deleted


def retry_pending_artifact_cleanup(
    *,
    limit: int = 20,
    force: bool = False,
) -> int:
    """Consume a bounded batch of the authenticated owner's durable outbox."""
    bounded = max(1, min(int(limit), 100))
    query = (
        _server_sb()
        .table("pdf_artifact_cleanup_queue")
        .select(
            "id, document_id, r2_key, content_hash, generation_id, not_before"
        )
        .eq("owner_id", _verified_owner_id())
    )
    if not force:
        query = query.lte("not_before", datetime.now(timezone.utc).isoformat())
    rows = (
        query.order("attempt_count")
        .order("created_at")
        .limit(bounded)
        .execute()
        .data
        or []
    )
    normalized = [
        {
            "cleanup_id": row.get("id"),
            "document_id": row.get("document_id"),
            "r2_key": row.get("r2_key"),
            "content_hash": row.get("content_hash"),
            "generation_id": row.get("generation_id"),
            "not_before": row.get("not_before"),
        }
        for row in rows
    ]
    return delete_detached_artifacts(normalized, strict=False, force=force)


def pending_artifact_cleanup_count() -> int:
    """Return zero when the current owner has no durable cleanup work."""
    rows = (
        _server_sb()
        .table("pdf_artifact_cleanup_queue")
        .select("id")
        .eq("owner_id", _verified_owner_id())
        .limit(1)
        .execute()
        .data
        or []
    )
    return 1 if rows else 0


def _candidate_cleanup_row(expected_key: str, verified_owner: str) -> dict | None:
    """Return the durable candidate row for one exact generation key."""
    try:
        rows = (
            _server_sb()
            .table("pdf_artifact_cleanup_queue")
            .select(
                "id, document_id, r2_key, content_hash, generation_id, not_before"
            )
            .eq("owner_id", verified_owner)
            .eq("r2_key", expected_key)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        return None
    if len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    return {
        "cleanup_id": row.get("id"),
        "document_id": row.get("document_id"),
        "r2_key": row.get("r2_key"),
        "content_hash": row.get("content_hash"),
        "generation_id": row.get("generation_id"),
        "not_before": row.get("not_before"),
    }


def _discard_upload_candidate(expected_key: str, verified_owner: str) -> None:
    """Best-effort immediate cleanup after an explicit non-publication result."""
    row = _candidate_cleanup_row(expected_key, verified_owner)
    if row is not None:
        delete_detached_artifacts([row], strict=False, force=True)


def _reconcile_publication(
    *,
    document_id: str,
    document_revision: object,
    chash: str,
    expected_key: str,
    verified_owner: str,
    dependency_document_id: str | None,
    dependency_revision: object | None,
) -> dict | None:
    """Resolve an ambiguous publish response without deleting a possible winner."""
    winner = _resolve_current_artifact(
        document_id=document_id,
        document_revision=document_revision,
        chash=chash,
        owner_id=verified_owner,
        dependency_document_id=dependency_document_id,
        dependency_revision=dependency_revision,
    )
    if winner is None:
        return None
    winner_key = _validated_row_key(winner, verified_owner)
    if winner_key != expected_key:
        _discard_upload_candidate(expected_key, verified_owner)
    return {
        **winner,
        "status": "published" if winner_key == expected_key else "cached",
        "artifacts": [],
    }


def _insert_artifact(
    document_id: str,
    r2_key: str,
    chash: str,
    page_count: int,
    document_revision: object,
    generation_id: str,
    dependency_document_id: str | None = None,
    dependency_revision: object | None = None,
) -> dict:
    """Publish server-authored artifact metadata under document-revision CAS."""
    verified_owner = _verified_owner_id()
    generation = _canonical_uuid(generation_id, field="generation id")
    expected_key = _r2_key(verified_owner, document_id, chash, generation)
    if r2_key != expected_key:
        raise StorageError("PDF artifact publication key mismatch")
    rpc_error: Exception | None = None
    try:
        response = _server_sb().rpc(
            "publish_pdf_artifact",
            {
                "p_owner_id": verified_owner,
                "p_document_id": document_id,
                "p_document_revision": document_revision,
                "p_content_hash": chash,
                "p_generation_id": generation,
                "p_page_count": int(page_count),
                "p_dependency_document_id": dependency_document_id,
                "p_dependency_revision": dependency_revision,
            },
        ).execute().data
    except Exception as exc:  # noqa: BLE001 — normalize database transport errors
        rpc_error = exc
        response = None

    result = response[0] if isinstance(response, list) and response else response
    if rpc_error is not None or not isinstance(result, dict):
        # A transport failure or malformed body is ambiguous: the transaction
        # may already have committed. Resolve authoritative metadata first and
        # never directly delete the prospective winner. If resolution is still
        # a miss, the candidate outbox remains the cleanup authority.
        try:
            reconciled = _reconcile_publication(
                document_id=document_id,
                document_revision=document_revision,
                chash=chash,
                expected_key=expected_key,
                verified_owner=verified_owner,
                dependency_document_id=dependency_document_id,
                dependency_revision=dependency_revision,
            )
        except ArtifactRevisionConflict:
            raise
        except ArtifactAccountUnavailable:
            raise
        except Exception as exc:
            cause = rpc_error or exc
            raise StorageError("PDF artifact publication reconciliation failed") from cause
        if reconciled is not None:
            return reconciled
        if rpc_error is not None:
            raise StorageError(
                f"PDF artifact publication failed: {type(rpc_error).__name__}"
            ) from rpc_error
        raise StorageError("PDF artifact publication returned invalid evidence")

    status = result.get("status")
    if status == "revision_conflict":
        _discard_upload_candidate(expected_key, verified_owner)
        raise ArtifactRevisionConflict("document changed during PDF render")
    if status not in {"published", "cached"}:
        _discard_upload_candidate(expected_key, verified_owner)
        if result.get("code") == "account_unavailable":
            raise ArtifactAccountUnavailable("account is unavailable")
        raise StorageError(str(result.get("code") or "PDF artifact publication blocked"))

    try:
        result_key = _validated_row_key(result, verified_owner)
        result_page_count = int(result.get("page_count") or 0)
        evidence_valid = (
            result.get("document_id") == document_id
            and result.get("content_hash") == chash
            and result_page_count >= 1
        )
    except (StorageError, TypeError, ValueError):
        evidence_valid = False
        result_key = ""
    if not evidence_valid:
        reconciled = _reconcile_publication(
            document_id=document_id,
            document_revision=document_revision,
            chash=chash,
            expected_key=expected_key,
            verified_owner=verified_owner,
            dependency_document_id=dependency_document_id,
            dependency_revision=dependency_revision,
        )
        if reconciled is not None:
            return reconciled
        raise StorageError("PDF artifact publication evidence mismatch")

    discard_key = result.get("discard_r2_key")
    if discard_key is not None:
        if discard_key != expected_key or result_key == expected_key:
            raise StorageError("PDF artifact discard evidence mismatch")
        _discard_upload_candidate(expected_key, verified_owner)
    cleanup = result.get("artifacts") or []
    if not isinstance(cleanup, list) or any(not isinstance(row, dict) for row in cleanup):
        raise StorageError("PDF artifact publication cleanup evidence is invalid")
    # Publication is authoritative even if cleanup of superseded objects is
    # temporarily deferred; the durable queue is consumed by request retries
    # and the autonomous cleanup worker.
    delete_detached_artifacts(cleanup, strict=False)
    return result


def _register_upload_candidate(
    *,
    document_id: str,
    document_revision: object,
    chash: str,
    generation_id: str,
    dependency_document_id: str | None = None,
    dependency_revision: object | None = None,
) -> dict:
    """Persist cleanup ownership for a unique key before any R2 PUT."""
    owner = _verified_owner_id()
    expected_key = _r2_key(owner, document_id, chash, generation_id)
    try:
        response = _server_sb().rpc(
            "register_pdf_upload_candidate",
            {
                "p_owner_id": owner,
                "p_document_id": document_id,
                "p_document_revision": document_revision,
                "p_content_hash": chash,
                "p_generation_id": generation_id,
                "p_dependency_document_id": dependency_document_id,
                "p_dependency_revision": dependency_revision,
            },
        ).execute().data
    except Exception as exc:  # noqa: BLE001 - normalize database transport errors
        raise StorageError(
            f"PDF upload candidate registration failed: {type(exc).__name__}"
        ) from exc
    result = response[0] if isinstance(response, list) and response else response
    if not isinstance(result, dict):
        raise StorageError("PDF upload candidate registration returned invalid evidence")
    if result.get("status") == "revision_conflict":
        raise ArtifactRevisionConflict("document changed during PDF render")
    if result.get("status") != "registered":
        if result.get("code") == "account_unavailable":
            raise ArtifactAccountUnavailable("account is unavailable")
        raise StorageError("PDF upload candidate registration was blocked")
    if (
        result.get("document_id") != document_id
        or result.get("content_hash") != chash
        or result.get("generation_id") != generation_id
        or result.get("r2_key") != expected_key
    ):
        raise StorageError("PDF upload candidate evidence mismatch")
    _canonical_uuid(result.get("cleanup_id"), field="cleanup id")
    return result


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
    document_revision: object,
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
    for _repair_attempt in range(2):
        hit = _resolve_current_artifact(
            document_id=document_id,
            document_revision=document_revision,
            chash=chash,
            owner_id=verified_owner,
        )
        if hit is None:
            break
        if _r2_object_exists_strict(hit["r2_key"]):
            url = presigned_get(hit["r2_key"], filename="cv.pdf")
            rendered = RenderedPdf(
                overflowed=False,
                page_count=hit.get("page_count") or 1,
                pdf_bytes=None,
                density=density,
            )
            return rendered, BuiltPdf(
                url=url,
                cached=True,
                page_count=rendered.page_count,
            )
        outcome = _evict_broken_artifact(
            document_id=document_id,
            document_revision=document_revision,
            artifact=hit,
        )
        if outcome == "evicted":
            break
    else:
        raise StorageError("PDF cache repair did not converge")

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

    generation_id = str(uuid.uuid4())
    candidate = _register_upload_candidate(
        document_id=document_id,
        document_revision=document_revision,
        chash=chash,
        generation_id=generation_id,
    )
    key = candidate["r2_key"]
    try:
        put_pdf(key, rendered.pdf_bytes or b"", filename="cv.pdf")
    except Exception:
        # PUT failures are ambiguous at the network boundary. The registered
        # generation stays in the durable outbox and is reclaimed after its
        # build lease instead of risking a late-write orphan.
        raise
    publication = _insert_artifact(
        document_id,
        key,
        chash,
        rendered.page_count,
        document_revision,
        generation_id,
    )
    published_key = publication["r2_key"]
    url = presigned_get(published_key, filename="cv.pdf")
    built = BuiltPdf(
        url=url,
        cached=publication.get("status") == "cached",
        page_count=int(publication.get("page_count") or rendered.page_count),
    )
    return rendered, built


def build_cover_letter_pdf(
    *,
    document_id: str,
    owner_id: str,
    cache_key_text: str,
    render_doc: dict[str, Any],
    document_revision: object,
    dependency_document_id: str | None = None,
    dependency_revision: object | None = None,
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
    chash = cover_letter_content_hash(cache_key_text)
    for _repair_attempt in range(2):
        hit = _resolve_current_artifact(
            document_id=document_id,
            document_revision=document_revision,
            chash=chash,
            owner_id=verified_owner,
            dependency_document_id=dependency_document_id,
            dependency_revision=dependency_revision,
        )
        if hit is None:
            break
        if _r2_object_exists_strict(hit["r2_key"]):
            return BuiltPdf(
                url=presigned_get(hit["r2_key"], filename="cover-letter.pdf"),
                cached=True,
                page_count=hit.get("page_count") or 1,
            )
        outcome = _evict_broken_artifact(
            document_id=document_id,
            document_revision=document_revision,
            artifact=hit,
        )
        if outcome == "evicted":
            break
    else:
        raise StorageError("cover-letter PDF cache repair did not converge")

    pdf_bytes = render_cover_letter(render_doc, base_url=base_url)
    page_count = _count_pdf_pages(pdf_bytes)
    generation_id = str(uuid.uuid4())
    candidate = _register_upload_candidate(
        document_id=document_id,
        document_revision=document_revision,
        chash=chash,
        generation_id=generation_id,
        dependency_document_id=dependency_document_id,
        dependency_revision=dependency_revision,
    )
    key = candidate["r2_key"]
    try:
        put_pdf(key, pdf_bytes, filename="cover-letter.pdf")
    except Exception:
        raise
    publication = _insert_artifact(
        document_id,
        key,
        chash,
        page_count,
        document_revision,
        generation_id,
        dependency_document_id,
        dependency_revision,
    )
    published_key = publication["r2_key"]
    return BuiltPdf(
        url=presigned_get(published_key, filename="cover-letter.pdf"),
        cached=publication.get("status") == "cached",
        page_count=int(publication.get("page_count") or page_count),
    )


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
