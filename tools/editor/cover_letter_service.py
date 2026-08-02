"""Cover-letter generation + persistence service (Stage 5).

This sits between the editor's cover-letter routes and the engine. It:

  1. loads the user's CV (RLS-scoped) as the SOLE source of candidate facts;
  2. parses the pasted JD into keyed JSON (``parse_jd``);
  3. assembles the grounded prompt (CV JSON + JD JSON + tone);
  4. resolves the key/credit via ``llm_gateway.resolve_llm('cover_letter_generate')``
     — the exact Stage 4 BYO-key → managed-credit → needs_key path;
  5. calls the provider-agnostic ``LLMClient.complete``;
  6. runs the POST-GEN GUARDS (cliché scan, anti-fabrication cross-reference,
     word cap) — a hallucinated employer or invented metric BLOCKS the save;
  7. stores the result as a ``documents`` row with ``type='cover_letter'``,
     reusing the same RLS-scoped table the CVs use (generalized, not
     duplicated);
  8. logs a ``usage_events`` row on the BYO path (the managed path is logged
     inside the RPC).

The document storage helpers here are deliberately ``type``-parameterized so
the Stage 3 multi-doc CRUD (list / rename / delete) generalizes to cover
letters with no duplicated SQL.

The cover-letter ``yaml_blob`` is a small structured payload (stored as YAML
text to match the ``documents.yaml_blob`` column the CV uses). It now carries
the full business-letter envelope so the rendered PDF reads like a finished
letter rather than bare body prose:

    recipient, subject, salutation, body, closing, attachments,
    company, role, tone, language, source_cv_id, job_id

``recipient`` is a SCALAR string (a name and/or organization line), NOT a list.
``attachments`` is a list of enclosure labels (e.g. ["CV"]). All envelope fields
are optional — the renderer derives sensible EN/DE defaults when they are
absent, so old blobs (written before these fields existed) still render.
"""
from __future__ import annotations

import unicodedata
from typing import Any

import yaml

import auth as auth_mod
import llm_gateway
import local_demo_store

# engine/ is on sys.path in every entrypoint (server boot, tests).
try:
    from cover_letter import (
        COVER_LETTER_MAX_TOKENS,
        build_cover_letter_messages,
        parse_jd,
        run_guards,
    )
    from cover_letter.guards import GroundingError, WordCapError
    from llm.client import canonical_provider, make_client, resolve_model
except ImportError:  # pragma: no cover - unusual import roots
    from engine.cover_letter import (
        COVER_LETTER_MAX_TOKENS,
        build_cover_letter_messages,
        parse_jd,
        run_guards,
    )
    from engine.cover_letter.guards import GroundingError, WordCapError
    from engine.llm.client import canonical_provider, make_client, resolve_model

GENERATE_KIND = "cover_letter_generate"
DEFAULT_NAME = "Cover letter"
COVER_ENVELOPE_MAX_BYTES = 64 * 1024
COVER_BODY_MAX_CHARS = 32_000
COVER_ATTACHMENT_MAX_COUNT = 20
COVER_ATTACHMENT_MAX_CHARS = 200
DOCUMENT_LOOKUP_BATCH_SIZE = 100
DOCUMENT_LIST_PAGE_SIZE = 500


def _doc_sb():
    if local_demo_store.active():
        return local_demo_store.db()
    return auth_mod.user_supabase()


class CoverLetterError(Exception):
    """Raised for a recoverable generation failure (maps to a 4xx/5xx JSON).

    ``status`` is the HTTP status; ``payload`` is the JSON body the route
    returns — so a grounding block or word-cap rejection maps cleanly.
    """

    def __init__(self, status: int, payload: dict) -> None:
        super().__init__(payload.get("error", "cover-letter error"))
        self.status = status
        self.payload = payload


