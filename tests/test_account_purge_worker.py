"""Focused contracts for the D+30 service-role account-erasure worker."""
from __future__ import annotations

import logging
from pathlib import Path
import sys
from typing import Any

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools" / "editor"))

import account_purge_worker as worker_mod  # noqa: E402
from demo_policy import (  # noqa: E402
    LOCAL_PRIVACY_CONTACT,
    resolve_privacy_contact,
)
from tools.ci.compose_env_to_docker import convert as convert_compose_env  # noqa: E402
from tools.ci.compose_env_to_docker import read_values as read_compose_env  # noqa: E402


OWNER = "11111111-1111-4111-8111-111111111111"
TOKEN = "22222222-2222-4222-8222-222222222222"


class _Response:
    def __init__(self, data: Any):
        self.data = data


class _Rpc:
    def __init__(self, client: "_Supabase", name: str):
        self._client = client
        self._name = name

    def execute(self) -> _Response:
        value = self._client.rpc_values[self._name]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        return _Response(value)


class _Query:
    def __init__(self, client: "_Supabase", table: str):
        self._client = client
        self._table = table
        self._filters: dict[str, Any] = {}
        self._limit = 1000

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, column: str, value: Any):
        self._filters[column] = value
        return self

    def limit(self, value: int):
        self._limit = value
        return self

    def execute(self) -> _Response:
        rows = self._client.tables.get(self._table, [])
        matched = [
            row
            for row in rows
            if all(row.get(key) == value for key, value in self._filters.items())
        ]
        return _Response(matched[: self._limit])


class _Admin:
    def __init__(self, client: "_Supabase"):
        self._client = client
        self.calls: list[tuple[str, bool]] = []
        self.failure: Exception | None = None
        self.failure_after_cascade: Exception | None = None
        self.cascade = True

    def delete_user(self, owner_id: str, should_soft_delete: bool = False):
        self.calls.append((owner_id, should_soft_delete))
        if self.failure:
            raise self.failure
        if self.cascade:
            self._client.tables["profiles"] = [
                row
                for row in self._client.tables.get("profiles", [])
                if row.get("id") != owner_id
            ]
        if self.failure_after_cascade:
            raise self.failure_after_cascade


class _Auth:
    def __init__(self, client: "_Supabase"):
        self.admin = _Admin(client)


class _Supabase:
    def __init__(self):
        self.rpc_values: dict[str, Any] = {
            "account_deletion_worker_contract_version": 2,
            "claim_accounts_for_deletion": [
                {"owner_id": OWNER, "claim_token": TOKEN}
            ],
            "account_deletion_claim_is_current": True,
        }
        self.tables: dict[str, list[dict[str, Any]]] = {
            "pdf_artifacts": [],
            "profiles": [{"id": OWNER}],
        }
        self.auth = _Auth(self)

    def rpc(self, name: str, _params: dict[str, Any]):
        return _Rpc(self, name)

    def table(self, name: str):
        return _Query(self, name)


class _R2:
    def __init__(self, keys: list[str]):
        self.keys = list(keys)
        self.fail_list = False
        self.fail_delete = False
        self.delete_errors = False
        self.inject_out_of_prefix = False
        self.ignore_delete = False
        self.delete_calls = 0

    def list_objects_v2(self, **kwargs):
        if self.fail_list:
            raise OSError("synthetic listing secret: owner=" + OWNER)
        prefix = kwargs.get("Prefix", "")
        keys = [key for key in self.keys if key.startswith(prefix)][:1000]
        if self.inject_out_of_prefix:
            keys = ["another-owner/private.pdf"]
        return {"Contents": [{"Key": key} for key in keys]}

    def delete_objects(self, *, Delete: dict[str, Any], **_kwargs):
        self.delete_calls += 1
        if self.fail_delete:
            raise OSError("synthetic deletion secret: token=" + TOKEN)
        if self.delete_errors:
            return {"Errors": [{"Code": "synthetic"}]}
        deleting = {row["Key"] for row in Delete["Objects"]}
        if not self.ignore_delete:
            self.keys = [key for key in self.keys if key not in deleting]
        return {}


def _configured_r2(fake: _R2) -> worker_mod.R2OwnerStore:
    return worker_mod.R2OwnerStore(
        {
            "R2_ACCOUNT_ID": "0123456789abcdef0123456789ABCDEF",
            "R2_ACCESS_KEY_ID": "access",
            "R2_SECRET_ACCESS_KEY": "secret",
            "R2_BUCKET": "bucket",
        },
        client=fake,
    )


