"""Shared pytest fixtures for the editor gateway tests.

Two kinds of fixtures live here:

  * ``bypass_auth`` — replaces the real Supabase-JWT guard with a stub that
    injects a fixed fake identity onto ``g``. This lets the auth-INDEPENDENT
    route tests (photo upload, theme import, polish validation, repo safety)
    run without a live token. The real guard is still what production uses;
    we only swap it in-process for these unit tests.

  * ``fake_supabase`` — an in-memory stand-in for the per-user supabase-py
    client so DB-backed routes (/api/cv, /api/cvs, /api/preview, /api/build)
    can be unit-tested without network. Tenant isolation against the REAL
    project + RLS is covered separately in test_stage2_auth.py.

The genuinely cross-tenant / RLS assertions deliberately do NOT use these
stubs — they run against the real Supabase project with real user JWTs.
"""
from __future__ import annotations

import itertools
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))


# Monotonic counter so inserted rows get strictly increasing timestamps —
# lets the fake honour ``order("updated_at", desc=True)`` deterministically.
_TS = itertools.count(1)


def _next_ts() -> int:
    return next(_TS)


# ---------------------------------------------------------------------------
# In-memory fake of the supabase-py client (only the chain server.py uses).
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, table: "_Table", op: str, payload=None):
        self._t = table
        self._op = op
        self._payload = payload
        self._eq: dict[str, object] = {}
        self._in: dict[str, set] = {}
        self._is_null: set[str] = set()
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None

    # Filter methods are chainable; select() is a no-op (columns ignored).
    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def in_(self, col, vals, *_a, **_k):
        # Honour `.in_("job_id", [ids])` so the grouped has_cover query the jobs
        # catalog uses (RLS-scoped) filters correctly in-memory.
        self._in[col] = set(vals or [])
        return self

    def is_(self, col, val, *_a, **_k):
        # Honour `is_("deleted_at", "null")` so soft-deleted rows are excluded
        # exactly as RLS+PostgREST would (the prod list/get queries rely on it).
        if str(val).lower() == "null":
            self._is_null.add(col)
        return self

    def order(self, col, desc=False, **_k):
        self._order = (col, bool(desc))
        return self

    def limit(self, n, *_a, **_k):
        self._limit = n
        return self

    def execute(self) -> _Resp:
        if self._op == "_noop_select_existing":
            # upsert-updated an existing row: payload IS the returned data.
            return _Resp(self._payload)
        if self._op == "select":
            rows = [r for r in self._t.rows if self._matches(r)]
            if self._order is not None:
                col, desc = self._order
                rows = sorted(
                    rows, key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc
                )
            if self._limit is not None:
                rows = rows[: self._limit]
            return _Resp(rows)
        if self._op == "insert":
            row = dict(self._payload)
            row.setdefault("id", str(uuid.uuid4()))
            row.setdefault("owner_id", self._t.store.owner_id)
            row.setdefault("deleted_at", None)
            # Stamp timestamps so list-ordering by updated_at has a value.
            row.setdefault("created_at", _next_ts())
            row.setdefault("updated_at", row["created_at"])
            row.setdefault("last_used_at", row["updated_at"])
            self._t.rows.append(row)
            return _Resp([row])
        if self._op == "update":
            updated = []
            for r in self._t.rows:
                if self._matches(r):
                    r.update(self._payload)
                    updated.append(r)
            return _Resp(updated)
        if self._op == "delete":
            kept, removed = [], []
            for r in self._t.rows:
                (removed if self._matches(r) else kept).append(r)
            # Mutate the shared list in place so the table keeps the same ref.
            self._t.rows[:] = kept
            return _Resp(removed)
        raise AssertionError(f"unsupported op {self._op}")

    def _matches(self, row: dict) -> bool:
        if not all(row.get(k) == v for k, v in self._eq.items()):
            return False
        # `.in_(col, vals)` filters: the row's value must be in the set.
        if not all(row.get(c) in vals for c, vals in self._in.items()):
            return False
        # `is null` filters: the column must be absent or None on the row.
        return all(row.get(c) is None for c in self._is_null)


