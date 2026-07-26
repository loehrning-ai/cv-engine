"""Stage 4 (plan 002) tests — the Gemini provider, mocked end-to-end.

Adds Gemini alongside Claude as a BYO-key provider with a provider/model
selector. EVERYTHING here runs WITHOUT a Google key or network: the
``google-genai`` SDK is mocked at every touch-point (client construction,
``generate_content``, the error classes). The security-critical assertions are:

  * a Google key (``AIza…`` / ``ya29.…``) is masked in logs (must-fix 4a);
  * ``validate_provider_key`` accepts a mock-valid ``AIza`` key and rejects a
    bad one without ever echoing the key (must-fix 4d);
  * the SELECTED provider that DECRYPTS the vault key is the SAME provider
    handed to ``make_client`` — never cross-vendor (must-fix 4e);
  * a managed (no-BYO-key) Gemini request returns ``needs_key`` — managed
    credits stay Anthropic-only (must-fix 4g);
  * a Google key encrypts/decrypts in the AES-GCM vault (per-provider row);
  * the provider/model selection persists on the profile.

Requires ``ONEPAGER_KEK`` for the vault round-trip (set in .env; the Docker run
passes --env-file .env). Vault/credit tests skip cleanly without it.
"""
from __future__ import annotations

import logging
import os
import sys
import types as _pytypes
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

_HAVE_KEK = bool(os.environ.get("ONEPAGER_KEK"))
_kek_required = pytest.mark.skipif(
    not _HAVE_KEK, reason="ONEPAGER_KEK not set — vault crypto tests need it"
)

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"
OTHER_OWNER = "00000000-0000-0000-0000-000000000002"
# A realistically-shaped Google AI key: 'AIza' + 35 url-safe chars.
GEMINI_KEY = "AIza" + "Bcd3F_gh-IJklMNop0123456789_AbCdEfG"
ANTHROPIC_KEY = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJ"


# ===========================================================================
#  Mock google-genai SDK — installed into sys.modules so the lazy imports in
#  client.py / llm_gateway.py resolve to this fake (no real package needed).
# ===========================================================================
class _FakeClientError(Exception):
    """Stand-in for google.genai.errors.ClientError (carries a .code)."""

    def __init__(self, code=400, message="bad"):
        super().__init__(message)
        self.code = code
        self.message = message


class _FakeServerError(Exception):
    def __init__(self, code=500, message="server"):
        super().__init__(message)
        self.code = code
        self.message = message


class _FakeApiError(Exception):
    pass


def _install_fake_genai(monkeypatch, *, text="Gemini says hi.", raise_exc=None):
    """Register a fake ``google.genai`` (+ ``.types`` / ``.errors``) module tree.

    ``generate_content`` returns an object whose ``.text`` is ``text`` (use
    ``text=None`` to exercise the None-guard), or raises ``raise_exc``. Records
    the api_key + model on the constructed client so tests can assert the BYO
    key is the one passed (never an env fallback).
    """
    captured: dict = {"api_key": None, "model": None, "http_options": None}

    class _HttpOptions:
        def __init__(self, timeout=None):
            self.timeout = timeout

    class _GenerateContentConfig:
        def __init__(self, system_instruction=None, max_output_tokens=None):
            self.system_instruction = system_instruction
            self.max_output_tokens = max_output_tokens

    class _Models:
        def generate_content(self, *, model, contents, config=None):
            captured["model"] = model
            captured["contents"] = contents
            if raise_exc is not None:
                raise raise_exc
            return _pytypes.SimpleNamespace(text=text)

    class _Client:
        def __init__(self, *, api_key=None, http_options=None, **_k):
            captured["api_key"] = api_key
            captured["http_options"] = http_options
            self.models = _Models()

    google_mod = _pytypes.ModuleType("google")
    genai_mod = _pytypes.ModuleType("google.genai")
    types_mod = _pytypes.ModuleType("google.genai.types")
    errors_mod = _pytypes.ModuleType("google.genai.errors")

    genai_mod.Client = _Client
    types_mod.HttpOptions = _HttpOptions
    types_mod.GenerateContentConfig = _GenerateContentConfig
    errors_mod.ClientError = _FakeClientError
    errors_mod.ServerError = _FakeServerError
    errors_mod.APIError = _FakeApiError
    genai_mod.types = types_mod
    genai_mod.errors = errors_mod
    google_mod.genai = genai_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
    monkeypatch.setitem(sys.modules, "google.genai.errors", errors_mod)
    return captured


