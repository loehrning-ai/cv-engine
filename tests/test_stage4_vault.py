"""Stage 4 tests — BYO-key vault, key endpoints, credit resolution, redaction.

Security-critical surface: key secrecy (AES-GCM envelope, AAD binding, log
redaction) and credit-tamper resistance (column grant + SECURITY DEFINER RPC).

These run WITHOUT a live provider — every ``validate_provider_key`` and every
Anthropic network call is mocked. The DB-backed routes use the in-memory
``fake_supabase`` fixture from conftest (whose ``rpc('consume_managed_credit')``
faithfully decrements + audits, so the "3 free then needs_key" flow is real).

The genuinely cross-tenant credit-tamper assertion runs against the REAL
Supabase project with a real user JWT when ``TEST_A_TOKEN`` is set; it SKIPS
cleanly otherwise so the suite still runs offline.

Requires ``ONEPAGER_KEK`` in the environment (set in .env; the Docker run
passes --env-file .env). Tests that need a *wrong* KEK generate their own.
"""
from __future__ import annotations

import base64
import logging
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

import vault  # noqa: E402

_HAVE_KEK = bool(os.environ.get("ONEPAGER_KEK"))
_kek_required = pytest.mark.skipif(
    not _HAVE_KEK, reason="ONEPAGER_KEK not set — vault crypto tests need it"
)

# A second, independent 32-byte KEK used to prove decrypt fails under the
# wrong master key.
_WRONG_KEK_B64 = base64.b64encode(os.urandom(32)).decode("ascii")

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"
OTHER_OWNER = "00000000-0000-0000-0000-000000000002"
SAMPLE_KEY = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJ"


# ===========================================================================
#  1. Envelope encryption — round-trip + tamper resistance.
# ===========================================================================
@_kek_required
class TestVaultCrypto:
    def test_round_trip(self):
        enc = vault.encrypt_key(FAKE_OWNER, SAMPLE_KEY)
        # Ciphertext must not contain the plaintext.
        assert SAMPLE_KEY.encode() not in enc.ciphertext
        assert enc.last4 == SAMPLE_KEY[-4:]
        # Round-trips back to the exact plaintext.
        row = _enc_to_row(enc)
        assert vault.decrypt_key(FAKE_OWNER, row) == SAMPLE_KEY

    def test_wrong_user_id_aad_raises(self):
        from cryptography.exceptions import InvalidTag

        enc = vault.encrypt_key(FAKE_OWNER, SAMPLE_KEY)
        row = _enc_to_row(enc)
        # Decrypting under a DIFFERENT user_id must fail GCM auth (AAD bind).
        with pytest.raises(InvalidTag):
            vault.decrypt_key(OTHER_OWNER, row)

    def test_tampered_ciphertext_raises(self):
        from cryptography.exceptions import InvalidTag

        enc = vault.encrypt_key(FAKE_OWNER, SAMPLE_KEY)
        row = _enc_to_row(enc)
        # Flip a byte of the ciphertext.
        bad = bytearray(enc.ciphertext)
        bad[0] ^= 0x01
        row["ciphertext"] = bytes(bad)
        with pytest.raises(InvalidTag):
            vault.decrypt_key(FAKE_OWNER, row)

    def test_wrong_kek_raises(self, monkeypatch):
        from cryptography.exceptions import InvalidTag

        enc = vault.encrypt_key(FAKE_OWNER, SAMPLE_KEY)
        row = _enc_to_row(enc)
        # Swap the master KEK out from under the unwrap step.
        monkeypatch.setenv("ONEPAGER_KEK", _WRONG_KEK_B64)
        with pytest.raises(InvalidTag):
            vault.decrypt_key(FAKE_OWNER, row)

    def test_bad_kek_length_fails_fast(self, monkeypatch):
        monkeypatch.setenv("ONEPAGER_KEK", base64.b64encode(b"too-short").decode())
        with pytest.raises(vault.VaultConfigError):
            vault.encrypt_key(FAKE_OWNER, SAMPLE_KEY)


def _enc_to_row(enc: "vault.EncryptedKey") -> dict:
    """An in-memory row holding raw bytes (decrypt_key coerces both forms)."""
    return {
        "ciphertext": enc.ciphertext,
        "nonce": enc.nonce,
        "wrapped_dek": enc.wrapped_dek,
        "dek_nonce": enc.dek_nonce,
        "last4": enc.last4,
    }


# ===========================================================================
#  2. Log redaction — a key must never survive into a log record.
# ===========================================================================
class TestLogRedaction:
    def test_redacts_anthropic_key(self):
        import logredact

        masked = logredact.redact(f"calling provider with {SAMPLE_KEY} now")
        assert SAMPLE_KEY not in masked
        assert "REDACTED" in masked

    def test_redacts_generic_sk_key(self):
        import logredact

        generic = "sk-" + "A" * 40
        assert generic not in logredact.redact(f"key={generic}")

    def test_filter_masks_emitted_record(self, caplog):
        import logredact

        logger = logging.getLogger("stage4.redact.test")
        logredact.install(logger)
        with caplog.at_level(logging.INFO, logger="stage4.redact.test"):
            logger.info("user key is %s", SAMPLE_KEY)
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert SAMPLE_KEY not in joined
        assert "REDACTED" in joined


