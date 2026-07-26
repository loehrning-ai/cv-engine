"""Regression tests for the DNS-rebinding TOCTOU fix in the LLM client.

``engine/llm/client.py`` used to validate the custom OpenAI-compatible base
URL hostname ONCE (at config time) and then let urlopen RE-RESOLVE DNS at
connect time. A hostile resolver could answer ``8.8.8.8`` for the validation
lookup and ``127.0.0.1`` for the connect lookup, exfiltrating the user's BYO
``Authorization`` header to an internal host (reproduced in QA with a
monkeypatched ``getaddrinfo``).

The fix mirrors ``tools/editor/ssrf.py``'s pinning technique: resolve once,
require every resolved address to be public, and dial a VALIDATED address
while TLS SNI + certificate hostname verification keep using the ORIGINAL
hostname (certificate verification is NOT disabled — that would trade the
rebinding hole for a bigger MITM hole).

These tests drive a two-stage ``getaddrinfo`` (public answers for the
validation/pin lookups, loopback for anything later — the exact QA repro)
and assert at the socket layer which address is dialed; no TLS-terminating
test double is needed because the pinned address selection happens before
TLS completes.
"""
from __future__ import annotations

import json
import socket
import ssl
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from llm import client as client_mod  # noqa: E402

HOST = "api.example.test"
PUBLIC_IP = "93.184.216.34"
PUBLIC_IP_2 = "93.184.216.35"


class _TwoStageDNS:
    """getaddrinfo double: public answers first, loopback at connect time.

    The first ``public_lookups`` calls (config-time validation + the pin
    resolution inside ``complete``) return ``addresses``; any LATER call —
    i.e. a connect-time re-resolution, which the fix must eliminate — returns
    loopback, so a regression would dial 127.0.0.1 and fail the assertions.
    """

    def __init__(self, public_lookups: int = 2, addresses=(PUBLIC_IP,)):
        self.public_lookups = public_lookups
        self.addresses = list(addresses)
        self.calls: list[str] = []

    def __call__(self, host, *_args, **_kwargs):
        self.calls.append(host)
        answers = self.addresses if len(self.calls) <= self.public_lookups else ["127.0.0.1"]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in answers]


class _RecordingTLSContext:
    """SSLContext stand-in: records ``server_hostname``, refuses to finish TLS.

    ``check_hostname`` stays True throughout: pinning must never disable
    certificate hostname verification to make the dial work.
    """

    check_hostname = True

    def __init__(self):
        self.server_hostnames: list[str | None] = []

    def wrap_socket(self, sock, server_hostname=None, **_kwargs):
        self.server_hostnames.append(server_hostname)
        raise ssl.SSLError("test probe: stop before real TLS")


class _FakeSocket:
    def close(self):
        pass


def _recording_create_connection(dialed: list, *, fail: bool):
    def _fake(address, *_args, **_kwargs):
        dialed.append(address)
        if fail:
            raise ConnectionRefusedError("test probe: no listener")
        return _FakeSocket()

    return _fake