def _worker(
    tmp_path: Path,
    *,
    supabase: _Supabase | None = None,
    r2_store: worker_mod.R2OwnerStore | None = None,
) -> tuple[worker_mod.AccountPurgeWorker, _Supabase]:
    sb = supabase or _Supabase()
    store = r2_store or worker_mod.R2OwnerStore(
        None,
        never_used_asserted=True,
    )
    return (
        worker_mod.AccountPurgeWorker(
            supabase=sb,
            r2_store=store,
            photo_root=tmp_path / "photos",
        ),
        sb,
    )


class TestMigrationContract:
    @pytest.fixture
    def sql(self) -> str:
        migrations = REPO / "supabase" / "migrations"
        return "\n".join(
            (
                (migrations / "0019_account_deletion_worker_claim.sql").read_text(),
                (migrations / "0023_security_contract_hardening.sql").read_text(),
            )
        )

    def test_claim_and_cancel_serialize_at_fixed_d30(self, sql: str):
        lowered = sql.lower()
        assert "private.account_deletion_claims" in lowered
        assert "interval '30 days'" in lowered
        assert "for update of p skip locked" in lowered
        assert "for update;" in lowered
        assert "p.deletion_requested_at <=" in lowered
        assert "c.owner_id = caller_id" in lowered
        assert "lease_expires_at <= claimed_at" in lowered

    def test_service_role_only_worker_rpcs_and_legacy_purge_removed(self, sql: str):
        lowered = sql.lower()
        assert "drop function if exists public.purge_deleted_accounts(integer)" in lowered
        for signature in (
            "public.claim_accounts_for_deletion(integer, integer)",
            "public.account_deletion_claim_is_current(uuid, uuid)",
            "public.account_deletion_worker_contract_version()",
        ):
            assert f"revoke execute on function {signature}" in lowered
            assert f"grant execute on function {signature}" in lowered
        assert "to service_role" in lowered

    def test_versioned_contract_probe_is_exact_and_non_mutating(self, sql: str):
        block = sql.split(
            "create or replace function public.account_deletion_worker_contract_version()",
            1,
        )[1].split("$$;", 1)[0]
        assert "select 2" in block
        assert "insert " not in block.lower()
        assert "update " not in block.lower()
        assert "delete " not in block.lower()

    def test_claim_controls_reject_null(self, sql: str):
        latest_claim = sql.rsplit(
            "create or replace function public.claim_accounts_for_deletion(",
            1,
        )[1].split("$$;", 1)[0]
        assert "p_batch_size is null" in latest_claim.lower()
        assert "p_lease_minutes is null" in latest_claim.lower()


class TestR2Deletion:
    def test_deletes_more_than_one_page_until_prefix_is_empty(self):
        fake = _R2([f"{OWNER}/{i:04d}.pdf" for i in range(1002)])
        store = _configured_r2(fake)

        assert store.delete_owner_objects(OWNER) == 1002
        assert fake.keys == []
        assert fake.delete_calls == 2

    @pytest.mark.parametrize(
        ("attribute", "expected"),
        [
            ("fail_list", "PurgeExternalError"),
            ("fail_delete", "PurgeExternalError"),
            ("delete_errors", "PurgeExternalError"),
            ("inject_out_of_prefix", "PurgeExternalError"),
        ],
    )
    def test_storage_errors_fail_closed(self, attribute: str, expected: str):
        fake = _R2([f"{OWNER}/document.pdf"])
        setattr(fake, attribute, True)

        with pytest.raises(getattr(worker_mod, expected)):
            _configured_r2(fake).delete_owner_objects(OWNER)

    def test_delete_that_makes_no_progress_fails_closed(self):
        fake = _R2([f"{OWNER}/document.pdf"])
        fake.ignore_delete = True

        with pytest.raises(worker_mod.PurgeExternalError, match="progress"):
            _configured_r2(fake).delete_owner_objects(OWNER)
        assert fake.delete_calls == 1

    def test_partial_config_and_unproven_absence_are_refused(self):
        with pytest.raises(worker_mod.PurgeConfigurationError):
            worker_mod._r2_config({"R2_ACCOUNT_ID": "only-one"})
        with pytest.raises(worker_mod.PurgeConfigurationError):
            worker_mod.R2OwnerStore(None)

    @pytest.mark.parametrize(
        "account_id",
        [
            "a" * 31,
            "g" * 32,
            "a" * 31 + "/",
            "a" * 31 + ".",
            "a" * 31 + "@",
            "a" * 31 + ":",
        ],
    )
    def test_malformed_account_id_cannot_change_endpoint(
        self, account_id: str
    ):
        environ = {
            "R2_ACCOUNT_ID": account_id,
            "R2_ACCESS_KEY_ID": "access",
            "R2_SECRET_ACCESS_KEY": "secret",
            "R2_BUCKET": "bucket",
        }
        with pytest.raises(
            worker_mod.PurgeConfigurationError,
            match="32 hexadecimal",
        ):
            worker_mod._r2_config(environ)

    def test_explicit_never_used_mode_allows_no_r2(self):
        store = worker_mod.R2OwnerStore(None, never_used_asserted=True)
        assert store.delete_owner_objects(OWNER) == 0

    def test_probe_authenticates_to_configured_bucket(self):
        fake = _R2([])
        _configured_r2(fake).probe()
        fake.fail_list = True
        with pytest.raises(worker_mod.PurgeExternalError):
            _configured_r2(fake).probe()


