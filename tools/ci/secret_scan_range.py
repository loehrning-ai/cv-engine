#!/usr/bin/env python3
"""Resolve the exact commit range that CI secret scanners must inspect.

Pull requests and ordinary pushes scan only commits introduced by the event.
This lets the private release repository certify new work without treating its
explicitly non-publishable legacy ancestry as release content. Scheduled runs
and first pushes have no base commit, so scanners walk all ancestry reachable
from the selected head. The future public repository starts from one sanitized
root commit, making that full-history path the public release invariant.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

SHA_RE = re.compile(r"[0-9a-f]{40}")
ZERO_SHA = "0" * 40
SUPPORTED_EVENTS = {"pull_request", "push", "schedule"}


class RangeError(RuntimeError):
    """A safe-to-print range resolution failure."""


class SecretScanRange(NamedTuple):
    """Validated scanner inputs."""

    base_sha: str
    head_sha: str
    gitleaks_log_opts: str


def _sha(value: object, label: str, *, allow_zero: bool = False) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise RangeError(f"{label} is not a lowercase 40-character commit SHA")
    if value == ZERO_SHA and not allow_zero:
        raise RangeError(f"{label} must not be the zero SHA")
    return value


def _load_event(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RangeError("GitHub event payload is unavailable or malformed") from None
    if not isinstance(payload, dict):
        raise RangeError("GitHub event payload is unavailable or malformed")
    return payload


def _nested_sha(
    payload: dict[str, object],
    keys: tuple[str, ...],
    label: str,
) -> str:
    value: object = payload
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise RangeError(f"GitHub event payload is missing {label}")
        value = value[key]
    return _sha(value, label)


def resolve_range(
    event_name: str,
    github_sha: str,
    payload: dict[str, object],
) -> SecretScanRange:
    """Resolve scanner inputs from a validated GitHub event payload."""
    if event_name not in SUPPORTED_EVENTS:
        raise RangeError("GitHub event type is unsupported")
    checked_github_sha = _sha(github_sha, "GITHUB_SHA")

    if event_name == "pull_request":
        base_sha = _nested_sha(
            payload,
            ("pull_request", "base", "sha"),
            "pull request base SHA",
        )
        head_sha = _nested_sha(
            payload,
            ("pull_request", "head", "sha"),
            "pull request head SHA",
        )
    elif event_name == "push":
        before_sha = _sha(
            payload.get("before"),
            "push before SHA",
            allow_zero=True,
        )
        head_sha = _sha(payload.get("after"), "push after SHA")
        if head_sha != checked_github_sha:
            raise RangeError("push after SHA does not match GITHUB_SHA")
        base_sha = "" if before_sha == ZERO_SHA else before_sha
    else:
        base_sha = ""
        head_sha = checked_github_sha

    log_opts = f"{base_sha}..{head_sha}" if base_sha else head_sha
    return SecretScanRange(base_sha, head_sha, log_opts)


def run(
    event_name: str,
    event_path: Path,
    github_sha: str,
) -> SecretScanRange:
    """Load an event and resolve its scanner range."""
    return resolve_range(event_name, github_sha, _load_event(event_path))


def main(argv: list[str] | None = None) -> int:
    """Write validated values in GitHub Actions output-file syntax."""
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 3:
        print("secret scan range failed: expected event, path, and SHA", file=sys.stderr)
        return 2

    try:
        resolved = run(arguments[0], Path(arguments[1]), arguments[2])
    except RangeError as error:
        print(f"secret scan range failed: {error}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - never echo untrusted event details.
        print("secret scan range failed: unexpected internal error", file=sys.stderr)
        return 2

    print(f"base_sha={resolved.base_sha}")
    print(f"head_sha={resolved.head_sha}")
    print(f"gitleaks_log_opts={resolved.gitleaks_log_opts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
