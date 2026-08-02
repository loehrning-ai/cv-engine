"""Backend-only external application policy and attempt logging.

This module deliberately does not automate live websites. It classifies
application targets, checks whether a reviewed application packet is ready, and
records explicit user-confirmed manual-open or safe-fixture dry-run attempts.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ipaddress
import os
import re
import socket
import time
import uuid
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

import yaml

import auth as auth_mod
import cover_letter_service as cl_service
import jobs_service as jobs_svc
import local_demo_store
import pdf_store
import ssrf

APPLY_ENABLED_ENV = "ONEPAGER_EXTERNAL_APPLY_ENABLED"
APPLY_KILL_SWITCH_ENV = "ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH"
SAFE_FIXTURE_HOSTS_ENV = "ONEPAGER_EXTERNAL_APPLY_SAFE_FIXTURE_HOSTS"
ATTEMPT_TABLE = "application_apply_attempts"
PACKET_TABLE = "application_packets"

_FALSE_VALUES = {"0", "false", "no", "off"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_LINKEDIN_SUFFIXES = ("linkedin.com", "linkedin.cn", "lnkd.in")
_KNOWN_ATS_SUFFIXES = (
    "ashbyhq.com",
    "greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "lever.co",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
)
_DISALLOWED_APPLY_HOSTS = {"localhost", "metadata", "metadata.google.internal", "169.254.169.254"}
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_NUMERIC_IPV4_HOST_RE = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*\Z", re.IGNORECASE)
_REQUIRED_CONFIRMATIONS = (
    "user_confirmed",
    "reviewed_packet_confirmed",
    "profile_answers_complete",
    "legal_answers_confirmed",
    "visa_answers_confirmed",
)
APPLY_ACTION_RATE_LIMIT_PER_MINUTE = int(os.environ.get("ONEPAGER_EXTERNAL_APPLY_RATE_LIMIT_PER_MINUTE", "6"))
_APPLY_ACTION_BUCKETS: dict[str, list[float]] = {}


def _user_sb():
    if local_demo_store.active():
        return local_demo_store.db()
    return auth_mod.user_supabase()


def _server_sb():
    if local_demo_store.active():
        return local_demo_store.db()
    return auth_mod.server_supabase()


class ApplyError(Exception):
    """External-apply policy failure surfaced as a JSON response."""

    def __init__(self, status: int, payload: dict[str, Any]):
        super().__init__(payload.get("error") or payload.get("code") or "apply error")
        self.status = status
        self.payload = payload


def external_apply_config() -> dict[str, Any]:
    """Return fail-closed runtime policy.

    Both flags must be explicit for action routes to run:
    ``ONEPAGER_EXTERNAL_APPLY_ENABLED=1`` and
    ``ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH=0``. An unset or malformed kill-switch
    value is treated as active.
    """
    enabled = _env_truthy(APPLY_ENABLED_ENV)
    kill_raw = os.environ.get(APPLY_KILL_SWITCH_ENV)
    kill_switch_active = True if kill_raw is None else not _env_falsey_value(kill_raw)
    safe_fixture_hosts = sorted(_safe_fixture_hosts())
    return {
        "enabled": enabled,
        "kill_switch_active": kill_switch_active,
        "available": bool(enabled and not kill_switch_active),
        "safe_fixture_hosts": safe_fixture_hosts,
    }


def classify_apply_url(url: object) -> dict[str, Any]:
    """Classify a target URL without contacting it."""
    normalized, host, valid = _normalize_apply_url(url)
    safe_url = _redacted_url(url)
    base = {
        "url": safe_url,
        "host": host,
        "classification": "invalid",
        "method": "manual_open",
        "open_allowed": False,
        "dry_run_allowed": False,
        "submit_allowed": False,
        "automation_allowed": False,
        "reason_code": "invalid_application_url",
        "reason": "Application URL must be an http(s) URL without credentials.",
    }
    if not valid:
        return base
    if _apply_host_is_disallowed(host):
        return base

    if _host_matches(host, _LINKEDIN_SUFFIXES):
        return {
            **base,
            "classification": "linkedin",
            "open_allowed": True,
            "reason_code": "linkedin_manual_open_only",
            "reason": "LinkedIn targets are manual/open only.",
        }
    if _host_matches(host, _KNOWN_ATS_SUFFIXES):
        return {
            **base,
            "classification": "known_ats",
            "open_allowed": True,
            "reason_code": "known_ats_manual_open_only",
            "reason": "This ATS is recognized but has no enabled automation adapter.",
        }
    if host in _safe_fixture_hosts():
        return {
            **base,
            "classification": "safe_fixture",
            "open_allowed": True,
            "dry_run_allowed": True,
            "reason_code": "safe_fixture_host",
            "reason": "Dry-run is allowed only for this explicitly configured fixture host.",
        }
    return {
        **base,
        "classification": "unknown",
        "open_allowed": True,
        "reason_code": "unknown_manual_open_only",
        "reason": "Unknown hosts are manual/open only.",
    }


def application_readiness(job_id: str, packet_id: str | None = None) -> dict[str, Any]:
    """Return apply readiness using saved-job detail and packet state only."""
    if packet_id is not None and not _is_uuid(packet_id):
        raise ApplyError(400, {"error": "invalid packet id", "code": "invalid_packet_id"})
    detail = jobs_svc.job_detail(job_id)
    job = detail.get("job") or {}
    target = classify_apply_url(job.get("source_url"))
    if job.get("source_url_redacted"):
        target = {
            **target,
            "classification": "invalid",
            "open_allowed": False,
            "dry_run_allowed": False,
            "submit_allowed": False,
            "automation_allowed": False,
            "reason_code": "invalid_application_url",
            "reason": "Application URL was stored with credentials, query parameters, or a fragment and must be re-saved before opening.",
        }
    packet = _select_packet(detail.get("packets") or [], packet_id)
    blockers: list[dict[str, str]] = []

    if not (job.get("source_url") or "").strip():
        blockers.append(_blocker("missing_application_url", "Job has no application URL."))
    elif not target["open_allowed"]:
        blockers.append(_blocker("invalid_application_url", "Application URL is not safe to open."))

    source_metadata = job.get("source_metadata") if isinstance(job.get("source_metadata"), dict) else {}
    if source_metadata.get("full_jd_status") == "missing" or not (job.get("jd_text") or "").strip():
        blockers.append(_blocker("full_jd_required", "Full job description is required."))

    if packet_id and packet is None:
        blockers.append(_blocker("packet_not_found", "Requested application packet was not found."))
    elif packet is None:
        blockers.append(_blocker("reviewed_packet_missing", "A reviewed application packet is required."))
    else:
        blockers.extend(_packet_blockers(packet, detail))

    config = external_apply_config()
    ready = not blockers
    return {
        "job_id": job.get("id") or job_id,
        "ready": ready,
        "blockers": blockers,
        "attempts": application_attempt_history(job_id),
        "target": target,
        "packet": _public_packet(packet),
        "config": {
            "available": config["available"],
            "enabled": config["enabled"],
            "kill_switch_active": config["kill_switch_active"],
        },
        "requires_confirmation": True,
        "required_confirmations": list(_REQUIRED_CONFIRMATIONS),
        "actions": {
            "manual_open": bool(ready and target["open_allowed"] and config["available"]),
            "dry_run": bool(ready and target["dry_run_allowed"] and config["available"]),
            "submit": False,
        },
    }


def application_attempt_history(job_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Return the caller's append-only apply evidence for one job.

    Attempts are deliberately separate from the mutable saved-job status. A
    manual-open or fixture dry-run row proves only that exact action; neither
    action claims that an external submission happened.
    """
    bounded_limit = max(1, min(int(limit), 100))
    rows = (
        _user_sb()
        .table(ATTEMPT_TABLE)
        .select(
            "id, job_id, packet_id, action, status, reason_code, target_host, "
            "target_classification, submitted, automation_performed, created_at"
        )
        .eq("job_id", job_id)
        .order("created_at", desc=True)
        .limit(bounded_limit)
        .execute()
        .data
        or []
    )
    return [_public_attempt(row) for row in rows]


