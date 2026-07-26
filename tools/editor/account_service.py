"""Account self-service — GDPR data portability + erasure (Stage 10).

This is the server-side data layer for the account page. Three jobs, all
RLS-scoped to the caller via :func:`auth.user_supabase` so a request can only
ever touch its own rows:

  * :func:`export_account_data` — structured live-data export. Gathers the
    user's active profile, live documents (CVs + cover letters, full
    ``yaml_blob``), saved jobs, import provenance, live application-packet
    lineage, and non-secret key hints. It intentionally is not described as
    every stored record: operational/audit rows, deleted/archived records,
    rendered files, uploaded photos, and encrypted key material are excluded.

  * :func:`request_account_deletion` — Right to erasure (GDPR Art. 17), phase 1.
    Calls the SECURITY DEFINER RPC ``request_account_deletion()`` which stamps
    ``profiles.deletion_requested_at = now()`` and soft-deletes the user's
    documents (``deleted_at = now()``) so they vanish from every list/get
    endpoint immediately. After D+30, the production worker first removes
    external object/file storage and then admin-hard-deletes the Auth user so
    owned database rows cascade.

  * :func:`cancel_account_deletion` — undo a pending erasure inside the grace
    window (clears the flag + restores the soft-deleted documents).

  * :func:`update_profile` — edit the safe, user-writable profile columns
    (``display_name``, ``default_language``). The column-UPDATE grant from
    migration 0002 means a write touching ``managed_credits_remaining`` (or the
    new ``deletion_requested_at``) is rejected by Postgres column privileges —
    this function never sends those columns, and the DB enforces it regardless.

Why a thin service module rather than inlining in ``server.py``: it keeps the
route handlers tiny, mirrors the existing ``cover_letter_service`` /
``jobs_service`` split, and gives the Stage 10 tests a small, mockable surface.
"""
from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlparse, urlunparse

import auth as auth_mod
import local_demo_store
import ssrf

try:
    # engine/ is on sys.path in every entrypoint (server boot, tests).
    from llm.client import (
        SUPPORTED_PROVIDERS,
        canonical_provider,
        resolve_model,
    )
except ImportError:  # pragma: no cover - unusual import roots
    from engine.llm.client import (
        SUPPORTED_PROVIDERS,
        canonical_provider,
        resolve_model,
    )

# Profile columns a user is allowed to edit from the account page. The DB
# column-UPDATE grant (migrations 0002 + 0006) is the real enforcement; this
# list keeps the app from even attempting to write a privileged column.
# Deliberately EXCLUDES managed_credits_remaining and deletion_requested_at.
_EDITABLE_PROFILE_FIELDS = (
    "display_name",
    "default_language",
    "preferred_provider",
    "preferred_model",
)

# Languages the editor ships (EN default + DE). Anything else is rejected so a
# garbage value can't wedge the UI's language toggle.
_ALLOWED_LANGUAGES = ("en", "de")


def _now_iso() -> str:
    """UTC timestamp for ``updated_at`` bumps (matches server._now_iso)."""
    return datetime.now(timezone.utc).isoformat()


def _sb():
    """The storage client for the caller: the in-memory demo store for the
    local demo owner, else the real RLS-scoped Supabase client.

    The demo session's ``alg:none`` token can never authorize against the real
    Supabase project, and with a configured-but-unreachable ``SUPABASE_URL``
    every PostgREST call raises ``httpx.ConnectError`` → HTML 500. Account reads
    (profile / GDPR export) must route through the same demo seam that
    jobs_service uses so the demo shell stays functional offline. See
    :func:`local_demo_store.active`.
    """
    if local_demo_store.active():
        return local_demo_store.db()
    return auth_mod.user_supabase()