# ===========================================================================
#  1. Google-key redaction (MUST land before the client).
# ===========================================================================
class TestGoogleKeyRedaction:
    def test_redacts_aiza_key(self):
        import logredact

        masked = logredact.redact(f"calling gemini with {GEMINI_KEY} now")
        assert GEMINI_KEY not in masked
        assert "REDACTED" in masked

    def test_redacts_oauth_access_token(self):
        import logredact

        tok = "ya29." + "AbC_def-123" * 4
        assert tok not in logredact.redact(f"token={tok}")

    def test_filter_masks_emitted_record(self, caplog):
        import logredact

        logger = logging.getLogger("stage4.gemini.redact")
        logredact.install(logger)
        with caplog.at_level(logging.INFO, logger="stage4.gemini.redact"):
            logger.info("user google key is %s", GEMINI_KEY)
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert GEMINI_KEY not in joined
        assert "REDACTED" in joined


# ===========================================================================
#  2. GeminiClient + make_client.
# ===========================================================================
class TestGeminiClient:
    def test_complete_returns_text(self, monkeypatch):
        from llm import client as llm_client

        cap = _install_fake_genai(monkeypatch, text="  Hello from Gemini.  ")
        c = llm_client.GeminiClient(GEMINI_KEY)
        out = c.complete(system="sys", user="hi", max_tokens=64)
        assert out == "Hello from Gemini."
        # BYO key was passed explicitly (never an env fallback).
        assert cap["api_key"] == GEMINI_KEY
        # Default model when none requested.
        assert cap["model"] == llm_client.GEMINI_DEFAULT_MODEL

    def test_complete_guards_none_text(self, monkeypatch):
        from llm import client as llm_client

        _install_fake_genai(monkeypatch, text=None)  # blocked/empty response
        c = llm_client.GeminiClient(GEMINI_KEY)
        assert c.complete(system="s", user="u") == ""

    def test_complete_honours_allowlisted_model(self, monkeypatch):
        from llm import client as llm_client

        cap = _install_fake_genai(monkeypatch)
        c = llm_client.GeminiClient(GEMINI_KEY)
        c.complete(system="s", user="u", model="gemini-2.5-pro")
        assert cap["model"] == "gemini-2.5-pro"

    def test_blank_key_rejected(self):
        from llm import client as llm_client

        with pytest.raises(llm_client.LLMError):
            llm_client.GeminiClient("")

    def test_satisfies_protocol(self, monkeypatch):
        from llm import client as llm_client

        _install_fake_genai(monkeypatch)
        assert isinstance(llm_client.GeminiClient(GEMINI_KEY), llm_client.LLMClient)

    def test_make_client_returns_gemini(self):
        from llm import client as llm_client

        for p in ("gemini", "google", "GEMINI", " Google "):
            c = llm_client.make_client(p, GEMINI_KEY)
            assert isinstance(c, llm_client.GeminiClient)

    def test_make_client_anthropic_unchanged(self):
        from llm import client as llm_client

        c = llm_client.make_client("anthropic", ANTHROPIC_KEY)
        assert isinstance(c, llm_client.AnthropicClient)

    def test_resolve_model_allowlist(self):
        from llm import client as llm_client

        # known model passes through
        assert llm_client.resolve_model("gemini", "gemini-2.5-pro") == "gemini-2.5-pro"
        # unknown / cross-provider model falls back to provider default (no 404)
        assert llm_client.resolve_model("gemini", "claude-sonnet-4-5") == llm_client.GEMINI_DEFAULT_MODEL
        assert llm_client.resolve_model("google", None) == llm_client.GEMINI_DEFAULT_MODEL
        assert llm_client.resolve_model("anthropic", "bogus") == llm_client.DEFAULT_MODEL


# ===========================================================================
#  3. validate_provider_key — Gemini branch (mocked SDK).
# ===========================================================================
class TestValidateGeminiKey:
    def test_accepts_valid_key(self, monkeypatch):
        import llm_gateway

        _install_fake_genai(monkeypatch, text="ok")
        # No exception == accepted.
        llm_gateway.validate_provider_key("gemini", GEMINI_KEY)
        llm_gateway.validate_provider_key("google", GEMINI_KEY)  # alias

    def test_rejects_wrong_prefix_without_network(self, monkeypatch):
        import llm_gateway

        # Even with a working SDK, a non-AIza key is rejected on shape alone.
        _install_fake_genai(monkeypatch)
        with pytest.raises(llm_gateway.KeyValidationError):
            llm_gateway.validate_provider_key("gemini", "not-a-google-key")

    def test_maps_auth_error_and_never_echoes_key(self, monkeypatch):
        import llm_gateway

        _install_fake_genai(monkeypatch, raise_exc=_FakeClientError(code=403, message="denied"))
        with pytest.raises(llm_gateway.KeyValidationError) as ei:
            llm_gateway.validate_provider_key("gemini", GEMINI_KEY)
        # The error message must NOT contain the key.
        assert GEMINI_KEY not in str(ei.value)


