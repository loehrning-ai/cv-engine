from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools/ci/secret_scan_range.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "secret_scan_range_test_module",
    SCRIPT_PATH,
)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
scan_range = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = scan_range
MODULE_SPEC.loader.exec_module(scan_range)

BASE = "a" * 40
HEAD = "b" * 40
MERGE = "c" * 40
SECRET = "DO_NOT_PRINT_EVENT_SECRET"


def _write_event(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_pull_request_scans_only_introduced_commits(tmp_path: Path) -> None:
    event = _write_event(
        tmp_path,
        {
            "pull_request": {
                "base": {"sha": BASE},
                "head": {"sha": HEAD},
            }
        },
    )
    resolved = scan_range.run("pull_request", event, MERGE)
    assert resolved == (BASE, HEAD, f"{BASE}..{HEAD}")


def test_push_scans_only_commits_after_before_sha(tmp_path: Path) -> None:
    event = _write_event(tmp_path, {"before": BASE, "after": HEAD})
    resolved = scan_range.run("push", event, HEAD)
    assert resolved == (BASE, HEAD, f"{BASE}..{HEAD}")


def test_first_push_scans_all_ancestry_from_sanitized_root(tmp_path: Path) -> None:
    event = _write_event(
        tmp_path,
        {"before": scan_range.ZERO_SHA, "after": HEAD},
    )
    resolved = scan_range.run("push", event, HEAD)
    assert resolved == ("", HEAD, HEAD)


def test_schedule_scans_all_reachable_history(tmp_path: Path) -> None:
    event = _write_event(tmp_path, {"repository": {"private": False}})
    resolved = scan_range.run("schedule", event, HEAD)
    assert resolved == ("", HEAD, HEAD)


@pytest.mark.parametrize(
    ("event_name", "payload", "github_sha"),
    [
        ("workflow_dispatch", {}, HEAD),
        ("pull_request", {"pull_request": {"base": {}, "head": {"sha": HEAD}}}, MERGE),
        (
            "pull_request",
            {"pull_request": {"base": {"sha": BASE}, "head": {"sha": "ABC"}}},
            MERGE,
        ),
        ("push", {"before": "bad", "after": HEAD}, HEAD),
        ("push", {"before": BASE, "after": scan_range.ZERO_SHA}, scan_range.ZERO_SHA),
        ("push", {"before": BASE, "after": HEAD}, MERGE),
        ("schedule", {}, "ABC"),
    ],
)
def test_invalid_event_ranges_fail_closed(
    tmp_path: Path,
    event_name: str,
    payload: object,
    github_sha: str,
) -> None:
    event = _write_event(tmp_path, payload)
    with pytest.raises(scan_range.RangeError):
        scan_range.run(event_name, event, github_sha)


def test_cli_outputs_only_validated_github_output_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event = _write_event(tmp_path, {"before": BASE, "after": HEAD})
    assert scan_range.main(["push", str(event), HEAD]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        f"base_sha={BASE}",
        f"head_sha={HEAD}",
        f"gitleaks_log_opts={BASE}..{HEAD}",
    ]


def test_malformed_event_fails_without_echoing_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    event = tmp_path / "event.json"
    event.write_text(f'{{"secret":"{SECRET}"', encoding="utf-8")
    assert scan_range.main(["push", str(event), HEAD]) == 2
    captured = capsys.readouterr()
    assert "secret scan range failed" in captured.err
    assert SECRET not in captured.out + captured.err


def test_cli_rejects_missing_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert scan_range.main([]) == 2
    captured = capsys.readouterr()
    assert "secret scan range failed" in captured.err
