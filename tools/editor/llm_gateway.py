"""LLM key + managed-credit gateway (Stage 4).

This is the server-side policy layer between the editor's AI routes and the
provider call. It answers one question per request: *with which API key, if
any, may this user run an AI generation right now?*

Resolution order (:func:`resolve_llm`):

  1. The user has a stored BYO key  → decrypt it, use it (managed credit NOT
     consumed). This is the user's own account / own spend.
  2. No BYO key, but managed credits remain → call the SECURITY DEFINER RPC
     ``consume_managed_credit(kind)``. The RPC atomically decrements the
     balance and writes the audit row; only if it returns ``true`` do we use
     the OPERATOR key (``ANTHROPIC_API_KEY``). If the operator key is blank
     (dev), we surface a 503 so non-AI tests still pass without a live key.
  3. Otherwise → a 402/403 ``needs_key`` JSON telling the user to add a key.

Why the RPC instead of a client-side decrement: a user must never be able to
raise their own ``managed_credits_remaining``. The column UPDATE grant is
revoked from ``authenticated`` (see migration 0002), so the *only* way the
balance moves is through the definer function — which only ever decrements,
guarded by ``> 0`` and scoped to ``auth.uid()``.

Key validation (:func:`validate_provider_key`) makes a minimal provider call
so an obviously-bad key is rejected on entry rather than at first use. It's a
standalone function so tests can mock it without a live provider.
"""
from __future__ import annotations

import os

import auth as auth_mod
import local_demo_store
import vault

try:
    # engine/ is on sys.path in every entrypoint (server boot, tests).
    from llm.client import (
        DEMO_STUB_API_KEY,
        GEMINI_TAILOR_MODEL,
        SUPPORTED_PROVIDERS,
        canonical_provider,
        demo_mode_enabled,
        ollama_health,
        provider_spec,
    )
except ImportError:  # pragma: no cover - unusual import roots
    from engine.llm.client import (
        DEMO_STUB_API_KEY,
        GEMINI_TAILOR_MODEL,
        SUPPORTED_PROVIDERS,
        canonical_provider,
        demo_mode_enabled,
        ollama_health,
        provider_spec,
    )


class LLMUnavailable(Exception):
    """Raised by :func:`resolve_llm` when no usable key is available.

    Carries the HTTP ``status`` and JSON ``payload`` the route should return,
    so credit/key policy decisions map cleanly to responses.
    """

    def __init__(self, status: int, payload: dict) -> None:
        super().__init__(payload.get("error", "LLM unavailable"))
        self.status = status
        self.payload = payload


class KeyValidationError(Exception):
    """Raised when a user-supplied API key fails the provider probe."""


# ---------------------------------------------------------------------------
# Key validation (mockable provider probe).
# ---------------------------------------------------------------------------
def validate_provider_key(provider: str, key: str) -> None:
    """Probe ``key`` against ``provider`` with a minimal call.

    Raises :class:`KeyValidationError` if the key is invalid/unauthorized.
    Kept as a single standalone function so tests can monkeypatch it (no live
    provider needed). Anthropic + Gemini are the wired providers; a Google key
    can ONLY be saved because of the Gemini branch here (the route gates entry
    on this probe), so this is one of the Stage-4 security must-fixes. The key
    is NEVER echoed in any error message.
    """
    p = canonical_provider(provider)
    if p == "anthropic":
        _probe_anthropic(key)
        return
    if p == "gemini":
        _probe_gemini(key)
        return
    spec = provider_spec(p)
    if spec.auth_type == "none":
        return
    if spec.validation == "openai_compatible":
        _probe_openai_compatible_shape(p, key)
        return
    raise KeyValidationError(f"Unsupported provider {provider!r}.")


def _probe_anthropic(key: str) -> None:
    """Validate an Anthropic key with the cheapest authenticated call."""
    if not key or not key.startswith("sk-"):
        raise KeyValidationError("That doesn't look like an Anthropic key (expected sk-ant-…).")

    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - import guard
        raise KeyValidationError("anthropic SDK is not installed on the server.") from exc

    client = anthropic.Anthropic(api_key=key)
    try:
        # Cheapest possible authenticated call: 1 token, trivial prompt. A bad
        # key raises AuthenticationError; we translate to a clean message and
        # never echo the key.
        client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1,
            messages=[{"role": "user", "content": "ok"}],
        )
    except anthropic.AuthenticationError as exc:
        raise KeyValidationError("Anthropic rejected that key (authentication failed).") from exc
    except anthropic.PermissionDeniedError as exc:
        raise KeyValidationError("That key lacks permission for the Claude API.") from exc
    except anthropic.APIStatusError as exc:
        # 4xx other than auth → treat as a bad key/request; 5xx → provider
        # hiccup, but we still can't validate, so report a soft failure.
        raise KeyValidationError(f"Could not validate the key ({exc.status_code}).") from exc
    except anthropic.APIConnectionError as exc:
        raise KeyValidationError("Could not reach Anthropic to validate the key.") from exc


