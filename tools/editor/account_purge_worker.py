"""Service-role worker for complete D+30 account erasure.

The database claim is acquired before this process touches external state.
For each claimed account, cleanup is deliberately ordered:

1. delete every Cloudflare R2 object below the exact ``{owner_uuid}/`` prefix;
2. delete the exact ``design/photos/{owner_uuid}`` persistent-volume directory;
3. revalidate the claim lease;
4. hard-delete the Supabase Auth user so database foreign-key cascades run.

Any external cleanup failure stops that account before step 4. The database
claim lease later expires and another cycle retries the idempotent cleanup.
Logs contain cycle-level counts and exception class names only: never owner
identifiers, claim tokens, object keys, credentials, or exception messages.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import signal
import threading
import uuid
from typing import Any

from r2_security import validate_r2_account_id


_LOG = logging.getLogger("account_purge_worker")
_REPO = Path(__file__).resolve().parents[2]
_R2_KEYS = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
)


class PurgeConfigurationError(RuntimeError):
    """Raised when the worker cannot safely start."""


class PurgeExternalError(RuntimeError):
    """Raised when external deletion is incomplete or unverifiable."""


class PurgeClaimError(RuntimeError):
    """Raised when a database claim is malformed, expired, or superseded."""


@dataclass(frozen=True)
class PurgeClaim:
    owner_id: str
    claim_token: str


@dataclass(frozen=True)
class PurgeCycle:
    claimed: int
    purged: int
    deferred: int
    r2_objects: int
    photo_directories: int
    error_types: dict[str, int]


def _bounded_int(
    environ: dict[str, str],
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
        raise PurgeConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise PurgeConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _canonical_uuid(raw: Any, *, field: str) -> str:
    try:
        parsed = uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise PurgeClaimError(f"invalid {field}") from exc
    canonical = str(parsed)
    if str(raw) != canonical:
        raise PurgeClaimError(f"non-canonical {field}")
    return canonical


def _r2_config(environ: dict[str, str]) -> dict[str, str] | None:
    values = {key: (environ.get(key) or "").strip() for key in _R2_KEYS}
    present = [key for key, value in values.items() if value]
    if present and len(present) != len(_R2_KEYS):
        raise PurgeConfigurationError(
            "R2 configuration must provide all four R2 settings or none"
        )
    if present:
        try:
            values["R2_ACCOUNT_ID"] = validate_r2_account_id(
                values["R2_ACCOUNT_ID"]
            )
        except ValueError as exc:
            raise PurgeConfigurationError(str(exc)) from exc
    return values if present else None


class R2OwnerStore:
    """Minimal R2 deletion client with an owner-prefix boundary."""

    def __init__(
        self,
        config: dict[str, str] | None,
        *,
        never_used_asserted: bool = False,
        client: Any | None = None,
    ):
        if config is not None:
            try:
                account_id = validate_r2_account_id(config["R2_ACCOUNT_ID"])
            except (KeyError, ValueError) as exc:
                raise PurgeConfigurationError(
                    "R2_ACCOUNT_ID must be exactly 32 hexadecimal characters"
                ) from exc
            config = dict(config)
            config["R2_ACCOUNT_ID"] = account_id
        self._config = config
        self._never_used_asserted = never_used_asserted
        self._client_override = client
        self._client_instance: Any | None = None
        if self.enabled and never_used_asserted:
            raise PurgeConfigurationError(
                "R2 credentials conflict with the never-used assertion"
            )
        if not self.enabled and not never_used_asserted:
            raise PurgeConfigurationError(
                "R2 credentials or ONEPAGER_R2_NEVER_USED=1 are required"
            )

    @property
    def enabled(self) -> bool:
        return self._config is not None

    def _client(self) -> Any:
        if not self._config:
            raise PurgeConfigurationError("R2 is not configured")
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
        """Authenticate a non-mutating bucket listing when R2 is configured."""
        if not self.enabled:
            return
        try:
            self._client().list_objects_v2(
                Bucket=self._config["R2_BUCKET"],
                Prefix="_onepager_account_purge_healthcheck_/",
                MaxKeys=1,
            )
        except Exception as exc:  # noqa: BLE001
            raise PurgeExternalError("R2 readiness probe failed") from exc

    def delete_owner_objects(self, owner_id: str) -> int:
        """Delete all objects below the exact canonical owner UUID prefix."""
        owner = _canonical_uuid(owner_id, field="owner id")
        if not self.enabled:
            return 0

        prefix = f"{owner}/"
        bucket = self._config["R2_BUCKET"]
        client = self._client()
        deleted = 0
        previous_page: tuple[str, ...] | None = None

        # Re-list from the start after each delete. R2 provides strongly
        # consistent object listing, and this avoids continuation-token gaps
        # caused by mutating the key set during pagination.
        while True:
            try:
                page = client.list_objects_v2(
                    Bucket=bucket,
                    Prefix=prefix,
                    MaxKeys=1000,
                )
            except Exception as exc:  # noqa: BLE001
                raise PurgeExternalError("R2 listing failed") from exc

            contents = page.get("Contents") or []
            if not contents:
                return deleted

            keys: list[str] = []
            for item in contents:
                key = item.get("Key") if isinstance(item, dict) else None
                if not isinstance(key, str) or not key.startswith(prefix):
                    raise PurgeExternalError("R2 returned an out-of-prefix key")
                keys.append(key)
            if not keys:
                raise PurgeExternalError("R2 listing did not make progress")
            current_page = tuple(keys)
            if current_page == previous_page:
                raise PurgeExternalError("R2 deletion did not make progress")
            previous_page = current_page

            try:
                response = client.delete_objects(
                    Bucket=bucket,
                    Delete={
                        "Objects": [{"Key": key} for key in keys],
                        "Quiet": True,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                raise PurgeExternalError("R2 deletion failed") from exc
            if response.get("Errors"):
                raise PurgeExternalError("R2 reported incomplete deletion")
            deleted += len(keys)


def delete_owner_photo_directory(photo_root: Path, owner_id: str) -> bool:
    """Safely remove only ``photo_root/{canonical-owner-uuid}``.

    Symlinked roots/directories and any resolved path outside the configured
    root fail closed. ``shutil.rmtree`` does not follow symlinks contained
    inside a real directory.
    """
    owner = _canonical_uuid(owner_id, field="owner id")
    root = Path(photo_root)
    if not root.exists():
        return False
    if root.is_symlink() or not root.is_dir():
        raise PurgeExternalError("photo root is not a real directory")

    root_resolved = root.resolve(strict=True)
    candidate = root / owner
    if not candidate.exists() and not candidate.is_symlink():
        return False
    if candidate.is_symlink() or not candidate.is_dir():
        raise PurgeExternalError("owner photo path is not a real directory")

    candidate_resolved = candidate.resolve(strict=True)
    if (
        candidate_resolved.parent != root_resolved
        or candidate_resolved.name != owner
    ):
        raise PurgeExternalError("owner photo path escaped its storage root")

    try:
        shutil.rmtree(candidate)
    except OSError as exc:
        raise PurgeExternalError("owner photo deletion failed") from exc
    return True


class AccountPurgeWorker:
    def __init__(
        self,
        *,
        supabase: Any,
        r2_store: R2OwnerStore,
        photo_root: Path,
        batch_size: int = 20,
        lease_minutes: int = 30,
    ):
        if not 1 <= batch_size <= 100:
            raise PurgeConfigurationError("batch size must be between 1 and 100")
        if not 5 <= lease_minutes <= 1440:
            raise PurgeConfigurationError("lease minutes must be between 5 and 1440")
        self._supabase = supabase
        self._r2_store = r2_store
        self._photo_root = Path(photo_root)
        self._batch_size = batch_size
        self._lease_minutes = lease_minutes

    def _claim(self) -> list[PurgeClaim]:
        response = self._supabase.rpc(
            "claim_accounts_for_deletion",
            {
                "p_batch_size": self._batch_size,
                "p_lease_minutes": self._lease_minutes,
            },
        ).execute()
        rows = response.data or []
        if not isinstance(rows, list):
            raise PurgeClaimError("claim response must be a list")

        claims: list[PurgeClaim] = []
        for row in rows:
            if not isinstance(row, dict):
                raise PurgeClaimError("claim row must be an object")
            claims.append(
                PurgeClaim(
                    owner_id=_canonical_uuid(row.get("owner_id"), field="owner id"),
                    claim_token=_canonical_uuid(
                        row.get("claim_token"), field="claim token"
                    ),
                )
            )
        return claims

    def probe(self) -> None:
        """Verify the exact database contract without claiming any account."""
        self._r2_store.probe()
        response = self._supabase.rpc(
            "account_deletion_worker_contract_version",
            {},
        ).execute()
        if response.data != 2:
            raise PurgeConfigurationError(
                "unsupported account deletion worker database contract"
            )
        if not self._r2_store.enabled:
            artifacts = (
                self._supabase.table("pdf_artifacts")
                .select("id")
                .limit(1)
                .execute()
                .data
                or []
            )
            if artifacts:
                raise PurgeConfigurationError(
                    "R2 never-used assertion conflicts with artifact rows"
                )

    def _claim_is_current(self, claim: PurgeClaim) -> bool:
        response = self._supabase.rpc(
            "account_deletion_claim_is_current",
            {
                "p_owner_id": claim.owner_id,
                "p_claim_token": claim.claim_token,
            },
        ).execute()
        return response.data is True

    def run_once(self) -> PurgeCycle:
        claims = self._claim()
        purged = 0
        deferred = 0
        r2_objects = 0
        photo_directories = 0
        errors: Counter[str] = Counter()

        for claim in claims:
            try:
                if not self._r2_store.enabled:
                    artifacts = (
                        self._supabase.table("pdf_artifacts")
                        .select("id")
                        .eq("owner_id", claim.owner_id)
                        .limit(1)
                        .execute()
                        .data
                        or []
                    )
                    if artifacts:
                        raise PurgeExternalError(
                            "R2 is unavailable while artifact rows still exist"
                        )
                r2_objects += self._r2_store.delete_owner_objects(claim.owner_id)
                if delete_owner_photo_directory(self._photo_root, claim.owner_id):
                    photo_directories += 1
                if not self._claim_is_current(claim):
                    raise PurgeClaimError("claim is no longer current")
                auth_error: Exception | None = None
                try:
                    self._supabase.auth.admin.delete_user(
                        claim.owner_id,
                        should_soft_delete=False,
                    )
                except Exception as exc:  # noqa: BLE001 - verify postcondition
                    # A transport error can be ambiguous: Supabase may have
                    # committed the delete before the response was lost.
                    auth_error = exc
                remaining = (
                    self._supabase.table("profiles")
                    .select("id")
                    .eq("id", claim.owner_id)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                if remaining:
                    if auth_error is not None:
                        raise PurgeExternalError(
                            "Auth deletion failed or remained uncommitted"
                        ) from auth_error
                    raise PurgeExternalError(
                        "Auth deletion did not cascade the owner profile"
                    )
                purged += 1
            except Exception as exc:  # noqa: BLE001 - defer, never hard-delete
                deferred += 1
                errors[type(exc).__name__] += 1

        return PurgeCycle(
            claimed=len(claims),
            purged=purged,
            deferred=deferred,
            r2_objects=r2_objects,
            photo_directories=photo_directories,
            error_types=dict(sorted(errors.items())),
        )


def _build_worker(environ: dict[str, str] | None = None) -> tuple[AccountPurgeWorker, int]:
    env = dict(os.environ if environ is None else environ)
    supabase_url = (env.get("SUPABASE_URL") or "").strip()
    service_key = (env.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not supabase_url or not service_key:
        raise PurgeConfigurationError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required"
        )

    batch_size = _bounded_int(
        env,
        "ONEPAGER_ACCOUNT_PURGE_BATCH_SIZE",
        default=20,
        minimum=1,
        maximum=100,
    )
    lease_minutes = _bounded_int(
        env,
        "ONEPAGER_ACCOUNT_PURGE_LEASE_MINUTES",
        default=30,
        minimum=5,
        maximum=1440,
    )
    poll_seconds = _bounded_int(
        env,
        "ONEPAGER_ACCOUNT_PURGE_POLL_SECONDS",
        default=300,
        minimum=30,
        maximum=86400,
    )

    from supabase import create_client

    client = create_client(supabase_url, service_key)
    photo_root = Path(
        (env.get("ONEPAGER_PHOTO_ROOT") or "").strip()
        or _REPO / "design" / "photos"
    )
    never_used_asserted = (
        (env.get("ONEPAGER_R2_NEVER_USED") or "").strip().lower()
        in {"1", "true", "yes"}
    )
    worker = AccountPurgeWorker(
        supabase=client,
        r2_store=R2OwnerStore(
            _r2_config(env),
            never_used_asserted=never_used_asserted,
        ),
        photo_root=photo_root,
        batch_size=batch_size,
        lease_minutes=lease_minutes,
    )
    worker.probe()
    return worker, poll_seconds


def _error_summary(error_types: dict[str, int]) -> str:
    if not error_types:
        return "none"
    return ",".join(f"{name}:{count}" for name, count in error_types.items())


def _log_cycle(cycle: PurgeCycle) -> None:
    _LOG.info(
        "account_purge_cycle claimed=%d purged=%d deferred=%d "
        "r2_objects=%d photo_directories=%d error_types=%s",
        cycle.claimed,
        cycle.purged,
        cycle.deferred,
        cycle.r2_objects,
        cycle.photo_directories,
        _error_summary(cycle.error_types),
    )


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("ONEPAGER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        worker, poll_seconds = _build_worker()
    except Exception as exc:  # noqa: BLE001
        _LOG.critical("account_purge_startup_failed error_type=%s", type(exc).__name__)
        return 2

    if "--probe" in os.sys.argv[1:]:
        return 0

    stop = threading.Event()

    def _stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stop.is_set():
        try:
            cycle = worker.run_once()
            _log_cycle(cycle)
        except Exception as exc:  # noqa: BLE001
            _LOG.error(
                "account_purge_cycle_failed error_type=%s",
                type(exc).__name__,
            )
        stop.wait(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