# ---------------------------------------------------------------------------
# Profile read / update (RLS-scoped; credit column is non-user-writable).
# ---------------------------------------------------------------------------
def get_profile() -> dict[str, Any]:
    """The caller's own profile row (RLS-scoped), or an empty-ish default.

    Returns the user-relevant fields only. ``managed_credits_remaining`` is
    read-only context for the UI (the BYO-key panel shows it); it can never be
    written through :func:`update_profile`.
    """
    sb = _sb()
    resp = (
        sb.table("profiles")
        .select(
            "id, email, display_name, default_language, preferred_provider, "
            "preferred_model, managed_credits_remaining, deletion_requested_at"
        )
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return {
            "id": getattr(auth_mod.auth_g(), "owner_id", None),
            "email": getattr(auth_mod.auth_g(), "email", None),
            "display_name": None,
            "default_language": "en",
            "preferred_provider": "anthropic",
            "preferred_model": None,
            "managed_credits_remaining": 0,
            "deletion_requested_at": None,
        }
    row = dict(rows[0])
    # Default the provider preference for rows created before migration 0006.
    row.setdefault("preferred_provider", "anthropic")
    if row.get("preferred_provider") is None:
        row["preferred_provider"] = "anthropic"
    row.setdefault("preferred_model", None)
    return row


def update_profile(fields: dict[str, Any]) -> dict[str, Any]:
    """Update the caller's safe profile columns; return the fresh profile.

    Only ``display_name`` and ``default_language`` are ever written.
    ``default_language`` is validated against the supported set. A
    ``managed_credits_remaining`` (or any other) key in ``fields`` is silently
    ignored here — and even if it weren't, the DB column-UPDATE grant rejects
    it. The credit balance therefore cannot be tampered with via this path.
    """
    payload: dict[str, Any] = {}

    if "display_name" in fields:
        name = fields.get("display_name")
        if name is not None and not isinstance(name, str):
            raise ValueError("display_name must be a string")
        # Trim and cap; empty string clears the display name (stored as None).
        cleaned = (name or "").strip()[:200]
        payload["display_name"] = cleaned or None

    if "default_language" in fields:
        lang = (fields.get("default_language") or "").strip().lower()
        if lang not in _ALLOWED_LANGUAGES:
            raise ValueError(
                f"default_language must be one of {_ALLOWED_LANGUAGES}"
            )
        payload["default_language"] = lang

    # Provider/model preference (plan 002 stage 4). Validated against the
    # centralized allowlist so a garbage provider or a CROSS-PROVIDER model
    # (e.g. a Claude id under Gemini) is rejected rather than silently stored —
    # the cross-vendor guard starts at the preference layer.
    if "preferred_provider" in fields or "preferred_model" in fields:
        # The effective provider for validating the model: the one being set, or
        # the one already stored, or the default.
        if "preferred_provider" in fields:
            raw_provider = fields.get("preferred_provider")
            # Reject a non-string provider up front so a junk type (int/list/
            # dict) is a clean 400, never an AttributeError 500 inside
            # canonical_provider's .strip(). Fail closed at the boundary.
            if raw_provider is not None and not isinstance(raw_provider, str):
                raise ValueError(
                    f"preferred_provider must be one of {SUPPORTED_PROVIDERS}"
                )
            prov = canonical_provider(raw_provider)
            if prov not in SUPPORTED_PROVIDERS:
                raise ValueError(
                    f"preferred_provider must be one of {SUPPORTED_PROVIDERS}"
                )
            payload["preferred_provider"] = prov
        else:
            prov = canonical_provider(get_profile().get("preferred_provider"))

        if "preferred_model" in fields:
            requested = fields.get("preferred_model")
            if requested is None or (isinstance(requested, str) and not requested.strip()):
                # Clearing the model → fall back to the provider default at use.
                payload["preferred_model"] = None
            else:
                if not isinstance(requested, str):
                    raise ValueError("preferred_model must be a string")
                requested = requested.strip()
                # resolve_model returns the request only if it's in the
                # provider's allowlist; an unknown / cross-provider id maps to
                # the provider default, so a mismatch is detectable here.
                if resolve_model(prov, requested) != requested:
                    raise ValueError(
                        f"preferred_model {requested!r} is not valid for provider {prov!r}"
                    )
                payload["preferred_model"] = requested

    if not payload:
        raise ValueError("no editable profile fields supplied")

    payload["updated_at"] = _now_iso()

    # _sb() (not user_supabase) so a demo-owner profile WRITE routes through the
    # in-memory store instead of the unreachable Supabase (which would 500 —
    # hard rule 5). For the demo owner this is an ephemeral no-op update; a real
    # tenant still writes through the RLS-scoped client exactly as before.
    _sb().table("profiles").update(payload).eq("id", _owner_id()).execute()
    return get_profile()


def _owner_id() -> str:
    owner = getattr(auth_mod.auth_g(), "owner_id", None)
    if not owner:
        raise RuntimeError("no authenticated owner on request")
    return owner


_SAFE_SOURCE_METADATA_FIELDS = {
    "terms_basis",
    "manual_verification_required",
    "crawl_policy",
    "origin",
    "site",
    "date_posted",
    "first_seen",
    "normalized_role",
    "source_db",
    "priority_rank",
    "rank_bucket",
    "rank_badge",
    "fit_note",
    "risk_note",
    "source_artifact",
    "verified_at",
    "full_jd_status",
    "source_links",
}

_SAFE_RUN_METADATA_FIELDS = {
    "crawl_policy",
    "filters",
    "source_registry",
}

_SAFE_REGISTRY_FIELDS = {
    "id",
    "label",
    "terms_basis",
    "supports_preview",
    "supports_fetch",
    "requires_review",
    "filters",
}

_SENSITIVE_METADATA_TOKENS = (
    "auth",
    "authorization",
    "bearer",
    "cookie",
    "csrf",
    "key",
    "li_at",
    "password",
    "secret",
    "session",
    "token",
)
_NUMERIC_IPV4_HOST_RE = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*\Z", re.IGNORECASE)


def _looks_sensitive(value: Any) -> bool:
    text = str(value).lower()
    return any(token in text for token in _SENSITIVE_METADATA_TOKENS)


def _safe_metadata_value(value: Any) -> Any:
    """Return JSON-safe metadata values while dropping secret-shaped material."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return None if _looks_sensitive(value) else value[:1000]
    if isinstance(value, list):
        items = [_safe_metadata_value(item) for item in value[:50]]
        return [item for item in items if item is not None]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _looks_sensitive(key):
                continue
            if key.lower() in {"url", "href", "source_url", "open_url"}:
                cleaned = _safe_source_url(item)
            else:
                cleaned = _safe_metadata_value(item)
            if cleaned is not None:
                out[key] = cleaned
        return out
    return None


def _safe_source_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    host = parsed.hostname or ""
    if not host:
        return None
    lowered = host.lower().rstrip(".")
    if lowered in {"localhost", "metadata", "metadata.google.internal", "169.254.169.254"} or lowered.endswith(".localhost"):
        return None
    try:
        parsed_ip = ipaddress.ip_address(lowered)
    except ValueError:
        parsed_ip = None
        if _NUMERIC_IPV4_HOST_RE.fullmatch(lowered):
            try:
                parsed_ip = ipaddress.ip_address(socket.inet_aton(lowered))
            except OSError:
                parsed_ip = None
    if parsed_ip is not None and not ssrf._ip_is_public(parsed_ip):
        return None
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    path = "" if _looks_sensitive(parsed.path or "") else (parsed.path or "")
    return urlunparse((scheme, netloc, path, "", "", ""))[:500]


def _safe_source_scalar(value: Any) -> str | None:
    cleaned = _safe_metadata_value(value)
    if isinstance(cleaned, str):
        return cleaned[:1000]
    return None


def _safe_source_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _SAFE_SOURCE_METADATA_FIELDS:
        cleaned = _safe_metadata_value(value.get(key))
        if key == "source_db" and isinstance(cleaned, str):
            if "/" in cleaned or "\\" in cleaned or cleaned.startswith("."):
                continue
        if key == "source_artifact" and isinstance(cleaned, str):
            cleaned = cleaned.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if cleaned is not None:
            out[key] = cleaned
    return out


def _safe_run_filters(value: Any) -> Any:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _looks_sensitive(key):
            continue
        cleaned = _safe_metadata_value(item)
        if cleaned is not None:
            out[key] = cleaned
    return out


def _safe_source_registry(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        rows = list(value.values())
    else:
        rows = value if isinstance(value, list) else []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item: dict[str, Any] = {}
        for key in _SAFE_REGISTRY_FIELDS:
            cleaned = _safe_metadata_value(row.get(key))
            if cleaned is not None:
                item[key] = cleaned
        if item:
            out.append(item)
    return out


def _safe_run_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _SAFE_RUN_METADATA_FIELDS:
        if key not in value:
            continue
        if key == "filters":
            cleaned = _safe_run_filters(value.get(key))
        elif key == "source_registry":
            cleaned = _safe_source_registry(value.get(key))
        else:
            cleaned = _safe_metadata_value(value.get(key))
        if cleaned not in (None, {}, []):
            out[key] = cleaned
    return out


def _project_saved_job(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "company": row.get("company"),
        "jd_text": row.get("jd_text"),
        "jd_json": row.get("jd_json"),
        "source_url": _safe_source_url(row.get("source_url")),
        "status": row.get("status"),
        "field": row.get("field"),
        "deadline": row.get("deadline"),
        "confidence": row.get("confidence"),
        "why_fit": row.get("why_fit"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "import_run_id": row.get("import_run_id"),
        "source_provider": row.get("source_provider"),
        "source_type": row.get("source_type"),
        "source_external_id": _safe_source_scalar(row.get("source_external_id")),
        "source_fetched_at": row.get("source_fetched_at"),
        "source_metadata": _safe_source_metadata(row.get("source_metadata")),
        "source_country": _safe_source_scalar(row.get("source_country")),
        "source_region": _safe_source_scalar(row.get("source_region")),
        "source_location": _safe_source_scalar(row.get("source_location")),
        "remote_mode": _safe_source_scalar(row.get("remote_mode")),
        "salary_text": _safe_source_scalar(row.get("salary_text")),
        "seniority": _safe_source_scalar(row.get("seniority")),
        "verification_status": row.get("verification_status"),
        "content_hash": row.get("content_hash"),
        "experience_requirements": row.get("experience_requirements") if isinstance(row.get("experience_requirements"), dict) else {},
        "last_seen_at": row.get("last_seen_at"),
        "expires_at": row.get("expires_at"),
    }


def _project_import_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "provider": row.get("provider"),
        "query": row.get("query"),
        "location": row.get("location"),
        "status": row.get("status"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "requested_limit": row.get("requested_limit"),
        "imported_count": row.get("imported_count"),
        "skipped_count": row.get("skipped_count"),
        "error": row.get("error"),
        "metadata": _safe_run_metadata(row.get("metadata")),
    }


def _project_import_result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "import_run_id": row.get("import_run_id"),
        "source_provider": row.get("source_provider"),
        "source_type": row.get("source_type"),
        "source_external_id": _safe_source_scalar(row.get("source_external_id")),
        "title": row.get("title"),
        "company": row.get("company"),
        "jd_text": row.get("jd_text"),
        "jd_json": row.get("jd_json"),
        "source_url": _safe_source_url(row.get("source_url")),
        "source_metadata": _safe_source_metadata(row.get("source_metadata")),
        "source_country": _safe_source_scalar(row.get("source_country")),
        "source_region": _safe_source_scalar(row.get("source_region")),
        "source_location": _safe_source_scalar(row.get("source_location")),
        "remote_mode": _safe_source_scalar(row.get("remote_mode")),
        "salary_text": _safe_source_scalar(row.get("salary_text")),
        "seniority": _safe_source_scalar(row.get("seniority")),
        "verification_status": row.get("verification_status"),
        "content_hash": row.get("content_hash"),
        "experience_requirements": row.get("experience_requirements") if isinstance(row.get("experience_requirements"), dict) else {},
        "last_seen_at": row.get("last_seen_at"),
        "expires_at": row.get("expires_at"),
        "selected": row.get("selected"),
        "saved_job_id": row.get("saved_job_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


_PACKET_SENSITIVE_KEY_TOKENS = (
    "api_key",
    "authorization",
    "bearer",
    "ciphertext",
    "cookie",
    "csrf",
    "dek",
    "li_at",
    "nonce",
    "password",
    "raw_prompt",
    "session",
    "secret",
    "system_prompt",
    "token",
    "user_prompt",
)

_PACKET_SENSITIVE_VALUE_TOKENS = (
    "bearer ",
    "cookie",
    "developer prompt",
    "li_at=",
    "password",
    "raw prompt",
    "review token",
    "secret",
    "session",
    "sk-",
    "system prompt",
    "token",
    "user prompt",
)


def _packet_key_is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered == "prompt_version":
        return False
    return any(token in lowered for token in _PACKET_SENSITIVE_KEY_TOKENS)


def _packet_value_is_sensitive(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in _PACKET_SENSITIVE_VALUE_TOKENS)


def _safe_packet_value(value: Any) -> Any:
    """Project packet metadata without prompt bodies, review tokens, or secrets."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return None if _packet_value_is_sensitive(value) else value[:1000]
    if isinstance(value, list):
        items = [_safe_packet_value(item) for item in value[:50]]
        return [item for item in items if item is not None]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _packet_key_is_sensitive(key):
                continue
            cleaned = _safe_packet_value(item)
            if cleaned is not None:
                out[key] = cleaned
        return out
    return None


def _safe_packet_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return None if _packet_value_is_sensitive(value) else value[:300]
    return None


def _project_application_packet(row: dict[str, Any]) -> dict[str, Any]:
    score_snapshot = _safe_packet_value(row.get("score_snapshot"))
    if not isinstance(score_snapshot, dict):
        score_snapshot = {}
    return {
        "id": _safe_packet_scalar(row.get("id")),
        "job_id": _safe_packet_scalar(row.get("job_id")),
        "source_cv_id": _safe_packet_scalar(row.get("source_cv_id")),
        "tailored_cv_id": _safe_packet_scalar(row.get("tailored_cv_id")),
        "cover_letter_id": _safe_packet_scalar(row.get("cover_letter_id")),
        "status": _safe_packet_scalar(row.get("status")),
        "review_status": _safe_packet_scalar(row.get("review_status")),
        "provider": _safe_packet_scalar(row.get("provider")),
        "model": _safe_packet_scalar(row.get("model")),
        "prompt_version": _safe_packet_scalar(row.get("prompt_version")),
        "score_system_id": _safe_packet_scalar(row.get("score_system_id")),
        "score_snapshot": score_snapshot,
        "source_cv_hash": _safe_packet_scalar(row.get("source_cv_hash") or row.get("input_hash")),
        "job_content_hash": _safe_packet_scalar(row.get("job_content_hash")),
        "tailored_cv_hash": _safe_packet_scalar(row.get("tailored_cv_hash") or row.get("output_hash")),
        "cover_letter_hash": _safe_packet_scalar(row.get("cover_letter_hash")),
        "cover_letter_review_status": _safe_packet_scalar(row.get("cover_letter_review_status")),
        "cover_letter_review_confirmed_at": _safe_packet_scalar(row.get("cover_letter_review_confirmed_at")),
        "cover_letter_review_id": _safe_packet_scalar(row.get("cover_letter_review_id")),
        "packet_version": _safe_packet_scalar(row.get("packet_version")),
        "profile_answers_complete": row.get("profile_answers_complete") is True,
        "legal_answers_confirmed": row.get("legal_answers_confirmed") is True,
        "visa_answers_confirmed": row.get("visa_answers_confirmed") is True,
        "archived_at": _safe_packet_scalar(row.get("archived_at")),
        "created_at": _safe_packet_scalar(row.get("created_at")),
        "updated_at": _safe_packet_scalar(row.get("updated_at")),
    }


def _packet_references_live_documents(row: dict[str, Any], live_doc_ids: set[str]) -> bool:
    if str(row.get("status") or "").lower() == "archived" or row.get("archived_at"):
        return False
    for key in ("source_cv_id", "tailored_cv_id", "cover_letter_id"):
        value = row.get(key)
        if value and str(value) not in live_doc_ids:
            return False
    return True


# ---------------------------------------------------------------------------
# Structured account export — explicit live-data scope.
# ---------------------------------------------------------------------------
def export_account_data() -> dict[str, Any]:
    """Assemble the caller's structured live-data export (RLS-scoped).

    Includes: profile, live documents (CVs + cover letters with full
    ``yaml_blob``), saved jobs, import provenance, and application packet
    lineage. For BYO keys it includes ONLY a non-secret ``{provider, last4}``
    hint — the encrypted columns (ciphertext / nonce / wrapped_dek / dek_nonce)
    are never selected, so the export can never contain key secret material.

    Excluded by design: soft-deleted/archived records, usage and credit events,
    PDF artifact metadata and R2 bytes, application-attempt audit rows, tailor
    review state, uploaded photo files, and encrypted BYO-key material.
    """
    sb = _sb()

    profile = get_profile()
    deletion_pending = bool(profile.get("deletion_requested_at"))

    if deletion_pending:
        documents: list[dict[str, Any]] = []
        saved_jobs: list[dict[str, Any]] = []
        import_runs: list[dict[str, Any]] = []
        import_results: list[dict[str, Any]] = []
        application_packets: list[dict[str, Any]] = []
    else:
        if local_demo_store.active():
            # The seeded primary demo CV is materialized lazily by the route
            # layer (server._local_demo_cv_store); make sure it exists in the
            # demo documents table so the export includes it alongside any
            # created / tailored demo documents.
            local_demo_store.demo_cv_row()
        documents = (
            sb.table("documents")
            .select(
                "id, type, name, slug, yaml_blob, accent, font, density, "
                "job_id, created_at, updated_at"
            )
            .is_("deleted_at", "null")
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
        live_doc_ids = {str(row.get("id")) for row in documents if row.get("id")}

        saved_jobs = (
            sb.table("saved_jobs")
            .select(
                "id, title, company, jd_text, jd_json, source_url, status, "
                "field, deadline, confidence, why_fit, created_at, updated_at, "
                "import_run_id, source_provider, source_type, source_external_id, "
                "source_fetched_at, source_metadata, source_country, "
                "source_region, source_location, remote_mode, salary_text, "
                "seniority, verification_status, content_hash, experience_requirements, "
                "last_seen_at, expires_at"
            )
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        saved_jobs = [_project_saved_job(row) for row in saved_jobs]

        import_runs = (
            sb.table("job_import_runs")
            .select(
                "id, provider, query, location, status, started_at, finished_at, "
                "requested_limit, imported_count, skipped_count, error, metadata"
            )
            .order("started_at", desc=True)
            .execute()
            .data
            or []
        )
        import_runs = [_project_import_run(row) for row in import_runs]

        import_results = (
            sb.table("job_import_results")
            .select(
                "id, import_run_id, source_provider, source_type, "
                "source_external_id, title, company, jd_text, jd_json, "
                "source_url, source_metadata, source_country, source_region, "
                "source_location, remote_mode, salary_text, seniority, "
                "verification_status, content_hash, experience_requirements, last_seen_at, expires_at, "
                "selected, saved_job_id, created_at, updated_at"
            )
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        import_results = [_project_import_result(row) for row in import_results]

        application_packets = (
            sb.table("application_packets")
            .select(
                "id, job_id, source_cv_id, tailored_cv_id, cover_letter_id, "
                "status, review_status, provider, model, prompt_version, "
                "score_system_id, score_snapshot, source_cv_hash, job_content_hash, "
                "tailored_cv_hash, cover_letter_hash, input_hash, output_hash, "
                "cover_letter_review_status, cover_letter_review_confirmed_at, cover_letter_review_id, "
                "packet_version, profile_answers_complete, legal_answers_confirmed, "
                "visa_answers_confirmed, archived_at, created_at, updated_at"
            )
            .is_("archived_at", "null")
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
        application_packets = [
            _project_application_packet(row)
            for row in application_packets
            if str(row.get("status") or "").lower() != "archived"
            and _packet_references_live_documents(row, live_doc_ids)
        ]

    # BYO keys: NEVER the secret columns. The query already selects only safe
    # columns, but we ALSO project explicitly in Python — defence in depth, so
    # the export can never carry ciphertext/nonce/wrapped_dek/dek_nonce even if
    # the client/query were ever changed to return more. Only the hint survives.
    key_rows = (
        sb.table("user_llm_keys")
        .select("provider, last4, created_at, updated_at")
        .execute()
        .data
        or []
    )
    llm_keys = [
        {
            "provider": r.get("provider"),
            "last4": r.get("last4"),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }
        for r in key_rows
    ]

    return {
        "export_format": "onepager.account.v1",
        "exported_at": _now_iso(),
        "deletion_pending": deletion_pending,
        "profile": profile,
        "documents": documents,
        "saved_jobs": saved_jobs,
        "job_import_runs": import_runs,
        "job_import_results": import_results,
        "application_packets": application_packets,
        # Non-secret hint ONLY — proves a key exists without revealing it.
        "llm_keys": llm_keys,
    }


# ---------------------------------------------------------------------------
# Right to erasure — hide now, complete external + database purge after D+30.
# ---------------------------------------------------------------------------
def request_account_deletion() -> dict[str, Any]:
    """Trigger the immediate soft-delete + start the D+30 grace window.

    Calls the SECURITY DEFINER RPC which stamps ``deletion_requested_at`` and
    soft-deletes the caller's documents. Returns a confirmation including the
    request timestamp and the grace period. The production service-role worker
    claims due accounts, removes R2 objects and the photo-volume directory, and
    then hard-deletes the Auth user so database cascades run. External cleanup
    failures are retried and never permit the database delete to proceed first.
    """
    if local_demo_store.active():
        # The demo shell has no real Supabase account to erase; the RPC would
        # ConnectError → 500. Return the confirmation shape so the UI flow works
        # against the ephemeral in-memory store without pretending to delete a
        # real tenant.
        return {
            "deletion_requested": True,
            "requested_at": _now_iso(),
            "grace_days": 30,
            "message": (
                "Demo mode: no real account is stored, so nothing is erased. "
                "In a signed-in account this schedules deletion after 30 days."
            ),
        }
    sb = auth_mod.user_supabase()
    resp = sb.rpc("request_account_deletion", {}).execute()
    requested_at = resp.data
    return {
        "deletion_requested": True,
        "requested_at": requested_at,
        "grace_days": 30,
        "message": (
            "Your account is scheduled for deletion. Your documents are hidden "
            "immediately. You can cancel for 30 days; after that, the erasure "
            "worker removes active files and account records, retrying safely "
            "if an external storage deletion fails."
        ),
    }


def cancel_account_deletion() -> dict[str, Any]:
    """Undo a pending erasure strictly before D+30 (RPC-backed).

    Clears ``deletion_requested_at`` and restores the documents the request
    soft-deleted. Returns ``{cancelled: bool}`` — ``False`` if nothing was
    pending.
    """
    if local_demo_store.active():
        # The demo shell never persists a pending deletion (see
        # request_account_deletion — nothing is written to the in-memory
        # store), so the pending panel the UI shows after /api/account/delete
        # is purely client-side. Returning ``cancelled: True`` lets the
        # panel's Cancel button dismiss it; ``False`` would dead-end the demo
        # user on an uncancellable "pending" state.
        return {"cancelled": True}
    sb = auth_mod.user_supabase()
    resp = sb.rpc("cancel_account_deletion", {}).execute()
    return {"cancelled": resp.data is True}
