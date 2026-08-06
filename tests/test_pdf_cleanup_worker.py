"""Focused contracts for the service-role PDF cleanup outbox worker."""
from __future__ import annotations

from copy import deepcopy
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools" / "editor"))

import pdf_cleanup_worker as worker_mod  # noqa: E402


OWNER = "11111111-1111-4111-8111-111111111111"
OTHER_OWNER = "22222222-2222-4222-8222-222222222222"
DOCUMENT = "33333333-3333-4333-8333-333333333333"
OTHER_DOCUMENT = "44444444-4444-4444-8444-444444444444"
CLEANUP = "55555555-5555-4555-8555-555555555555"
OTHER_CLEANUP = "66666666-6666-4666-8666-666666666666"
GENERATION = "77777777-7777-4777-8777-777777777777"
CONTENT_HASH = "a" * 64
OTHER_HASH = "b" * 64
KEY = f"{OWNER}/{DOCUMENT}/{CONTENT_HASH}.pdf"
OTHER_KEY = f"{OTHER_OWNER}/{OTHER_DOCUMENT}/{OTHER_HASH}.pdf"
VERSIONED_KEY = f"{OWNER}/{DOCUMENT}/{CONTENT_HASH}/{GENERATION}.pdf"


def _row(
    *,
    cleanup_id: str = CLEANUP,
    owner_id: str = OWNER,
    document_id: str = DOCUMENT,
    content_hash: str = CONTENT_HASH,
    generation_id: str | None = None,
    r2_key: str = KEY,
    attempt_count: int = 0,
    created_at: str = "2026-08-01T10:00:00+00:00",
    last_attempt_at: str | None = None,
    not_before: str = "2026-08-01T10:00:00+00:00",
) -> dict[str, Any]:
    return {
        "id": cleanup_id,
        "owner_id": owner_id,
        "document_id": document_id,
        "content_hash": content_hash,
        "generation_id": generation_id,
        "r2_key": r2_key,
        "attempt_count": attempt_count,
        "created_at": created_at,
        "last_attempt_at": last_attempt_at,
        "not_before": not_before,
        "claim_token": None,
        "claimed_at": None,
    }


class _Response:
    def __init__(self, data: Any):
        self.data = data


class _Rpc:
    def __init__(self, client: "_Supabase", name: str, params: dict[str, Any]):
        self._client = client
        self._name = name
        self._params = params

    def execute(self) -> _Response:
        if self._client.fail_all_queries:
            raise OSError("database secret=" + self._client.secret)
        rows = self._client.tables["pdf_artifact_cleanup_queue"]
        cleanup_id = self._params.get("p_cleanup_id")
        owner_id = self._params.get("p_owner_id")
        matching = [
            row
            for row in rows
            if row.get("id") == cleanup_id and row.get("owner_id") == owner_id
        ]
        if self._name == "claim_pdf_artifact_cleanup":
            if not matching:
                return _Response({"status": "missing"})
            row = matching[0]
            if row.get("claim_token") is not None:
                return _Response({"status": "busy"})
            active = any(
                artifact.get("owner_id") == owner_id
                and artifact.get("document_id") == row.get("document_id")
                and artifact.get("content_hash") == row.get("content_hash")
                and artifact.get("generation_id") == row.get("generation_id")
                and artifact.get("r2_key") == row.get("r2_key")
                and artifact.get("stale") is False
                for artifact in self._client.tables["pdf_artifacts"]
            )
            if active:
                self._client.tables["pdf_artifact_cleanup_queue"].remove(row)
                return _Response({"status": "active"})
            try:
                worker_mod._cleanup_item(row)
            except worker_mod.CleanupItemError:
                row["attempt_count"] += 1
                row["last_attempt_at"] = "2026-08-01T12:00:00+00:00"
                return _Response(
                    {
                        "status": "blocked",
                        "code": "cleanup_key_invalid",
                        "failure_recorded": True,
                    }
                )
            claim_token = self._params.get("p_claim_token")
            row["claim_token"] = claim_token
            row["claimed_at"] = "2026-08-01T12:00:00+00:00"
            return _Response(
                {
                    "status": "claimed",
                    "cleanup_id": row["id"],
                    "owner_id": row["owner_id"],
                    "document_id": row["document_id"],
                    "content_hash": row["content_hash"],
                    "generation_id": row.get("generation_id"),
                    "r2_key": row["r2_key"],
                    "not_before": row["not_before"],
                    "attempt_count": row["attempt_count"],
                    "claim_token": claim_token,
                }
            )
        if self._name == "complete_pdf_artifact_cleanup":
            if self._client.fail_next_queue_delete:
                self._client.fail_next_queue_delete = False
                raise TimeoutError("delete response secret=" + self._client.secret)
            self._client.events.append("queue-delete")
            matching = [
                row
                for row in matching
                if row.get("claim_token") == self._params.get("p_claim_token")
            ]
            deleting = {id(row) for row in matching}
            self._client.tables["pdf_artifact_cleanup_queue"] = [
                row for row in rows if id(row) not in deleting
            ]
            return _Response(bool(matching))
        if self._name == "record_pdf_artifact_cleanup_failure":
            if self._client.before_failure_rpc is not None:
                self._client.before_failure_rpc()
                self._client.before_failure_rpc = None
            self._client.events.append("failure-update")
            # Re-resolve after a simulated concurrent mutation.
            matching = [
                row
                for row in self._client.tables["pdf_artifact_cleanup_queue"]
                if row.get("id") == cleanup_id
                and row.get("owner_id") == owner_id
                and row.get("claim_token") == self._params.get("p_claim_token")
            ]
            for row in matching:
                row["attempt_count"] += 1
                row["last_attempt_at"] = "2026-08-01T12:00:00+00:00"
                row["claim_token"] = None
                row["claimed_at"] = None
            return _Response(bool(matching))
        raise AssertionError("unsupported RPC")


