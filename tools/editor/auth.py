"""Supabase JWT authentication for the editor gateway (Stage 2).

The browser logs in with the Supabase JS client and sends the resulting
access token as ``Authorization: Bearer <jwt>`` on every ``/api/*`` call.
This module verifies that token *locally* against Supabase's JWKS (no
network round-trip to GoTrue per request) and, on success, stashes the
caller's identity on Flask's request-global ``g``:

    g.owner_id     — the verified ``sub`` claim (== auth.uid() in Postgres)
    g.email        — the ``email`` claim, if present
    g.access_token — the raw bearer token (passed to PostgREST so RLS fires)

Hard security rules (a regression here is a tenant-isolation break):

  * MANDATORY algorithm allow-list ``["ES256", "RS256"]`` for real Supabase
    JWTs. We NEVER accept ``none`` or ``HS256`` on that path — an HS256 token
    verified against a *public* JWK would let an attacker forge tokens.
    Supabase projects created today sign with ES256 (asymmetric), so the
    public JWKS is safe. The only unsigned token accepted is the local demo
    sentinel, and only while ``ONEPAGER_DEMO_MODE`` is explicitly enabled.
  * ``audience="authenticated"`` and issuer ``${SUPABASE_URL}/auth/v1``.
  * ``require=["exp", "sub"]`` — a token without an expiry or subject is
    rejected outright.
  * ``owner_id`` is derived ONLY from the verified ``sub`` claim, never from
    the request body.

On any verification failure the guard returns a 401 JSON response; it never
leaks the underlying token or claim values.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from functools import lru_cache

import jwt
from flask import Response, current_app, g, has_app_context, jsonify, request
from jwt import PyJWKClient

try:
    from demo_policy import demo_mode_enabled as _demo_mode_enabled
except ImportError:  # pragma: no cover - unusual package import roots
    from engine.demo_policy import demo_mode_enabled as _demo_mode_enabled

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (read from the environment — see .env, never hardcoded).
# ---------------------------------------------------------------------------
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_JWKS_URL = os.environ.get("SUPABASE_JWKS_URL") or (
    f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json" if SUPABASE_URL else ""
)
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY") or ""
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""

# Asymmetric algorithms ONLY. Adding HS256 here would be a security hole
# (symmetric verification against a public key == forgeable tokens).
ALLOWED_ALGS = ["ES256", "RS256"]
JWT_AUDIENCE = "authenticated"
JWT_ISSUER = f"{SUPABASE_URL}/auth/v1" if SUPABASE_URL else ""
LOCAL_DEMO_OWNER_ID = "00000000-0000-4000-8000-000000000042"
LOCAL_DEMO_EMAIL = "demo@onepager.test"
LOCAL_DEMO_ISSUER = "onepager-local-demo"

# Routes that do NOT require a bearer token. Everything under /api/* DOES
# (that's where all per-user data lives — enforced in :func:`require_auth`).
#
# Exempt, because none of these expose per-user data and several are loaded
# by the browser via plain URLs that can't carry a bearer header:
#   /static/  — the editor's own JS/CSS (login page must load pre-session)
#   /asset/   — repo files (fonts) referenced by the preview's CSS url()
#   /repo/    — repo files (e.g. the shared demo photo) referenced by <img>
# ``/auth/config`` returns only the publishable (browser-safe) Supabase URL
# + key so the login page can boot the Supabase JS client.
# NOTE: per-user photo isolation is Stage 8 (R2-backed); Stage 2 keeps the
# existing shared demo-asset behavior for /asset and /repo.
# Stage 9 adds public, install-free routes that must be reachable without a
# session: the editor HTML at ``/app`` (auth is enforced client-side by
# auth.js + server-side on every ``/api/*`` call — the HTML shell itself is
# safe to serve), the SEO routes ``/robots.txt`` + ``/sitemap.xml``, and the
# i18n string table ``/api/i18n`` (the landing page fetches it pre-session).
#   /account, /settings — the account-settings shell (gated client-side by auth.js;
#               every /api/account/* call it makes is auth-required server-side)
#   /privacy  — a fully public, static privacy policy (no per-user data at all)
_EXEMPT_PATHS = frozenset(
    {
        "/",
        "/app",
        "/login",
        "/cover-letters",
        "/tailor",
        "/jobs",
        "/account",
        "/settings",
        "/privacy",
        "/healthz",
        "/favicon.ico",
        "/auth/config",
        # DEV-ONLY one-click demo login. It IS the login path (no session yet),
        # so it must be reachable without a bearer token — exactly like
        # /auth/config. It is independently GATED behind ONEPAGER_DEMO_MODE in
        # the route handler (404 when off), so being auth-exempt exposes nothing
        # in production.
        "/auth/demo-login",
        "/robots.txt",
        "/sitemap.xml",
        "/api/i18n",
        "/api/themes",
        "/api/templates",
        "/api/ats-profiles",
    }
)
_EXEMPT_PREFIXES = ("/static/", "/asset/", "/repo/")


class AuthError(Exception):
    """Raised internally when a request cannot be authenticated."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _b64_json(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode_json(part: str) -> dict:
    padding = "=" * (-len(part) % 4)
    raw = base64.urlsafe_b64decode((part + padding).encode("ascii"))
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def make_local_demo_access_token(ttl_seconds: int = 3600) -> str:
    """Return a local-only JWT-shaped token for demo-mode browser tests.

    It is intentionally accepted only by :func:`verify_token` while
    ``ONEPAGER_DEMO_MODE`` is on. The normal Supabase path still rejects
    unsigned tokens and still allow-lists asymmetric algorithms only.
    """
    now = int(time.time())
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "aud": JWT_AUDIENCE,
        "email": LOCAL_DEMO_EMAIL,
        "exp": now + ttl_seconds,
        "iat": now,
        "iss": LOCAL_DEMO_ISSUER,
        "sub": LOCAL_DEMO_OWNER_ID,
    }
    return f"{_b64_json(header)}.{_b64_json(payload)}.local-demo"