class TestPhotoDeletion:
    def test_deletes_exact_owner_directory_only(self, tmp_path: Path):
        root = tmp_path / "photos"
        owner_dir = root / OWNER
        other_dir = root / "33333333-3333-4333-8333-333333333333"
        owner_dir.mkdir(parents=True)
        other_dir.mkdir()
        (owner_dir / "photo.png").write_bytes(b"owner")
        (other_dir / "photo.png").write_bytes(b"other")

        assert worker_mod.delete_owner_photo_directory(root, OWNER) is True
        assert not owner_dir.exists()
        assert (other_dir / "photo.png").read_bytes() == b"other"

    def test_symlink_owner_directory_fails_without_touching_target(
        self, tmp_path: Path
    ):
        root = tmp_path / "photos"
        target = tmp_path / "outside"
        root.mkdir()
        target.mkdir()
        secret = target / "do-not-delete"
        secret.write_text("preserved")
        (root / OWNER).symlink_to(target, target_is_directory=True)

        with pytest.raises(worker_mod.PurgeExternalError):
            worker_mod.delete_owner_photo_directory(root, OWNER)
        assert secret.read_text() == "preserved"

    @pytest.mark.parametrize(
        "owner",
        [
            "../../tmp",
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            "not-a-uuid",
        ],
    )
    def test_noncanonical_owner_is_refused(self, tmp_path: Path, owner: str):
        with pytest.raises(worker_mod.PurgeClaimError):
            worker_mod.delete_owner_photo_directory(tmp_path, owner)