def _probe_gemini(key: str) -> None:
    """Validate a Google (Gemini) key with a cheap authenticated probe.

    Shape check first (a Gemini Developer API key starts with ``AIza``) so an
    obvious paste error is rejected without spending a call, mirroring the
    ``sk-`` check for Anthropic. Then a 1-token generation on the cheapest tier.
    A bad/unauthorized key surfaces as a ``ClientError`` with ``.code`` in
    {400 (API_KEY_INVALID), 401, 403} — there's no dedicated auth-error class
    like Anthropic's, so we branch on the code. The key is NEVER echoed.
    """
    if not key or not key.startswith("AIza"):
        raise KeyValidationError("That doesn't look like a Google AI key (expected AIza…).")

    try:
        from google import genai
        from google.genai import errors, types
    except ImportError as exc:  # pragma: no cover - import guard
        raise KeyValidationError("google-genai SDK is not installed on the server.") from exc

    # Pass api_key explicitly — never fall through to an ambient GOOGLE_API_KEY,
    # which would let a bogus user key "validate" against the operator's quota.
    client = genai.Client(api_key=key)
    try:
        client.models.generate_content(
            model=GEMINI_TAILOR_MODEL,
            contents="ok",
            config=types.GenerateContentConfig(max_output_tokens=1),
        )
    except errors.ClientError as exc:
        code = getattr(exc, "code", None)
        if code in (400, 401, 403):
            raise KeyValidationError("Google rejected that key (authentication failed).") from exc
        raise KeyValidationError(f"Could not validate the key ({code}).") from exc
    except errors.ServerError as exc:  # pragma: no cover - provider hiccup
        raise KeyValidationError("Could not reach Google to validate the key.") from exc


def _probe_openai_compatible_shape(provider: str, key: str) -> None:
    """Fail-closed shape check for OpenAI-compatible BYO keys.

    Hosted providers differ in exact key format and model availability; a live
    probe would spend user quota during setup. This check rejects obvious paste
    errors without echoing the secret. The first real generation still goes
    through the same sanitized provider-error path.
    """
    if not isinstance(key, str) or not key.strip():
        raise KeyValidationError(f"{provider} key is empty.")
    token = key.strip()
    if any(ch.isspace() for ch in token) or len(token) < 16:
        raise KeyValidationError(f"That doesn't look like a {provider} API key.")


