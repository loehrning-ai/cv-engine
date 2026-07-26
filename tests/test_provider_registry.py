from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))


def test_registry_exposes_expected_providers():
    from llm.client import PROVIDER_REGISTRY, SUPPORTED_PROVIDERS

    expected = {
        "anthropic",
        "gemini",
        "ollama",
        "openrouter",
        "deepseek",
        "groq",
        "mistral",
        "openai_compatible",
    }
    assert expected.issubset(PROVIDER_REGISTRY)
    assert expected.issubset(set(SUPPORTED_PROVIDERS))
    assert PROVIDER_REGISTRY["anthropic"].managed_credit_eligible is True
    for provider in expected - {"anthropic"}:
        assert PROVIDER_REGISTRY[provider].managed_credit_eligible is False


def test_google_alias_and_model_resolution_still_work():
    from llm.client import GEMINI_DEFAULT_MODEL, canonical_provider, resolve_model

    assert canonical_provider("google") == "gemini"
    assert canonical_provider("OpenRouter") == "openrouter"
    assert resolve_model("google", "claude-sonnet-4-5") == GEMINI_DEFAULT_MODEL


def test_ollama_base_url_is_local_only(monkeypatch):
    from llm.client import ProviderConfigError, validate_provider_base_url

    assert validate_provider_base_url("ollama", "http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert validate_provider_base_url("ollama", "http://localhost:11434") == "http://localhost:11434"
    with pytest.raises(ProviderConfigError):
        validate_provider_base_url("ollama", "https://127.0.0.1:11434")
    with pytest.raises(ProviderConfigError):
        validate_provider_base_url("ollama", "http://10.0.0.1:11434")


def test_ollama_health_and_model_discovery(monkeypatch):
    from llm.client import discover_ollama_models, ollama_health

    calls = []

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self._payload).encode()

    def _fake_urlopen(req, timeout=1.5):
        calls.append((req.full_url, timeout))
        if req.full_url.endswith("/api/version"):
            return _FakeResponse({"version": "0.5.0"})
        return _FakeResponse({"models": [{"name": "llama3.1:8b"}, {"model": "mistral:7b"}]})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    assert ollama_health() == {"available": True, "version": "0.5.0"}
    assert discover_ollama_models() == ("llama3.1:8b", "mistral:7b")
    assert all(url.startswith("http://127.0.0.1:11434/") for url, _timeout in calls)


def test_ollama_discovery_fails_closed(monkeypatch):
    from llm.client import discover_ollama_models, ollama_health

    def _boom(*_args, **_kwargs):
        raise OSError("daemon down")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert ollama_health()["available"] is False
    assert discover_ollama_models() == ()


def test_api_models_uses_discovered_ollama_models(client, fake_supabase, monkeypatch):
    import server

    monkeypatch.setattr(server, "models_for_display", lambda p: ("local-a", "local-b") if p == "ollama" else ("m",))
    res = client.get("/api/models")
    assert res.status_code == 200
    ollama = next(p for p in res.get_json()["providers"] if p["id"] == "ollama")
    assert ollama["auth_type"] == "none"
    assert ollama["models"] == ["local-a", "local-b"]


def test_openai_compatible_base_url_must_be_public_https():
    from llm.client import ProviderConfigError, validate_provider_base_url

    assert validate_provider_base_url("openai_compatible", "https://example.com/v1") == "https://example.com/v1"
    with pytest.raises(ProviderConfigError):
        validate_provider_base_url("openai_compatible", "http://example.com/v1")
    with pytest.raises(ProviderConfigError):
        validate_provider_base_url("openai_compatible", "https://127.0.0.1/v1")
    with pytest.raises(ProviderConfigError):
        validate_provider_base_url("openai_compatible", "https://169.254.169.254/v1")