# ===========================================================================
#  3. Key endpoints — save / status / delete (validate mocked).
# ===========================================================================
@_kek_required
class TestKeyEndpoints:
    def test_save_status_delete(self, client, fake_supabase, monkeypatch):
        import llm_gateway

        # Mock the provider probe so no live call is made.
        monkeypatch.setattr(llm_gateway, "validate_provider_key", lambda p, k: None)
        # Seed the caller's profile (default 3 managed credits).
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        # POST a key → ok + last4, never the plaintext.
        res = client.post("/api/key", json={"provider": "anthropic", "key": SAMPLE_KEY})
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["ok"] is True
        assert body["last4"] == SAMPLE_KEY[-4:]
        assert SAMPLE_KEY not in res.get_data(as_text=True)
        # Row stores ciphertext only — the plaintext key appears nowhere in it.
        rows = fake_supabase.tables.get("user_llm_keys", [])
        assert len(rows) == 1
        assert SAMPLE_KEY not in str(rows[0])
        assert SAMPLE_KEY[:-4] not in str(rows[0])  # not even the non-last4 part

        # GET status → per-provider connection state + shared credit balance.
        # (Stage 4 / plan 002 widened this from a single provider to all.)
        st = client.get("/api/key").get_json()
        assert st["providers"]["anthropic"]["connected"] is True
        assert st["providers"]["anthropic"]["last4"] == SAMPLE_KEY[-4:]
        assert st["providers"]["gemini"]["connected"] is False
        assert st["managed_credits_remaining"] == 3

        # DELETE → row gone.
        d = client.delete("/api/key").get_json()
        assert d["deleted"] is True
        assert client.get("/api/key").get_json()["providers"]["anthropic"]["connected"] is False

    def test_invalid_key_rejected(self, client, fake_supabase, monkeypatch):
        import llm_gateway

        def _reject(provider, key):
            raise llm_gateway.KeyValidationError("Anthropic rejected that key.")

        monkeypatch.setattr(llm_gateway, "validate_provider_key", _reject)
        res = client.post("/api/key", json={"provider": "anthropic", "key": "sk-ant-bad"})
        assert res.status_code == 400
        assert res.get_json()["code"] == "invalid_key"
        # Nothing stored on a bad key.
        assert not fake_supabase.tables.get("user_llm_keys")

    def test_missing_key_is_400(self, client, fake_supabase):
        res = client.post("/api/key", json={"provider": "anthropic"})
        assert res.status_code == 400


# ===========================================================================
#  4. Credit resolution — 3 managed gens, then needs_key on the 4th.
# ===========================================================================
@_kek_required
class TestCreditResolution:
    def test_three_managed_then_needs_key(self, client, fake_supabase, monkeypatch):
        from render import ai_extract

        # Operator key present so the managed path can "run".
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-key-XXXXXXXXXXXXXXXXXXXX")
        # Mock the actual Claude polish call — assert it's only reached with a key.
        calls = {"n": 0}

        def _fake_polish(text, *, api_key=None, model=None, provider="anthropic"):
            calls["n"] += 1
            assert api_key, "polish must receive a resolved key"
            return "Rewrote: " + text

        monkeypatch.setattr(ai_extract, "polish_bullet", _fake_polish)

        # User has NO BYO key but 3 managed credits.
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)

        # 3 successful managed generations.
        for i in range(3):
            res = client.post("/api/polish/bullet", json={"text": f"bullet {i}"})
            assert res.status_code == 200, res.get_data(as_text=True)
            assert res.get_json()["rewritten"].startswith("Rewrote:")

        # Balance is now 0 and 3 audit rows were written by the RPC.
        prof = _profile_row(fake_supabase, FAKE_OWNER)
        assert prof["managed_credits_remaining"] == 0
        managed_events = [
            e for e in fake_supabase.tables.get("usage_events", []) if e.get("used_managed_credit")
        ]
        assert len(managed_events) == 3

        # 4th call → 402 needs_key, and the Claude call was NOT made a 4th time.
        res = client.post("/api/polish/bullet", json={"text": "bullet 4"})
        assert res.status_code == 402
        assert res.get_json()["code"] == "needs_key"
        assert calls["n"] == 3

    def test_byo_key_used_and_logged(self, client, fake_supabase, monkeypatch):
        from render import ai_extract
        import llm_gateway

        # No operator key — proves the BYO path doesn't need it.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _seed_profile(fake_supabase, FAKE_OWNER, credits=0)

        # Store a BYO key for the user (validate mocked).
        monkeypatch.setattr(llm_gateway, "validate_provider_key", lambda p, k: None)
        client.post("/api/key", json={"provider": "anthropic", "key": SAMPLE_KEY})

        seen = {"key": None}

        def _fake_polish(text, *, api_key=None, model=None, provider="anthropic"):
            seen["key"] = api_key
            return "ok"

        monkeypatch.setattr(ai_extract, "polish_bullet", _fake_polish)

        res = client.post("/api/polish/bullet", json={"text": "hello"})
        assert res.status_code == 200
        # The decrypted BYO key was passed through (round-trips to plaintext).
        assert seen["key"] == SAMPLE_KEY
        # A BYO usage_events row was logged (used_managed_credit False).
        byo = [
            e
            for e in fake_supabase.tables.get("usage_events", [])
            if e.get("used_managed_credit") is False
        ]
        assert len(byo) == 1
        assert byo[0]["kind"] == "polish_bullet"

    def test_blank_operator_key_is_503(self, client, fake_supabase, monkeypatch):
        # Credits remain but operator key blank → 503, not a crash.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        _seed_profile(fake_supabase, FAKE_OWNER, credits=3)
        res = client.post("/api/polish/bullet", json={"text": "x"})
        assert res.status_code == 503
        assert res.get_json()["code"] == "ai_unavailable"
        # No credit consumed when we can't actually run.
        assert _profile_row(fake_supabase, FAKE_OWNER)["managed_credits_remaining"] == 3


