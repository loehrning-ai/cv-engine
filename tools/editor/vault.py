"""BYO LLM-key vault — AES-256-GCM envelope encryption (Stage 4).

A user may bring their own provider API key (e.g. an Anthropic key) instead
of burning operator-funded managed credits. That key is a secret: it must
never be stored in plaintext, never logged, and only ever exist in
plaintext in-process for the moment immediately before the outbound LLM
call. This module is the only place key bytes are encrypted/decrypted.

Envelope encryption (two layers, both AES-256-GCM):

    plaintext key ──[DEK]──▶ ciphertext        (DEK = random per-key 32-byte data key)
    DEK           ──[KEK]──▶ wrapped_dek        (KEK = platform master key, base64 in env)

* A fresh 32-byte **DEK** (data-encryption key) is generated per stored key.
  The plaintext key is encrypted with the DEK (random 12-byte nonce).
* The DEK is then itself encrypted ("wrapped") with the platform **KEK**
  (key-encryption key, random 12-byte nonce). Only the wrapped DEK is
  persisted — the raw DEK never touches disk.
* ``user_id`` (the owner's auth uid) is the **AAD** (additional authenticated
  data) on BOTH layers. AAD is authenticated but not encrypted: GCM verifies
  it on decrypt, so a ciphertext row stolen and replayed under a *different*
  user_id fails authentication (``InvalidTag``). This cryptographically binds
  each stored key to its owner — a DB-level row swap can't move a key between
  tenants.

A wrong KEK, a tampered ciphertext/wrapped-DEK, or a mismatched user_id all
raise :class:`cryptography.exceptions.InvalidTag` on decrypt — the function
never silently returns garbage.

The KEK comes from ``ONEPAGER_KEK`` (base64 of exactly 32 bytes). It is
loaded LAZILY on first encrypt/decrypt (:func:`_kek`), not at import: the
module must be importable in contexts that never touch the vault (e.g.
unrelated unit tests) without the env var set — but any actual use fails
fast with :class:`VaultConfigError` if the KEK is missing or the wrong
length, so a misconfigured deploy can't run with a broken vault.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-256 → 32-byte keys; GCM standard nonce length is 12 bytes.
_KEY_LEN = 32
_NONCE_LEN = 12


class VaultConfigError(RuntimeError):
    """Raised at startup when the master KEK is missing or malformed."""


def _load_kek() -> bytes:
    """Decode the platform KEK from ``ONEPAGER_KEK`` (base64 → 32 bytes).

    Fails fast: a missing or wrong-length KEK is a deploy misconfiguration,
    not a per-request error. We never fall back to a default key (that would
    silently make every stored key forgeable).
    """
    raw = os.environ.get("ONEPAGER_KEK")
    if not raw:
        raise VaultConfigError(
            "ONEPAGER_KEK is not set — cannot encrypt/decrypt BYO keys."
        )
    try:
        kek = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001 — surface a clear config error
        raise VaultConfigError(f"ONEPAGER_KEK is not valid base64: {exc}") from exc
    if len(kek) != _KEY_LEN:
        raise VaultConfigError(
            f"ONEPAGER_KEK must decode to {_KEY_LEN} bytes, got {len(kek)}."
        )
    return kek


def _kek() -> bytes:
    """Return the master KEK, loaded lazily on first use.

    Lazy (rather than at import) so the module can be imported in contexts
    that don't touch the vault (e.g. unrelated unit tests) without the env
    var set — but any *use* of encrypt/decrypt fails fast if it's absent.
    Tests that monkeypatch ``ONEPAGER_KEK`` see the change because we read
    the env each call rather than caching a module global.
    """
    return _load_kek()


@dataclass(frozen=True)
class EncryptedKey:
    """The four ciphertext columns + a display hint for one stored key.

    Mirrors the ``user_llm_keys`` schema. ``last4`` is the only
    plaintext-derived value we keep, and it's just the trailing 4 chars so
    the UI can show "…last4" without ever revealing the key.
    """

    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    dek_nonce: bytes
    last4: str

    def as_row(self) -> dict[str, object]:
        """Serialise to the PostgREST insert/update payload.

        ``bytea`` columns are sent base64-encoded; supabase-py / PostgREST
        accept that for ``bytea`` and round-trip it back as base64 on read,
        which :func:`decrypt_key` decodes.
        """
        return {
            "ciphertext": base64.b64encode(self.ciphertext).decode("ascii"),
            "nonce": base64.b64encode(self.nonce).decode("ascii"),
            "wrapped_dek": base64.b64encode(self.wrapped_dek).decode("ascii"),
            "dek_nonce": base64.b64encode(self.dek_nonce).decode("ascii"),
            "last4": self.last4,
        }


def encrypt_key(user_id: str, plaintext: str) -> EncryptedKey:
    """Envelope-encrypt ``plaintext`` for ``user_id``.

    Generates a fresh DEK, encrypts the key with it (AAD=user_id), then wraps
    the DEK with the master KEK (AAD=user_id). Returns the four ciphertext
    blobs plus ``last4``. The plaintext is never persisted or logged.
    """
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("plaintext key must be a non-empty string")
    aad = user_id.encode("utf-8")
    pt = plaintext.encode("utf-8")

    # 1) Encrypt the key under a fresh per-key DEK.
    dek = os.urandom(_KEY_LEN)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(dek).encrypt(nonce, pt, aad)

    # 2) Wrap (encrypt) the DEK under the master KEK.
    dek_nonce = os.urandom(_NONCE_LEN)
    wrapped_dek = AESGCM(_kek()).encrypt(dek_nonce, dek, aad)

    return EncryptedKey(
        ciphertext=ciphertext,
        nonce=nonce,
        wrapped_dek=wrapped_dek,
        dek_nonce=dek_nonce,
        last4=plaintext[-4:],
    )


def _coerce_bytes(value: object) -> bytes:
    """Normalise a ``bytea`` column value to raw bytes.

    PostgREST returns ``bytea`` as base64 text; the in-memory test fake keeps
    raw ``bytes``. Accept both (and ``memoryview``) so the same decrypt path
    works against the real DB and the unit-test fake.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        # PostgREST may prefix bytea with the Postgres hex escape "\x".
        if value.startswith("\\x"):
            return bytes.fromhex(value[2:])
        return base64.b64decode(value)
    raise TypeError(f"cannot coerce {type(value).__name__} to bytes")


def decrypt_key(user_id: str, row: dict) -> str:
    """Recover the plaintext key for ``user_id`` from a stored ``row``.

    Unwraps the DEK with the master KEK (AAD=user_id), then decrypts the
    ciphertext with the DEK (AAD=user_id). Any of: wrong KEK, tampered
    ciphertext/wrapped-DEK, or a ``user_id`` that differs from the one the
    row was encrypted under, fails GCM authentication and raises
    ``InvalidTag`` — never a silent wrong answer.

    The caller should treat any exception as "re-enter your API key" and must
    not log the row or the result.
    """
    aad = user_id.encode("utf-8")
    wrapped_dek = _coerce_bytes(row["wrapped_dek"])
    dek_nonce = _coerce_bytes(row["dek_nonce"])
    ciphertext = _coerce_bytes(row["ciphertext"])
    nonce = _coerce_bytes(row["nonce"])

    # Unwrap the DEK (authenticates AAD=user_id against the KEK layer).
    dek = AESGCM(_kek()).decrypt(dek_nonce, wrapped_dek, aad)
    # Decrypt the key (authenticates AAD=user_id against the DEK layer).
    plaintext = AESGCM(dek).decrypt(nonce, ciphertext, aad)
    return plaintext.decode("utf-8")
