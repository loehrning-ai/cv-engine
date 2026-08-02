from __future__ import annotations

import runpy
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_e2e_retry_guard_can_be_disabled_suite_wide(monkeypatch):
    plugin = runpy.run_path(str(REPO / "tests/e2e/conftest.py"))
    monkeypatch.setenv("ONEPAGER_E2E_RETRY", "0")

    # A bare object proves the env gate returns before touching pytest's item
    # protocol or attempting a hidden rerun.
    assert plugin["pytest_runtest_protocol"](object(), None) is None


def test_release_browser_gates_disable_retries() -> None:
    workflow = (REPO / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    browser_gate = workflow.split("- name: Auth bootstrap browser gate", 1)[1].split(
        "- name: Build and verify hardened containers",
        1,
    )[0]
    assert 'ONEPAGER_E2E_RETRY: "0"' in browser_gate

    deploy = (REPO / "DEPLOY.md").read_text(encoding="utf-8")
    browser_verification = deploy.split("Browser verification:", 1)[1].split(
        "Demo mode is restricted",
        1,
    )[0]
    assert "ONEPAGER_E2E_RETRY=0 ONEPAGER_E2E_BASE_URL=" in browser_verification