class TestWorkerOrderingAndRetry:
    def test_probe_fails_when_migration_is_absent(self, tmp_path: Path):
        worker, sb = _worker(tmp_path)
        sb.rpc_values["account_deletion_worker_contract_version"] = RuntimeError(
            "missing RPC"
        )
        with pytest.raises(RuntimeError, match="missing RPC"):
            worker.probe()

    @pytest.mark.parametrize("version", [None, True, 1, 3, "2"])
    def test_probe_rejects_incompatible_contract_versions(
        self, tmp_path: Path, version: object
    ):
        worker, sb = _worker(tmp_path)
        sb.rpc_values["account_deletion_worker_contract_version"] = version

        with pytest.raises(
            worker_mod.PurgeConfigurationError,
            match="unsupported account deletion worker database contract",
        ):
            worker.probe()

    def test_probe_rejects_false_r2_never_used_assertion(self, tmp_path: Path):
        worker, sb = _worker(tmp_path)
        sb.tables["pdf_artifacts"] = [{"id": "artifact", "owner_id": OWNER}]
        with pytest.raises(worker_mod.PurgeConfigurationError):
            worker.probe()

    def test_expired_claim_never_reaches_admin_delete(self, tmp_path: Path):
        worker, sb = _worker(tmp_path)
        sb.rpc_values["account_deletion_claim_is_current"] = False

        cycle = worker.run_once()

        assert cycle.purged == 0
        assert cycle.deferred == 1
        assert cycle.error_types == {"PurgeClaimError": 1}
        assert sb.auth.admin.calls == []
        assert sb.tables["profiles"] == [{"id": OWNER}]

    def test_r2_failure_defers_then_idempotent_retry_completes(self, tmp_path: Path):
        fake_r2 = _R2([f"{OWNER}/document.pdf"])
        fake_r2.fail_delete = True
        store = _configured_r2(fake_r2)
        worker, sb = _worker(tmp_path, r2_store=store)
        owner_dir = tmp_path / "photos" / OWNER
        owner_dir.mkdir(parents=True)
        (owner_dir / "photo.png").write_bytes(b"photo")

        first = worker.run_once()
        assert first.purged == 0
        assert first.deferred == 1
        assert sb.auth.admin.calls == []
        assert owner_dir.exists()

        fake_r2.fail_delete = False
        second = worker.run_once()
        assert second.purged == 1
        assert second.deferred == 0
        assert fake_r2.keys == []
        assert not owner_dir.exists()
        assert sb.tables["profiles"] == []

    def test_admin_failure_keeps_database_profile_for_retry(self, tmp_path: Path):
        worker, sb = _worker(tmp_path)
        sb.auth.admin.failure = RuntimeError("synthetic admin error")

        cycle = worker.run_once()

        assert cycle.purged == 0
        assert cycle.deferred == 1
        assert sb.tables["profiles"] == [{"id": OWNER}]

    def test_missing_r2_credentials_with_artifact_rows_fails_closed(
        self, tmp_path: Path
    ):
        worker, sb = _worker(tmp_path)
        sb.tables["pdf_artifacts"] = [{"id": "artifact", "owner_id": OWNER}]

        cycle = worker.run_once()

        assert cycle.purged == 0
        assert cycle.deferred == 1
        assert sb.auth.admin.calls == []

    def test_auth_success_requires_observed_profile_cascade(self, tmp_path: Path):
        worker, sb = _worker(tmp_path)
        sb.auth.admin.cascade = False

        cycle = worker.run_once()

        assert cycle.purged == 0
        assert cycle.deferred == 1
        assert sb.auth.admin.calls == [(OWNER, False)]
        assert sb.tables["profiles"] == [{"id": OWNER}]

    def test_ambiguous_auth_error_counts_as_success_when_cascade_is_observed(
        self, tmp_path: Path
    ):
        worker, sb = _worker(tmp_path)
        sb.auth.admin.failure_after_cascade = TimeoutError(
            "synthetic lost response"
        )

        cycle = worker.run_once()

        assert cycle.purged == 1
        assert cycle.deferred == 0
        assert sb.tables["profiles"] == []

    def test_success_uses_hard_delete_after_cleanup(self, tmp_path: Path):
        worker, sb = _worker(tmp_path)
        owner_dir = tmp_path / "photos" / OWNER
        owner_dir.mkdir(parents=True)
        (owner_dir / "photo.webp").write_bytes(b"photo")

        cycle = worker.run_once()

        assert cycle == worker_mod.PurgeCycle(
            claimed=1,
            purged=1,
            deferred=0,
            r2_objects=0,
            photo_directories=1,
            error_types={},
        )
        assert sb.auth.admin.calls == [(OWNER, False)]
        assert not owner_dir.exists()

    def test_cycle_log_is_aggregate_only(self, caplog: pytest.LogCaptureFixture):
        caplog.set_level(logging.INFO, logger="account_purge_worker")
        cycle = worker_mod.PurgeCycle(
            claimed=1,
            purged=0,
            deferred=1,
            r2_objects=0,
            photo_directories=0,
            error_types={"PurgeExternalError": 1},
        )

        worker_mod._log_cycle(cycle)

        text = caplog.text
        assert "claimed=1" in text
        assert "PurgeExternalError:1" in text
        for sensitive in (OWNER, TOKEN, "secret", "document.pdf"):
            assert sensitive not in text