class DocumentInsertError(Exception):
    """An atomic document insert was rejected by current account/link state."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# yaml_blob (de)serialization — small structured payload.
# ---------------------------------------------------------------------------
_BLOB_FIELDS = (
    # Core letter content + linkage (existing — order preserved for old blobs).
    "recipient",
    "company",
    "role",
    "tone",
    "body",
    "language",
    "source_cv_id",
    "sender_name",
    "job_id",
    # Business-letter envelope (NEW — all optional; renderer falls back to
    # EN/DE defaults when absent). ``recipient`` above is the addressee line;
    # these complete the letter around the guarded body.
    "subject",  # str       — overrides the auto "Application for <role>" subject
    "salutation",  # str    — overrides the auto "Dear <recipient>," / "Sehr geehrte…"
    "closing",  # str       — overrides the auto "Best regards," / "Mit freundlichen Grüßen"
    "attachments",  # list[str] — enclosure labels ("CV") → "Enclosures"/"Anlagen"
)

_ENVELOPE_TEXT_LIMITS = {
    "recipient": 500,
    "company": 500,
    "role": 500,
    "tone": 200,
    "body": COVER_BODY_MAX_CHARS,
    "language": 16,
    "source_cv_id": 128,
    "sender_name": 500,
    "job_id": 128,
    "subject": 500,
    "salutation": 500,
    "closing": 500,
}


def _envelope_error(
    *, field: str, message: str, code: str, status: int = 400, **details: Any
) -> CoverLetterError:
    return CoverLetterError(
        status,
        {"error": message, "code": code, "field": field, **details},
    )


def _contains_forbidden_control(text: str, *, multiline: bool) -> bool:
    """Reject controls/invisible format characters instead of deleting them."""
    for char in text:
        if char in "\t\r\n":
            if multiline:
                continue
            return True
        if unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            return True
    return False


def _normalize_text(
    field: str,
    value: object,
    *,
    max_chars: int,
    multiline: bool = False,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _envelope_error(
            field=field,
            message=f"'{field}' must be a string",
            code="invalid_cover_envelope_type",
        )
    if _contains_forbidden_control(value, multiline=multiline):
        raise _envelope_error(
            field=field,
            message=f"'{field}' contains control characters",
            code="invalid_cover_envelope_control",
        )
    normalized = value if multiline else value.strip()
    if len(normalized) > max_chars:
        raise _envelope_error(
            field=field,
            message=f"'{field}' is too long",
            code="cover_envelope_field_too_large",
            status=413,
            max_chars=max_chars,
        )
    return normalized


def _normalize_attachments(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if _contains_forbidden_control(value, multiline=True):
            raise _envelope_error(
                field="attachments",
                message="'attachments' contains control characters",
                code="invalid_cover_envelope_control",
            )
        items: list[object] = value.splitlines()
    elif isinstance(value, list):
        items = value
    else:
        raise _envelope_error(
            field="attachments",
            message="'attachments' must be a list of strings or newline-delimited string",
            code="invalid_cover_envelope_type",
        )

    if len(items) > COVER_ATTACHMENT_MAX_COUNT:
        raise _envelope_error(
            field="attachments",
            message="too many attachments",
            code="cover_envelope_attachment_count",
            status=413,
            max_items=COVER_ATTACHMENT_MAX_COUNT,
        )

    normalized: list[str] = []
    for index, item in enumerate(items):
        if item is None:
            continue
        if not isinstance(item, str):
            raise _envelope_error(
                field="attachments",
                message="each attachment must be a string",
                code="invalid_cover_envelope_type",
                index=index,
            )
        label = _normalize_text(
            "attachments",
            item,
            max_chars=COVER_ATTACHMENT_MAX_CHARS,
        )
        if label:
            normalized.append(label)
    return normalized


def normalize_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Return a type-stable, bounded envelope suitable for persistence."""
    if not isinstance(data, dict):
        raise CoverLetterError(
            400,
            {
                "error": "cover-letter envelope must be an object",
                "code": "invalid_cover_envelope_type",
            },
        )
    payload: dict[str, Any] = {}
    for field in _BLOB_FIELDS:
        if field == "attachments":
            payload[field] = _normalize_attachments(data.get(field))
            continue
        payload[field] = _normalize_text(
            field,
            data.get(field),
            max_chars=_ENVELOPE_TEXT_LIMITS[field],
            multiline=field == "body",
        )
    if payload.get("language"):
        payload["language"] = payload["language"].lower()
    return payload


def normalize_generation_inputs(
    *, tone: object, recipient: object, company_context: object
) -> tuple[str, str, str]:
    """Validate prompt-bound generation fields before an AI call is made."""
    normalized_tone = _normalize_text("tone", tone, max_chars=200) or "auto"
    normalized_recipient = _normalize_text("recipient", recipient, max_chars=500) or ""
    normalized_context = _normalize_text(
        "company_context", company_context, max_chars=2000, multiline=True
    ) or ""
    return normalized_tone, normalized_recipient, normalized_context


