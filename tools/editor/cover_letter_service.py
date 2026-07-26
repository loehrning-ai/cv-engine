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
    "job_id",
    # Business-letter envelope (NEW — all optional; renderer falls back to
    # EN/DE defaults when absent). ``recipient`` above is the addressee line;
    # these complete the letter around the guarded body.
    "subject",  # str       — overrides the auto "Application for <role>" subject
    "salutation",  # str    — overrides the auto "Dear <recipient>," / "Sehr geehrte…"
    "closing",  # str       — overrides the auto "Best regards," / "Mit freundlichen Grüßen"
    "attachments",  # list[str] — enclosure labels ("CV") → "Enclosures"/"Anlagen"
)


def serialize_blob(data: dict[str, Any]) -> str:
    """Serialize a cover-letter payload to the YAML text stored in the row."""
    payload = {k: data.get(k) for k in _BLOB_FIELDS}
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=120)


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
    resp = (
        sb.table("documents")
        .select("id, name, slug, yaml_blob, job_id, created_at, updated_at, last_used_at")
        .eq("type", doc_type)
        .is_("deleted_at", "null")
        .order("last_used_at", desc=True)
        .execute()
    )
    return resp.data or []


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
    sb = _doc_sb()
    resp = (
        sb.table("saved_jobs")
        .select("id, title")
        .in_("id", ids)
        .execute()
    )
    out: dict[str, str] = {}
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
    doc_type: str, name: str, yaml_blob: str, *, job_id: str | None = None
) -> dict:
    """Insert a document of ``doc_type`` for the caller; return the new row.

    Runs under the user's JWT so the INSERT satisfies the RLS WITH CHECK
    (``owner_id = auth.uid()``); ``owner_id`` defaults to ``auth.uid()`` in the
    schema, so it is never taken from the request body. An optional ``job_id``
    is stamped on the ``documents.job_id`` column so a job's detail view can
    group its documents by FK (not just by the value buried in the blob).
    """
    payload: dict[str, Any] = {"type": doc_type, "name": name, "yaml_blob": yaml_blob}
    if job_id is not None:
        payload["job_id"] = job_id
    sb = _doc_sb()
    resp = sb.table("documents").insert(payload).execute()
    rows = resp.data or []
    if not rows:
        raise RuntimeError(f"failed to insert {doc_type} document")
    return rows[0]


def update_document_blob(doc_id: str, doc_type: str, yaml_blob: str, *, now_iso: str) -> bool:
    """Update a document's ``yaml_blob`` + ``updated_at``. True iff a row moved."""
    sb = _doc_sb()
    resp = (
        sb.table("documents")
        .update({"yaml_blob": yaml_blob, "updated_at": now_iso})
        .eq("id", doc_id)
        .eq("type", doc_type)
        .execute()
    )
    return bool(resp.data or [])


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


def delete_document(doc_id: str, doc_type: str) -> bool:
    """Hard-delete a document. True iff a row was removed (False → 404)."""
    sb = _doc_sb()
    resp = (
        sb.table("documents")
        .delete()
        .eq("id", doc_id)
        .eq("type", doc_type)
        .execute()
    )
    return bool(resp.data or [])


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
    # 1) CV (RLS) — the only source of candidate facts.
    cv_model, _cv_row = load_cv_for_grounding(cv_id)

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
    row = insert_document("cover_letter", name, blob, job_id=job_id)

    # 8) Audit the BYO path (managed path already logged inside the RPC).
    if not used_managed:
        llm_gateway.log_byo_usage(GENERATE_KIND, provider=prov)

    return {
        "id": row["id"],
        "name": row.get("name") or name,
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
