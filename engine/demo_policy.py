"""Fail-closed policy for the local-only demo mode.

``ONEPAGER_DEMO_MODE`` deliberately relaxes authentication and provider
requirements. The flag alone is therefore insufficient authorization:

* an explicitly configured ``PUBLIC_BASE_URL`` must name loopback;
* every HTTP request must use a loopback Host and arrive from a loopback peer.

The direct development server binds to ``127.0.0.1``, so a fresh clone remains
zero-configuration. A reverse proxy, container, or public WSGI binding cannot
turn the demo path into an internet-facing login by setting one environment
variable.
"""
from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

_TRUTHY = frozenset({"1", "true", "yes"})
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
LOCAL_PRIVACY_CONTACT = "privacy@localhost.invalid"


def demo_mode_requested(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the operator explicitly requested demo mode."""
    env = os.environ if environ is None else environ
    return (env.get("ONEPAGER_DEMO_MODE") or "").strip().lower() in _TRUTHY


def _is_loopback_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _configured_base_url_error(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    raw = (env.get("PUBLIC_BASE_URL") or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return "PUBLIC_BASE_URL is malformed"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is not None and not 1 <= port <= 65535)
    ):
        return "PUBLIC_BASE_URL must be a loopback HTTP(S) origin"
    if not _is_loopback_hostname(parsed.hostname):
        return "PUBLIC_BASE_URL names a non-loopback origin"
    return None


def demo_mode_configuration_error(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return the unsafe-config reason, or ``None`` when demo mode is safe."""
    env = os.environ if environ is None else environ
    if not demo_mode_requested(env):
        return None
    return _configured_base_url_error(env)


def assert_demo_mode_configuration_safe(
    environ: Mapping[str, str] | None = None,
) -> None:
    """Refuse application startup for an explicitly public demo origin."""
    error = demo_mode_configuration_error(environ)
    if error:
        raise RuntimeError(f"Refusing unsafe demo mode: {error}")


def demo_mode_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return true only for an explicit flag with a loopback-safe base URL."""
    env = os.environ if environ is None else environ
    return demo_mode_requested(env) and demo_mode_configuration_error(env) is None


def demo_request_is_loopback(
    host: str | None,
    remote_addr: str | None,
    proxy_headers: Mapping[str, str] | None = None,
) -> bool:
    """Require direct loopback with no reverse-proxy forwarding evidence."""
    if proxy_headers and any(
        (value or "").strip() for value in proxy_headers.values()
    ):
        return False
    try:
        parsed_host = urlsplit(f"//{host or ''}").hostname
    except ValueError:
        return False
    if not _is_loopback_hostname(parsed_host):
        return False
    if not remote_addr:
        return False
    try:
        return ipaddress.ip_address(remote_addr.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def resolve_privacy_contact(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return a validated controller email or refuse hosted startup.

    A loopback-only development instance receives a reserved ``.invalid``
    placeholder. Any non-loopback ``PUBLIC_BASE_URL`` must explicitly name the
    operator's monitored controller address; the project maintainer is never
    used as an implicit default.
    """
    env = os.environ if environ is None else environ
    raw_base = (env.get("PUBLIC_BASE_URL") or "http://localhost:8000").strip()
    try:
        parsed = urlsplit(raw_base)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("Refusing startup: PUBLIC_BASE_URL is malformed") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise RuntimeError(
            "Refusing startup: PUBLIC_BASE_URL must be an HTTP(S) origin"
        )

    contact = (env.get("ONEPAGER_PRIVACY_CONTACT") or "").strip()
    hosted = not _is_loopback_hostname(parsed.hostname)
    if hosted and not contact:
        raise RuntimeError(
            "Refusing hosted startup: ONEPAGER_PRIVACY_CONTACT is required"
        )
    if not contact:
        return LOCAL_PRIVACY_CONTACT
    if (
        len(contact) > 254
        or "\r" in contact
        or "\n" in contact
        or not _EMAIL_RE.fullmatch(contact)
    ):
        raise RuntimeError(
            "Refusing startup: ONEPAGER_PRIVACY_CONTACT is not a valid email"
        )
    return contact