# ---------------------------------------------------------------------------
# Stored-key persistence (RLS-scoped to the caller via user_supabase()).
# ---------------------------------------------------------------------------
def _stored_key_row(provider: str = "anthropic") -> dict | None:
    """The caller's encrypted key row for ``provider``, or None.

    RLS scopes the SELECT to ``owner_id = auth.uid()`` so this is always the
    caller's own row. The provider is normalized (``google`` → ``gemini``) so a
    lookup always hits the single canonical row.
    """
    if local_demo_store.active():
        # The reserved demo owner never has durable BYO keys. Its alg:none
        # token cannot authorize against Supabase, so even a passive lookup is
        # both misleading and a source of hanging external sockets.
        return None
    sb = auth_mod.user_supabase()
    resp = (
        sb.table("user_llm_keys")
        .select("provider, ciphertext, nonce, wrapped_dek, dek_nonce, last4")
        .eq("provider", canonical_provider(provider))
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def save_key(owner_id: str, provider: str, plaintext: str) -> str:
    """Encrypt + upsert the caller's key for ``provider``. Returns ``last4``.

    The plaintext is encrypted with :func:`vault.encrypt_key` (AAD=owner_id)
    and only the ciphertext columns are written. ``owner_id`` defaults to
    ``auth.uid()`` in the schema and the unique (owner_id, provider) makes this
    an upsert; the provider is normalized (``google`` → ``gemini``) so the
    Gemini key always upserts onto the SAME canonical row (never two split
    ``google``/``gemini`` rows). Never stores/returns/logs the plaintext.

    In demo mode this raises :class:`LLMUnavailable` (400) — keys are never
    stored there (see below) and pretending otherwise contradicted the key
    status endpoint.
    """
    if local_demo_store.active():
        # The demo owner can never authorize against the real Supabase (its
        # alg:none token is rejected), so an upsert here raises httpx.ConnectError
        # → HTML 500 (hard rule 5). The demo shell uses managed credits and never
        # needs a durably-stored BYO key. Returning a fake ``last4`` here made
        # the save report {ok:true} while GET /api/key immediately showed
        # connected:false — a direct UI contradiction. Fail loudly instead:
        # server.py maps LLMUnavailable to a clean JSON error the Account UI
        # already renders.
        raise LLMUnavailable(
            400,
            {
                "error": "API keys are not stored in demo mode — the demo uses managed credits.",
                "code": "demo_mode_key_not_stored",
            },
        )
    enc = vault.encrypt_key(owner_id, plaintext)
    payload = {"provider": canonical_provider(provider), **enc.as_row()}
    sb = auth_mod.user_supabase()
    sb.table("user_llm_keys").upsert(
        payload, on_conflict="owner_id,provider"
    ).execute()
    return enc.last4


def delete_key(provider: str = "anthropic") -> bool:
    """Remove the caller's stored key for ``provider``. True if a row went."""
    if local_demo_store.active():
        # No durable demo key store (see save_key) → nothing to delete, and a
        # real Supabase delete would 500 for the demo owner.
        return False
    sb = auth_mod.user_supabase()
    resp = (
        sb.table("user_llm_keys")
        .delete()
        .eq("provider", canonical_provider(provider))
        .execute()
    )
    return bool(resp.data or [])


def key_status(provider: str = "anthropic") -> dict:
    """Connection + credit summary for the caller's AI panel.

    Returns ``{connected, provider, last4, managed_credits_remaining}``.
    Reads credits from the caller's own profile row (RLS-scoped).
    """
    row = _stored_key_row(provider)
    credits = _managed_credits_remaining()
    return {
        "connected": row is not None,
        "provider": provider,
        "last4": (row or {}).get("last4"),
        "managed_credits_remaining": credits,
    }


def key_status_all() -> dict:
    """Per-provider connection summary + the shared managed-credit balance.

    The AI-settings UI needs the connection state of BOTH providers at once, so
    this returns one entry per supported provider plus the (Anthropic-only)
    managed-credit count. RLS scopes the row read to the caller. Only the
    non-secret ``last4`` hint is exposed — never any ciphertext.
    """
    if local_demo_store.active():
        # Demo AI uses the offline StubLLMClient through resolve_llm(); it has
        # no stored keys or spendable managed credits. Keep passive status
        # reads deterministic and entirely process-local.
        providers = {}
        for p in SUPPORTED_PROVIDERS:
            spec = provider_spec(p)
            providers[p] = {
                "connected": False,
                "last4": None,
                "auth_type": spec.auth_type,
            }
            if spec.auth_type == "none":
                providers[p]["available"] = False
        return {
            "providers": providers,
            "managed_credits_remaining": 0,
            "demo": True,
        }

    sb = auth_mod.user_supabase()
    rows = sb.table("user_llm_keys").select("provider, last4").execute().data or []
    by_provider = {
        canonical_provider(r.get("provider")): {
            "connected": True,
            "last4": r.get("last4"),
        }
        for r in rows
    }
    providers = {}
    for p in SUPPORTED_PROVIDERS:
        spec = provider_spec(p)
        if spec.auth_type == "none":
            health = ollama_health() if p == "ollama" else {"available": True}
            providers[p] = {
                "connected": bool(health.get("available")),
                "last4": None,
                "auth_type": "none",
                "available": bool(health.get("available")),
                "version": health.get("version"),
                "error": health.get("error"),
            }
        else:
            providers[p] = {
                **by_provider.get(p, {"connected": False, "last4": None}),
                "auth_type": spec.auth_type,
            }
    return {
        "providers": providers,
        "managed_credits_remaining": _managed_credits_remaining(),
    }


def _managed_credits_remaining() -> int:
    """The caller's remaining managed credits (0 if no profile row)."""
    if local_demo_store.active():
        # Demo generation is a local stub and must never inspect or spend a
        # real tenant's credit ledger.
        return 0
    sb = auth_mod.user_supabase()
    resp = (
        sb.table("profiles")
        .select("managed_credits_remaining")
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return 0
    value = rows[0].get("managed_credits_remaining")
    return int(value) if value is not None else 0


# ---------------------------------------------------------------------------
# The resolution decision.
# ---------------------------------------------------------------------------
def _operator_key_for(provider: str) -> str:
    """The operator (managed-credit) key for ``provider``, or ``""``.

    Managed credits are ANTHROPIC-ONLY in v1, so only Anthropic has an operator
    key. We deliberately read it from ``ANTHROPIC_API_KEY`` (a non-magic name) —
    the Gemini SDK auto-reads ``GOOGLE_API_KEY``/``GEMINI_API_KEY`` from the
    env, so we must NOT keep an operator Gemini key under those names (it would
    silently shadow a user's BYO key). Gemini therefore returns ``""`` here →
    the credit branch is never taken for Gemini.
    """
    if canonical_provider(provider) == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY") or ""
    return ""


def resolve_llm(kind: str, *, provider: str = "anthropic") -> tuple[str, bool]:
    """Decide which API key to use for an AI call of ``kind`` for ``provider``.

    Returns ``(api_key, used_managed_credit)``:
      * BYO key for ``provider`` present → (decrypted key, False)
      * else (Anthropic only) credits remain & RPC succeeds → (operator key, True)
      * else → raises :class:`LLMUnavailable` (402 needs_key, or 503 if the
        Anthropic operator key is blank).

    SECURITY: the key returned is decrypted from the row stored UNDER
    ``provider`` (canonicalized). The caller MUST hand this key to
    ``make_client(provider, key)`` with the SAME provider — never cross-vendor
    (a Google key to Anthropic, or vice-versa). Managed credits are Anthropic-
    only, so a managed Gemini request (no BYO key) returns ``needs_key`` rather
    than borrowing the operator's Anthropic subsidy.

    ``kind`` is logged into ``usage_events`` (e.g. ``"extract"``,
    ``"polish_bullet"``) for the audit trail.
    """
    p = canonical_provider(provider)
    spec = provider_spec(p)

    # 0) DEV-ONLY demo mode (defaults OFF): bypass the whole key/credit policy
    #    and hand back a sentinel key. ``make_client`` ignores it and returns
    #    the offline StubLLMClient, so AI features run locally with NO real key,
    #    NEVER 503/402, and consume NO managed credits. Production (flag off) is
    #    unchanged — this branch is simply never taken.
    if demo_mode_enabled():
        return DEMO_STUB_API_KEY, False

    if spec.auth_type == "none":
        return "", False

    owner_id = getattr(auth_mod_g(), "owner_id", None)

    # 1) Bring-your-own key wins — the user spends on their own account. The row
    #    is scoped to THIS provider, so the decrypted key always matches the
    #    provider the caller will build the client for (no cross-vendor leak).
    row = _stored_key_row(p)
    if row is not None:
        try:
            key = vault.decrypt_key(owner_id, row)
        except Exception as exc:  # noqa: BLE001 — wrong KEK / tampered / AAD mismatch
            # We can decrypt nothing useful — force a re-entry rather than
            # leaking a crash. Never log the row.
            raise LLMUnavailable(
                400,
                {
                    "error": "Couldn't use your stored API key — please re-enter it.",
                    "code": "key_decrypt_failed",
                },
            ) from exc
        return key, False

    # 2) Managed-credit path — Anthropic only, and only if credits remain AND
    #    the RPC says yes. For Gemini, ``_operator_key_for`` returns "" so this
    #    branch is skipped entirely → a managed Gemini request falls through to
    #    needs_key (Gemini is BYO-only in v1).
    if spec.managed_credit_eligible and _managed_credits_remaining() > 0:
        # Check the operator key BEFORE spending a credit — otherwise a
        # blank-key dev/deploy would decrement the balance on a call that
        # can't actually run. Fail with 503 without touching the balance.
        operator = _operator_key_for(p)
        if not operator:
            raise LLMUnavailable(
                503,
                {"error": "AI temporarily unavailable.", "code": "ai_unavailable"},
            )
        if _consume_managed_credit(kind):
            return operator, True

    # 3) Out of credits / no key for this provider.
    label = spec.label
    raise LLMUnavailable(
        402,
        {
            "error": f"Add your {label} API key to continue",
            "code": "needs_key",
        },
    )


def _consume_managed_credit(kind: str) -> bool:
    """Call the SECURITY DEFINER RPC; True iff a credit was spent.

    The RPC decrements ``managed_credits_remaining`` and writes the
    ``used_managed_credit=true`` usage_events row atomically, as the table
    owner (the column UPDATE grant is revoked from ``authenticated``). Returns
    its boolean result; treats any unexpected shape as False (fail closed).
    """
    sb = auth_mod.user_supabase()
    resp = sb.rpc("consume_managed_credit", {"p_kind": kind}).execute()
    return resp.data is True


def log_byo_usage(kind: str, provider: str = "anthropic") -> None:
    """Append a usage_events row for a BYO (non-managed-credit) AI call.

    Managed-credit calls already get their audit row inside the RPC; this
    records the BYO path so usage is fully accounted for. The RPC derives the
    owner and authoritative audit fields; browser roles cannot insert directly
    into the immutable table. Best-effort: a logging failure must not fail the
    user's generation.
    """
    if demo_mode_enabled():
        # Demo generations use StubLLMClient and spend no key or credit. They
        # are neither BYO usage nor a reason to contact the audit database.
        return
    try:
        sb = auth_mod.user_supabase()
        sb.rpc(
            "record_byo_usage",
            {
                "p_kind": kind,
                "p_provider": canonical_provider(provider),
            },
        ).execute()
    except Exception:  # noqa: BLE001 — audit best-effort, never break the call
        pass


def auth_mod_g():
    """Return Flask's request-global ``g`` (indirection eases testing)."""
    from flask import g

    return g
