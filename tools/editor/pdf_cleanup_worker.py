"""Service-role worker for the durable PDF artifact cleanup outbox.

The database transaction that deletes a document first records every canonical
R2 object identity in ``pdf_artifact_cleanup_queue``. This worker consumes a
bounded batch of those rows. Each row is isolated from every other row:

1. validate the complete content-addressed identity;
2. delete the exact R2 object;
3. delete the queue row only after the object deletion returns successfully.

Failures increment the row's attempt counter and do not stop the remainder of
the batch. Logs contain aggregate counts and exception class names only. They
never contain tenant IDs, document IDs, object keys, credentials, or exception
messages.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
import re
import signal
import threading
import uuid
from typing import Any, Mapping, Sequence

from r2_security import validate_r2_account_id


_LOG = logging.getLogger("pdf_cleanup_worker")
_QUEUE_TABLE = "pdf_artifact_cleanup_queue"
_R2_KEYS = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
)
_HEALTHCHECK_R2_PREFIX = "_onepager_pdf_cleanup_healthcheck_/"
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


class CleanupConfigurationError(RuntimeError):
    """Raised when the worker cannot start with a fail-closed configuration."""


class CleanupItemError(RuntimeError):
    """Raised when a queue row is not a canonical artifact identity."""


class CleanupActiveArtifactError(RuntimeError):
    """Raised when a queued key is again referenced by fresh metadata."""


class CleanupExternalError(RuntimeError):
    """Raised when R2 deletion or probing does not complete."""


class CleanupDatabaseError(RuntimeError):
    """Raised when queue acknowledgement or failure accounting is uncertain."""


@dataclass(frozen=True)
class CleanupItem:
    cleanup_id: str
    owner_id: str
    document_id: str
    content_hash: str
    generation_id: str | None
    r2_key: str
    attempt_count: int


@dataclass(frozen=True)
class CleanupCycle:
    selected: int
    completed: int
    deferred: int
    r2_objects: int
    failure_updates: int
    failure_update_errors: int
    error_types: dict[str, int]


def _bounded_int(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = (environ.get(name) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise CleanupConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise CleanupConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _canonical_uuid(raw: Any, *, field: str) -> str:
    if not isinstance(raw, str):
        raise CleanupItemError(f"invalid {field}")
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError) as exc:
        raise CleanupItemError(f"invalid {field}") from exc
    canonical = str(parsed)
    if raw != canonical:
        raise CleanupItemError(f"non-canonical {field}")
    return canonical


def _canonical_hash(raw: Any) -> str:
    if not isinstance(raw, str) or not _HASH_RE.fullmatch(raw):
        raise CleanupItemError("invalid content hash")
    return raw


def _attempt_count(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise CleanupItemError("invalid attempt count")
    return raw


def _cleanup_item(row: Any) -> CleanupItem:
    if not isinstance(row, dict):
        raise CleanupItemError("cleanup row must be an object")
    cleanup_id = _canonical_uuid(row.get("id"), field="cleanup id")
    owner_id = _canonical_uuid(row.get("owner_id"), field="owner id")
    document_id = _canonical_uuid(row.get("document_id"), field="document id")
    content_hash = _canonical_hash(row.get("content_hash"))
    generation_raw = row.get("generation_id")
    generation_id = (
        None
        if generation_raw is None
        else _canonical_uuid(generation_raw, field="generation id")
    )
    if generation_id is None:
        expected_key = f"{owner_id}/{document_id}/{content_hash}.pdf"
    else:
        expected_key = (
            f"{owner_id}/{document_id}/{content_hash}/{generation_id}.pdf"
        )
    key = row.get("r2_key")
    if not isinstance(key, str) or key != expected_key or len(key) > 500:
        raise CleanupItemError("non-canonical R2 key")
    return CleanupItem(
        cleanup_id=cleanup_id,
        owner_id=owner_id,
        document_id=document_id,
        content_hash=content_hash,
        generation_id=generation_id,
        r2_key=key,
        attempt_count=_attempt_count(row.get("attempt_count")),
    )


def _r2_config(environ: Mapping[str, str]) -> dict[str, str]:
    values = {key: (environ.get(key) or "").strip() for key in _R2_KEYS}
    present = [key for key, value in values.items() if value]
    if len(present) != len(_R2_KEYS):
        raise CleanupConfigurationError(
            "R2 configuration must provide all four R2 settings"
        )
    try:
        values["R2_ACCOUNT_ID"] = validate_r2_account_id(
            values["R2_ACCOUNT_ID"]
        )
    except ValueError as exc:
        raise CleanupConfigurationError(str(exc)) from exc
    return values


class R2ArtifactStore:
    """Minimal single-object R2 client used by the cleanup outbox."""

    def __init__(
        self,
        config: Mapping[str, str],
        *,
        client: Any | None = None,
    ):
        try:
            account_id = validate_r2_account_id(config["R2_ACCOUNT_ID"])
            self._config = {
                key: str(config[key]).strip()
                for key in _R2_KEYS
            }
        except (KeyError, ValueError) as exc:
            raise CleanupConfigurationError("invalid R2 configuration") from exc
        if not all(self._config.values()):
            raise CleanupConfigurationError("incomplete R2 configuration")
        self._config["R2_ACCOUNT_ID"] = account_id
        self._client_override = client
        self._client_instance: Any | None = None

    def _client(self) -> Any:
        if self._client_override is not None:
            return self._client_override
        if self._client_instance is None:
            import boto3
            from botocore.config import Config

            endpoint = (
                f"https://{self._config['R2_ACCOUNT_ID']}"
                ".r2.cloudflarestorage.com"
            )
            self._client_instance = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=self._config["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=self._config["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
                config=Config(signature_version="s3v4"),
            )
        return self._client_instance

    def probe(self) -> None:
        """Prove exact ``DeleteObject`` authority on a reserved absent key.

        Listing a bucket does not prove that the worker can perform its only
        required R2 mutation.  A fresh UUID under the worker-owned health
        prefix cannot collide with an application artifact (whose canonical
        key always starts with an owner UUID), and S3 ``DeleteObject`` is
        idempotent when that key does not exist.
        """
        probe_key = f"{_HEALTHCHECK_R2_PREFIX}{uuid.uuid4()}.pdf"
        try:
            self._client().delete_object(
                Bucket=self._config["R2_BUCKET"],
                Key=probe_key,
            )
        except Exception as exc:  # noqa: BLE001 - redact provider details
            raise CleanupExternalError("R2 readiness probe failed") from exc

    def delete_object(self, key: str) -> None:
        """Delete one already-validated key with S3's idempotent operation."""
        try:
            self._client().delete_object(
                Bucket=self._config["R2_BUCKET"],
                Key=key,
            )
        except Exception as exc:  # noqa: BLE001 - redact provider details
            raise CleanupExternalError("R2 object deletion failed") from exc


