"""Zero-config demo login (fresh clone, no Supabase configured).

The README advertises a "zero cloud setup" local path: clone, install,
``python3 tools/editor/server.py`` with ``ONEPAGER_DEMO_MODE=true`` — no
``.env``, no Supabase. Previously that flow dead-ended: the auth gate hid
the demo button when Supabase config was absent, and ``/auth/demo-login``
returned 503 ``demo_unconfigured`` instead of falling back to a local demo
token (the same fallback a failed grant already used).
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AUTH_JS = REPO / "tools" / "editor" / "static" / "auth.js"


@pytest.fixture
def demo_no_config(client, monkeypatch):
    """Demo mode ON with Supabase config absent (fresh-clone simulation)."""
    import auth as auth_mod

    monkeypatch.setenv("ONEPAGER_DEMO_MODE", "true")
    monkeypatch.setattr(auth_mod, "SUPABASE_URL", "")
    monkeypatch.setattr(auth_mod, "SUPABASE_PUBLISHABLE_KEY", "")
    return client


def test_demo_login_falls_back_to_local_session_without_supabase(demo_no_config):
    res = demo_no_config.post("/auth/demo-login")
    assert res.status_code == 200, res.get_data(as_text=True)
    data = res.get_json()
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["local_demo"] is True


def test_local_demo_token_unlocks_api_without_supabase(demo_no_config):
    token = demo_no_config.post("/auth/demo-login").get_json()["access_token"]
    res = demo_no_config.get("/api/cv", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200


def test_auth_js_renders_demo_only_gate_without_config():
    """Static contract: the gate must offer the demo button when demo_mode is
    true even though Supabase config is absent (localOnly path), and boot()
    must not throw 'Auth is not configured' before considering demo mode."""
    src = AUTH_JS.read_text(encoding="utf-8")
    assert "opts.localOnly" in src
    assert 'renderLogin({ demoMode: true, localOnly: true })' in src
    # demoMode + local-demo session are resolved BEFORE the not-configured throw.
    assert src.index("demoMode = !!cfg.demo_mode") < src.index(
        'throw new Error("Auth is not configured on the server.")'
    )