def run_apply_action(
    *,
    job_id: str,
    action: str,
    packet_id: str | None = None,
    request_payload: dict[str, Any] | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Run a no-network apply action and record an attempt log row."""
    if action not in {"manual_open", "dry_run"}:
        raise ApplyError(400, {"error": "unsupported apply action", "code": "unsupported_apply_action"})
    payload = request_payload if isinstance(request_payload, dict) else {}
    now = now_iso or _now_iso()
    readiness = application_readiness(job_id, packet_id=packet_id)
    target = readiness["target"]
    packet = readiness.get("packet") or {}
    config = external_apply_config()
    try:
        _enforce_apply_action_rate_limit(action, job_id)
    except ApplyError as exc:
        attempt = _record_attempt(
            job_id=job_id,
            packet_id=packet.get("id"),
            action=action,
            status="blocked",
            reason_code=exc.payload.get("code") or "external_apply_rate_limited",
            target=target,
            readiness=readiness,
            confirmations={},
            now_iso=now,
        )
        exc.payload["attempt"] = attempt
        exc.payload["ready"] = readiness["ready"]
        raise

    if not config["available"]:
        code = "external_apply_disabled" if not config["enabled"] else "external_apply_kill_switch"
        attempt = _record_attempt(
            job_id=job_id,
            packet_id=packet.get("id"),
            action=action,
            status="blocked",
            reason_code=code,
            target=target,
            readiness=readiness,
            confirmations={},
            now_iso=now,
        )
        raise ApplyError(
            403,
            {
                "error": "External apply actions are disabled.",
                "code": code,
                "attempt": attempt,
                "ready": readiness["ready"],
            },
        )

    confirmations = _confirmation_state(payload)
    missing_confirmations = [
        name for name in _REQUIRED_CONFIRMATIONS if confirmations.get(name) is not True
    ]
    confirmation_blocker_codes = {
        "profile_answers_incomplete",
        "legal_answers_unconfirmed",
        "visa_answers_unconfirmed",
    }
    non_confirmation_blockers = [
        blocker for blocker in readiness.get("blockers", [])
        if blocker.get("code") not in confirmation_blocker_codes
    ]
    if non_confirmation_blockers:
        reason = non_confirmation_blockers[0]["code"] if non_confirmation_blockers else "apply_not_ready"
        attempt = _record_attempt(
            job_id=job_id,
            packet_id=packet.get("id"),
            action=action,
            status="blocked",
            reason_code=reason,
            target=target,
            readiness=readiness,
            confirmations=confirmations,
            now_iso=now,
        )
        raise ApplyError(
            409,
            {
                "error": "Application packet is not ready.",
                "code": "apply_not_ready",
                "blockers": readiness["blockers"],
                "attempt": attempt,
            },
        )
    if missing_confirmations:
        attempt = _record_attempt(
            job_id=job_id,
            packet_id=packet.get("id"),
            action=action,
            status="blocked",
            reason_code="confirmation_required",
            target=target,
            readiness=readiness,
            confirmations=confirmations,
            now_iso=now,
        )
        raise ApplyError(
            400,
            {
                "error": "Explicit user confirmation is required.",
                "code": "confirmation_required",
                "missing_confirmations": missing_confirmations,
                "attempt": attempt,
            },
        )

    if packet.get("id") and _packet_needs_confirmation_persistence(packet, confirmations):
        _persist_packet_confirmations(str(packet.get("id")), confirmations, now_iso=now)
        readiness = application_readiness(job_id, packet_id=str(packet.get("id")))
        target = readiness["target"]
        packet = readiness.get("packet") or packet

    if not readiness["ready"]:
        reason = readiness["blockers"][0]["code"] if readiness["blockers"] else "apply_not_ready"
        attempt = _record_attempt(
            job_id=job_id,
            packet_id=packet.get("id"),
            action=action,
            status="blocked",
            reason_code=reason,
            target=target,
            readiness=readiness,
            confirmations={},
            now_iso=now,
        )
        raise ApplyError(
            409,
            {
                "error": "Application packet is not ready.",
                "code": "apply_not_ready",
                "blockers": readiness["blockers"],
                "attempt": attempt,
            },
        )

    if action == "dry_run":
        if not target["dry_run_allowed"]:
            attempt = _record_attempt(
                job_id=job_id,
                packet_id=packet.get("id"),
                action=action,
                status="blocked",
                reason_code="dry_run_not_allowed",
                target=target,
                readiness=readiness,
                confirmations=confirmations,
                now_iso=now,
            )
            raise ApplyError(
                403,
                {
                    "error": "Dry-run is allowed only for explicitly configured safe fixture hosts.",
                    "code": "dry_run_not_allowed",
                    "attempt": attempt,
                },
            )
        fields = _fixture_field_report(payload.get("fixture_fields"))
        attempt, readiness = _record_successful_attempt(
            job_id=job_id,
            packet_id=packet.get("id"),
            action=action,
            status="dry_run_completed",
            reason_code=target["reason_code"],
            target=target,
            readiness=readiness,
            confirmations=confirmations,
            now_iso=now,
            metadata={"field_count": len(fields), "field_names": [f["name"] for f in fields]},
        )
        target = readiness["target"]
        packet = readiness.get("packet") or packet
        return {
            "status": "dry_run_completed",
            "submitted": False,
            "automation_performed": False,
            "target": target,
            "packet": packet,
            "fields": fields,
            "attempt": attempt,
        }

    attempt, readiness = _record_successful_attempt(
        job_id=job_id,
        packet_id=packet.get("id"),
        action=action,
        status="manual_open_ready",
        reason_code=target["reason_code"],
        target=target,
        readiness=readiness,
        confirmations=confirmations,
        now_iso=now,
    )
    target = readiness["target"]
    packet = readiness.get("packet") or packet
    return {
        "status": "manual_open_ready",
        "submitted": False,
        "automation_performed": False,
        "open_url": target["url"],
        "target": target,
        "packet": packet,
        "attempt": attempt,
    }


def _record_attempt(
    *,
    job_id: str,
    packet_id: str | None,
    action: str,
    status: str,
    reason_code: str,
    target: dict[str, Any],
    readiness: dict[str, Any],
    confirmations: dict[str, bool],
    now_iso: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "job_id": job_id,
        "packet_id": packet_id,
        "action": action,
        "status": status,
        "reason_code": reason_code,
        "target_url": _redacted_url(target.get("url")),
        "target_host": target.get("host") or "",
        "target_classification": target.get("classification") or "unknown",
        "submitted": False,
        "automation_performed": False,
        "readiness": {
            "ready": bool(readiness.get("ready")),
            "blockers": [b.get("code") for b in readiness.get("blockers", [])],
        },
        "confirmations": confirmations,
        "metadata": metadata or {},
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    if local_demo_store.active():
        try:
            inserted = (
                local_demo_store.db()
                .table(ATTEMPT_TABLE)
                .insert(row)
                .execute()
                .data
                or []
            )
        except Exception as exc:  # noqa: BLE001 - demo audit writes also fail closed
            raise ApplyError(
                500,
                {
                    "error": "Failed to record application attempt.",
                    "code": "attempt_log_failed",
                },
            ) from exc
        return _public_attempt(inserted[0] if inserted else row)

    try:
        inserted = (
            _server_sb()
            .table(ATTEMPT_TABLE)
            .insert({**row, "owner_id": _owner_id()})
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 - fail closed if the audit table cannot record
        raise ApplyError(
            500,
            {"error": "Failed to record application attempt.", "code": "attempt_log_failed"},
        ) from exc
    if not inserted:
        raise ApplyError(
            500,
            {"error": "Failed to record application attempt.", "code": "attempt_log_failed"},
        )
    return _public_attempt(inserted[0])


class _ApplyStateChanged(Exception):
    """The final database-bound apply snapshot no longer matches readiness."""

    def __init__(self, code: str = "apply_state_changed"):
        super().__init__(code)
        self.code = code


def _record_successful_attempt(
    *,
    job_id: str,
    packet_id: str | None,
    action: str,
    status: str,
    reason_code: str,
    target: dict[str, Any],
    readiness: dict[str, Any],
    confirmations: dict[str, bool],
    now_iso: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Atomically bind a successful action to the final current DB snapshot."""
    if local_demo_store.active():
        return (
            _record_attempt(
                job_id=job_id,
                packet_id=packet_id,
                action=action,
                status=status,
                reason_code=reason_code,
                target=target,
                readiness=readiness,
                confirmations=confirmations,
                now_iso=now_iso,
                metadata=metadata,
            ),
            readiness,
        )

    refreshed: dict[str, Any] | None = None
    try:
        if not packet_id or not _is_uuid(packet_id):
            raise _ApplyStateChanged("packet_unavailable")
        try:
            refreshed = application_readiness(job_id, packet_id=packet_id)
        except jobs_svc.JobError as exc:
            if exc.status >= 500:
                raise ApplyError(
                    500,
                    {
                        "error": "Failed to finalize application evidence.",
                        "code": "apply_finalize_failed",
                    },
                ) from exc
            raise _ApplyStateChanged(
                str(exc.payload.get("code") or "apply_state_changed")
            ) from exc
        current_config = external_apply_config()
        current_target = refreshed.get("target") or {}
        current_packet = refreshed.get("packet") or {}
        if not current_config["available"]:
            raise _ApplyStateChanged(
                "external_apply_disabled"
                if not current_config["enabled"]
                else "external_apply_kill_switch"
            )
        if not refreshed.get("ready"):
            blockers = refreshed.get("blockers") or []
            code = blockers[0].get("code") if blockers and isinstance(blockers[0], dict) else None
            raise _ApplyStateChanged(str(code or "apply_state_changed"))
        if current_packet.get("id") != packet_id:
            raise _ApplyStateChanged("packet_unavailable")
        if any(
            current_target.get(key) != target.get(key)
            for key in ("url", "host", "classification", "reason_code")
        ):
            raise _ApplyStateChanged("target_changed")
        if action == "manual_open" and current_target.get("open_allowed") is not True:
            raise _ApplyStateChanged("target_changed")
        if action == "dry_run" and current_target.get("dry_run_allowed") is not True:
            raise _ApplyStateChanged("target_changed")

        try:
            expected = _final_apply_state(
                job_id=job_id,
                packet_id=packet_id,
                target=current_target,
            )
        except (_ApplyStateChanged, ApplyError):
            raise
        except Exception as exc:  # noqa: BLE001 - snapshot reads must fail closed
            raise ApplyError(
                500,
                {
                    "error": "Failed to finalize application evidence.",
                    "code": "apply_finalize_failed",
                },
            ) from exc
        readiness_evidence = {
            "ready": True,
            "blockers": [],
        }
        try:
            response = _server_sb().rpc(
                "finalize_application_apply_action",
                {
                    "p_owner_id": _owner_id(),
                    "p_job_id": job_id,
                    "p_packet_id": packet_id,
                    "p_action": action,
                    "p_status": status,
                    "p_reason_code": reason_code,
                    "p_target_url": current_target.get("url") or "",
                    "p_target_host": current_target.get("host") or "",
                    "p_target_classification": current_target.get("classification") or "invalid",
                    "p_expected": expected,
                    "p_readiness": readiness_evidence,
                    "p_confirmations": confirmations,
                    "p_metadata": metadata or {},
                },
            ).execute().data
        except ApplyError:
            raise
        except Exception as exc:  # noqa: BLE001 - final evidence must fail closed
            raise ApplyError(
                500,
                {
                    "error": "Failed to finalize application evidence.",
                    "code": "apply_finalize_failed",
                },
            ) from exc
        result = response[0] if isinstance(response, list) and response else response
        if not isinstance(result, dict):
            raise ApplyError(
                500,
                {
                    "error": "Application evidence finalization returned invalid evidence.",
                    "code": "attempt_log_failed",
                },
            )
        if result.get("status") != "recorded":
            raise _ApplyStateChanged(str(result.get("code") or "apply_state_changed"))
        inserted = result.get("attempt")
        if not isinstance(inserted, dict) or any(
            (
                inserted.get("job_id") != job_id,
                inserted.get("packet_id") != packet_id,
                inserted.get("action") != action,
                inserted.get("status") != status,
                inserted.get("target_url") != current_target.get("url"),
                inserted.get("target_host") != current_target.get("host"),
                inserted.get("target_classification") != current_target.get("classification"),
                inserted.get("submitted") is not False,
                inserted.get("automation_performed") is not False,
            )
        ):
            raise ApplyError(
                500,
                {
                    "error": "Application evidence finalization mismatch.",
                    "code": "attempt_log_failed",
                },
            )
        return _public_attempt(inserted), refreshed
    except _ApplyStateChanged as exc:
        blocked_readiness = refreshed or readiness
        blocked_target = (
            blocked_readiness.get("target")
            if isinstance(blocked_readiness.get("target"), dict)
            else target
        )
        blocked_packet = (
            blocked_readiness.get("packet")
            if isinstance(blocked_readiness.get("packet"), dict)
            else {}
        )
        blocked_attempt = None
        try:
            blocked_attempt = _record_attempt(
                job_id=job_id,
                packet_id=blocked_packet.get("id") or packet_id,
                action=action,
                status="blocked",
                reason_code=exc.code,
                target=blocked_target,
                readiness=blocked_readiness,
                confirmations=confirmations,
                now_iso=now_iso,
                metadata=metadata,
            )
        except ApplyError:
            pass
        payload: dict[str, Any] = {
            "error": "Application state changed before the action was finalized.",
            "code": "apply_state_changed",
            "reason_code": exc.code,
        }
        if blocked_attempt is not None:
            payload["attempt"] = blocked_attempt
        raise ApplyError(409, payload) from exc


def _final_apply_state(
    *,
    job_id: str,
    packet_id: str,
    target: dict[str, Any],
) -> dict[str, Any]:
    """Capture and validate the exact rows the final RPC will lock and compare."""
    sb = _user_sb()
    jobs = (
        sb.table("saved_jobs")
        .select("id, source_url, source_metadata, jd_text, updated_at")
        .eq("id", job_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    packets = (
        sb.table(PACKET_TABLE)
        .select(
            "id, job_id, source_cv_id, tailored_cv_id, cover_letter_id, status, "
            "review_status, archived_at, prompt_version, packet_version, "
            "source_cv_hash, job_content_hash, tailored_cv_hash, cover_letter_hash, "
            "profile_answers_complete, legal_answers_confirmed, visa_answers_confirmed, "
            "cover_letter_review_status, cover_letter_review_confirmed_at, "
            "cover_letter_review_id, updated_at"
        )
        .eq("id", packet_id)
        .eq("job_id", job_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if len(jobs) != 1 or len(packets) != 1:
        raise _ApplyStateChanged("packet_unavailable")
    job = jobs[0]
    packet = packets[0]
    current_target = classify_apply_url(job.get("source_url"))
    if any(
        current_target.get(key) != target.get(key)
        for key in ("url", "host", "classification", "reason_code")
    ):
        raise _ApplyStateChanged("target_changed")
    metadata = job.get("source_metadata") if isinstance(job.get("source_metadata"), dict) else {}
    if metadata.get("full_jd_status") == "missing" or not str(job.get("jd_text") or "").strip():
        raise _ApplyStateChanged("full_jd_required")
    if packet.get("archived_at") or packet.get("status") != "packet_created":
        raise _ApplyStateChanged("packet_unavailable")
    if (
        packet.get("review_status") != "reviewed"
        or packet.get("prompt_version") != jobs_svc.PACKET_PROMPT_VERSION
        or int(packet.get("packet_version") or 0) < jobs_svc.PACKET_VERSION
        or packet.get("profile_answers_complete") is not True
        or packet.get("legal_answers_confirmed") is not True
        or packet.get("visa_answers_confirmed") is not True
        or packet.get("cover_letter_review_status") != "confirmed"
        or not packet.get("cover_letter_review_confirmed_at")
        or not packet.get("cover_letter_review_id")
    ):
        raise _ApplyStateChanged("packet_unavailable")

    document_ids = [
        str(packet.get("source_cv_id") or ""),
        str(packet.get("tailored_cv_id") or ""),
        str(packet.get("cover_letter_id") or ""),
    ]
    if any(not _is_uuid(doc_id) for doc_id in document_ids):
        raise _ApplyStateChanged("document_changed")
    documents = (
        sb.table("documents")
        .select("id, type, job_id, name, yaml_blob, accent, font, density, updated_at")
        .in_("id", document_ids)
        .is_("deleted_at", "null")
        .execute()
        .data
        or []
    )
    by_id = {str(row.get("id")): row for row in documents}
    source = by_id.get(document_ids[0])
    tailored = by_id.get(document_ids[1])
    letter = by_id.get(document_ids[2])
    if (
        source is None
        or source.get("type") != "cv"
        or tailored is None
        or tailored.get("type") != "cv"
        or tailored.get("job_id") != job_id
        or letter is None
        or letter.get("type") != "cover_letter"
        or letter.get("job_id") != job_id
    ):
        raise _ApplyStateChanged("document_changed")

    job_hash = _content_hash(str(job.get("jd_text") or ""))
    source_hash = _content_hash(str(source.get("yaml_blob") or ""))
    tailored_hash = _content_hash(str(tailored.get("yaml_blob") or ""))
    letter_hash = _content_hash(str(letter.get("yaml_blob") or ""))
    if (
        packet.get("job_content_hash") != job_hash
        or packet.get("source_cv_hash") != source_hash
        or packet.get("tailored_cv_hash") != tailored_hash
        or packet.get("cover_letter_hash") != letter_hash
    ):
        raise _ApplyStateChanged("document_changed")

    tailored_artifact = _fresh_pdf_artifact_evidence(tailored, require_one_page=True)
    letter_artifact = _fresh_pdf_artifact_evidence(letter, require_one_page=False)
    if tailored_artifact is None or letter_artifact is None:
        raise _ApplyStateChanged("artifact_changed")
    revisions = (
        job.get("updated_at"),
        packet.get("updated_at"),
        source.get("updated_at"),
        tailored.get("updated_at"),
        letter.get("updated_at"),
    )
    if any(value is None for value in revisions):
        raise _ApplyStateChanged("document_changed")
    return {
        "job_updated_at": revisions[0],
        "job_source_url": job.get("source_url") or "",
        "job_jd_text": job.get("jd_text") or "",
        "job_content_hash": job_hash,
        "packet_updated_at": revisions[1],
        "prompt_version": packet.get("prompt_version"),
        "packet_version": int(packet.get("packet_version") or 0),
        "source_cv_id": document_ids[0],
        "source_cv_updated_at": revisions[2],
        "source_cv_hash": source_hash,
        "tailored_cv_id": document_ids[1],
        "tailored_cv_updated_at": revisions[3],
        "tailored_cv_hash": tailored_hash,
        "tailored_cv_accent": tailored.get("accent") or "",
        "tailored_cv_font": tailored.get("font") or "",
        "cover_letter_id": document_ids[2],
        "cover_letter_updated_at": revisions[4],
        "cover_letter_hash": letter_hash,
        "target_url": target.get("url") or "",
        "target_host": target.get("host") or "",
        "target_classification": target.get("classification") or "invalid",
        "tailored_artifact_id": tailored_artifact.get("id"),
        "tailored_artifact_r2_key": tailored_artifact.get("r2_key"),
        "tailored_artifact_content_hash": tailored_artifact.get("content_hash"),
        "tailored_artifact_generation_id": tailored_artifact.get("generation_id"),
        "tailored_artifact_page_count": tailored_artifact.get("page_count"),
        "letter_artifact_id": letter_artifact.get("id"),
        "letter_artifact_r2_key": letter_artifact.get("r2_key"),
        "letter_artifact_content_hash": letter_artifact.get("content_hash"),
        "letter_artifact_generation_id": letter_artifact.get("generation_id"),
        "letter_artifact_page_count": letter_artifact.get("page_count"),
    }


def _packet_needs_confirmation_persistence(packet: dict[str, Any], confirmations: dict[str, bool]) -> bool:
    if not packet:
        return False
    for field in (
        "profile_answers_complete",
        "legal_answers_confirmed",
        "visa_answers_confirmed",
    ):
        if packet.get(field) is not True and confirmations.get(field) is True:
            return True
    return False


def _owner_id() -> str:
    owner = getattr(auth_mod.auth_g(), "owner_id", None)
    if not owner:
        raise ApplyError(401, {"error": "unauthorized", "code": "unauthorized"})
    return str(owner)


def _persist_packet_confirmations(packet_id: str, confirmations: dict[str, bool], *, now_iso: str) -> None:
    updates = {
        "profile_answers_complete": confirmations.get("profile_answers_complete") is True,
        "legal_answers_confirmed": confirmations.get("legal_answers_confirmed") is True,
        "visa_answers_confirmed": confirmations.get("visa_answers_confirmed") is True,
        "updated_at": now_iso,
    }
    try:
        rows = (
            _server_sb()
            .table(PACKET_TABLE)
            .update(updates)
            .eq("id", packet_id)
            .eq("owner_id", _owner_id())
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 - fail closed if readiness cannot be persisted
        raise ApplyError(
            500,
            {"error": "Failed to persist application confirmations.", "code": "packet_confirmation_update_failed"},
        ) from exc
    if not rows:
        raise ApplyError(
            404,
            {"error": "Application packet was not found.", "code": "packet_not_found"},
        )


def _public_attempt(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "job_id": row.get("job_id"),
        "packet_id": row.get("packet_id"),
        "action": row.get("action"),
        "status": row.get("status"),
        "reason_code": row.get("reason_code"),
        "target_host": row.get("target_host"),
        "target_classification": row.get("target_classification"),
        "submitted": bool(row.get("submitted")),
        "automation_performed": bool(row.get("automation_performed")),
        "created_at": row.get("created_at"),
    }


def _select_packet(packets: list[dict[str, Any]], packet_id: str | None) -> dict[str, Any] | None:
    if packet_id:
        return next((p for p in packets if p.get("id") == packet_id), None)
    active_packets = [p for p in packets if not _packet_is_archived(p)]
    reviewed = [
        p for p in active_packets
        if p.get("status") == "packet_created"
        and p.get("review_status") == "reviewed"
        and p.get("tailored_cv_id")
        and p.get("cover_letter_id")
    ]
    return reviewed[0] if reviewed else (active_packets[0] if active_packets else None)


def _packet_blockers(packet: dict[str, Any], detail: dict[str, Any]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    job = detail.get("job") if isinstance(detail.get("job"), dict) else {}
    if _packet_is_archived(packet):
        blockers.append(_blocker("packet_archived", "Application packet is archived."))
    if packet.get("review_status") != "reviewed":
        blockers.append(_blocker("packet_not_reviewed", "Application packet has not been reviewed."))
    if packet.get("prompt_version") != jobs_svc.PACKET_PROMPT_VERSION:
        blockers.append(_blocker("packet_prompt_version_stale", "Application packet prompt version is stale."))
    try:
        packet_version = int(packet.get("packet_version") or 0)
    except (TypeError, ValueError):
        packet_version = 0
    if packet_version < jobs_svc.PACKET_VERSION:
        blockers.append(_blocker("packet_version_stale", "Application packet version is stale."))
    if not packet.get("score_system_id"):
        blockers.append(_blocker("score_system_missing", "Application packet score-system lineage is missing."))
    if not packet.get("tailored_cv_id"):
        blockers.append(_blocker("tailored_cv_missing", "Reviewed tailored CV is missing."))
    if not packet.get("cover_letter_id"):
        blockers.append(_blocker("cover_letter_missing", "Reviewed cover letter is missing."))
    if packet.get("profile_answers_complete") is not True:
        blockers.append(_blocker("profile_answers_incomplete", "Required profile answers are incomplete."))
    if packet.get("legal_answers_confirmed") is not True:
        blockers.append(_blocker("legal_answers_unconfirmed", "Legal answers have not been confirmed."))
    if packet.get("visa_answers_confirmed") is not True:
        blockers.append(_blocker("visa_answers_unconfirmed", "Visa/work-authorization answers have not been confirmed."))

    cv_ids = {row.get("id") for row in detail.get("cvs", [])}
    cover_ids = {row.get("id") for row in detail.get("cover_letters", [])}
    tailored_cv_linked = bool(
        packet.get("tailored_cv_id") and packet.get("tailored_cv_id") in cv_ids
    )
    cover_letter_linked = bool(
        packet.get("cover_letter_id") and packet.get("cover_letter_id") in cover_ids
    )
    if packet.get("tailored_cv_id") and not tailored_cv_linked:
        blockers.append(_blocker("tailored_cv_unlinked", "Tailored CV is not linked to this job."))
    if packet.get("cover_letter_id") and not cover_letter_linked:
        blockers.append(_blocker("cover_letter_unlinked", "Cover letter is not linked to this job."))

    source_cv_id = str(packet.get("source_cv_id") or "")
    if not source_cv_id:
        blockers.append(_blocker("source_cv_missing", "Source CV lineage is missing."))
    else:
        _append_document_hash_blocker(
            blockers,
            expected=packet.get("source_cv_hash"),
            actual=_document_yaml_hash(source_cv_id, "cv"),
            stale_code="source_cv_stale",
            missing_document_code="source_cv_missing",
            missing_hash_code="source_cv_hash_missing",
            stale_message="Source CV changed after this packet was created.",
            missing_document_message="Source CV is missing.",
            missing_hash_message="Source CV lineage hash is missing.",
        )

    _append_hash_blocker(
        blockers,
        expected=packet.get("job_content_hash"),
        actual=_content_hash(job.get("jd_text") or ""),
        stale_code="job_description_stale",
        missing_code="job_content_hash_missing",
        stale_message="Job description changed after this packet was created.",
        missing_message="Job description lineage hash is missing.",
    )
    if packet.get("tailored_cv_id"):
        _append_document_hash_blocker(
            blockers,
            expected=packet.get("tailored_cv_hash"),
            actual=_document_yaml_hash(str(packet.get("tailored_cv_id")), "cv"),
            stale_code="tailored_cv_stale",
            missing_document_code="tailored_cv_missing",
            missing_hash_code="tailored_cv_hash_missing",
            stale_message="Tailored CV changed after this packet was created.",
            missing_document_message="Tailored CV is missing.",
            missing_hash_message="Tailored CV lineage hash is missing.",
        )
    if packet.get("cover_letter_id"):
        _append_document_hash_blocker(
            blockers,
            expected=packet.get("cover_letter_hash"),
            actual=_document_yaml_hash(str(packet.get("cover_letter_id")), "cover_letter"),
            stale_code="cover_letter_stale",
            missing_document_code="cover_letter_missing",
            missing_hash_code="cover_letter_hash_missing",
            stale_message="Cover letter changed after this packet was created.",
            missing_document_message="Cover letter is missing.",
            missing_hash_message="Cover letter lineage hash is missing.",
        )
    # Artifact blockers follow content-lineage blockers so an edited document
    # reports the underlying stale revision before the derived PDF consequence.
    if tailored_cv_linked and not _has_fresh_pdf_artifact(
        str(packet.get("tailored_cv_id")), require_one_page=True
    ):
        blockers.append(_blocker("tailored_cv_pdf_missing", "Tailored CV PDF must be generated before apply."))
    if cover_letter_linked and not _has_fresh_pdf_artifact(
        str(packet.get("cover_letter_id")), require_one_page=False
    ):
        blockers.append(_blocker("cover_letter_pdf_missing", "Cover-letter PDF must be generated before apply."))
    if not _packet_is_archived(packet) and packet.get("status") != "packet_created":
        blockers.append(_blocker("packet_incomplete", "Application packet is not complete."))
    if packet.get("cover_letter_id"):
        if packet.get("cover_letter_review_status") != "confirmed":
            blockers.append(_blocker("cover_letter_review_unconfirmed", "Cover-letter grounding review is not confirmed."))
        if not packet.get("cover_letter_review_confirmed_at"):
            blockers.append(_blocker("cover_letter_review_timestamp_missing", "Cover-letter review confirmation timestamp is missing."))
        if not packet.get("cover_letter_review_id"):
            blockers.append(_blocker("cover_letter_review_id_missing", "Cover-letter review confirmation id is missing."))
    return blockers


def _packet_is_archived(packet: dict[str, Any]) -> bool:
    return bool(packet.get("archived_at") or str(packet.get("status") or "").lower() == "archived")


def _public_packet(packet: dict[str, Any] | None) -> dict[str, Any] | None:
    if not packet:
        return None
    return {
        "id": packet.get("id"),
        "job_id": packet.get("job_id"),
        "source_cv_id": packet.get("source_cv_id"),
        "tailored_cv_id": packet.get("tailored_cv_id"),
        "cover_letter_id": packet.get("cover_letter_id"),
        "status": packet.get("status"),
        "review_status": packet.get("review_status"),
        "provider": packet.get("provider"),
        "model": packet.get("model"),
        "prompt_version": packet.get("prompt_version"),
        "score_system_id": packet.get("score_system_id"),
        "score_snapshot": packet.get("score_snapshot") if isinstance(packet.get("score_snapshot"), dict) else {},
        "source_cv_hash": packet.get("source_cv_hash"),
        "job_content_hash": packet.get("job_content_hash"),
        "tailored_cv_hash": packet.get("tailored_cv_hash"),
        "cover_letter_hash": packet.get("cover_letter_hash"),
        "cover_letter_review_status": packet.get("cover_letter_review_status"),
        "cover_letter_review_confirmed_at": packet.get("cover_letter_review_confirmed_at"),
        "cover_letter_review_id": packet.get("cover_letter_review_id"),
        "packet_version": packet.get("packet_version"),
        "profile_answers_complete": packet.get("profile_answers_complete") is True,
        "legal_answers_confirmed": packet.get("legal_answers_confirmed") is True,
        "visa_answers_confirmed": packet.get("visa_answers_confirmed") is True,
        "document_links": packet.get("document_links") if isinstance(packet.get("document_links"), dict) else {},
        "updated_at": packet.get("updated_at"),
    }


def _confirmation_state(payload: dict[str, Any]) -> dict[str, bool]:
    nested = payload.get("confirmations") if isinstance(payload.get("confirmations"), dict) else {}
    return {
        "user_confirmed": payload.get("confirm") is True
        or payload.get("user_confirmed") is True
        or nested.get("user_confirmed") is True,
        "reviewed_packet_confirmed": payload.get("reviewed_packet_confirmed") is True
        or nested.get("reviewed_packet_confirmed") is True,
        "profile_answers_complete": payload.get("profile_answers_complete") is True
        or nested.get("profile_answers_complete") is True,
        "legal_answers_confirmed": payload.get("legal_answers_confirmed") is True
        or nested.get("legal_answers_confirmed") is True,
        "visa_answers_confirmed": payload.get("visa_answers_confirmed") is True
        or payload.get("work_authorization_confirmed") is True
        or nested.get("visa_answers_confirmed") is True
        or nested.get("work_authorization_confirmed") is True,
    }


def _fixture_field_report(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    fields: list[dict[str, Any]] = []
    for item in value[:50]:
        if not isinstance(item, dict):
            continue
        name = _clean_short(item.get("name"))
        if not name:
            continue
        fields.append(
            {
                "name": name,
                "label": _clean_short(item.get("label")),
                "type": _clean_short(item.get("type")) or "text",
                "required": item.get("required") is True,
            }
        )
    return fields


def _append_hash_blocker(
    blockers: list[dict[str, str]],
    *,
    expected: object,
    actual: str | None,
    stale_code: str,
    missing_code: str,
    stale_message: str,
    missing_message: str,
) -> None:
    expected_hash = expected if isinstance(expected, str) and expected else None
    if not expected_hash or not actual:
        blockers.append(_blocker(missing_code, missing_message))
    elif expected_hash != actual:
        blockers.append(_blocker(stale_code, stale_message))


def _append_document_hash_blocker(
    blockers: list[dict[str, str]],
    *,
    expected: object,
    actual: str | None,
    stale_code: str,
    missing_document_code: str,
    missing_hash_code: str,
    stale_message: str,
    missing_document_message: str,
    missing_hash_message: str,
) -> None:
    expected_hash = expected if isinstance(expected, str) and expected else None
    if not actual:
        blockers.append(_blocker(missing_document_code, missing_document_message))
    elif not expected_hash:
        blockers.append(_blocker(missing_hash_code, missing_hash_message))
    elif expected_hash != actual:
        blockers.append(_blocker(stale_code, stale_message))


def _document_yaml_hash(doc_id: str, expected_type: str) -> str | None:
    if not _is_uuid(doc_id):
        return None
    rows = (
        _user_sb()
        .table("documents")
        .select("id, type, yaml_blob")
        .eq("id", doc_id)
        .is_("deleted_at", "null")
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows or rows[0].get("type") != expected_type:
        return None
    return _content_hash(rows[0].get("yaml_blob") or "")


def _has_fresh_pdf_artifact(doc_id: str, *, require_one_page: bool) -> bool:
    if not _is_uuid(doc_id):
        return False
    expected_type = "cv" if require_one_page else "cover_letter"
    document_rows = (
        _user_sb()
        .table("documents")
        .select("id, type, name, yaml_blob, accent, font, density, job_id")
        .eq("id", doc_id)
        .is_("deleted_at", "null")
        .limit(1)
        .execute()
        .data
        or []
    )
    if not document_rows or document_rows[0].get("type") != expected_type:
        return False
    return _fresh_pdf_artifact_evidence(
        document_rows[0],
        require_one_page=require_one_page,
    ) is not None


def _fresh_pdf_artifact_evidence(
    document: dict[str, Any],
    *,
    require_one_page: bool,
) -> dict[str, Any] | None:
    """Return the sole canonical, live R2 artifact for an exact document."""
    if local_demo_store.active():
        # The ephemeral demo never owns authoritative remote artifacts. Do not
        # query R2 or turn process-local rows into application-readiness proof.
        return None
    doc_id = document.get("id")
    expected_type = "cv" if require_one_page else "cover_letter"
    if not _is_uuid(doc_id) or document.get("type") != expected_type:
        return None
    try:
        if expected_type == "cv":
            expected_hash = pdf_store.persisted_cv_content_hash(
                document.get("yaml_blob") or "",
                document.get("accent") or "",
                document.get("font") or "",
            )
        else:
            blob = cl_service.normalized_blob_for_row(document)
            sender_name = str(blob.get("sender_name") or "").strip()
            if not sender_name:
                sender_name = _cover_letter_sender_name(blob.get("source_cv_id"))
            cache_key_text = cl_service.serialize_blob(blob) + "\x00" + sender_name
            expected_hash = pdf_store.cover_letter_content_hash(cache_key_text)
    # `normalized_blob_for_row` parses stored YAML leniently, but `serialize_blob`
    # re-runs the strict envelope validation, so a letter saved before those caps
    # existed (e.g. a body carrying U+200B/U+00AD) raises CoverLetterError here.
    # Readiness must degrade to "no fresh artifact" and report a blocker; letting
    # this escape would turn GET .../apply/readiness into a 400 and take the whole
    # Application Studio panel down for that job.
    except (pdf_store.StorageError, TypeError, ValueError, cl_service.CoverLetterError):
        return None

    rows = (
        _user_sb()
        .table("pdf_artifacts")
        .select(
            "id, document_id, r2_key, content_hash, generation_id, stale, page_count"
        )
        .eq("document_id", doc_id)
        .eq("stale", False)
        .limit(2)
        .execute()
        .data
        or []
    )
    # A normal build deletes superseded artifacts before inserting the current
    # one. Multiple rows marked fresh indicate bypassed or corrupt metadata;
    # rejecting the set avoids arbitrary first-row selection.
    if len(rows) != 1:
        return None
    row = rows[0]
    try:
        expected_key = pdf_store.verified_artifact_key(
            doc_id,
            expected_hash,
            row.get("generation_id"),
        )
    except (pdf_store.StorageError, TypeError, ValueError):
        return None
    if row.get("content_hash") != expected_hash or row.get("r2_key") != expected_key:
        return None
    try:
        page_count = int(row.get("page_count") or 0)
    except (TypeError, ValueError):
        return None
    if page_count < 1 or (require_one_page and page_count != 1):
        return None
    if not pdf_store.r2_object_exists(expected_key):
        return None
    return row


def _cover_letter_sender_name(source_cv_id: object) -> str:
    """Mirror the cover-letter build's current source-CV signer input."""
    if not isinstance(source_cv_id, str) or not _is_uuid(source_cv_id):
        return ""
    try:
        row = cl_service.document_by_id(source_cv_id, "cv")
    except Exception as exc:  # noqa: BLE001 - readiness must not guess on lookup failure
        raise pdf_store.StorageError("cover-letter signer lookup failed") from exc
    if row is None:
        return ""
    try:
        cv = yaml.safe_load(row.get("yaml_blob") or "")
    except Exception as exc:  # noqa: BLE001 - readiness must not guess on invalid input
        raise pdf_store.StorageError("cover-letter signer content is invalid") from exc
    if isinstance(cv, dict):
        return str(cv.get("name") or "").strip()
    return ""


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _normalize_apply_url(value: object) -> tuple[str, str, bool]:
    if not isinstance(value, str):
        return "", "", False
    raw = value.strip()
    if not raw:
        return "", "", False
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        # Merely accessing .port raises ValueError for an unparseable or
        # out-of-range port (":badport", ":99999") → invalid URL.
        parsed.port
    except ValueError:
        return raw, host, False
    valid = (
        parsed.scheme.lower() in {"http", "https"}
        and bool(host)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and not _redacted_url_path_sensitive(parsed.path or "")
    )
    return raw, host, valid


def _apply_host_is_disallowed(host: str) -> bool:
    """Reject local/private/metadata hosts before offering manual-open actions."""
    normalized = (host or "").lower().rstrip(".")
    if not normalized:
        return True
    if normalized in _DISALLOWED_APPLY_HOSTS or normalized.endswith(".localhost"):
        return True

    def _parse_ip(addr: str):
        try:
            return ipaddress.ip_address(addr)
        except ValueError:
            if not _NUMERIC_IPV4_HOST_RE.fullmatch(addr):
                return None
            try:
                return ipaddress.ip_address(socket.inet_aton(addr))
            except OSError:
                return None

    def _bad_ip(addr: str) -> bool:
        ip = _parse_ip(addr)
        if ip is None:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_WELL_KNOWN:
            embedded = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
            return not ssrf._ip_is_public(embedded)
        return not ssrf._ip_is_public(ip)  # shared SSRF address contract

    if _bad_ip(normalized):
        return True

    return False


def _redacted_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if scheme not in {"http", "https"} or not host:
        return ""
    netloc = host
    try:
        port = parsed.port
    except ValueError:
        # Unparseable/out-of-range port (":badport", ":99999") → treat the
        # URL as invalid rather than crashing the classifier.
        return ""
    if port:
        netloc = f"{host}:{port}"
    path = "" if _redacted_url_path_sensitive(parsed.path or "") else (parsed.path or "")
    return urlunparse((scheme, netloc, path, "", "", ""))


def _redacted_url_path_sensitive(value: str) -> bool:
    sensitive_segments = {
        "access_token",
        "api_key",
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "csrf",
        "id_token",
        "jsessionid",
        "key",
        "li_at",
        "password",
        "refresh_token",
        "secret",
        "session",
        "session_token",
        "token",
    }
    for raw_segment in str(value or "").split("/"):
        segment = unquote(raw_segment).strip().lower()
        if not segment:
            continue
        if segment in sensitive_segments:
            return True
        if re.search(
            r"\b(?:li_at|jsessionid|session[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|secret[_-]?key|authorization|cookie|password|secret|token)\s*[:=]|\bbearer\s+[A-Za-z0-9._~+/=-]+|"
            r"\bsk-(?:ant|proj|live|test|or|gsk|mistral|[A-Za-z0-9])[A-Za-z0-9_-]{8,}\b|\bAIza[0-9A-Za-z_-]{20,}\b|\bya29\.[0-9A-Za-z_-]{20,}\b",
            segment,
            re.IGNORECASE,
        ):
            return True
    return False


def _enforce_apply_action_rate_limit(action: str, job_id: str) -> None:
    limit = max(1, APPLY_ACTION_RATE_LIMIT_PER_MINUTE)
    now = time.time()
    window_start = now - 60
    bucket_key = f"{action}:{job_id}"
    bucket = [ts for ts in _APPLY_ACTION_BUCKETS.get(bucket_key, []) if ts >= window_start]
    if bucket:
        _APPLY_ACTION_BUCKETS[bucket_key] = bucket
    else:
        # Drop empty buckets so the dict can't grow unboundedly with one
        # entry per (action, job_id) pair ever seen.
        _APPLY_ACTION_BUCKETS.pop(bucket_key, None)
    if len(bucket) >= limit:
        _APPLY_ACTION_BUCKETS[bucket_key] = bucket
        raise ApplyError(
            429,
            {
                "error": "Too many external apply actions. Retry later.",
                "code": "external_apply_rate_limited",
            },
        )
    bucket.append(now)
    _APPLY_ACTION_BUCKETS[bucket_key] = bucket


def _safe_fixture_hosts() -> set[str]:
    hosts: set[str] = set()
    for item in (os.environ.get(SAFE_FIXTURE_HOSTS_ENV) or "").split(","):
        host = item.strip().lower().rstrip(".")
        if host and "*" not in host and "/" not in host and ":" not in host:
            hosts.add(host)
    return hosts


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUE_VALUES


def _env_falsey_value(value: str) -> bool:
    return value.strip().lower() in _FALSE_VALUES


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _clean_short(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())[:120]


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
