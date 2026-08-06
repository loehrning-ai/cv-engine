#!/usr/bin/env python3
"""Cloud CV editor — Flask gateway.

Two-pane editor: outline + CodeMirror in the middle, paged.js preview on
the right. Talks to ``engine/render`` to turn a CV's YAML into the same
HTML WeasyPrint will use.

Since Stage 2 the editor is multi-tenant: the browser logs in with
Supabase, sends a bearer token on every ``/api/*`` call, and each user's
CVs are stored as ``documents`` rows (type ``cv``) in Supabase Postgres
under row-level security (``owner_id = auth.uid()``). The render engine
itself is unchanged — it still renders YAML text to a 1-page PDF.

Stage 3 makes this multi-document: a user owns *many* CVs and manages them
end-to-end (list / create / rename / duplicate / delete). Every data route
takes an optional ``id`` selecting which document; with no id we fall back
to the most-recently-updated CV (seeding a skeleton if the user has none).
Document ids are validated UUIDs and every query runs under the user's JWT,
so RLS scopes each row to ``auth.uid()`` — a row the caller doesn't own is
simply invisible (treated as 404).

Run from the project root for local dev:

    python3 tools/editor/server.py

Then open http://127.0.0.1:5567.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fcntl

# Load .env (ANTHROPIC_API_KEY, Supabase/R2/KEK secrets) before anything
# reads os.environ. Guarded so the app still boots if python-dotenv isn't
# installed.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    request,
    send_file,
    send_from_directory,
)

THIS_DIR = Path(__file__).resolve().parent
REPO = THIS_DIR.parent.parent
CONTENT_DIR = REPO / "content"
STATIC_DIR = THIS_DIR / "static"

# Local-dev fallback document used only to seed a brand-new user's first CV
# (and as the shape reference). Per-user CVs live in Postgres now.
# ``ONEPAGER_CV_FILE`` (a filename under content/, or an absolute path) pins the
# seed document explicitly and always wins. Without it we fall back to the
# operator's own ``content/cv.yaml`` when it exists — that file is real personal
# data, so screenshot capture should pin ``cv.example.yaml`` and the demo shell
# always seeds the sample instead (see ``_local_demo_seed_cv_path``).
_CV_FILE_OVERRIDE = (os.environ.get("ONEPAGER_CV_FILE") or "").strip()
DEFAULT_CV_FILENAME = _CV_FILE_OVERRIDE or (
    "cv.yaml" if (CONTENT_DIR / "cv.yaml").exists() else "cv.example.yaml"
)
# An absolute override replaces CONTENT_DIR outright (pathlib join semantics).
CV_FILE = CONTENT_DIR / DEFAULT_CV_FILENAME

if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from render_preview import render_cv_html  # noqa: E402
from render import ai_extract  # noqa: E402
from render import content as content_mod  # noqa: E402
from render import css_base  # noqa: E402
from render import importers  # noqa: E402
from render import sections as sections_mod  # noqa: E402
from render import template_registry  # noqa: E402
from demo_policy import (  # noqa: E402
    assert_demo_mode_configuration_safe,
    demo_mode_configuration_error,
    demo_mode_requested,
    demo_request_is_loopback,
    resolve_privacy_contact,
)
try:
    from ats import profiles as ats_profiles  # noqa: E402
except ImportError:  # pragma: no cover - unusual import roots
    from engine.ats import profiles as ats_profiles  # noqa: E402

try:  # DEV-ONLY demo-mode flag (defaults OFF — see engine/llm/client.py).
    from llm.client import (  # noqa: E402
        SUPPORTED_PROVIDERS,
        canonical_provider,
        default_model_for,
        demo_mode_enabled,
        models_for_display,
        provider_spec,
        resolve_model,
    )
except ImportError:  # pragma: no cover - unusual import roots
    from engine.llm.client import (  # noqa: E402
        SUPPORTED_PROVIDERS,
        canonical_provider,
        default_model_for,
        demo_mode_enabled,
        models_for_display,
        provider_spec,
        resolve_model,
    )

import account_service  # noqa: E402
import analytics  # noqa: E402
import apply_service as apply_svc  # noqa: E402
import auth as auth_mod  # noqa: E402
import cover_letter_service as cl_service  # noqa: E402
import demo_seed  # noqa: E402 — DEV-ONLY demo content seeding (gated by ONEPAGER_DEMO_MODE)
import i18n  # noqa: E402
import image_guard  # noqa: E402
import jobs_service as jobs_svc  # noqa: E402
import llm_gateway  # noqa: E402
import local_demo_store  # noqa: E402
import logredact  # noqa: E402
import pdf_store  # noqa: E402
import tailor_service as tailor_svc  # noqa: E402

# Install log redaction on the ROOT logger before anything can log. This masks
# any sk-ant-… / sk-… key-shaped substring in every emitted record — a
# defence-in-depth backstop on top of "never log keys deliberately".
logredact.install()

DEFAULT_PORT = 5567

# Hard request-size ceiling (defence-in-depth for uploads). A separate,
# tighter cap applies to the CV YAML body in /api/cv POST.
MAX_REQUEST_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_CV_YAML_BYTES = 512 * 1024  # 512 KB

# Free-tier soft limit on how many CVs one user may keep. Stage 3 only
# *surfaces* this (the count + an ``at_limit`` flag on /api/cvs) so the UI
# can warn; it does NOT hard-block creation. Stage 11 wires real plan
# limits (and may enforce per-tier values). Keep it a single constant so
# that later change is one edit.
FREE_CV_LIMIT = 25

# Cap on the rename payload so a hostile/garbage name can't bloat a row.
MAX_CV_NAME_LEN = 200
LOCAL_DEMO_CV_ID = "00000000-0000-4000-8000-000000000001"
FIRST_RUN_TEMPLATE_CV_NAME = "Template CV - John Doe"
_FIRST_CV_MARKER_RPC_MISSING_LOGGED = False

# Public base URL of the deployment — used for SEO canonical/og:url tags,
# robots.txt, and sitemap.xml. Defaults to localhost for dev; the operator
# sets PUBLIC_BASE_URL to the real https origin in production (see DEPLOY.md).
PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL") or "http://localhost:8000"
).rstrip("/")
assert_demo_mode_configuration_safe()

# Deliverable contact address for GDPR requests, substituted into the privacy
# policy at serve time. Public origins fail startup unless the operator
# explicitly supplies a validated address. Loopback dev uses a reserved
# non-deliverable placeholder and never impersonates the repository maintainer.
PRIVACY_CONTACT = resolve_privacy_contact()

# Warn (don't fail) when the operator hasn't set PUBLIC_BASE_URL: the localhost
# default breaks SEO canonical/og:url tags and the strict-CORS allow-list in
# production. Emitted once at import via the (redaction-filtered) logger.
if not os.environ.get("PUBLIC_BASE_URL"):
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "PUBLIC_BASE_URL is unset — defaulting to %s. Set it to the real https "
        "origin in production (SEO canonical/og:url + CORS allow-list).",
        PUBLIC_BASE_URL,
    )


class _BadRequest(Exception):
    """Raised for a malformed id/payload → surfaced as a 400 JSON response."""


class _NotFound(Exception):
    """Raised when a doc id isn't owned/visible → surfaced as a 404 JSON.

    RLS makes another tenant's row invisible, so "row not found" and
    "owned by someone else" collapse to the same signal — both are 404.
    """


class _Conflict(Exception):
    """Raised when a valid request conflicts with account/document state."""


class _ArtifactCleanupUnavailable(Exception):
    """Raised when derived PDF storage cannot be cleaned."""

    def __init__(self, *, document_deleted: bool = False) -> None:
        super().__init__("PDF artifact cleanup unavailable")
        self.document_deleted = document_deleted


class _CoverSourceUnavailable(Exception):
    """A legacy letter cannot resolve the signer it did not snapshot."""

    def __init__(self, *, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _request_provider_model(payload: dict) -> tuple[str, str | None]:
    """Resolve the effective ``(provider, model)`` for an AI request.

    Precedence: explicit request body → caller profile preference → global
    default. Explicit invalid provider/model values fail closed with 400; only
    omitted values fall back. This prevents a stale or malicious body from
    silently spending another provider path.
    """
    body_provider = payload.get("provider") if isinstance(payload, dict) else None
    body_model = payload.get("model") if isinstance(payload, dict) else None

    provider = None
    explicit_provider = body_provider is not None and str(body_provider).strip() != ""
    if explicit_provider:
        if not isinstance(body_provider, str):
            raise _BadRequest("invalid provider")
        provider = canonical_provider(body_provider)
        if provider not in SUPPORTED_PROVIDERS:
            raise _BadRequest("invalid provider")

    profile: dict = {}
    if provider is None or body_model is None:
        try:
            profile = account_service.get_profile()
        except Exception:  # noqa: BLE001 — never fail an AI call on a profile read
            profile = {}

    if provider is None:
        pref = canonical_provider(profile.get("preferred_provider"))
        provider = pref if pref in SUPPORTED_PROVIDERS else "anthropic"

    model = None
    if body_model is not None and not isinstance(body_model, str):
        raise _BadRequest("invalid model")
    if isinstance(body_model, str) and body_model.strip():
        model = body_model.strip()
    elif profile.get("preferred_model"):
        model = profile.get("preferred_model")

    if model is not None and resolve_model(provider, model) != model:
        raise _BadRequest("invalid model")

    return provider, model


app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES


@app.before_request
def _refuse_non_loopback_demo_request() -> object | None:
    """Block every route when demo mode reaches a non-loopback HTTP peer."""
    if demo_mode_requested() and (
        demo_mode_configuration_error()
        or not demo_request_is_loopback(
            request.host,
            request.remote_addr,
            {
                name: request.headers.get(name, "")
                for name in (
                    "Forwarded",
                    "Via",
                    "CF-Connecting-IP",
                    "X-Forwarded-For",
                    "X-Forwarded-Host",
                    "X-Forwarded-Proto",
                    "X-Real-IP",
                )
            },
        )
    ):
        return (
            jsonify(
                {
                    "error": "Demo mode is restricted to direct loopback access.",
                    "code": "unsafe_demo_mode",
                }
            ),
            503,
        )
    return None


# Auth gate: every /api/* request needs a valid Supabase JWT; the login
# page, static assets, and /healthz are exempt (see auth.require_auth).
# Registered via a thin wrapper (not the function object directly) so the
# guard is resolved through ``auth_mod`` on each request — which lets tests
# swap in a stub identity without disabling the gate for real traffic.
def _before_request_auth() -> object:
    return auth_mod.require_auth()


app.before_request(_before_request_auth)


# ---------------------------------------------------------------------------
# Security response headers + strict CORS (web-layer hardening).
#
# Every response gets nosniff + clickjacking protection + a Content-Security-
# Policy. The CSP is constraining (default-src 'self', object-src 'none',
# base-uri 'self', frame-ancestors 'none'). Every browser-executable dependency
# ships below /static/vendor, so script/style origins stay same-origin. The only
# cross-origin browser connection is the configured Supabase origin used for
# auth/PostgREST. Inline CV photos/fonts use data: URLs.
#
# CORS is deliberately NON-permissive: we NEVER reflect an arbitrary request
# Origin onto /api/*. The browser app is same-origin with its API (no CORS
# header needed); only an explicitly-configured PUBLIC_BASE_URL origin is
# allowed cross-origin. An attacker page's Origin is simply never echoed.
# ---------------------------------------------------------------------------
# Supabase origin (auth + PostgREST) the browser must reach via fetch/XHR.
_SUPABASE_ORIGIN = (auth_mod.SUPABASE_URL or "").rstrip("/")

# Allowed cross-origin callers for /api/* (same-origin needs no entry). Only the
# operator-configured public origin is trusted; everything else is refused.
_ALLOWED_CORS_ORIGINS = frozenset(
    o for o in (PUBLIC_BASE_URL,) if o and not o.startswith("http://localhost")
)


def _csp_for(path: str) -> str:
    """Build the Content-Security-Policy for ``path``.

    All scripts and stylesheets load from this service. Rendered preview HTML
    contains inline CSS but no inline JavaScript: preview-runtime.js and
    Paged.js are versioned local assets. Keeping script-src at 'self' on every
    route prevents a compromised CDN from reading or exfiltrating CV contents.
    """
    script_src = "script-src 'self'"
    style_src = "style-src 'self' 'unsafe-inline'"
    connect_extra = f" {_SUPABASE_ORIGIN}" if _SUPABASE_ORIGIN else ""
    directives = [
        "default-src 'self'",
        script_src,
        "script-src-attr 'none'",
        style_src,
        # Inline CV photos arrive as data: URIs; fonts are served from /asset/.
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        f"connect-src 'self'{connect_extra}",
        # The preview is a same-origin iframe; never allow being framed.
        "frame-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
    return "; ".join(directives)


@app.after_request
def _security_headers(resp):  # type: ignore[no-untyped-def]
    """Attach security headers + a strict CORS policy to every response."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers["Content-Security-Policy"] = _csp_for(request.path)

    # Keep the editor app + per-user API out of search indexes.
    if request.path == "/app" or request.path.startswith("/api/"):
        resp.headers.setdefault("X-Robots-Tag", "noindex")

    # API responses can contain complete CVs, job descriptions, account data,
    # private photos, previews, and rendered exports. They must not survive in
    # browser, proxy, or intermediary caches after logout or erasure.
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "private, no-store, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"

    # Strict CORS: only echo an explicitly-allowed origin, never reflect a
    # hostile one. Same-origin requests carry no Origin and need no header.
    origin = request.headers.get("Origin")
    if origin and origin in _ALLOWED_CORS_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers.setdefault("Access-Control-Allow-Credentials", "true")
        resp.headers.setdefault(
            "Access-Control-Allow-Headers", "Authorization, Content-Type"
        )
        resp.headers.setdefault(
            "Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS"
        )
    return resp


@app.after_request
def _demo_write_signal(resp):  # type: ignore[no-untyped-def]
    """Mark ephemeral demo-shell writes with ``X-OnePager-Demo-Write``.

    Every successful mutating request by the LOCAL demo owner is served from
    the in-memory ``local_demo_store`` — nothing survives a restart. The
    header gives the UI + tests ONE consistent ephemeral-write signal on all
    those mutations (POST /api/jobs, multi-CV creates, …); previously only
    the /api/cv save fallback set it. Deliberately cheap: a header setdefault,
    gated to the demo owner + mutating method + non-error response.
    """
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and resp.status_code < 400
        and local_demo_store.active()
    ):
        resp.headers.setdefault("X-OnePager-Demo-Write", "server-process-memory")
    return resp


# ---------------------------------------------------------------------------
# Per-user rate limiting (Flask-Limiter). Keys are derived from the verified
# JWT subject (g.owner_id) so limits are per-account, not per-IP — one user
# can't exhaust another's budget, and a shared NAT doesn't collapse everyone
# into one bucket. In-memory storage is fine for the single-process editor;
# Stage 8+ can swap in Redis if we scale out. Applied to the AI + key routes
# (the expensive / abuse-prone ones) below.
# ---------------------------------------------------------------------------
def _rate_limit_key() -> str:
    """Identify the caller for rate limiting: their owner_id, else remote IP."""
    owner = getattr(g, "owner_id", None)
    if owner:
        return f"user:{owner}"
    return f"ip:{request.remote_addr or 'unknown'}"


try:
    from flask_limiter import Limiter

    limiter = Limiter(
        key_func=_rate_limit_key,
        app=app,
        default_limits=[],  # opt-in per route; no global cap
        storage_uri="memory://",
    )
    _LIMIT_AI = "20 per minute"
    _LIMIT_RENDER = "12 per minute"
    _LIMIT_PREVIEW = "120 per minute"
    _LIMIT_PHOTO = "20 per hour"
    _LIMIT_EXPORT = "6 per hour"
    # The demo-login route is unauthenticated (it IS the login) and proxies a
    # GoTrue password grant, so it's keyed by IP and capped tighter to blunt
    # brute-force/abuse. Only ever reachable when demo mode is on anyway.
    _LIMIT_DEMO_LOGIN = "10 per minute"
except ImportError:  # pragma: no cover - limiter optional in bare envs
    limiter = None
    _LIMIT_AI = ""
    _LIMIT_RENDER = ""
    _LIMIT_PREVIEW = ""
    _LIMIT_PHOTO = ""
    _LIMIT_EXPORT = ""
    _LIMIT_DEMO_LOGIN = ""


def _ai_rate_limit(fn):
    """Decorator applying the per-user AI limit when the limiter is present."""
    if limiter is None:
        return fn
    return limiter.limit(_LIMIT_AI)(fn)


def _demo_login_rate_limit(fn):
    """Decorator applying the demo-login limit when the limiter is present."""
    if limiter is None:
        return fn
    return limiter.limit(_LIMIT_DEMO_LOGIN)(fn)


def _resource_rate_limit(limit: str):
    """Apply an authenticated resource budget; unit tests are exempt."""
    def decorate(fn):
        if limiter is None:
            return fn
        return limiter.limit(
            limit,
            exempt_when=lambda: bool(app.config.get("TESTING")),
        )(fn)

    return decorate


def _render_rate_limit(fn):
    return _resource_rate_limit(_LIMIT_RENDER)(fn)


def _preview_rate_limit(fn):
    return _resource_rate_limit(_LIMIT_PREVIEW)(fn)


def _photo_rate_limit(fn):
    return _resource_rate_limit(_LIMIT_PHOTO)(fn)


def _export_rate_limit(fn):
    return _resource_rate_limit(_LIMIT_EXPORT)(fn)


@app.errorhandler(429)
def _rate_limited(_exc) -> object:
    """Uniform JSON for a per-user rate-limit hit."""
    return (
        jsonify({"error": "Too many requests — slow down a moment.", "code": "rate_limited"}),
        429,
    )


@app.errorhandler(413)
def _too_large(_exc) -> object:
    """Uniform JSON for a request body over MAX_CONTENT_LENGTH."""
    return (
        jsonify({"error": "payload too large", "max_bytes": MAX_REQUEST_BYTES}),
        413,
    )


@app.errorhandler(_BadRequest)
def _handle_bad_request(exc: _BadRequest) -> object:
    return jsonify({"error": str(exc) or "bad request"}), 400


@app.errorhandler(_NotFound)
def _handle_not_found(exc: _NotFound) -> object:
    return jsonify({"error": str(exc) or "not found"}), 404


@app.errorhandler(_Conflict)
def _handle_conflict(exc: _Conflict) -> object:
    return jsonify({"error": str(exc) or "conflict"}), 409


@app.errorhandler(_ArtifactCleanupUnavailable)
def _handle_artifact_cleanup_unavailable(exc: _ArtifactCleanupUnavailable) -> object:
    payload = {
        "error": "PDF artifact storage is temporarily unavailable.",
        "code": "artifact_cleanup_unavailable",
    }
    if exc.document_deleted:
        payload["document_deleted"] = True
    return (
        jsonify(payload),
        503,
    )


@app.errorhandler(llm_gateway.LLMUnavailable)
def _handle_llm_unavailable(exc: llm_gateway.LLMUnavailable) -> object:
    """Map a key/credit policy decision to its HTTP response (402/503/…)."""
    return jsonify(exc.payload), exc.status


@app.errorhandler(cl_service.CoverLetterError)
def _handle_cover_letter_error(exc: cl_service.CoverLetterError) -> object:
    """Map a cover-letter generation/guard failure to its JSON response.

    Covers the grounding block (422 ``unverified_claims`` + offending items),
    the word-cap rejection, a 404 CV-not-found, and provider failures.
    """
    return jsonify(exc.payload), exc.status


@app.errorhandler(cl_service.DocumentInsertError)
def _handle_document_insert_error(exc: cl_service.DocumentInsertError) -> object:
    status = 409 if exc.code in {"account_unavailable", "job_unavailable"} else 503
    return jsonify({"error": "Document creation was blocked.", "code": exc.code}), status


@app.errorhandler(tailor_svc.TailorError)
def _handle_tailor_error(exc: tailor_svc.TailorError) -> object:
    """Map a CV-tailoring failure to its JSON response.

    Covers the anti-fabrication block (422 ``unverified_claims`` + offending
    items), the 1-page overflow (409 ``overflow`` + the offending heading), a
    404 CV-not-found, and provider failures.
    """
    return jsonify(exc.payload), exc.status


@app.errorhandler(jobs_svc.JobError)
def _handle_job_error(exc: jobs_svc.JobError) -> object:
    """Map a saved-jobs failure to its JSON response.

    Covers an SSRF block / fetch failure (400), an empty JD (400), a job not
    owned/visible (404), and an oversized JD (413). One-click sub-step failures
    (tailor / cover-letter guards) surface via their own handlers above.
    """
    return jsonify(exc.payload), exc.status


@app.errorhandler(auth_mod.AuthError)
def _handle_auth_error(exc: auth_mod.AuthError) -> object:
    """Keep auth/storage-configuration failures on the JSON API contract.

    ``user_supabase()``/``server_supabase()`` raise ``AuthError(500)`` when no
    Supabase project is configured; without this handler those surfaced as
    Flask's HTML 500 page on ``/api/*`` writes (plan 107, F14).
    """
    code = "storage_unavailable" if exc.status >= 500 else "auth_error"
    return jsonify({"error": exc.message, "code": code}), exc.status


@app.errorhandler(apply_svc.ApplyError)
def _handle_apply_error(exc: apply_svc.ApplyError) -> object:
    """Map external-apply policy failures to their JSON response."""
    return jsonify(exc.payload), exc.status


@app.route("/healthz")
def healthz() -> object:
    """Liveness probe — no auth, no DB. Used by the container/orchestrator."""
    return jsonify({"ok": True})