class _Table:
    def __init__(self, store: "FakeSupabase", name: str):
        self.store = store
        self.name = name
        self.rows = store.tables.setdefault(name, [])

    def select(self, *a, **k):
        return _Query(self, "select").select(*a, **k)

    def insert(self, payload):
        return _Query(self, "insert", payload)

    def upsert(self, payload, on_conflict=None, **_k):
        # Emulate Postgres ON CONFLICT (owner_id, provider) DO UPDATE: replace
        # the matching row in place if one exists, else insert. Good enough
        # for the user_llm_keys (owner_id, provider) unique used by save_key.
        conflict_cols = [c.strip() for c in (on_conflict or "").split(",") if c.strip()]
        if conflict_cols:
            row = dict(payload)
            row.setdefault("owner_id", self.store.owner_id)
            for existing in self.rows:
                if all(existing.get(c) == row.get(c) for c in conflict_cols):
                    existing.update(payload)
                    return _Query(self, "_noop_select_existing", [existing])
        return _Query(self, "insert", payload)

    def update(self, payload):
        return _Query(self, "update", payload)

    def delete(self):
        return _Query(self, "delete")


class _Postgrest:
    def __init__(self):
        self.token = None

    def auth(self, token):
        self.token = token


class _Rpc:
    """A single ``.rpc(name, params).execute()`` call against the fake."""

    def __init__(self, store: "FakeSupabase", name: str, params: dict):
        self._store = store
        self._name = name
        self._params = params or {}

    def execute(self) -> _Resp:
        if self._name == "consume_managed_credit":
            return _Resp(self._store._consume_managed_credit(self._params.get("p_kind")))
        if self._name == "record_byo_usage":
            return _Resp(
                self._store._record_byo_usage(
                    self._params.get("p_kind"),
                    self._params.get("p_provider"),
                )
            )
        if self._name == "record_application_apply_attempt":
            return _Resp(self._store._record_application_apply_attempt(self._params))
        if self._name == "request_account_deletion":
            return _Resp(self._store._request_account_deletion())
        if self._name == "cancel_account_deletion":
            return _Resp(self._store._cancel_account_deletion())
        if self._name == "mark_first_cv_seen":
            return _Resp(self._store._mark_first_cv_seen())
        if self._name == "seed_first_cv_template":
            return _Resp(self._store._seed_first_cv_template(self._params))
        raise AssertionError(f"unsupported rpc {self._name}")