# ===========================================================================
#  4. Vault — a Google key encrypts/decrypts per-provider (no schema change).
# ===========================================================================
@_kek_required
class TestGeminiVault:
    def test_round_trip(self):
        import vault

        enc = vault.encrypt_key(FAKE_OWNER, GEMINI_KEY)
        assert GEMINI_KEY.encode() not in enc.ciphertext
        assert enc.last4 == GEMINI_KEY[-4:]
        row = {
            "ciphertext": enc.ciphertext,
            "nonce": enc.nonce,
            "wrapped_dek": enc.wrapped_dek,
            "dek_nonce": enc.dek_nonce,
            "last4": enc.last4,
        }
        assert vault.decrypt_key(FAKE_OWNER, row) == GEMINI_KEY

    def test_save_get_delete_gemini_alongside_anthropic(self, client, fake_supabase, monkeypatch):
        import llm_gateway

        monkeypatch.setattr(llm_gateway, "validate_provider_key", lambda p, k: None)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        # Store BOTH an Anthropic and a Gemini key — distinct rows (owner,provider).
        a = client.post("/api/key", json={"provider": "anthropic", "key": ANTHROPIC_KEY})
        g = client.post("/api/key", json={"provider": "gemini", "key": GEMINI_KEY})
        assert a.status_code == 200 and g.status_code == 200
        rows = fake_supabase.tables.get("user_llm_keys", [])
        assert {r["provider"] for r in rows} == {"anthropic", "gemini"}
        # Neither plaintext key appears in storage.
        assert GEMINI_KEY not in str(rows) and ANTHROPIC_KEY not in str(rows)

        # GET (all providers) reports both connected.
        st = client.get("/api/key").get_json()
        provs = st.get("providers") or {}
        assert provs["anthropic"]["connected"] is True
        assert provs["gemini"]["connected"] is True
        assert provs["gemini"]["last4"] == GEMINI_KEY[-4:]

        # DELETE only the gemini row; anthropic survives.
        d = client.delete("/api/key?provider=gemini").get_json()
        assert d["deleted"] is True
        rows2 = fake_supabase.tables.get("user_llm_keys", [])
        assert {r["provider"] for r in rows2} == {"anthropic"}


# ===========================================================================
#  5. Provider threading — decrypt-provider == make_client-provider (no cross
#     vendor). Drives the real cover-letter service with mocked SDK + guards.
# ===========================================================================
@_kek_required
class TestProviderThreading:
    def test_cover_letter_uses_gemini_end_to_end(self, client, fake_supabase, monkeypatch):
        import cover_letter_service as cl
        import llm_gateway
        from llm import client as llm_client

        cap = _install_fake_genai(monkeypatch, text="A short grounded letter.")
        monkeypatch.setattr(llm_gateway, "validate_provider_key", lambda p, k: None)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=0)

        # Spy on make_client to capture the provider actually used.
        seen = {"provider": None, "key": None}
        real_make = llm_client.make_client

        def _spy(provider, key):
            seen["provider"] = provider
            seen["key"] = key
            return real_make(provider, key)

        monkeypatch.setattr(cl, "make_client", _spy)
        # Neutralize the post-gen guards so the (mock) letter is accepted; we are
        # asserting provider routing, not grounding (covered elsewhere).
        monkeypatch.setattr(
            cl, "run_guards",
            lambda text, cv, jd_context=None: _pytypes.SimpleNamespace(
                text=text, word_count=len(text.split()), cliches_removed=[]
            ),
        )

        # Store ONLY a Gemini key.
        client.post("/api/key", json={"provider": "gemini", "key": GEMINI_KEY})
        # Seed a CV the service can load (RLS-scoped read).
        cv_id = _seed_cv(fake_supabase, FAKE_OWNER)

        res = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": cv_id, "jd_text": "We need a data engineer at Acme.",
                  "provider": "gemini"},
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        # The provider that built the client is gemini …
        assert seen["provider"] == "gemini"
        # … and the key handed to it is the decrypted GEMINI key (NOT anthropic).
        assert seen["key"] == GEMINI_KEY
        # … and it actually went through the (mocked) Gemini SDK.
        assert cap["api_key"] == GEMINI_KEY

    def test_no_cross_vendor_key(self, client, fake_supabase, monkeypatch):
        """resolve_llm decrypts the GEMINI row when provider=gemini — it must
        never hand back the stored ANTHROPIC key for a gemini request."""
        import llm_gateway

        monkeypatch.setattr(llm_gateway, "validate_provider_key", lambda p, k: None)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=0)
        client.post("/api/key", json={"provider": "anthropic", "key": ANTHROPIC_KEY})
        client.post("/api/key", json={"provider": "gemini", "key": GEMINI_KEY})

        with client.application.test_request_context():
            from flask import g

            g.owner_id = FAKE_OWNER
            key_g, _ = llm_gateway.resolve_llm("cover_letter_generate", provider="gemini")
            key_a, _ = llm_gateway.resolve_llm("cover_letter_generate", provider="anthropic")
        assert key_g == GEMINI_KEY
        assert key_a == ANTHROPIC_KEY
        assert key_g != key_a


