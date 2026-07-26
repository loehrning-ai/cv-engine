"""CV-to-job tailoring + persistence service (Stage 6).

This sits between the editor's tailoring route and the engine. It implements
the **duplicate-then-tailor** contract and the two deterministic guards:

  1. load the user's source CV (RLS-scoped) as the SOLE source of facts;
  2. parse the pasted/fetched JD into keyed JSON (``parse_jd``);
  3. run ONE structured LLM call (``resolve_llm('cv_tailor')`` → ``LLMClient
     .complete`` at the cheaper Haiku tier) that returns typed, machine-
     applyable suggestions — it never rewrites the whole CV;
  4. apply those suggestions DETERMINISTICALLY onto a COPY of the model
     (``engine.tailor.apply``);
  5. **Guard A — anti-fabrication.** Cross-reference every proper noun / number
     in the tailored CV against the SOURCE CV (the Stage-5
     ``cross_reference_facts``). Any ungrounded token → reject with
     ``422 unverified_claims`` and persist NOTHING;
  6. **Guard B — the 1-page gate.** Re-render the tailored YAML through the
     existing engine path (``render_cv_pdf``). If it overflows → ``409
     overflow`` with the offending heading, and persist NOTHING;
  7. only if BOTH guards pass, INSERT a NEW ``documents`` row (type ``cv``,
     name "… — tailored for {company}") so the ORIGINAL is preserved byte-for-
     byte. Roll the row back if a late failure occurs;
  8. compute the deterministic in-house ATS score against the SOURCE CV and
     return ``{tailored_cv_id, ats, gaps}``.

The document helpers reuse the type-parameterized CRUD in
``cover_letter_service`` (no duplicated SQL). Every LLM call is mockable in
tests; the guards are pure/offline.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any

import yaml
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import auth as auth_mod
import cover_letter_service as cl_service
import llm_gateway
import local_demo_store
import pdf_store

# engine/ is on sys.path in every entrypoint (server boot, tests).
try:
    from ats import profiles as ats_profiles
    from ats.experience import build_experience_fit
    from ats.rubric import RubricResult, score_cv_with_rubric
    from ats.score import score_cv_against_jd
    from cover_letter.guards import cross_reference_facts
    from cover_letter.jd import parse_jd
    from llm.client import (
        GEMINI_TAILOR_MODEL,
        canonical_provider,
        default_model_for,
        demo_mode_enabled,
        make_client,
        resolve_model,
    )
    from render.pdf_service import CvYamlError
    from tailor import (
        CV_TAILOR_MAX_TOKENS,
        CV_TAILOR_MODEL,
        apply_suggestions,
        build_tailor_messages,
    )
except ImportError:  # pragma: no cover - unusual import roots
    from engine.ats import profiles as ats_profiles
    from engine.ats.experience import build_experience_fit
    from engine.ats.rubric import RubricResult, score_cv_with_rubric
    from engine.ats.score import score_cv_against_jd
    from engine.cover_letter.guards import cross_reference_facts
    from engine.cover_letter.jd import parse_jd
    from engine.llm.client import (
        GEMINI_TAILOR_MODEL,
        canonical_provider,
        default_model_for,
        demo_mode_enabled,
        make_client,
        resolve_model,
    )
    from engine.render.pdf_service import CvYamlError
    from engine.tailor import (
        CV_TAILOR_MAX_TOKENS,
        CV_TAILOR_MODEL,
        apply_suggestions,
        build_tailor_messages,
    )

TAILOR_KIND = "cv_tailor"

# The ``kind`` recorded for the ATS rubric LLM call in the usage/audit trail.
ATS_KIND = "ats_score"

_log = logging.getLogger(__name__)

REVIEW_TOKEN_TTL_SECONDS = int(os.environ.get("ONEPAGER_TAILOR_REVIEW_TTL", "1800"))
LOCAL_REVIEW_TOKEN_SECRET = b"onepager-local-demo-tailor-review-token"
TAILOR_REVIEW_TABLE = "tailor_review_states"


def _server_sb():
    if local_demo_store.active():
        return local_demo_store.db()
    return auth_mod.server_supabase()


def _doc_sb():
    if local_demo_store.active():
        return local_demo_store.db()
    return auth_mod.user_supabase()


def ats_rubric_enabled() -> bool:
    """True iff the LLM ATS rubric is switched on (soft flag, default ON).

    ``ONEPAGER_ATS_RUBRIC`` is the operator kill-switch for the rubric: with it
    UNSET (or set to a truthy value) the rubric runs when a key is available and
    degrades to the deterministic scorer otherwise. Setting it to a falsey value
    (``0`` / ``false`` / ``no`` / ``off``) forces the deterministic path for
    EVERY request — a one-flag rollback if the rubric ever misbehaves in prod.
    Read live from the env on every call so it can be toggled without a restart
    and so tests can flip it per-case.
    """
    raw = os.environ.get("ONEPAGER_ATS_RUBRIC")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _tailor_model(provider: str, requested: str | None) -> str:
    """Pick the tailoring model for ``provider``.

    Tailoring runs on the cheaper "Haiku-equivalent" tier by default — Claude's
    ``CV_TAILOR_MODEL`` or Gemini's ``gemini-2.5-flash-lite``. An explicit
    ``requested`` model is honoured only if it's in the provider's allowlist
    (``resolve_model`` guards cross-provider / retired ids), otherwise we fall
    back to the cheap default for that provider.
    """
    p = canonical_provider(provider)
    if requested:
        resolved = resolve_model(p, requested)
        # resolve_model returns the provider's GENERIC default for an unknown
        # id; if the request was valid it returns the request itself.
        if resolved == requested:
            return resolved
    if p == "anthropic":
        return CV_TAILOR_MODEL
    if p == "gemini":
        return GEMINI_TAILOR_MODEL
    return default_model_for(p)


class TailorError(Exception):
    """Recoverable tailoring failure → maps to a 4xx/5xx JSON.

    ``status`` is the HTTP status; ``payload`` is the JSON body the route
    returns — so a grounding block (422), an overflow (409), a 404, or an LLM
    failure each map cleanly.
    """

    def __init__(self, status: int, payload: dict) -> None:
        super().__init__(payload.get("error", "tailoring error"))
        self.status = status
        self.payload = payload


# ---------------------------------------------------------------------------
# LLM response parsing — tolerant of fences / leading prose despite the prompt.
# ---------------------------------------------------------------------------
def _parse_tailor_json(raw: str) -> dict[str, Any]:
    """Parse the model's structured tailoring JSON (first ``{`` … last ``}``).

    The prompt asks for a bare JSON object, but we defensively strip any code
    fence / leading prose and slice to the outermost braces before
    ``json.loads`` — a truncated or malformed object raises so the route can
    surface a clean retry rather than corrupting the CV.
    """
    if not raw or not raw.strip():
        raise TailorError(
            502, {"error": "AI returned an empty tailoring result — try again.", "code": "llm_empty"}
        )
    text = raw.strip()
    # Strip a ```json … ``` fence if the model added one despite instructions.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise TailorError(
            502, {"error": "AI returned an unparseable tailoring result.", "code": "llm_malformed"}
        )
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError) as exc:
        raise TailorError(
            502, {"error": "AI returned invalid JSON for tailoring.", "code": "llm_malformed"}
        ) from exc
    if not isinstance(parsed, dict):
        raise TailorError(
            502, {"error": "AI tailoring result was not an object.", "code": "llm_malformed"}
        )
    return parsed


def _tailored_name(source_name: str, jd_json: dict[str, Any]) -> str:
    """A human name for the tailored copy: "<source> — tailored for <company>"."""
    base = (source_name or "CV").strip() or "CV"
    company = (jd_json.get("company") or "").strip()
    role = (jd_json.get("title") or "").strip()
    target = company or role
    if target:
        name = f"{base} — tailored for {target}"
    else:
        name = f"{base} — tailored"
    return name[:200]


def _owner_id() -> str:
    owner = getattr(auth_mod.auth_g(), "owner_id", None)
    return str(owner or "")


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _review_token_secret() -> bytes:
    for name in ("ONEPAGER_REVIEW_TOKEN_SECRET", "FLASK_SECRET_KEY", "SECRET_KEY"):
        secret = os.environ.get(name)
        if secret and secret.strip():
            return secret.encode("utf-8")
    if demo_mode_enabled():
        return LOCAL_REVIEW_TOKEN_SECRET
    raise TailorError(
        503,
        {
            "error": "Tailor review signing secret is not configured.",
            "code": "review_secret_missing",
        },
    )


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def _review_state_body(state: dict[str, Any]) -> bytes:
    return json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _review_state_digest(state: dict[str, Any]) -> str:
    return _b64e(hmac.new(_review_token_secret(), _review_state_body(state), hashlib.sha256).digest())


def _review_state_key() -> bytes:
    return hashlib.sha256(_review_token_secret() + b":tailor-review-state:v1").digest()


def _review_state_aad(review_id: str, digest: str) -> bytes:
    return f"tailor-review-state:v1:{review_id}:{digest}".encode("utf-8")


def _encrypt_review_state(state: dict[str, Any], *, digest: str) -> str:
    review_id = str(state.get("review_id") or "")
    if not review_id:
        raise TailorError(500, {"error": "review_id missing", "code": "review_state_invalid"})
    nonce = os.urandom(12)
    ciphertext = AESGCM(_review_state_key()).encrypt(
        nonce,
        _review_state_body(state),
        _review_state_aad(review_id, digest),
    )
    return f"v1:{_b64e(nonce)}:{_b64e(ciphertext)}"


def _decrypt_review_state(ciphertext: object, *, review_id: str, digest: str) -> dict[str, Any]:
    if not isinstance(ciphertext, str) or not ciphertext.startswith("v1:"):
        raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"})
    try:
        _version, nonce64, cipher64 = ciphertext.split(":", 2)
        raw = AESGCM(_review_state_key()).decrypt(
            _b64d(nonce64),
            _b64d(cipher64),
            _review_state_aad(review_id, digest),
        )
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - all decryption/parsing failures are invalid tokens
        raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"}) from exc
    if not isinstance(parsed, dict):
        raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"})
    return json.loads(json.dumps(parsed))


def _expiry_iso(expires_at: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()


def _store_review_state(state: dict[str, Any], *, digest: str) -> None:
    review_id = str(state.get("review_id") or "")
    expires_at = int(state.get("expires_at") or 0)
    row = {
        "id": review_id,
        "owner_id": _owner_id(),
        "cv_id": state.get("cv_id"),
        "job_id": state.get("job_id"),
        "source_hash": state.get("source_hash"),
        "jd_hash": state.get("jd_hash"),
        "tailored_hash": state.get("tailored_hash"),
        "state_digest": digest,
        "state_ciphertext": _encrypt_review_state(state, digest=digest),
        "expires_at": _expiry_iso(expires_at),
    }
    _server_sb().table(TAILOR_REVIEW_TABLE).insert(row).execute()


def _load_review_state(review_id: str, *, digest: str) -> dict[str, Any]:
    rows = (
        _server_sb()
        .table(TAILOR_REVIEW_TABLE)
        .select("id, owner_id, state_digest, state_ciphertext, expires_at, applied_at")
        .eq("id", review_id)
        .eq("owner_id", _owner_id())
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        raise TailorError(409, {"error": "review_expired", "code": "review_expired"})
    row = rows[0]
    if str(row.get("owner_id") or "") not in {"", _owner_id()}:
        raise TailorError(404, {"error": "review not found", "code": "review_not_found"})
    if row.get("state_digest") != digest:
        raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"})
    state = _decrypt_review_state(row.get("state_ciphertext"), review_id=review_id, digest=digest)
    if int(state.get("expires_at") or 0) < int(time.time()):
        raise TailorError(409, {"error": "review_expired", "code": "review_expired"})
    return state


def _claim_review_apply(review_id: str, *, digest: str, now_iso: str) -> None:
    updated = (
        _server_sb()
        .table(TAILOR_REVIEW_TABLE)
        .update({"applied_at": now_iso, "updated_at": now_iso})
        .eq("id", review_id)
        .eq("owner_id", _owner_id())
        .eq("state_digest", digest)
        .is_("applied_at", "null")
        .execute()
        .data
        or []
    )
    if updated:
        return
    rows = (
        _server_sb()
        .table(TAILOR_REVIEW_TABLE)
        .select("id, applied_at")
        .eq("id", review_id)
        .eq("owner_id", _owner_id())
        .limit(1)
        .execute()
        .data
        or []
    )
    if rows and rows[0].get("applied_at"):
        raise TailorError(
            409,
            {
                "error": "duplicate_review_apply",
                "code": "duplicate_review_apply",
                "hint": "This reviewed copy was already applied. Start a new review for another copy.",
            },
        )
    raise TailorError(409, {"error": "review_expired", "code": "review_expired"})


def _release_review_apply(review_id: str) -> None:
    _server_sb().table(TAILOR_REVIEW_TABLE).update({"applied_at": None}).eq("id", review_id).eq("owner_id", _owner_id()).execute()


def release_review_apply(review_id: str) -> None:
    """Release a review claim when a downstream packet step rolls back."""
    if review_id:
        _release_review_apply(review_id)


def _sign_review_state(state: dict[str, Any]) -> str:
    review_id = str(state.get("review_id") or "")
    if not review_id:
        raise TailorError(500, {"error": "review_id missing", "code": "review_state_invalid"})
    expires_at = int(state.get("expires_at") or 0)
    digest = _review_state_digest(state)
    _store_review_state(state, digest=digest)
    claims = {
        "v": 1,
        "review_id": review_id,
        "owner_id": state.get("owner_id"),
        "cv_id": state.get("cv_id"),
        "job_id": state.get("job_id"),
        "source_hash": state.get("source_hash"),
        "jd_hash": state.get("jd_hash"),
        "tailored_hash": state.get("tailored_hash"),
        "state_digest": digest,
        "expires_at": expires_at,
    }
    body = json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig = hmac.new(_review_token_secret(), body, hashlib.sha256).digest()
    return f"{_b64e(body)}.{_b64e(sig)}"


def _verify_review_token(token: object) -> dict[str, Any]:
    if not isinstance(token, str) or "." not in token:
        raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"})
    try:
        body64, sig64 = token.split(".", 1)
        body = _b64d(body64)
        supplied = _b64d(sig64)
    except Exception as exc:  # noqa: BLE001
        raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"}) from exc
    expected = hmac.new(_review_token_secret(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied, expected):
        raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"})
    try:
        state = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"}) from exc
    if state.get("v") != 1:
        raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"})
    if int(state.get("expires_at") or 0) < int(time.time()):
        raise TailorError(409, {"error": "review_expired", "code": "review_expired"})
    if state.get("owner_id") != _owner_id():
        raise TailorError(404, {"error": "review not found", "code": "review_not_found"})
    review_id = str(state.get("review_id") or "")
    stored = _load_review_state(review_id, digest=str(state.get("state_digest") or ""))
    stored["_review_state_digest"] = state.get("state_digest")
    return stored


def _diff_payload(source_yaml: str, tailored_yaml: str) -> dict[str, Any]:
    lines = list(
        difflib.unified_diff(
            (source_yaml or "").splitlines(),
            (tailored_yaml or "").splitlines(),
            fromfile="source-cv.yaml",
            tofile="reviewed-copy.yaml",
            lineterm="",
            n=2,
        )
    )
    body_lines = [line for line in lines if not line.startswith("---") and not line.startswith("+++")]
    added = sum(1 for line in body_lines if line.startswith("+"))
    removed = sum(1 for line in body_lines if line.startswith("-"))
    limit = 240
    return {
        "changed": bool(lines),
        "added": added,
        "removed": removed,
        "truncated": len(lines) > limit,
        "lines": lines[:limit],
    }


# ---------------------------------------------------------------------------
# The tailoring entry point.
# ---------------------------------------------------------------------------
def prepare_tailor_review(
    *,
    cv_id: str,
    jd_text: str,
    model: str | None = None,
    provider: str = "anthropic",
    job_id: str | None = None,
    profile: str = "semantic_fit",
    compare_profiles: list[str] | None = None,
    packet_mode: str | None = None,
    replaces_packet_id: str | None = None,
    now_iso: str,
) -> dict[str, Any]:
    """Prepare a signed review state for tailoring without saving a new CV.

    ``job_id`` (optional): when the tailoring is run against a saved job, the
    new ``cv`` document is stamped with this id (``documents.job_id``) so the
    job's detail view can group its documents. It is only stamped onto the
    newly-created copy AFTER both guards pass — a guard trip persists nothing.

    Steps mirror the module docstring. On success returns a dict the route
    JSON-ifies::

        {
          "tailored_cv_id": <new cv document id>,
          "name": <tailored doc name>,
          "ats": { score, tier1_coverage, matched, gaps, ... },
          "gaps": [ {term, importance}, ... ],   # convenience mirror of ats.gaps
          "applied": [<suggestion ids applied>],
          "warnings": [<model warnings>],
          "used_managed_credit": <bool>,
        }

    Raises :class:`TailorError` for a 404 (CV not found), the anti-fabrication
    block (422 ``unverified_claims``), the 1-page overflow (409 ``overflow``),
    or an LLM failure. Lets :class:`llm_gateway.LLMUnavailable` propagate so the
    route's existing handler maps the 402/503 credit/key responses.

    The ORIGINAL CV row is NEVER modified. This function runs the LLM and both
    guards, then returns a signed token that must be explicitly applied before a
    new document exists.
    """
    profile_id = ats_profiles.normalize_profile(profile)
    compare_ids = ats_profiles.normalize_profile_list(compare_profiles)
    # 0) (SECURITY MUST-FIX) If the tailored copy will be stamped with a job_id,
    #    verify the caller OWNS that job BEFORE doing any work or writing the row.
    #    Defense-in-depth with the DB ``with check`` (migration 0007): a request
    #    can never stamp another tenant's job onto a document. Lazy import avoids
    #    the jobs_service ⇄ tailor_service circular import.
    if job_id is not None:
        import jobs_service as _jobs_svc

        _jobs_svc.assert_job_owned(job_id)  # JobError 404 if invisible/not owned

    # 1) Source CV (RLS) — the only source of facts. Its row is NEVER modified;
    #    only a new row is created (duplicate-then-tailor).
    source_model, source_row = _load_source_cv(cv_id)

    # 2) Parse the JD into keyed JSON.
    try:
        jd_json = parse_jd(jd_text)
    except ValueError as exc:
        raise TailorError(400, {"error": str(exc), "code": "empty_jd"}) from exc

    # 3) Structured LLM tailoring call (cheaper tier by default) for the
    #    SELECTED provider. ``prov`` is the canonical provider the key is
    #    decrypted under; the client MUST be built for the same provider.
    system, user = build_tailor_messages(cv=source_model, jd_json=jd_json)
    prov = canonical_provider(provider)
    api_key, used_managed = llm_gateway.resolve_llm(TAILOR_KIND, provider=prov)
    client = make_client(prov, api_key)
    client_provider = getattr(client, "provider", prov)
    if client_provider not in (prov, "stub"):
        raise TailorError(
            502,
            {"error": "AI provider routing failed.", "code": "provider_mismatch"},
        )
    selected_model = _tailor_model(prov, model)
    try:
        raw = client.complete(
            system=system,
            user=user,
            model=selected_model,
            max_tokens=CV_TAILOR_MAX_TOKENS,
            cache_system=True,
        )
    except Exception as exc:  # noqa: BLE001 — provider/timeout/refusal
        raise TailorError(
            502, {"error": f"AI tailoring failed: {type(exc).__name__}", "code": "llm_failed"}
        ) from exc

    result = _parse_tailor_json(raw)
    suggestions = result.get("suggestions") if isinstance(result.get("suggestions"), list) else []
    summary_block = result.get("summary") if isinstance(result.get("summary"), dict) else None
    warnings = [w for w in (result.get("warnings") or []) if isinstance(w, str)]

    # 4) Apply suggestions deterministically onto a COPY of the source model.
    tailored_model, applied_ids, _skipped = apply_suggestions(
        source_model, suggestions, summary=summary_block
    )

    # 5) GUARD A — anti-fabrication. Every proper noun / number in the tailored
    #    CV must be grounded in the SOURCE CV (Stage-5 cross-reference). No JD
    #    context here: a tailored CV must trace to what the candidate ACTUALLY
    #    wrote, not to the job posting — that is the whole no-fabrication
    #    contract. Any ungrounded token blocks the result.
    tailored_yaml = yaml.safe_dump(tailored_model, sort_keys=False, allow_unicode=True, width=1000)
    offenders = cross_reference_facts(_cv_text_blob(tailored_model), source_model)
    if offenders:
        raise TailorError(422, {"error": "unverified_claims", "items": offenders})

    # 6) GUARD B — the 1-page gate. Re-render the tailored YAML through the
    #    EXISTING engine path; an overflow blocks the result (never ship a
    #    2-page CV).
    try:
        rendered = pdf_store.render_cv(tailored_yaml)
    except CvYamlError as exc:
        raise TailorError(400, {"error": f"Tailored CV YAML invalid: {exc}", "code": "cv_invalid"}) from exc
    except RuntimeError as exc:  # WeasyPrint missing — can't verify, fail safe.
        raise TailorError(503, {"error": "PDF renderer unavailable; tailoring not saved.", "code": "render_unavailable"}) from exc
    if rendered.overflowed:
        raise TailorError(
            409,
            {
                "error": "overflow",
                "heading": rendered.overflow_label,
                "pages": rendered.page_count,
                "hint": (
                    f"The tailored CV overflowed to {rendered.page_count} pages "
                    f"(first section on page 2: {rendered.overflow_label!r}). "
                    "Trim a bullet or shorten a rewrite, then tailor again."
                ),
            },
        )

    # 7) BOTH guards passed -> create a signed, explicit review state. No
    #    document is inserted until apply_tailor_review validates this token
    #    against the current source row.
    tailored_name = _tailored_name(source_row.get("name") or "CV", jd_json)
    source_yaml = source_row.get("yaml_blob") or ""
    source_hash = _sha256_text(source_yaml)
    source_revision = str(source_row.get("updated_at") or "")
    review_id = str(uuid.uuid4())
    expires_at = int(time.time()) + REVIEW_TOKEN_TTL_SECONDS

    # 8) Deterministic ATS score against the reviewed tailored artifact. The
    #    source row remains unchanged; this evidence explains the post-tailor
    #    fit without overloading the score itself.
    try:
        det_result = score_cv_against_jd(tailored_model, jd_text, jd_json=jd_json)
        det_payload = _deterministic_ats_payload(
            det_result,
            cv_model=tailored_model,
            jd_text=jd_text,
            jd_json=jd_json,
        )
        ats = ats_profiles.attach_profile_metadata(
            ats_profiles.select_payload(profile_id, deterministic=det_payload, rubric=None),
            profile_id=profile_id,
            job_id=job_id,
            computed_at=now_iso,
        )
        if compare_ids:
            ats["profile_results"] = {
                pid: ats_profiles.attach_profile_metadata(
                    ats_profiles.select_payload(pid, deterministic=det_payload, rubric=None),
                    profile_id=pid,
                    job_id=job_id,
                    computed_at=now_iso,
                )
                for pid in compare_ids
            }
    except Exception:  # noqa: BLE001 — scoring is advisory, never blocks the save
        ats = {"score": None, "matched": [], "gaps": [], "notes": "scoring unavailable"}

    # Audit the BYO path (managed path is logged inside the RPC).
    if not used_managed:
        llm_gateway.log_byo_usage(TAILOR_KIND, provider=prov)

    state = {
        "v": 1,
        "review_id": review_id,
        "owner_id": _owner_id(),
        "cv_id": cv_id,
        "job_id": job_id,
        "source_hash": source_hash,
        "source_revision": source_revision,
        "jd_hash": _sha256_text(jd_text),
        "tailored_hash": _sha256_text(tailored_yaml),
        "tailored_name": tailored_name,
        "tailored_yaml": tailored_yaml,
        "applied": applied_ids,
        "warnings": warnings,
        "ats": ats,
        "provider": prov,
        "model": selected_model,
        "used_managed_credit": used_managed,
        "expires_at": expires_at,
    }
    if packet_mode:
        state["packet_mode"] = str(packet_mode)
    if replaces_packet_id:
        state["replaces_packet_id"] = str(replaces_packet_id)

    response = {
        "review_token": _sign_review_state(state),
        "review_id": review_id,
        "expires_at": expires_at,
        "source_cv_id": cv_id,
        "job_id": job_id,
        "source_hash": source_hash,
        "source_revision": source_revision,
        "jd_hash": state["jd_hash"],
        "tailored_hash": state["tailored_hash"],
        "provider": prov,
        "model": selected_model,
        "created_at": now_iso,
        "name": tailored_name,
        # Diff SEMANTICS, not serialization: the stored blob is the operator's
        # hand-written YAML (comments, custom list indentation, line wrapping),
        # while tailored_yaml is a safe_dump — diffing raw-vs-dumped reports the
        # whole document as changed even with ZERO applied suggestions. Re-dump
        # the parsed source model through the SAME serializer/options so an
        # empty change set yields an empty diff. (source_hash above deliberately
        # stays on the RAW blob: apply_tailor_review re-hashes the stored row.)
        "diff": _diff_payload(
            yaml.safe_dump(source_model, sort_keys=False, allow_unicode=True, width=1000),
            tailored_yaml,
        ),
        "ats": ats,
        "gaps": ats.get("gaps", []),
        "applied": applied_ids,
        "warnings": warnings,
        "used_managed_credit": used_managed,
    }
    if packet_mode:
        response["packet_mode"] = str(packet_mode)
    if replaces_packet_id:
        response["replaces_packet_id"] = str(replaces_packet_id)
    return response


def apply_tailor_review(
    *,
    cv_id: str,
    review_token: object,
    now_iso: str,
    expected_jd_text: str | None = None,
    expected_job_id: str | None = None,
) -> dict[str, Any]:
    """Apply a previously prepared tailoring review into one new CV document."""
    state = _verify_review_token(review_token)
    if state.get("cv_id") != cv_id:
        raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"})

    review_id = str(state.get("review_id") or "")
    if not review_id:
        raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"})
    review_digest = str(state.get("_review_state_digest") or "")
    review_claimed = False

    try:
        job_id = state.get("job_id")
        if expected_job_id is not None and str(job_id or "") != str(expected_job_id):
            raise TailorError(
                409,
                {
                    "error": "review_job_mismatch",
                    "code": "review_job_mismatch",
                    "hint": "This review token was prepared for a different job.",
                },
            )
        if job_id is not None:
            import jobs_service as _jobs_svc

            _jobs_svc.assert_job_owned(job_id)
            if expected_jd_text is None:
                job = _jobs_svc.get_job(job_id)
                expected_jd_text = (job or {}).get("jd_text") or ""

        if expected_jd_text is not None and _sha256_text(expected_jd_text) != state.get("jd_hash"):
            raise TailorError(
                409,
                {
                    "error": "job_description_changed",
                    "code": "job_description_changed",
                    "hint": "The job description changed after this review was prepared. Review the current job description before applying.",
                },
            )

        source_model, source_row = _load_source_cv(cv_id)
        source_yaml = source_row.get("yaml_blob") or ""
        current_hash = _sha256_text(source_yaml)
        current_revision = str(source_row.get("updated_at") or "")
        if current_hash != state.get("source_hash") or current_revision != str(state.get("source_revision") or ""):
            raise TailorError(
                409,
                {
                    "error": "source_changed",
                    "code": "source_changed",
                    "hint": "The source CV changed after this review was prepared. Review the job against the current CV before applying.",
                },
            )

        _claim_review_apply(review_id, digest=review_digest, now_iso=now_iso)
        review_claimed = True
        tailored_yaml = state.get("tailored_yaml")
        if not isinstance(tailored_yaml, str) or _sha256_text(tailored_yaml) != state.get("tailored_hash"):
            raise TailorError(400, {"error": "invalid_review_token", "code": "invalid_review_token"})
        tailored_model = _parse_cv_yaml(tailored_yaml)
        offenders = cross_reference_facts(_cv_text_blob(tailored_model), source_model)
        if offenders:
            raise TailorError(422, {"error": "unverified_claims", "items": offenders})
        try:
            rendered = pdf_store.render_cv(tailored_yaml)
        except CvYamlError as exc:
            raise TailorError(400, {"error": f"Tailored CV YAML invalid: {exc}", "code": "cv_invalid"}) from exc
        except RuntimeError as exc:
            raise TailorError(503, {"error": "PDF renderer unavailable; tailoring not saved.", "code": "render_unavailable"}) from exc
        if rendered.overflowed:
            raise TailorError(
                409,
                {
                    "error": "overflow",
                    "heading": rendered.overflow_label,
                    "pages": rendered.page_count,
                    "hint": (
                        f"The tailored CV overflowed to {rendered.page_count} pages "
                        f"(first section on page 2: {rendered.overflow_label!r}). "
                        "Trim a bullet or shorten a rewrite, then tailor again."
                    ),
                },
            )

        new_row = _insert_tailored_cv(
            str(state.get("tailored_name") or "Reviewed CV"),
            tailored_yaml,
            source_row,
            job_id=job_id,
        )
    except Exception:
        if review_claimed:
            _release_review_apply(review_id)
        raise
    ats = state.get("ats") if isinstance(state.get("ats"), dict) else {}
    return {
        "tailored_cv_id": new_row["id"],
        "name": new_row.get("name") or state.get("tailored_name") or "Reviewed CV",
        "source_cv_id": cv_id,
        "job_id": state.get("job_id"),
        "source_hash": state.get("source_hash"),
        "source_revision": state.get("source_revision"),
        "jd_hash": state.get("jd_hash"),
        "tailored_hash": state.get("tailored_hash"),
        "review_id": review_id,
        "provider": state.get("provider"),
        "model": state.get("model"),
        "ats": ats,
        "gaps": ats.get("gaps", []) if isinstance(ats, dict) else [],
        "applied": state.get("applied") if isinstance(state.get("applied"), list) else [],
        "warnings": state.get("warnings") if isinstance(state.get("warnings"), list) else [],
        "used_managed_credit": bool(state.get("used_managed_credit")),
        "packet_mode": state.get("packet_mode"),
        "replaces_packet_id": state.get("replaces_packet_id"),
    }


def tailor_cv(
    *,
    cv_id: str,
    jd_text: str,
    model: str | None = None,
    provider: str = "anthropic",
    job_id: str | None = None,
    profile: str = "semantic_fit",
    compare_profiles: list[str] | None = None,
    now_iso: str,
) -> dict[str, Any]:
    """Legacy one-shot path: prepare a review and immediately apply it."""
    review = prepare_tailor_review(
        cv_id=cv_id,
        jd_text=jd_text,
        model=model,
        provider=provider,
        job_id=job_id,
        profile=profile,
        compare_profiles=compare_profiles,
        now_iso=now_iso,
    )
    return apply_tailor_review(
        cv_id=cv_id,
        review_token=review["review_token"],
        now_iso=now_iso,
        expected_jd_text=jd_text,
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _load_source_cv(cv_id: str) -> tuple[dict, dict]:
    """Load + parse the caller's source CV. Raises ``TailorError`` 404 if hidden.

    Reuses the RLS-scoped ``document_by_id`` so another tenant's CV is simply
    invisible (→ 404). Returns ``(model, row)``; the row carries the exact
    ``yaml_blob`` we preserve and the theme columns to copy.
    """
    row = cl_service.document_by_id(cv_id, "cv")
    if row is None:
        raise TailorError(404, {"error": "CV not found", "code": "cv_not_found"})
    model = _parse_cv_yaml(row.get("yaml_blob") or "")
    return model, row


def _parse_cv_yaml(yaml_text: str) -> dict:
    """Parse CV YAML to a dict (raises ``TailorError`` 400 if malformed)."""
    try:
        parsed = yaml.safe_load(yaml_text)
    except Exception as exc:  # noqa: BLE001
        raise TailorError(400, {"error": f"CV YAML invalid: {exc}", "code": "cv_invalid"}) from exc
    if not isinstance(parsed, dict):
        raise TailorError(400, {"error": "CV is empty or malformed", "code": "cv_invalid"})
    return parsed


def _cv_text_blob(cv: dict) -> str:
    """Flatten every string/number value in the CV into one text blob.

    This is what the anti-fabrication cross-reference scans: the prose the
    tailoring produced (rewritten bullets, reordered stacks, an emphasized
    skills row). Keys are ignored — only values are checked, matching the
    Stage-5 corpus semantics so "grounded here" == "grounded there".
    """
    parts: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            parts.append(str(node))
        elif isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(cv or {})
    return "\n".join(parts)


def _insert_tailored_cv(
    name: str, yaml_blob: str, source_row: dict, *, job_id: str | None = None
) -> dict:
    """INSERT a new ``cv`` document for the caller (duplicate-then-tailor).

    Runs under the user's JWT so the INSERT satisfies the RLS WITH CHECK
    (``owner_id = auth.uid()``). The source row is never modified. Theme
    columns are carried over so the tailored copy renders identically; an
    optional ``job_id`` links the copy to a saved job. Raises ``TailorError``
    500 if the insert returns nothing (so we never report a success without a
    row).
    """
    insert: dict[str, Any] = {"type": "cv", "name": name, "yaml_blob": yaml_blob}
    for col in ("accent", "font", "density"):
        if source_row.get(col) is not None:
            insert[col] = source_row[col]
    if job_id is not None:
        insert["job_id"] = job_id
    sb = _doc_sb()
    resp = sb.table("documents").insert(insert).execute()
    rows = resp.data or []
    if not rows:
        raise TailorError(
            500, {"error": "Failed to save the tailored CV.", "code": "insert_failed"}
        )
    return rows[0]


# ---------------------------------------------------------------------------
# Standalone ATS scoring (no document created) — for POST /api/ats-score.
#
# Two layers, deterministic-first:
#   * the in-house keyword scorer (``score_cv_against_jd``) — pure, offline, and
#     the SOLE source of truth when no LLM key is available, the rubric flag is
#     off, or the rubric call fails for ANY reason. It always returns a score;
#   * the LLM rubric (Stage 4 ``score_cv_with_rubric``) — richer, JD-relative
#     category scoring with grounded evidence/strengths/gaps/suggestions, run
#     ONLY when the flag is on AND a key resolves. The deterministic result is
#     fed in as a signal and stays the fallback.
#
# CONTRACT: this function ALWAYS returns a score and NEVER raises
# ``LLMUnavailable`` (no key → deterministic, never a 402). The only failures it
# raises are the deterministic ones the route already maps: 404 (CV not found)
# and 400 (empty JD).
# ---------------------------------------------------------------------------
def _deterministic_ats_payload(
    det_result,
    *,
    note: str = "",
    cv_model: dict[str, Any] | None = None,
    jd_text: str = "",
    jd_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape a deterministic :class:`AtsResult` into the route JSON.

    Stamps ``source="deterministic"`` (so the UI knows which scorer ran) onto
    the existing ``AtsResult.to_json()`` shape — additive, so older callers that
    only read ``score``/``gaps`` are unaffected. An optional ``note`` records
    WHY the deterministic path was taken (flag off / no key / rubric failure).
    """
    payload = det_result.to_json()
    payload["source"] = "deterministic"
    if cv_model is not None:
        try:
            payload["experience_fit"] = build_experience_fit(cv_model, jd_text, jd_json=jd_json)
        except Exception:  # noqa: BLE001 - years evidence is additive only
            payload["experience_fit"] = {
                "required_years": None,
                "candidate_years": None,
                "missing_years": None,
                "fit": "unknown",
                "uncertain": True,
                "evidence": {"requirements": [], "candidate": [], "notes": ["experience fit unavailable"]},
            }
    if note:
        payload["note"] = note
    return payload