class TestDNSRebindingPinned:
    """The QA two-stage-getaddrinfo repro, now closed by IP pinning."""

    def test_rebind_still_dials_validated_ip_with_original_sni(self, monkeypatch):
        dns = _TwoStageDNS(public_lookups=2)
        monkeypatch.setattr(socket, "getaddrinfo", dns)
        dialed: list = []
        monkeypatch.setattr(
            socket, "create_connection", _recording_create_connection(dialed, fail=False)
        )
        tls = _RecordingTLSContext()
        monkeypatch.setattr(client_mod.ssl, "create_default_context", lambda: tls)

        client = client_mod.OpenAICompatibleClient(
            "openai_compatible", "sk-byo-test", base_url=f"https://{HOST}:8443/v1"
        )
        with pytest.raises(client_mod.LLMError):
            client.complete(system="s", user="u")

        # The dialed address is the VALIDATED IP:port — never the loopback the
        # connect-time lookup would have returned before the fix.
        assert dialed == [(PUBLIC_IP, 8443)]
        # SNI + certificate hostname verification use the ORIGINAL hostname,
        # not the pinned IP, and hostname verification stays enabled.
        assert tls.server_hostnames == [HOST]
        assert tls.check_hostname is True
        # No connect-time re-resolution at all: config validation + pin only.
        assert len(dns.calls) <= 2

    def test_all_validated_ips_are_pin_candidates_in_resolver_order(self, monkeypatch):
        dns = _TwoStageDNS(public_lookups=2, addresses=(PUBLIC_IP, PUBLIC_IP_2))
        monkeypatch.setattr(socket, "getaddrinfo", dns)
        dialed: list = []
        monkeypatch.setattr(
            socket, "create_connection", _recording_create_connection(dialed, fail=False)
        )
        tls = _RecordingTLSContext()
        monkeypatch.setattr(client_mod.ssl, "create_default_context", lambda: tls)

        client = client_mod.OpenAICompatibleClient(
            "openai_compatible", "sk-byo-test", base_url=f"https://{HOST}/v1"
        )
        with pytest.raises(client_mod.LLMError):
            client.complete(system="s", user="u")

        # Both validated addresses are eligible pins (failover order follows
        # the resolver); every dial keeps the original hostname for TLS.
        assert dialed == [(PUBLIC_IP, 443), (PUBLIC_IP_2, 443)]
        assert tls.server_hostnames == [HOST, HOST]

    def test_ip_literal_base_url_never_touches_dns(self, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise AssertionError("IP-literal base URL must not be re-resolved")

        monkeypatch.setattr(socket, "getaddrinfo", _boom)
        dialed: list = []
        monkeypatch.setattr(
            socket, "create_connection", _recording_create_connection(dialed, fail=True)
        )

        client = client_mod.OpenAICompatibleClient(
            "openai_compatible", "sk-byo-test", base_url=f"https://{PUBLIC_IP}:4443/v1"
        )
        with pytest.raises(client_mod.LLMError):
            client.complete(system="s", user="u")
        assert dialed == [(PUBLIC_IP, 4443)]

    def test_pinned_opener_keeps_auth_stripping_redirect_handler(self):
        # The recent cross-host Authorization-strip fix must also hold on the
        # pinned path — the pinned opener installs the same redirect policy.
        opener = client_mod._pinned_https_opener(HOST, [PUBLIC_IP])
        assert any(isinstance(h, client_mod._AuthStrippingRedirectHandler) for h in opener.handlers)
        assert any(isinstance(h, client_mod._PinnedHTTPSHandler) for h in opener.handlers)

    def test_hosted_provider_keeps_shared_opener_and_no_local_resolution(self, monkeypatch):
        """Hosted allowlisted URLs are unchanged: no extra DNS lookup in
        ``complete`` and the ``_SECURE_OPENER`` seam is preserved."""

        def _boom(*_args, **_kwargs):
            raise AssertionError("hosted base URL must not be resolved at request time")

        monkeypatch.setattr(socket, "getaddrinfo", _boom)

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

        monkeypatch.setattr(
            client_mod._SECURE_OPENER, "open", lambda req, timeout=60: _FakeResponse()
        )
        client = client_mod.OpenAICompatibleClient("groq", "gsk_test")
        assert client.complete(system="s", user="u") == "ok"


class TestValidationStillFailsClosed:
    """Pinning must not weaken the existing resolve-then-veto validation."""

    def test_loopback_resolution_rejected_at_config_time(self, monkeypatch):
        dns = _TwoStageDNS(public_lookups=0)  # every answer is loopback
        monkeypatch.setattr(socket, "getaddrinfo", dns)
        with pytest.raises(client_mod.ProviderConfigError, match="non-public"):
            client_mod.OpenAICompatibleClient(
                "openai_compatible", "sk-byo-test", base_url=f"https://{HOST}/v1"
            )

    def test_mixed_public_and_private_resolution_vetoed(self, monkeypatch):
        def _mixed(host, *_args, **_kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            ]

        monkeypatch.setattr(socket, "getaddrinfo", _mixed)
        with pytest.raises(client_mod.ProviderConfigError, match="non-public"):
            client_mod.OpenAICompatibleClient(
                "openai_compatible", "sk-byo-test", base_url=f"https://{HOST}/v1"
            )

    def test_resolve_returns_all_public_addresses_deduped_in_order(self, monkeypatch):
        def _dup(host, *_args, **_kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP_2, 0)),
            ]

        monkeypatch.setattr(socket, "getaddrinfo", _dup)
        assert client_mod._resolve_public_addresses(HOST) == [PUBLIC_IP, PUBLIC_IP_2]