# ===========================================================================
#  6. Managed credits are Anthropic-only — managed Gemini → needs_key.
# ===========================================================================
@_kek_required
class TestManagedGeminiNeedsKey:
    def test_managed_gemini_without_key_needs_key(self, client, fake_supabase, monkeypatch):
        import llm_gateway

        # Operator Anthropic key present + credits remain — but the request is
        # for GEMINI, which has no managed subsidy → 402 needs_key (no credit
        # consumed, no cross-vendor operator key used).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXX")
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        with client.application.test_request_context():
            from flask import g

            g.owner_id = FAKE_OWNER
            with pytest.raises(llm_gateway.LLMUnavailable) as ei:
                llm_gateway.resolve_llm("cover_letter_generate", provider="gemini")
        assert ei.value.status == 402
        assert ei.value.payload["code"] == "needs_key"
        # No credit was spent on the rejected gemini request.
        assert _profile_row(fake_supabase, FAKE_OWNER)["managed_credits_remaining"] == 3

    def test_anthropic_managed_still_works(self, client, fake_supabase, monkeypatch):
        import llm_gateway

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXX")
        _seed_profile(fake_supabase, FAKE_OWNER, credits=2)
        with client.application.test_request_context():
            from flask import g

            g.owner_id = FAKE_OWNER
            key, managed = llm_gateway.resolve_llm("cover_letter_generate", provider="anthropic")
        assert managed is True
        assert key.startswith("sk-ant-operator")
        assert _profile_row(fake_supabase, FAKE_OWNER)["managed_credits_remaining"] == 1


# ===========================================================================
#  7. Selector persistence — preferred_provider / preferred_model on profile.
# ===========================================================================
class TestSelectorPersistence:
    def test_profile_persists_provider_and_model(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post(
            "/api/account/profile",
            json={"preferred_provider": "gemini", "preferred_model": "gemini-2.5-pro"},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["preferred_provider"] == "gemini"
        assert body["preferred_model"] == "gemini-2.5-pro"
        # Persisted on the row.
        row = _profile_row(fake_supabase, FAKE_OWNER)
        assert row["preferred_provider"] == "gemini"
        assert row["preferred_model"] == "gemini-2.5-pro"

    def test_rejects_unknown_provider(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post("/api/account/profile", json={"preferred_provider": "skynet"})
        assert res.status_code == 400

    def test_rejects_cross_provider_model(self, client, fake_supabase):
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        # gemini provider + a Claude model is incoherent → rejected.
        res = client.post(
            "/api/account/profile",
            json={"preferred_provider": "gemini", "preferred_model": "claude-sonnet-4-5"},
        )
        assert res.status_code == 400


# ===========================================================================
#  8. Demo mode still returns the Stub for any provider.
# ===========================================================================
class TestDemoModeProviderAgnostic:
    def test_stub_returned_for_gemini_in_demo_mode(self, monkeypatch):
        from llm import client as llm_client

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        for p in ("gemini", "google", "anthropic"):
            c = llm_client.make_client(p, "anything")
            assert isinstance(c, llm_client.StubLLMClient)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _seed_profile(store, owner_id: str, *, credits: int) -> None:
    store.tables.setdefault("profiles", []).append(
        {"id": owner_id, "managed_credits_remaining": credits}
    )


def _profile_row(store, owner_id: str) -> dict:
    return next(r for r in store.tables["profiles"] if r["id"] == owner_id)


def _seed_cv(store, owner_id: str) -> str:
    """Insert a minimal valid CV document the cover-letter service can load."""
    import uuid

    cv_id = str(uuid.uuid4())
    store.tables.setdefault("documents", []).append(
        {
            "id": cv_id,
            "owner_id": owner_id,
            "type": "cv",
            "name": "My CV",
            "yaml_blob": (
                "name: Tim Loehr\n"
                "experience:\n"
                "  - role: Data Engineer\n"
                "    company: Meta\n"
                "    bullets:\n"
                "      - Built pipelines\n"
            ),
            "deleted_at": None,
        }
    )
    return cv_id
