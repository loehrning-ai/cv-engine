"""Ephemeral in-memory storage for local demo-mode browser QA.

When the editor runs with ``ONEPAGER_DEMO_MODE`` and no real Supabase project
(``SUPABASE_URL`` unset), authenticated local-demo requests would otherwise
500 on every write because ``auth.user_supabase()`` cannot build a client.
This module provides a process-local stand-in that implements just the
PostgREST query surface the services use, so the jobs repository, the
multi-CV document routes, and the downstream saved-job flows stay usable in
the local shell. The seeded primary demo CV also lives here (``documents``
table, id ``LOCAL_DEMO_CV_ID`` in server.py) so created CVs and the primary
CV share one consistent model.

It is intentionally NOT a database: no durability, no cross-process state,
single tenant (the local demo owner). Production and tests with a configured
Supabase never touch it — see :func:`active`.
"""
from __future__ import annotations

import datetime as _dt
import itertools
import threading
import uuid
from contextlib import contextmanager
from typing import Any

from flask import g, has_app_context

import auth as auth_mod

_ts_counter = itertools.count(1)
_STORE_LOCK = threading.RLock()


def _next_ts() -> str:
    base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    return (base + _dt.timedelta(seconds=next(_ts_counter))).isoformat()


class _Resp:
    def __init__(self, data: list[dict]):
        self.data = data


class _Query:
    """One fluent query against a table: filters accumulate, execute applies."""

    def __init__(self, table: "_Table", op: str, payload: Any = None):
        self._t = table
        self._op = op
        self._payload = payload
        self._select: str | None = None
        self._eq: dict[str, Any] = {}
        self._in: dict[str, list] = {}
        self._is_null: list[str] = []
        self._order: list[tuple[str, bool]] = []
        self._limit: int | None = None
        self._range: tuple[int, int] | None = None

    def select(self, columns: str = "*", **_k) -> "_Query":
        self._select = columns
        return self

    def eq(self, column: str, value: Any) -> "_Query":
        self._eq[column] = value
        return self

    def in_(self, column: str, values: list) -> "_Query":
        self._in[column] = list(values)
        return self

    def is_(self, column: str, value: Any) -> "_Query":
        # jobs_service only uses `.is_(col, "null")`.
        if value in ("null", None):
            self._is_null.append(column)
        return self

    def order(self, column: str, desc: bool = False, **_k) -> "_Query":
        self._order.append((column, desc))
        return self

    def limit(self, count: int) -> "_Query":
        self._limit = count
        return self

    def range(self, start: int, end: int, **_k) -> "_Query":
        self._range = (int(start), int(end))
        return self

    def _matches(self, row: dict) -> bool:
        if not all(row.get(k) == v for k, v in self._eq.items()):
            return False
        if not all(row.get(c) in vals for c, vals in self._in.items()):
            return False
        return all(row.get(c) is None for c in self._is_null)

    def execute(self) -> _Resp:
        with _STORE_LOCK:
            if self._op == "select":
                rows = [dict(r) for r in self._t.rows if self._matches(r)]
                for column, desc in reversed(self._order):
                    rows.sort(key=lambda r: (r.get(column) is None, r.get(column) or ""), reverse=desc)
                if self._range is not None:
                    start, end = self._range
                    rows = rows[start : end + 1]
                if self._limit is not None:
                    rows = rows[: self._limit]
                return _Resp(rows)
            if self._op == "insert":
                payloads = self._payload if isinstance(self._payload, list) else [self._payload]
                inserted = []
                for item in payloads:
                    row = dict(item)
                    row.setdefault("id", str(uuid.uuid4()))
                    row.setdefault("owner_id", self._t.store.owner_id)
                    row.setdefault("created_at", _next_ts())
                    row.setdefault("updated_at", row["created_at"])
                    self._t.rows.append(row)
                    inserted.append(dict(row))
                return _Resp(inserted)
            if self._op == "update":
                updated = []
                for row in self._t.rows:
                    if self._matches(row):
                        row.update(self._payload)
                        # Honour an explicit updated_at (server.py stamps one so the
                        # response matches the stored row — the CV save revision
                        # check depends on it); otherwise bump like the DB would.
                        row["updated_at"] = self._payload.get("updated_at") or _next_ts()
                        updated.append(dict(row))
                return _Resp(updated)
            if self._op == "delete":
                kept, removed = [], []
                for row in self._t.rows:
                    (removed if self._matches(row) else kept).append(row)
                self._t.rows[:] = kept
                return _Resp(removed)
            raise RuntimeError(f"local demo store: unsupported op {self._op}")


class _Table:
    def __init__(self, store: "LocalDemoDb", name: str):
        self.store = store
        self.rows = store.tables.setdefault(name, [])

    def select(self, columns: str = "*", **k) -> _Query:
        return _Query(self, "select").select(columns, **k)

    def insert(self, payload: Any) -> _Query:
        return _Query(self, "insert", payload)

    def update(self, payload: dict) -> _Query:
        return _Query(self, "update", payload)

    def delete(self) -> _Query:
        return _Query(self, "delete")


class LocalDemoDb:
    """Single-tenant in-memory stand-in for the demo owner's job tables."""

    def __init__(self, owner_id: str):
        self.owner_id = owner_id
        self.tables: dict[str, list[dict]] = {}

    def table(self, name: str) -> _Table:
        return _Table(self, name)


_DB: LocalDemoDb | None = None


def active() -> bool:
    """True only for requests authenticated as the local demo owner.

    A demo session's ``alg:none`` token can never authorize against a real
    Supabase project (JWKS verification would reject it), so routing the demo
    owner to PostgREST is always wrong — with a configured-but-unreachable
    ``SUPABASE_URL`` it even 500s on every write (plan 107, F14). Demo mode
    must be on (local tokens only verify then, auth.py) and the identity must
    be the reserved demo-owner sentinel; every real tenant keeps the real,
    failing-loudly Supabase path.
    """
    if not auth_mod._demo_mode_enabled():
        return False
    if not has_app_context():
        return False
    return getattr(g, "owner_id", None) == auth_mod.LOCAL_DEMO_OWNER_ID


@contextmanager
def locked():
    """Serialize compound operations against the process-local demo store."""
    with _STORE_LOCK:
        yield


def db() -> LocalDemoDb:
    global _DB
    with _STORE_LOCK:
        if _DB is None:
            _DB = LocalDemoDb(auth_mod.LOCAL_DEMO_OWNER_ID)
        return _DB


def reset() -> None:
    """Drop all stored rows (test isolation helper)."""
    global _DB
    with _STORE_LOCK:
        _DB = None


# The seeded primary demo CV is materialized into this store's ``documents``
# table by server.py (id ``LOCAL_DEMO_CV_ID``) so the multi-CV routes and the
# GDPR export see one consistent model. Service-layer reads (tailor/ats-score
# loading a source CV via ``cover_letter_service.document_by_id``) need the
# same row without a circular import, so server.py registers a provider at
# boot (plan 107, F19).
_DEMO_CV_PROVIDER = None


def register_demo_cv_provider(provider) -> None:
    global _DEMO_CV_PROVIDER
    _DEMO_CV_PROVIDER = provider


def demo_cv_row() -> dict | None:
    if _DEMO_CV_PROVIDER is None:
        return None
    try:
        return _DEMO_CV_PROVIDER()
    except Exception:
        return None