def test_openai_compatible_client_uses_explicit_key(monkeypatch):
    from llm.client import OpenAICompatibleClient

    seen = {"authorization": None}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def _fake_urlopen(req, timeout=60):
        seen["authorization"] = req.get_header("Authorization")
        return _FakeResponse()

    # The client posts through its redirect-hardened opener (cross-host
    # redirects strip the BYO Authorization header), so patch that seam.
    monkeypatch.setattr("llm.client._SECURE_OPENER.open", _fake_urlopen)
    client = OpenAICompatibleClient("openrouter", "sk-or-v1-" + "A" * 32)
    assert client.complete(system="s", user="u") == "ok"
    assert seen["authorization"] == "Bearer sk-or-v1-" + "A" * 32


def test_openai_compatible_key_shape_validation():
    import llm_gateway

    llm_gateway.validate_provider_key("openrouter", "sk-or-v1-" + "A" * 32)
    llm_gateway.validate_provider_key("groq", "gsk_" + "A" * 32)
    with pytest.raises(llm_gateway.KeyValidationError):
        llm_gateway.validate_provider_key("openrouter", "short")


def test_no_key_provider_resolution_does_not_need_secret(client, fake_supabase):
    import llm_gateway

    with client.application.test_request_context():
        from flask import g

        g.owner_id = "00000000-0000-0000-0000-000000000001"
        key, managed = llm_gateway.resolve_llm("extract", provider="ollama")
    assert key == ""
    assert managed is False


def test_no_key_provider_status_uses_health(client, fake_supabase, monkeypatch):
    import llm_gateway

    monkeypatch.setattr(llm_gateway, "ollama_health", lambda: {"available": False, "error": "URLError"})
    with client.application.test_request_context():
        from flask import g

        g.owner_id = "00000000-0000-0000-0000-000000000001"
        status = llm_gateway.key_status_all()
    assert status["providers"]["ollama"]["auth_type"] == "none"
    assert status["providers"]["ollama"]["connected"] is False
    assert status["providers"]["ollama"]["error"] == "URLError"


def test_ai_extract_can_use_no_key_ollama(monkeypatch):
    from render import ai_extract

    class _Client:
        provider = "ollama"

        def complete(self, **_kwargs):
            return "name: Test User\nexperience:\n  - role: Engineer\n    company: Acme\n    start: '2024'\n    end: Present\n"

    monkeypatch.setattr(ai_extract, "ollama_health", lambda: {"available": True})
    monkeypatch.setattr(ai_extract, "make_client", lambda provider, api_key: _Client())
    assert ai_extract.is_available(api_key="", provider="ollama") is True
    assert "name: Test User" in ai_extract.extract("resume text", api_key="", provider="ollama")


def test_tailor_default_model_uses_provider_default_for_new_providers():
    import tailor_service
    from llm.client import default_model_for

    assert tailor_service._tailor_model("openrouter", None) == default_model_for("openrouter")
    assert tailor_service._tailor_model("ollama", None) == default_model_for("ollama")


def test_ai_routes_reject_invalid_explicit_provider(client, fake_supabase):
    res = client.post("/api/polish/bullet", json={"text": "Built systems.", "provider": "skynet"})
    assert res.status_code == 400
    assert res.get_json()["error"] == "invalid provider"


def test_ai_routes_reject_cross_provider_model(client, fake_supabase):
    res = client.post(
        "/api/polish/bullet",
        json={"text": "Built systems.", "provider": "gemini", "model": "claude-sonnet-4-5"},
    )
    assert res.status_code == 400
    assert res.get_json()["error"] == "invalid model"


def test_provider_migration_widens_check_and_preserves_grants():
    sql = (REPO / "supabase" / "migrations" / "0009_provider_registry.sql").read_text()
    for provider in ("ollama", "openrouter", "deepseek", "groq", "mistral", "openai_compatible"):
        assert provider in sql
    assert "managed_credits_remaining" not in sql.split("grant update", 1)[1]
    assert "deletion_requested_at" not in sql.split("grant update", 1)[1]


def test_log_redacts_new_provider_key_shapes():
    import logredact

    keys = [
        "sk-or-v1-" + "A" * 32,
        "gsk_" + "B" * 32,
        "sk-" + "C" * 32,
    ]
    for key in keys:
        assert key not in logredact.redact(f"provider error contained {key}")