class PdfCleanupWorker:
    def __init__(
        self,
        *,
        supabase: Any,
        r2_store: R2ArtifactStore,
        batch_size: int = 20,
    ):
        if not 1 <= batch_size <= 100:
            raise CleanupConfigurationError(
                "batch size must be between 1 and 100"
            )
        self._supabase = supabase
        self._r2_store = r2_store
        self._batch_size = batch_size

    def probe(self) -> None:
        """Verify exact R2 delete and claim-RPC authority without tenant work."""
        self._r2_store.probe()
        probe_owner_id = str(uuid.uuid4())
        probe_cleanup_id = str(uuid.uuid4())
        probe_claim_token = str(uuid.uuid4())
        try:
            response = self._supabase.rpc(
                "claim_pdf_artifact_cleanup",
                {
                    "p_owner_id": probe_owner_id,
                    "p_cleanup_id": probe_cleanup_id,
                    "p_claim_token": probe_claim_token,
                    "p_force": False,
                },
            ).execute().data
        except Exception as exc:  # noqa: BLE001 - normalize transport details
            raise CleanupDatabaseError("cleanup claim RPC probe failed") from exc
        result = response[0] if isinstance(response, list) and response else response
        if not isinstance(result, dict) or result.get("status") != "missing":
            raise CleanupDatabaseError("cleanup claim RPC probe was not a safe miss")

    def _batch(self) -> list[dict[str, Any]]:
        try:
            response = (
                self._supabase.table(_QUEUE_TABLE)
                .select(
                    "id, owner_id, document_id, r2_key, content_hash, "
                    "generation_id, attempt_count, not_before"
                )
                .lte("not_before", datetime.now(timezone.utc).isoformat())
                .order("last_attempt_at", desc=False, nullsfirst=True)
                .order("created_at", desc=False)
                .order("id", desc=False)
                .limit(self._batch_size)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - normalize transport details
            raise CleanupDatabaseError("cleanup queue lookup failed") from exc
        rows = response.data or []
        if not isinstance(rows, list):
            raise CleanupDatabaseError("cleanup queue response must be a list")
        return rows

    def _remaining(self, cleanup_id: str) -> list[dict[str, Any]]:
        response = (
            self._supabase.table(_QUEUE_TABLE)
            .select("id, attempt_count")
            .eq("id", cleanup_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        if not isinstance(rows, list):
            raise CleanupDatabaseError("cleanup row response must be a list")
        return rows

    def _claim(
        self,
        row: Any,
    ) -> tuple[str, CleanupItem | None, str | None, bool]:
        """Acquire the DB cleanup lease before any external deletion."""
        if not isinstance(row, dict):
            raise CleanupItemError("cleanup row must be an object")
        cleanup_id = _canonical_uuid(row.get("id"), field="cleanup id")
        owner_id = _canonical_uuid(row.get("owner_id"), field="owner id")
        claim_token = str(uuid.uuid4())
        try:
            response = self._supabase.rpc(
                "claim_pdf_artifact_cleanup",
                {
                    "p_owner_id": owner_id,
                    "p_cleanup_id": cleanup_id,
                    "p_claim_token": claim_token,
                    "p_force": False,
                },
            ).execute().data
        except Exception as exc:  # noqa: BLE001 - normalize transport details
            raise CleanupDatabaseError("cleanup claim failed") from exc
        result = response[0] if isinstance(response, list) and response else response
        if not isinstance(result, dict):
            raise CleanupDatabaseError("cleanup claim response is invalid")
        status = result.get("status")
        if status in {"active", "busy", "missing", "pending"}:
            return status, None, None, False
        if status == "blocked":
            return status, None, None, result.get("failure_recorded") is True
        if status != "claimed":
            raise CleanupDatabaseError("cleanup claim was not authoritative")
        if (
            result.get("cleanup_id") != cleanup_id
            or result.get("owner_id") != owner_id
            or result.get("claim_token") != claim_token
        ):
            raise CleanupDatabaseError("cleanup claim identity mismatch")
        claimed_row = {
            "id": result.get("cleanup_id"),
            "owner_id": result.get("owner_id"),
            "document_id": result.get("document_id"),
            "content_hash": result.get("content_hash"),
            "generation_id": result.get("generation_id"),
            "r2_key": result.get("r2_key"),
            "attempt_count": result.get("attempt_count"),
        }
        return status, _cleanup_item(claimed_row), claim_token, False

    def _acknowledge(self, item: CleanupItem, claim_token: str) -> None:
        """Delete only the unchanged queue identity after R2 deletion."""
        try:
            completed = self._supabase.rpc(
                "complete_pdf_artifact_cleanup",
                {
                    "p_owner_id": item.owner_id,
                    "p_cleanup_id": item.cleanup_id,
                    "p_claim_token": claim_token,
                },
            ).execute().data
            if completed is True:
                return
            if completed not in (False, None):
                raise CleanupDatabaseError(
                    "cleanup acknowledgement response is invalid"
                )
            if self._remaining(item.cleanup_id):
                raise CleanupDatabaseError(
                    "cleanup acknowledgement did not remove the queue row"
                )
        except CleanupDatabaseError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize transport details
            raise CleanupDatabaseError("cleanup acknowledgement failed") from exc

    def _record_failure(self, item: CleanupItem, claim_token: str) -> bool:
        """Atomically increment one failure counter through the service RPC."""
        try:
            updated = self._supabase.rpc(
                "record_pdf_artifact_cleanup_failure",
                {
                    "p_owner_id": item.owner_id,
                    "p_cleanup_id": item.cleanup_id,
                    "p_claim_token": claim_token,
                },
            ).execute().data
            if updated is True:
                return True
            if updated not in (False, None):
                raise CleanupDatabaseError(
                    "cleanup failure response is invalid"
                )
            if not self._remaining(item.cleanup_id):
                return False
        except CleanupDatabaseError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize transport details
            raise CleanupDatabaseError(
                "cleanup failure accounting failed"
            ) from exc
        raise CleanupDatabaseError("cleanup failure accounting did not update")

    def run_once(self) -> CleanupCycle:
        rows = self._batch()
        completed = 0
        deferred = 0
        r2_objects = 0
        failure_updates = 0
        failure_update_errors = 0
        errors: Counter[str] = Counter()

        for row in rows:
            item: CleanupItem | None = None
            claim_token: str | None = None
            try:
                status, item, claim_token, claim_recorded_failure = self._claim(row)
                if claim_recorded_failure:
                    failure_updates += 1
                if status in {"active", "missing"}:
                    completed += 1
                    continue
                if status in {"busy", "pending"}:
                    deferred += 1
                    continue
                if status == "blocked":
                    raise CleanupItemError("cleanup claim rejected a non-canonical row")
                if item is None or claim_token is None:
                    raise CleanupDatabaseError("cleanup claim omitted its identity")
                self._r2_store.delete_object(item.r2_key)
                r2_objects += 1
                self._acknowledge(item, claim_token)
                completed += 1
            except Exception as exc:  # noqa: BLE001 - isolate every queue row
                deferred += 1
                errors[type(exc).__name__] += 1
                if item is not None and claim_token is not None:
                    try:
                        if self._record_failure(item, claim_token):
                            failure_updates += 1
                    except Exception as failure_exc:  # noqa: BLE001 - keep batch moving
                        failure_update_errors += 1
                        errors[type(failure_exc).__name__] += 1

        return CleanupCycle(
            selected=len(rows),
            completed=completed,
            deferred=deferred,
            r2_objects=r2_objects,
            failure_updates=failure_updates,
            failure_update_errors=failure_update_errors,
            error_types=dict(sorted(errors.items())),
        )


class NeverUsedPdfCleanupWorker:
    """Fail-closed no-op for deployments that have never stored R2 PDFs."""

    def __init__(self, *, supabase: Any):
        self._supabase = supabase

    def probe(self) -> None:
        """Prove that the no-R2 assertion still matches durable DB state."""
        try:
            for table in ("pdf_artifacts", _QUEUE_TABLE):
                response = (
                    self._supabase.table(table)
                    .select("id")
                    .limit(1)
                    .execute()
                )
                rows = response.data or []
                if not isinstance(rows, list):
                    raise CleanupDatabaseError(
                        "artifact evidence response must be a list"
                    )
                if rows:
                    raise CleanupConfigurationError(
                        "R2 never-used assertion conflicts with artifact evidence"
                    )
        except (CleanupConfigurationError, CleanupDatabaseError):
            raise
        except Exception as exc:  # noqa: BLE001 - normalize transport details
            raise CleanupDatabaseError(
                "R2 never-used artifact evidence probe failed"
            ) from exc

    def run_once(self) -> CleanupCycle:
        self.probe()
        return CleanupCycle(0, 0, 0, 0, 0, 0, {})


def _build_worker(
    environ: Mapping[str, str] | None = None,
) -> tuple[PdfCleanupWorker | NeverUsedPdfCleanupWorker, int]:
    env = dict(os.environ if environ is None else environ)
    supabase_url = (env.get("SUPABASE_URL") or "").strip()
    service_key = (env.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not supabase_url or not service_key:
        raise CleanupConfigurationError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required"
        )

    batch_size = _bounded_int(
        env,
        "ONEPAGER_PDF_CLEANUP_BATCH_SIZE",
        default=20,
        minimum=1,
        maximum=100,
    )
    poll_seconds = _bounded_int(
        env,
        "ONEPAGER_PDF_CLEANUP_POLL_SECONDS",
        default=60,
        minimum=30,
        maximum=86400,
    )

    from supabase import create_client

    client = create_client(supabase_url, service_key)
    never_used_asserted = (
        (env.get("ONEPAGER_R2_NEVER_USED") or "").strip().lower()
        in {"1", "true", "yes"}
    )
    r2_present = any((env.get(key) or "").strip() for key in _R2_KEYS)
    if never_used_asserted:
        if r2_present:
            raise CleanupConfigurationError(
                "R2 credentials conflict with the never-used assertion"
            )
        worker: PdfCleanupWorker | NeverUsedPdfCleanupWorker = (
            NeverUsedPdfCleanupWorker(supabase=client)
        )
    else:
        worker = PdfCleanupWorker(
            supabase=client,
            r2_store=R2ArtifactStore(_r2_config(env)),
            batch_size=batch_size,
        )
    worker.probe()
    return worker, poll_seconds


def _error_summary(error_types: Mapping[str, int]) -> str:
    if not error_types:
        return "none"
    return ",".join(f"{name}:{count}" for name, count in error_types.items())


def _log_cycle(cycle: CleanupCycle) -> None:
    _LOG.info(
        "pdf_cleanup_cycle selected=%d completed=%d deferred=%d "
        "r2_objects=%d failure_updates=%d failure_update_errors=%d "
        "error_types=%s",
        cycle.selected,
        cycle.completed,
        cycle.deferred,
        cycle.r2_objects,
        cycle.failure_updates,
        cycle.failure_update_errors,
        _error_summary(cycle.error_types),
    )


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = list(os.sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=(
            (os.environ if environ is None else environ).get(
                "ONEPAGER_LOG_LEVEL", "INFO"
            )
        ).upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args not in ([], ["--probe"]):
        _LOG.critical("pdf_cleanup_startup_failed error_type=ArgumentError")
        return 2
    try:
        worker, poll_seconds = _build_worker(environ)
    except Exception as exc:  # noqa: BLE001 - log only the exception class
        _LOG.critical(
            "pdf_cleanup_startup_failed error_type=%s",
            type(exc).__name__,
        )
        return 2

    if args == ["--probe"]:
        return 0

    stop = threading.Event()

    def _stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stop.is_set():
        try:
            _log_cycle(worker.run_once())
        except Exception as exc:  # noqa: BLE001 - preserve recurring service
            _LOG.error(
                "pdf_cleanup_cycle_failed error_type=%s",
                type(exc).__name__,
            )
        stop.wait(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