@app.route("/auth/config")
def auth_config() -> object:
    """Publishable Supabase config for the browser login client.

    Returns ONLY the publishable URL + key (both browser-safe by design).
    Never the service-role key, JWT secret, or KEK. Exempt from auth so
    the login page can boot the Supabase JS client before a session exists.

    ``demo_mode`` (DEV-ONLY, defaults False) tells the login UI whether to show
    the one-click demo button. It is just a boolean — the demo CREDENTIALS are
    NEVER exposed here; the grant happens server-side in ``/auth/demo-login``.
    """
    return jsonify(
        {
            "supabase_url": auth_mod.SUPABASE_URL,
            "supabase_publishable_key": auth_mod.SUPABASE_PUBLISHABLE_KEY,
            "demo_mode": demo_mode_enabled(),
        }
    )


def _local_demo_session() -> dict[str, object]:
    expires_at = int(time.time()) + 3600
    return {
        "access_token": auth_mod.make_local_demo_access_token(),
        "refresh_token": secrets.token_urlsafe(32),
        "expires_at": expires_at,
        "local_demo": True,
    }


def _remote_pdf_storage_enabled() -> bool:
    """Whether this request may use authoritative R2 artifact storage."""
    return pdf_store.r2_enabled() and not local_demo_store.active()


@app.route("/auth/demo-login", methods=["POST"])
@_demo_login_rate_limit
def auth_demo_login() -> object:
    """DEV-ONLY one-click demo login (gated behind ``ONEPAGER_DEMO_MODE``).

    When demo mode is OFF (production) this route does not functionally exist —
    it returns 404, exposing nothing. When ON, it performs a SERVER-SIDE GoTrue
    password grant for the demo user against ``${SUPABASE_URL}/auth/v1/token``
    only when ``DEMO_EMAIL`` and ``DEMO_PASSWORD`` are both explicitly set.
    Otherwise it stays entirely local. The route returns the same
    ``{access_token, refresh_token}`` shape the browser login client expects.

    The credentials and tokens are never logged. A failed explicit grant falls
    back to a local demo token that is accepted only while loopback-safe demo
    mode is enabled. Auth-exempt (it is itself the login path) and rate-limited.
    """
    if not demo_mode_enabled():
        # Off by default: behave as if the route does not exist.
        abort(404)

    supabase_url = (auth_mod.SUPABASE_URL or "").rstrip("/")
    publishable_key = auth_mod.SUPABASE_PUBLISHABLE_KEY or ""
    email = (os.environ.get("DEMO_EMAIL") or "").strip()
    password = os.environ.get("DEMO_PASSWORD") or ""
    if not supabase_url or not publishable_key or not email or not password:
        # Zero-config local runs (fresh clone, no .env): fall back to the
        # local session. A configured Supabase project is not contacted unless
        # the operator also supplied both demo credentials explicitly.
        return jsonify(_local_demo_session())

    try:
        tokens = _demo_password_grant(supabase_url, publishable_key, email, password)
    except Exception:  # noqa: BLE001 — never leak the creds/tokens/exception text
        tokens = _local_demo_session()

    if not tokens or not tokens.get("access_token") or not tokens.get("refresh_token"):
        tokens = _local_demo_session()

    # Seed the demo user with real, working content (a sample CV + cover letter)
    # so the demo lands on a populated product, not an empty skeleton. We bind
    # the request's JWT to the just-granted demo token so the RLS-scoped inserts
    # run AS the demo user (owner_id = auth.uid()). Idempotent + best-effort:
    # repeated demo logins don't duplicate, and a seeding failure never blocks
    # the login (it only returns the tokens regardless). Gated: only ever runs
    # here, which itself requires demo mode to be on.
    if not tokens.get("local_demo"):
        try:
            g.access_token = tokens["access_token"]
            demo_seed.seed_demo_documents()
        except Exception:  # noqa: BLE001 — seeding must never break the login
            pass

    # Return the SAME shape Supabase returns so the browser's setSession works.
    body = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
    }
    if tokens.get("local_demo"):
        body["local_demo"] = True
        body["expires_at"] = tokens.get("expires_at")
    return jsonify(body)


def _demo_password_grant(
    supabase_url: str, publishable_key: str, email: str, password: str
) -> dict:
    """Server-side GoTrue password grant for the demo user (DEV-ONLY).

    Returns the parsed token JSON on success (``access_token`` / ``refresh_token``
    among the fields). Raises on any transport/HTTP failure so the caller maps
    it to a clean 503. NEVER logs the email/password/tokens. Kept as a separate
    function so tests can monkeypatch it without hitting the network.
    """
    import urllib.request

    url = f"{supabase_url}/auth/v1/token?grant_type=password"
    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "apikey": publishable_key,
            "Authorization": f"Bearer {publishable_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed https origin
        raw = resp.read().decode("utf-8")
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# BYO LLM key management (Stage 4). AES-GCM envelope-encrypted at rest; the
# plaintext key is never stored, returned, or logged. RLS scopes every row to
# the caller. Key mutations are rate-limited; passive status reads are not,
# because the editor and account page poll them as lightweight UI state.
# ---------------------------------------------------------------------------
@app.route("/api/key", methods=["GET"])
def api_key_status() -> object:
    """Return per-provider BYO-key connection state and managed credits."""
    try:
        return jsonify(llm_gateway.key_status_all())
    except Exception as exc:  # noqa: BLE001 — passive UI state must not break the shell
        app.logger.warning("key status unavailable: %s", exc)
        return jsonify(_degraded_key_status())


def _degraded_key_status() -> dict[str, object]:
    return {
        "providers": {
            provider: {"connected": False, "last4": None}
            for provider in SUPPORTED_PROVIDERS
        },
        "managed_credits_remaining": 0,
        "degraded": True,
    }


@app.route("/api/key", methods=["POST", "DELETE"])
@_ai_rate_limit
def api_key() -> object:
    """Manage the caller's BYO provider keys (Anthropic + Gemini) + credits.

    POST {provider:'anthropic'|'gemini', key} → validate with a minimal provider
        call, then encrypt + upsert. Returns {ok, provider, last4}. Invalid key →
        400 with a clear message. The plaintext is never persisted/returned.
    DELETE ?provider= → remove that provider's stored key. {deleted: bool}.
    """
    _ensure_account_mutations_allowed()
    if request.method == "DELETE":
        # The provider to remove may come from the query string or a JSON body;
        # default Anthropic for backward compatibility.
        prov = (
            request.args.get("provider")
            or (request.get_json(silent=True) or {}).get("provider")
            or "anthropic"
        )
        provider = canonical_provider(prov)
        if provider not in SUPPORTED_PROVIDERS:
            return jsonify({"error": "unsupported provider", "code": "invalid_provider"}), 400
        return jsonify({"deleted": llm_gateway.delete_key(provider)})

    # POST — validate then store.
    payload = request.get_json(silent=True) or {}
    provider = canonical_provider(payload.get("provider") or "anthropic")
    if provider not in SUPPORTED_PROVIDERS:
        return jsonify({"error": "unsupported provider", "code": "invalid_provider"}), 400
    if provider_spec(provider).auth_type == "none":
        return jsonify({"error": "provider does not use stored API keys", "code": "no_key_required"}), 400
    if local_demo_store.active():
        # Reject before validate_provider_key: that validation intentionally
        # calls the live provider, while demo mode never stores or uses BYO
        # keys and must stay offline.
        return jsonify(
            {
                "error": "API keys are not stored in demo mode — the demo uses local AI stubs.",
                "code": "demo_mode_key_not_stored",
            }
        ), 400
    key = payload.get("key")
    if not isinstance(key, str) or not key.strip():
        return jsonify({"error": "missing 'key' string"}), 400
    key = key.strip()

    try:
        llm_gateway.validate_provider_key(provider, key)
    except llm_gateway.KeyValidationError as exc:
        # Clear, non-leaking message — never echo the key back.
        return jsonify({"error": str(exc), "code": "invalid_key"}), 400

    last4 = llm_gateway.save_key(g.owner_id, provider, key)
    return jsonify({"ok": True, "provider": provider, "last4": last4})


@app.route("/api/models")
def api_models() -> object:
    """The provider/model allowlist for the AI-settings selector.

    Returns ``{providers:[{id, label, default, models:[…]}], default_provider}``
    so the UI can render the provider segmented control + the per-provider model
    dropdown from a single centralized source (no model strings hardcoded in
    JS). Hosted-provider entries are the in-process allowlist; Ollama may add
    best-effort models discovered from its loopback-only local endpoint. No
    remote provider account or secret is queried.
    """
    providers = [
        {
            "id": p,
            "label": provider_spec(p).label,
            "auth_type": provider_spec(p).auth_type,
            "managed_credit_eligible": provider_spec(p).managed_credit_eligible,
            "default": default_model_for(p),
            "models": list(models_for_display(p)),
        }
        for p in SUPPORTED_PROVIDERS
    ]
    return jsonify({"providers": providers, "default_provider": "anthropic"})


# ---------------------------------------------------------------------------
# Account self-service (Stage 10): profile, GDPR data export + erasure.
# Every route here is RLS-scoped via account_service → user_supabase(), so a
# caller can only ever read/modify/delete their OWN account.
# ---------------------------------------------------------------------------
@app.route("/api/account/profile", methods=["GET", "POST"])
def api_account_profile() -> object:
    """Read or edit the caller's profile (display_name, default_language).

    GET  → ``{id, email, display_name, default_language,
             managed_credits_remaining, deletion_requested_at}``.
    POST ``{display_name?, default_language?}`` → updated profile.

    ``managed_credits_remaining`` is read-only context — it is NEVER writable
    through this route (server-side ignore + DB column-grant enforcement), so a
    user cannot mint themselves free credits by editing their profile.
    """
    if request.method == "GET":
        try:
            return jsonify(account_service.get_profile())
        except Exception as exc:  # noqa: BLE001 — demo/local shell should stay readable offline
            if not _can_use_demo_fallback_when_not_pending():
                raise
            app.logger.warning("using degraded demo profile fallback: %s", exc)
            return jsonify(
                {
                    "id": getattr(g, "owner_id", "") or "",
                    "email": getattr(g, "email", "") or "",
                    "display_name": "",
                    "default_language": "en",
                    "managed_credits_remaining": 0,
                    "deletion_requested_at": None,
                    "degraded": True,
                }
            )

    _ensure_account_mutations_allowed()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        raise _BadRequest("expected a JSON object")
    try:
        updated = account_service.update_profile(payload)
    except ValueError as exc:
        raise _BadRequest(str(exc)) from exc
    return jsonify(updated)