def _local_demo_claims(token: str) -> dict | None:
    if not _demo_mode_enabled():
        return None
    try:
        header64, payload64, signature = token.split(".", 2)
        header = _b64_decode_json(header64)
        payload = _b64_decode_json(payload64)
    except Exception:
        return None
    if signature != "local-demo" or header.get("alg") != "none":
        return None
    if payload.get("iss") != LOCAL_DEMO_ISSUER or payload.get("aud") != JWT_AUDIENCE:
        return None
    if payload.get("sub") != LOCAL_DEMO_OWNER_ID:
        return None
    if int(payload.get("exp") or 0) <= int(time.time()):
        raise AuthError("local demo token expired")
    return payload


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    """Return a cached :class:`PyJWKClient` for the project's JWKS endpoint.

    ``PyJWKClient`` itself caches fetched signing keys, so a single shared
    instance both avoids re-fetching the JWKS on every request and survives
    key rotation (it refetches when it sees an unknown ``kid``).
    """
    if not SUPABASE_JWKS_URL:
        raise AuthError("auth not configured: SUPABASE_JWKS_URL is empty", status=500)
    return PyJWKClient(SUPABASE_JWKS_URL)


def _bearer_token() -> str:
    """Extract the bearer token from the Authorization header, or raise."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise AuthError("missing or malformed Authorization header")
    token = header[len("Bearer ") :].strip()
    if not token:
        raise AuthError("empty bearer token")
    return token


def verify_token(token: str) -> dict:
    """Verify ``token`` against the Supabase JWKS and return its claims.

    Raises :class:`AuthError` on any failure. The algorithm allow-list,
    audience, issuer, and required claims are all enforced here.
    """
    local_demo = _local_demo_claims(token)
    if local_demo is not None:
        return local_demo
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=ALLOWED_ALGS,
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": ["exp", "sub"]},
        )
    except AuthError:
        raise
    except jwt.PyJWTError as exc:
        # Includes ExpiredSignatureError, InvalidAudienceError,
        # InvalidIssuerError, InvalidAlgorithmError, etc. We surface a STATIC
        # message — the exception class name reveals infra state (e.g.
        # PyJWKClientConnectionError) — and log only the class name
        # server-side (never the token or claim contents).
        _log.warning("rejecting bearer token: %s", type(exc).__name__)
        raise AuthError("invalid token") from exc
    except Exception as exc:  # noqa: BLE001 — e.g. JWKS fetch/network errors
        _log.warning("token verification failed: %s", type(exc).__name__)
        raise AuthError("invalid token") from exc

    sub = claims.get("sub")
    if not sub:
        raise AuthError("token has no subject")
    return claims


def _is_exempt(path: str) -> bool:
    if path in _EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def require_auth() -> Response | None:
    """Flask ``before_request`` guard.

    Exempts the login page, static assets, and ``/healthz``. For every
    other route — in particular everything under ``/api/*`` — a valid
    Supabase bearer token is required. On success the verified identity is
    stored on ``g``; on failure a 401 JSON response is returned (which
    short-circuits the request).
    """
    # Always allow CORS preflight through without auth.
    if request.method == "OPTIONS":
        return None
    if _is_exempt(request.path):
        return None
    try:
        token = _bearer_token()
        claims = verify_token(token)
    except AuthError as exc:
        return jsonify({"error": "unauthorized", "detail": exc.message}), exc.status
    g.owner_id = claims["sub"]
    g.email = claims.get("email")
    g.access_token = token
    return None


def auth_g():
    """Return Flask's request-global ``g`` (indirection eases testing).

    Lets modules read ``g.owner_id`` / ``g.email`` through ``auth_mod`` so a
    test can stub identity without importing Flask directly.
    """
    return g


def user_supabase():
    """Build a supabase-py client bound to the current user's JWT.

    PostgREST receives ``g.access_token`` as its Authorization, so every
    query runs as the authenticated user and row-level security fires
    (``owner_id = auth.uid()``). Never call this outside an authenticated
    request — it relies on ``require_auth`` having populated ``g``.
    """
    from supabase import create_client

    token = getattr(g, "access_token", None)
    if not token:
        raise AuthError("no access token on request", status=401)
    if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
        raise AuthError(
            "supabase not configured: SUPABASE_URL/SUPABASE_PUBLISHABLE_KEY missing",
            status=500,
        )
    client = create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)
    # Run PostgREST requests under the user's JWT so RLS scopes every row
    # to auth.uid(). This is the single seam that enforces tenant isolation.
    client.postgrest.auth(token)
    return client


def server_supabase():
    """Build a server-owned Supabase client for narrow server-only writes.

    Browser sessions use :func:`user_supabase`; this helper is for rows whose
    integrity is owned by reviewed server flows, such as packet lineage and
    one-time review claims. Production requires ``SUPABASE_SERVICE_ROLE_KEY``.
    Tests and explicit local demo mode fall back to the user/fake client so the
    same service code remains runnable without a real Supabase project.
    """
    if not SUPABASE_SERVICE_ROLE_KEY:
        if (has_app_context() and current_app.config.get("TESTING")) or _demo_mode_enabled():
            return user_supabase()
        raise AuthError(
            "supabase not configured: SUPABASE_SERVICE_ROLE_KEY missing",
            status=500,
        )
    if not SUPABASE_URL:
        raise AuthError("supabase not configured: SUPABASE_URL missing", status=500)
    from supabase import create_client

    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
