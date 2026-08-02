"""Thin, NO-OP-by-default analytics hook (Stage 9).

CV Copilot has exactly two product moments worth measuring for activation:
``signup`` (a brand-new account seeded its first CV) and ``first_export``
(that account built its first PDF). This module instruments both — but it
is **off unless explicitly configured**.

Design rules:

  * **No-op by default.** With ``POSTHOG_KEY`` unset, every call is a cheap
    early return. It NEVER raises, never blocks the request, never imports a
    network client, and never slows a route — a missing key is the normal
    case in dev/test/self-host.
  * **Best-effort, fire-and-forget.** When configured, the event is POSTed to
    PostHog's capture endpoint on a daemon thread with a short timeout, so a
    slow or down analytics backend can't stall the user's request. Any error
    is swallowed (analytics must never be load-bearing).
  * **No PII in the payload.** We send the verified ``owner_id`` as the
    ``distinct_id`` (already an opaque UUID) and a tiny set of non-identifying
    properties. No email, no CV content, no keys.

Configuration (see ``.env.example``):

    POSTHOG_KEY=phc_...            # PostHog project API key. Unset → no-op.
    POSTHOG_HOST=https://eu.posthog.com   # optional; EU cloud default.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request

_LOG = logging.getLogger("onepager.analytics")

# Read at import time; ``is_enabled`` re-reads so tests can toggle via env.
_DEFAULT_HOST = "https://eu.posthog.com"
_TIMEOUT_SECONDS = 3.0

# Event names — the two activation moments. Kept as constants so callers can't
# typo them and so a rename is a single edit.
EVENT_SIGNUP = "signup"
EVENT_FIRST_EXPORT = "first_export"


def _api_key() -> str:
    return (os.environ.get("POSTHOG_KEY") or "").strip()


def _host() -> str:
    return (os.environ.get("POSTHOG_HOST") or _DEFAULT_HOST).strip().rstrip("/")


def _demo_mode_enabled() -> bool:
    return (os.environ.get("ONEPAGER_DEMO_MODE") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def is_enabled() -> bool:
    """True only when a PostHog key is configured. Re-reads env every call."""
    # Local demo sessions are process-local and must remain network-isolated
    # even when the operator's shell exports production analytics settings.
    return bool(_api_key()) and not _demo_mode_enabled()


def capture(event: str, distinct_id: str, properties: dict | None = None) -> None:
    """Record an analytics event — a guaranteed no-op when unconfigured.

    Safe to call from any request path: it returns immediately when no
    ``POSTHOG_KEY`` is set, and otherwise dispatches the POST on a daemon
    thread so the caller is never blocked. Never raises.
    """
    if not is_enabled():
        return  # No-op: the normal dev/self-host case.
    if not event or not distinct_id:
        return
    try:
        payload = {
            "api_key": _api_key(),
            "event": str(event),
            "distinct_id": str(distinct_id),
            "properties": {"$lib": "onepager", **(properties or {})},
        }
        thread = threading.Thread(
            target=_post, args=(payload,), name="analytics-capture", daemon=True
        )
        thread.start()
    except Exception:  # noqa: BLE001 — analytics must never break a request
        _LOG.debug("analytics dispatch failed", exc_info=True)


def _post(payload: dict) -> None:
    """POST one event to PostHog's capture endpoint. Best-effort; swallows all."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{_host()}/capture/",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS)  # noqa: S310 — fixed host
    except Exception:  # noqa: BLE001 — never propagate; analytics is non-critical
        _LOG.debug("analytics POST failed", exc_info=True)
