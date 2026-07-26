from __future__ import annotations

from pathlib import Path

import pytest

from tools.ci import live_rls_gate

REPO = Path(__file__).resolve().parent.parent


def _complete_scoped_env() -> dict[str, str]:
    return {
        "ONEPAGER_RLS_SUPABASE_URL": "https://project.supabase.co",
        "ONEPAGER_RLS_SUPABASE_PUBLISHABLE_KEY": "publishable",
        "ONEPAGER_RLS_SUPABASE_JWKS_URL": "https://project.supabase.co/auth/v1/.well-known/jwks.json",
        "ONEPAGER_RLS_USER_A_JWT": "jwt-a",
        "ONEPAGER_RLS_USER_A_ID": "00000000-0000-4000-8000-0000000000aa",
        "ONEPAGER_RLS_USER_B_JWT": "jwt-b",
        "ONEPAGER_RLS_USER_B_ID": "00000000-0000-4000-8000-0000000000bb",
    }


def test_live_rls_gate_maps_scoped_env_to_legacy_test_env() -> None:
    source = _complete_scoped_env()
    source.update(
        {
            "HOME": "/tmp/home",
            "PATH": "/usr/bin",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role",
            "TEST_A_TOKEN": "legacy-token",
        }
    )
    env, missing = live_rls_gate.resolved_test_env(source)
    assert missing == []
    assert env["SUPABASE_URL"] == "https://project.supabase.co"
    assert env["SUPABASE_PUBLISHABLE_KEY"] == "publishable"
    assert env["SUPABASE_JWKS_URL"].endswith("/jwks.json")
    assert env["TEST_A_TOKEN"] == "jwt-a"
    assert env["TEST_A_ID"].endswith("aa")
    assert env["TEST_B_TOKEN"] == "jwt-b"
    assert env["TEST_B_ID"].endswith("bb")
    assert env["HOME"] == "/tmp/home"
    assert env["PATH"] == "/usr/bin"
    assert "SUPABASE_SERVICE_ROLE_KEY" not in env
    assert "SUPABASE_SERVICE_ROLE_KEY" not in live_rls_gate.ENV_MAP


def test_live_rls_gate_ignores_legacy_generic_env_names() -> None:
    env, missing = live_rls_gate.resolved_test_env(
        {
            "SUPABASE_URL": "https://legacy.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "legacy-publishable",
            "SUPABASE_JWKS_URL": "https://legacy.supabase.co/jwks.json",
            "TEST_A_TOKEN": "legacy-a",
            "TEST_A_ID": "legacy-a-id",
            "TEST_B_TOKEN": "legacy-b",
            "TEST_B_ID": "legacy-b-id",
        }
    )
    assert env == {}
    assert missing == list(live_rls_gate.ENV_MAP.values())


def test_live_rls_gate_rejects_service_role_shaped_publishable_key() -> None:
    env = _complete_scoped_env()
    env["ONEPAGER_RLS_SUPABASE_PUBLISHABLE_KEY"] = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJyb2xlIjoic2VydmljZV9yb2xlIiwiaXNzIjoic3VwYWJhc2UifQ."
        "signature"
    )
    child_env, missing = live_rls_gate.resolved_test_env(env)
    assert "SUPABASE_PUBLISHABLE_KEY" not in child_env
    assert missing == ["ONEPAGER_RLS_SUPABASE_PUBLISHABLE_KEY"]


def test_live_rls_gate_skips_locally_when_unprovisioned(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ONEPAGER_LIVE_RLS_REQUIRED", raising=False)
    monkeypatch.setattr(live_rls_gate.subprocess, "run", pytest.fail)
    assert live_rls_gate.main([]) == 0
    out = capsys.readouterr().out
    assert "live RLS gate skipped" in out
    assert "ONEPAGER_RLS_USER_A_JWT" in out
    assert "jwt-" not in out


def test_live_rls_gate_fails_closed_when_required(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ONEPAGER_LIVE_RLS_REQUIRED", "1")
    for key in live_rls_gate.ENV_MAP.values():
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(live_rls_gate.subprocess, "run", pytest.fail)
    assert live_rls_gate.main([]) == 2
    err = capsys.readouterr().err
    assert "live RLS gate failed" in err
    assert "ONEPAGER_RLS_SUPABASE_URL" in err


def test_live_rls_gate_runs_focused_pytest_targets(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Result:
        returncode = 7

    def fake_run(cmd, *, env, check):
        captured["cmd"] = cmd
        captured["env"] = env
        captured["check"] = check
        return Result()

    for key, value in _complete_scoped_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(live_rls_gate.subprocess, "run", fake_run)

    assert live_rls_gate.main([]) == 7
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[:3] == [live_rls_gate.sys.executable, "-m", "pytest"]
    assert "tests/test_stage7_library.py::TestCrossTenantLibrary" in cmd
    assert "tests/test_stage10_account.py::TestPurgeCascadeReal" not in cmd
    assert captured["check"] is False
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["TEST_A_TOKEN"] == "jwt-a"
    assert "SUPABASE_SERVICE_ROLE_KEY" not in env
    assert "SUPABASE_SERVICE_ROLE_KEY" not in live_rls_gate.ENV_MAP


def test_workflow_declares_live_rls_gate_without_service_role_or_generic_tokens() -> None:
    workflow = (REPO / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "ONEPAGER_LIVE_RLS_REQUIRED: \"1\"" in workflow
    assert "python tools/ci/live_rls_gate.py" in workflow
    start = workflow.index("- name: Live RLS token gate")
    end = workflow.index("- name: Auth bootstrap browser gate")
    live_step = workflow[start:end]
    assert "SUPABASE_SERVICE_ROLE_KEY" not in live_step
    assert "TEST_A_TOKEN" not in live_step
    assert "TEST_B_TOKEN" not in live_step