class TestPublicPrivacyContracts:
    def test_loopback_uses_reserved_placeholder(self):
        assert resolve_privacy_contact({}) == LOCAL_PRIVACY_CONTACT
        assert LOCAL_PRIVACY_CONTACT.endswith(".invalid")

    def test_hosted_origin_requires_explicit_valid_contact(self):
        with pytest.raises(RuntimeError, match="ONEPAGER_PRIVACY_CONTACT"):
            resolve_privacy_contact({"PUBLIC_BASE_URL": "https://cv.example.com"})
        assert (
            resolve_privacy_contact(
                {
                    "PUBLIC_BASE_URL": "https://cv.example.com",
                    "ONEPAGER_PRIVACY_CONTACT": "privacy@example.com",
                }
            )
            == "privacy@example.com"
        )
        with pytest.raises(RuntimeError, match="valid email"):
            resolve_privacy_contact(
                {
                    "PUBLIC_BASE_URL": "https://cv.example.com",
                    "ONEPAGER_PRIVACY_CONTACT": '"><script>alert(1)</script>',
                }
            )

    def test_privacy_page_states_exact_export_and_erasure_scope(self):
        privacy = (REPO / "tools" / "editor" / "static" / "privacy.html").read_text()
        account = (REPO / "tools" / "editor" / "static" / "account.html").read_text()
        assert "copy of all your data" not in privacy
        for included in (
            "active profile",
            "job-import provenance",
            "live application-packet lineage",
            "provider/key hints",
        ):
            assert included in privacy
        for excluded in (
            "usage/credit events",
            "R2 files",
            "application-attempt audit rows",
            "uploaded photo files",
            "encrypted key material",
        ):
            assert excluded in privacy
        assert "completion can therefore occur after D+30" in privacy
        assert "External cleanup failures" in account

    def test_production_compose_and_deploy_require_healthy_worker(self):
        compose = (REPO / "docker-compose.yml").read_text()
        workflow = (REPO / ".github" / "workflows" / "deploy.yml").read_text()
        deploy = (REPO / "DEPLOY.md").read_text()
        assert "purge-worker:" in compose
        assert 'profiles: ["prod"]' in compose
        assert "account_purge_worker.py\", \"--probe" in compose
        assert "worker_health" in workflow
        assert 'test "$worker_health" = "healthy"' in workflow
        assert "onepager-erasure-keeper" in workflow
        assert "onepager-erasure-candidate" in workflow
        assert "--volume onepager_onepager_photos:/app/design/photos" in workflow
        assert 'docker load --input "$REMOTE_IMAGES"' in workflow
        assert "up -d --no-build --pull never --remove-orphans" in workflow
        assert "ERASURE_CANDIDATE_READY=1" in workflow
        assert '--env-file "$NEXT_DIR/.env.docker"' in workflow
        assert "tools/ci/compose_env_to_docker.py" in workflow
        assert 'rm -f "$NEXT_DIR/.env.docker"' in workflow
        assert "PUBLIC_BASE_URL='$PUBLIC_BASE_URL'" not in workflow
        assert "--get \"$NEXT_DIR/.env\" PUBLIC_BASE_URL" in workflow
        assert "umask 077" in workflow
        assert "0019_account_deletion_worker_claim.sql" in deploy
        assert "Remove any old pg_cron" in deploy
        assert "forward-only" in deploy
        assert "never restores a state without erasure" not in deploy
        normalized_deploy = " ".join(deploy.split())
        assert (
            "existing application remains in an explicit erasure-blocked state"
            in normalized_deploy
        )


class TestDeployEnvConversion:
    def test_decodes_compose_quotes_without_changing_values(self, tmp_path: Path):
        source = tmp_path / "compose.env"
        target = tmp_path / "docker.env"
        source.write_text(
            'SUPABASE_URL="https://example.supabase.co"\n'
            'SERVICE_KEY="value with spaces and #hash = equals"\n'
            'ONEPAGER_R2_NEVER_USED="1"\n'
        )

        convert_compose_env(source, target)

        assert target.read_text() == (
            "SUPABASE_URL=https://example.supabase.co\n"
            "SERVICE_KEY=value with spaces and #hash = equals\n"
            "ONEPAGER_R2_NEVER_USED=1\n"
        )
        assert target.stat().st_mode & 0o777 == 0o600
        assert read_compose_env(source)["SUPABASE_URL"] == (
            "https://example.supabase.co"
        )

    def test_rejects_line_breaks_that_docker_env_cannot_represent(
        self, tmp_path: Path
    ):
        source = tmp_path / "compose.env"
        source.write_text('SECRET="line\\nsecond"\n')

        with pytest.raises(ValueError, match="Docker-incompatible"):
            convert_compose_env(source, tmp_path / "docker.env")