class _Query:
    def __init__(self, client: "_Supabase", table: str):
        self._client = client
        self._table = table
        self._filters: list[tuple[str, Any]] = []
        self._orders: list[tuple[str, bool, bool]] = []
        self._limit: int | None = None
        self._operation = "select"
        self._updates: dict[str, Any] = {}
        self._lte: list[tuple[str, Any]] = []

    def select(self, *_args, **_kwargs):
        return self

    def order(
        self,
        column: str,
        *,
        desc: bool = False,
        nullsfirst: bool = False,
    ):
        self._orders.append((column, desc, nullsfirst))
        return self

    def limit(self, value: int):
        self._limit = value
        self._client.observed_limits.append(value)
        return self

    def eq(self, column: str, value: Any):
        self._filters.append((column, value))
        return self

    def lte(self, column: str, value: Any):
        self._lte.append((column, value))
        return self

    def is_(self, column: str, value: str):
        if value != "null":
            raise AssertionError("fake query supports only IS NULL")
        self._filters.append((column, None))
        return self

    def delete(self):
        self._operation = "delete"
        return self

    def update(self, values: dict[str, Any]):
        self._operation = "update"
        self._updates = dict(values)
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        return all(
            row.get(column) == value for column, value in self._filters
        ) and all(
            row.get(column) is not None and str(row.get(column)) <= str(value)
            for column, value in self._lte
        )

    @staticmethod
    def _sort_value(value: Any, *, nullsfirst: bool) -> tuple[int, Any]:
        if value is None:
            return (0 if nullsfirst else 1, "")
        return (1 if nullsfirst else 0, value)

    def execute(self) -> _Response:
        if self._client.fail_all_queries:
            raise OSError("database secret=" + self._client.secret)
        rows = self._client.tables.setdefault(self._table, [])
        matched = [row for row in rows if self._matches(row)]
        for column, desc, nullsfirst in reversed(self._orders):
            matched.sort(
                key=lambda row: self._sort_value(
                    row.get(column), nullsfirst=nullsfirst
                ),
                reverse=desc,
            )
        if self._limit is not None:
            matched = matched[: self._limit]

        if self._operation == "delete":
            if self._client.fail_next_queue_delete:
                self._client.fail_next_queue_delete = False
                raise TimeoutError("delete response secret=" + self._client.secret)
            deleting = {id(row) for row in matched}
            self._client.events.append("queue-delete")
            self._client.tables[self._table] = [
                row for row in rows if id(row) not in deleting
            ]
            return _Response(deepcopy(matched))

        if self._operation == "update":
            self._client.events.append("failure-update")
            for row in matched:
                row.update(self._updates)
            return _Response(deepcopy(matched))

        return _Response(deepcopy(matched))