# ===========================================================================
#  5. SSRF guard.
# ===========================================================================
class TestSSRFGuard:
    def test_blocks_loopback(self):
        import ssrf

        for url in (
            "http://127.0.0.1/x",
            "http://localhost:5567/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://metadata.google.internal/",
        ):
            with pytest.raises(ssrf.SSRFBlocked):
                ssrf.assert_safe_url(url)

    def test_blocks_private_ranges(self):
        import ssrf

        for url in ("http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/"):
            with pytest.raises(ssrf.SSRFBlocked):
                ssrf.assert_safe_url(url)

    def test_rejects_non_http_scheme(self):
        import ssrf

        with pytest.raises(ssrf.SSRFBlocked):
            ssrf.assert_safe_url("file:///etc/passwd")

    def test_allows_public_host(self):
        import ssrf

        # A public DNS name resolves to public IPs → allowed (returns host).
        assert ssrf.assert_safe_url("https://example.com/theme.json") == "example.com"


# ===========================================================================
#  6. Cross-tenant credit-tamper (REAL Supabase) — column grant blocks self-grant.
# ===========================================================================
A_TOKEN = os.environ.get("TEST_A_TOKEN")
A_ID = os.environ.get("TEST_A_ID")
_HAVE_USER_A = bool(A_TOKEN and A_ID)


@pytest.mark.skipif(
    not _HAVE_USER_A, reason="TEST_A_TOKEN/TEST_A_ID not set — needs a real provisioned user"
)
class TestCreditTamperReal:
    def test_user_cannot_self_grant_credits_but_rpc_works(self):
        """A direct UPDATE of managed_credits_remaining is a no-op/denied
        (column grant revoked), while consume_managed_credit still works."""
        from supabase import create_client

        url = os.environ["SUPABASE_URL"]
        pub = os.environ["SUPABASE_PUBLISHABLE_KEY"]
        sb = create_client(url, pub)
        sb.postgrest.auth(A_TOKEN)

        before = (
            sb.table("profiles")
            .select("managed_credits_remaining")
            .eq("id", A_ID)
            .single()
            .execute()
            .data["managed_credits_remaining"]
        )

        # Attempt to self-grant 999 credits via the RLS client. The row-level
        # policy would allow the row, but the column UPDATE grant excludes
        # managed_credits_remaining — so this must NOT raise the balance.
        try:
            sb.table("profiles").update({"managed_credits_remaining": 999}).eq(
                "id", A_ID
            ).execute()
        except Exception:
            # A hard PostgREST permission error is also an acceptable outcome.
            pass

        after = (
            sb.table("profiles")
            .select("managed_credits_remaining")
            .eq("id", A_ID)
            .single()
            .execute()
            .data["managed_credits_remaining"]
        )
        assert after == before, f"credit self-grant leaked: {before} -> {after}"

        # The sanctioned definer RPC still decrements (if any credits remain).
        if before > 0:
            ok = sb.rpc(
                "consume_managed_credit",
                {"p_kind": "extract"},
            ).execute().data
            assert ok is True
            spent = (
                sb.table("profiles")
                .select("managed_credits_remaining")
                .eq("id", A_ID)
                .single()
                .execute()
                .data["managed_credits_remaining"]
            )
            assert spent == before - 1


# ---------------------------------------------------------------------------
# Helpers for the fake-supabase profile row.
# ---------------------------------------------------------------------------
def _seed_profile(store, owner_id: str, *, credits: int) -> None:
    store.tables.setdefault("profiles", []).append(
        {"id": owner_id, "managed_credits_remaining": credits}
    )


def _profile_row(store, owner_id: str) -> dict:
    return next(r for r in store.tables["profiles"] if r["id"] == owner_id)