class FakeSupabase:
    """Minimal in-memory client; one instance == one tenant's view."""

    def __init__(self, owner_id: str):
        self.owner_id = owner_id
        self.tables: dict[str, list] = {}
        self.postgrest = _Postgrest()

    def table(self, name: str) -> _Table:
        return _Table(self, name)

    def rpc(self, name: str, params: dict | None = None) -> _Rpc:
        return _Rpc(self, name, params or {})

    def _consume_managed_credit(self, kind) -> bool:
        """Faithful emulation of the SECURITY DEFINER credit RPC.

        Decrements this tenant's profile balance when > 0, writes the
        ``used_managed_credit=true`` usage_events audit row, and returns True;
        otherwise returns False without side effects. Lets the credit-
        exhaustion test see real "3 then blocked" behaviour.
        """
        profiles = self.tables.setdefault("profiles", [])
        row = next((r for r in profiles if r.get("id") == self.owner_id), None)
        if kind not in {
            "cv_tailor",
            "ats_score",
            "cover_letter_generate",
            "polish_bullet",
            "polish_section",
            "extract",
        }:
            raise ValueError("unsupported usage kind")
        if (
            row is None
            or row.get("deletion_requested_at") is not None
            or (row.get("managed_credits_remaining") or 0) <= 0
        ):
            return False
        row["managed_credits_remaining"] -= 1
        self.tables.setdefault("usage_events", []).append(
            {
                "owner_id": self.owner_id,
                "kind": kind,
                "used_managed_credit": True,
            }
        )
        return True

    def _record_byo_usage(self, kind, provider) -> bool:
        if kind not in {
            "cv_tailor",
            "ats_score",
            "cover_letter_generate",
            "polish_bullet",
            "polish_section",
            "extract",
        }:
            raise ValueError("unsupported usage kind")
        if provider not in {
            "anthropic",
            "gemini",
            "ollama",
            "openrouter",
            "deepseek",
            "groq",
            "mistral",
            "openai_compatible",
        }:
            raise ValueError("unsupported usage provider")
        profiles = self.tables.setdefault("profiles", [])
        row = next((r for r in profiles if r.get("id") == self.owner_id), None)
        if row is None or row.get("deletion_requested_at") is not None:
            raise ValueError("account is unavailable")
        self.tables.setdefault("usage_events", []).append(
            {
                "id": str(uuid.uuid4()),
                "owner_id": self.owner_id,
                "kind": kind,
                "used_managed_credit": False,
                "provider": provider,
                "created_at": _next_ts(),
            }
        )
        return True

    def _record_application_apply_attempt(self, params: dict) -> dict:
        row = self._profile_row()
        if row.get("deletion_requested_at") is not None:
            raise ValueError("account is unavailable")
        job_id = params.get("p_job_id")
        packet_id = params.get("p_packet_id")
        if job_id is not None and not any(
            job.get("id") == job_id
            and job.get("owner_id", self.owner_id) == self.owner_id
            for job in self.tables.setdefault("saved_jobs", [])
        ):
            raise ValueError("application job is unavailable")
        if packet_id is not None and not any(
            packet.get("id") == packet_id
            and packet.get("owner_id", self.owner_id) == self.owner_id
            and packet.get("job_id") == job_id
            for packet in self.tables.setdefault("application_packets", [])
        ):
            raise ValueError("application packet is unavailable")
        event_at = _next_ts()
        attempt = {
            "id": str(uuid.uuid4()),
            "owner_id": self.owner_id,
            "job_id": job_id,
            "packet_id": packet_id,
            "action": params.get("p_action"),
            "status": params.get("p_status"),
            "reason_code": params.get("p_reason_code"),
            "target_url": params.get("p_target_url"),
            "target_host": params.get("p_target_host") or "",
            "target_classification": params.get("p_target_classification"),
            "submitted": False,
            "automation_performed": False,
            "readiness": params.get("p_readiness") or {},
            "confirmations": params.get("p_confirmations") or {},
            "metadata": params.get("p_metadata") or {},
            "created_at": event_at,
            "updated_at": event_at,
        }
        self.tables.setdefault("application_apply_attempts", []).append(attempt)
        return attempt

    def _profile_row(self) -> dict:
        profiles = self.tables.setdefault("profiles", [])
        row = next((r for r in profiles if r.get("id") == self.owner_id), None)
        if row is None:
            row = {"id": self.owner_id, "updated_at": _next_ts()}
            profiles.append(row)
        return row

    def _mark_first_cv_seen(self):
        row = self._profile_row()
        if row.get("first_cv_seeded_at") is None:
            row["first_cv_seeded_at"] = _next_ts()
        row["updated_at"] = _next_ts()
        return row["first_cv_seeded_at"]

    def _seed_first_cv_template(self, params: dict):
        row = self._profile_row()
        if row.get("deletion_requested_at") is not None:
            return None
        docs = self.tables.setdefault("documents", [])
        has_live_cv = any(
            doc.get("owner_id") == self.owner_id
            and doc.get("type") == "cv"
            and doc.get("deleted_at") is None
            for doc in docs
        )
        if has_live_cv:
            self._mark_first_cv_seen()
            return None
        if row.get("first_cv_seeded_at") is not None:
            return None
        doc = {
            "id": str(uuid.uuid4()),
            "owner_id": self.owner_id,
            "type": "cv",
            "name": params.get("p_name") or "Template CV - John Doe",
            "yaml_blob": params.get("p_yaml_blob") or "",
            "accent": params.get("p_accent"),
            "font": params.get("p_font"),
            "density": params.get("p_density"),
            "deleted_at": None,
            "created_at": _next_ts(),
        }
        doc["updated_at"] = doc["created_at"]
        docs.append(doc)
        row["first_cv_seeded_at"] = _next_ts()
        row["updated_at"] = _next_ts()
        return doc

    def _request_account_deletion(self):
        """Emulate the SECURITY DEFINER erasure RPC (phase 1, soft-delete).

        Stamps the caller's profile ``deletion_requested_at`` and soft-deletes
        their not-already-deleted documents — exactly what the SQL function
        does — so the route + endpoint-disappearance tests are faithful.
        Returns the request timestamp (mirrors the SQL return).
        """
        requested_at = _next_ts()
        profiles = self.tables.setdefault("profiles", [])
        row = next((r for r in profiles if r.get("id") == self.owner_id), None)
        if row is not None:
            row["deletion_requested_at"] = requested_at
        for doc in self.tables.setdefault("documents", []):
            if doc.get("owner_id") == self.owner_id and doc.get("deleted_at") is None:
                doc["deleted_at"] = requested_at
        return requested_at

    def _cancel_account_deletion(self) -> bool:
        """Emulate the cancel RPC: clear the flag + restore soft-deleted docs."""
        profiles = self.tables.setdefault("profiles", [])
        row = next((r for r in profiles if r.get("id") == self.owner_id), None)
        requested_at = row.get("deletion_requested_at") if row else None
        if requested_at is None:
            return False
        for doc in self.tables.setdefault("documents", []):
            if (
                doc.get("owner_id") == self.owner_id
                and doc.get("deleted_at") is not None
                and doc["deleted_at"] >= requested_at
            ):
                doc["deleted_at"] = None
        row["deletion_requested_at"] = None
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_ai_rate_limiter():
    """Clear the in-memory AI rate-limit counters before each test.

    The per-user AI limit ("20 per minute", keyed by owner_id) uses an in-process
    ``memory://`` store. Every fake_supabase test shares the SAME stub owner, so
    AI-route calls (cover-letter / tailor / generate) otherwise ACCUMULATE across
    tests within the 60s window and eventually trip a spurious 429. Resetting the
    limiter storage per test restores isolation without weakening the real limit
    (the dedicated rate-limit test re-exercises it explicitly).
    """
    try:
        import server  # noqa: PLC0415

        if getattr(server, "limiter", None) is not None:
            try:
                server.limiter.reset()
            except Exception:  # noqa: BLE001 — best-effort; storage may be lazy
                store = getattr(server.limiter, "storage", None)
                if store is not None and hasattr(store, "reset"):
                    store.reset()
    except Exception:  # noqa: BLE001 — server import failure surfaces elsewhere
        pass
    yield