class _Supabase:
    def __init__(self, rows: list[dict[str, Any]]):
        self.tables = {
            "pdf_artifact_cleanup_queue": deepcopy(rows),
            "pdf_artifacts": [],
        }
        self.events: list[str] = []
        self.observed_limits: list[int] = []
        self.fail_next_queue_delete = False
        self.fail_all_queries = False
        self.secret = "database-token-do-not-log"
        self.before_failure_rpc = None
        self.rpc_calls: list[tuple[str, dict[str, Any]]] = []

    def table(self, name: str) -> _Query:
        return _Query(self, name)

    def rpc(self, name: str, params: dict[str, Any]) -> _Rpc:
        self.rpc_calls.append((name, deepcopy(params)))
        return _Rpc(self, name, params)


class _R2:
    def __init__(self):
        self.deleted: list[str] = []
        self.fail_keys: set[str] = set()
        self.events: list[str] = []
        self.probes = 0
        self.list_probes = 0
        self.secret = "r2-secret-do-not-log"

    def list_objects_v2(self, **_kwargs):
        self.list_probes += 1
        return {"Contents": []}

    def delete_object(self, *, Key: str, **_kwargs):
        self.events.append("r2-delete")
        if Key.startswith(worker_mod._HEALTHCHECK_R2_PREFIX):
            self.probes += 1
        if Key in self.fail_keys:
            raise OSError(f"provider key={Key} secret={self.secret}")
        self.deleted.append(Key)
        return {}


def _store(fake: _R2) -> worker_mod.R2ArtifactStore:
    return worker_mod.R2ArtifactStore(
        {
            "R2_ACCOUNT_ID": "0123456789abcdef0123456789ABCDEF",
            "R2_ACCESS_KEY_ID": "access",
            "R2_SECRET_ACCESS_KEY": "secret",
            "R2_BUCKET": "bucket",
        },
        client=fake,
    )


def _worker(
    rows: list[dict[str, Any]],
    *,
    batch_size: int = 20,
) -> tuple[worker_mod.PdfCleanupWorker, _Supabase, _R2]:
    supabase = _Supabase(rows)
    r2 = _R2()
    return (
        worker_mod.PdfCleanupWorker(
            supabase=supabase,
            r2_store=_store(r2),
            batch_size=batch_size,
        ),
        supabase,
        r2,
    )