def _rubric_ats_payload(
    rubric: RubricResult,
    det_result,
    *,
    cv_model: dict[str, Any] | None = None,
    jd_text: str = "",
    jd_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape a successful :class:`RubricResult` into the route JSON.

    Carries the rubric's full breakdown (score / categories / strengths / gaps /
    suggestions) PLUS the deterministic keyword signal (``tier1_coverage`` +
    target) so the UI can still show must-have coverage alongside the LLM view.
    """
    payload = rubric.to_json()
    payload["source"] = "rubric"
    payload["tier1_coverage"] = det_result.tier1_coverage
    payload["tier1_target"] = det_result.tier1_target
    if cv_model is not None:
        try:
            payload["experience_fit"] = build_experience_fit(cv_model, jd_text, jd_json=jd_json)
        except Exception:  # noqa: BLE001 - years evidence is additive only
            payload["experience_fit"] = {
                "required_years": None,
                "candidate_years": None,
                "missing_years": None,
                "fit": "unknown",
                "uncertain": True,
                "evidence": {"requirements": [], "candidate": [], "notes": ["experience fit unavailable"]},
            }
    return payload


def _log_ats_call(
    *, source: str, provider: str | None, model: str | None, latency_ms: int,
    score: object, reason: str = "",
) -> None:
    """Structured log line for one ATS scoring call (redaction-filtered logger).

    Emitted for both the rubric and the deterministic-fallback paths so the ATS
    LLM path is observable (source / provider / model / latency / score, plus a
    short reason on a fallback). No key or CV content is logged; the root
    redaction filter (``logredact``) is a defence-in-depth backstop.
    """
    _log.info(
        "ats_score source=%s provider=%s model=%s latency_ms=%d score=%s%s",
        source,
        provider or "-",
        model or "-",
        latency_ms,
        score,
        f" reason={reason}" if reason else "",
    )


def ats_score_for_cv(
    *,
    cv_id: str,
    jd_text: str,
    provider: str | None = None,
    model: str | None = None,
    profile: str = "semantic_fit",
    compare_profiles: list[str] | None = None,
    job_id: str | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Score the caller's CV ``cv_id`` against ``jd_text`` (rubric → fallback).

    Loads the CV (RLS-scoped, so another tenant's is invisible → 404), parses
    the JD, computes the deterministic keyword score, then — when the rubric is
    enabled and an LLM key resolves — runs the LLM rubric with the deterministic
    score as a signal. On ANY rubric failure (no key, timeout, 429, auth,
    empty/malformed reply, unparseable JD, unexpected error) it falls back to the
    deterministic score. No document is created; the rubric is read-only so the
    1-page gate is untouched.

    Returns a JSON dict carrying ``source`` (``"rubric"`` | ``"deterministic"``)
    plus the matching fields. NEVER raises :class:`llm_gateway.LLMUnavailable`
    (no key degrades to deterministic, never a 402); only the deterministic 404 /
    400 :class:`TailorError`\\ s propagate.
    """
    started = time.monotonic()
    profile_id = ats_profiles.normalize_profile(profile)
    compare_ids = ats_profiles.normalize_profile_list(compare_profiles)
    wanted_profiles = []
    for item in [profile_id, *compare_ids]:
        if item not in wanted_profiles:
            wanted_profiles.append(item)
    wants_rubric = any(ats_profiles.rubric_preferred(p) for p in wanted_profiles)
    cv_model, _row = _load_source_cv(cv_id)
    try:
        jd_json = parse_jd(jd_text)
    except ValueError as exc:
        raise TailorError(400, {"error": str(exc), "code": "empty_jd"}) from exc

    # Deterministic score: always computed — it is both the fallback and the
    # signal fed to the rubric.
    det_result = score_cv_against_jd(cv_model, jd_text, jd_json=jd_json)
    det_payload = _deterministic_ats_payload(
        det_result,
        cv_model=cv_model,
        jd_text=jd_text,
        jd_json=jd_json,
    )

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    rubric_payload: dict[str, Any] | None = None
    active_det_payload = det_payload
    prov = canonical_provider(provider)

    if not wants_rubric:
        _log_ats_call(
            source="deterministic", provider=None, model=None,
            latency_ms=_elapsed_ms(), score=det_result.score, reason=f"profile_{profile_id}",
        )
    elif not ats_rubric_enabled():
        _log_ats_call(
            source="deterministic", provider=None, model=None,
            latency_ms=_elapsed_ms(), score=det_result.score, reason="rubric_flag_off",
        )
        active_det_payload = _deterministic_ats_payload(
            det_result,
            cv_model=cv_model,
            jd_text=jd_text,
            jd_json=jd_json,
        )
    else:
        # Resolve a key for the rubric. No key / no credit (LLMUnavailable) is
        # NOT an error here — ATS is free, so we silently use deterministic.
        try:
            api_key, used_managed = llm_gateway.resolve_llm(ATS_KIND, provider=prov)
        except llm_gateway.LLMUnavailable:
            _log_ats_call(
                source="deterministic", provider=prov, model=model,
                latency_ms=_elapsed_ms(), score=det_result.score, reason="needs_key",
            )
            active_det_payload = _deterministic_ats_payload(
                det_result,
                note="needs_key",
                cv_model=cv_model,
                jd_text=jd_text,
                jd_json=jd_json,
            )
        except Exception:  # noqa: BLE001 — never let key resolution break a free score
            _log_ats_call(
                source="deterministic", provider=prov, model=model,
                latency_ms=_elapsed_ms(), score=det_result.score, reason="key_error",
            )
            active_det_payload = _deterministic_ats_payload(
                det_result,
                note="key_error",
                cv_model=cv_model,
                jd_text=jd_text,
                jd_json=jd_json,
            )
        else:
            eff_model = _tailor_model(prov, model)
            try:
                client = make_client(prov, api_key)
                rubric, rubric_error = score_cv_with_rubric(
                    cv=cv_model,
                    jd_text=jd_text,
                    jd_json=jd_json,
                    deterministic_ats=det_result,
                    llm_client=client,
                    model=eff_model,
                )
            except Exception:  # noqa: BLE001 — client build / unexpected → deterministic
                _log_ats_call(
                    source="deterministic", provider=prov, model=eff_model,
                    latency_ms=_elapsed_ms(), score=det_result.score, reason="client_error",
                )
                active_det_payload = _deterministic_ats_payload(
                    det_result,
                    note="client_error",
                    cv_model=cv_model,
                    jd_text=jd_text,
                    jd_json=jd_json,
                )
            else:
                # Rubric returned a typed, recoverable error → deterministic.
                if rubric_error is not None or rubric is None:
                    reason = getattr(rubric_error, "reason", "rubric_unavailable")
                    _log_ats_call(
                        source="deterministic", provider=prov, model=eff_model,
                        latency_ms=_elapsed_ms(), score=det_result.score, reason=reason,
                    )
                    active_det_payload = _deterministic_ats_payload(
                        det_result,
                        note=reason or "Using the deterministic score.",
                        cv_model=cv_model,
                        jd_text=jd_text,
                        jd_json=jd_json,
                    )
                else:
                    if not used_managed:
                        llm_gateway.log_byo_usage(ATS_KIND, provider=prov)
                    _log_ats_call(
                        source="rubric", provider=prov, model=eff_model,
                        latency_ms=_elapsed_ms(), score=rubric.score,
                    )
                    rubric_payload = _rubric_ats_payload(
                        rubric,
                        det_result,
                        cv_model=cv_model,
                        jd_text=jd_text,
                        jd_json=jd_json,
                    )

    active_payload = ats_profiles.select_payload(
        profile_id,
        deterministic=active_det_payload,
        rubric=rubric_payload,
    )
    result = ats_profiles.attach_profile_metadata(
        active_payload,
        profile_id=profile_id,
        job_id=job_id,
        computed_at=now_iso,
    )
    result["jd_hash"] = _sha256_text(jd_text)
    result["source_hash"] = _sha256_text(_row.get("yaml_blob") or "")
    if compare_ids:
        result["profile_results"] = {
            pid: ats_profiles.attach_profile_metadata(
                ats_profiles.select_payload(pid, deterministic=active_det_payload, rubric=rubric_payload),
                profile_id=pid,
                job_id=job_id,
                computed_at=now_iso,
            )
            for pid in compare_ids
        }
    return result
