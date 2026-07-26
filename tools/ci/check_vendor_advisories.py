#!/usr/bin/env python3
"""Fail closed when pinned browser dependencies have OSV advisories."""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, Protocol, TextIO

REPO_ROOT = Path(__file__).resolve().parents[2]
BROWSER_ASSET_LOCK = REPO_ROOT / "tools/editor/static/vendor/browser-assets.lock.json"
ICON_ASSET_LOCK = REPO_ROOT / "LICENSES/icon-assets.lock.json"
OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

NPM_PACKAGE_RE = re.compile(
    r"(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*"
)
SEMVER_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
ADVISORY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class GateError(RuntimeError):
    """A safe-to-print fail-closed gate error."""


class PackagePin(NamedTuple):
    """An exact npm package pin."""

    name: str
    version: str


class HttpResponse(Protocol):
    """The response surface used by the OSV client."""

    status: int

    def read(self, amount: int = -1) -> bytes:
        """Read response bytes."""

    def __enter__(self) -> HttpResponse:
        """Enter the response context."""

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> bool | None:
        """Exit the response context."""


Opener = Callable[..., HttpResponse]


def _load_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise GateError(f"{label} is unavailable or malformed") from None


def _parse_pin(name: object, version: object, label: str) -> PackagePin:
    if (
        not isinstance(name, str)
        or len(name) > 214
        or NPM_PACKAGE_RE.fullmatch(name) is None
        or not isinstance(version, str)
        or SEMVER_RE.fullmatch(version) is None
    ):
        raise GateError(f"{label} contains an invalid npm package pin")
    return PackagePin(name=name, version=version)


def _browser_pins(path: Path) -> tuple[PackagePin, ...]:
    payload = _load_json(path, "browser asset lock")
    if not isinstance(payload, dict) or type(payload.get("schema")) is not int:
        raise GateError("browser asset lock has an unsupported schema")
    if payload["schema"] != 1:
        raise GateError("browser asset lock has an unsupported schema")

    packages = payload.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise GateError("browser asset lock has an invalid packages map")

    pins: list[PackagePin] = []
    for name, record in packages.items():
        if not isinstance(record, dict):
            raise GateError("browser asset lock has an invalid package record")
        pins.append(_parse_pin(name, record.get("version"), "browser asset lock"))

        if "bundled_components" not in record:
            continue
        bundled_components = record["bundled_components"]
        if not isinstance(bundled_components, dict):
            raise GateError("browser asset lock has an invalid bundled components map")
        for bundled_name, bundled_record in bundled_components.items():
            if not isinstance(bundled_record, dict):
                raise GateError(
                    "browser asset lock has an invalid bundled component record"
                )
            pins.append(
                _parse_pin(
                    bundled_name,
                    bundled_record.get("version"),
                    "browser asset lock",
                )
            )
    return tuple(pins)


def _icon_pins(path: Path) -> tuple[PackagePin, ...]:
    payload = _load_json(path, "icon asset lock")
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 2
    ):
        raise GateError("icon asset lock has an unsupported schema")

    source = payload.get("source")
    if not isinstance(source, dict):
        raise GateError("icon asset lock has an invalid source record")
    return (
        _parse_pin(
            source.get("package"),
            source.get("version"),
            "icon asset lock",
        ),
    )


def load_package_pins(
    browser_lock: Path = BROWSER_ASSET_LOCK,
    icon_lock: Path = ICON_ASSET_LOCK,
) -> tuple[PackagePin, ...]:
    """Load and validate every pinned npm dependency from both asset locks."""
    versions: dict[str, str] = {}
    for pin in (*_browser_pins(browser_lock), *_icon_pins(icon_lock)):
        previous = versions.get(pin.name)
        if previous is not None and previous != pin.version:
            raise GateError("asset locks contain conflicting npm package pins")
        versions[pin.name] = pin.version

    if not versions:
        raise GateError("asset locks contain no npm package pins")
    return tuple(PackagePin(name, version) for name, version in sorted(versions.items()))