class TestCanonicalCleanup:
    def test_deletes_object_before_acknowledging_queue_row(self):
        worker, supabase, r2 = _worker([_row()])
        original_delete = r2.delete_object

        def delete_with_shared_event(**kwargs):
            supabase.events.append("r2-delete")
            return original_delete(**kwargs)

        r2.delete_object = delete_with_shared_event

        cycle = worker.run_once()

        assert cycle == worker_mod.CleanupCycle(
            selected=1,
            completed=1,
            deferred=0,
            r2_objects=1,
            failure_updates=0,
            failure_update_errors=0,
            error_types={},
        )
        assert r2.deleted == [KEY]
        assert supabase.events == ["r2-delete", "queue-delete"]
        assert supabase.tables["pdf_artifact_cleanup_queue"] == []

    def test_deletes_exact_generation_versioned_key(self):
        worker, supabase, r2 = _worker(
            [_row(generation_id=GENERATION, r2_key=VERSIONED_KEY)]
        )

        cycle = worker.run_once()

        assert cycle.completed == 1
        assert cycle.deferred == 0
        assert r2.deleted == [VERSIONED_KEY]
        assert supabase.tables["pdf_artifact_cleanup_queue"] == []

    @pytest.mark.parametrize(
        ("generation_id", "r2_key"),
        [
            (GENERATION, KEY),
            (None, VERSIONED_KEY),
            (
                "88888888-8888-4888-8888-888888888888",
                VERSIONED_KEY,
            ),
            (GENERATION.upper().replace("7", "A"), VERSIONED_KEY),
        ],
    )
    def test_generation_and_key_must_match_exactly(
        self,
        generation_id: str | None,
        r2_key: str,
    ):
        row = _row(generation_id=generation_id, r2_key=r2_key)

        with pytest.raises(worker_mod.CleanupItemError):
            worker_mod._cleanup_item(row)

    @pytest.mark.parametrize(
        "changes",
        [
            {"owner_id": OTHER_OWNER},
            {"document_id": "not-a-document"},
            {"content_hash": "A" * 64},
            {"r2_key": f"{OWNER}/../another-object.pdf"},
        ],
    )
    def test_noncanonical_identity_is_never_sent_to_r2(self, changes: dict):
        row = _row()
        row.update(changes)
        worker, supabase, r2 = _worker([row])

        cycle = worker.run_once()

        assert cycle.selected == 1
        assert cycle.completed == 0
        assert cycle.deferred == 1
        assert cycle.failure_updates == 1
        assert cycle.error_types == {"CleanupItemError": 1}
        assert r2.deleted == []
        queued = supabase.tables["pdf_artifact_cleanup_queue"]
        assert queued[0]["attempt_count"] == 1
        assert queued[0]["last_attempt_at"] is not None

    def test_noncanonical_owner_representation_is_rejected(self):
        row = _row(owner_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")

        with pytest.raises(worker_mod.CleanupItemError, match="owner id"):
            worker_mod._cleanup_item(row)

    def test_batch_is_bounded_and_prioritizes_unattempted_rows(self):
        first = _row(last_attempt_at="2026-08-01T12:00:00+00:00")
        second = _row(
            cleanup_id=OTHER_CLEANUP,
            owner_id=OTHER_OWNER,
            document_id=OTHER_DOCUMENT,
            content_hash=OTHER_HASH,
            r2_key=OTHER_KEY,
            created_at="2026-08-01T11:00:00+00:00",
        )
        third_hash = "c" * 64
        third_cleanup = "77777777-7777-4777-8777-777777777777"
        third_document = "88888888-8888-4888-8888-888888888888"
        third = _row(
            cleanup_id=third_cleanup,
            document_id=third_document,
            content_hash=third_hash,
            r2_key=f"{OWNER}/{third_document}/{third_hash}.pdf",
            created_at="2026-08-01T09:00:00+00:00",
        )
        worker, supabase, r2 = _worker([first, second, third], batch_size=2)

        cycle = worker.run_once()

        assert cycle.selected == 2
        assert cycle.completed == 2
        assert r2.deleted == [
            f"{OWNER}/{third_document}/{third_hash}.pdf",
            OTHER_KEY,
        ]
        remaining = supabase.tables["pdf_artifact_cleanup_queue"]
        assert [row["id"] for row in remaining] == [CLEANUP]
        assert 2 in supabase.observed_limits


class TestIsolationAndRetry:
    def test_fresh_metadata_reference_defers_without_deleting_object(self):
        worker, supabase, r2 = _worker([_row()])
        supabase.tables["pdf_artifacts"] = [
            {
                "id": "99999999-9999-4999-8999-999999999999",
                "owner_id": OWNER,
                "document_id": DOCUMENT,
                "content_hash": CONTENT_HASH,
                "generation_id": None,
                "r2_key": KEY,
                "stale": False,
            }
        ]

        cycle = worker.run_once()

        assert cycle.completed == 1
        assert cycle.deferred == 0
        assert cycle.r2_objects == 0
        assert cycle.failure_updates == 0
        assert cycle.error_types == {}
        assert r2.deleted == []
        assert supabase.tables["pdf_artifact_cleanup_queue"] == []

    def test_matching_fresh_versioned_reference_is_deferred(self):
        worker, supabase, r2 = _worker(
            [_row(generation_id=GENERATION, r2_key=VERSIONED_KEY)]
        )
        supabase.tables["pdf_artifacts"] = [
            {
                "id": "99999999-9999-4999-8999-999999999999",
                "owner_id": OWNER,
                "document_id": DOCUMENT,
                "content_hash": CONTENT_HASH,
                "generation_id": GENERATION,
                "r2_key": VERSIONED_KEY,
                "stale": False,
            }
        ]

        cycle = worker.run_once()

        assert cycle.completed == 1
        assert cycle.deferred == 0
        assert cycle.error_types == {}
        assert r2.deleted == []
        assert supabase.tables["pdf_artifact_cleanup_queue"] == []

    def test_r2_failure_increments_attempt_and_continues_next_row(self):
        good = _row(
            cleanup_id=OTHER_CLEANUP,
            owner_id=OTHER_OWNER,
            document_id=OTHER_DOCUMENT,
            content_hash=OTHER_HASH,
            r2_key=OTHER_KEY,
            created_at="2026-08-01T11:00:00+00:00",
        )
        worker, supabase, r2 = _worker([_row(), good])
        r2.fail_keys.add(KEY)

        cycle = worker.run_once()

        assert cycle.selected == 2
        assert cycle.completed == 1
        assert cycle.deferred == 1
        assert cycle.r2_objects == 1
        assert cycle.failure_updates == 1
        assert cycle.error_types == {"CleanupExternalError": 1}
        assert r2.deleted == [OTHER_KEY]
        remaining = supabase.tables["pdf_artifact_cleanup_queue"]
        assert len(remaining) == 1
        assert remaining[0]["id"] == CLEANUP
        assert remaining[0]["attempt_count"] == 1

    def test_ack_failure_retains_queue_for_idempotent_object_retry(self):
        worker, supabase, r2 = _worker([_row()])
        supabase.fail_next_queue_delete = True

        first = worker.run_once()

        assert first.completed == 0
        assert first.deferred == 1
        assert first.r2_objects == 1
        assert first.failure_updates == 1
        assert r2.deleted == [KEY]
        assert supabase.tables["pdf_artifact_cleanup_queue"][0][
            "attempt_count"
        ] == 1

        second = worker.run_once()

        assert second.completed == 1
        assert second.deferred == 0
        assert r2.deleted == [KEY, KEY]
        assert supabase.tables["pdf_artifact_cleanup_queue"] == []

    def test_failure_rpc_atomically_increments_after_concurrent_advance(self):
        worker, supabase, r2 = _worker([_row()])
        r2.fail_keys.add(KEY)
        supabase.before_failure_rpc = lambda: supabase.tables[
            "pdf_artifact_cleanup_queue"
        ][0].update({"attempt_count": 1})

        cycle = worker.run_once()

        assert cycle.failure_updates == 1
        assert cycle.failure_update_errors == 0
        assert supabase.tables["pdf_artifact_cleanup_queue"][0][
            "attempt_count"
        ] == 2


class TestProbeAndServiceLoop:
    def test_probe_deletes_only_a_unique_reserved_absent_key_and_safe_miss_rpc(self):
        worker, supabase, r2 = _worker([_row()])

        worker.probe()

        assert r2.probes == 1
        assert r2.list_probes == 0
        assert len(r2.deleted) == 1
        first_probe_key = r2.deleted[0]
        assert first_probe_key.startswith(worker_mod._HEALTHCHECK_R2_PREFIX)
        assert first_probe_key.endswith(".pdf")
        assert "/" not in first_probe_key.removeprefix(
            worker_mod._HEALTHCHECK_R2_PREFIX
        )
        assert first_probe_key != KEY
        assert supabase.tables["pdf_artifact_cleanup_queue"] == [_row()]
        assert supabase.events == []
        assert supabase.observed_limits == []
        assert len(supabase.rpc_calls) == 1
        rpc_name, params = supabase.rpc_calls[0]
        assert rpc_name == "claim_pdf_artifact_cleanup"
        assert params["p_force"] is False
        assert params["p_cleanup_id"] != CLEANUP
        assert params["p_owner_id"] != OWNER
        assert params["p_claim_token"] not in {
            params["p_cleanup_id"],
            params["p_owner_id"],
        }

        worker.probe()

        assert len(r2.deleted) == 2
        assert r2.deleted[1] != first_probe_key
        assert supabase.tables["pdf_artifact_cleanup_queue"] == [_row()]

    def test_probe_normalizes_provider_and_database_failures(self):
        worker, supabase, r2 = _worker([])
        r2.delete_object = lambda **_kwargs: (_ for _ in ()).throw(
            OSError("r2-secret-do-not-log")
        )
        with pytest.raises(worker_mod.CleanupExternalError):
            worker.probe()

        worker, supabase, _r2 = _worker([])
        supabase.fail_all_queries = True
        with pytest.raises(worker_mod.CleanupDatabaseError):
            worker.probe()

    def test_never_used_worker_is_noop_only_while_artifact_tables_are_empty(self):
        supabase = _Supabase([])
        worker = worker_mod.NeverUsedPdfCleanupWorker(supabase=supabase)

        worker.probe()
        assert worker.run_once() == worker_mod.CleanupCycle(0, 0, 0, 0, 0, 0, {})
        assert supabase.rpc_calls == []

        supabase.tables["pdf_artifacts"] = [{"id": CLEANUP}]
        with pytest.raises(worker_mod.CleanupConfigurationError):
            worker.probe()

    def test_never_used_build_path_needs_no_r2_credentials(
        self,
        monkeypatch,
    ):
        supabase = _Supabase([])
        monkeypatch.setitem(
            sys.modules,
            "supabase",
            SimpleNamespace(create_client=lambda *_args: supabase),
        )

        worker, poll_seconds = worker_mod._build_worker(
            {
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "service-key",
                "ONEPAGER_R2_NEVER_USED": "1",
            }
        )

        assert isinstance(worker, worker_mod.NeverUsedPdfCleanupWorker)
        assert poll_seconds == 60

    def test_never_used_build_path_rejects_r2_conflict(
        self,
        monkeypatch,
    ):
        monkeypatch.setitem(
            sys.modules,
            "supabase",
            SimpleNamespace(create_client=lambda *_args: _Supabase([])),
        )
        with pytest.raises(worker_mod.CleanupConfigurationError):
            worker_mod._build_worker(
                {
                    "SUPABASE_URL": "https://project.supabase.co",
                    "SUPABASE_SERVICE_ROLE_KEY": "service-key",
                    "ONEPAGER_R2_NEVER_USED": "1",
                    "R2_ACCOUNT_ID": "a" * 32,
                }
            )

    def test_probe_mode_does_not_enter_recurring_loop(self, monkeypatch):
        class _Worker:
            def run_once(self):
                raise AssertionError("probe entered the mutation loop")

        monkeypatch.setattr(
            worker_mod,
            "_build_worker",
            lambda _environ: (_Worker(), 60),
        )

        assert worker_mod.main(["--probe"], {}) == 0

    def test_default_mode_runs_recurring_cycle_until_signal_event(self, monkeypatch):
        cycles: list[int] = []

        class _Worker:
            def run_once(self):
                cycles.append(1)
                return worker_mod.CleanupCycle(0, 0, 0, 0, 0, 0, {})

        class _Stop:
            def __init__(self):
                self.stopped = False

            def is_set(self):
                return self.stopped

            def set(self):
                self.stopped = True

            def wait(self, _seconds: int):
                self.stopped = True

        monkeypatch.setattr(
            worker_mod,
            "_build_worker",
            lambda _environ: (_Worker(), 60),
        )
        monkeypatch.setattr(worker_mod.threading, "Event", _Stop)
        monkeypatch.setattr(worker_mod.signal, "signal", lambda *_args: None)

        assert worker_mod.main([], {}) == 0
        assert cycles == [1]


class TestConfigurationAndLogging:
    def test_partial_or_absent_r2_configuration_is_rejected(self):
        with pytest.raises(worker_mod.CleanupConfigurationError):
            worker_mod._r2_config({})
        with pytest.raises(worker_mod.CleanupConfigurationError):
            worker_mod._r2_config({"R2_ACCOUNT_ID": "a" * 32})

    @pytest.mark.parametrize(
        "account_id",
        ["a" * 31, "g" * 32, "a" * 31 + "/", "https://host.invalid"],
    )
    def test_account_id_cannot_change_r2_endpoint(self, account_id: str):
        with pytest.raises(worker_mod.CleanupConfigurationError):
            worker_mod._r2_config(
                {
                    "R2_ACCOUNT_ID": account_id,
                    "R2_ACCESS_KEY_ID": "access",
                    "R2_SECRET_ACCESS_KEY": "secret",
                    "R2_BUCKET": "bucket",
                }
            )

    def test_batch_and_poll_controls_are_bounded(self):
        for name, minimum, maximum in (
            ("ONEPAGER_PDF_CLEANUP_BATCH_SIZE", 1, 100),
            ("ONEPAGER_PDF_CLEANUP_POLL_SECONDS", 30, 86400),
        ):
            with pytest.raises(worker_mod.CleanupConfigurationError):
                worker_mod._bounded_int(
                    {name: str(minimum - 1)},
                    name,
                    default=minimum,
                    minimum=minimum,
                    maximum=maximum,
                )
            with pytest.raises(worker_mod.CleanupConfigurationError):
                worker_mod._bounded_int(
                    {name: str(maximum + 1)},
                    name,
                    default=minimum,
                    minimum=minimum,
                    maximum=maximum,
                )

    def test_cycle_log_is_aggregate_only(self, caplog: pytest.LogCaptureFixture):
        caplog.set_level(logging.INFO, logger="pdf_cleanup_worker")
        cycle = worker_mod.CleanupCycle(
            selected=2,
            completed=1,
            deferred=1,
            r2_objects=1,
            failure_updates=1,
            failure_update_errors=0,
            error_types={"CleanupExternalError": 1},
        )

        worker_mod._log_cycle(cycle)

        text = caplog.text
        assert "selected=2" in text
        assert "CleanupExternalError:1" in text
        for sensitive in (
            OWNER,
            DOCUMENT,
            CLEANUP,
            KEY,
            "r2-secret-do-not-log",
            "database-token-do-not-log",
        ):
            assert sensitive not in text

    def test_startup_error_log_never_contains_exception_message(
        self,
        monkeypatch,
        caplog: pytest.LogCaptureFixture,
    ):
        secret = "service-role-secret-do-not-log"
        monkeypatch.setattr(
            worker_mod,
            "_build_worker",
            lambda _environ: (_ for _ in ()).throw(RuntimeError(secret)),
        )
        caplog.set_level(logging.CRITICAL, logger="pdf_cleanup_worker")

        assert worker_mod.main([], {}) == 2
        assert "RuntimeError" in caplog.text
        assert secret not in caplog.text


class TestReadPathDrainIsThrottled:
    """The in-request outbox drain must not scale with library read traffic.

    The dedicated worker owns the durable queue; the drain inside
    ``GET /api/cvs`` and ``GET /api/cover-letters`` is only the fallback for
    deployments that do not run it. Unthrottled, every list request could block
    on up to ten sequential R2 deletes, putting object-store latency directly on
    the user's library view.
    """

    def _server(self, monkeypatch):
        import server as server_mod

        monkeypatch.setattr(server_mod, "_last_pending_cleanup_at", 0.0)
        monkeypatch.setattr(server_mod, "_artifact_cleanup_applies", lambda: True)
        return server_mod

    def test_batch_is_small_and_drain_is_rate_limited(self, monkeypatch):
        server_mod = self._server(monkeypatch)
        calls: list[int] = []

        def _drain(*, limit: int) -> int:
            calls.append(limit)
            return 0

        monkeypatch.setattr(
            server_mod.pdf_store, "retry_pending_artifact_cleanup", _drain
        )
        clock = {"now": 1_000.0}
        monkeypatch.setattr(server_mod.time, "monotonic", lambda: clock["now"])

        server_mod._retry_pending_pdf_artifacts_best_effort()
        server_mod._retry_pending_pdf_artifacts_best_effort()
        server_mod._retry_pending_pdf_artifacts_best_effort()
        assert calls == [server_mod._PENDING_CLEANUP_BATCH], calls
        assert server_mod._PENDING_CLEANUP_BATCH <= 3

        clock["now"] += server_mod._PENDING_CLEANUP_MIN_INTERVAL_S + 1
        server_mod._retry_pending_pdf_artifacts_best_effort()
        assert len(calls) == 2, calls

    def test_a_failing_backend_does_not_retry_on_every_read(self, monkeypatch):
        """The throttle stamps before the attempt, not after a success."""
        server_mod = self._server(monkeypatch)
        calls: list[int] = []

        def _boom(*, limit: int) -> int:
            calls.append(limit)
            raise RuntimeError("object store unavailable")

        monkeypatch.setattr(
            server_mod.pdf_store, "retry_pending_artifact_cleanup", _boom
        )
        monkeypatch.setattr(server_mod.time, "monotonic", lambda: 2_000.0)

        for _ in range(5):
            assert server_mod._retry_pending_pdf_artifacts_best_effort() == 0
        assert len(calls) == 1, calls

    def test_demo_shell_never_drains(self, monkeypatch):
        server_mod = self._server(monkeypatch)
        monkeypatch.setattr(server_mod, "_artifact_cleanup_applies", lambda: False)

        def _never(**_kwargs):
            raise AssertionError("demo shell reached the durable cleanup queue")

        monkeypatch.setattr(
            server_mod.pdf_store, "retry_pending_artifact_cleanup", _never
        )
        assert server_mod._retry_pending_pdf_artifacts_best_effort() == 0
