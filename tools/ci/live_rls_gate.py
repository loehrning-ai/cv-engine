#!/usr/bin/env python3
"""Fail-closed live RLS test gate for CI.

Local runs skip honestly when the provisioned Supabase project and two real
user JWTs are absent. CI can declare the gate mandatory with
``ONEPAGER_LIVE_RLS_REQUIRED=1``; in that mode missing env is a hard failure.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from collections.abc import Mapping

TRUTHY = {"1", "true", "yes", "on", "required"}

# Scoped CI secret names are mapped to the legacy names used by existing
# live-token tests only inside the child pytest environment. Do not add
# service-role material here; this gate is for user-JWT RLS behavior only.
ENV_MAP: dict[str, str] = {
    "SUPABASE_URL": "ONEPAGER_RLS_SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY": "ONEPAGER_RLS_SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_JWKS_URL": "ONEPAGER_RLS_SUPABASE_JWKS_URL",
    "TEST_A_TOKEN": "ONEPAGER_RLS_USER_A_JWT",
    "TEST_A_ID": "ONEPAGER_RLS_USER_A_ID",
    "TEST_B_TOKEN": "ONEPAGER_RLS_USER_B_JWT",
    "TEST_B_ID": "ONEPAGER_RLS_USER_B_ID",
}

PASS_THROUGH_ENV = (
    "CI",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPATH",
    "PYTHONWARNINGS",
    "TEMP",
    "TMP",
    "TMPDIR",
)

PYTEST_TARGETS = [
    "tests/test_stage2_auth.py",
    "tests/test_stage5_cover_letter.py::TestCoverLetterRLS",
    "tests/test_stage6_catalog.py::TestCrossTenantDbLayer",
    "tests/test_stage6_tailor.py::TestTailorRLS",
    "tests/test_stage7_jobs.py::TestJobsRLS",
    "tests/test_stage7_library.py::TestCrossTenantLibrary",
    "tests/test_stage8_pdf_store.py::TestPdfArtifactsRLS",
    "tests/test_stage10_account.py::TestCrossTenantReal",
]


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def gate_required(environ: Mapping[str, str] | None = None) -> bool:
    env = environ or os.environ
    return truthy(env.get("ONEPAGER_LIVE_RLS_REQUIRED"))


def _jwt_payload(value: str) -> dict[str, object]:
    parts = value.split(".")
    if len(parts) != 3:
        return {}
    raw = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except Exception:  # noqa: BLE001 - malformed keys are handled by live auth tests
        return {}
    return payload if isinstance(payload, dict) else {}


def _looks_privileged_publishable_key(value: str) -> bool:
    lowered = value.lower()
    if "service_role" in lowered or "supabase_admin" in lowered:
        return True
    payload = _jwt_payload(value)
    role = str(payload.get("role") or "").lower()
    return role in {"service_role", "supabase_admin"}


def _base_child_env(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        key: str(environ[key])
        for key in PASS_THROUGH_ENV
        if environ.get(key)
    }


def resolved_test_env(
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    source = dict(environ or os.environ)
    env = _base_child_env(source)
    missing: list[str] = []
    for legacy_name, scoped_name in ENV_MAP.items():
        value = source.get(scoped_name, "").strip()
        if legacy_name == "SUPABASE_PUBLISHABLE_KEY" and _looks_privileged_publishable_key(value):
            value = ""
        if value:
            env[legacy_name] = value
        else:
            missing.append(scoped_name)
    return env, missing


def main(argv: list[str] | None = None) -> int:
    targets = argv if argv else PYTEST_TARGETS
    env, missing = resolved_test_env()
    if missing:
        message = (
            "live RLS gate skipped: missing "
            + ", ".join(missing)
            + " (set ONEPAGER_LIVE_RLS_REQUIRED=1 to fail closed)"
        )
        if gate_required():
            print(message.replace("skipped", "failed"), file=sys.stderr)
            return 2
        print(message)
        return 0

    cmd = [sys.executable, "-m", "pytest", *targets, "-q"]
    print("running live RLS gate: " + " ".join(targets))
    return subprocess.run(cmd, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
