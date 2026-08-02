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

import hashlib
import itertools
import json
import re
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


_SAFE_JOB_SOURCE_METADATA_KEYS = {
    "terms_basis",
    "manual_verification_required",
    "crawl_policy",
    "origin",
    "site",
    "date_posted",
    "first_seen",
    "normalized_role",
    "source_db",
    "priority_rank",
    "rank_bucket",
    "rank_badge",
    "fit_note",
    "risk_note",
    "source_artifact",
    "verified_at",
    "revalidation",
    "full_jd_status",
    "source_links",
    "job_family",
    "source_city",
    "search_criteria",
    "match_reasons",
    "broader_match",
    "ranking",
    "starred",
}
_COMPLEX_JOB_SOURCE_METADATA_KEYS = {
    "source_links",
    "search_criteria",
    "match_reasons",
    "ranking",
}
_JOB_SOURCE_METADATA_SECRET_RE = re.compile(
    r"(?:li_at|jsessionid|csrf|session[_-]?token|access[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|authorization|cookie|password|"
    r"secret|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|"
    r"bearer)[\s_\"-]*[:=]|"
    r"bearer\s+[A-Za-z0-9._~+/=-]+|"
    r"sk-(?:ant|proj|live|test|or|gsk|mistral|[A-Za-z0-9])[A-Za-z0-9_-]{8,}|"
    r"AIza[0-9A-Za-z_-]{20,}|ya29\.[0-9A-Za-z_-]{20,}",
    re.IGNORECASE,
)