@app.route("/api/account/export")
@_export_rate_limit
def api_account_export() -> object:
    """Download the caller's structured live-data export as JSON.

    Returns the explicitly scoped account-service export with a
    ``Content-Disposition: attachment`` header. It is not a claim to include
    every operational, deleted, archived, or external-file record.
    """
    data = account_service.export_account_data()
    body = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    stamp = data.get("exported_at", "")[:10] or "export"
    filename = f"onepager-export-{stamp}.json"
    return (
        body,
        200,
        {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.route("/api/account/delete", methods=["POST"])
def api_account_delete() -> object:
    """Right to erasure — hide now, ordered external/database purge after D+30.

    Calls the SECURITY DEFINER RPC that stamps ``deletion_requested_at`` and
    soft-deletes the caller's documents (they disappear from every list/get
    endpoint at once). The production worker claims the account at D+30,
    removes R2/photo storage, validates its lease, and only then admin-deletes
    the Auth user so database cascades run.
    """
    payload = _json_object()
    if payload.get("confirm") != "DELETE":
        return jsonify({"error": "confirmation required", "code": "confirm_required"}), 400
    # The account transition and every photo operation share this cross-process
    # owner fence. A photo mutation therefore finishes before the transition,
    # or acquires the lock afterward and observes the pending account.
    with _owner_photo_render_lock(exclusive=True):
        result = account_service.request_account_deletion()
    artifacts = result.pop("_artifacts", [])
    if artifacts:
        try:
            pdf_store.delete_detached_artifacts(artifacts, strict=False)
        except Exception as exc:  # noqa: BLE001 - account transition committed
            app.logger.warning(
                "account PDF cleanup deferred: %s",
                type(exc).__name__,
            )
    invalid_artifact_count = int(result.get("invalid_artifact_count") or 0)
    if _artifact_cleanup_applies():
        try:
            result["cleanup_pending"] = (
                pdf_store.pending_artifact_cleanup_count() > 0
                or invalid_artifact_count > 0
            )
        except Exception:
            result["cleanup_pending"] = True
    # Do NOT delete the on-disk photo here: the request only SOFT-deletes (the
    # 30-day grace window is fully reversible via /api/account/cancel-delete,
    # which restores the documents). The photo is already unreachable while
    # deletion is pending — api_get_photo returns 409 via
    # _ensure_account_mutations_allowed — so keeping the file lets a cancel fully
    # restore the account instead of silently losing the headshot (GDPR
    # reversibility). The production purge worker removes the exact owner
    # directory after D+30, before it deletes the Auth/database account.
    return jsonify(result)


@app.route("/api/account/cancel-delete", methods=["POST"])
def api_account_cancel_delete() -> object:
    """Cancel a pending erasure strictly before D+30 (restores documents)."""
    return jsonify(account_service.cancel_account_deletion())


# ---------------------------------------------------------------------------
# Per-user primary CV storage (Supabase ``documents``, type='cv', RLS-scoped).
# ---------------------------------------------------------------------------
def _skeleton_cv_yaml() -> str:
    """Realistic safe template CV YAML used to seed a brand-new user.

    The seeded first-run document must be useful enough to render, edit, and
    delete without looking like a broken empty state. It is fictitious, ASCII
    only, and compact enough for the one-page renderer.
    """
    return (
        "# Template CV - John Doe. Replace every field with your own facts.\n"
        "name: John Doe\n"
        "accent: '#0f766e'\n"
        "font: sans\n"
        "density: tight\n"
        "paper: white\n"
        "template: classic\n"
        "contact:\n"
        "  - network: mail\n"
        "    username: john.doe@example.com\n"
        "  - network: linkedin\n"
        "    username: john-doe\n"
        "  - label: Warsaw, Poland\n"
        "summary: Software engineer focused on reliable product systems, clear documentation, and measurable delivery outcomes.\n"
        "skills:\n"
        "  - label: Languages\n"
        "    items: Python, TypeScript, SQL\n"
        "  - label: Product\n"
        "    items: Product analytics, documentation, release quality\n"
        "experience:\n"
        "  - role: Software Engineer\n"
        "    company: Example Systems\n"
        "    location: Warsaw, Poland\n"
        "    start: '2022'\n"
        "    end: Present\n"
        "    bullets:\n"
        "      - Built workflow tools that reduced manual review time by 35%.\n"
        "      - Improved release quality with automated checks and clearer runbooks.\n"
        "    stack: Python, TypeScript, PostgreSQL\n"
        "  - role: Data Analyst\n"
        "    company: Example Analytics\n"
        "    location: Berlin, Germany\n"
        "    start: '2020'\n"
        "    end: '2022'\n"
        "    bullets:\n"
        "      - Created dashboards used by sales and operations leadership.\n"
        "      - Turned raw customer data into weekly planning metrics.\n"
        "    stack: SQL, dbt, Looker\n"
        "education:\n"
        "  - degree: BSc Computer Science\n"
        "    school: Example University\n"
        "    location: Warsaw, Poland\n"
        "    start: '2017'\n"
        "    end: '2020'\n"
        "projects:\n"
        "  - title: Document Automation Toolkit\n"
        "    desc: Small internal tool that generated reviewed PDFs from structured source data.\n"
        "    bullets:\n"
        "      - Added validation rules that caught missing fields before export.\n"
    )


def _is_uuid(value: object) -> bool:
    """True iff ``value`` is a well-formed UUID string.

    Document ids arrive from the URL/body and are fed into PostgREST ``eq``
    filters; validating the shape up front lets us return a clean 400 for
    obvious garbage rather than leaking a Postgres cast error.
    """
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _list_cv_rows() -> list[dict]:
    """All of the user's non-deleted CVs, newest-edited first.

    RLS scopes the SELECT to ``owner_id = auth.uid()``, so this only ever
    returns the caller's own rows. Ordering by ``updated_at`` desc makes the
    first element the "active by default" CV used when no id is supplied.
    """
    if local_demo_store.active():
        # Materialize the seeded primary CV so the demo library lists it
        # alongside any CVs created through POST /api/cvs.
        _local_demo_cv_store()
    sb = _document_store()
    rows: list[dict] = []
    page_size = 500
    offset = 0
    while True:
        page = (
            sb.table("documents")
            .select("id, name, slug, job_id, created_at, updated_at, last_used_at")
            .eq("type", "cv")
            .is_("deleted_at", "null")
            .order("last_used_at", desc=True)
            .order("updated_at", desc=True)
            .order("id")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return rows


def _most_recent_cv_row() -> dict | None:
    """Fetch one full CV only when an editor/build needs its content."""
    if local_demo_store.active():
        _local_demo_cv_store()
    rows = (
        _document_store()
        .table("documents")
        .select(
            "id, name, slug, yaml_blob, accent, font, density, job_id, "
            "created_at, updated_at, last_used_at"
        )
        .eq("type", "cv")
        .is_("deleted_at", "null")
        .order("last_used_at", desc=True)
        .order("updated_at", desc=True)
        .order("id")
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def _has_any_cv_rows() -> bool:
    """Whether the caller has ever had a CV row, including soft-deleted rows.

    This distinguishes first login from an intentional last-CV deletion. First
    login seeds the template. A user who deletes the last CV sees an empty
    library instead of having the template silently recreated.
    """
    sb = auth_mod.user_supabase()
    resp = (
        sb.table("documents")
        .select("id")
        .eq("type", "cv")
        .limit(1)
        .execute()
    )
    return bool(resp.data or [])


def _document_store():
    """Storage client for CV document CRUD: the in-memory demo store for the
    local demo owner, else the real RLS-scoped Supabase client.

    Mirrors ``account_service._sb``: the demo session's ``alg:none`` token can
    never authorize against a real Supabase project, so multi-CV writes
    (create / rename / duplicate / delete / save) would otherwise 500 in the
    local demo shell. Every real tenant keeps the failing-loudly Supabase path.
    """
    if local_demo_store.active():
        return local_demo_store.db()
    return auth_mod.user_supabase()


def _cv_row_by_id(doc_id: str) -> dict | None:
    """Return the user's CV row with ``doc_id``, or ``None``.

    ``None`` covers both "no such id" and "owned by someone else" — RLS
    makes another tenant's row invisible, so an empty result is the
    intended 404 signal. Callers must have validated ``doc_id`` as a UUID.
    """
    if local_demo_store.active():
        # Materialize the seeded primary CV so LOCAL_DEMO_CV_ID resolves
        # exactly like any created demo CV (rename / duplicate / delete).
        _local_demo_cv_store()
    sb = _document_store()
    resp = (
        sb.table("documents")
        .select("id, name, slug, yaml_blob, accent, font, density, job_id, created_at, updated_at, last_used_at")
        .eq("type", "cv")
        .eq("id", doc_id)
        .is_("deleted_at", "null")
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def _local_demo_seed_cv_path() -> Path:
    """Seed document for the LOCAL demo shell's primary CV.

    A demo instance must never serve the operator's real ``content/cv.yaml``
    (personal data — see the note at ``DEFAULT_CV_FILENAME``), so in demo mode
    we always seed the shipped public sample ``content/cv.example.yaml``
    (Jordan Avery), regardless of whether ``cv.yaml`` exists on this machine.
    An explicit ``ONEPAGER_CV_FILE`` pin still wins (it is the documented way
    to override the seed); non-demo seeding keeps ``CV_FILE`` unchanged.
    """
    if demo_mode_enabled():
        return CONTENT_DIR / (_CV_FILE_OVERRIDE or "cv.example.yaml")
    return CV_FILE


def _local_demo_cv_store() -> dict:
    """The seeded primary demo CV row for local demo-mode browser QA.

    The row lives in ``local_demo_store``'s ``documents`` table under the
    fixed ``LOCAL_DEMO_CV_ID`` so the seeded primary CV and any CVs created
    through the multi-CV routes share ONE model — list / read / rename /
    duplicate / delete and the GDPR export all see the same rows. It is
    (re-)materialized lazily, so a store reset (test isolation) or a demo
    shell restart starts from the bundled sample CV again (see
    ``_local_demo_seed_cv_path``).

    This is intentionally process-local and gated by ``ONEPAGER_DEMO_MODE``
    at the call sites. It lets strict local tests exercise save/reload
    behavior when Supabase is unavailable, without claiming a durable
    database write.
    """
    with local_demo_store.locked():
        table = local_demo_store.db().table("documents")
        matches = [row for row in table.rows if row.get("id") == LOCAL_DEMO_CV_ID]
        if matches:
            # Older processes could race the lazy materialization and create
            # duplicate rows with the fixed sentinel id. Prefer the newest
            # saved copy and collapse the invalid duplicates in place.
            row = max(matches, key=lambda item: str(item.get("updated_at") or ""))
            if len(matches) > 1:
                table.rows[:] = [
                    item for item in table.rows
                    if item.get("id") != LOCAL_DEMO_CV_ID or item is row
                ]
            return row
        seed_path = _local_demo_seed_cv_path()
        row = {
            "id": LOCAL_DEMO_CV_ID,
            "owner_id": local_demo_store.db().owner_id,
            "type": "cv",
            "slug": "local-demo-cv",
            "name": f"Local {seed_path.stem.replace('.', ' ').title()}",
            "yaml_blob": seed_path.read_text(encoding="utf-8"),
            "job_id": None,
            "created_at": None,
            "updated_at": None,
            "last_used_at": None,
        }
        table.rows.append(row)
        return row


def _local_demo_cv_row() -> dict:
    with local_demo_store.locked():
        store = _local_demo_cv_store()
        src = str(store.get("yaml_blob") or "")
        appearance = _cv_appearance_from_yaml(src)
        return {
            "id": LOCAL_DEMO_CV_ID,
            "name": store.get("name") or f"Local {_local_demo_seed_cv_path().stem.replace('.', ' ').title()}",
            "slug": "local-demo-cv",
            "yaml_blob": src,
            "job_id": None,
            "created_at": store.get("created_at"),
            "updated_at": store.get("updated_at"),
            "last_used_at": store.get("last_used_at"),
            **appearance,
        }


def _save_local_demo_cv(src: str) -> dict:
    with local_demo_store.locked():
        store = _local_demo_cv_store()
        updated_at = _now_iso()
        store["yaml_blob"] = src
        store["updated_at"] = updated_at
        store["last_used_at"] = updated_at
        return _local_demo_cv_row()


# Let the service layer (tailor/ats-score source-CV loading) reach the seeded
# demo CV without a circular import (plan 107, F19).
local_demo_store.register_demo_cv_provider(_local_demo_cv_row)


def _can_use_local_demo_cv_fallback() -> bool:
    if not _can_use_demo_read_fallback():
        return False
    try:
        return not _account_deletion_pending()
    except Exception:
        return True


def _can_use_demo_read_fallback() -> bool:
    return demo_mode_enabled() and request.method == "GET"


def _can_use_demo_write_fallback(doc_id: str | None) -> bool:
    if not demo_mode_enabled() or request.method != "POST":
        return False
    if doc_id not in (None, LOCAL_DEMO_CV_ID):
        return False
    try:
        return not _account_deletion_pending()
    except Exception:
        return True


def _local_demo_document_items(type_filter: str = "all") -> list[dict]:
    row = _local_demo_cv_row()
    items = []
    if type_filter in ("cv", "all"):
        items.append(_document_summary(row, "cv"))
    return items


def _document_summary(
    row: dict,
    doc_type: str,
    *,
    job_title: str | None = None,
    company: str = "",
    role: str = "",
    packet_lineage: dict | None = None,
) -> dict:
    """Return the mixed-document catalog contract without exposing document bodies."""
    name = row.get("name") or ("Cover letter" if doc_type == "cover_letter" else "Untitled CV")
    doc_id = row["id"]
    job_id = row.get("job_id")
    open_url = (
        f"/cover-letters?id={doc_id}"
        if doc_type == "cover_letter"
        else f"/app?id={doc_id}"
    )
    actions = (
        ["open", "build_pdf", "rename", "delete"]
        if doc_type == "cover_letter"
        else ["open", "build_pdf", "rename", "duplicate", "delete", "download_yaml"]
    )
    if job_id and job_title:
        provenance = f"Linked to job: {job_title}"
    elif job_id:
        provenance = "Linked to job"
    elif doc_type == "cover_letter" and (company or role):
        target = " - ".join(part for part in (company, role) if part)
        provenance = f"Cover letter target: {target}"
    else:
        provenance = "Standalone document"
    out = {
        "id": doc_id,
        "type": doc_type,
        "name": name,
        "title": name,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_used_at": row.get("last_used_at"),
        "job_id": job_id,
        "job_title": job_title,
        "job_label": job_title,
        "provenance_summary": provenance,
        "open_url": open_url,
        "actions": actions,
    }
    if packet_lineage:
        out["packet_lineage"] = packet_lineage
    return out


def _summary_text(value: object) -> str:
    if isinstance(value, str):
        return " ".join(value.split())[:160]
    return ""


def _seed_cv(name: str = FIRST_RUN_TEMPLATE_CV_NAME) -> dict:
    """Insert a template CV for the current user and return the new row.

    Runs under the user's JWT so the INSERT satisfies the RLS WITH CHECK
    (``owner_id = auth.uid()``). ``owner_id`` defaults to ``auth.uid()`` in
    the schema, so we don't (and must not) set it from the request body. In
    the local demo shell the insert lands in the in-memory demo store.
    """
    yaml_blob = _skeleton_cv_yaml()
    appearance = _cv_appearance_from_yaml(yaml_blob)
    document_appearance = {
        key: value for key, value in appearance.items()
        if key in {"accent", "font", "density"}
    }
    row = cl_service.insert_document(
        "cv",
        name,
        yaml_blob,
        accent=document_appearance.get("accent"),
        font=document_appearance.get("font"),
        density=document_appearance.get("density"),
    )
    _mark_first_cv_seen()
    return row


def _rpc_row(data: object) -> dict | None:
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def _mark_first_cv_seen() -> None:
    """Persist that this account has crossed first-CV onboarding.

    Idempotent and SECURITY DEFINER-backed so regular users do not need direct
    UPDATE privilege on the marker column. This marker is secondary state: a
    stale local schema cache must not make an otherwise readable/writable CV
    document fall back to the pseudo demo row.
    """
    global _FIRST_CV_MARKER_RPC_MISSING_LOGGED
    if local_demo_store.active():
        # The process-local demo store has no onboarding RPC, and its reserved
        # token must never be sent to the configured Supabase project.
        return
    try:
        auth_mod.user_supabase().rpc("mark_first_cv_seen", {}).execute()
    except Exception as exc:  # noqa: BLE001 - do not break document flows on marker drift
        text = str(exc)
        if "mark_first_cv_seen" in text and ("PGRST202" in text or "Could not find the function" in text):
            if not _FIRST_CV_MARKER_RPC_MISSING_LOGGED:
                app.logger.info(
                    "first-CV marker RPC is not available; apply supabase/migrations/0008_first_run_template.sql"
                )
                _FIRST_CV_MARKER_RPC_MISSING_LOGGED = True
            return
        app.logger.warning("could not mark first CV seen: %s", exc)


def _account_deletion_pending(*, strict: bool = False) -> bool:
    """Whether the caller has requested account deletion and is in the grace window."""
    owner_id = getattr(g, "owner_id", "") or ""
    if not owner_id:
        return False
    if local_demo_store.active():
        # Demo deletion is explicitly non-persistent and client-side only;
        # there is no real account row to inspect. Never leak this passive
        # shell check to Supabase with the reserved demo token.
        return False
    try:
        resp = (
            auth_mod.user_supabase()
            .table("profiles")
            .select("deletion_requested_at")
            .eq("id", owner_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - strict privacy paths fail closed
        app.logger.warning("could not check account deletion state: %s", exc)
        if strict:
            raise auth_mod.AuthError("Account state is temporarily unavailable.", 503) from exc
        return False
    rows = resp.data or []
    if strict and len(rows) != 1:
        raise auth_mod.AuthError("Account state is unavailable.", 503)
    return bool(rows and rows[0].get("deletion_requested_at") is not None)


def _ensure_document_writes_allowed() -> None:
    """Block new visible document writes while account erasure is pending."""
    if _account_deletion_pending():
        raise _Conflict("account deletion is pending")


def _ensure_account_mutations_allowed() -> None:
    """Block account-level mutations while account erasure is pending."""
    if _account_deletion_pending():
        raise _Conflict("account deletion is pending")


def _ensure_account_access_strict() -> None:
    """Fail closed when privacy-sensitive account state is absent or unreadable."""
    if _account_deletion_pending(strict=True):
        raise _Conflict("account deletion is pending")


def _can_use_demo_fallback_when_not_pending() -> bool:
    if not _can_use_demo_read_fallback():
        return False
    try:
        return not _account_deletion_pending()
    except Exception:
        return True


def _seed_first_cv_if_allowed() -> dict | None:
    """One-time first-login template seed.

    The database function locks the profile row and refuses to seed when the
    user already has a live CV, has already received/deleted the first template,
    or has pending account deletion.
    """
    yaml_blob = _skeleton_cv_yaml()
    appearance = _cv_appearance_from_yaml(yaml_blob)
    resp = auth_mod.user_supabase().rpc(
        "seed_first_cv_template",
        {
            "p_name": FIRST_RUN_TEMPLATE_CV_NAME,
            "p_yaml_blob": yaml_blob,
            "p_accent": appearance.get("accent"),
            "p_font": appearance.get("font"),
            "p_density": appearance.get("density"),
        },
    ).execute()
    row = _rpc_row(resp.data)
    if row:
        # Activation signal: a brand-new account's first seeded CV ≈ signup.
        # No-op unless POSTHOG_KEY is set; best-effort, never blocks/raises.
        analytics.capture(analytics.EVENT_SIGNUP, getattr(g, "owner_id", "") or "")
    return row


def _resolve_active_cv(doc_id: str | None) -> dict:
    """Return the CV the request is addressing, seeding one if needed.

    * ``doc_id`` given  → that exact row (404 via :class:`_NotFound` if the
      caller doesn't own it / it doesn't exist).
    * ``doc_id`` absent → the most-recently-updated CV; if the user owns
      none yet, a template is seeded only if the user has no CV history. If
      the user intentionally deleted their last CV, the empty library is
      preserved instead of silently recreating the template.
    """
    try:
        if doc_id is not None:
            if not _is_uuid(doc_id):
                raise _BadRequest("invalid document id")
            row = _cv_row_by_id(doc_id)
            if row is None:
                raise _NotFound("CV not found")
            return row
        row = _most_recent_cv_row()
        if row:
            _mark_first_cv_seen()
            return row
        seeded = _seed_first_cv_if_allowed()
        if seeded:
            return seeded
        raise _NotFound("No active CVs")
    except _BadRequest:
        raise
    except _NotFound:
        if (doc_id is None or doc_id == LOCAL_DEMO_CV_ID) and _can_use_local_demo_cv_fallback():
            return _local_demo_cv_row()
        raise
    except Exception as exc:  # noqa: BLE001 — demo/local shell should stay readable offline
        if _can_use_local_demo_cv_fallback():
            app.logger.warning("using local demo CV fallback: %s", exc)
            return _local_demo_cv_row()
        raise


def _json_object() -> dict:
    """Parse the request JSON body as an OBJECT, or 400.

    ``request.get_json(silent=True) or {}`` silently lets a non-object body
    through — a JSON array/string/number is truthy, so it reaches ``.get()``
    and raises ``AttributeError`` → HTTP 500. A malformed body is a client
    error: absent/None coerces to ``{}`` (routes treat missing fields
    themselves), but a non-object is rejected with 400.
    """
    body = request.get_json(silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise _BadRequest("expected a JSON object")
    return body


def _doc_id_from_request() -> str | None:
    """Pull an optional document id from the query string or JSON body.

    Both ``/api/cv?id=...`` (GET) and ``{"id": ...}`` (POST) are accepted so
    the frontend can address a specific CV either way.
    """
    qs_id = request.args.get("id")
    if qs_id:
        return qs_id
    if request.method in ("POST", "PUT", "PATCH"):
        payload = _json_object()
        body_id = payload.get("id")
        if body_id:
            return str(body_id)
    return None


# Subdirectories (relative to REPO) the auth-exempt /asset and /repo routes may
# serve. These hold the assets the renderer/editor actually fetch over HTTP:
#   design/fonts/*  — @font-face url() in the preview CSS
#   design/photos/* — per-owner uploaded CV photos (/repo/design/photos/...)
# Application source (*.py), .env, configs, plans, themes JSON (read server-
# side via /api/themes — never served raw) are all OUTSIDE this set, so they
# can NEVER be disclosed through these routes. Resolved against the CURRENT
# ``REPO`` at call time (tests monkeypatch REPO to a temp tree).
_ASSET_SUBDIRS: tuple[str, ...] = ("design",)


def _asset_roots() -> tuple[Path, ...]:
    """The allowed asset roots, resolved against the current ``REPO``."""
    return tuple((REPO / sub).resolve() for sub in _ASSET_SUBDIRS)


def _safe_repo_path(rel: str) -> Path:
    """Resolve a request-supplied path to a file under an allowed asset dir.

    Hardened against two prior holes:

      1. **Absolute-path embedding.** The old code did ``Path("/" +
         rel.lstrip("/")).resolve()`` and only checked ``relative_to(REPO)``.
         With REPO=/app, ``rel="app/tools/editor/server.py"`` resolved to
         ``/app/tools/editor/server.py`` — inside REPO — so it passed and
         leaked source. We now resolve ``rel`` RELATIVE to REPO and additionally
         require the result to live under :data:`_ASSET_ROOTS`.
      2. **Over-broad scope.** Even within REPO, only the asset dirs are
         serveable; *.py / .env / configs / plans are never reachable.

    render_preview now rewrites ``file://<REPO>/design/...`` → REPO-RELATIVE
    ``/asset/design/...`` (no absolute prefix, no REPO-basename segment), which
    is identical in local dev and in the container and resolves directly under
    ``REPO/design`` here. For BACKWARD COMPATIBILITY we also tolerate two legacy
    forms a cached client might still send: a leading ``<repo-basename>/``
    segment (the container's old ``app/design/...`` CSS form) and a leading
    absolute REPO path (the old local-dev ``<abs-repo-path>/design/...``
    form). Both are stripped to ``design/...`` before resolving. ``..``
    traversal and absolute paths can never escape the asset roots because the
    FINAL resolved path is checked against them — that scope check, not the
    prefix-stripping, is the security backstop. A disallowed path → 404 (no
    signal whether the file exists).
    """
    raw = (rel or "").strip()
    # Reject absolute paths and explicit traversal outright — they have no
    # legitimate use for an asset URL and only show up in attacks.
    if raw.startswith("/") or raw.startswith("\\"):
        abort(404)
    cleaned = raw.lstrip("/")
    # Legacy form A: a cached client may still send the full absolute REPO path
    # (e.g. "home/user/checkout/design/...") from the OLD rewrite. Strip
    # that exact prefix so it maps to "design/..." (the scope check below is
    # still the real guard).
    repo_abs_prefix = str(REPO).lstrip("/") + "/"
    if cleaned.startswith(repo_abs_prefix):
        cleaned = cleaned[len(repo_abs_prefix):]
    # Legacy form B: the container's old CSS prefixed just the REPO basename
    # (e.g. "app/design/..."); strip exactly one leading occurrence.
    repo_base = REPO.name + "/"
    if cleaned.startswith(repo_base):
        cleaned = cleaned[len(repo_base):]
    if not cleaned:
        abort(404)
    candidate = (REPO / cleaned).resolve()
    # Per-owner CV photos are PRIVATE: they are served ONLY via the
    # authenticated ``/api/asset/photo/<filename>`` route, never through the
    # auth-exempt ``/asset`` / ``/repo`` asset routes. Enforce the block on the
    # RESOLVED, CASEFOLDED path — a raw-string check on ``cleaned`` is bypassable
    # by a dot-segment (``design/foo/../photos/…`` collapses in resolve() on
    # every filesystem, incl. Linux prod) or a case variant (``design/Photos/…``
    # opens the real file on a case-insensitive FS such as macOS/Windows). The
    # owner dirs are lowercase UUIDs and ``design/photos`` is a fixed lowercase
    # literal, so casefolded containment has no false positives (fonts/icons
    # under design/ are unaffected — they are not under design/photos/).
    photos_root = str((REPO / "design" / "photos").resolve()).casefold()
    cand_cf = str(candidate).casefold()
    if cand_cf == photos_root or cand_cf.startswith(photos_root + os.sep):
        abort(404)
    # The resolved path MUST live under one of the allowed asset roots. This is
    # what blocks "app/tools/editor/server.py", "app/.env", "../etc/passwd",
    # and any ".." that climbs out of design/.
    for root in _asset_roots():
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return candidate
    abort(404)


@app.route("/")
def index() -> object:
    """Public landing page (logged-OUT front door).

    Marketing page with the value prop + a "Get started" CTA to ``/app``.
    Auth-exempt and install-free. A small inline script (landing.js) checks
    for an existing Supabase session and redirects to ``/app`` so a logged-in
    visitor lands in the editor, not on marketing. The ``PUBLIC_BASE_URL``
    token in the HTML is filled here so canonical/og:url reflect the real
    origin (SEO).
    """
    legacy_id = request.args.get("id")
    if legacy_id:
        return redirect(f"/app?id={legacy_id}", code=302)
    html = (STATIC_DIR / "landing.html").read_text(encoding="utf-8")
    html = html.replace("__PUBLIC_BASE_URL__", PUBLIC_BASE_URL)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/app")
def app_editor() -> object:
    """The editor application (auth-gated client-side by auth.js).

    Served at a stable path so ``/`` can be the public landing page. The
    editor's HTML/JS is unchanged; auth.js still shows the login view until a
    Supabase session exists and patches fetch so no ``/api/*`` call goes out
    without a bearer token. All ``/api/*`` routes remain auth-required.
    """
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/robots.txt")
def robots_txt() -> object:
    """SEO: allow crawling, point at the sitemap. Auth-exempt, static."""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        # The editor app + per-user API are not useful to crawlers.
        "Disallow: /app\n"
        "Disallow: /api/\n"
        f"Sitemap: {PUBLIC_BASE_URL}/sitemap.xml\n"
    )
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/sitemap.xml")
def sitemap_xml() -> object:
    """SEO: minimal sitemap listing the public landing page. Auth-exempt."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url>\n    <loc>{PUBLIC_BASE_URL}/</loc>\n"
        "    <changefreq>weekly</changefreq>\n    <priority>1.0</priority>\n  </url>\n"
        "</urlset>\n"
    )
    return body, 200, {"Content-Type": "application/xml; charset=utf-8"}


@app.route("/api/i18n")
def api_i18n() -> object:
    """Resolved i18n string table for a language (EN default, EN-fallback).

    Auth-exempt: the landing page (pre-session) and the editor both fetch
    this. ``?lang=en|de`` selects the language; anything else resolves to EN.
    """
    lang = i18n.normalize_lang(request.args.get("lang"))
    return jsonify({"lang": lang, "supported": list(i18n.SUPPORTED), "strings": i18n.resolve(lang)})


@app.route("/api/analytics/event", methods=["POST"])
def api_analytics_event() -> object:
    """Record a client-side activation event (no-op unless POSTHOG_KEY set).

    The browser posts ``{"event": "..."}`` for the two activation moments it
    owns (e.g. ``first_export`` after a successful build). The event is keyed
    to the verified ``g.owner_id`` server-side — never a body-supplied id — so
    no caller can spoof another user's analytics. Auth-required (it's under
    ``/api/``). Always returns 200; analytics must never break a flow.
    """
    payload = request.get_json(silent=True) or {}
    event = (payload.get("event") or "").strip()
    allowed = {analytics.EVENT_SIGNUP, analytics.EVENT_FIRST_EXPORT}
    if event in allowed:
        analytics.capture(event, getattr(g, "owner_id", "") or "", {"source": "client"})
    return jsonify({"ok": True})


@app.route("/api/cv", methods=["GET", "POST"])
def api_cv() -> object:
    """Read or write ONE of the user's CVs (Supabase, RLS-scoped).

    An optional ``id`` (``?id=`` on GET, ``{"id": ...}`` in the POST body)
    selects which CV. With no id we default to the most-recently-updated
    CV, seeding a skeleton if the user owns none (Stage 2 first-run).

    GET  → returns the selected CV's ``yaml_blob`` as ``text/plain``.
    POST → upserts that CV's ``yaml_blob`` from ``{content: <yaml text>}``
           (frontend contract unchanged). The YAML is size-capped (512 KB)
           and shape-validated before storage; ``updated_at`` is bumped so
           the document picker re-sorts.

    A non-UUID id → 400; an id the caller doesn't own → 404 (RLS hides it).
    """
    if request.method == "POST":
        _ensure_document_writes_allowed()
    doc_id = _doc_id_from_request()
    payload = _json_object() if request.method == "POST" else {}
    src = None
    if request.method == "POST":
        src = payload.get("content")
        if src is None or not isinstance(src, str):
            return jsonify({"error": "missing 'content' string"}), 400
        if len(src.encode("utf-8")) > MAX_CV_YAML_BYTES:
            return (
                jsonify({"error": "CV too large", "max_bytes": MAX_CV_YAML_BYTES}),
                413,
            )
        try:
            _validate_cv_yaml(src)
        except Exception as exc:  # noqa: BLE001 — surface a clear message to the UI
            return jsonify({"error": f"YAML invalid: {exc}"}), 400
    try:
        row = _resolve_active_cv(doc_id)
    except Exception as exc:
        if _can_use_demo_write_fallback(doc_id):
            app.logger.warning("using local demo CV write fallback: %s", exc)
            row = _local_demo_cv_row()
        else:
            raise

    if request.method == "GET":
        if row.get("id") and row.get("id") != LOCAL_DEMO_CV_ID:
            cl_service.mark_document_used(row["id"], "cv", now_iso=_now_iso())
        return (
            row.get("yaml_blob") or "",
            200,
            {
                "Content-Type": "text/plain; charset=utf-8",
                "X-OnePager-Revision": str(row.get("updated_at") or ""),
            },
        )

    expected_revision = payload.get("revision")
    if expected_revision is not None and str(expected_revision) != str(row.get("updated_at") or ""):
        if src == (row.get("yaml_blob") or ""):
            return jsonify(
                {
                    "saved": True,
                    "bytes": len(src.encode("utf-8")),
                    "id": row["id"],
                    "updated_at": row.get("updated_at"),
                }
            )
        return jsonify({"error": "save conflict", "code": "revision_conflict"}), 409

    appearance = _cv_appearance_from_yaml(src)
    document_appearance = {
        key: value for key, value in appearance.items()
        if key in {"accent", "font", "density"}
    }
    if row.get("id") == LOCAL_DEMO_CV_ID and _can_use_demo_write_fallback(doc_id):
        saved_row = _save_local_demo_cv(src)
        return (
            jsonify(
                {
                    "saved": True,
                    "bytes": len(src.encode("utf-8")),
                    "id": saved_row["id"],
                    "updated_at": saved_row.get("updated_at"),
                    "storage": "local-demo-memory",
                }
            ),
            200,
            {"X-OnePager-Demo-Write": "server-process-memory"},
        )
    updated_at = _now_iso()
    if local_demo_store.active():
        saved_rows = (
            _document_store()
            .table("documents")
            .update({"yaml_blob": src, "updated_at": updated_at, **document_appearance})
            .eq("id", row["id"])
            .eq("type", "cv")
            .is_("deleted_at", "null")
            .execute()
            .data
            or []
        )
        if not saved_rows:
            raise _NotFound("CV not found")
        return (
            jsonify(
                {
                    "saved": True,
                    "bytes": len(src.encode("utf-8")),
                    "id": row["id"],
                    "updated_at": saved_rows[0].get("updated_at"),
                    "storage": "local-demo-memory",
                }
            ),
            200,
            {"X-OnePager-Demo-Write": "server-process-memory"},
        )
    try:
        saved = pdf_store.update_document_with_artifact_detach(
            document_id=row["id"],
            document_type="cv",
            expected_revision=(
                expected_revision
                if expected_revision is not None
                else row.get("updated_at")
            ),
            yaml_blob=src,
            updated_at=updated_at,
            accent=document_appearance.get("accent"),
            font=document_appearance.get("font"),
            density=document_appearance.get("density"),
        )
    except pdf_store.ArtifactAccountUnavailable:
        return jsonify({"error": "Account is unavailable.", "code": "account_unavailable"}), 409
    except pdf_store.StorageError as exc:
        app.logger.warning("atomic CV save failed: %s", type(exc).__name__)
        return (
            jsonify(
                {
                    "error": "CV storage is temporarily unavailable.",
                    "code": "document_save_unavailable",
                }
            ),
            503,
        )
    if saved.get("status") == "not_found":
        raise _NotFound("CV not found")
    if saved.get("status") == "revision_conflict":
        latest = _cv_row_by_id(row["id"]) or row
        latest_appearance = {
            key: latest.get(key) for key in ("accent", "font", "density")
        }
        if (
            src == (latest.get("yaml_blob") or "")
            and latest_appearance == document_appearance
        ):
            return jsonify(
                {
                    "saved": True,
                    "bytes": len(src.encode("utf-8")),
                    "id": row["id"],
                    "updated_at": latest.get("updated_at"),
                }
            )
        return (
            jsonify(
                {
                    "error": "save conflict",
                    "code": "revision_conflict",
                    "updated_at": latest.get("updated_at"),
                }
            ),
            409,
        )
    detached = saved.get("artifacts") or []
    if detached:
        try:
            pdf_store.delete_detached_artifacts(detached, strict=False)
        except Exception as exc:  # noqa: BLE001 - save is already committed
            app.logger.warning("deferred CV PDF cleanup: %s", type(exc).__name__)
    return jsonify(
        {
            "saved": True,
            "bytes": len(src.encode("utf-8")),
            "id": row["id"],
            "updated_at": saved.get("document_revision") or updated_at,
            "cleanup_pending": bool(detached),
        }
    )


def _artifact_cleanup_applies() -> bool:
    """Whether derived-PDF cleanup has anything it could possibly delete.

    The ephemeral demo shell writes documents into server-process memory and
    never reaches Supabase or R2, so no ``pdf_artifacts`` row and no stored
    object can exist for the demo owner. Running the cleanup there would send
    the demo's ``alg:none`` token at a real project, and the resulting
    transport error would fail the mutation closed for a document that has no
    derived artifact in the first place. Every real tenant keeps the strict
    path.
    """
    return not local_demo_store.active()


def _delete_document_pdf_artifacts(document_id: str) -> int:
    """Delete derived PDFs before mutating or deleting their source document."""
    if not _artifact_cleanup_applies():
        return 0
    try:
        return pdf_store.delete_document_artifacts(document_id)
    except Exception as exc:  # noqa: BLE001 — mutation must fail closed
        app.logger.warning(
            "PDF artifact cleanup blocked document mutation: %s",
            type(exc).__name__,
        )
        raise _ArtifactCleanupUnavailable from exc


def _delete_detached_document_pdf_artifacts(rows: list[dict[str, Any]]) -> int:
    """Delete objects captured by the atomic DB delete, never before it."""
    if not rows or not _artifact_cleanup_applies():
        return 0
    try:
        return pdf_store.delete_detached_artifacts(rows)
    except Exception as exc:  # noqa: BLE001 - DB delete already committed
        app.logger.warning(
            "PDF object cleanup failed after document deletion: %s",
            type(exc).__name__,
        )
        raise _ArtifactCleanupUnavailable(document_deleted=True) from exc


# Library reads are a hot path, and draining the outbox costs one R2 round-trip
# per row. The dedicated pdf-cleanup-worker owns the durable queue; this in-
# request drain is only the fallback for deployments that do not run it, so it
# is throttled per process rather than firing on every list request.
_PENDING_CLEANUP_MIN_INTERVAL_S = 60.0
_PENDING_CLEANUP_BATCH = 3
_last_pending_cleanup_at = 0.0


def _reset_pending_cleanup_throttle() -> None:
    """Allow the next library read to drain the outbox immediately.

    The throttle is process-global by design, which makes it order-dependent
    under a test session sharing one imported module. Tests reset it so the
    drain contract is exercised deterministically rather than depending on
    which test happened to consume the window first.
    """
    global _last_pending_cleanup_at
    _last_pending_cleanup_at = 0.0


def _retry_pending_pdf_artifacts_best_effort() -> int:
    """Opportunistically consume a small durable cleanup batch on library reads.

    Bounded twice over: at most ``_PENDING_CLEANUP_BATCH`` objects, and at most
    once every ``_PENDING_CLEANUP_MIN_INTERVAL_S`` seconds per process. Before
    the throttle, every ``GET /api/cvs`` and ``GET /api/cover-letters`` could
    block on up to ten sequential object deletes, so a slow or failing R2 put
    that latency straight onto the user's library view.
    """
    global _last_pending_cleanup_at
    if not _artifact_cleanup_applies():
        return 0
    now = time.monotonic()
    if now - _last_pending_cleanup_at < _PENDING_CLEANUP_MIN_INTERVAL_S:
        return 0
    # Stamp before the attempt: a failing backend must not be retried on every
    # subsequent read just because it never reached the success path.
    _last_pending_cleanup_at = now
    try:
        return pdf_store.retry_pending_artifact_cleanup(
            limit=_PENDING_CLEANUP_BATCH
        )
    except Exception as exc:  # noqa: BLE001 - queue remains durable for retry
        app.logger.warning(
            "Pending PDF object cleanup deferred: %s",
            type(exc).__name__,
        )
        return 0


def _delete_owner_cv_pdf_artifacts(*, force: bool = False) -> int:
    """Atomically detach every owner CV PDF before shared-photo mutation."""
    if not _artifact_cleanup_applies():
        return 0
    try:
        return pdf_store.delete_owner_cv_artifacts(force=force)
    except Exception as exc:  # noqa: BLE001 — mutation must fail closed
        app.logger.warning(
            "Owner-wide PDF artifact cleanup blocked photo mutation: %s",
            type(exc).__name__,
        )
        raise _ArtifactCleanupUnavailable from exc


def _now_iso() -> str:
    """UTC timestamp for ``updated_at`` bumps.

    The DB default only fills ``updated_at`` on INSERT; without an explicit
    write on UPDATE the document-picker sort would never change. Set it
    here so an edited CV jumps to the top of the list.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _validate_cv_yaml(src: str) -> None:
    """Validate raw CV YAML text via the engine's content validator.

    Writes to a temp file because ``content.load`` takes a path; the
    renderer/validator is left byte-identical (no engine change).

    Additionally guards the ``photo:`` field. Inline ``data:`` images are
    decoded + validated (type / size / decoded-dimension caps). Non-data photos
    must be the caller's own uploaded ``design/photos/<owner>/photo.*`` path;
    arbitrary URLs, absolute paths, ``file://`` values, and traversal are
    rejected before WeasyPrint can fetch them.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=True, encoding="utf-8"
    ) as tmp:
        tmp.write(src)
        tmp.flush()
        # Lenient on per-item required fields: half-finished form entries
        # must autosave (the renderers skip incomplete items). Structure,
        # types, and top-level required fields are still enforced.
        content_mod.load(tmp.name, strict_required=False)

    # Reject a bomb-shaped / SVG inline data: URI photo before it can persist.
    try:
        parsed = yaml_safe_load(src)
    except Exception:  # noqa: BLE001 — content.load above already validated YAML
        return
    if isinstance(parsed, dict):
        photo = parsed.get("photo")
        if isinstance(photo, str):
            _validate_cv_photo_value(photo)


def _validate_cv_photo_value(photo: str) -> None:
    value = (photo or "").strip()
    if not value:
        return
    if image_guard.is_data_uri(value):
        image_guard.validate_data_uri_image(value)  # ImageRejected → caller 400
        return
    normalized = value.replace("\\", "/").lstrip("/")
    if normalized.startswith("repo/"):
        normalized = normalized[len("repo/"):]
    if normalized.startswith("/repo/"):
        normalized = normalized[len("/repo/"):]
    if "://" in normalized or normalized.startswith(("/", "\\")) or ".." in normalized.split("/"):
        raise image_guard.ImageRejected("photo must be an uploaded owner photo or a validated data image")
    owner_dir = _owner_photo_dir()
    try:
        allowed_prefix = str(owner_dir.relative_to(REPO)).replace("\\", "/") + "/"
    except ValueError:
        raise image_guard.ImageRejected("invalid photo path")
    if not normalized.startswith(allowed_prefix):
        raise image_guard.ImageRejected("photo must be an uploaded owner photo")
    name = Path(normalized).name
    if name not in {"photo.png", "photo.jpg", "photo.jpeg", "photo.webp"}:
        raise image_guard.ImageRejected("photo must be an uploaded owner photo")


def _cv_appearance_from_yaml(src: str) -> dict[str, str]:
    """Extract normalized appearance metadata from already-validated CV YAML."""
    parsed = yaml_safe_load(src)
    if not isinstance(parsed, dict):
        return {"accent": "#111111", "font": "serif", "density": "normal", "template": "classic", "paper": "white"}
    accent = parsed.get("accent") if isinstance(parsed.get("accent"), str) else ""
    font = (
        (parsed.get("font") or "serif").strip().lower()
        if isinstance(parsed.get("font"), str)
        else "serif"
    )
    density = (
        (parsed.get("density") or "normal").strip().lower()
        if isinstance(parsed.get("density"), str)
        else "normal"
    )
    paper = (
        (parsed.get("paper") or "white").strip().lower()
        if isinstance(parsed.get("paper"), str)
        else "white"
    )
    template = (
        (parsed.get("template") or "classic").strip().lower()
        if isinstance(parsed.get("template"), str)
        else "classic"
    )
    if font not in {"serif", "sans", "mono"}:
        font = "serif"
    if density not in {"tight", "normal", "airy"}:
        density = "normal"
    template = template_registry.normalize_template(template)
    try:
        paper = css_base.normalize_paper(paper)
    except ValueError:
        paper = "white"
    return {
        "accent": accent.strip() or "#111111",
        "font": font,
        "density": density,
        "template": template,
        "paper": paper,
    }


def _effective_density_from_yaml(src: str) -> str:
    """Return the YAML density default used when an HTTP caller omits density."""
    try:
        appearance = _cv_appearance_from_yaml(src)
    except Exception:  # noqa: BLE001
        return "normal"
    return appearance.get("density") or "normal"


def _effective_template_from_yaml(src: str) -> str:
    """Return the YAML template default used when an HTTP caller omits it."""
    try:
        appearance = _cv_appearance_from_yaml(src)
    except Exception:  # noqa: BLE001
        return "classic"
    return template_registry.normalize_template(appearance.get("template"))


def _effective_paper_from_yaml(src: str) -> str:
    appearance = _cv_appearance_from_yaml(src)
    try:
        return css_base.normalize_paper(appearance.get("paper"))
    except ValueError:
        return "white"


def _materialize_cv_to_path(yaml_text: str) -> Path:
    """Write the user's CV YAML to a temp file and return its path.

    The render engine (``render_cv_html`` / ``content.load``) takes a
    *path*; rather than touch the renderer we hand it a per-request temp
    file holding the DB-sourced YAML. Caller is responsible for cleanup.
    """
    fd = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    try:
        fd.write(yaml_text or "")
        fd.flush()
    finally:
        fd.close()
    return Path(fd.name)


@app.route("/api/cvs", methods=["GET", "POST"])
def api_cvs() -> object:
    """List or create the user's CVs (Supabase, RLS-scoped).

    GET → ``{cvs: [{id, name, slug, updated_at}], limit, count, at_limit}``
          ordered by ``updated_at`` desc. ``limit``/``count``/``at_limit``
          surface the free-tier soft cap for the UI (Stage 3 does NOT block
          on it — Stage 11 wires real plan enforcement). If this is the user's
          first CV visit, one template CV is seeded. If they deleted their last
          CV, the list remains empty.

    POST → create a new CV (optional ``{name}``), seeded with the template.
           Returns ``{id, name}``. Creation is intentionally NOT blocked at
           the limit yet (the flag above is advisory).
    """
    if request.method == "POST":
        _ensure_document_writes_allowed()
        payload = _json_object()
        name = _clean_cv_name(payload.get("name")) or "Untitled CV"
        row = _seed_cv(name=name)
        return jsonify({"id": row["id"], "name": row.get("name") or name}), 201

    _retry_pending_pdf_artifacts_best_effort()
    try:
        rows = _list_cv_rows()
        if not rows:
            # First login only: seed a template so the picker has something
            # real to show. A prior template/deletion marker means the user
            # intentionally emptied the library; do not recreate the template.
            seeded = _seed_first_cv_if_allowed()
            rows = [seeded] if seeded else []
        else:
            _mark_first_cv_seen()
    except Exception as exc:  # noqa: BLE001 — demo/local shell should stay readable offline
        if not _can_use_local_demo_cv_fallback():
            raise
        app.logger.warning("using local demo CV list fallback: %s", exc)
        rows = [_local_demo_cv_row()]
    # Resolve the display title of each CV's linked job in ONE grouped, RLS-scoped
    # query (Stage 7 live ATS badge): a CV tailored to a saved job carries
    # ``documents.job_id``; the editor topbar shows that job's ATS fit. Absent /
    # deleted jobs simply drop out of the map (job_title → None).
    job_ids = [r.get("job_id") for r in rows if r.get("job_id")]
    try:
        titles = cl_service.job_titles_for(job_ids) if job_ids else {}
    except Exception as exc:  # noqa: BLE001 — passive title enrichment must not break listing
        if not _can_use_demo_read_fallback():
            raise
        app.logger.warning("using empty demo CV job-title fallback: %s", exc)
        titles = {}
    doc_ids = [r["id"] for r in rows if r.get("id")]
    try:
        packet_lineages = jobs_svc.packet_lineage_for_documents(doc_ids)
    except Exception as exc:  # noqa: BLE001 — passive lineage must not break CV list
        if not _can_use_demo_read_fallback():
            raise
        app.logger.warning("using empty demo CV packet-lineage fallback: %s", exc)
        packet_lineages = {}
    out = []
    for r in rows:
        item = {
            "id": r["id"],
            "name": r.get("name") or "Untitled CV",
            "slug": r.get("slug"),
            "job_id": r.get("job_id"),
            "job_title": titles.get(r.get("job_id")) if r.get("job_id") else None,
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
            "last_used_at": r.get("last_used_at"),
        }
        lineage = packet_lineages.get(r["id"])
        if lineage:
            item["packet_lineage"] = lineage
        out.append(item)
    count = len(out)
    return jsonify(
        {
            "cvs": out,
            "limit": FREE_CV_LIMIT,
            "count": count,
            "at_limit": count >= FREE_CV_LIMIT,
        }
    )


def _clean_cv_name(raw: object) -> str:
    """Normalise a user-supplied CV name: strip, collapse, length-cap.

    Returns ``""`` for empty/garbage so callers can fall back to a default.
    Never raises — names are display-only and don't feed SQL directly
    (PostgREST parameterises the value), but we still cap the length so a
    single row can't be bloated by a hostile name.
    """
    if not isinstance(raw, str):
        return ""
    name = " ".join(raw.split()).strip()
    return name[:MAX_CV_NAME_LEN]


@app.route("/api/cv/<doc_id>/duplicate", methods=["POST"])
def api_cv_duplicate(doc_id: str) -> object:
    """Duplicate a CV into a new ``"Copy of …"`` document. Returns ``{id}``.

    Copies the source ``yaml_blob`` and the three theme columns
    (accent/font/density) so the clone looks identical. Runs entirely under
    the user's JWT — the source must be owned (else 404) and the INSERT
    satisfies the RLS WITH CHECK via the ``owner_id`` default.
    """
    if not _is_uuid(doc_id):
        raise _BadRequest("invalid document id")
    _ensure_document_writes_allowed()
    src = _cv_row_by_id(doc_id)
    if src is None:
        raise _NotFound("CV not found")

    base_name = src.get("name") or "Untitled CV"
    copy_name = _clean_cv_name(f"Copy of {base_name}") or "Copy of CV"
    insert: dict[str, object] = {
        "type": "cv",
        "name": copy_name,
        "yaml_blob": src.get("yaml_blob") or "",
    }
    # Carry over theme columns when the source set them (skip nulls so the
    # DB defaults apply otherwise).
    for col in ("accent", "font", "density"):
        if src.get(col) is not None:
            insert[col] = src[col]

    row = cl_service.insert_document(
        "cv",
        copy_name,
        str(insert["yaml_blob"]),
        accent=insert.get("accent"),
        font=insert.get("font"),
        density=insert.get("density"),
    )
    return jsonify({"id": row["id"], "name": row.get("name") or copy_name}), 201


@app.route("/api/cv/<doc_id>/rename", methods=["POST"])
def api_cv_rename(doc_id: str) -> object:
    """Rename a CV. Body: ``{name}``. Returns ``{id, name}``.

    A blank/garbage name → 400. An id the caller doesn't own → 404 (the
    UPDATE matches zero rows under RLS, which we detect and report).
    """
    if not _is_uuid(doc_id):
        raise _BadRequest("invalid document id")
    _ensure_document_writes_allowed()
    payload = _json_object()
    name = _clean_cv_name(payload.get("name"))
    if not name:
        raise _BadRequest("missing or empty 'name'")

    # Confirm ownership first so we can 404 cleanly (an UPDATE on an
    # invisible row simply affects nothing under RLS).
    if _cv_row_by_id(doc_id) is None:
        raise _NotFound("CV not found")

    sb = _document_store()
    resp = (
        sb.table("documents")
        .update({"name": name, "updated_at": _now_iso()})
        .eq("id", doc_id)
        .eq("type", "cv")
        .execute()
    )
    if not (resp.data or []):
        # Belt-and-braces: RLS denied the write between the check and here.
        raise _NotFound("CV not found")
    return jsonify({"id": doc_id, "name": name})


@app.route("/api/cv/<doc_id>", methods=["DELETE"])
def api_cv_delete(doc_id: str) -> object:
    """Hard-delete a CV. Returns ``{deleted: id}``.

    The first-run marker, not ``documents.deleted_at``, prevents silent template
    recreation after deleting the last CV. ``deleted_at`` remains reserved for
    account-erasure grace flows. An id the caller doesn't own → 404 (RLS hides
    the row, so DELETE affects nothing and we report not-found).
    """
    if not _is_uuid(doc_id):
        raise _BadRequest("invalid document id")
    _ensure_document_writes_allowed()
    if _cv_row_by_id(doc_id) is None:
        raise _NotFound("CV not found")

    deletion = cl_service.delete_document(doc_id, "cv", include_artifacts=True)
    if not isinstance(deletion, dict):
        raise _NotFound("CV not found")
    _delete_detached_document_pdf_artifacts(deletion.get("artifacts") or [])
    return jsonify({"deleted": doc_id})


# ---------------------------------------------------------------------------
# Cover letters (Stage 5). Stored as ``documents`` rows with type
# ``cover_letter`` (RLS owner-scoped, reusing the multi-doc machinery).
# Generation is grounded in a CV + parsed JD and gated by anti-fabrication +
# cliché guards; the renderer is SEPARATE from the CV path (multi-page OK).
# ---------------------------------------------------------------------------
@app.route("/api/cover-letter/generate", methods=["POST"])
@_ai_rate_limit
def api_cover_letter_generate() -> object:
    """Generate a grounded cover letter from a CV + pasted JD.

    Body: ``{cv_id, jd_text, tone?, recipient?, company_context?, job_id?}``.

    Loads the user's CV (RLS), parses the JD into keyed JSON, builds the
    grounded prompt, resolves the key/credit via ``resolve_llm`` (BYO → managed
    → needs_key), calls the LLM, then runs the POST-GEN guards:
      * cliché scan (strips the ~15 reject phrases, reports them);
      * anti-fabrication cross-reference — any proper noun / number not in the
        CV BLOCKS the save and returns ``{error:'unverified_claims', items}``;
      * word cap (~400, hard 450).
    On success the letter is stored as a ``cover_letter`` document and returned.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be an object", "code": "invalid_cover_envelope_type"}), 400
    _ensure_document_writes_allowed()
    cv_id = payload.get("cv_id")
    if not _is_uuid(cv_id):
        return jsonify({"error": "missing or invalid 'cv_id'"}), 400
    job_id = payload.get("job_id")
    if job_id is not None and not _is_uuid(job_id):
        return jsonify({"error": "invalid 'job_id'"}), 400
    saved_job = _saved_job_for_jd(job_id) if job_id is not None else None
    jd_text = saved_job.get("jd_text") if saved_job is not None else payload.get("jd_text")
    if not isinstance(jd_text, str) or not jd_text.strip():
        return jsonify({"error": "missing 'jd_text' (paste the job description)"}), 400
    if len(jd_text.encode("utf-8")) > MAX_CV_YAML_BYTES:
        return jsonify({"error": "job description too large", "max_bytes": MAX_CV_YAML_BYTES}), 413

    # Reject malformed prompt-bound values before ownership lookup or an AI
    # call. Validation is shared with direct service callers and persistence;
    # controls are rejected rather than silently deleted.
    tone, recipient, company_context = cl_service.normalize_generation_inputs(
        tone=payload.get("tone", "auto"),
        recipient=payload.get("recipient", ""),
        company_context=payload.get("company_context", ""),
    )
    if job_id is not None:
        jobs_svc.assert_job_owned(job_id)

    # Effective provider/model: request body → profile preference → default.
    provider, model = _request_provider_model(payload)

    # CoverLetterError (404/422/502) + LLMUnavailable (402/503) propagate to
    # their registered error handlers.
    result = cl_service.generate_cover_letter(
        cv_id=cv_id,
        jd_text=jd_text,
        tone=tone,
        recipient=recipient,
        company_context=company_context,
        job_id=job_id,
        jd_title=str(saved_job.get("title") or "") if saved_job is not None else None,
        jd_company=str(saved_job.get("company") or "") if saved_job is not None else None,
        provider=provider,
        model=model,
        now_iso=_now_iso(),
    )
    if job_id is not None:
        source_cv_id = cv_id
        tailored_cv_id = None
        tailored_hash = None
        try:
            cv_lineage = jobs_svc.packet_lineage_for_document(str(cv_id))
        except Exception as exc:  # noqa: BLE001 - lineage enrichment must not hide the saved letter
            app.logger.warning("cover-letter packet source lineage lookup failed: %s", exc)
            cv_lineage = None
        if isinstance(cv_lineage, dict) and cv_lineage.get("job_id") == job_id:
            if cv_lineage.get("document_role") == "tailored_cv":
                source_cv_id = cv_lineage.get("source_cv_id") or cv_id
                tailored_cv_id = cv_lineage.get("tailored_cv_id") or cv_id
                tailored_hash = cv_lineage.get("tailored_cv_hash")
            elif cv_lineage.get("source_cv_id"):
                source_cv_id = cv_lineage.get("source_cv_id")
        try:
            jobs_svc.record_cover_letter_packet_lineage(
                job_id=job_id,
                source_cv_id=str(source_cv_id),
                tailored_cv_id=str(tailored_cv_id) if tailored_cv_id else None,
                cover_letter_id=str(result["id"]),
                provider=provider,
                model=model,
                ats=None,
                source_hash=cv_lineage.get("source_cv_hash") if isinstance(cv_lineage, dict) else None,
                job_content_hash=cv_lineage.get("job_content_hash") if isinstance(cv_lineage, dict) else None,
                tailored_hash=tailored_hash,
                score_system_id=cv_lineage.get("score_system_id") if isinstance(cv_lineage, dict) else None,
                review_id=(cv_lineage.get("cover_letter_review_id") or cv_lineage.get("review_id")) if isinstance(cv_lineage, dict) else None,
                now_iso=_now_iso(),
            )
            lineage = jobs_svc.packet_lineage_for_document(str(result["id"]))
            if lineage:
                result = {**result, "packet_lineage": lineage}
        except Exception:
            try:
                cl_service.delete_document(str(result["id"]), "cover_letter")
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve lineage failure
                app.logger.warning("cover-letter rollback after packet-lineage failure failed: %s", cleanup_exc)
            raise
    return jsonify(result), 201


@app.route("/api/cover-letters")
def api_cover_letters() -> object:
    """List the user's cover letters (RLS-scoped), newest-edited first.

    Returns ``{cover_letters: [{id, name, company, role, updated_at}], count}``.
    """
    _retry_pending_pdf_artifacts_best_effort()
    try:
        rows = cl_service.list_documents("cover_letter")
    except Exception as exc:  # noqa: BLE001 — demo/local shell should stay readable offline
        if not _can_use_demo_fallback_when_not_pending():
            if _account_deletion_pending():
                raise _Conflict("account deletion is pending")
            raise
        app.logger.warning("using empty demo cover-letter fallback: %s", exc)
        rows = []
    out = []
    job_ids = [r.get("job_id") for r in rows if r.get("job_id")]
    try:
        titles = cl_service.job_titles_for(job_ids) if job_ids else {}
    except Exception as exc:  # noqa: BLE001 — passive title enrichment must not break listing
        if not _can_use_demo_read_fallback():
            raise
        app.logger.warning("using empty demo cover-letter job-title fallback: %s", exc)
        titles = {}
    try:
        packet_lineages = jobs_svc.packet_lineage_for_documents([r["id"] for r in rows if r.get("id")])
    except Exception as exc:  # noqa: BLE001 — passive lineage must not break listing
        if not _can_use_demo_read_fallback():
            raise
        app.logger.warning("using empty demo cover-letter packet-lineage fallback: %s", exc)
        packet_lineages = {}
    for r in rows:
        company = _summary_text(r.get("summary_company"))
        role = _summary_text(r.get("summary_role"))
        if not company and not role and r.get("yaml_blob") is not None:
            legacy_blob = cl_service.parse_blob(r.get("yaml_blob"))
            company = _summary_text(legacy_blob.get("company"))
            role = _summary_text(legacy_blob.get("role"))
        job_id = r.get("job_id")
        item = {
            "id": r["id"],
            "name": r.get("name") or "Cover letter",
            "company": company,
            "role": role,
            # Back-link to the source job (additive; None for standalone letters).
            "job_id": job_id,
            "job_title": titles.get(job_id) if job_id else None,
            "open_url": f"/cover-letters?id={r['id']}",
            "updated_at": r.get("updated_at"),
        }
        lineage = packet_lineages.get(r["id"])
        if lineage:
            item["packet_lineage"] = lineage
        out.append(item)
    return jsonify({"cover_letters": out, "count": len(out)})


@app.route("/api/documents")
def api_documents() -> object:
    """Unified document library — the caller's CVs AND cover letters (RLS-scoped).

    Backs the two-type library on /cover-letters (Stage 7). Returns BOTH document
    types in one payload so the page can render a single list with a CV / Cover
    letter type filter::

        {documents: [{id, type, name, updated_at, company?, role?, job_id?,
                      job_title?}], count}

    STRICTLY RLS-scoped: both lists come from ``cl_service.list_documents`` which
    runs under the caller's JWT (``auth.user_supabase()``) so Postgres RLS
    enforces ``owner_id = auth.uid()`` — NEVER the service-role client and NEVER a
    SECURITY DEFINER function/view to aggregate across the table. Soft-deleted
    rows are excluded (``deleted_at is null``, inside ``list_documents``). The
    linked-job title (for documents carrying a Stage-6 ``job_id``) is resolved
    with ONE additional RLS-scoped grouped lookup over the caller's own jobs.

    An optional ``?type=`` filters the payload server-side. It must be one of
    ``cv | cover_letter | all`` (default ``all``); anything else is a 400 (fail
    closed) so a junk value can never coerce the listing into unexpected output.
    The client still has its own filter chips — this is defense-in-depth so the
    boundary is validated regardless of caller.
    """
    type_filter = (request.args.get("type") or "all").strip().lower()
    if type_filter not in ("cv", "cover_letter", "all"):
        raise _BadRequest("type must be one of: cv, cover_letter, all")

    try:
        cvs = cl_service.list_documents("cv") if type_filter in ("cv", "all") else []
        letters = (
            cl_service.list_documents("cover_letter")
            if type_filter in ("cover_letter", "all")
            else []
        )
    except Exception as exc:  # noqa: BLE001 — demo/local shell should stay readable offline
        if not _can_use_demo_fallback_when_not_pending():
            if _account_deletion_pending():
                raise _Conflict("account deletion is pending")
            raise
        app.logger.warning("using local demo document-list fallback: %s", exc)
        items = _local_demo_document_items(type_filter)
        return jsonify({"documents": items, "count": len(items), "degraded": True})

    items: list[dict] = []
    # Resolve linked-job titles in ONE RLS-scoped grouped lookup (no N+1, never
    # SECURITY DEFINER) — a document only shows a job line if the caller owns the job.
    job_ids = [r.get("job_id") for r in cvs if r.get("job_id")]
    job_ids.extend(r.get("job_id") for r in letters if r.get("job_id"))
    try:
        titles = cl_service.job_titles_for(job_ids)
    except Exception as exc:  # noqa: BLE001 — passive title enrichment must not break listing
        if not _can_use_demo_read_fallback():
            raise
        app.logger.warning("using empty demo document job-title fallback: %s", exc)
        titles = {}
    doc_ids = [r["id"] for r in cvs if r.get("id")]
    doc_ids.extend(r["id"] for r in letters if r.get("id"))
    try:
        packet_lineages = jobs_svc.packet_lineage_for_documents(doc_ids)
    except Exception as exc:  # noqa: BLE001 — passive lineage must not break listing
        if not _can_use_demo_read_fallback():
            raise
        app.logger.warning("using empty demo document packet-lineage fallback: %s", exc)
        packet_lineages = {}
    for r in cvs:
        job_id = r.get("job_id")
        items.append(
            _document_summary(
                r,
                "cv",
                job_title=titles.get(job_id) if job_id else None,
                packet_lineage=packet_lineages.get(r["id"]),
            )
        )
    for r in letters:
        job_id = r.get("job_id")
        company = _summary_text(r.get("summary_company"))
        role = _summary_text(r.get("summary_role"))
        if not company and not role and r.get("yaml_blob") is not None:
            legacy_blob = cl_service.parse_blob(r.get("yaml_blob"))
            company = _summary_text(legacy_blob.get("company"))
            role = _summary_text(legacy_blob.get("role"))
        item = _document_summary(
            r,
            "cover_letter",
            job_title=titles.get(job_id) if job_id else None,
            company=company,
            role=role,
            packet_lineage=packet_lineages.get(r["id"]),
        )
        item["company"] = company
        item["role"] = role
        items.append(item)
    # Last-opened first across both types. Passive library reads do not bump
    # this value; only explicit document open/read paths do.
    items.sort(
        key=lambda d: (
            (d.get("last_used_at") is not None),
            d.get("last_used_at") or "",
            d.get("updated_at") or "",
            d.get("created_at") or "",
        ),
        reverse=True,
    )
    return jsonify({"documents": items, "count": len(items)})


@app.route("/api/cover-letter", methods=["GET", "POST"])
def api_cover_letter() -> object:
    """Read or edit one cover letter (RLS-scoped). ``?id=`` selects which.

    GET  → ``{id, name, ...blob}`` (recipient/company/role/tone/body/language/…).
    POST → save an edited ``{id, body, recipient?, company?, role?, tone?}``.
           The letter is editable after generation; this persists edits.
           Re-runs the word cap on the edited body (no LLM, no re-grounding —
           the user owns their own edits, but a runaway paste is still capped).
    """
    payload = request.get_json(silent=True) if request.method == "POST" else {}
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be an object", "code": "invalid_cover_envelope_type"}), 400
    doc_id = request.args.get("id") or payload.get("id")
    if not _is_uuid(doc_id):
        raise _BadRequest("missing or invalid cover-letter id")
    row = cl_service.document_by_id(str(doc_id), "cover_letter")
    if row is None:
        raise _NotFound("cover letter not found")

    blob = cl_service.normalized_blob_for_row(row)

    if request.method == "GET":
        cl_service.mark_document_used(str(doc_id), "cover_letter", now_iso=_now_iso())
        lineage = jobs_svc.packet_lineage_for_document(str(doc_id))
        out = {
            "id": row["id"],
            "name": row.get("name"),
            "updated_at": row.get("updated_at"),
            **blob,
        }
        if lineage:
            out["packet_lineage"] = lineage
        return jsonify(out)

    _ensure_document_writes_allowed()
    body = payload.get("body")
    if body is None or not isinstance(body, str):
        return jsonify({"error": "missing 'body' string"}), 400

    merged = dict(blob)
    merged["body"] = body
    merged["job_id"] = row.get("job_id")
    for key in ("recipient", "company", "role", "tone", "subject", "salutation", "closing"):
        if key in payload:
            merged[key] = payload[key]
    if "attachments" in payload:
        merged["attachments"] = payload.get("attachments")
    merged = cl_service.normalize_envelope(merged)

    # Cap a user-edited body too (defence-in-depth — not a page count).
    try:
        from cover_letter.guards import enforce_word_cap

        enforce_word_cap(merged.get("body") or "")
    except Exception as exc:  # noqa: BLE001 — WordCapError → 422
        return jsonify({"error": str(exc), "code": "word_cap"}), 422

    new_blob = cl_service.serialize_blob(merged)
    expected_revision = payload.get("revision")
    if expected_revision is None:
        expected_revision = row.get("updated_at")
    updated_at = _now_iso()
    if local_demo_store.active():
        saved_rows = (
            _document_store()
            .table("documents")
            .update(
                {
                    "yaml_blob": new_blob,
                    "updated_at": updated_at,
                    "summary_company": _summary_text(merged.get("company")),
                    "summary_role": _summary_text(merged.get("role")),
                }
            )
            .eq("id", str(doc_id))
            .eq("type", "cover_letter")
            .eq("updated_at", expected_revision)
            .is_("deleted_at", "null")
            .execute()
            .data
            or []
        )
        if not saved_rows:
            latest = cl_service.document_by_id(str(doc_id), "cover_letter")
            if latest is None:
                raise _NotFound("cover letter not found")
            if new_blob == (latest.get("yaml_blob") or ""):
                updated_at = latest.get("updated_at")
            else:
                return (
                    jsonify(
                        {
                            "error": "save conflict; reload the current letter before editing further",
                            "code": "revision_conflict",
                            "updated_at": latest.get("updated_at"),
                        }
                    ),
                    409,
                )
        else:
            updated_at = saved_rows[0].get("updated_at")
        lineage = jobs_svc.packet_lineage_for_document(str(doc_id))
        out = {
            "saved": True,
            "id": row["id"],
            "updated_at": updated_at,
            "cleanup_pending": False,
        }
        if lineage:
            out["packet_lineage"] = lineage
        return jsonify(out)
    try:
        saved = pdf_store.update_document_with_artifact_detach(
            document_id=str(doc_id),
            document_type="cover_letter",
            expected_revision=expected_revision,
            yaml_blob=new_blob,
            updated_at=updated_at,
            summary_company=merged.get("company") or "",
            summary_role=merged.get("role") or "",
        )
    except pdf_store.ArtifactAccountUnavailable:
        return jsonify({"error": "Account is unavailable.", "code": "account_unavailable"}), 409
    except pdf_store.StorageError as exc:
        app.logger.warning("atomic cover-letter save failed: %s", type(exc).__name__)
        return (
            jsonify(
                {
                    "error": "Letter storage is temporarily unavailable.",
                    "code": "document_save_unavailable",
                }
            ),
            503,
        )
    if saved.get("status") == "not_found":
        raise _NotFound("cover letter not found")
    if saved.get("status") == "revision_conflict":
        latest = cl_service.document_by_id(str(doc_id), "cover_letter")
        if latest is None:
            raise _NotFound("cover letter not found")
        if new_blob == (latest.get("yaml_blob") or ""):
            updated_at = latest.get("updated_at")
        else:
            return (
                jsonify(
                    {
                        "error": "save conflict; reload the current letter before editing further",
                        "code": "revision_conflict",
                        "updated_at": latest.get("updated_at"),
                    }
                ),
                409,
            )
    detached = saved.get("artifacts") or []
    if detached:
        try:
            pdf_store.delete_detached_artifacts(detached, strict=False)
        except Exception as exc:  # noqa: BLE001 - save is already committed
            app.logger.warning(
                "deferred cover-letter PDF cleanup: %s", type(exc).__name__
            )
    lineage = jobs_svc.packet_lineage_for_document(str(doc_id))
    out = {
        "saved": True,
        "id": row["id"],
        "updated_at": saved.get("document_revision") or updated_at,
        "cleanup_pending": bool(detached),
    }
    if lineage:
        out["packet_lineage"] = lineage
    return jsonify(out)


@app.route("/api/cover-letter/<doc_id>/preview")
def api_cover_letter_preview(doc_id: str) -> object:
    """Render a cover letter preview using the same mapping as PDF build."""
    if not _is_uuid(doc_id):
        raise _BadRequest("invalid cover-letter id")
    row = cl_service.document_by_id(doc_id, "cover_letter")
    if row is None:
        raise _NotFound("cover letter not found")
    blob = cl_service.normalized_blob_for_row(row)
    try:
        sender_name, _sender_cv_id, _sender_cv_revision = (
            _sender_identity_for_cover(blob)
        )
    except _CoverSourceUnavailable as exc:
        return jsonify({"error": "Cover-letter signer is unavailable.", "code": exc.code}), exc.status
    render_doc = cl_service.build_render_doc(
        blob, name=row.get("name") or "Cover letter", sender_name=sender_name
    )
    try:
        from cover_letter.renderer import render_cover_letter_html
    except ImportError:  # pragma: no cover - unusual import roots
        from engine.cover_letter.renderer import render_cover_letter_html
    html = render_cover_letter_html(render_doc, base_url=f"file://{REPO}")
    return (
        html,
        200,
        {
            "Content-Type": "text/html; charset=utf-8",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@app.route("/api/cover-letter/<doc_id>/rename", methods=["POST"])
def api_cover_letter_rename(doc_id: str) -> object:
    """Rename a cover letter. Body ``{name}``. Reuses the doc-CRUD machinery."""
    if not _is_uuid(doc_id):
        raise _BadRequest("invalid cover-letter id")
    _ensure_document_writes_allowed()
    payload = _json_object()
    name = _clean_cv_name(payload.get("name"))
    if not name:
        raise _BadRequest("missing or empty 'name'")
    if cl_service.document_by_id(doc_id, "cover_letter") is None:
        raise _NotFound("cover letter not found")
    if not cl_service.rename_document(doc_id, "cover_letter", name, now_iso=_now_iso()):
        raise _NotFound("cover letter not found")
    return jsonify({"id": doc_id, "name": name})


@app.route("/api/cover-letter/<doc_id>", methods=["DELETE"])
def api_cover_letter_delete(doc_id: str) -> object:
    """Delete a cover letter (RLS-scoped). 404 if not owned/visible."""
    if not _is_uuid(doc_id):
        raise _BadRequest("invalid cover-letter id")
    _ensure_document_writes_allowed()
    if cl_service.document_by_id(doc_id, "cover_letter") is None:
        raise _NotFound("cover letter not found")
    deletion = cl_service.delete_document(doc_id, "cover_letter", include_artifacts=True)
    if not isinstance(deletion, dict):
        raise _NotFound("cover letter not found")
    _delete_detached_document_pdf_artifacts(deletion.get("artifacts") or [])
    return jsonify({"deleted": doc_id})


@app.route("/api/cover-letter/<doc_id>/build", methods=["GET", "POST"])
@_render_rate_limit
def api_cover_letter_build(doc_id: str) -> object:
    """Render a cover letter to a PDF (R2-cached or streamed; multi-page OK).

    This uses the SEPARATE cover-letter renderer — it has NO 1-page gate. The
    CV's overflow invariant (engine/build.py) is untouched.

    Same two-mode delivery as ``/api/build`` (Stage 8): when R2 is configured
    an unchanged letter is served from the content-hash cache via a signed URL
    (JSON ``{url, cached, page_count}``); otherwise the bytes are streamed
    exactly as before. Both go through the semaphore-bounded renderer.
    """
    if not _is_uuid(doc_id):
        raise _BadRequest("invalid cover-letter id")
    row = cl_service.document_by_id(doc_id, "cover_letter")
    if row is None:
        raise _NotFound("cover letter not found")

    blob = cl_service.normalized_blob_for_row(row)
    # Use the signer's name from the source CV if we can read it, else blank.
    try:
        sender_name, sender_cv_id, sender_cv_revision = (
            _sender_identity_for_cover(blob)
        )
    except _CoverSourceUnavailable as exc:
        return jsonify({"error": "Cover-letter signer is unavailable.", "code": exc.code}), exc.status
    render_doc = cl_service.build_render_doc(
        blob, name=row.get("name") or "Cover letter", sender_name=sender_name
    )

    base_url = f"file://{REPO}"

    # R2 path: cache-aware build returning a signed URL (or a cache hit). The
    # cache key is the stored blob text (captures body/recipient/company/etc.)
    # plus the signer name (changing the source CV's name re-renders).
    if _remote_pdf_storage_enabled():
        cache_key_text = cl_service.serialize_blob(blob) + "\x00" + sender_name
        try:
            built = pdf_store.build_cover_letter_pdf(
                document_id=row["id"],
                owner_id=g.owner_id,
                cache_key_text=cache_key_text,
                render_doc=render_doc,
                document_revision=row.get("updated_at"),
                dependency_document_id=sender_cv_id,
                dependency_revision=sender_cv_revision,
                base_url=base_url,
            )
        except pdf_store.ArtifactRevisionConflict:
            return (
                jsonify(
                    {
                        "error": "cover letter changed during PDF build",
                        "code": "revision_conflict",
                    }
                ),
                409,
            )
        except pdf_store.ArtifactAccountUnavailable:
            return jsonify({"error": "Account is unavailable.", "code": "account_unavailable"}), 409
        except pdf_store.StorageError:
            return _authorized_letter_stream(row, render_doc, base_url)
        except Exception as exc:  # noqa: BLE001 — render failure → 500 JSON
            return jsonify({"error": f"render failed: {type(exc).__name__}: {exc}"}), 500
        authorization_error = _export_authorization_error(row, "Cover letter")
        if authorization_error is not None:
            return authorization_error
        return jsonify(
            {"url": built.url, "cached": built.cached, "page_count": built.page_count}
        )

    # Fallback path: render + stream the bytes (current behaviour, no R2).
    return _authorized_letter_stream(row, render_doc, base_url)


def _export_authorization_error(
    row: dict[str, Any],
    document_label: str,
) -> object | None:
    """Return an HTTP error unless this exact revision remains exportable."""
    try:
        if not local_demo_store.active():
            pdf_store.authorize_document_export(
                document_id=str(row.get("id")),
                document_revision=row.get("updated_at"),
            )
    except pdf_store.ArtifactAccountUnavailable:
        return jsonify({"error": "Account is unavailable.", "code": "account_unavailable"}), 409
    except pdf_store.ArtifactRevisionConflict:
        return jsonify(
            {
                "error": f"{document_label} changed before export.",
                "code": "revision_conflict",
            }
        ), 409
    except pdf_store.StorageError:
        return jsonify(
            {
                "error": "Export authorization is unavailable.",
                "code": "export_authorization_unavailable",
            }
        ), 503
    return None


def _authorized_letter_stream(
    row: dict[str, Any],
    render_doc: dict[str, Any],
    base_url: str,
) -> object:
    authorization_error = _export_authorization_error(row, "Cover letter")
    if authorization_error is not None:
        return authorization_error
    return _stream_letter_fallback(render_doc, base_url, row=row)


def _stream_letter_fallback(
    render_doc: dict,
    base_url: str,
    *,
    row: dict[str, Any] | None = None,
) -> object:
    """Render a cover letter (semaphore-bounded) and stream the bytes (no R2)."""
    try:
        pdf_bytes = pdf_store.render_cover_letter(render_doc, base_url=base_url)
    except Exception as exc:  # noqa: BLE001 — render failure → 500 JSON
        return jsonify({"error": f"render failed: {type(exc).__name__}: {exc}"}), 500
    if row is not None:
        authorization_error = _export_authorization_error(row, "Cover letter")
        if authorization_error is not None:
            return authorization_error
    return (
        pdf_bytes,
        200,
        {
            "Content-Type": "application/pdf",
            "Content-Disposition": pdf_store._pdf_disposition("cover-letter.pdf"),
        },
    )


def _sender_identity_for_cover(
    blob: dict[str, Any],
) -> tuple[str, str | None, object | None]:
    """Return an immutable signer snapshot, with strict legacy fallback."""
    snapshot = str(blob.get("sender_name") or "").strip()
    if snapshot:
        return snapshot, None, None
    cv_id = blob.get("source_cv_id")
    if not _is_uuid(cv_id):
        return "", None, None
    try:
        row = cl_service.document_by_id(str(cv_id), "cv")
    except Exception as exc:  # noqa: BLE001 - fail closed on storage outage
        raise _CoverSourceUnavailable(
            status=503,
            code="source_cv_lookup_unavailable",
        ) from exc
    if row is None:
        raise _CoverSourceUnavailable(status=409, code="source_cv_missing")
    try:
        cv = yaml_safe_load(row.get("yaml_blob") or "")
    except Exception as exc:  # noqa: BLE001 - malformed legacy source
        raise _CoverSourceUnavailable(status=409, code="source_cv_invalid") from exc
    name = str(cv.get("name") or "").strip() if isinstance(cv, dict) else ""
    if not name:
        raise _CoverSourceUnavailable(status=409, code="source_cv_name_missing")
    return name, str(row.get("id")), row.get("updated_at")


# ---------------------------------------------------------------------------
# CV tailoring + ATS keyword-gap score (Stage 6). Tailoring DUPLICATES the
# source CV (the original is preserved byte-for-byte), runs ONE structured LLM
# call at the cheaper Haiku tier, applies the typed suggestions
# deterministically, then enforces the two deterministic guards:
#   * anti-fabrication cross-reference (any non-source proper noun/number → 422)
#   * the engine's 1-page gate (overflow → 409)
# The in-house ATS scorer is pure/offline (no LLM, no paid feed).
# ---------------------------------------------------------------------------
def _jd_from_request(payload: dict) -> str:
    """Resolve pasted or URL job descriptions through the saved-jobs policy."""
    return jobs_svc.resolve_jd_text(payload.get("jd_text"), payload.get("jd_url"))


def _tailor_request_options(payload: dict) -> tuple[str, str | None, str | None, str, list[str]]:
    """Validate the shared Tailor route options and return normalized values."""
    model = payload.get("model")
    if model is not None and not isinstance(model, str):
        raise _BadRequest("invalid 'model'")
    job_id = payload.get("job_id")
    if job_id is not None and not _is_uuid(job_id):
        raise _BadRequest("invalid 'job_id'")
    try:
        profile = ats_profiles.normalize_profile(payload.get("profile"))
        compare_profiles = ats_profiles.normalize_profile_list(payload.get("compare_profiles"))
    except ValueError as exc:
        raise tailor_svc.TailorError(400, {
            "error": "invalid_profile",
            "available_profiles": ats_profiles.available_profile_ids(),
        }) from exc
    provider, eff_model = _request_provider_model(payload)
    return provider, eff_model, job_id, profile, compare_profiles


def _saved_job_for_jd(job_id: str) -> dict:
    job = jobs_svc.get_job(job_id)
    if job is None:
        raise jobs_svc.JobError(404, {"error": "Job not found", "code": "job_not_found"})
    jobs_svc._require_full_jd(job)
    text = job.get("jd_text") or ""
    if not text.strip():
        raise _BadRequest("saved job has no job description")
    return job


def _saved_job_jd_text(job_id: str) -> str:
    job = _saved_job_for_jd(job_id)
    text = job.get("jd_text") or ""
    return text


def _jd_from_request_or_saved_job(payload: dict, job_id: str | None) -> str:
    if job_id is not None:
        return _saved_job_jd_text(job_id)
    return _jd_from_request(payload)


@app.route("/api/cv/<doc_id>/tailor", methods=["POST"])
@_ai_rate_limit
def api_cv_tailor(doc_id: str) -> object:
    """Tailor a CV to a job (duplicate-then-tailor). Body: ``{jd_text?|jd_url?}``.

    Duplicates the source CV into a NEW ``cv`` document (the original is never
    modified), reorders / re-emphasizes / lightly rewrites the user's genuine
    bullets via one structured LLM call, then enforces the two deterministic
    guards before persisting:
      * anti-fabrication — any proper noun / number not grounded in the SOURCE
        CV → 422 ``{error:'unverified_claims', items:[…]}`` (nothing saved);
      * the 1-page gate — an overflowing tailored CV → 409
        ``{error:'overflow', heading:…}`` (nothing saved).
    Returns ``{tailored_cv_id, ats:{…}, gaps:[…], applied, warnings}``.
    """
    if not _is_uuid(doc_id):
        return jsonify({"error": "invalid document id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    provider, eff_model, job_id, profile, compare_profiles = _tailor_request_options(payload)
    jd_text = _jd_from_request_or_saved_job(payload, job_id)  # _BadRequest → 400

    # TailorError (404/409/422/502) + LLMUnavailable (402/503) propagate to
    # their registered error handlers.
    result = tailor_svc.tailor_cv(
        cv_id=doc_id,
        jd_text=jd_text,
        model=eff_model,
        provider=provider,
        job_id=job_id,
        profile=profile,
        compare_profiles=compare_profiles,
        now_iso=_now_iso(),
    )
    if job_id is not None:
        tailored_cv_id = str(result["tailored_cv_id"])
        try:
            jobs_svc.record_tailored_cv_packet_lineage(
                job_id=job_id,
                source_cv_id=doc_id,
                tailored_cv_id=tailored_cv_id,
                provider=provider,
                model=eff_model,
                ats=result.get("ats") if isinstance(result.get("ats"), dict) else None,
                source_hash=result.get("source_hash"),
                job_content_hash=result.get("jd_hash"),
                tailored_hash=result.get("tailored_hash"),
                score_system_id=profile,
                review_id=result.get("review_id"),
                now_iso=_now_iso(),
            )
        except Exception:
            try:
                cl_service.delete_document(tailored_cv_id, "cv")
            except Exception as cleanup_exc:  # noqa: BLE001
                app.logger.warning("tailored CV rollback after packet-lineage failure failed: %s", cleanup_exc)
            raise
        lineage = jobs_svc.packet_lineage_for_document(str(result["tailored_cv_id"]))
        if lineage:
            result = {**result, "packet_lineage": lineage}
    return jsonify(result), 201


@app.route("/api/cv/<doc_id>/tailor/review", methods=["POST"])
@_ai_rate_limit
def api_cv_tailor_review(doc_id: str) -> object:
    """Prepare a reviewed CV diff and signed apply token without saving it."""
    if not _is_uuid(doc_id):
        return jsonify({"error": "invalid document id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    provider, eff_model, job_id, profile, compare_profiles = _tailor_request_options(payload)
    jd_text = _jd_from_request_or_saved_job(payload, job_id)
    result = tailor_svc.prepare_tailor_review(
        cv_id=doc_id,
        jd_text=jd_text,
        model=eff_model,
        provider=provider,
        job_id=job_id,
        profile=profile,
        compare_profiles=compare_profiles,
        now_iso=_now_iso(),
    )
    return jsonify(result), 200


@app.route("/api/cv/<doc_id>/tailor/apply", methods=["POST"])
def api_cv_tailor_apply(doc_id: str) -> object:
    """Apply a signed Tailor review token into one new CV document."""
    if not _is_uuid(doc_id):
        return jsonify({"error": "invalid document id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    result = tailor_svc.apply_tailor_review(
        cv_id=doc_id,
        review_token=payload.get("review_token"),
        now_iso=_now_iso(),
    )
    job_id = result.get("job_id")
    if job_id is not None:
        try:
            lineage = jobs_svc.record_tailored_cv_packet_lineage(
                job_id=str(job_id),
                source_cv_id=doc_id,
                tailored_cv_id=str(result["tailored_cv_id"]),
                provider=result.get("provider"),
                model=result.get("model"),
                ats=result.get("ats") if isinstance(result.get("ats"), dict) else None,
                source_hash=result.get("source_hash"),
                job_content_hash=result.get("jd_hash"),
                tailored_hash=result.get("tailored_hash"),
                score_system_id=None,
                review_id=result.get("review_id"),
                now_iso=_now_iso(),
            )
            lineage = jobs_svc.packet_lineage_for_document(str(result["tailored_cv_id"])) or lineage
            if lineage:
                result = {**result, "packet_lineage": lineage}
        except Exception:
            try:
                cl_service.delete_document(str(result["tailored_cv_id"]), "cv")
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve lineage failure
                app.logger.warning("tailored CV rollback after packet-lineage failure failed: %s", cleanup_exc)
            try:
                tailor_svc.release_review_apply(str(result.get("review_id") or ""))
            except Exception as release_exc:  # noqa: BLE001 - preserve lineage failure
                app.logger.warning("tailor review release after packet-lineage failure failed: %s", release_exc)
            raise
    return jsonify(result), 201


@app.route("/api/ats-score", methods=["POST"])
@_ai_rate_limit
def api_ats_score() -> object:
    """Score a CV against a JD WITHOUT tailoring. Body: ``{cv_id, jd_text?|jd_url?, provider?, model?}``.

    No document is created. When the LLM rubric is enabled (``ONEPAGER_ATS
    _RUBRIC``, default on) AND a key resolves, the response is the rubric's
    JD-relative breakdown (``source:"rubric"`` + categories/strengths/gaps/
    suggestions); otherwise — no key, flag off, or any rubric failure — it
    degrades to the deterministic keyword score (``source:"deterministic"`` +
    ``{score, tier1_coverage, matched, gaps, …}``). It ALWAYS returns a score and
    never a 402: ATS scoring is free. The CV read is RLS-scoped, so another
    tenant's CV is invisible (→ 404).
    """
    payload = request.get_json(silent=True) or {}
    cv_id = payload.get("cv_id")
    if not _is_uuid(cv_id):
        return jsonify({"error": "missing or invalid 'cv_id'"}), 400
    job_id = payload.get("job_id")
    if job_id is not None and not _is_uuid(job_id):
        return jsonify({"error": "invalid 'job_id'"}), 400
    jd_text = _jd_from_request_or_saved_job(payload, job_id)  # _BadRequest → 400
    try:
        profile = ats_profiles.normalize_profile(payload.get("profile"))
        compare_profiles = ats_profiles.normalize_profile_list(payload.get("compare_profiles"))
    except ValueError:
        return jsonify({
            "error": "invalid_profile",
            "available_profiles": ats_profiles.available_profile_ids(),
        }), 400
    # Effective provider/model for the rubric: request body → profile → default.
    provider, model = _request_provider_model(payload)
    # TailorError (404 / empty_jd) propagates to its handler. LLMUnavailable is
    # handled INSIDE the service (degrades to deterministic) — never a 402 here.
    result = tailor_svc.ats_score_for_cv(
        cv_id=cv_id,
        jd_text=jd_text,
        provider=provider,
        model=model,
        job_id=job_id,
        profile=profile,
        compare_profiles=compare_profiles,
        now_iso=_now_iso(),
    )
    return jsonify(result), 200


@app.route("/api/ats-profiles")
def api_ats_profiles() -> object:
    """Return transparent ATS scoring profile metadata for frontend selectors."""
    return jsonify({
        "profiles": ats_profiles.public_profiles(),
        "default_profile": ats_profiles.DEFAULT_PROFILE,
    })


# ---------------------------------------------------------------------------
# Saved jobs (Stage 7). A user saves a job (paste or SSRF-guarded URL), works
# against it, and one-clicks "tailor a CV + write a cover letter for this job".
# Documents created against a job carry ``documents.job_id`` so the job's
# detail view groups them. All routes are RLS owner-scoped via user_supabase().
# Indeed-MCP import is OUT OF SCOPE for this stage (paste/URL only).
# ---------------------------------------------------------------------------
@app.route("/api/jobs/sources/status")
def api_job_source_status() -> object:
    """Read-only job-source capability/freshness status for Settings."""
    return jsonify(jobs_svc.job_source_status())


@app.route("/api/jobs/local-search", methods=["POST"])
def api_jobs_local_search() -> object:
    """Search a configured local jobs DB as a read-only preview source."""
    if _account_deletion_pending():
        return jsonify({"results": [], "count": 0, "deletion_pending": True})
    payload = request.get_json(silent=True) or {}
    return jsonify(jobs_svc.local_jobs_search(
        query=payload.get("query"),
        filters=payload.get("filters"),
        limit=payload.get("limit"),
    ))


@app.route("/api/jobs", methods=["GET", "POST"])
def api_jobs() -> object:
    """List the caller's saved jobs, or save a new one.

    GET  → ``{jobs: [{id, title, company, status, created_at}], count}`` newest
           first (RLS-scoped).
    POST → ``{jd_text?|jd_url?, title?, company?}`` saves a job: a ``jd_url`` is
           fetched via the SSRF guard (RFC-1918 / metadata / redirect blocked),
           else ``jd_text`` is used; the JD is parsed into keyed JSON and stored
           with ``status='saved'``. Returns ``{id, title, company, status, …}``
           (201).
    """
    if request.method == "GET":
        try:
            rows = [] if _account_deletion_pending() else jobs_svc.list_jobs()
        except Exception as exc:  # noqa: BLE001 — demo/local shell should stay readable offline
            if not _can_use_demo_read_fallback():
                raise
            app.logger.warning("using empty demo jobs fallback: %s", exc)
            rows = jobs_svc.demo_saved_jobs()
        return jsonify({"jobs": rows, "count": len(rows)})

    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    # JobError (400 SSRF/empty / 413 oversized / 500) propagates to its handler.
    # The catalog-metadata fields are sanitized in the service (confidence →
    # int 0-100, deadline → ISO date, why_fit length-capped) — malformed
    # advisory values drop silently rather than erroring.
    result = jobs_svc.create_job(
        jd_text=payload.get("jd_text"),
        jd_url=payload.get("jd_url"),
        title=payload.get("title"),
        company=payload.get("company"),
        field=payload.get("field"),
        deadline=payload.get("deadline"),
        confidence=payload.get("confidence"),
        why_fit=payload.get("why_fit"),
    )
    return jsonify(result), 201


@app.route("/api/jobs/import-runs", methods=["GET", "POST"])
@_ai_rate_limit
def api_job_import_runs() -> object:
    """List import runs or attempt an approved-adapter import.

    Blind crawling is disabled by default. Manual imports continue through
    `POST /api/jobs` with pasted job text.
    """
    if request.method == "GET":
        if _account_deletion_pending():
            return jsonify({"runs": [], "deletion_pending": True})
        return jsonify({"runs": jobs_svc.list_import_runs()})
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    run = jobs_svc.start_import_run(
        provider=payload.get("provider"),
        query=payload.get("query"),
        location=payload.get("location"),
        limit=payload.get("limit"),
        filters=payload.get("filters"),
        postings=payload.get("postings"),
    )
    return jsonify(run), 202


@app.route("/api/jobs/import-runs/<run_id>/results")
def api_job_import_results(run_id: str) -> object:
    if not _is_uuid(run_id):
        return jsonify({"error": "invalid import run id"}), 400
    if _account_deletion_pending():
        return jsonify({"results": [], "deletion_pending": True})
    return jsonify({"results": jobs_svc.list_import_results(run_id)})


@app.route("/api/jobs/import-runs/<run_id>/save-selected", methods=["POST"])
def api_job_import_save_selected(run_id: str) -> object:
    if not _is_uuid(run_id):
        return jsonify({"error": "invalid import run id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    return jsonify(jobs_svc.save_selected_import_results(run_id, payload.get("result_ids")))


@app.route("/api/jobs/import-runs/<run_id>/mark-stale", methods=["POST"])
def api_job_import_mark_stale(run_id: str) -> object:
    if not _is_uuid(run_id):
        return jsonify({"error": "invalid import run id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    return jsonify(jobs_svc.mark_import_results_stale(run_id, payload.get("result_ids")))


@app.route("/api/jobs/<job_id>")
def api_job_detail(job_id: str) -> object:
    """A saved job plus its linked documents (CVs + cover letters), grouped.

    Reads the job (RLS) and the caller's documents whose ``job_id`` matches —
    the linkage written by the tailor / cover-letter flows. 404 if the job
    isn't owned/visible. Returns ``{job, cvs:[…], cover_letters:[…]}``.
    """
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    try:
        if _account_deletion_pending():
            raise _NotFound("job not found")
        return jsonify(jobs_svc.job_detail(job_id))  # JobError 404 → handler
    except jobs_svc.JobError:
        raise
    except _NotFound:
        raise
    except Exception as exc:  # noqa: BLE001 — demo/local shell should stay readable offline
        if not _can_use_demo_read_fallback():
            raise
        app.logger.warning("using degraded demo job-detail fallback: %s", exc)
        return jsonify(
            {
                "job": {
                    "id": job_id,
                    "title": "Unavailable job",
                    "company": "",
                    "status": "saved",
                    "jd_text": "",
                },
                "cvs": [],
                "cover_letters": [],
                "degraded": True,
            }
        )


@app.route("/api/jobs/<job_id>", methods=["PATCH"])
def api_job_update(job_id: str) -> object:
    """Edit a saved job's catalog metadata (RLS-scoped). 404 if not owned.

    Body: a partial ``{title?, company?, field?, deadline?, confidence?,
    why_fit?, status?}``. Only keys present are written; the service sanitizes
    each (confidence → int 0-100, deadline → ISO date, why_fit length-capped,
    status validated against the enum). An empty/invalid patch → 400. Returns
    the updated job (with ``has_cover``).
    """
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "expected a JSON object"}), 400
    # JobError (400 empty_patch/bad_status, 404) propagates to its handler.
    return jsonify(jobs_svc.update_job(job_id, payload))


@app.route("/api/jobs/<job_id>/status", methods=["POST"])
def api_job_status(job_id: str) -> object:
    """Set a saved job's lifecycle status. Body ``{status}``.

    ``status`` must be one of saved | open | speculative | closed. Application
    state is derived from immutable attempt evidence, never this mutable label.
    A garbage status → 400; a job not owned/visible → 404. Returns
    ``{id, status}``.
    """
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if not isinstance(status, str):
        return jsonify({"error": "missing 'status' string"}), 400
    # JobError (400 bad_status / 404) propagates to its handler.
    return jsonify(jobs_svc.set_status(job_id, status.strip()))


@app.route("/api/jobs/<job_id>/source/revalidate", methods=["POST"])
def api_job_source_revalidate(job_id: str) -> object:
    """Manual saved-job source revalidation; never fetches external pages."""
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    return jsonify(jobs_svc.revalidate_job_source(
        job_id,
        full_jd_text=payload.get("full_jd_text"),
        mark_stale=payload.get("mark_stale") is True,
        now_iso=_now_iso(),
    ))


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def api_job_delete(job_id: str) -> object:
    """Delete an unused saved job; linked application history is protected."""
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    _ensure_document_writes_allowed()
    return jsonify(jobs_svc.delete_job(job_id))  # JobError 404 → handler


@app.route("/api/jobs/<job_id>/generate", methods=["POST"])
@_ai_rate_limit
def api_job_generate(job_id: str) -> object:
    """Legacy direct packet creation endpoint.

    Packet completion now requires the review-first flow, including explicit
    cover-letter grounding review evidence. This route validates legacy callers
    and returns a review-required error instead of writing packet documents.
    """
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    source_cv_id = payload.get("source_cv_id")
    if not _is_uuid(source_cv_id):
        return jsonify({"error": "missing or invalid 'source_cv_id'"}), 400
    provider, model = _request_provider_model(payload)
    try:
        profile = ats_profiles.normalize_profile(payload.get("profile"))
    except ValueError as exc:
        raise tailor_svc.TailorError(400, {
            "error": "invalid_profile",
            "available_profiles": ats_profiles.available_profile_ids(),
        }) from exc
    # JobError (404), TailorError (404/409/422/502), CoverLetterError
    # (404/422/502), LLMUnavailable (402/503) all propagate to their handlers.
    result = jobs_svc.generate_for_job(
        job_id=job_id,
        source_cv_id=source_cv_id,
        provider=provider,
        model=model,
        profile=profile,
        force=payload.get("force") is True,
        now_iso=_now_iso(),
    )
    return jsonify(result), 201


@app.route("/api/jobs/<job_id>/review", methods=["POST"])
@_ai_rate_limit
def api_job_review(job_id: str) -> object:
    """Prepare a job-scoped tailored-CV review before generating the packet."""
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    source_cv_id = payload.get("source_cv_id")
    if not _is_uuid(source_cv_id):
        return jsonify({"error": "missing or invalid 'source_cv_id'"}), 400
    provider, model = _request_provider_model(payload)
    try:
        profile = ats_profiles.normalize_profile(payload.get("profile"))
        compare_profiles = ats_profiles.normalize_profile_list(payload.get("compare_profiles"))
    except ValueError as exc:
        raise tailor_svc.TailorError(400, {
            "error": "invalid_profile",
            "available_profiles": ats_profiles.available_profile_ids(),
        }) from exc
    result = jobs_svc.prepare_packet_review(
        job_id=job_id,
        source_cv_id=source_cv_id,
        provider=provider,
        model=model,
        profile=profile,
        compare_profiles=compare_profiles,
        now_iso=_now_iso(),
    )
    return jsonify(result), 200


@app.route("/api/jobs/<job_id>/apply-review", methods=["POST"])
@_ai_rate_limit
def api_job_apply_review(job_id: str) -> object:
    """Apply a reviewed tailored CV and generate the linked cover-letter packet."""
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    source_cv_id = payload.get("source_cv_id")
    if not _is_uuid(source_cv_id):
        return jsonify({"error": "missing or invalid 'source_cv_id'"}), 400
    provider, model = _request_provider_model(payload)
    result = jobs_svc.apply_packet_review(
        job_id=job_id,
        source_cv_id=source_cv_id,
        review_token=payload.get("review_token"),
        provider=provider,
        model=model,
        now_iso=_now_iso(),
    )
    return jsonify(result), 201


@app.route("/api/jobs/<job_id>/packets/<packet_id>/confirm-cover-letter-review", methods=["POST"])
def api_job_packet_confirm_cover_letter_review(job_id: str, packet_id: str) -> object:
    """Confirm review of the generated cover-letter artifact by exact hash."""
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    if not _is_uuid(packet_id):
        return jsonify({"error": "invalid packet id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    result = jobs_svc.confirm_cover_letter_review(
        job_id=job_id,
        packet_id=packet_id,
        cover_letter_hash=payload.get("cover_letter_hash"),
        now_iso=_now_iso(),
    )
    return jsonify(result), 200


@app.route("/api/jobs/<job_id>/packets/<packet_id>/archive", methods=["POST"])
def api_job_packet_archive(job_id: str, packet_id: str) -> object:
    """Archive a packet row without deleting linked documents."""
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    if not _is_uuid(packet_id):
        return jsonify({"error": "invalid packet id"}), 400
    _ensure_document_writes_allowed()
    return jsonify(jobs_svc.archive_packet(job_id, packet_id, now_iso=_now_iso()))


@app.route("/api/jobs/<job_id>/packets/<packet_id>/regenerate", methods=["POST"])
@_ai_rate_limit
def api_job_packet_regenerate(job_id: str, packet_id: str) -> object:
    """Archive and regenerate a packet from its recorded source CV."""
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    if not _is_uuid(packet_id):
        return jsonify({"error": "invalid packet id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    provider, model = _request_provider_model(payload)
    return jsonify(jobs_svc.regenerate_packet(
        job_id=job_id,
        packet_id=packet_id,
        provider=provider,
        model=model,
        profile=payload.get("profile") or jobs_svc.DEFAULT_SCORE_SYSTEM_ID,
        now_iso=_now_iso(),
    )), 201


@app.route("/api/jobs/<job_id>/apply/readiness")
def api_job_apply_readiness(job_id: str) -> object:
    """Read backend readiness for external apply actions. No automation."""
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    packet_id = request.args.get("packet_id")
    if packet_id and not _is_uuid(packet_id):
        return jsonify({"error": "invalid packet id"}), 400
    if _account_deletion_pending():
        raise _NotFound("job not found")
    return jsonify(apply_svc.application_readiness(job_id, packet_id=packet_id or None))


@app.route("/api/jobs/<job_id>/apply/open", methods=["POST"])
@_ai_rate_limit
def api_job_apply_open(job_id: str) -> object:
    """Record a confirmed manual-open attempt and return the target URL."""
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        # A truthy non-object JSON body (array/string/bool/number) would
        # otherwise raise AttributeError on .get → HTML 500.
        payload = {}
    packet_id = payload.get("packet_id")
    if packet_id is not None and not _is_uuid(packet_id):
        return jsonify({"error": "invalid packet id"}), 400
    return jsonify(
        apply_svc.run_apply_action(
            job_id=job_id,
            action="manual_open",
            packet_id=packet_id,
            request_payload=payload,
            now_iso=_now_iso(),
        )
    ), 202


@app.route("/api/jobs/<job_id>/apply/dry-run", methods=["POST"])
@_ai_rate_limit
def api_job_apply_dry_run(job_id: str) -> object:
    """Run a no-network dry-run against an explicitly configured fixture host."""
    if not _is_uuid(job_id):
        return jsonify({"error": "invalid job id"}), 400
    _ensure_document_writes_allowed()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        # A truthy non-object JSON body (array/string/bool/number) would
        # otherwise raise AttributeError on .get → HTML 500.
        payload = {}
    packet_id = payload.get("packet_id")
    if packet_id is not None and not _is_uuid(packet_id):
        return jsonify({"error": "invalid packet id"}), 400
    return jsonify(
        apply_svc.run_apply_action(
            job_id=job_id,
            action="dry_run",
            packet_id=packet_id,
            request_payload=payload,
            now_iso=_now_iso(),
        )
    )


_CHROME_TOKEN = "__CHROME_TOPBAR__"
_CHROME_CURRENT_TOKENS = {
    "jobs": "__CHROME_CURRENT_JOBS__",
    "letters": "__CHROME_CURRENT_LETTERS__",
    "tailor": "__CHROME_CURRENT_TAILOR__",
    "account": "__CHROME_CURRENT_ACCOUNT__",
}


def _subpage_chrome_html(
    *, title_icon: str, title_key: str, title_text: str, current: str, right_lead: str
) -> str:
    """Render the shared subpage topbar (static/_chrome.html) for one page.

    The fragment is the single source of truth for the .cl-topbar markup
    (plan 108 stage 2) — pages carry only a __CHROME_TOPBAR__ placeholder.
    Raises loudly on a missing fragment or an unknown ``current`` marker so
    a broken chrome can never ship silently.
    """
    if current not in _CHROME_CURRENT_TOKENS:
        raise ValueError(f"unknown subpage chrome current-page marker: {current!r}")
    fragment = (STATIC_DIR / "_chrome.html").read_text(encoding="utf-8")
    fragment = (
        fragment.replace("__CHROME_TITLE_ICON__", title_icon)
        .replace("__CHROME_TITLE_KEY__", title_key)
        .replace("__CHROME_TITLE_TEXT__", title_text)
        .replace("__CHROME_RIGHT_LEAD__", right_lead)
    )
    for name, token in _CHROME_CURRENT_TOKENS.items():
        fragment = fragment.replace(token, ' aria-current="page"' if name == current else "")
    return fragment


def _serve_subpage(filename: str, **chrome: str) -> object:
    """Serve a subpage HTML file with the shared chrome injected at serve time."""
    html = (STATIC_DIR / filename).read_text(encoding="utf-8")
    if _CHROME_TOKEN not in html:
        raise RuntimeError(
            f"{filename} is missing the {_CHROME_TOKEN} placeholder — "
            "the shared subpage chrome cannot be injected"
        )
    html = html.replace(_CHROME_TOKEN, _subpage_chrome_html(**chrome))
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/tailor")
def tailor_page() -> object:
    """Serve the CV-tailoring page (shared chrome injected at serve time)."""
    return _serve_subpage(
        "tailor.html",
        title_icon="scissors",
        title_key="app.tailor",
        title_text="ATS score &amp; review",
        current="tailor",
        right_lead='        <span id="tl-credit" class="cl-credit muted" role="status" hidden></span>',
    )


@app.route("/cover-letters")
def cover_letters_page() -> object:
    """Serve the cover-letter editor page (shared chrome injected at serve time)."""
    return _serve_subpage(
        "cover-letters.html",
        title_icon="mail",
        title_key="cl.title",
        title_text="Cover letters",
        current="letters",
        right_lead='        <span id="cl-credit" class="cl-credit muted" role="status" hidden></span>',
    )


@app.route("/jobs")
def jobs_page() -> object:
    """Serve the saved-jobs page (shared chrome injected at serve time)."""
    return _serve_subpage(
        "jobs.html",
        title_icon="briefcase",
        title_key="jobs.title",
        title_text="Jobs catalog",
        current="jobs",
        right_lead='        <span id="jb-credit" class="cl-credit muted" role="status" hidden></span>',
    )


_ACCOUNT_RIGHT_LEAD = """        <a href="/privacy" class="cl-back ac-topbar-extra" data-tooltip="Privacy policy">
          <span data-icon="shield" data-icon-size="14"></span><span data-i18n="nav.privacy">Privacy</span>
        </a>
        <button id="ac-logout" type="button" class="cl-back ac-topbar-extra" data-tooltip="Sign out">
          <span data-icon="log-out" data-icon-size="14"></span><span data-i18n="app.sign_out">Sign out</span>
        </button>"""


@app.route("/account")
@app.route("/settings")
def account_page() -> object:
    """Serve the account-settings page (auth-gated client-side by auth.js).

    Like ``/app``, the HTML shell is safe to serve pre-session — every
    ``/api/account/*`` call it makes is auth-required server-side.
    """
    return _serve_subpage(
        "account.html",
        title_icon="settings",
        title_key="account.title",
        title_text="Settings &amp; account",
        current="account",
        right_lead=_ACCOUNT_RIGHT_LEAD,
    )


@app.route("/privacy")
def privacy_page() -> object:
    """Serve the privacy policy (fully public, static, no per-user data).

    The ``PUBLIC_BASE_URL`` and ``PRIVACY_CONTACT`` tokens are filled so the
    controller's site URL and GDPR contact address are correct in production.
    """
    html = (STATIC_DIR / "privacy.html").read_text(encoding="utf-8")
    html = html.replace("__PUBLIC_BASE_URL__", PUBLIC_BASE_URL)
    html = html.replace("__PRIVACY_CONTACT__", PRIVACY_CONTACT)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/outline")
def api_outline() -> object:
    """Return outline entries (slug + label) the sidebar uses for jumps."""
    row = _resolve_active_cv(_doc_id_from_request())
    cv_path = _materialize_cv_to_path(row.get("yaml_blob") or "")
    try:
        cv = content_mod.load(cv_path, strict_required=False)
    except Exception as exc:
        return jsonify({"error": str(exc), "items": []}), 200
    finally:
        cv_path.unlink(missing_ok=True)
    return jsonify({"items": content_mod.section_outline(cv)})


@app.route("/api/preview")
@_preview_rate_limit
def api_preview() -> object:
    doc_id = _doc_id_from_request()
    try:
        row = _resolve_active_cv(doc_id)
    except Exception as exc:  # noqa: BLE001 - local demo preview should still render the bundled CV
        # An explicit id the caller doesn't own must 404 like production;
        # only the no-id / primary-demo-CV path may use the bundled preview.
        if _remote_pdf_storage_enabled() or not demo_mode_enabled() or doc_id not in (None, LOCAL_DEMO_CV_ID):
            raise
        app.logger.warning("using local demo CV preview fallback: %s", exc)
        row = _local_demo_cv_row()
    yaml_blob = row.get("yaml_blob") or ""
    try:
        _validate_cv_yaml(yaml_blob)
    except (ValueError, image_guard.ImageRejected) as exc:
        return jsonify({"error": f"YAML invalid: {exc}"}), 400
    cv_path = _materialize_cv_to_path(yaml_blob)
    theme = request.args.get("theme", "light")
    if theme not in {"light", "dark"}:
        theme = "light"
    mode = request.args.get("mode", "paged")
    if mode not in {"continuous", "paged"}:
        mode = "paged"
    density = (request.args.get("density") or "").strip().lower()
    if density and density not in {"tight", "normal", "airy"}:
        density = ""
    font = request.args.get("font") or None
    if font and font not in {"serif", "sans", "mono"}:
        font = None
    accent = (request.args.get("accent") or "").strip()
    if accent and not _THEME_HEX_RE.match(accent):
        accent = ""
    template = (request.args.get("template") or "").strip().lower()
    if template and template not in template_registry.available_template_ids():
        template = ""
    paper = (request.args.get("paper") or "").strip().lower()
    if paper:
        try:
            paper = css_base.normalize_paper(paper)
        except ValueError:
            paper = ""
    embedded = request.args.get("embed") == "1"
    thumbnail = request.args.get("thumb") == "1"
    try:
        html = render_cv_html(
            cv_path,
            theme=theme,
            mode=mode,
            density=density,
            accent_override=accent or None,
            font_override=font,
            template_override=template or None,
            paper_override=paper or None,
            embedded=embedded,
            thumbnail=thumbnail,
            owner_id=getattr(g, "owner_id", None),
        )
    finally:
        cv_path.unlink(missing_ok=True)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/import", methods=["POST"])
def api_import() -> object:
    """Convert an external resume into our YAML format.

    Body: ``{"format": "rendercv"|"text", "content": "..."}``.
    Response: ``{"yaml": "...", "bytes": N}``.
    """
    payload = request.get_json(silent=True) or {}
    fmt = (payload.get("format") or "").strip().lower()
    src = payload.get("content")
    if not src or not isinstance(src, str):
        return jsonify({"error": "Missing 'content' string."}), 400
    try:
        if fmt == "rendercv":
            cv = importers.from_rendercv(src)
        elif fmt in {"text", "plain", "plaintext"}:
            cv = importers.from_plain_text(src)
        else:
            return jsonify({"error": f"Unknown format {fmt!r}."}), 400
    except Exception as exc:  # noqa: BLE001 — surface message to UI
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 400
    return _yaml_response(cv)


@app.route("/api/extract/status")
def api_extract_status() -> object:
    """Tell the UI whether AI extraction is available for THIS user.

    ``ai_available`` is true when the active provider can run: no-secret local
    provider, connected BYO key, or Anthropic managed credits. Falls back
    gracefully if the status lookup fails.
    """
    try:
        profile = account_service.get_profile()
        provider = canonical_provider(profile.get("preferred_provider"))
        if provider not in SUPPORTED_PROVIDERS:
            provider = "anthropic"
        model = resolve_model(provider, profile.get("preferred_model"))
        status_all = llm_gateway.key_status_all()
        provider_status = (status_all.get("providers") or {}).get(provider, {})
        managed = status_all.get("managed_credits_remaining") or 0
        ai_available = bool(
            local_demo_store.active()
            or provider_status.get("connected")
            or (provider == "anthropic" and managed > 0)
        )
    except Exception:  # noqa: BLE001 — never 500 the badge probe
        provider = "anthropic"
        model = default_model_for(provider)
        provider_status = {"connected": False, "last4": None}
        managed = 0
        ai_available = False
    return jsonify(
        {
            "ai_available": ai_available,
            "provider": provider,
            "provider_label": provider_spec(provider).label,
            "model": model,
            "connected": provider_status.get("connected", False),
            "last4": provider_status.get("last4"),
            "auth_type": provider_status.get("auth_type"),
            "available": provider_status.get("available", provider_status.get("connected", False)),
            "managed_credits_remaining": managed,
        }
    )


@app.route("/api/polish/bullet", methods=["POST"])
@_ai_rate_limit
def api_polish_bullet() -> object:
    """Rewrite one bullet via Claude. Body: ``{text: str}``.

    Routes through ``resolve_llm`` for the per-request key + credit policy:
    BYO key → user's account; else a managed credit; else 402 needs_key.
    """
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty text"}), 400
    provider, model = _request_provider_model(payload)
    api_key, used_managed = llm_gateway.resolve_llm("polish_bullet", provider=provider)
    try:
        rewritten = ai_extract.polish_bullet(text, api_key=api_key, model=model, provider=provider)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("polish_bullet provider failure: %s", type(exc).__name__)
        return jsonify({"error": "AI polishing failed.", "code": "llm_failed"}), 502
    if not used_managed:
        llm_gateway.log_byo_usage("polish_bullet", provider=provider)
    return jsonify({"original": text, "rewritten": rewritten})


@app.route("/api/polish/section", methods=["POST"])
@_ai_rate_limit
def api_polish_section() -> object:
    """Rewrite the bullets in a role at once.

    Body: ``{bullets: [str], context: str}``. Context is freeform —
    typically ``f"Role: {role}\\nCompany: {company}\\nStack: {stack}"``
    so Claude can match the voice. Routed through ``resolve_llm`` (BYO key →
    managed credit → 402 needs_key).
    """
    payload = request.get_json(silent=True) or {}
    bullets = payload.get("bullets") or []
    if not isinstance(bullets, list) or not bullets:
        return jsonify({"error": "bullets must be a non-empty list"}), 400
    context = str(payload.get("context") or "")
    provider, model = _request_provider_model(payload)
    api_key, used_managed = llm_gateway.resolve_llm("polish_section", provider=provider)
    try:
        rewritten = ai_extract.polish_section(
            [str(b) for b in bullets], context=context, api_key=api_key,
            model=model, provider=provider,
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("polish_section provider failure: %s", type(exc).__name__)
        return jsonify({"error": "AI polishing failed.", "code": "llm_failed"}), 502
    if not used_managed:
        llm_gateway.log_byo_usage("polish_section", provider=provider)
    return jsonify({"original": bullets, "rewritten": rewritten})


@app.route("/api/extract", methods=["POST"])
@_ai_rate_limit
def api_extract() -> object:  # noqa: C901 — keep linear, easy to follow
    """Run Claude (with prompt-cached system prompt) on resume text.

    Accepts either:
      * JSON body: ``{"content": "..."}`` for pasted plain text
      * multipart/form-data with a ``file`` field for PDF/DOCX/TXT/MD upload

    Resolves a key via ``resolve_llm``: a BYO key or a managed credit drives
    the Claude path. When the user has neither (``needs_key``) or AI is
    operator-unavailable, we fall back to the heuristic parser rather than
    blocking — extraction has always promised "you always get something".
    Response: ``{"yaml": "...", "bytes": N, "source": "<provider>"|"heuristic"}``.
    """
    try:
        text = _resolve_extraction_input()
    except Exception:  # noqa: BLE001 — pypdf/zipfile raise on a corrupt upload
        # Keep the JSON API contract: a garbage PDF/DOCX is a 400, never
        # Werkzeug's HTML 500 (same regression class as the AuthError note at
        # the ``_handle_auth_error`` handler).
        return (
            jsonify({"error": f"Could not read file — corrupt or unsupported {_upload_fmt()}."}),
            400,
        )
    if text is None:
        return jsonify({"error": "Provide 'content' (JSON) or a 'file' (multipart)."}), 400
    if not text.strip():
        return jsonify({"error": "Extracted text is empty — file may be image-only."}), 400

    # Effective provider/model: JSON body / form field → profile pref → default.
    pm_src = request.get_json(silent=True)
    if not isinstance(pm_src, dict):
        pm_src = {"provider": request.form.get("provider"), "model": request.form.get("model")}
    provider, model = _request_provider_model(pm_src)

    # Resolve a key/credit; on no-key / unavailable, degrade to heuristic.
    api_key: str | None = None
    used_managed = False
    try:
        api_key, used_managed = llm_gateway.resolve_llm("extract", provider=provider)
    except llm_gateway.LLMUnavailable:
        api_key = None  # heuristic fallback below

    used_ai = False
    try:
        can_attempt_ai = api_key is not None or provider_spec(provider).auth_type == "none"
        if can_attempt_ai and ai_extract.is_available(api_key=api_key, provider=provider):
            cv = yaml_safe_load(
                ai_extract.extract(text, api_key=api_key, model=model, provider=provider)
            )
            if not isinstance(cv, dict):
                raise ValueError("AI returned non-mapping YAML.")
            used_ai = True
        else:
            cv = importers.from_plain_text(text)
    except Exception:  # noqa: BLE001
        # Last-resort fallback so the user always gets something.
        try:
            cv = importers.from_plain_text(text)
        except Exception:
            return jsonify({"error": "AI extraction failed; fallback parse failed.", "code": "extract_failed"}), 500
    if used_ai and not used_managed:
        llm_gateway.log_byo_usage("extract", provider=provider)
    return _yaml_response(cv, source=provider if used_ai else "heuristic")


def _upload_fmt() -> str:
    """Uppercased extension of the uploaded file, for the corrupt-file 400."""
    file_obj = request.files.get("file") if request.files else None
    name = (file_obj.filename or "") if file_obj else ""
    if "." in name:
        return name.rsplit(".", 1)[-1].upper()
    return "file"


def _resolve_extraction_input() -> str | None:
    """Pull text out of the request, preferring uploaded files over JSON."""
    file_obj = request.files.get("file") if request.files else None
    if file_obj and file_obj.filename:
        data = file_obj.read()
        name = (file_obj.filename or "").lower()
        if name.endswith(".pdf"):
            return ai_extract.extract_text_from_pdf(data)
        if name.endswith(".docx"):
            return ai_extract.extract_text_from_docx(data)
        return data.decode("utf-8", errors="replace")

    payload = request.get_json(silent=True) or {}
    src = payload.get("content")
    if isinstance(src, str):
        return src
    return None


def _yaml_response(cv: dict, source: str | None = None) -> object:
    """Common helper to dump a cv dict into our editor's YAML response."""
    import yaml as _yaml

    out_yaml = _yaml.safe_dump(
        cv,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=120,
    )
    body: dict[str, object] = {"yaml": out_yaml, "bytes": len(out_yaml.encode("utf-8"))}
    if source:
        body["source"] = source
    return jsonify(body)


def yaml_safe_load(text: str) -> object:
    import yaml as _yaml

    return _yaml.safe_load(text)


@app.route("/asset/<path:rel>")
def serve_asset(rel: str) -> object:
    if rel.replace("\\", "/").lstrip("/").startswith("design/photos/"):
        abort(404)
    abs_path = _safe_repo_path(rel)
    if not abs_path.exists() or not abs_path.is_file():
        abort(404)
    response = send_file(str(abs_path))
    # Sandboxed srcdoc previews intentionally have an opaque ``null`` origin.
    # Browser font loading enforces CORS even for public, same-service assets,
    # so permit only immutable public font files cross-origin. User photos are
    # excluded above and API/data responses never receive this wildcard.
    if abs_path.suffix.lower() in {".otf", ".ttf", ".woff", ".woff2"}:
        response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/repo/<path:rel>")
def serve_repo(rel: str) -> object:
    """Serve an ASSET file by its repo-relative path (e.g. a CV photo).

    Used by the editor for the photo thumbnail
    (``/repo/design/photos/<owner>/photo.jpg``). Scoped through
    :func:`_safe_repo_path`, so it can ONLY serve files under the allowed asset
    dirs (design/…) — application source, .env, configs and ``..`` traversal
    are all blocked (→ 404), even though this route is auth-exempt.
    """
    if rel.replace("\\", "/").lstrip("/").startswith("design/photos/"):
        abort(404)
    abs_path = _safe_repo_path(rel)
    if not abs_path.exists() or not abs_path.is_file():
        abort(404)
    return send_file(str(abs_path))


# Map a validated raster format → on-disk extension. The stored extension is
# derived from the MAGIC BYTES (image_guard), never the upload's filename.
_FORMAT_EXT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
_EXT_FORMAT = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


def _owner_photo_dir() -> Path:
    """Per-owner photo directory: ``design/photos/<owner_id>/``.

    Namespacing by the verified ``g.owner_id`` means two tenants can never
    overwrite each other's photo by uploading the same filename (the old shared
    ``design/photo.<ext>`` was a cross-tenant clobber). The id is a verified
    UUID from the JWT ``sub`` claim, so it's a safe single path segment.
    """
    owner = str(getattr(g, "owner_id", "") or "")
    # Defensive: keep it to a single safe segment (UUIDs already are).
    safe = "".join(ch for ch in owner if ch.isalnum() or ch in "-_") or "anon"
    return REPO / "design" / "photos" / safe


@contextmanager
def _owner_photo_render_lock(*, exclusive: bool):
    """Serialize photo mutation against every cacheable CV render process."""
    owner = str(uuid.UUID(str(getattr(g, "owner_id", ""))))
    lock_root = Path(tempfile.gettempdir()) / "onepager-photo-render-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = lock_root / f"{owner}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _remove_owner_photo_files() -> int:
    design_dir = _owner_photo_dir()
    n = 0
    if design_dir.is_dir():
        for stale in design_dir.glob("photo.*"):
            try:
                stale.unlink()
                n += 1
            except OSError:
                pass
    return n


def _owner_photo_identity_extension() -> str | None:
    """Return the stable per-owner photo suffix, surviving photo deletion."""
    design_dir = _owner_photo_dir()
    marker = design_dir / "format"
    try:
        marked = marker.read_text(encoding="ascii").strip().lower()
    except OSError:
        marked = ""
    if marked in _EXT_FORMAT:
        return marked
    if design_dir.is_dir():
        for candidate in sorted(design_dir.glob("photo.*")):
            suffix = candidate.suffix.lower()
            if suffix in _EXT_FORMAT:
                return suffix
    return None


def _persist_owner_photo_identity(extension: str) -> None:
    design_dir = _owner_photo_dir()
    design_dir.mkdir(parents=True, exist_ok=True)
    marker = design_dir / "format"
    fd, temp_name = tempfile.mkstemp(prefix=".format-", suffix=".tmp", dir=design_dir)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as tmp:
            tmp.write(extension)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, marker)
    finally:
        temp_path.unlink(missing_ok=True)


@app.route("/api/asset/photo", methods=["POST"])
@_photo_rate_limit
def api_upload_photo() -> object:
    """Save an uploaded photo under ``design/photos/<owner_id>/photo.<ext>``.

    Hardened: the photo is namespaced by the caller (no cross-tenant
    overwrite), the type is validated by MAGIC BYTES (PNG/JPEG/WebP only — SVG
    and everything else rejected, not by extension), and BOTH the byte size and
    the decoded pixel dimensions/area are capped BEFORE the bytes are ever
    handed to the renderer (blocks a data-URI-style decompression bomb).

    Returns the relative path the YAML should reference. The form auto-fills
    ``cv.yaml::photo:`` after a successful upload.
    """
    if local_demo_store.active():
        return (
            jsonify(
                {
                    "error": "Photo upload is disabled in the ephemeral demo.",
                    "code": "demo_photo_disabled",
                }
            ),
            409,
        )
    if not request.files or "file" not in request.files:
        return jsonify({"error": "no file part"}), 400
    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"error": "empty filename"}), 400

    uploaded_data = upload.read()
    try:
        input_info = image_guard.validate_image_bytes(uploaded_data)
    except image_guard.ImageRejected as exc:
        return jsonify({"error": str(exc), "code": "invalid_image"}), 400

    _ensure_account_access_strict()
    with _owner_photo_render_lock(exclusive=True):
        _ensure_account_access_strict()
        extension = _owner_photo_identity_extension() or _FORMAT_EXT[input_info.fmt]
        try:
            data, info = image_guard.sanitise_image_bytes(
                uploaded_data,
                output_format=_EXT_FORMAT[extension],
            )
        except image_guard.ImageRejected as exc:
            return jsonify({"error": str(exc), "code": "invalid_image"}), 400
        try:
            _persist_owner_photo_identity(extension)
        except OSError:
            return (
                jsonify(
                    {
                        "error": "Photo storage is temporarily unavailable.",
                        "code": "photo_storage_unavailable",
                    }
                ),
                503,
            )
        # Existing signed URLs are revoked for a privacy-sensitive photo
        # mutation. The exclusive lock prevents a render from publishing old
        # bytes between artifact cleanup and the atomic file replacement.
        _delete_owner_cv_pdf_artifacts(force=True)
        design_dir = _owner_photo_dir()
        design_dir.mkdir(parents=True, exist_ok=True)
        out_path = design_dir / f"photo{extension}"
        fd, temp_name = tempfile.mkstemp(
            prefix=".photo-",
            suffix=".tmp",
            dir=design_dir,
        )
        temp_path = Path(temp_name)
        try:
            try:
                with os.fdopen(fd, "wb") as tmp:
                    tmp.write(data)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.chmod(temp_path, 0o600)
                os.replace(temp_path, out_path)
            except OSError:
                return (
                    jsonify(
                        {
                            "error": "Photo storage is temporarily unavailable.",
                            "code": "photo_storage_unavailable",
                        }
                    ),
                    503,
                )
        finally:
            temp_path.unlink(missing_ok=True)
        for stale in design_dir.glob("photo.*"):
            if stale == out_path:
                continue
            try:
                stale.unlink()
            except OSError:
                pass
        rel = str(out_path.relative_to(REPO))
        return jsonify(
            {
                "path": rel,
                "bytes": out_path.stat().st_size,
                "width": info.width,
                "height": info.height,
            }
        )


@app.route("/api/asset/photo", methods=["DELETE"])
@_photo_rate_limit
def api_delete_photo() -> object:
    """Remove the caller's own design/photos/<owner_id>/photo.* on disk."""
    if local_demo_store.active():
        return (
            jsonify(
                {
                    "error": "Photo storage is disabled in the ephemeral demo.",
                    "code": "demo_photo_disabled",
                }
            ),
            409,
        )
    _ensure_account_access_strict()
    with _owner_photo_render_lock(exclusive=True):
        _ensure_account_access_strict()
        design_dir = _owner_photo_dir()
        has_photo = design_dir.is_dir() and any(design_dir.glob("photo.*"))
        if has_photo:
            extension = _owner_photo_identity_extension()
            if extension is None:
                raise _ArtifactCleanupUnavailable
            _persist_owner_photo_identity(extension)
            _delete_owner_cv_pdf_artifacts(force=True)
        n = _remove_owner_photo_files()
    return jsonify({"deleted": n})


@app.route("/api/asset/photo/<filename>", methods=["GET"])
def api_get_photo(filename: str) -> object:
    """Serve only the caller's own uploaded photo variant."""
    if local_demo_store.active():
        abort(404)
    _ensure_account_access_strict()
    if filename not in {"photo.png", "photo.jpg", "photo.jpeg", "photo.webp"}:
        abort(404)
    with _owner_photo_render_lock(exclusive=False):
        _ensure_account_access_strict()
        path = _owner_photo_dir() / filename
        if not path.exists() or not path.is_file():
            abort(404)
        try:
            payload = path.read_bytes()
        except OSError:
            abort(404)
    content_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(Path(filename).suffix.lower(), "application/octet-stream")
    return payload, 200, {"Content-Type": content_type, "Cache-Control": "private, no-store"}


@app.route("/api/build", methods=["POST"])
@_render_rate_limit
def api_build() -> object:
    """Render the user's primary CV to a 1-page PDF (R2-cached or streamed).

    Equivalent to running ``python3 engine/build.py`` from the shell, but
    in-process so the editor can offer a "Build PDF" button. On overflow
    or validation failure, returns 400 with a structured error so the UI
    can show a clear message. The CV is read from Postgres (the user's
    primary ``documents`` row) rather than disk. The overflow/1-page gate
    is byte-identical to the disk path — do not weaken it.

    Delivery (Stage 8) has TWO modes, chosen by ``pdf_store.r2_enabled()``:

      * **R2 configured** → render-on-change + content-hash cache: an UNCHANGED
        CV is NOT re-rendered (its non-stale artifact's signed URL is returned);
        a changed/new CV is rendered (semaphore-bounded), uploaded, and a fresh
        ``pdf_artifacts`` row recorded. Returns JSON ``{url, cached, page_count}``.
      * **R2 unset** (dev/test default) → graceful fallback: render and STREAM
        the PDF bytes exactly as before Stage 8. The app works fully with no R2.

    Either way the render + 1-page gate is the consolidated ``render_cv_pdf``
    service (via ``pdf_store.render_cv``, which bounds concurrency) — the same
    function the tailoring post-processor re-runs, so the overflow behaviour is
    byte-identical across the CLI, this route, and tailoring.
    """
    from render.pdf_service import CvYamlError

    payload = request.get_json(silent=True) or {}
    density = (payload.get("density") or "").strip().lower()
    if density and density not in {"tight", "normal", "airy"}:
        density = ""
    template = (payload.get("template") or "").strip().lower()
    if template and template not in template_registry.available_template_ids():
        template = ""
    paper = (payload.get("paper") or "").strip().lower()
    if paper:
        try:
            paper = css_base.normalize_paper(paper)
        except ValueError:
            paper = ""

    doc_id = _doc_id_from_request()
    try:
        row = _resolve_active_cv(doc_id)
    except Exception as exc:  # noqa: BLE001 - no-R2 local demo build should still render the bundled CV
        # An explicit id the caller doesn't own must 404 like production;
        # only the no-id / primary-demo-CV path may use the bundled CV.
        if _remote_pdf_storage_enabled() or not demo_mode_enabled() or doc_id not in (None, LOCAL_DEMO_CV_ID):
            raise
        app.logger.warning("using local demo CV build fallback: %s", exc)
        row = _local_demo_cv_row()
    yaml_blob = row.get("yaml_blob") or ""
    try:
        _validate_cv_yaml(yaml_blob)
    except (ValueError, image_guard.ImageRejected) as exc:
        return jsonify({"error": f"YAML invalid: {exc}"}), 400
    render_density = density or None
    render_template = template or None
    render_paper = paper or None
    cache_density = density or _effective_density_from_yaml(yaml_blob)
    cache_template = template or _effective_template_from_yaml(yaml_blob)
    cache_paper = paper or _effective_paper_from_yaml(yaml_blob)
    persisted_density = _effective_density_from_yaml(yaml_blob)
    persisted_template = _effective_template_from_yaml(yaml_blob)
    persisted_paper = _effective_paper_from_yaml(yaml_blob)
    if (
        (density and density != persisted_density)
        or (template and template != persisted_template)
        or (paper and paper != persisted_paper)
    ):
        return (
            jsonify(
                {
                    "error": "PDF appearance overrides must be saved before export",
                    "code": "stale_build_inputs",
                }
            ),
            409,
        )

    with _owner_photo_render_lock(exclusive=False):
        return _execute_cv_build(
            row=row,
            yaml_blob=yaml_blob,
            render_density=render_density,
            render_template=render_template,
            render_paper=render_paper,
            cache_density=cache_density,
            cache_template=cache_template,
            cache_paper=cache_paper,
            cv_yaml_error=CvYamlError,
        )


def _execute_cv_build(
    *,
    row: dict[str, Any],
    yaml_blob: str,
    render_density: str | None,
    render_template: str | None,
    render_paper: str | None,
    cache_density: str,
    cache_template: str,
    cache_paper: str,
    cv_yaml_error: type[Exception],
) -> object:
    """Build one CV while the caller holds the owner's shared photo lock."""
    if _remote_pdf_storage_enabled():
        try:
            rendered, built = pdf_store.build_cv_pdf(
                document_id=row["id"],
                owner_id=g.owner_id,
                yaml_blob=yaml_blob,
                accent=row.get("accent") or "",
                font=row.get("font") or "",
                density=cache_density,
                template=cache_template,
                paper=cache_paper,
                document_revision=row.get("updated_at"),
            )
        except cv_yaml_error as exc:
            return jsonify({"error": f"YAML invalid: {exc}"}), 400
        except pdf_store.ArtifactRevisionConflict:
            return (
                jsonify(
                    {
                        "error": "CV changed during PDF build",
                        "code": "revision_conflict",
                    }
                ),
                409,
            )
        except pdf_store.ArtifactAccountUnavailable:
            return jsonify({"error": "Account is unavailable.", "code": "account_unavailable"}), 409
        except pdf_store.StorageError:
            return _authorized_cv_stream(
                row=row,
                yaml_blob=yaml_blob,
                density=render_density,
                template=render_template,
                paper=render_paper,
            )
        except Exception as exc:  # noqa: BLE001 - normalized render failure
            return jsonify(
                {"error": f"render failed: {type(exc).__name__}: {exc}"}
            ), 503
        if rendered.overflowed:
            return _overflow_response(rendered)
        authorization_error = _export_authorization_error(row, "CV")
        if authorization_error is not None:
            return authorization_error
        analytics.capture(
            analytics.EVENT_FIRST_EXPORT,
            getattr(g, "owner_id", "") or "",
        )
        return jsonify(
            {
                "url": built.url,
                "cached": built.cached,
                "page_count": built.page_count,
            }
        )
    return _authorized_cv_stream(
        row=row,
        yaml_blob=yaml_blob,
        density=render_density,
        template=render_template,
        paper=render_paper,
    )


def _authorized_cv_stream(
    *,
    row: dict[str, Any],
    yaml_blob: str,
    density: str | None,
    template: str | None,
    paper: str | None,
) -> object:
    authorization_error = _export_authorization_error(row, "CV")
    if authorization_error is not None:
        return authorization_error
    return _stream_cv_fallback(yaml_blob, density, template, paper, row=row)


def _overflow_response(rendered) -> object:
    """Uniform 400 JSON naming the first section that spilled onto page 2."""
    return (
        jsonify(
            {
                "error": "overflow",
                "pages": rendered.page_count,
                "section": rendered.overflow_label,
                "hint": (
                    f"CV overflowed to {rendered.page_count} pages. First section "
                    f"on page 2: {rendered.overflow_label!r}. Try density=tight or "
                    "trim a bullet."
                ),
            }
        ),
        400,
    )


def _stream_cv_fallback(
    yaml_blob: str,
    density: str | None,
    template: str | None = None,
    paper: str | None = None,
    *,
    row: dict[str, Any] | None = None,
) -> object:
    """Render the CV (semaphore-bounded) and stream the bytes — the no-R2 path.

    Used when R2 is unconfigured (dev/test) and as a degrade path if an R2
    operation fails mid-build. The 1-page gate is identical to the R2 path.
    """
    from render.pdf_service import CvYamlError

    try:
        rendered = pdf_store.render_cv(yaml_blob, density=density, template=template, paper=paper)
    except CvYamlError as exc:
        return jsonify({"error": f"YAML invalid: {exc}"}), 400
    except Exception as exc:  # noqa: BLE001 — render environment failure -> JSON, not HTML 500
        return jsonify({"error": f"render failed: {type(exc).__name__}: {exc}"}), 503
    if rendered.overflowed:
        return _overflow_response(rendered)
    if row is not None:
        authorization_error = _export_authorization_error(row, "CV")
        if authorization_error is not None:
            return authorization_error
    # Activation signal: a successful PDF export (the no-R2 / degrade path).
    # No-op unless POSTHOG_KEY is set; best-effort, never blocks/raises.
    analytics.capture(analytics.EVENT_FIRST_EXPORT, getattr(g, "owner_id", "") or "")
    return (
        rendered.pdf_bytes,
        200,
        {
            "Content-Type": "application/pdf",
            "Content-Disposition": pdf_store._pdf_disposition("cv.pdf"),
        },
    )


@app.route("/api/schema")
def api_schema() -> object:
    """Expose the section registry to the frontend.

    The form (``form.js``) and outline (``app.js``) consume this to build
    sections dynamically. Adding a SectionDef in
    ``engine/render/sections.py`` shows up here automatically.
    """
    return jsonify({"sections": sections_mod.to_json_dict()})


@app.route("/api/templates")
def api_templates() -> object:
    """Return built-in resume layout templates."""
    return jsonify({
        "templates": template_registry.public_templates(),
        "default_template": template_registry.DEFAULT_TEMPLATE,
    })


THEMES_DIR = REPO / "themes"
_THEME_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _normalise_theme(data: dict, *, fallback_name: str = "Theme") -> dict[str, str] | None:
    """Return validated theme metadata, or None for invalid theme JSON."""
    name = str(data.get("name") or fallback_name).strip()
    accent = str(data.get("accent") or "").strip()
    font = str(data.get("font") or "serif").strip().lower()
    density = str(data.get("density") or "normal").strip().lower()
    paper = str(data.get("paper") or "white").strip().lower()
    if not name or not _THEME_HEX_RE.match(accent):
        return None
    if font not in {"serif", "sans", "mono"}:
        return None
    if density not in {"tight", "normal", "airy"}:
        return None
    try:
        paper = css_base.normalize_paper(paper)
    except ValueError:
        return None
    return {"name": name, "accent": accent, "font": font, "density": density, "paper": paper}


@app.route("/api/themes/import", methods=["POST"])
def api_themes_import() -> object:
    """Reject remote theme imports.

    Theme presets are reviewed source files shipped with the application.
    A multi-tenant request must never fetch an arbitrary URL or write into the
    shared application tree. Custom appearance remains document-scoped through
    the existing accent, font, density, and paper controls.
    """
    return (
        jsonify(
            {
                "error": "Remote theme import is disabled; use the document appearance controls.",
                "code": "theme_import_disabled",
            }
        ),
        404,
    )


@app.route("/api/themes")
def api_themes() -> object:
    """List the JSON theme presets in ``themes/``.

    Each theme is a JSON file at the repo root's ``themes/`` folder.
    Schema: ``{name, accent, font, density, paper?}`` — extra fields ignored.
    """
    import json as _json

    out: list[dict[str, object]] = []
    if THEMES_DIR.is_dir():
        for path in sorted(THEMES_DIR.glob("*.json")):
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            theme = _normalise_theme(data, fallback_name=path.stem.title())
            if theme is None:
                continue
            out.append({
                "id": path.stem,
                **theme,
            })
    return jsonify({"themes": out})


def _cv_file_display_path() -> Path:
    """Return a safe startup-display path for internal or absolute CV pins."""
    try:
        return CV_FILE.relative_to(REPO)
    except ValueError:
        return CV_FILE


def main() -> int:
    port = DEFAULT_PORT
    print(f"CV editor → http://127.0.0.1:{port}")
    print(f"  seed YAML: {_cv_file_display_path()}")
    if demo_mode_enabled():
        print("  storage: process memory (resets when this server stops)")
    else:
        print("  storage: Supabase documents (configuration required)")
    print("  Ctrl+C to stop")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
