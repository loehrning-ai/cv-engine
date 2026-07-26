from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools/ci/check_vendor_advisories.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "check_vendor_advisories_test_module",
    SCRIPT_PATH,
)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
vendor_gate = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = vendor_gate
MODULE_SPEC.loader.exec_module(vendor_gate)

SECRET = "DO_NOT_PRINT_SECRET"


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            return self.body
        return self.body[:amount]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_arguments: object) -> bool:
        return False


def _browser_lock() -> dict[str, object]:
    return {
        "schema": 1,
        "packages": {
            "@scope/editor": {"version": "1.2.3"},
            "pagedjs": {
                "version": "0.4.3",
                "bundled_components": {
                    "source-map": {"version": "0.6.1"},
                },
            },
        },
    }


def _icon_lock() -> dict[str, object]:
    return {
        "schema_version": 2,
        "source": {
            "package": "lucide-static",
            "version": "1.26.0",
        },
    }


def _write_locks(
    tmp_path: Path,
    *,
    browser: dict[str, object] | None = None,
    icon: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    browser_path = tmp_path / "browser-assets.lock.json"
    icon_path = tmp_path / "icon-assets.lock.json"
    browser_path.write_text(
        json.dumps(_browser_lock() if browser is None else browser),
        encoding="utf-8",
    )
    icon_path.write_text(
        json.dumps(_icon_lock() if icon is None else icon),
        encoding="utf-8",
    )
    return browser_path, icon_path


def _json_bytes(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def test_clean_response_passes_and_queries_every_pin(tmp_path: Path) -> None:
    browser_path, icon_path = _write_locks(tmp_path)
    captured: dict[str, object] = {}

    def clean_opener(request: object, *, timeout: float) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        payload = json.loads(request.data)
        return FakeResponse(
            _json_bytes({"results": [{} for _query in payload["queries"]]})
        )

    stdout = io.StringIO()
    stderr = io.StringIO()
    result = vendor_gate.run_gate(
        browser_lock=browser_path,
        icon_lock=icon_path,
        opener=clean_opener,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0
    assert stderr.getvalue() == ""
    assert "4 pinned npm packages" in stdout.getvalue()
    request = captured["request"]
    payload = json.loads(request.data)
    assert [
        (query["package"]["name"], query["version"]) for query in payload["queries"]
    ] == [
        ("@scope/editor", "1.2.3"),
        ("lucide-static", "1.26.0"),
        ("pagedjs", "0.4.3"),
        ("source-map", "0.6.1"),
    ]
    assert all(
        query["package"]["ecosystem"] == "npm" for query in payload["queries"]
    )
    assert request.full_url == vendor_gate.OSV_BATCH_URL
    assert captured["timeout"] == vendor_gate.DEFAULT_TIMEOUT_SECONDS


def test_vulnerable_response_blocks_with_only_package_and_advisory_ids(
    tmp_path: Path,
) -> None:
    browser_path, icon_path = _write_locks(tmp_path)

    def vulnerable_opener(request: object, *, timeout: float) -> FakeResponse:
        assert timeout == vendor_gate.DEFAULT_TIMEOUT_SECONDS
        payload = json.loads(request.data)
        results: list[dict[str, object]] = [{} for _query in payload["queries"]]
        vulnerable_index = next(
            index
            for index, query in enumerate(payload["queries"])
            if query["package"]["name"] == "lucide-static"
        )
        results[vulnerable_index] = {
            "vulns": [
                {
                    "id": "GHSA-1234-5678-9abc",
                    "details": SECRET,
                },
                {"id": "CVE-2026-12345"},
                {"id": "GHSA-1234-5678-9abc"},
            ]
        }
        return FakeResponse(_json_bytes({"results": results}))

    stdout = io.StringIO()
    stderr = io.StringIO()
    result = vendor_gate.run_gate(
        browser_lock=browser_path,
        icon_lock=icon_path,
        opener=vulnerable_opener,
        stdout=stdout,
        stderr=stderr,
    )

    output = stdout.getvalue() + stderr.getvalue()
    assert result == 1
    assert stdout.getvalue() == ""
    assert "lucide-static@1.26.0: CVE-2026-12345" in stderr.getvalue()
    assert "lucide-static@1.26.0: GHSA-1234-5678-9abc" in stderr.getvalue()
    assert stderr.getvalue().count("GHSA-1234-5678-9abc") == 1
    assert SECRET not in output
    assert "details" not in output


@pytest.mark.parametrize(
    "response_body",
    [
        b'{"results":[' + SECRET.encode("utf-8"),
        _json_bytes({"results": [{"details": SECRET}]}),
        _json_bytes(
            {
                "results": [
                    {},
                    {"vulns": [{"id": f"BAD ID {SECRET}"}]},
                    {},
                    {},
                ]
            }
        ),
    ],
)
def test_malformed_response_fails_closed_without_echoing_content(
    tmp_path: Path,
    response_body: bytes,
) -> None:
    browser_path, icon_path = _write_locks(tmp_path)

    def malformed_opener(_request: object, *, timeout: float) -> FakeResponse:
        assert timeout == vendor_gate.DEFAULT_TIMEOUT_SECONDS
        return FakeResponse(response_body)

    stdout = io.StringIO()
    stderr = io.StringIO()
    result = vendor_gate.run_gate(
        browser_lock=browser_path,
        icon_lock=icon_path,
        opener=malformed_opener,
        stdout=stdout,
        stderr=stderr,
    )

    output = stdout.getvalue() + stderr.getvalue()
    assert result == 2
    assert "vendor advisory gate failed" in stderr.getvalue()
    assert SECRET not in output


def test_unavailable_osv_fails_closed_without_echoing_exception(
    tmp_path: Path,
) -> None:
    browser_path, icon_path = _write_locks(tmp_path)

    def unavailable_opener(_request: object, *, timeout: float) -> FakeResponse:
        assert timeout == vendor_gate.DEFAULT_TIMEOUT_SECONDS
        raise urllib.error.URLError(SECRET)

    stdout = io.StringIO()
    stderr = io.StringIO()
    result = vendor_gate.run_gate(
        browser_lock=browser_path,
        icon_lock=icon_path,
        opener=unavailable_opener,
        stdout=stdout,
        stderr=stderr,
    )

    output = stdout.getvalue() + stderr.getvalue()
    assert result == 2
    assert "OSV advisory query unavailable" in stderr.getvalue()
    assert SECRET not in output


@pytest.mark.parametrize(
    "case",
    [
        "browser_schema",
        "bundled_components",
        "icon_schema",
        "missing_icon_version",
        "conflicting_pin",
        "version_range",
    ],
)
def test_invalid_lock_schema_fails_before_network(
    tmp_path: Path,
    case: str,
) -> None:
    browser = copy.deepcopy(_browser_lock())
    icon = copy.deepcopy(_icon_lock())

    if case == "browser_schema":
        browser["schema"] = 2
    elif case == "bundled_components":
        browser["packages"]["pagedjs"]["bundled_components"] = SECRET
    elif case == "icon_schema":
        icon["schema_version"] = 1
    elif case == "missing_icon_version":
        del icon["source"]["version"]
    elif case == "conflicting_pin":
        browser["packages"]["lucide-static"] = {"version": "9.9.9"}
    elif case == "version_range":
        browser["packages"]["@scope/editor"]["version"] = "^1.2.3"

    browser_path, icon_path = _write_locks(
        tmp_path,
        browser=browser,
        icon=icon,
    )
    network_called = False

    def forbidden_opener(_request: object, *, timeout: float) -> FakeResponse:
        nonlocal network_called
        network_called = True
        raise AssertionError(timeout)

    stdout = io.StringIO()
    stderr = io.StringIO()
    result = vendor_gate.run_gate(
        browser_lock=browser_path,
        icon_lock=icon_path,
        opener=forbidden_opener,
        stdout=stdout,
        stderr=stderr,
    )

    output = stdout.getvalue() + stderr.getvalue()
    assert result == 2
    assert not network_called
    assert "vendor advisory gate failed" in stderr.getvalue()
    assert SECRET not in output