def serialize_blob(data: dict[str, Any]) -> str:
    """Serialize a cover-letter payload to the YAML text stored in the row."""
    payload = normalize_envelope(data)
    serialized = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=120)
    size = len(serialized.encode("utf-8"))
    if size > COVER_ENVELOPE_MAX_BYTES:
        raise CoverLetterError(
            413,
            {
                "error": "cover-letter envelope is too large",
                "code": "cover_envelope_too_large",
                "max_bytes": COVER_ENVELOPE_MAX_BYTES,
            },
        )
    return serialized


def parse_blob(yaml_text: str | None) -> dict[str, Any]:
    """Parse a stored cover-letter ``yaml_blob`` back into a dict (lenient)."""
    if not yaml_text:
        return {k: None for k in _BLOB_FIELDS}
    try:
        parsed = yaml.safe_load(yaml_text)
    except Exception:  # noqa: BLE001 — a malformed blob shouldn't 500 a read
        parsed = None
    if not isinstance(parsed, dict):
        parsed = {}
    return {k: parsed.get(k) for k in _BLOB_FIELDS}


def normalized_blob_for_row(row: dict[str, Any]) -> dict[str, Any]:
    """Parse a document row and force authoritative row-level linkage.

    `documents.job_id` is the durable application-packet link. Older blobs may
    still contain a stale `job_id`; direct reads, previews, and PDF cache keys
    must ignore that stale value.
    """
    blob = parse_blob(row.get("yaml_blob"))
    blob["job_id"] = row.get("job_id")
    return blob