def _sanitize_saved_job_source_metadata(value: object) -> dict:
    """Mirror the database allowlist and nested secret-pattern rejection."""
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, object] = {}
    for key, item in value.items():
        if key not in _SAFE_JOB_SOURCE_METADATA_KEYS:
            continue
        try:
            serialized = json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError):
            continue
        if (
            len(serialized.encode("utf-8")) > 65_536
            or _JOB_SOURCE_METADATA_SECRET_RE.search(serialized)
        ):
            continue
        if key in _COMPLEX_JOB_SOURCE_METADATA_KEYS:
            if isinstance(item, (dict, list, str, int, float, bool)):
                sanitized[key] = json.loads(serialized)
            continue
        if isinstance(item, str):
            if key == "source_db" and (
                "/" in item or "\\" in item or item.startswith(".")
            ):
                continue
            sanitized[key] = (
                re.split(r"[\\/]", item)[-1]
                if key == "source_artifact"
                else item[:1000]
            )
        elif isinstance(item, (int, float, bool)):
            sanitized[key] = item
    return sanitized


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
        self._lte: dict[str, object] = {}
        self._orders: list[tuple[str, bool]] = []
        self._limit: int | None = None
        self._range: tuple[int, int] | None = None

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

    def lte(self, col, val, *_a, **_k):
        self._lte[col] = val
        return self

    def order(self, col, desc=False, **_k):
        self._orders.append((col, bool(desc)))
        return self

    def limit(self, n, *_a, **_k):
        self._limit = n
        return self

    def range(self, start, end, *_a, **_k):
        self._range = (int(start), int(end))
        return self

    def execute(self) -> _Resp:
        if self._op == "_noop_select_existing":
            # upsert-updated an existing row: payload IS the returned data.
            return _Resp(self._payload)
        if self._op == "select":
            rows = [r for r in self._t.rows if self._matches(r)]
            # A real authenticated user has a profile from the auth-user
            # trigger. Keep that invariant virtual until a test explicitly
            # writes profile state, without creating a row as a read side effect.
            if (
                not rows
                and self._t.name == "profiles"
                and self._eq.get("id") == self._t.store.owner_id
            ):
                rows = [
                    {
                        "id": self._t.store.owner_id,
                        "deletion_requested_at": None,
                    }
                ]
            for col, desc in reversed(self._orders):
                rows = sorted(
                    rows, key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc
                )
            if self._range is not None:
                start, end = self._range
                rows = rows[start : end + 1]
            if self._limit is not None:
                rows = rows[: self._limit]
            return _Resp(rows)
        if self._op == "insert":
            row = dict(self._payload)
            if self._t.name == "saved_jobs" and "source_metadata" in row:
                row["source_metadata"] = _sanitize_saved_job_source_metadata(
                    row.get("source_metadata")
                )
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
            payload = dict(self._payload)
            if self._t.name == "saved_jobs" and "source_metadata" in payload:
                payload["source_metadata"] = _sanitize_saved_job_source_metadata(
                    payload.get("source_metadata")
                )
            updated = []
            for r in self._t.rows:
                if self._matches(r):
                    r.update(payload)
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
        for column, maximum in self._lte.items():
            actual = row.get(column)
            if actual is None:
                return False
            try:
                if actual > maximum:
                    return False
            except TypeError:
                if str(actual) > str(maximum):
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
        if self._name == "finalize_application_apply_action":
            return _Resp(self._store._finalize_application_apply_action(self._params))
        if self._name == "replace_active_application_packet":
            return _Resp(self._store._replace_active_application_packet(self._params))
        if self._name == "save_import_result_as_job":
            return _Resp(self._store._save_import_result_as_job(self._params))
        if self._name == "set_saved_job_star":
            return _Resp(self._store._set_saved_job_star(self._params))
        if self._name == "revalidate_saved_job_source":
            return _Resp(self._store._revalidate_saved_job_source(self._params))
        if self._name == "confirm_application_packet_cover_review":
            return _Resp(
                self._store._confirm_application_packet_cover_review(self._params)
            )
        if self._name == "merge_incomplete_application_packet":
            return _Resp(self._store._merge_incomplete_application_packet(self._params))
        if self._name == "request_account_deletion":
            return _Resp(self._store._request_account_deletion())
        if self._name == "request_account_deletion_with_artifact_detach":
            return _Resp(self._store._request_account_deletion_with_artifact_detach())
        if self._name == "cancel_account_deletion":
            return _Resp(self._store._cancel_account_deletion())
        if self._name == "mark_first_cv_seen":
            return _Resp(self._store._mark_first_cv_seen())
        if self._name == "seed_first_cv_template":
            return _Resp(self._store._seed_first_cv_template(self._params))
        if self._name == "delete_saved_job_safely":
            return _Resp(self._store._delete_saved_job_safely(self._params))
        if self._name == "delete_document_safely":
            return _Resp(self._store._delete_document_safely(self._params))
        if self._name == "detach_pdf_artifacts":
            return _Resp(self._store._detach_pdf_artifacts(self._params))
        if self._name == "detach_owner_cv_pdf_artifacts":
            return _Resp(self._store._detach_owner_cv_pdf_artifacts(self._params))
        if self._name == "insert_document_if_account_active":
            return _Resp(self._store._insert_document_if_account_active(self._params))
        if self._name == "update_document_with_artifact_detach":
            return _Resp(self._store._update_document_with_artifact_detach(self._params))
        if self._name == "register_pdf_upload_candidate":
            return _Resp(self._store._register_pdf_upload_candidate(self._params))
        if self._name == "resolve_pdf_artifact":
            return _Resp(self._store._resolve_pdf_artifact(self._params))
        if self._name == "authorize_document_export":
            return _Resp(self._store._authorize_document_export(self._params))
        if self._name == "publish_pdf_artifact":
            return _Resp(self._store._publish_pdf_artifact(self._params))
        if self._name == "evict_broken_pdf_artifact":
            return _Resp(self._store._evict_broken_pdf_artifact(self._params))
        if self._name == "claim_pdf_artifact_cleanup":
            return _Resp(self._store._claim_pdf_artifact_cleanup(self._params))
        if self._name == "complete_pdf_artifact_cleanup":
            return _Resp(self._store._complete_pdf_artifact_cleanup(self._params))
        if self._name == "record_pdf_artifact_cleanup_failure":
            return _Resp(self._store._record_pdf_artifact_cleanup_failure(self._params))
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

    def _finalize_application_apply_action(self, params: dict) -> dict:
        """Emulate the final locked/CAS apply-evidence transaction."""
        if params.get("p_owner_id") != self.owner_id:
            return {"status": "blocked", "code": "account_unavailable"}
        profile = self._profile_row()
        if profile.get("deletion_requested_at") is not None:
            return {"status": "blocked", "code": "account_unavailable"}

        expected = params.get("p_expected") or {}
        job_id = params.get("p_job_id")
        packet_id = params.get("p_packet_id")
        job = next(
            (
                row for row in self.tables.setdefault("saved_jobs", [])
                if row.get("id") == job_id
                and row.get("owner_id", self.owner_id) == self.owner_id
            ),
            None,
        )
        if job is None:
            return {"status": "blocked", "code": "job_unavailable"}
        job_hash = hashlib.sha256(
            str(job.get("jd_text") or "").encode("utf-8")
        ).hexdigest()
        if any(
            (
                job.get("updated_at") != expected.get("job_updated_at"),
                (job.get("source_url") or "") != expected.get("job_source_url"),
                (job.get("jd_text") or "") != expected.get("job_jd_text"),
                job_hash != expected.get("job_content_hash"),
                (job.get("source_metadata") or {}).get("full_jd_status") == "missing",
                not str(job.get("jd_text") or "").strip(),
                expected.get("target_url") != params.get("p_target_url"),
                expected.get("target_host") != params.get("p_target_host"),
                expected.get("target_classification")
                != params.get("p_target_classification"),
            )
        ):
            return {"status": "blocked", "code": "target_changed"}

        packet = next(
            (
                row for row in self.tables.setdefault("application_packets", [])
                if row.get("id") == packet_id
                and row.get("owner_id", self.owner_id) == self.owner_id
                and row.get("job_id") == job_id
            ),
            None,
        )
        if packet is None or any(
            (
                packet.get("updated_at") != expected.get("packet_updated_at"),
                packet.get("status") != "packet_created",
                packet.get("review_status") != "reviewed",
                packet.get("archived_at") is not None,
                packet.get("prompt_version") != "jobs_packet.v1",
                packet.get("prompt_version") != expected.get("prompt_version"),
                packet.get("packet_version") != expected.get("packet_version"),
                packet.get("source_cv_id") != expected.get("source_cv_id"),
                packet.get("tailored_cv_id") != expected.get("tailored_cv_id"),
                packet.get("cover_letter_id") != expected.get("cover_letter_id"),
                packet.get("source_cv_hash") != expected.get("source_cv_hash"),
                packet.get("job_content_hash") != expected.get("job_content_hash"),
                packet.get("tailored_cv_hash") != expected.get("tailored_cv_hash"),
                packet.get("cover_letter_hash") != expected.get("cover_letter_hash"),
                packet.get("profile_answers_complete") is not True,
                packet.get("legal_answers_confirmed") is not True,
                packet.get("visa_answers_confirmed") is not True,
                packet.get("cover_letter_review_status") != "confirmed",
                not packet.get("cover_letter_review_confirmed_at"),
                not packet.get("cover_letter_review_id"),
            )
        ):
            return {"status": "blocked", "code": "packet_changed"}

        docs = self.tables.setdefault("documents", [])

        def _document(doc_id: object) -> dict | None:
            return next(
                (
                    row for row in docs
                    if row.get("id") == doc_id
                    and row.get("owner_id", self.owner_id) == self.owner_id
                ),
                None,
            )

        source = _document(expected.get("source_cv_id"))
        tailored = _document(expected.get("tailored_cv_id"))
        letter = _document(expected.get("cover_letter_id"))

        def _yaml_hash(row: dict | None) -> str:
            value = "" if row is None else str(row.get("yaml_blob") or "")
            return hashlib.sha256(value.encode("utf-8")).hexdigest()

        if (
            source is None
            or source.get("type") != "cv"
            or source.get("deleted_at") is not None
            or source.get("updated_at") != expected.get("source_cv_updated_at")
            or _yaml_hash(source) != expected.get("source_cv_hash")
            or tailored is None
            or tailored.get("type") != "cv"
            or tailored.get("job_id") != job_id
            or tailored.get("deleted_at") is not None
            or tailored.get("updated_at") != expected.get("tailored_cv_updated_at")
            or _yaml_hash(tailored) != expected.get("tailored_cv_hash")
            or (tailored.get("accent") or "") != expected.get("tailored_cv_accent")
            or (tailored.get("font") or "") != expected.get("tailored_cv_font")
            or letter is None
            or letter.get("type") != "cover_letter"
            or letter.get("job_id") != job_id
            or letter.get("deleted_at") is not None
            or letter.get("updated_at") != expected.get("cover_letter_updated_at")
            or _yaml_hash(letter) != expected.get("cover_letter_hash")
        ):
            return {"status": "blocked", "code": "document_changed"}

        artifacts = self.tables.setdefault("pdf_artifacts", [])

        def _artifact(artifact_id: object) -> dict | None:
            return next(
                (
                    row for row in artifacts
                    if row.get("id") == artifact_id
                    and row.get("owner_id", self.owner_id) == self.owner_id
                ),
                None,
            )

        tailored_artifact = _artifact(expected.get("tailored_artifact_id"))
        letter_artifact = _artifact(expected.get("letter_artifact_id"))
        if (
            tailored_artifact is None
            or tailored_artifact.get("document_id") != expected.get("tailored_cv_id")
            or tailored_artifact.get("stale") is not False
            or tailored_artifact.get("r2_key") != expected.get("tailored_artifact_r2_key")
            or tailored_artifact.get("content_hash")
            != expected.get("tailored_artifact_content_hash")
            or tailored_artifact.get("generation_id")
            != expected.get("tailored_artifact_generation_id")
            or tailored_artifact.get("page_count")
            != expected.get("tailored_artifact_page_count")
            or tailored_artifact.get("page_count") != 1
            or letter_artifact is None
            or letter_artifact.get("document_id") != expected.get("cover_letter_id")
            or letter_artifact.get("stale") is not False
            or letter_artifact.get("r2_key") != expected.get("letter_artifact_r2_key")
            or letter_artifact.get("content_hash")
            != expected.get("letter_artifact_content_hash")
            or letter_artifact.get("generation_id")
            != expected.get("letter_artifact_generation_id")
            or letter_artifact.get("page_count")
            != expected.get("letter_artifact_page_count")
            or int(letter_artifact.get("page_count") or 0) < 1
        ):
            return {"status": "blocked", "code": "artifact_changed"}

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
        return {"status": "recorded", "attempt": attempt}

    def _replace_active_application_packet(self, params: dict) -> dict:
        payload = dict(params.get("p_payload") or {})
        if params.get("p_owner_id") != self.owner_id:
            return {"status": "blocked", "code": "account_unavailable"}
        profile = self._profile_row()
        if profile.get("deletion_requested_at") is not None:
            return {"status": "blocked", "code": "account_unavailable"}
        packets = self.tables.setdefault("application_packets", [])
        matching = [
            row
            for row in packets
            if row.get("owner_id", self.owner_id) == self.owner_id
            and row.get("job_id") == payload.get("job_id")
            and row.get("source_cv_id") == payload.get("source_cv_id")
        ]
        next_version = max(
            (int(row.get("packet_version") or 0) for row in matching),
            default=0,
        ) + 1
        archived_ids = []
        for row in matching:
            if row.get("archived_at") is None:
                row["status"] = "archived"
                row["archived_at"] = params.get("p_now")
                row["updated_at"] = params.get("p_now")
                archived_ids.append(row.get("id"))
        created = {
            **payload,
            "id": str(uuid.uuid4()),
            "owner_id": self.owner_id,
            "packet_version": next_version,
            "archived_at": None,
            "created_at": params.get("p_now"),
            "updated_at": params.get("p_now"),
        }
        packets.append(created)
        return {
            "status": "created",
            "packet": created,
            "archived_ids": archived_ids,
        }

    def _save_import_result_as_job(self, params: dict) -> dict:
        """Emulate the locked import-result-to-job transaction."""
        if params.get("p_owner_id") != self.owner_id:
            return {"status": "blocked", "code": "account_unavailable"}
        if self._profile_row().get("deletion_requested_at") is not None:
            return {"status": "blocked", "code": "account_unavailable"}

        run_id = params.get("p_run_id")
        run = next(
            (
                row
                for row in self.tables.setdefault("job_import_runs", [])
                if row.get("id") == run_id
                and row.get("owner_id", self.owner_id) == self.owner_id
            ),
            None,
        )
        if run is None:
            return {"status": "blocked", "code": "import_run_not_found"}

        result_id = params.get("p_result_id")
        result = next(
            (
                row
                for row in self.tables.setdefault("job_import_results", [])
                if row.get("id") == result_id
                and row.get("import_run_id") == run_id
                and row.get("owner_id", self.owner_id) == self.owner_id
            ),
            None,
        )
        if result is None:
            return {"status": "blocked", "code": "import_result_not_found"}

        jobs = self.tables.setdefault("saved_jobs", [])
        linked_job = next(
            (
                row
                for row in jobs
                if row.get("id") == result.get("saved_job_id")
                and row.get("owner_id", self.owner_id) == self.owner_id
            ),
            None,
        )
        if linked_job is not None:
            return {
                "status": "linked",
                "job": linked_job,
                "result_id": result_id,
            }

        payload = dict(params.get("p_job_payload") or {})
        provider = payload.get("source_provider") or None
        external_id = payload.get("source_external_id") or None
        content_hash = payload.get("content_hash") or None
        duplicate = None
        if provider and external_id:
            duplicate = next(
                (
                    row
                    for row in jobs
                    if row.get("owner_id", self.owner_id) == self.owner_id
                    and row.get("source_provider") == provider
                    and row.get("source_external_id") == external_id
                ),
                None,
            )
        if duplicate is None and content_hash:
            duplicate = next(
                (
                    row
                    for row in jobs
                    if row.get("owner_id", self.owner_id) == self.owner_id
                    and row.get("content_hash") == content_hash
                ),
                None,
            )

        if duplicate is None:
            now = params.get("p_now")
            written = {
                **payload,
                "id": str(uuid.uuid4()),
                "owner_id": self.owner_id,
                "import_run_id": run_id,
                "status": "saved",
                "created_at": now,
                "updated_at": now,
            }
            written["source_metadata"] = _sanitize_saved_job_source_metadata(
                written.get("source_metadata")
            )
            jobs.append(written)
            status = "created"
        else:
            written = duplicate
            status = "linked"

        result.update(
            {
                "selected": True,
                "saved_job_id": written.get("id"),
                "updated_at": params.get("p_now"),
            }
        )
        return {"status": status, "job": written, "result_id": result_id}

    def _set_saved_job_star(self, params: dict) -> dict:
        """Merge only the star bit into current metadata under the job lock."""
        if params.get("p_owner_id") != self.owner_id:
            return {"status": "blocked", "code": "account_unavailable"}
        if self._profile_row().get("deletion_requested_at") is not None:
            return {"status": "blocked", "code": "account_unavailable"}
        job = next(
            (
                row
                for row in self.tables.setdefault("saved_jobs", [])
                if row.get("id") == params.get("p_job_id")
                and row.get("owner_id", self.owner_id) == self.owner_id
            ),
            None,
        )
        if job is None:
            return {"status": "not_found", "code": "job_not_found"}
        metadata = _sanitize_saved_job_source_metadata(job.get("source_metadata"))
        metadata["starred"] = params.get("p_starred") is True
        job["source_metadata"] = metadata
        job["updated_at"] = params.get("p_now")
        return {"status": "saved", "job": job}

    def _revalidate_saved_job_source(self, params: dict) -> dict:
        """Merge revalidation evidence without replacing concurrent metadata."""
        if params.get("p_owner_id") != self.owner_id:
            return {"status": "blocked", "code": "account_unavailable"}
        if self._profile_row().get("deletion_requested_at") is not None:
            return {"status": "blocked", "code": "account_unavailable"}
        job = next(
            (
                row
                for row in self.tables.setdefault("saved_jobs", [])
                if row.get("id") == params.get("p_job_id")
                and row.get("owner_id", self.owner_id) == self.owner_id
            ),
            None,
        )
        if job is None:
            return {"status": "not_found", "code": "job_not_found"}

        metadata = _sanitize_saved_job_source_metadata(job.get("source_metadata"))
        metadata["revalidation"] = "manual_review"
        mark_stale = params.get("p_mark_stale") is True
        full_jd_text = params.get("p_full_jd_text")
        now = params.get("p_now")
        if mark_stale:
            verification_status = "stale"
        else:
            verification_status = (
                "needs_review"
                if full_jd_text is None
                and metadata.get("full_jd_status") == "missing"
                else "user_provided"
            )
            job["last_seen_at"] = now
            metadata["verified_at"] = str(now or "")[:10]
        if full_jd_text is not None:
            metadata["full_jd_status"] = "reviewed"
            job["jd_text"] = full_jd_text
            job["jd_json"] = params.get("p_jd_json")
            job["content_hash"] = params.get("p_content_hash")
            job["experience_requirements"] = params.get(
                "p_experience_requirements"
            )
        job.update(
            {
                "verification_status": verification_status,
                "source_metadata": metadata,
                "updated_at": now,
            }
        )
        return {"status": "saved", "job": job}

    @staticmethod
    def _text_hash(value: object) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

    def _confirm_application_packet_cover_review(self, params: dict) -> dict:
        """Validate all packet lineage and confirm it in one fake transaction."""
        if params.get("p_owner_id") != self.owner_id:
            return {"status": "blocked", "code": "account_unavailable"}
        if self._profile_row().get("deletion_requested_at") is not None:
            return {"status": "blocked", "code": "account_unavailable"}

        job_id = params.get("p_job_id")
        job = next(
            (
                row
                for row in self.tables.setdefault("saved_jobs", [])
                if row.get("id") == job_id
                and row.get("owner_id", self.owner_id) == self.owner_id
            ),
            None,
        )
        if job is None:
            return {"status": "not_found", "code": "job_not_found"}
        packet = next(
            (
                row
                for row in self.tables.setdefault("application_packets", [])
                if row.get("id") == params.get("p_packet_id")
                and row.get("job_id") == job_id
                and row.get("owner_id", self.owner_id) == self.owner_id
                and row.get("archived_at") is None
            ),
            None,
        )
        if packet is None:
            return {"status": "not_found", "code": "packet_not_found"}
        if packet.get("cover_letter_hash") != params.get("p_cover_letter_hash"):
            return {"status": "blocked", "code": "cover_letter_hash_mismatch"}

        documents = self.tables.setdefault("documents", [])

        def _document(document_id: object) -> dict | None:
            return next(
                (
                    row
                    for row in documents
                    if row.get("id") == document_id
                    and row.get("owner_id", self.owner_id) == self.owner_id
                ),
                None,
            )

        stale_reasons: list[str] = []
        source = _document(packet.get("source_cv_id"))
        if (
            source is None
            or source.get("type") != "cv"
            or source.get("deleted_at") is not None
        ):
            stale_reasons.append("source_cv_missing")
        elif not packet.get("source_cv_hash"):
            stale_reasons.append("source_cv_hash_missing")
        elif self._text_hash(source.get("yaml_blob")) != packet.get("source_cv_hash"):
            stale_reasons.append("source_cv_stale")

        metadata = job.get("source_metadata")
        full_jd_status = metadata.get("full_jd_status") if isinstance(metadata, dict) else None
        if not str(job.get("jd_text") or "").strip() or full_jd_status == "missing":
            stale_reasons.append("job_description_missing")
        elif not packet.get("job_content_hash"):
            stale_reasons.append("job_content_hash_missing")
        elif self._text_hash(job.get("jd_text")) != packet.get("job_content_hash"):
            stale_reasons.append("job_description_stale")

        tailored = _document(packet.get("tailored_cv_id"))
        if (
            tailored is None
            or tailored.get("type") != "cv"
            or tailored.get("job_id") != job_id
            or tailored.get("deleted_at") is not None
        ):
            stale_reasons.append("tailored_cv_missing")
        elif not packet.get("tailored_cv_hash"):
            stale_reasons.append("tailored_cv_hash_missing")
        elif self._text_hash(tailored.get("yaml_blob")) != packet.get("tailored_cv_hash"):
            stale_reasons.append("tailored_cv_stale")

        letter = _document(packet.get("cover_letter_id"))
        if (
            letter is None
            or letter.get("type") != "cover_letter"
            or letter.get("job_id") != job_id
            or letter.get("deleted_at") is not None
        ):
            stale_reasons.append("cover_letter_missing")
        elif not packet.get("cover_letter_hash"):
            stale_reasons.append("cover_letter_hash_missing")
        elif self._text_hash(letter.get("yaml_blob")) != packet.get("cover_letter_hash"):
            stale_reasons.append("cover_letter_stale")

        if stale_reasons:
            return {
                "status": "blocked",
                "code": "packet_stale",
                "stale_reasons": stale_reasons,
            }
        if (
            packet.get("status") == "packet_created"
            and packet.get("review_status") == "reviewed"
            and packet.get("cover_letter_review_status") == "confirmed"
            and packet.get("cover_letter_review_confirmed_at") is not None
            and str(packet.get("cover_letter_review_id") or "").strip()
        ):
            return {"status": "confirmed", "packet": packet, "idempotent": True}
        if (
            packet.get("status") != "review_applied"
            or packet.get("review_status") != "reviewed"
            or packet.get("cover_letter_review_status") != "pending"
            or not str(packet.get("cover_letter_review_id") or "").strip()
        ):
            return {
                "status": "blocked",
                "code": "cover_letter_review_not_pending",
            }

        packet.update(
            {
                "status": "packet_created",
                "cover_letter_review_status": "confirmed",
                "cover_letter_review_confirmed_at": params.get("p_now"),
                "updated_at": params.get("p_now"),
            }
        )
        return {"status": "confirmed", "packet": packet, "idempotent": False}

    def _merge_incomplete_application_packet(self, params: dict) -> dict:
        """Emulate the advisory-locked standalone packet lineage merge."""
        if params.get("p_owner_id") != self.owner_id:
            return {"status": "blocked", "code": "account_unavailable"}
        if self._profile_row().get("deletion_requested_at") is not None:
            return {"status": "blocked", "code": "account_unavailable"}
        payload = dict(params.get("p_payload") or {})
        job_id = payload.get("job_id")
        source_cv_id = payload.get("source_cv_id")
        jobs = self.tables.setdefault("saved_jobs", [])
        job = next(
            (
                row
                for row in jobs
                if row.get("id") == job_id
                and row.get("owner_id", self.owner_id) == self.owner_id
            ),
            None,
        )
        if job is None:
            return {"status": "blocked", "code": "job_not_found"}

        documents = self.tables.setdefault("documents", [])

        def _live_document(
            document_id: object,
            document_type: str,
            *,
            require_job: bool = False,
        ) -> dict | None:
            return next(
                (
                    row
                    for row in documents
                    if row.get("id") == document_id
                    and row.get("owner_id", self.owner_id) == self.owner_id
                    and row.get("type") == document_type
                    and row.get("deleted_at") is None
                    and (not require_job or row.get("job_id") == job_id)
                ),
                None,
            )

        source = _live_document(source_cv_id, "cv")
        if source is None:
            return {"status": "blocked", "code": "source_cv_not_found"}
        tailored_cv_id = payload.get("tailored_cv_id")
        tailored = (
            _live_document(tailored_cv_id, "cv", require_job=True)
            if tailored_cv_id
            else None
        )
        if tailored_cv_id and tailored is None:
            return {"status": "blocked", "code": "tailored_cv_not_found"}
        cover_letter_id = payload.get("cover_letter_id")
        letter = (
            _live_document(cover_letter_id, "cover_letter", require_job=True)
            if cover_letter_id
            else None
        )
        if cover_letter_id and letter is None:
            return {"status": "blocked", "code": "cover_letter_not_found"}

        packets = self.tables.setdefault("application_packets", [])
        active_rows = [
            row
            for row in packets
            if row.get("owner_id", self.owner_id) == self.owner_id
            and row.get("job_id") == job_id
            and row.get("source_cv_id") == source_cv_id
            and row.get("archived_at") is None
        ]
        active_packet = max(
            active_rows,
            key=lambda row: (
                str(row.get("updated_at") or ""),
                str(row.get("id") or ""),
            ),
            default=None,
        )
        metadata = job.get("source_metadata")
        full_jd_status = (
            metadata.get("full_jd_status") if isinstance(metadata, dict) else None
        )
        if any(
            (
                not str(job.get("jd_text") or "").strip(),
                full_jd_status == "missing",
                self._text_hash(job.get("jd_text"))
                != payload.get("job_content_hash"),
                self._text_hash(source.get("yaml_blob"))
                != payload.get("source_cv_hash"),
                tailored is not None
                and self._text_hash(tailored.get("yaml_blob"))
                != payload.get("tailored_cv_hash"),
                letter is not None
                and self._text_hash(letter.get("yaml_blob"))
                != payload.get("cover_letter_hash"),
            )
        ):
            return {"status": "blocked", "code": "packet_input_changed"}
        matching = None
        if active_packet is not None and all(
            (
                (active_packet.get("source_cv_hash") or active_packet.get("input_hash"))
                == payload.get("source_cv_hash"),
                active_packet.get("job_content_hash")
                == payload.get("job_content_hash"),
                active_packet.get("prompt_version") == payload.get("prompt_version"),
                active_packet.get("score_system_id")
                == payload.get("score_system_id"),
            )
        ):
            matching = active_packet

        compatible = bool(
            matching is not None
            and matching.get("status") != "packet_created"
            and (
                tailored_cv_id is None
                or matching.get("tailored_cv_id") in {None, tailored_cv_id}
            )
            and (
                cover_letter_id is None
                or matching.get("cover_letter_id") in {None, cover_letter_id}
            )
        )
        if compatible:
            matching["tailored_cv_id"] = tailored_cv_id or matching.get(
                "tailored_cv_id"
            )
            matching["cover_letter_id"] = cover_letter_id or matching.get(
                "cover_letter_id"
            )
            matching["status"] = "review_applied"
            matching["review_status"] = "reviewed"
            matching["provider"] = payload.get("provider") or matching.get("provider")
            matching["model"] = payload.get("model") or matching.get("model")
            if payload.get("score_snapshot_present") is True:
                matching["score_snapshot"] = payload.get("score_snapshot") or {}
            matching["tailored_cv_hash"] = (
                payload.get("tailored_cv_hash")
                or matching.get("tailored_cv_hash")
                or matching.get("output_hash")
            )
            matching["cover_letter_hash"] = (
                payload.get("cover_letter_hash") or matching.get("cover_letter_hash")
            )
            matching["input_hash"] = payload.get("source_cv_hash")
            matching["output_hash"] = (
                payload.get("tailored_cv_hash")
                or matching.get("tailored_cv_hash")
                or matching.get("output_hash")
            )
            matching["cover_letter_review_status"] = "pending"
            matching["cover_letter_review_confirmed_at"] = None
            matching["cover_letter_review_id"] = (
                payload.get("cover_letter_review_id")
                or matching.get("cover_letter_review_id")
            )
            matching["updated_at"] = params.get("p_now")
            return {"status": "merged", "packet": matching}

        next_version = max(
            (
                int(row.get("packet_version") or 0)
                for row in packets
                if row.get("owner_id", self.owner_id) == self.owner_id
                and row.get("job_id") == job_id
                and row.get("source_cv_id") == source_cv_id
            ),
            default=0,
        ) + 1
        now = params.get("p_now")
        if active_packet is not None and active_packet.get("status") != "packet_created":
            active_packet.update(
                {"status": "archived", "archived_at": now, "updated_at": now}
            )
        archived = active_packet is not None and active_packet.get("status") == "packet_created"
        written = {
            "id": str(uuid.uuid4()),
            "owner_id": self.owner_id,
            "job_id": job_id,
            "source_cv_id": source_cv_id,
            "tailored_cv_id": tailored_cv_id,
            "cover_letter_id": cover_letter_id,
            "status": "archived" if archived else "review_applied",
            "review_status": "reviewed",
            "provider": payload.get("provider") or None,
            "model": payload.get("model") or None,
            "prompt_version": payload.get("prompt_version"),
            "score_system_id": payload.get("score_system_id"),
            "score_snapshot": payload.get("score_snapshot") or {},
            "source_cv_hash": payload.get("source_cv_hash"),
            "job_content_hash": payload.get("job_content_hash"),
            "tailored_cv_hash": payload.get("tailored_cv_hash"),
            "cover_letter_hash": payload.get("cover_letter_hash"),
            "input_hash": payload.get("source_cv_hash"),
            "output_hash": payload.get("tailored_cv_hash"),
            "packet_version": next_version,
            "cover_letter_review_status": "pending",
            "cover_letter_review_confirmed_at": None,
            "cover_letter_review_id": payload.get("cover_letter_review_id") or None,
            "archived_at": now if archived else None,
            "created_at": now,
            "updated_at": now,
        }
        packets.append(written)
        return {"status": "archived" if archived else "created", "packet": written}

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

    def _delete_saved_job_safely(self, params: dict) -> dict:
        """Emulate the atomic authenticated saved-job deletion RPC."""
        job_id = params.get("p_job_id")
        profile = next(
            (r for r in self.tables.setdefault("profiles", []) if r.get("id") == self.owner_id),
            None,
        )
        if profile and profile.get("deletion_requested_at") is not None:
            return {"status": "blocked", "code": "account_unavailable"}

        jobs = self.tables.setdefault("saved_jobs", [])
        job = next(
            (
                row
                for row in jobs
                if row.get("id") == job_id
                and row.get("owner_id", self.owner_id) == self.owner_id
            ),
            None,
        )
        if job is None:
            return {"status": "not_found", "code": "job_not_found"}

        protected_tables = (
            ("documents", "job_id"),
            ("application_packets", "job_id"),
            ("application_apply_attempts", "job_id"),
            ("tailor_review_states", "job_id"),
        )
        if any(
            row.get(column) == job_id
            and row.get("owner_id", self.owner_id) == self.owner_id
            for table, column in protected_tables
            for row in self.tables.setdefault(table, [])
        ):
            return {"status": "blocked", "code": "job_has_application_history"}

        jobs.remove(job)
        for row in self.tables.setdefault("job_import_results", []):
            if row.get("saved_job_id") == job_id:
                row["saved_job_id"] = None
        return {"status": "deleted", "deleted": job_id}

    @staticmethod
    def _artifact_key(owner_id: str, row: dict) -> str:
        generation_id = row.get("generation_id")
        suffix = (
            f"{row.get('content_hash')}.pdf"
            if generation_id is None
            else f"{row.get('content_hash')}/{generation_id}.pdf"
        )
        return f"{owner_id}/{row.get('document_id')}/{suffix}"

    def _artifact_is_canonical(self, row: dict) -> bool:
        content_hash = str(row.get("content_hash") or "")
        return (
            len(content_hash) == 64
            and all(char in "0123456789abcdef" for char in content_hash)
            and row.get("r2_key") == self._artifact_key(self.owner_id, row)
        )

    def _queue_artifact(
        self,
        row: dict,
        *,
        not_before: str,
        accelerate: bool = False,
    ) -> dict:
        queue = self.tables.setdefault("pdf_artifact_cleanup_queue", [])
        existing = next(
            (
                item
                for item in queue
                if item.get("owner_id") == self.owner_id
                and item.get("r2_key") == row.get("r2_key")
            ),
            None,
        )
        if existing is None:
            existing = {
                "id": str(uuid.uuid4()),
                "owner_id": self.owner_id,
                "document_id": row.get("document_id"),
                "r2_key": row.get("r2_key"),
                "content_hash": row.get("content_hash"),
                "generation_id": row.get("generation_id"),
                "attempt_count": 0,
                "last_attempt_at": None,
                "claim_token": None,
                "claimed_at": None,
                "not_before": not_before,
                "created_at": "2026-08-01T00:00:00+00:00",
            }
            queue.append(existing)
        else:
            existing.update(
                {
                    "document_id": row.get("document_id"),
                    "content_hash": row.get("content_hash"),
                    "generation_id": row.get("generation_id"),
                }
            )
            current = str(existing.get("not_before") or "")
            existing["not_before"] = (
                min(current, not_before) if accelerate else max(current, not_before)
            )
        return existing

    @staticmethod
    def _cleanup_evidence(row: dict) -> dict:
        return {
            "cleanup_id": row.get("id"),
            "document_id": row.get("document_id"),
            "r2_key": row.get("r2_key"),
            "content_hash": row.get("content_hash"),
            "generation_id": row.get("generation_id"),
            "not_before": row.get("not_before"),
        }

    def _document_for_rpc(self, doc_id: str, doc_type: str | None = None) -> dict | None:
        return next(
            (
                row
                for row in self.tables.setdefault("documents", [])
                if row.get("id") == doc_id
                and row.get("owner_id", self.owner_id) == self.owner_id
                and row.get("deleted_at") is None
                and (doc_type is None or row.get("type") == doc_type)
            ),
            None,
        )

    def _detach_pdf_artifacts(self, params: dict) -> dict:
        doc_id = params.get("p_document_id")
        if params.get("p_owner_id") != self.owner_id:
            return {"status": "not_found", "artifacts": []}
        if self._document_for_rpc(doc_id) is None:
            return {"status": "not_found", "artifacts": []}
        artifacts = [
            row
            for row in self.tables.setdefault("pdf_artifacts", [])
            if row.get("document_id") == doc_id
            and row.get("owner_id", self.owner_id) == self.owner_id
        ]
        if any(not self._artifact_is_canonical(row) for row in artifacts):
            return {"status": "blocked", "code": "document_artifact_invalid"}
        for row in artifacts:
            self._queue_artifact(row, not_before="9999-12-31T23:59:59+00:00")
        self.tables["pdf_artifacts"] = [
            row for row in self.tables["pdf_artifacts"] if row not in artifacts
        ]
        evidence = [
            self._cleanup_evidence(row)
            for row in self.tables.setdefault("pdf_artifact_cleanup_queue", [])
            if row.get("owner_id") == self.owner_id
            and row.get("document_id") == doc_id
        ]
        return {"status": "detached", "document_id": doc_id, "artifacts": evidence}

    def _detach_owner_cv_pdf_artifacts(self, params: dict) -> dict:
        profile = self._profile_row()
        if (
            params.get("p_owner_id") != self.owner_id
            or profile.get("deletion_requested_at") is not None
        ):
            return {"status": "blocked", "code": "account_unavailable"}
        cv_ids = {
            row.get("id")
            for row in self.tables.setdefault("documents", [])
            if row.get("owner_id", self.owner_id) == self.owner_id
            and row.get("type") == "cv"
            and row.get("deleted_at") is None
        }
        artifacts = [
            row
            for row in self.tables.setdefault("pdf_artifacts", [])
            if row.get("owner_id", self.owner_id) == self.owner_id
            and row.get("document_id") in cv_ids
        ]
        if any(not self._artifact_is_canonical(row) for row in artifacts):
            return {"status": "blocked", "code": "document_artifact_invalid"}
        evidence = [
            self._cleanup_evidence(
                self._queue_artifact(
                    row,
                    not_before="1970-01-01T00:00:00+00:00",
                    accelerate=True,
                )
            )
            for row in artifacts
        ]
        self.tables["pdf_artifacts"] = [
            row for row in self.tables["pdf_artifacts"] if row not in artifacts
        ]
        return {"status": "detached", "artifacts": evidence}

    def _insert_document_if_account_active(self, params: dict) -> dict:
        profile = next(
            (
                row
                for row in self.tables.setdefault("profiles", [])
                if row.get("id") == self.owner_id
            ),
            None,
        )
        if profile and profile.get("deletion_requested_at") is not None:
            return {"status": "blocked", "code": "account_unavailable"}
        job_id = params.get("p_job_id")
        if job_id is not None and not any(
            row.get("id") == job_id
            and row.get("owner_id", self.owner_id) == self.owner_id
            for row in self.tables.setdefault("saved_jobs", [])
        ):
            return {"status": "blocked", "code": "job_unavailable"}
        doc_type = params.get("p_type")
        payload = {
            "type": doc_type,
            "name": params.get("p_name"),
            "yaml_blob": params.get("p_yaml_blob"),
            "job_id": job_id,
        }
        if doc_type == "cv":
            payload.update(
                {
                    "accent": params.get("p_accent"),
                    "font": params.get("p_font"),
                    "density": params.get("p_density"),
                }
            )
        else:
            payload.update(
                {
                    "summary_company": (params.get("p_summary_company") or "")[:500],
                    "summary_role": (params.get("p_summary_role") or "")[:500],
                }
            )
        rows = self.table("documents").insert(payload).execute().data or []
        return {
            "status": "created",
            "document": rows[0] if rows else None,
        }

    def _update_document_with_artifact_detach(self, params: dict) -> dict:
        doc_id = params.get("p_document_id")
        doc_type = params.get("p_document_type")
        document = self._document_for_rpc(doc_id, doc_type)
        if params.get("p_owner_id") != self.owner_id or document is None:
            return {"status": "not_found"}
        if document.get("updated_at") != params.get("p_expected_revision"):
            return {
                "status": "revision_conflict",
                "document_revision": document.get("updated_at"),
            }
        render_changed = document.get("yaml_blob") != params.get("p_yaml_blob")
        if doc_type == "cv":
            render_changed = render_changed or any(
                document.get(column) != params.get(parameter)
                for column, parameter in (
                    ("accent", "p_accent"),
                    ("font", "p_font"),
                    ("density", "p_density"),
                )
            )
        artifacts: list[dict] = []
        if render_changed:
            detached = self._detach_pdf_artifacts(
                {"p_owner_id": self.owner_id, "p_document_id": doc_id}
            )
            if detached.get("status") != "detached":
                return detached
            artifacts = detached.get("artifacts") or []
        document["yaml_blob"] = params.get("p_yaml_blob")
        document["updated_at"] = params.get("p_updated_at")
        if doc_type == "cv":
            document.update(
                {
                    "accent": params.get("p_accent"),
                    "font": params.get("p_font"),
                    "density": params.get("p_density"),
                }
            )
        else:
            document.update(
                {
                    "summary_company": (params.get("p_summary_company") or "")[:500],
                    "summary_role": (params.get("p_summary_role") or "")[:500],
                }
            )
        return {
            "status": "saved",
            "document_id": doc_id,
            "document_revision": params.get("p_updated_at"),
            "render_changed": bool(render_changed),
            "artifacts": artifacts,
        }

    def _dependency_is_current(self, params: dict) -> bool:
        dependency_id = params.get("p_dependency_document_id")
        dependency_revision = params.get("p_dependency_revision")
        if (dependency_id is None) != (dependency_revision is None):
            return False
        if dependency_id is None:
            return True
        dependency = self._document_for_rpc(dependency_id, "cv")
        return bool(
            dependency is not None
            and dependency.get("updated_at") == dependency_revision
        )

    def _register_pdf_upload_candidate(self, params: dict) -> dict:
        document = self._document_for_rpc(params.get("p_document_id"))
        if (
            params.get("p_owner_id") != self.owner_id
            or document is None
            or document.get("updated_at") != params.get("p_document_revision")
            or not self._dependency_is_current(params)
        ):
            return {"status": "revision_conflict"}
        generation_id = params.get("p_generation_id")
        candidate = {
            "document_id": params.get("p_document_id"),
            "content_hash": params.get("p_content_hash"),
            "generation_id": generation_id,
        }
        candidate["r2_key"] = self._artifact_key(self.owner_id, candidate)
        queued = self._queue_artifact(
            candidate,
            not_before="9999-12-31T23:59:59+00:00",
        )
        return {
            "status": "registered",
            "cleanup_id": queued["id"],
            **candidate,
        }

    def _resolve_pdf_artifact(self, params: dict) -> dict:
        document = self._document_for_rpc(params.get("p_document_id"))
        if (
            params.get("p_owner_id") != self.owner_id
            or document is None
            or document.get("updated_at") != params.get("p_document_revision")
            or not self._dependency_is_current(params)
        ):
            return {"status": "revision_conflict"}
        artifact = next(
            (
                row
                for row in self.tables.setdefault("pdf_artifacts", [])
                if row.get("document_id") == params.get("p_document_id")
                and row.get("owner_id", self.owner_id) == self.owner_id
                and row.get("content_hash") == params.get("p_content_hash")
                and row.get("stale") is False
            ),
            None,
        )
        if artifact is None:
            return {"status": "miss"}
        if not self._artifact_is_canonical(artifact):
            return {"status": "blocked", "code": "document_artifact_invalid"}
        return {
            "status": "hit",
            "artifact_id": artifact.get("id"),
            "document_id": artifact.get("document_id"),
            "r2_key": artifact.get("r2_key"),
            "content_hash": artifact.get("content_hash"),
            "generation_id": artifact.get("generation_id"),
            "page_count": artifact.get("page_count"),
        }

    def _authorize_document_export(self, params: dict) -> dict:
        profile = self._profile_row()
        if (
            params.get("p_owner_id") != self.owner_id
            or profile.get("deletion_requested_at") is not None
        ):
            return {"status": "blocked", "code": "account_unavailable"}
        document = self._document_for_rpc(params.get("p_document_id"))
        if document is None:
            return {"status": "blocked", "code": "document_unavailable"}
        if document.get("updated_at") != params.get("p_document_revision"):
            return {"status": "revision_conflict"}
        return {
            "status": "authorized",
            "document_id": document.get("id"),
            "document_revision": document.get("updated_at"),
        }

    def _publish_pdf_artifact(self, params: dict) -> dict:
        document = self._document_for_rpc(params.get("p_document_id"))
        if (
            params.get("p_owner_id") != self.owner_id
            or document is None
            or document.get("updated_at") != params.get("p_document_revision")
            or not self._dependency_is_current(params)
        ):
            return {"status": "revision_conflict"}
        candidate = {
            "document_id": params.get("p_document_id"),
            "content_hash": params.get("p_content_hash"),
            "generation_id": params.get("p_generation_id"),
        }
        candidate_key = self._artifact_key(self.owner_id, candidate)
        queue = self.tables.setdefault("pdf_artifact_cleanup_queue", [])
        candidate_row = next(
            (
                row
                for row in queue
                if row.get("owner_id") == self.owner_id
                and row.get("r2_key") == candidate_key
                and row.get("generation_id") == params.get("p_generation_id")
            ),
            None,
        )
        if candidate_row is None:
            return {"status": "candidate_missing"}
        if candidate_row.get("claim_token") is not None:
            return {"status": "candidate_expired"}
        artifacts = [
            row
            for row in self.tables.setdefault("pdf_artifacts", [])
            if row.get("document_id") == params.get("p_document_id")
            and row.get("owner_id", self.owner_id) == self.owner_id
        ]
        if any(not self._artifact_is_canonical(row) for row in artifacts):
            return {"status": "blocked", "code": "document_artifact_invalid"}
        existing = next(
            (
                row
                for row in artifacts
                if row.get("content_hash") == params.get("p_content_hash")
                and row.get("stale") is False
            ),
            None,
        )
        for row in artifacts:
            if existing is None or row is not existing:
                self._queue_artifact(row, not_before="9999-12-31T23:59:59+00:00")
        self.tables["pdf_artifacts"] = [
            row
            for row in self.tables["pdf_artifacts"]
            if row not in artifacts or row is existing
        ]
        if existing is None:
            existing = {
                "id": str(uuid.uuid4()),
                "owner_id": self.owner_id,
                "document_id": params.get("p_document_id"),
                "r2_key": candidate_key,
                "content_hash": params.get("p_content_hash"),
                "generation_id": params.get("p_generation_id"),
                "page_count": params.get("p_page_count"),
                "stale": False,
            }
            self.tables["pdf_artifacts"].append(existing)
            status = "published"
        else:
            status = "cached"
        queue[:] = [
            row
            for row in queue
            if not (
                row.get("owner_id") == self.owner_id
                and row.get("r2_key") == existing.get("r2_key")
            )
        ]
        cleanup = [
            self._cleanup_evidence(row)
            for row in queue
            if row.get("owner_id") == self.owner_id
            and row.get("document_id") == params.get("p_document_id")
        ]
        return {
            "status": status,
            "artifact_id": existing.get("id"),
            "document_id": existing.get("document_id"),
            "r2_key": existing.get("r2_key"),
            "content_hash": existing.get("content_hash"),
            "generation_id": existing.get("generation_id"),
            "page_count": existing.get("page_count"),
            "discard_r2_key": candidate_key if status == "cached" else None,
            "artifacts": cleanup,
        }

    def _evict_broken_pdf_artifact(self, params: dict) -> dict:
        document = self._document_for_rpc(params.get("p_document_id"))
        if (
            params.get("p_owner_id") != self.owner_id
            or document is None
            or document.get("updated_at") != params.get("p_document_revision")
        ):
            return {"status": "revision_conflict"}
        artifact = next(
            (
                row
                for row in self.tables.setdefault("pdf_artifacts", [])
                if row.get("id") == params.get("p_artifact_id")
                and row.get("document_id") == params.get("p_document_id")
                and row.get("owner_id", self.owner_id) == self.owner_id
            ),
            None,
        )
        if artifact is None:
            return {"status": "already_replaced"}
        if not self._artifact_is_canonical(artifact):
            return {"status": "blocked", "code": "document_artifact_invalid"}
        queued = self._queue_artifact(
            artifact,
            not_before="1970-01-01T00:00:00+00:00",
            accelerate=True,
        )
        self.tables["pdf_artifacts"].remove(artifact)
        return {
            "status": "evicted",
            "artifact": self._cleanup_evidence(queued),
        }

    def _claim_pdf_artifact_cleanup(self, params: dict) -> dict:
        row = next(
            (
                item
                for item in self.tables.setdefault("pdf_artifact_cleanup_queue", [])
                if item.get("id") == params.get("p_cleanup_id")
                and item.get("owner_id") == params.get("p_owner_id")
            ),
            None,
        )
        if row is None:
            return {"status": "missing"}
        if not params.get("p_force") and str(row.get("not_before")) > "2026-08-01T12:00:00+00:00":
            return {"status": "pending", "not_before": row.get("not_before")}
        if row.get("claim_token") is not None:
            return {"status": "busy"}
        if not self._artifact_is_canonical(row):
            row["attempt_count"] = int(row.get("attempt_count") or 0) + 1
            row["last_attempt_at"] = "2026-08-01T12:00:00+00:00"
            return {
                "status": "blocked",
                "code": "cleanup_key_invalid",
                "failure_recorded": True,
            }
        active = any(
            artifact.get("owner_id", self.owner_id) == self.owner_id
            and artifact.get("document_id") == row.get("document_id")
            and artifact.get("r2_key") == row.get("r2_key")
            and artifact.get("content_hash") == row.get("content_hash")
            and artifact.get("generation_id") == row.get("generation_id")
            and artifact.get("stale") is False
            for artifact in self.tables.setdefault("pdf_artifacts", [])
        )
        if active:
            self.tables["pdf_artifact_cleanup_queue"].remove(row)
            return {"status": "active"}
        row["claim_token"] = params.get("p_claim_token")
        row["claimed_at"] = "2026-08-01T12:00:00+00:00"
        return {
            "status": "claimed",
            "cleanup_id": row.get("id"),
            "owner_id": row.get("owner_id"),
            "document_id": row.get("document_id"),
            "r2_key": row.get("r2_key"),
            "content_hash": row.get("content_hash"),
            "generation_id": row.get("generation_id"),
            "not_before": row.get("not_before"),
            "attempt_count": row.get("attempt_count"),
            "claim_token": row.get("claim_token"),
        }

    def _delete_document_safely(self, params: dict) -> dict:
        """Emulate the atomic authenticated CV/cover-letter deletion RPC."""
        doc_id = params.get("p_document_id")
        doc_type = params.get("p_document_type")
        if doc_type not in {"cv", "cover_letter"}:
            raise ValueError("unsupported document type")
        profile = next(
            (r for r in self.tables.setdefault("profiles", []) if r.get("id") == self.owner_id),
            None,
        )
        if profile and profile.get("deletion_requested_at") is not None:
            return {"status": "blocked", "code": "account_unavailable"}

        documents = self.tables.setdefault("documents", [])
        document = next(
            (
                row
                for row in documents
                if row.get("id") == doc_id
                and row.get("type") == doc_type
                and row.get("owner_id", self.owner_id) == self.owner_id
                and row.get("deleted_at") is None
            ),
            None,
        )
        if document is None:
            return {"status": "not_found", "code": "document_not_found"}

        has_packet = any(
            row.get("owner_id", self.owner_id) == self.owner_id
            and doc_id
            in {
                row.get("source_cv_id"),
                row.get("tailored_cv_id"),
                row.get("cover_letter_id"),
            }
            for row in self.tables.setdefault("application_packets", [])
        )
        has_review = any(
            row.get("owner_id", self.owner_id) == self.owner_id
            and row.get("cv_id") == doc_id
            for row in self.tables.setdefault("tailor_review_states", [])
        )
        # Legacy standalone letters resolved their signer live and never
        # persisted sender_name, so deleting their source CV would strip the
        # signature permanently. The production RPC blocks that; mirror it here
        # or the whole path stays untested and only diverges in production.
        has_legacy_letter_dependency = doc_type == "cv" and any(
            row.get("owner_id", self.owner_id) == self.owner_id
            and row.get("type") == "cover_letter"
            and row.get("deleted_at") is None
            and re.search(
                r"^source_cv_id:[ \t]*[\"']?" + re.escape(str(doc_id)) + r"[\"']?[ \t]*$",
                row.get("yaml_blob") or "",
                re.MULTILINE,
            )
            and not re.search(
                r"^sender_name:[ \t]*\S",
                row.get("yaml_blob") or "",
                re.MULTILINE,
            )
            for row in documents
        )
        if has_packet or has_review or has_legacy_letter_dependency:
            return {"status": "blocked", "code": "document_has_application_history"}

        owned_artifacts = [
            row
            for row in self.tables.setdefault("pdf_artifacts", [])
            if row.get("document_id") == doc_id
            and row.get("owner_id", self.owner_id) == self.owner_id
        ]
        for row in owned_artifacts:
            if not self._artifact_is_canonical(row):
                return {"status": "blocked", "code": "document_artifact_invalid"}

        artifacts = [
            self._cleanup_evidence(
                self._queue_artifact(
                    row,
                    not_before="1970-01-01T00:00:00+00:00",
                    accelerate=True,
                )
            )
            for row in owned_artifacts
        ]
        documents.remove(document)
        self.tables["pdf_artifacts"] = [
            row
            for row in self.tables.setdefault("pdf_artifacts", [])
            if row.get("document_id") != doc_id
        ]
        return {
            "status": "deleted",
            "deleted": doc_id,
            "document_type": doc_type,
            "artifacts": artifacts,
        }

    def _complete_pdf_artifact_cleanup(self, params: dict) -> bool:
        cleanup_id = params.get("p_cleanup_id")
        rows = self.tables.setdefault("pdf_artifact_cleanup_queue", [])
        row = next(
            (
                item for item in rows
                if item.get("id") == cleanup_id
                and item.get("owner_id", self.owner_id) == self.owner_id
                and item.get("claim_token") == params.get("p_claim_token")
            ),
            None,
        )
        if row is None:
            return False
        rows.remove(row)
        return True

    def _record_pdf_artifact_cleanup_failure(self, params: dict) -> bool:
        cleanup_id = params.get("p_cleanup_id")
        row = next(
            (
                item for item in self.tables.setdefault("pdf_artifact_cleanup_queue", [])
                if item.get("id") == cleanup_id
                and item.get("owner_id", self.owner_id) == self.owner_id
                and item.get("claim_token") == params.get("p_claim_token")
            ),
            None,
        )
        if row is None:
            return False
        row["attempt_count"] = int(row.get("attempt_count") or 0) + 1
        row["last_attempt_at"] = _next_ts()
        row["claim_token"] = None
        row["claimed_at"] = None
        return True

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

    def _request_account_deletion_with_artifact_detach(self) -> dict:
        profile = self._profile_row()
        if profile.get("deletion_requested_at") is not None:
            return {
                "status": "already_requested",
                "requested_at": profile.get("deletion_requested_at"),
                "artifacts": [],
                "invalid_artifact_count": 0,
            }
        requested_at = _next_ts()
        profile["deletion_requested_at"] = requested_at
        profile["updated_at"] = requested_at
        owned = [
            row
            for row in self.tables.setdefault("pdf_artifacts", [])
            if row.get("owner_id", self.owner_id) == self.owner_id
        ]
        valid = [row for row in owned if self._artifact_is_canonical(row)]
        artifacts = [
            self._cleanup_evidence(
                self._queue_artifact(
                    row,
                    not_before="1970-01-01T00:00:00+00:00",
                    accelerate=True,
                )
            )
            for row in valid
        ]
        self.tables["pdf_artifacts"] = [
            row for row in self.tables["pdf_artifacts"] if row not in owned
        ]
        for document in self.tables.setdefault("documents", []):
            if (
                document.get("owner_id", self.owner_id) == self.owner_id
                and document.get("deleted_at") is None
            ):
                document["deleted_at"] = requested_at
                document["updated_at"] = requested_at
        return {
            "status": "requested",
            "requested_at": requested_at,
            "artifacts": artifacts,
            "invalid_artifact_count": len(owned) - len(valid),
        }

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
    monkeypatch.setattr(auth_mod, "server_supabase", lambda: store)
    return store


@pytest.fixture
def client(server_module):
    server_module.app.config.update(TESTING=True)
    return server_module.app.test_client()