def _parse_osv_response(
    raw_response: bytes,
    pins: tuple[PackagePin, ...],
) -> tuple[tuple[PackagePin, tuple[str, ...]], ...]:
    try:
        payload = json.loads(raw_response)
    except (UnicodeError, json.JSONDecodeError):
        raise GateError("OSV returned a malformed response") from None

    if not isinstance(payload, dict):
        raise GateError("OSV returned a malformed response")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != len(pins):
        raise GateError("OSV returned a malformed response")

    findings: list[tuple[PackagePin, tuple[str, ...]]] = []
    for pin, result in zip(pins, results, strict=True):
        if not isinstance(result, dict) or "error" in result:
            raise GateError("OSV returned a malformed response")
        if "vulns" not in result:
            continue
        vulnerabilities = result["vulns"]
        if not isinstance(vulnerabilities, list):
            raise GateError("OSV returned a malformed response")

        advisory_ids: set[str] = set()
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise GateError("OSV returned a malformed response")
            advisory_id = vulnerability.get("id")
            if (
                not isinstance(advisory_id, str)
                or ADVISORY_ID_RE.fullmatch(advisory_id) is None
            ):
                raise GateError("OSV returned a malformed response")
            advisory_ids.add(advisory_id)
        if advisory_ids:
            findings.append((pin, tuple(sorted(advisory_ids))))
    return tuple(findings)


def query_osv(
    pins: tuple[PackagePin, ...],
    *,
    opener: Opener | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[tuple[PackagePin, tuple[str, ...]], ...]:
    """Query the official OSV batch API for exact npm package versions."""
    request_body = json.dumps(
        {
            "queries": [
                {
                    "package": {"ecosystem": "npm", "name": pin.name},
                    "version": pin.version,
                }
                for pin in pins
            ]
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    request = urllib.request.Request(
        OSV_BATCH_URL,
        data=request_body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cv-engine-vendor-advisory-gate/1",
        },
        method="POST",
    )
    opener_function = opener if opener is not None else urllib.request.urlopen

    try:
        with opener_function(request, timeout=timeout) as response:
            if getattr(response, "status", None) != 200:
                raise GateError("OSV advisory query failed")
            raw_response = response.read(MAX_RESPONSE_BYTES + 1)
    except GateError:
        raise
    except Exception:  # noqa: BLE001 - fail closed without exposing transport details.
        raise GateError("OSV advisory query unavailable") from None

    if not isinstance(raw_response, bytes):
        raise GateError("OSV returned a malformed response")
    if len(raw_response) > MAX_RESPONSE_BYTES:
        raise GateError("OSV returned an oversized response")
    return _parse_osv_response(raw_response, pins)


def run_gate(
    *,
    browser_lock: Path = BROWSER_ASSET_LOCK,
    icon_lock: Path = ICON_ASSET_LOCK,
    opener: Opener | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the gate. Return 0 clean, 1 vulnerable, or 2 on gate failure."""
    output = sys.stdout if stdout is None else stdout
    error_output = sys.stderr if stderr is None else stderr

    try:
        pins = load_package_pins(browser_lock, icon_lock)
        findings = query_osv(pins, opener=opener, timeout=timeout)
    except GateError as error:
        print(f"vendor advisory gate failed: {error}", file=error_output)
        return 2
    except Exception:  # noqa: BLE001 - prevent accidental disclosure on fail-closed exit.
        print("vendor advisory gate failed: unexpected internal error", file=error_output)
        return 2

    if findings:
        print("vendor advisory gate blocked:", file=error_output)
        for pin, advisory_ids in findings:
            for advisory_id in advisory_ids:
                print(f"{pin.name}@{pin.version}: {advisory_id}", file=error_output)
        return 1

    print(
        f"vendor advisory gate passed: {len(pins)} pinned npm packages, "
        "no OSV advisories",
        file=output,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print("vendor advisory gate failed: unexpected command arguments", file=sys.stderr)
        return 2
    return run_gate()


if __name__ == "__main__":
    raise SystemExit(main())