@pytest.fixture(autouse=True)
def _demo_mode_off_by_default(monkeypatch):
    """Pin ``ONEPAGER_DEMO_MODE`` OFF for every test unless it opts in.

    Demo mode is a DEV-ONLY flag that bypasses the real LLM key/credit gating
    (see engine/llm/client.py). The whole pre-existing suite asserts the REAL
    gating, so an ambient ``ONEPAGER_DEMO_MODE=1`` (e.g. the container launched
    with the demo flag) would otherwise flip those assertions. Defaulting it OFF
    here keeps the suite deterministic regardless of how it is launched; the
    demo-mode tests re-enable it explicitly with ``monkeypatch.setenv`` per test.
    """
    monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)


@pytest.fixture(autouse=True)
def _review_token_secret_for_tests(monkeypatch):
    """Run Tailor review/apply tests with an explicit signing secret."""
    monkeypatch.setenv("ONEPAGER_REVIEW_TOKEN_SECRET", "onepager-test-review-secret")


@pytest.fixture
def server_module():
    import importlib

    return importlib.import_module("server")


@pytest.fixture
def bypass_auth(server_module, monkeypatch):
    """Swap the JWT guard for a stub that injects a fake identity on g."""
    from flask import g

    import auth as auth_mod

    fake_owner = "00000000-0000-0000-0000-000000000001"

    def _stub() -> object:
        # Mirror auth.require_auth's exemptions so the shape matches prod.
        from flask import request

        if request.method == "OPTIONS":
            return None
        path = request.path
        exempt_paths = {
            "/", "/app", "/login", "/cover-letters", "/tailor", "/jobs",
            "/account", "/privacy",
            "/healthz", "/favicon.ico", "/auth/config", "/auth/demo-login",
            "/robots.txt", "/sitemap.xml", "/api/i18n", "/api/themes",
            "/api/templates", "/api/ats-profiles",
        }
        exempt_prefixes = ("/static/", "/asset/", "/repo/")
        if path in exempt_paths or path.startswith(exempt_prefixes):
            return None
        g.owner_id = fake_owner
        g.email = "stub@onepager.test"
        g.access_token = "stub-token"
        return None

    monkeypatch.setattr(auth_mod, "require_auth", _stub)
    return fake_owner


@pytest.fixture
def fake_supabase(server_module, monkeypatch, bypass_auth):
    """Bind a shared in-memory FakeSupabase to the stubbed identity."""
    import auth as auth_mod

    store = FakeSupabase(owner_id=bypass_auth)
    monkeypatch.setattr(auth_mod, "user_supabase", lambda: store)
    return store


@pytest.fixture
def client(server_module):
    server_module.app.config.update(TESTING=True)
    return server_module.app.test_client()