# ---------------------------------------------------------------------------
# Generalized document CRUD (type-parameterized; reuses the CV machinery).
# ---------------------------------------------------------------------------
def list_documents(doc_type: str) -> list[dict]:
    """All of the caller's non-deleted documents of ``doc_type``, newest first.

    RLS scopes the SELECT to ``owner_id = auth.uid()``. Mirrors the CV list
    query in server.py but parameterized by ``type`` so cover letters reuse it.
    """
    sb = _doc_sb()
    rows: list[dict] = []
    offset = 0
    while True:
        page = (
            sb.table("documents")
            .select(
                "id, name, slug, summary_company, summary_role, job_id, "
                "created_at, updated_at, last_used_at"
            )
            .eq("type", doc_type)
            .is_("deleted_at", "null")
            .order("last_used_at", desc=True)
            .order("updated_at", desc=True)
            .order("id")
            .range(offset, offset + DOCUMENT_LIST_PAGE_SIZE - 1)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < DOCUMENT_LIST_PAGE_SIZE:
            break
        offset += DOCUMENT_LIST_PAGE_SIZE
    if doc_type == "cover_letter":
        # Pre-summary-column letters retain their original company/role. Fetch
        # full blobs only for those legacy IDs; every newly created/saved row
        # remains on the bounded summary-only path above.
        legacy_ids = [
            str(row.get("id"))
            for row in rows
            if row.get("id")
            and (
                row.get("summary_company") is None
                or row.get("summary_role") is None
            )
        ]
        legacy_summaries: dict[str, tuple[str, str]] = {}
        for start in range(0, len(legacy_ids), DOCUMENT_LOOKUP_BATCH_SIZE):
            batch = legacy_ids[start : start + DOCUMENT_LOOKUP_BATCH_SIZE]
            legacy_rows = (
                sb.table("documents")
                .select("id, yaml_blob")
                .eq("type", "cover_letter")
                .is_("deleted_at", "null")
                .in_("id", batch)
                .execute()
                .data
                or []
            )
            for legacy in legacy_rows:
                blob = parse_blob(legacy.get("yaml_blob"))
                legacy_summaries[str(legacy.get("id"))] = (
                    (
                        str(blob.get("company") or "")[:500]
                        if isinstance(blob.get("company"), str)
                        else ""
                    ),
                    (
                        str(blob.get("role") or "")[:500]
                        if isinstance(blob.get("role"), str)
                        else ""
                    ),
                )
        for row in rows:
            summary = legacy_summaries.get(str(row.get("id")))
            if summary is not None:
                if row.get("summary_company") is None:
                    row["summary_company"] = summary[0]
                if row.get("summary_role") is None:
                    row["summary_role"] = summary[1]
    return rows


def job_titles_for(job_ids: list[str]) -> dict[str, str]:
    """Map each of ``job_ids`` the caller owns → its job's display title.

    Used by the unified document library to show "for <role> @ <company>" on a
    cover-letter card linked to a job (Stage 6's ``documents.job_id``). ONE
    grouped query over the caller's OWN ``saved_jobs`` rows — RLS scopes it to
    ``owner_id = auth.uid()``, so it can NEVER surface another tenant's job
    title (never a SECURITY DEFINER function/view or a service-role count).
    Avoids an N+1 per letter. A ``job_id`` the caller can't see (deleted job or
    — impossibly, since 0007 blocks cross-tenant stamping — another tenant's) is
    simply absent from the map. Returns ``{job_id: title}``.
    """
    ids = sorted({j for j in job_ids if j})
    if not ids:
        return {}
    out: dict[str, str] = {}
    sb = _doc_sb()
    for start in range(0, len(ids), DOCUMENT_LOOKUP_BATCH_SIZE):
        batch = ids[start : start + DOCUMENT_LOOKUP_BATCH_SIZE]
        resp = (
            sb.table("saved_jobs")
            .select("id, title")
            .in_("id", batch)
            .execute()
        )
        for r in resp.data or []:
            jid = r.get("id")
            title = (r.get("title") or "").strip()
            if jid and title:
                out[jid] = title
    return out


def document_by_id(doc_id: str, doc_type: str) -> dict | None:
    """The caller's document with ``doc_id`` of ``doc_type``, or None (→ 404).

    RLS hides another tenant's row, so ``None`` covers both "no such id" and
    "owned by someone else" — the route surfaces both as 404.
    """
    if local_demo_store.active():
        # Demo sessions can't query PostgREST (plan 107, F14/F19): serve the
        # seeded demo CV via the registered provider, else the ephemeral demo
        # documents table; a miss stays a 404, never a raw 500.
        if doc_type == "cv":
            demo_row = local_demo_store.demo_cv_row()
            if demo_row and demo_row.get("id") == doc_id:
                return demo_row
        resp = (
            local_demo_store.db()
            .table("documents")
            .select("id, name, slug, yaml_blob, job_id, created_at, updated_at, last_used_at")
            .eq("type", doc_type)
            .eq("id", doc_id)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0] if rows else None
    sb = _doc_sb()
    resp = (
        sb.table("documents")
        .select("id, name, slug, yaml_blob, job_id, created_at, updated_at, last_used_at")
        .eq("type", doc_type)
        .eq("id", doc_id)
        .is_("deleted_at", "null")
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def mark_document_used(doc_id: str, doc_type: str, *, now_iso: str) -> bool:
    """Bump open/select recency for one visible document owned by the caller."""
    sb = _doc_sb()
    resp = (
        sb.table("documents")
        .update({"last_used_at": now_iso})
        .eq("id", doc_id)
        .eq("type", doc_type)
        .is_("deleted_at", "null")
        .execute()
    )
    return bool(resp.data or [])


def insert_document(
    doc_type: str,
    name: str,
    yaml_blob: str,
    *,
    job_id: str | None = None,
    summary_company: str | None = None,
    summary_role: str | None = None,
    accent: str | None = None,
    font: str | None = None,
    density: str | None = None,
) -> dict:
    """Insert a document of ``doc_type`` for the caller; return the new row.

    Runs under the user's JWT so the INSERT satisfies the RLS WITH CHECK
    (``owner_id = auth.uid()``); ``owner_id`` defaults to ``auth.uid()`` in the
    schema, so it is never taken from the request body. An optional ``job_id``
    is stamped on the ``documents.job_id`` column so a job's detail view can
    group its documents by FK (not just by the value buried in the blob).
    """
    sb = _doc_sb()
    if local_demo_store.active():
        payload: dict[str, Any] = {
            "type": doc_type,
            "name": name,
            "yaml_blob": yaml_blob,
        }
        if job_id is not None:
            payload["job_id"] = job_id
        if doc_type == "cover_letter":
            payload["summary_company"] = (summary_company or "")[:500]
            payload["summary_role"] = (summary_role or "")[:500]
        else:
            payload.update({"accent": accent, "font": font, "density": density})
        rows = sb.table("documents").insert(payload).execute().data or []
        if not rows:
            raise RuntimeError(f"failed to insert {doc_type} document")
        return rows[0]

    response = sb.rpc(
        "insert_document_if_account_active",
        {
            "p_type": doc_type,
            "p_name": name,
            "p_yaml_blob": yaml_blob,
            "p_job_id": job_id,
            "p_accent": accent,
            "p_font": font,
            "p_density": density,
            "p_summary_company": summary_company,
            "p_summary_role": summary_role,
        },
    ).execute().data
    result = response[0] if isinstance(response, list) and response else response
    if not isinstance(result, dict):
        raise RuntimeError(f"failed to insert {doc_type} document")
    if result.get("status") != "created":
        raise DocumentInsertError(str(result.get("code") or "document_insert_blocked"))
    document = result.get("document")
    if not isinstance(document, dict) or not document.get("id"):
        raise RuntimeError(f"failed to insert {doc_type} document")
    return document


def update_document_blob(
    doc_id: str,
    doc_type: str,
    yaml_blob: str,
    *,
    now_iso: str,
    expected_revision: object | None = None,
) -> dict | None:
    """CAS-update a document blob and return the moved row, or ``None``."""
    sb = _doc_sb()
    query = (
        sb.table("documents")
        .update({"yaml_blob": yaml_blob, "updated_at": now_iso})
        .eq("id", doc_id)
        .eq("type", doc_type)
    )
    if expected_revision is not None:
        query = query.eq("updated_at", expected_revision)
    rows = query.execute().data or []
    return rows[0] if rows else None


def rename_document(doc_id: str, doc_type: str, name: str, *, now_iso: str) -> bool:
    """Rename a document. True iff a row moved (False → caller 404s)."""
    sb = _doc_sb()
    resp = (
        sb.table("documents")
        .update({"name": name, "updated_at": now_iso})
        .eq("id", doc_id)
        .eq("type", doc_type)
        .execute()
    )
    return bool(resp.data or [])


def delete_document(
    doc_id: str,
    doc_type: str,
    *,
    include_artifacts: bool = False,
) -> bool | dict[str, Any]:
    """Atomically hard-delete an unused document owned by the caller.

    Hosted tenants use the database RPC that locks the document before it
    checks packet/review lineage.  This closes the former SELECT/DELETE race
    and keeps authenticated callers from bypassing the history guard with a
    direct Data API DELETE.  The process-local demo store retains its direct
    in-memory mutation path while enforcing the same visible contract.

    Returns ``False`` when the document is absent/not owned.  Protected
    application history raises :class:`CoverLetterError` with HTTP 409.
    """
    if doc_type not in {"cv", "cover_letter"}:
        raise ValueError("unsupported document type")

    if local_demo_store.active():
        db = local_demo_store.db()
        has_packet = any(
            row.get("owner_id", db.owner_id) == db.owner_id
            and doc_id
            in {
                row.get("source_cv_id"),
                row.get("tailored_cv_id"),
                row.get("cover_letter_id"),
            }
            for row in db.tables.setdefault("application_packets", [])
        )
        has_review = any(
            row.get("owner_id", db.owner_id) == db.owner_id
            and row.get("cv_id") == doc_id
            for row in db.tables.setdefault("tailor_review_states", [])
        )
        if has_packet or has_review:
            raise CoverLetterError(
                409,
                {
                    "error": "This document has application history and cannot be deleted.",
                    "code": "document_has_application_history",
                },
            )
        resp = (
            db.table("documents")
            .delete()
            .eq("id", doc_id)
            .eq("type", doc_type)
            .execute()
        )
        deleted = bool(resp.data or [])
        if include_artifacts and deleted:
            return {"deleted": doc_id, "artifacts": []}
        return deleted

    response = auth_mod.user_supabase().rpc(
        "delete_document_safely",
        {"p_document_id": doc_id, "p_document_type": doc_type},
    ).execute()
    data = response.data
    if isinstance(data, list):
        result = data[0] if data and isinstance(data[0], dict) else {}
    else:
        result = data if isinstance(data, dict) else {}

    status = result.get("status")
    if status == "deleted" and str(result.get("deleted") or "") == doc_id:
        if include_artifacts:
            artifacts = result.get("artifacts")
            if not isinstance(artifacts, list) or any(not isinstance(row, dict) for row in artifacts):
                raise RuntimeError("safe document delete returned invalid artifact evidence")
            return {"deleted": doc_id, "artifacts": artifacts}
        return True
    if status == "not_found":
        return False
    if result.get("code") == "document_has_application_history":
        raise CoverLetterError(
            409,
            {
                "error": "This document has application history and cannot be deleted.",
                "code": "document_has_application_history",
            },
        )
    if result.get("code") == "document_artifact_invalid":
        raise CoverLetterError(
            503,
            {
                "error": "PDF artifact storage is temporarily unavailable.",
                "code": "artifact_cleanup_unavailable",
            },
        )
    if result.get("code") == "account_unavailable":
        raise CoverLetterError(
            409,
            {"error": "Account is unavailable.", "code": "account_unavailable"},
        )
    raise RuntimeError("safe document delete returned an invalid response")


# ---------------------------------------------------------------------------
# CV loading (RLS-scoped) — the SOLE source of candidate facts.
# ---------------------------------------------------------------------------
def load_cv_for_grounding(cv_id: str) -> tuple[dict, dict]:
    """Load + parse the caller's CV ``cv_id`` into the canonical model.

    Returns ``(cv_model, cv_row)``. Raises :class:`CoverLetterError` (404) if
    the CV isn't owned/visible (RLS). The parsed model is what both the
    grounded prompt and the anti-fabrication guard consume.
    """
    row = document_by_id(cv_id, "cv")
    if row is None:
        raise CoverLetterError(404, {"error": "CV not found", "code": "cv_not_found"})
    cv_model = _parse_cv_yaml(row.get("yaml_blob") or "")
    return cv_model, row


def _parse_cv_yaml(yaml_text: str) -> dict:
    """Parse CV YAML to a dict (lenient — grounding works on whatever's there)."""
    try:
        parsed = yaml.safe_load(yaml_text)
    except Exception as exc:  # noqa: BLE001
        raise CoverLetterError(
            400, {"error": f"CV YAML invalid: {exc}", "code": "cv_invalid"}
        ) from exc
    if not isinstance(parsed, dict):
        raise CoverLetterError(
            400, {"error": "CV is empty or malformed", "code": "cv_invalid"}
        )
    return parsed


# ---------------------------------------------------------------------------
# The generation entry point.
# ---------------------------------------------------------------------------
def generate_cover_letter(
    *,
    cv_id: str,
    jd_text: str,
    tone: str = "auto",
    recipient: str = "",
    company_context: str = "",
    job_id: str | None = None,
    jd_title: str | None = None,
    jd_company: str | None = None,
    provider: str = "anthropic",
    model: str | None = None,
    now_iso: str,
) -> dict[str, Any]:
    """Generate, guard, and store a grounded cover letter.

    Steps mirror the module docstring. On success returns a dict the route
    JSON-ifies::

        {
          "id": <new cover_letter document id>,
          "name": <document name>,
          "body": <cleaned letter prose>,
          "word_count": <int>,
          "cliches_removed": [{phrase, suggestion}, ...],
          "used_managed_credit": <bool>,
          "language": <jd language>,
          "company": <jd company or "">,
          "role": <jd title or "">,
        }

    Raises :class:`CoverLetterError` for a 404 (CV not found), a guard block
    (``unverified_claims`` / word cap), or an LLM failure. Lets
    :class:`llm_gateway.LLMUnavailable` propagate so the route's existing
    handler maps the 402/503 credit/key responses.
    """
    tone, recipient, company_context = normalize_generation_inputs(
        tone=tone,
        recipient=recipient,
        company_context=company_context,
    )

    # 1) CV (RLS) — the only source of candidate facts.
    cv_model, _cv_row = load_cv_for_grounding(cv_id)
    sender_name = str(cv_model.get("name") or "").strip()
    if not sender_name:
        raise CoverLetterError(
            422,
            {
                "error": "The source CV has no candidate name.",
                "code": "source_cv_name_missing",
            },
        )

    # 2) (SECURITY MUST-FIX) If this letter will be stamped with a job_id, verify
    #    the caller OWNS that job BEFORE any AI call or document write.
    #    Defense-in-depth with the DB ``with check`` (migration 0007): a request
    #    can never stamp another tenant's job onto a document. Lazy import avoids
    #    the jobs_service ⇄ cover_letter_service circular import.
    if job_id is not None:
        import jobs_service as _jobs_svc

        _jobs_svc.assert_job_owned(job_id)  # JobError 404 if invisible/not owned

    # 3) Parse the JD into keyed JSON first.
    try:
        jd_json = parse_jd(jd_text, title=jd_title, company=jd_company)
    except ValueError as exc:
        raise CoverLetterError(
            400, {"error": str(exc), "code": "empty_jd"}
        ) from exc

    # Bound parser-derived envelope metadata before it reaches either the
    # prompt or persisted YAML. The parser already returns strings, but this is
    # the authoritative per-field boundary for direct service callers too.
    metadata = normalize_envelope(
        {
            "company": jd_json.get("company"),
            "role": jd_json.get("title"),
            "language": jd_json.get("language"),
        }
    )
    jd_json = {
        **jd_json,
        "company": metadata.get("company"),
        "title": metadata.get("role"),
        "language": metadata.get("language"),
    }

    # 3) Build the grounded prompt (CV JSON + parsed JD JSON + tone).
    system, user = build_cover_letter_messages(
        cv=cv_model,
        jd_json=jd_json,
        tone=tone,
        recipient=recipient,
        company_context=company_context,
    )

    # 4) Resolve the key/credit (Stage 4 path) for the SELECTED provider: BYO
    #    key → managed credit (Anthropic only) → needs_key. LLMUnavailable
    #    propagates to the route handler. ``prov`` is the canonical provider the
    #    key was decrypted UNDER.
    prov = canonical_provider(provider)
    api_key, used_managed = llm_gateway.resolve_llm(GENERATE_KIND, provider=prov)

    # 5) Provider-agnostic completion. SECURITY: the provider that built the
    #    client MUST equal the provider the key was decrypted under — never send
    #    a Google key to Anthropic or vice-versa.
    client = make_client(prov, api_key)
    client_provider = getattr(client, "provider", prov)
    if client_provider not in (prov, "stub"):
        raise CoverLetterError(
            502,
            {"error": "AI provider routing failed.", "code": "provider_mismatch"},
        )
    try:
        raw_letter = client.complete(
            system=system,
            user=user,
            model=resolve_model(prov, model),
            max_tokens=COVER_LETTER_MAX_TOKENS,
            cache_system=True,
        )
    except Exception as exc:  # noqa: BLE001 — provider/timeout/refusal
        raise CoverLetterError(
            502,
            {"error": f"AI generation failed: {type(exc).__name__}", "code": "llm_failed"},
        ) from exc

    if not raw_letter or not raw_letter.strip():
        raise CoverLetterError(
            502, {"error": "AI returned an empty letter — try again.", "code": "llm_empty"}
        )

    # 6) POST-GEN GUARDS — block the save on a fabricated fact or word-cap bust.
    #    The JD is passed as grounding context so the candidate can name the
    #    employer/role they're applying to (G2) without tripping the guard —
    #    only a proper noun / number grounded in NEITHER the CV nor the JD
    #    (e.g. a hallucinated "Globex Industries") blocks the save.
    #    The caller-supplied recipient (a named hiring contact, e.g. "Frau Dr.
    #    Schmidt") is legitimately named in the salutation, so it must ground
    #    too — thread it through the JD context's recipient slot (which
    #    _jd_application_context_corpus already reads) so an addressed letter is
    #    not falsely blocked as an unverified proper noun.
    if recipient and isinstance(jd_json, dict) and not jd_json.get("recipient"):
        jd_json = {**jd_json, "recipient": recipient}
    try:
        result = run_guards(raw_letter, cv_model, jd_context=jd_json)
    except GroundingError as exc:
        # A hallucinated employer/metric never ships. Report the offenders.
        raise CoverLetterError(
            422, {"error": "unverified_claims", "items": exc.items}
        ) from exc
    except WordCapError as exc:
        raise CoverLetterError(
            422, {"error": str(exc), "code": "word_cap", "word_count": exc.count}
        ) from exc

    # 7) Store as a cover_letter document (reusing the documents table). The
    #    envelope fields (subject/attachments) make the rendered PDF a complete
    #    business letter; salutation/closing are left None so the renderer
    #    derives them per language (keeps the guarded body the source of truth).
    language = jd_json.get("language") or None
    role_title = jd_json.get("title") or None
    blob = serialize_blob(
        {
            "recipient": recipient or None,
            "company": jd_json.get("company") or None,
            "role": role_title,
            "tone": tone or "auto",
            "body": result.text,
            "language": language,
            "source_cv_id": cv_id,
            # Immutable render snapshot. A standalone letter remains complete
            # if its source CV is later deleted, and export never silently
            # drops the signature during a transient source lookup failure.
            "sender_name": sender_name,
            "job_id": job_id,
            "subject": _default_subject(role_title, language),
            "salutation": None,  # renderer derives from recipient + language
            "closing": None,  # renderer derives "Best regards," / "Mit freundlichen Grüßen"
            "attachments": _default_attachments(language),
        }
    )
    name = _doc_name(jd_json)
    # Stamp the job link on the documents.job_id column too (not only inside the
    # blob) so a job's detail view groups its letters by FK.
    row = insert_document(
        "cover_letter",
        name,
        blob,
        job_id=job_id,
        summary_company=jd_json.get("company") or "",
        summary_role=role_title or "",
    )

    # 8) Audit the BYO path (managed path already logged inside the RPC).
    if not used_managed:
        llm_gateway.log_byo_usage(GENERATE_KIND, provider=prov)

    return {
        "id": row["id"],
        "name": row.get("name") or name,
        "updated_at": row.get("updated_at"),
        "body": result.text,
        "word_count": result.word_count,
        "cliches_removed": [
            {"phrase": c.phrase, "suggestion": c.suggestion} for c in result.cliches_removed
        ],
        "used_managed_credit": used_managed,
        "language": jd_json.get("language") or "unknown",
        "company": jd_json.get("company") or "",
        "role": jd_json.get("title") or "",
    }


def _doc_name(jd_json: dict[str, Any]) -> str:
    """A human document name from the JD (role @ company), capped."""
    role = (jd_json.get("title") or "").strip()
    company = (jd_json.get("company") or "").strip()
    if role and company:
        base = f"{role} — {company}"
    elif role:
        base = role
    elif company:
        base = f"{DEFAULT_NAME} — {company}"
    else:
        base = DEFAULT_NAME
    return base[:200]


def _is_german(language: str | None) -> bool:
    """True iff the letter language is German (drives EN/DE label defaults)."""
    return (language or "").strip().lower() == "de"


def _default_subject(role: str | None, language: str | None) -> str | None:
    """Auto subject line from the role (EN/DE). None when no role is known.

    The renderer derives the same fallback, so this is an explicit override that
    survives an edit; English labels now, German handled via the language hook
    (full DE i18n is Stage 8).
    """
    r = (role or "").strip()
    if not r:
        return None
    return f"Bewerbung als {r}" if _is_german(language) else f"Application for {r}"


def _default_attachments(language: str | None) -> list[str]:
    """Default enclosure list (the CV travels with the letter), EN/DE label."""
    return ["Lebenslauf"] if _is_german(language) else ["CV"]


def build_render_doc(blob: dict[str, Any], *, name: str, sender_name: str = "") -> dict[str, Any]:
    """Map a stored cover-letter blob into the renderer's ``doc`` shape.

    Keeps the renderer decoupled from storage: the service owns the mapping.
    Threads the full business-letter envelope (subject/salutation/closing/
    attachments) through to the renderer so the rendered PDF is a complete
    letter, not bare body prose. Each envelope field is optional; the renderer
    falls back to its EN/DE defaults when a value is empty/None.
    """
    language = blob.get("language") or "en"
    attachments = blob.get("attachments")
    if not isinstance(attachments, list):
        attachments = [attachments] if attachments else []
    attachments = [str(a).strip() for a in attachments if a is not None and str(a).strip()]
    return {
        "sender_name": sender_name or "",
        "recipient": blob.get("recipient") or "",
        "company": blob.get("company") or "",
        "role": blob.get("role") or "",
        "subject": blob.get("subject") or "",
        "salutation": blob.get("salutation") or "",
        "body": blob.get("body") or "",
        "closing": blob.get("closing") or "",
        "attachments": attachments,
        "language": language,
        "signer_name": sender_name or "",
    }
