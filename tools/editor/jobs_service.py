"""Saved-jobs workflow service (Stage 7).

A user saves a job (by pasting the description or giving a URL), then works
against it: tailoring a CV to it and generating a cover letter grounded in
that CV + this job. This service is the data + orchestration layer behind the
``/api/jobs`` routes; the renderer / 1-page logic is untouched.

It deliberately REUSES the Stage-1 through Stage-6 machinery rather than
duplicating it:

  * ``auth.user_supabase()`` — the single RLS seam. Every query runs under the
    caller's JWT, so ``saved_jobs`` rows are scoped to ``owner_id = auth.uid()``
    and another tenant's job is simply invisible (→ 404). ``owner_id`` defaults
    to ``auth.uid()`` in the schema, so it is NEVER taken from the request body.
  * ``ssrf.safe_fetch_url`` — a ``jd_url`` is fetched through the SSRF guard
    (RFC-1918 / loopback / link-local / metadata / redirect all blocked) before
    any request leaves the box. Paste/URL only; the Indeed-MCP import is OUT OF
    SCOPE for this stage.
  * ``cover_letter.jd.parse_jd`` — the deterministic JD→JSON parser (no LLM, no
    fabrication risk). The parse fills ``title``/``company`` when the caller
    didn't supply them.
  * ``tailor_service.tailor_cv`` + ``cover_letter_service.generate_cover_letter``
    — the existing guarded generators. The one-click flow chains them and links
    BOTH produced documents to the job (``documents.job_id``).

Document↔job linkage: a tailored CV or a cover letter created against a job
carries that job's id in ``documents.job_id``. A job's detail view groups its
documents by reading them back by ``job_id`` (RLS-scoped).
"""
from __future__ import annotations

from html.parser import HTMLParser
import hashlib
import ipaddress
import os
import re
import sqlite3
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlparse, urlunparse

import auth as auth_mod
import cover_letter_service as cl_service
import local_demo_store
import ssrf
import tailor_service as tailor_svc


def _sb():
    """The caller's job-storage client.

    RLS-scoped Supabase normally; only for the local demo owner in an
    unconfigured shell it falls back to the ephemeral in-memory demo store so
    the jobs repository stays usable (instead of 500ing) without a project.
    """
    if local_demo_store.active():
        return local_demo_store.db()
    return auth_mod.user_supabase()


def _server_sb():
    """Server-owned client for reviewed writes (lineage, review claims).

    Same local-demo fallback as :func:`_sb` so packet flows degrade
    consistently in the local shell.
    """
    if local_demo_store.active():
        return local_demo_store.db()
    return auth_mod.server_supabase()

# engine/ is on sys.path in every entrypoint (server boot, tests).
try:
    from cover_letter.jd import MAX_JD_CHARS, parse_jd
    from ats.experience import build_experience_fit, extract_experience_requirements
    from demo_policy import demo_mode_enabled as _demo_mode_enabled
except ImportError:  # pragma: no cover - unusual import roots
    from engine.cover_letter.jd import MAX_JD_CHARS, parse_jd
    from engine.ats.experience import build_experience_fit, extract_experience_requirements
    from engine.demo_policy import demo_mode_enabled as _demo_mode_enabled

# Allowed lifecycle states for a saved job. Widened in Stage 6 (plan 002) in
# LOCKSTEP with the DB CHECK (migration 0007 saved_jobs_status_check). Kept tiny
# + explicit so an arbitrary/garbage status can never be written.
JOB_STATUSES = ("saved", "open", "speculative", "applied", "closed")

# Cap on a fetched/pasted JD (mirrors the parser's own cap so a giant paste
# can't bloat the row before parse_jd trims it).
MAX_JD_BYTES = MAX_JD_CHARS  # chars ~= bytes for the latin-heavy JD text

# Cap on the free-text title/company a caller may supply (defence-in-depth).
MAX_FIELD_LEN = 300

# Cap on the user-authored ``why_fit`` note (mirrors the DB length CHECK in
# migration 0007). Length-capped via _clean_why_fit; a runaway paste is trimmed.
MAX_WHY_FIT_LEN = 2000

# Confidence is a fit-percentage 0-100 (mirrors the DB range CHECK). Out-of-range
# or non-numeric input coerces to None rather than 500-ing (advisory metadata).
CONFIDENCE_MIN = 0
CONFIDENCE_MAX = 100

LINKEDIN_HOST_SUFFIXES = ("linkedin.com", "linkedin.cn")
JOB_URL_ALLOWLIST_ENV = "ONEPAGER_JOB_URL_ALLOWLIST"
LOCAL_JOBS_DB_ENV = "ONEPAGER_LOCAL_JOBS_DB"
LOCAL_JOBS_FRESH_DAYS = int(os.environ.get("ONEPAGER_LOCAL_JOBS_FRESH_DAYS", "7"))
LOCAL_JOBS_SEARCH_CANDIDATE_LIMIT = int(os.environ.get("ONEPAGER_LOCAL_JOBS_SEARCH_CANDIDATE_LIMIT", "1000"))
JOB_URL_RATE_LIMIT_PER_MINUTE = int(os.environ.get("ONEPAGER_JOB_URL_RATE_LIMIT_PER_MINUTE", "20"))
PACKET_PROMPT_VERSION = "jobs_packet.v1"
PACKET_VERSION = 1
DEFAULT_SCORE_SYSTEM_ID = "semantic_fit"
_URL_FETCH_BUCKETS: dict[str, list[float]] = {}
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_MAILTO_RE = re.compile(r"mailto:[^\s)>]+", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_INLINE_SECRET_RE = re.compile(
    r"\b(?:li_at|session[_-]?token|authorization|cookie|csrf|token|secret|password)\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_NUMERIC_IPV4_HOST_RE = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*\Z", re.IGNORECASE)
_PACKET_SENSITIVE_KEY_TOKENS = (
    "api_key",
    "authorization",
    "bearer",
    "ciphertext",
    "cookie",
    "csrf",
    "dek",
    "li_at",
    "nonce",
    "password",
    "raw_prompt",
    "session",
    "secret",
    "system_prompt",
    "token",
    "user_prompt",
)
_PACKET_SENSITIVE_VALUE_TOKENS = (
    "bearer ",
    "cookie",
    "jsessionid",
    "li_at",
    "raw-prompt",
    "review token",
    "secret",
    "session",
    "token",
)


def _owner_id() -> str:
    owner = getattr(auth_mod.auth_g(), "owner_id", None)
    if not owner:
        raise JobError(401, {"error": "unauthorized", "code": "unauthorized"})
    return str(owner)
_SAFE_SOURCE_METADATA_KEYS = {
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
_SENSITIVE_METADATA_TOKEN_RE = re.compile(
    r"(^|[^a-z0-9])(?:li_at|jsessionid|csrf|session[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token)([^a-z0-9]|$)|"
    r"\b(?:authorization|cookie|password|secret|token)\s*[:=]|\bbearer\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_SENSITIVE_METADATA_KEY_RE = re.compile(
    r"(^|[^a-z0-9])(?:api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|client[_-]?secret|key\s*[:=])",
    re.IGNORECASE,
)
_SENSITIVE_METADATA_PATTERNS = (
    re.compile(r"\bsk-(?:ant|proj|live|test|or|gsk|mistral|[A-Za-z0-9])[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}\b"),
)
_SENSITIVE_SOURCE_URL_SEGMENTS = {
    "access_token",
    "api_key",
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
_SAFE_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")

SOURCE_REGISTRY: dict[str, dict[str, Any]] = {
    "manual": {
        "id": "manual",
        "label": "Manual paste/export",
        "terms_basis": "user_supplied",
        "supports_preview": True,
        "supports_fetch": False,
        "requires_review": False,
        "filters": ("country", "region", "title", "remote_mode", "seniority", "freshness"),
    },
    "linkedin": {
        "id": "linkedin",
        "label": "LinkedIn manual paste",
        "terms_basis": "manual_user_supplied",
        "supports_preview": True,
        "supports_fetch": False,
        "requires_review": True,
        "approved_adapter_env": ("LINKEDIN_APPROVED_ADAPTER_ENABLED", "LINKEDIN_PARTNER_CONFIG_ID"),
        "filters": ("country", "region", "title", "remote_mode", "seniority", "freshness"),
    },
}

_DEMO_IMPORT_RUNS: dict[str, dict[str, Any]] = {}
_DEMO_IMPORT_RESULTS: dict[str, dict[str, Any]] = {}
_DEMO_SAVED_JOBS: dict[str, dict[str, Any]] = {}


def _local_jobs_db_path() -> tuple[Path | None, bool]:
    raw = os.environ.get(LOCAL_JOBS_DB_ENV, "").strip()
    if raw:
        return Path(raw).expanduser(), True
    default = Path.home() / "databases" / "jobs.db"
    return (default if default.exists() else None), False


def _open_local_jobs_db(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_job_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_days(value: object) -> int | None:
    parsed = _parse_job_date(value)
    if not parsed:
        return None
    return max(0, (datetime.now(timezone.utc).date() - parsed.date()).days)


def _metadata_secret_shaped(value: object) -> bool:
    text = str(value)
    return bool(_SENSITIVE_METADATA_TOKEN_RE.search(text) or _SENSITIVE_METADATA_KEY_RE.search(text)) or any(
        pattern.search(text) for pattern in _SENSITIVE_METADATA_PATTERNS
    )


def _source_url_path_secret_shaped(path: object) -> bool:
    for raw_segment in str(path or "").split("/"):
        segment = unquote(raw_segment).strip().lower()
        if not segment:
            continue
        if segment in _SENSITIVE_SOURCE_URL_SEGMENTS or _metadata_secret_shaped(segment):
            return True
    return False


def _safe_metadata_scalar(value: object) -> object | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        clean = value.strip()
        if not clean or _metadata_secret_shaped(clean):
            return None
        return clean[:1000]
    return None


def _safe_source_external_id(value: object, *, fallback_text: str) -> str:
    cleaned = _clean_field(value)
    if (
        cleaned
        and _SAFE_EXTERNAL_ID_RE.fullmatch(cleaned)
        and not _metadata_secret_shaped(cleaned)
    ):
        return cleaned
    return _content_hash(fallback_text)


def _public_source_external_id(value: object) -> str | None:
    cleaned = _clean_field(value)
    if (
        cleaned
        and _SAFE_EXTERNAL_ID_RE.fullmatch(cleaned)
        and not _metadata_secret_shaped(cleaned)
    ):
        return cleaned
    return None


def _safe_short_metadata_text(value: object) -> str:
    cleaned = _clean_field(value)
    return "" if _metadata_secret_shaped(cleaned) else cleaned


def _safe_source_scalar(value: object, *, limit: int = MAX_FIELD_LEN) -> str | None:
    cleaned = _safe_metadata_scalar(value)
    if not isinstance(cleaned, str):
        return None
    return cleaned[:limit]


def _safe_metadata_value(value: object) -> object | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_metadata_scalar(value)
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, list):
        items = [_safe_metadata_value(item) for item in value[:50]]
        return [item for item in items if item is not None]
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _metadata_secret_shaped(key):
                continue
            if key.lower() in {"url", "href", "source_url", "open_url"}:
                cleaned = _safe_source_url(item)
            else:
                cleaned = _safe_metadata_value(item)
            if cleaned is not None:
                out[key] = cleaned
        return out
    return None


def _packet_key_is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered == "prompt_version":
        return False
    return any(token in lowered for token in _PACKET_SENSITIVE_KEY_TOKENS)


def _packet_value_is_sensitive(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in _PACKET_SENSITIVE_VALUE_TOKENS)


def _safe_packet_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return None if _packet_value_is_sensitive(value) else value[:1000]
    if isinstance(value, list):
        items = [_safe_packet_value(item) for item in value[:50]]
        return [item for item in items if item is not None]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _packet_key_is_sensitive(key):
                continue
            cleaned = _safe_packet_value(item)
            if cleaned is not None:
                out[key] = cleaned
        return out
    return None


def _safe_source_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, object] = {}
    for key in _SAFE_SOURCE_METADATA_KEYS:
        if key not in value or _metadata_secret_shaped(key):
            continue
        if key in {"source_links", "search_criteria", "match_reasons", "ranking"}:
            cleaned = _safe_metadata_value(value.get(key))
        else:
            cleaned = _safe_metadata_scalar(value.get(key))
        if key == "source_db" and isinstance(cleaned, str):
            if "/" in cleaned or "\\" in cleaned or cleaned.startswith("."):
                continue
        if key == "source_artifact" and isinstance(cleaned, str):
            cleaned = Path(cleaned).name
        if cleaned is not None:
            out[key] = cleaned
    return out


class _ShortlistParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_section = ""
        self.in_h2 = False
        self.in_card = False
        self.card_depth = 0
        self.stack: list[str] = []
        self.cards: list[dict[str, Any]] = []
        self.card: dict[str, Any] | None = None
        self.current_link: dict[str, str] | None = None
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k: v or "" for k, v in attrs}
        classes = set((attrs_dict.get("class") or "").split())
        if tag == "h2":
            self.in_h2 = True
            self._h2_text = []
        if tag == "div" and "card" in classes and self.current_section:
            self.in_card = True
            self.card_depth = 1
            self.card = {
                "bucket": self.current_section,
                "company": "",
                "title": "",
                "badges": [],
                "chips": [],
                "location": "",
                "salary_text": "",
                "salary_label": "",
                "fit_note": "",
                "risk_note": "",
                "details": "",
                "links": [],
            }
            self.stack = []
            return
        if self.in_card:
            if tag == "div":
                self.card_depth += 1
            self.stack.append(" ".join(classes))
            if tag == "a" and "go" in classes:
                self.current_link = {"href": attrs_dict.get("href", ""), "label": ""}
            if tag == "div" and "title" in classes:
                self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self.in_h2:
            self.current_section = " ".join("".join(self._h2_text).split())
            self.in_h2 = False
        if self.in_card and tag == "a" and self.current_link is not None:
            if self.current_link.get("href"):
                self.card["links"].append(dict(self.current_link))  # type: ignore[index]
            self.current_link = None
        if self.in_card and tag == "div" and self.in_title:
            self.in_title = False
        if self.in_card:
            if self.stack:
                self.stack.pop()
            if tag == "div":
                self.card_depth -= 1
                if self.card_depth == 0 and self.card is not None:
                    self.cards.append(self.card)
                    self.card = None
                    self.in_card = False

    def handle_data(self, data: str) -> None:
        text = " ".join((data or "").split())
        if not text:
            return
        if self.in_h2:
            self._h2_text.append(text)
            return
        if not self.in_card or self.card is None:
            return
        if self.current_link is not None:
            self.current_link["label"] = (self.current_link.get("label", "") + " " + text).strip()
        path = " ".join(self.stack)
        if "name" in path:
            self.card["company"] = text
        elif "badge" in path:
            self.card["badges"].append(text)
        elif self.in_title:
            self.card["title"] = text
        elif "chip" in path:
            self.card["chips"].append(text)
            if "loc" in path:
                self.card["location"] = (self.card["location"] + " " + text).strip()
        elif "amt" in path:
            self.card["salary_text"] = text
        elif "lbl" in path:
            self.card["salary_label"] = text
        elif "fit" in path:
            self.card["fit_note"] = (self.card["fit_note"] + " " + text).strip()
        elif "catch" in path:
            self.card["risk_note"] = (self.card["risk_note"] + " " + text).strip()
        elif "details" in path or "summary" in path:
            self.card["details"] = (self.card["details"] + " " + text).strip()


def _verified_date_from_html(html_text: str) -> str | None:
    match = re.search(r"verified\s+(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})", html_text, re.IGNORECASE)
    if not match:
        match = re.search(r"checked\s+on\s+(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})", html_text, re.IGNORECASE)
    if not match:
        return None
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
        "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    month = months.get(match.group(2).lower())
    if not month:
        return None
    return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(1)):02d}"


def parse_reviewed_leads_html(html_text: str, *, source_artifact: str = "top-tier-jobs.html") -> list[dict[str, Any]]:
    """Parse the curated top-tier jobs HTML artifact as reviewed leads only.

    The returned rows are suitable for the existing import-run preview path.
    They intentionally carry `full_jd_status=missing`, so tailoring/generation
    remains blocked until the user reviews a full posting.
    """
    parser = _ShortlistParser()
    parser.feed(html_text or "")
    verified_at = _verified_date_from_html(html_text or "")
    leads: list[dict[str, Any]] = []
    for index, card in enumerate(parser.cards, start=1):
        badges = [b for b in card.get("badges", []) if str(b).lower() != "live"]
        rank_badge = str(badges[0]) if badges else ""
        source_links = [
            {"label": link.get("label") or "View and apply", "url": _safe_source_url(link.get("href"))}
            for link in card.get("links", [])
            if link.get("href")
        ]
        jd_text = "\n".join(
            part for part in (
                f"Title: {card.get('title')}",
                f"Company: {card.get('company')}",
                f"Location: {card.get('location')}",
                f"Fit note: {card.get('fit_note')}",
                f"Risk note: {card.get('risk_note')}",
                f"Details: {card.get('details')}",
            )
            if part and not part.endswith("None")
        )
        leads.append(
            {
                "title": card.get("title") or "Untitled role",
                "company": card.get("company") or "",
                "jd_text": jd_text,
                "source_url": source_links[0]["url"] if source_links else "",
                "source_external_id": f"top-tier-jobs:{index}:{_content_hash((card.get('company') or '') + (card.get('title') or ''))[:12]}",
                "source_provider": "other",
                "source_type": "csv_import",
                "location": card.get("location") or "",
                "salary_text": " ".join(part for part in (card.get("salary_text"), card.get("salary_label")) if part),
                "verification_status": "needs_review",
                "last_seen_at": verified_at,
                "source_metadata": {
                    "origin": "curated_reviewed_leads",
                    "site": "top-tier-jobs",
                    "priority_rank": index,
                    "rank_bucket": card.get("bucket") or "",
                    "rank_badge": rank_badge,
                    "fit_note": card.get("fit_note") or "",
                    "risk_note": card.get("risk_note") or "",
                    "source_artifact": Path(source_artifact).name,
                    "verified_at": verified_at,
                    "full_jd_status": "missing",
                    "source_links": source_links,
                    "manual_verification_required": True,
                    "crawl_policy": "local_curated_artifact_no_fetch",
                    "terms_basis": "user_supplied_curated_artifact",
                },
            }
        )
    return leads


def _safe_run_filters(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _metadata_secret_shaped(key):
            continue
        cleaned = _safe_metadata_value(item)
        if cleaned not in (None, {}, []):
            out[key] = cleaned
    return out


def _public_source_registry(row: object) -> dict[str, object]:
    if not isinstance(row, dict):
        return {}
    allowed = ("id", "label", "terms_basis", "supports_preview", "supports_fetch", "requires_review", "filters")
    out: dict[str, object] = {}
    for key in allowed:
        cleaned = _safe_metadata_value(row.get(key))
        if cleaned not in (None, {}, []):
            out[key] = cleaned
    return out


def _safe_run_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, object] = {}
    registry = value.get("source_registry")
    if isinstance(registry, dict):
        out["source_registry"] = _public_source_registry(registry)
    filters = _safe_run_filters(value.get("filters"))
    if filters:
        out["filters"] = filters
    crawl_policy = _safe_metadata_scalar(value.get("crawl_policy"))
    if crawl_policy is not None:
        out["crawl_policy"] = crawl_policy
    return out


def _public_import_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "provider": row.get("provider"),
        "query": _safe_source_scalar(row.get("query")),
        "location": _safe_source_scalar(row.get("location")),
        "status": row.get("status"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "requested_limit": row.get("requested_limit"),
        "imported_count": row.get("imported_count"),
        "skipped_count": row.get("skipped_count"),
        "error": row.get("error"),
        "metadata": _safe_run_metadata(row.get("metadata")),
        **({"demo_degraded": True} if row.get("demo_degraded") else {}),
    }


def _local_jobs_db_metadata(path: Path) -> dict[str, Any]:
    conn = _open_local_jobs_db(path)
    try:
        row_count = conn.execute("select count(*) from jobs").fetchone()[0]
        latest_seen_at = conn.execute("select max(last_seen) from jobs").fetchone()[0]
        latest_run = conn.execute(
            "select started_at from scrape_runs where status = 'completed' order by id desc limit 1"
        ).fetchone()
        age = _age_days(latest_seen_at)
        return {
            "row_count": row_count,
            "latest_seen_at": latest_seen_at,
            "latest_run_at": latest_run[0] if latest_run else None,
            "age_days": age,
            "stale": age is None or age > LOCAL_JOBS_FRESH_DAYS,
        }
    finally:
        conn.close()


def job_source_status() -> dict[str, Any]:
    """Server-owned source capability state for the Settings page.

    This reports local read-only projection availability and policy facts. It
    never exposes cookies, sessions, scraper credentials, or raw filesystem
    content.
    """
    path, configured = _local_jobs_db_path()
    local_available = bool(path and path.exists() and path.is_file())
    metadata = {
        "row_count": None,
        "latest_seen_at": None,
        "latest_run_at": None,
        "age_days": None,
        "stale": True,
    }
    warnings = ["no_cookies", "no_stealth", "manual_review_required"]
    if local_available and path:
        try:
            metadata = _local_jobs_db_metadata(path)
            if metadata["stale"]:
                warnings.append("local_db_stale")
        except sqlite3.Error:
            local_available = False
            warnings.append("local_db_unreadable")
    elif configured:
        warnings.append("local_db_missing")
    else:
        warnings.append("local_db_not_configured")

    adapter_configured = (
        os.environ.get("LINKEDIN_APPROVED_ADAPTER_ENABLED", "").strip().lower() in {"1", "true", "yes"}
        and bool(os.environ.get("LINKEDIN_PARTNER_CONFIG_ID", "").strip())
    )
    if not adapter_configured:
        warnings.append("approved_adapter_disabled")

    return {
        "sources": {
            "manual": {
                "enabled": True,
                "supports_preview": True,
                "supports_fetch": False,
                "filters": list(SOURCE_REGISTRY["manual"]["filters"]),
            },
            "linkedin": {
                "enabled": bool(adapter_configured or local_available),
                "supports_preview": True,
                "supports_fetch": False,
                "adapter_configured": adapter_configured,
                "local_db_configured": configured,
                "local_db_available": local_available,
                "latest_seen_at": metadata["latest_seen_at"],
                "latest_run_at": metadata["latest_run_at"],
                "age_days": metadata["age_days"],
                "stale": metadata["stale"],
                "row_count": metadata["row_count"],
                "warnings": sorted(set(warnings)),
            },
        },
        "policy": {
            "no_stored_cookies": True,
            "no_blind_linkedin_fetch": True,
            "no_stealth_automation": True,
            "operator_controlled_sources": True,
            "local_db_read_only": True,
        },
        # Capability signal only — never the allowlist contents. Lets the
        # Tailor page hide its dead URL-fetch field when no operator
        # allowlist is configured (plan 107, F2).
        "url_fetch": {"enabled": bool(_allowed_url_hosts())},
    }


def _sanitize_local_job_description(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    text = _MAILTO_RE.sub("[mailto redacted]", text)
    text = _EMAIL_RE.sub("[email redacted]", text)
    text = _PHONE_RE.sub("[phone redacted]", text)
    text = _BEARER_RE.sub("[secret redacted]", text)
    text = _INLINE_SECRET_RE.sub("[secret redacted]", text)
    kept: list[str] = []
    dropped: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.search(r"\b(contact|recruiter|email|phone|apply by email)\b", stripped, re.IGNORECASE):
            if len(stripped) < 180:
                dropped.append(line)
                continue
        kept.append(line)
    if not kept:
        # Every line matched the contact-line heuristic (e.g. a single-line
        # description): the inline redactions above already neutralized any
        # address/phone/secret, so keep the text rather than wiping the whole
        # description to an empty preview.
        kept = dropped
    return "\n".join(kept).strip()[:MAX_JD_BYTES]


def _safe_source_url(value: object) -> str:
    raw = _clean_field(value)
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if not parsed.scheme or not parsed.netloc:
        return ""
    if scheme not in {"http", "https"}:
        return ""
    host = parsed.hostname or ""
    if not host:
        return ""
    lowered = host.lower().rstrip(".")
    if lowered in {"localhost", "metadata", "metadata.google.internal", "169.254.169.254"} or lowered.endswith(".localhost"):
        return ""
    try:
        parsed_ip = ipaddress.ip_address(lowered)
    except ValueError:
        parsed_ip = None
        if _NUMERIC_IPV4_HOST_RE.fullmatch(lowered):
            try:
                parsed_ip = ipaddress.ip_address(socket.inet_aton(lowered))
            except OSError:
                parsed_ip = None
    if parsed_ip is not None and not ssrf._ip_is_public(parsed_ip):
        return ""
    netloc = host
    try:
        port = parsed.port
    except ValueError:  # non-numeric / out-of-range port → not a usable URL
        return ""
    if port:
        netloc = f"{netloc}:{port}"
    path = "" if _source_url_path_secret_shaped(parsed.path or "") else (parsed.path or "")
    return urlunparse((scheme, netloc, path, "", "", ""))


def _source_url_redacted(value: object) -> bool:
    if not isinstance(value, str):
        return False
    raw = value.strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return False
    return bool(parsed.username or parsed.password or parsed.query or parsed.fragment or _source_url_path_secret_shaped(parsed.path))


def _local_job_salary_text(row: sqlite3.Row) -> str | None:
    currency = row["currency"]
    min_amount = row["min_amount"]
    max_amount = row["max_amount"]
    interval = row["salary_interval"]
    if min_amount is None and max_amount is None:
        return None
    amount = ""
    if min_amount is not None and max_amount is not None and min_amount != max_amount:
        amount = f"{int(min_amount):,}-{int(max_amount):,}"
    else:
        amount = f"{int(min_amount if min_amount is not None else max_amount):,}"
    return " ".join(str(part) for part in (amount, currency, interval) if part)


_COUNTRY_ALIASES = {
    "de": ("de", "germany", "deutschland"),
    "germany": ("de", "germany", "deutschland"),
    "pl": ("pl", "poland", "polska"),
    "poland": ("pl", "poland", "polska"),
    "ch": ("ch", "switzerland", "schweiz", "suisse"),
    "switzerland": ("ch", "switzerland", "schweiz", "suisse"),
    "us": ("us", "usa", "united states", "america"),
    "usa": ("us", "usa", "united states", "america"),
}


def _norm_text(value: object) -> str:
    text = str(value or "").lower().replace("_", " ")
    return " ".join(re.sub(r"[^a-z0-9+]+", " ", text).split())


def _norm_tokens(value: object) -> list[str]:
    return _norm_text(value).split()


def _token_phrase_match(needle: object, haystack: object) -> bool:
    """Match normalized whole tokens/phrases, never arbitrary substrings."""
    needle_tokens = _norm_tokens(needle)
    hay_tokens = _norm_tokens(haystack)
    if not needle_tokens:
        return True
    if len(needle_tokens) > len(hay_tokens):
        return False
    width = len(needle_tokens)
    return any(hay_tokens[idx:idx + width] == needle_tokens for idx in range(0, len(hay_tokens) - width + 1))


def _any_token_phrase_match(needles: Iterable[object], haystack: object) -> bool:
    return any(_token_phrase_match(needle, haystack) for needle in needles if _norm_text(needle))


def _location_city_label(location: object) -> str:
    return _norm_text(str(location or "").split(",", 1)[0])


def _city_location_match(city: object, location: object) -> bool:
    city_norm = _norm_text(city)
    if not city_norm:
        return True
    return city_norm == _location_city_label(location)


def _criteria_list(value: object, *, max_items: int = 10) -> list[str]:
    if isinstance(value, str):
        raw_items = re.split(r"[,\n]", value)
    elif isinstance(value, (list, tuple)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = []
    out: list[str] = []
    for item in raw_items:
        cleaned = _clean_field(item)
        if cleaned and not _metadata_secret_shaped(cleaned):
            out.append(cleaned)
        if len(out) >= max_items:
            break
    return out


def _country_terms(value: object) -> list[str]:
    terms: list[str] = []
    for item in _criteria_list(value):
        lowered = item.strip().lower()
        terms.extend(_COUNTRY_ALIASES.get(lowered, (lowered,)))
    return sorted(set(_norm_text(term) for term in terms if _norm_text(term)))


def _normalize_search_criteria(*, query: object, filters: dict[str, Any]) -> dict[str, Any]:
    title = _clean_field(query) or _clean_field(filters.get("title")) or ""
    titles = _criteria_list(filters.get("titles"))
    if title and title not in titles:
        titles.insert(0, title)
    mode = (_clean_field(filters.get("search_mode") or filters.get("searchMode")) or "flexible").lower()
    if mode not in {"strict", "flexible"}:
        mode = "flexible"
    source = (_clean_field(filters.get("source") or filters.get("provider")) or "linkedin").lower()
    cities = _criteria_list(filters.get("cities") or filters.get("city"))
    countries = _criteria_list(filters.get("countries") or filters.get("country"))
    min_years = None
    raw_years = filters.get("min_years", filters.get("minYears"))
    try:
        if raw_years not in (None, ""):
            min_years = max(0.0, min(40.0, float(raw_years)))
    except (TypeError, ValueError):
        min_years = None
    ranking = filters.get("ranking") if isinstance(filters.get("ranking"), dict) else {}
    safe_ranking = {
        str(key): max(0.0, min(1.0, float(value)))
        for key, value in ranking.items()
        if isinstance(key, str)
        and re.fullmatch(r"[a-z_]{2,32}", key)
        and not _metadata_secret_shaped(key)
        and key.lower() not in {"key", "token", "secret", "password", "cookie", "authorization", "bearer", "session", "li_at", "jsessionid", "csrf"}
        and isinstance(value, (int, float))
    }
    return {
        "schema": "onepager.job_search_criteria.v1",
        "job_family": _clean_field(filters.get("job_family") or filters.get("family")) or "",
        "title": title,
        "titles": titles[:10],
        "search_mode": mode,
        "countries": countries[:10],
        "cities": cities[:10],
        "remote_mode": _clean_field(filters.get("remote_mode") or filters.get("remote")) or "",
        "seniority": _clean_field(filters.get("seniority")) or "",
        "min_years": min_years,
        "source": source,
        "freshness": (_clean_field(filters.get("freshness")) or "").lower(),
        "verification": (_clean_field(filters.get("verification")) or "").lower(),
        "rank_bucket": _clean_field(filters.get("rank_bucket") or filters.get("rankBucket")) or "",
        "ranking": safe_ranking,
    }


def _safe_search_criteria(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    criteria = _normalize_search_criteria(
        query=value.get("title") or value.get("query") or "",
        filters=value,
    )
    for key in ("job_family", "title", "remote_mode", "seniority", "source", "freshness", "verification", "rank_bucket"):
        if _metadata_secret_shaped(criteria.get(key, "")):
            criteria[key] = ""
    for key in ("titles", "countries", "cities"):
        criteria[key] = [
            item for item in (criteria.get(key) if isinstance(criteria.get(key), list) else [])
            if isinstance(item, str) and item and not _metadata_secret_shaped(item)
        ][:10]
    if criteria["search_mode"] not in {"strict", "flexible"}:
        criteria["search_mode"] = "flexible"
    if criteria["source"] not in {"", "linkedin", "manual", "all", "other"}:
        criteria["source"] = "linkedin"
    if criteria["remote_mode"] not in {"", "onsite", "hybrid", "remote"}:
        criteria["remote_mode"] = ""
    if criteria["freshness"] not in {"", "fresh", "stale"}:
        criteria["freshness"] = ""
    if criteria["verification"] not in {"", "needs_review", "stale", "user_provided"}:
        criteria["verification"] = ""
    return criteria


def _role_label(value: object) -> str:
    text = _norm_text(value)
    return " ".join(part.capitalize() for part in text.split()) if text else ""


def _years_requirement_from_text(text: str) -> float | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b", text or "", re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


_DATA_FAMILY_ROLE_TERMS = {
    "ai",
    "analyst",
    "analytics",
    "engineer",
    "learning",
    "machine",
    "ml",
    "platform",
    "scientist",
}


def _strict_family_match(raw_family: object, hay_title: str) -> bool:
    family = _norm_text(raw_family)
    if not family:
        return True
    if _token_phrase_match(family, hay_title):
        return True
    terms = [part for part in family.split() if len(part) >= 3]
    if not terms:
        return True
    raw = str(raw_family or "")
    if "/" in raw or "|" in raw:
        if set(terms) & {"analytics", "data", "learning", "machine"}:
            return _any_token_phrase_match(terms, hay_title) and _any_token_phrase_match(_DATA_FAMILY_ROLE_TERMS, hay_title)
        return _any_token_phrase_match(terms, hay_title)
    return all(_token_phrase_match(term, hay_title) for term in terms)


def _row_verification_status(row: sqlite3.Row, *, db_stale: bool) -> str:
    age = _age_days(row["last_seen"])
    return "stale" if db_stale or age is None or age > LOCAL_JOBS_FRESH_DAYS else "needs_review"


def _local_rank_score(row: sqlite3.Row, criteria: dict[str, Any]) -> float:
    weights = criteria.get("ranking") if isinstance(criteria.get("ranking"), dict) else {}
    if not weights:
        return 0.0
    title = _norm_text(row["title"])
    role = _norm_text(row["normalized_role"])
    description = _norm_text(row["description"])
    location = row["location"] or ""
    hay_title = " ".join(part for part in (title, role) if part)
    titles = [_norm_text(item) for item in criteria.get("titles") or [] if _norm_text(item)]
    title_hit = bool(titles and _any_token_phrase_match(titles, hay_title))
    if not title_hit and criteria["search_mode"] == "flexible":
        title_hit = bool(titles and _any_token_phrase_match(titles, description))
    city_hit = True
    cities = [_norm_text(item) for item in criteria.get("cities") or [] if _norm_text(item)]
    if cities:
        city_hit = any(_city_location_match(city, location) for city in cities)
    country_hit = True
    countries = _country_terms(criteria.get("countries"))
    if countries:
        country_hit = _any_token_phrase_match(countries, location)
    stale = _row_verification_status(row, db_stale=bool(criteria.get("_db_stale"))) == "stale"
    min_years = criteria.get("min_years")
    required_years = _years_requirement_from_text(row["description"] or "")
    experience_hit = True
    if min_years is not None and required_years is not None:
        experience_hit = required_years >= min_years
    score = 0.0
    score += float(weights.get("title", 0.0)) * (1.0 if title_hit else 0.0)
    score += float(weights.get("location", 0.0)) * (1.0 if city_hit and country_hit else 0.0)
    score += float(weights.get("freshness", 0.0)) * (1.0 if not stale else 0.0)
    score += float(weights.get("experience", 0.0)) * (1.0 if experience_hit else 0.0)
    return score


def _local_match_reasons(row: sqlite3.Row, criteria: dict[str, Any]) -> tuple[bool, list[str], bool]:
    title = _norm_text(row["title"])
    company = _norm_text(row["company"])
    description = _norm_text(row["description"])
    location = row["location"] or ""
    role = _norm_text(row["normalized_role"])
    hay_title = " ".join(part for part in (title, role) if part)
    reasons: list[str] = []
    broader = False

    titles = [_norm_text(item) for item in criteria.get("titles") or [] if _norm_text(item)]
    if titles:
        if _any_token_phrase_match(titles, hay_title):
            reasons.append("normalized title match")
        elif criteria["search_mode"] == "flexible" and _any_token_phrase_match(titles, company):
            reasons.append("broader company match")
            broader = True
        elif criteria["search_mode"] == "flexible" and _any_token_phrase_match(titles, description):
            reasons.append("broader description match")
            broader = True
        elif criteria["search_mode"] == "flexible" and _any_token_phrase_match(titles, location):
            reasons.append("broader location match")
            broader = True
        else:
            return False, [], False

    raw_family = criteria.get("job_family")
    family = _norm_text(raw_family)
    if family:
        family_terms = [part for part in family.split() if len(part) >= 2]
        if criteria["search_mode"] == "strict" and _strict_family_match(raw_family, hay_title):
            reasons.append("job family match")
        elif criteria["search_mode"] == "flexible" and _any_token_phrase_match(family_terms, hay_title):
            reasons.append("job family match")
        elif criteria["search_mode"] == "flexible" and _any_token_phrase_match(family_terms, description):
            reasons.append("broader family match")
            broader = True
        elif criteria["search_mode"] == "strict":
            return False, [], False

    cities = [_norm_text(item) for item in criteria.get("cities") or [] if _norm_text(item)]
    countries = _country_terms(criteria.get("countries"))
    if cities and not any(_city_location_match(city, location) for city in cities):
        return False, [], False
    if cities:
        reasons.append("city/location match")
    if countries and not _any_token_phrase_match(countries, location):
        return False, [], False
    if countries:
        reasons.append("country/location match")

    verification = criteria.get("verification")
    if verification:
        status = _row_verification_status(row, db_stale=bool(criteria.get("_db_stale")))
        if status != verification:
            return False, [], False
        reasons.append("verification match")

    min_years = criteria.get("min_years")
    if min_years is not None:
        required_years = _years_requirement_from_text(row["description"] or "")
        if required_years is not None and required_years < min_years:
            return False, [], False
        reasons.append("years requirement match")

    if not reasons and (criteria.get("title") or criteria.get("job_family")):
        return False, [], False
    if not reasons:
        reasons.append("source criteria match")
    return True, reasons, broader


def _local_job_preview(row: sqlite3.Row, *, filters: dict[str, Any], criteria: dict[str, Any], db_stale: bool) -> dict[str, Any]:
    description = _sanitize_local_job_description(row["description"])
    title = _safe_source_scalar(row["title"]) or "Untitled job"
    company = _safe_source_scalar(row["company"]) or ""
    if not description:
        safe_location = _safe_source_scalar(row["location"])
        description = "\n".join(part for part in (
            f"Title: {title}",
            f"Company: {company}" if company else "",
            f"Location: {safe_location}" if safe_location else "",
        ) if part)
    stale = _row_verification_status(row, db_stale=db_stale) == "stale"
    raw_site = _safe_source_scalar(row["site"]) or "local"
    site = raw_site.lower()
    _ok, match_reasons, broader_match = _local_match_reasons(row, criteria)
    role_label = _role_label(_safe_source_scalar(row["normalized_role"]))
    source_city = ""
    if row["location"]:
        source_city = str(row["location"]).split(",", 1)[0].strip()
    source_id = _safe_source_external_id(row["fingerprint"], fallback_text=f"{title}\n{company}\n{description}")
    return {
        "id": source_id,
        "source_external_id": source_id,
        "title": title,
        "company": company,
        "jd_text": description,
        "source_url": _safe_source_url(row["job_url"]),
        "source_provider": site if site in {"linkedin", "manual", "other"} else "other",
        "source_type": "csv_import",
        "job_family": role_label,
        "preferred_titles": criteria.get("titles") or [],
        "search_mode": criteria["search_mode"],
        "source_country": _safe_source_scalar(_clean_field(filters.get("country")) or ", ".join(criteria.get("countries") or [])),
        "source_city": _safe_source_scalar(source_city),
        "source_region": _safe_source_scalar(filters.get("region")),
        "source_location": _safe_source_scalar(row["location"]),
        "remote_mode": _safe_source_scalar(row["work_from_home_type"] or ("remote" if row["is_remote"] else None)),
        "salary_text": _safe_source_scalar(_local_job_salary_text(row)),
        "seniority": _safe_source_scalar(row["job_level"]),
        "verification_status": "stale" if stale else "needs_review",
        "content_hash": _content_hash(description),
        "experience_requirements": _job_experience_requirements(description),
        "search_criteria": _safe_search_criteria(criteria),
        "match_reasons": match_reasons,
        "broader_match": broader_match,
        "last_seen_at": _safe_source_scalar(row["last_seen"]),
        "expires_at": None,
        "source_metadata": {
            "origin": "local_jobs_db_read_only",
            "site": site if site in {"linkedin", "manual", "approved_adapter", "other", "local"} else "other",
            "date_posted": _safe_source_scalar(row["date_posted"]),
            "first_seen": _safe_source_scalar(row["first_seen"]),
            "normalized_role": _safe_source_scalar(row["normalized_role"]),
            "source_db": "jobs.db",
            "manual_verification_required": True,
            "crawl_policy": "read_only_projection_no_fetch_no_cookies",
        },
    }


def local_jobs_search(*, query: object = None, filters: object = None, limit: object = None) -> dict[str, Any]:
    path, configured = _local_jobs_db_path()
    if not path or not path.exists() or not path.is_file():
        raise JobError(
            400,
            {
                "error": "Local jobs DB is not available. Configure ONEPAGER_LOCAL_JOBS_DB or use manual pasted postings.",
                "code": "local_jobs_db_unavailable",
                "configured": configured,
            },
        )
    filters_dict = filters if isinstance(filters, dict) else {}
    criteria = _safe_search_criteria(_normalize_search_criteria(query=query, filters=filters_dict))
    if not criteria["source"]:
        criteria["source"] = "linkedin"
    criteria["_db_stale"] = False
    cap = _clean_limit(limit)
    status = job_source_status()
    db_stale = bool(status["sources"]["linkedin"]["stale"])
    criteria["_db_stale"] = db_stale
    clauses = ["1 = 1"]
    params: list[Any] = []
    source = criteria["source"]
    if source == "other":
        clauses.append("lower(j.site) not in ('linkedin', 'manual', 'approved_adapter')")
    elif source not in {"all", "local"}:
        clauses.append("lower(j.site) = ?")
        params.append(source)
    region = _safe_source_scalar(filters_dict.get("region"))
    if region:
        clauses.append("lower(coalesce(j.location, '')) like ?")
        params.append(f"%{region.lower()}%")
    title_terms = [_norm_text(item) for item in criteria.get("titles") or [] if _norm_text(item)]
    if title_terms:
        title_fields = [
            "lower(coalesce(j.title, ''))",
            "lower(replace(coalesce(j.normalized_role, ''), '_', ' '))",
        ]
        if criteria["search_mode"] == "flexible":
            title_fields.extend([
                "lower(coalesce(c.name, ''))",
                "lower(coalesce(j.description, ''))",
                "lower(coalesce(j.location, ''))",
            ])
        parts: list[str] = []
        for term in title_terms:
            for field in title_fields:
                parts.append(f"{field} like ?")
                params.append(f"%{term.lower()}%")
        clauses.append("(" + " or ".join(parts) + ")")
    family = _norm_text(criteria.get("job_family"))
    if family:
        family_terms = [family] + [part for part in family.split() if len(part) >= 2]
        family_terms = sorted(set(family_terms))
        family_fields = [
            "lower(coalesce(j.title, ''))",
            "lower(replace(coalesce(j.normalized_role, ''), '_', ' '))",
        ]
        if criteria["search_mode"] == "flexible":
            family_fields.append("lower(coalesce(j.description, ''))")
        parts = []
        for term in family_terms:
            for field in family_fields:
                parts.append(f"{field} like ?")
                params.append(f"%{term.lower()}%")
        clauses.append("(" + " or ".join(parts) + ")")
    city_terms = [_clean_field(city) for city in criteria.get("cities", []) if _clean_field(city)]
    if city_terms:
        clauses.append("(" + " or ".join(["lower(coalesce(j.location, '')) like ?"] * len(city_terms)) + ")")
        params.extend([f"%{city.lower()}%" for city in city_terms])
    country_terms = _country_terms(criteria.get("countries"))
    if country_terms:
        location_tokens = "lower(replace(replace(replace(coalesce(j.location, ''), ',', ' '), '.', ' '), '-', ' '))"
        clauses.append("(" + " or ".join([f"instr(' ' || {location_tokens} || ' ', ?) > 0"] * len(country_terms)) + ")")
        params.extend([f" {country.lower()} " for country in country_terms])
    remote = (criteria["remote_mode"] or "").lower()
    if remote and remote != "any":
        if remote == "remote":
            clauses.append("(j.is_remote = 1 or lower(coalesce(j.work_from_home_type, '')) like '%remote%')")
        else:
            clauses.append("lower(coalesce(j.work_from_home_type, '')) like ?")
            params.append(f"%{remote}%")
    seniority = criteria["seniority"]
    if seniority:
        clauses.append("lower(coalesce(j.job_level, '')) like ?")
        params.append(f"%{seniority.lower()}%")
    freshness = criteria["freshness"]
    if freshness == "fresh":
        clauses.append("j.last_seen is not null and date(j.last_seen) >= date('now', ?)")
        params.append(f"-{LOCAL_JOBS_FRESH_DAYS} days")
    elif freshness == "stale":
        clauses.append("(j.last_seen is null or date(j.last_seen) < date('now', ?))")
        params.append(f"-{LOCAL_JOBS_FRESH_DAYS} days")

    sql = f"""
        select
          j.fingerprint, j.site, j.job_url, j.title, c.name as company,
          j.location, j.work_from_home_type, j.is_remote, j.job_level,
          j.description, j.date_posted, j.first_seen, j.last_seen,
          j.normalized_role,
          s.min_amount, s.max_amount, s.currency, s.interval as salary_interval
        from jobs j
        left join companies c on c.id = j.company_id
        left join (
          select fingerprint, min(min_amount) as min_amount, max(max_amount) as max_amount,
                 max(currency) as currency, max(interval) as interval
          from job_salaries
          group by fingerprint
        ) s on s.fingerprint = j.fingerprint
        where {" and ".join(clauses)}
        order by date(j.last_seen) desc, j.title asc
        limit ?
    """
    read_cap = max(cap, max(50, LOCAL_JOBS_SEARCH_CANDIDATE_LIMIT))
    params.append(read_cap)
    conn = _open_local_jobs_db(path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    matched_rows = [row for row in rows if _local_match_reasons(row, criteria)[0]]
    if criteria.get("ranking"):
        matched_rows.sort(key=lambda row: _local_rank_score(row, criteria), reverse=True)
    matched_rows = matched_rows[:cap]
    results = [_local_job_preview(row, filters=filters_dict, criteria=criteria, db_stale=db_stale) for row in matched_rows]
    return {
        "results": results,
        "count": len(results),
        "limit": cap,
        "search_criteria": _safe_search_criteria(criteria),
        "stale": db_stale,
        "source_status": status["sources"]["linkedin"],
        "policy": status["policy"],
    }


class JobError(Exception):
    """A recoverable saved-jobs failure → maps to a 4xx JSON response.

    ``status`` is the HTTP status; ``payload`` is the JSON body the route
    returns — so an SSRF block, a 404, or a validation error maps cleanly.
    """

    def __init__(self, status: int, payload: dict) -> None:
        super().__init__(payload.get("error", "job error"))
        self.status = status
        self.payload = payload


# ---------------------------------------------------------------------------
# JD resolution (paste OR SSRF-guarded URL fetch). Paste wins when both given.
# ---------------------------------------------------------------------------
def resolve_jd_text(jd_text: object, jd_url: object) -> str:
    """Return the JD text from a paste, else by fetching ``jd_url`` (SSRF-guarded).

    A ``jd_url`` is fetched via :func:`ssrf.safe_fetch_url`, which validates the
    target (and disables redirects) BEFORE any request is made — an internal /
    metadata / RFC-1918 URL is blocked. Raises :class:`JobError` for neither
    input, an SSRF block (400), a transport failure (400), or an oversized JD
    (413). Indeed-MCP import is intentionally NOT a path here.
    """
    if isinstance(jd_text, str) and jd_text.strip():
        text = jd_text
    elif isinstance(jd_url, str) and jd_url.strip():
        if is_linkedin_url(jd_url):
            raise JobError(
                400,
                {
                    "error": "LinkedIn imports require pasted job text or an approved adapter.",
                    "code": "linkedin_manual_required",
                },
            )
        fetch_url = _safe_source_url(jd_url)
        if not fetch_url:
            raise JobError(400, {"error": "That URL isn't allowed.", "code": "bad_source_url"})
        _assert_job_url_allowed(fetch_url)
        try:
            raw = ssrf.safe_fetch_url(fetch_url, timeout=8, max_bytes=MAX_JD_BYTES)
        except ssrf.SSRFBlocked as exc:
            raise JobError(400, {"error": str(exc) or "That URL isn't allowed.", "code": "ssrf_blocked"}) from exc
        except OSError as exc:
            raise JobError(
                400, {"error": f"Couldn't fetch that URL ({type(exc).__name__}).", "code": "fetch_failed"}
            ) from exc
        text = raw.decode("utf-8", errors="replace")
    else:
        raise JobError(
            400, {"error": "provide 'jd_text' (paste the job description) or 'jd_url'", "code": "no_jd"}
        )

    if len(text.encode("utf-8")) > MAX_JD_BYTES:
        raise JobError(413, {"error": "job description too large", "max_bytes": MAX_JD_BYTES})
    return text


def is_linkedin_url(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        host = (urlparse(value.strip()).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in LINKEDIN_HOST_SUFFIXES)


def _allowed_url_hosts() -> set[str]:
    raw = os.environ.get(JOB_URL_ALLOWLIST_ENV, "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _assert_job_url_allowed(value: str) -> None:
    """Require explicit operator allowlisting before URL-only job fetching."""
    host = (urlparse(value.strip()).hostname or "").lower()
    allowed = _allowed_url_hosts()
    if not host or not allowed:
        raise JobError(
            400,
            {
                "error": "URL import is disabled for this source. Paste the job text or configure an explicit source allowlist.",
                "code": "source_not_allowed",
            },
        )
    if not any(host == item or host.endswith("." + item) for item in allowed):
        raise JobError(
            400,
            {
                "error": "That job source is not on the configured allowlist. Paste the job text instead.",
                "code": "source_not_allowed",
            },
        )
    _enforce_source_rate_limit(host)


def _enforce_source_rate_limit(host: str) -> None:
    now = time.time()
    window_start = now - 60
    bucket = [ts for ts in _URL_FETCH_BUCKETS.get(host, []) if ts >= window_start]
    if len(bucket) >= JOB_URL_RATE_LIMIT_PER_MINUTE:
        _URL_FETCH_BUCKETS[host] = bucket
        raise JobError(429, {"error": "Too many requests for that source. Paste the job text or retry later.", "code": "source_rate_limited"})
    bucket.append(now)
    _URL_FETCH_BUCKETS[host] = bucket


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _job_experience_requirements(jd_text: str, jd_json: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return extract_experience_requirements(jd_text or "", jd_json=jd_json)
    except Exception:  # noqa: BLE001 - years evidence is additive only
        return {}


def _job_experience_fit(row: dict[str, Any], cv_model: dict[str, Any] | None = None) -> dict[str, Any]:
    requirements = row.get("experience_requirements") if isinstance(row.get("experience_requirements"), dict) else {}
    required = requirements.get("total_years_min")
    if cv_model is None:
        return {
            "required_years": required,
            "candidate_years": None,
            "missing_years": None,
            "fit": "unknown",
            "uncertain": bool(requirements.get("uncertain")),
            "ranking_contribution": {
                "component": "experience_years",
                "score": 0.0,
                "weight": 0.18,
                "weighted_score": 0.0,
                "bucket": "unknown",
                "missing_years": None,
                "uncertain": bool(requirements.get("uncertain")),
                "reason": "Select or score a source CV to calculate candidate-years contribution.",
                "evidence": [str(item) for item in requirements.get("evidence", [])[:4]],
            },
            "evidence": {"requirements": requirements.get("evidence", []), "candidate": [], "notes": []},
            "experience_requirements": requirements,
        }
    return build_experience_fit(cv_model, row.get("jd_text") or "", jd_json=row.get("jd_json") or None)


def _full_jd_missing(row: dict[str, Any]) -> bool:
    metadata = row.get("source_metadata") if isinstance(row.get("source_metadata"), dict) else {}
    return metadata.get("full_jd_status") == "missing"


def _source_state(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("source_metadata") if isinstance(row.get("source_metadata"), dict) else {}
    verification = str(row.get("verification_status") or "").strip()
    age = _age_days(row.get("last_seen_at"))
    expired_at = _parse_job_date(row.get("expires_at"))
    expired = bool(expired_at and expired_at.timestamp() < datetime.now(timezone.utc).timestamp())
    full_jd_status = str(metadata.get("full_jd_status") or ("reviewed" if row.get("jd_text") else "missing"))
    stale = verification == "stale" or expired or (age is not None and age > LOCAL_JOBS_FRESH_DAYS)
    if stale:
        label = "stale"
    elif age is not None:
        label = "fresh"
    else:
        label = ""
    return {
        "state": label,
        "stale": stale,
        "age_days": age,
        "last_seen_at": row.get("last_seen_at"),
        "expires_at": row.get("expires_at"),
        "verified_at": metadata.get("verified_at"),
        "full_jd_status": full_jd_status,
        "requires_full_jd_review": full_jd_status == "missing",
        "revalidation_mode": "manual_review_only",
    }


def _rank_sort_value(row: dict[str, Any]) -> tuple[int, int]:
    metadata = row.get("source_metadata") if isinstance(row.get("source_metadata"), dict) else {}
    rank = row.get("priority_rank", metadata.get("priority_rank"))
    try:
        rank_int = int(rank)
    except (TypeError, ValueError):
        rank_int = 999_999
    bucket = str(row.get("rank_bucket") or metadata.get("rank_bucket") or "").strip().lower()
    bucket_order = {
        "apply first": 0,
        "strong volume": 1,
        "frontier stretch": 2,
        "berlin backups": 3,
    }.get(bucket, 9)
    return bucket_order, rank_int


def _require_full_jd(row: dict[str, Any]) -> None:
    if _full_jd_missing(row):
        raise JobError(
            422,
            {
                "error": "Full job description is required before tailoring or packet generation.",
                "code": "full_jd_required",
            },
        )


def _is_missing_import_schema(exc: BaseException) -> bool:
    text = str(exc)
    return (
        ("job_import_runs" in text or "job_import_results" in text or "saved_jobs.import_run_id" in text)
        and (
            "PGRST205" in text
            or "schema cache" in text
            or "Could not find the table" in text
            or "column" in text and "does not exist" in text
        )
    )


def _can_use_demo_import_fallback(exc: BaseException) -> bool:
    return _demo_mode_enabled() and _is_missing_import_schema(exc)


def _clean_field(value: object) -> str | None:
    """Trim + cap a caller-supplied title/company; None if empty."""
    if not isinstance(value, str):
        return None
    s = value.strip()[:MAX_FIELD_LEN]
    return s or None


def _clean_why_fit(value: object) -> str | None:
    """Trim + length-cap the user-authored ``why_fit`` note; None if empty.

    Capped at :data:`MAX_WHY_FIT_LEN` to mirror the DB length CHECK so a runaway
    paste is trimmed at the app boundary instead of raising a constraint error.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()[:MAX_WHY_FIT_LEN]
    return s or None


def _clean_confidence(value: object) -> int | None:
    """Coerce a caller-supplied confidence to an int in 0-100, else None.

    "Coerce to a safe value, never reject": a non-numeric or out-of-range
    confidence drops silently to None (advisory metadata) rather than 500-ing.
    Accepts ints and numeric strings; floats are truncated.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(float(value)) if isinstance(value, str) and value.strip() else int(value)
    except (TypeError, ValueError):
        return None
    if n < CONFIDENCE_MIN or n > CONFIDENCE_MAX:
        return None
    return n


def _clean_deadline(value: object) -> str | None:
    """Validate a caller-supplied deadline as an ISO ``YYYY-MM-DD`` date, else None.

    The deadline is advisory; a malformed value drops silently to None (never a
    500). Returns the normalized ISO date string the ``date`` column accepts.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    from datetime import date

    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Jobs CRUD (RLS owner-scoped via user_supabase()).
# ---------------------------------------------------------------------------
def create_job(
    *,
    jd_text: object = None,
    jd_url: object = None,
    title: object = None,
    company: object = None,
    field: object = None,
    deadline: object = None,
    confidence: object = None,
    why_fit: object = None,
) -> dict[str, Any]:
    """Save a job from a paste or URL; parse the JD and store the keyed JSON.

    Resolves the JD (paste / SSRF-guarded URL), runs :func:`parse_jd` (filling
    title/company when the caller didn't supply them), and inserts a
    ``saved_jobs`` row with ``status='saved'``. The optional catalog-metadata
    fields (``field``/``deadline``/``confidence``/``why_fit``) are sanitized:
    confidence coerces to an int 0-100 (else None), deadline validates as an ISO
    date (else None), and why_fit is length-capped — malformed advisory values
    drop silently rather than 500-ing. Returns the new row's public shape.
    """
    text = resolve_jd_text(jd_text, jd_url)
    given_title = _clean_field(title)
    given_company = _clean_field(company)

    # Deterministic parse — an explicit value always wins over the heuristic.
    try:
        jd_json = parse_jd(text, title=given_title, company=given_company)
    except ValueError as exc:
        raise JobError(400, {"error": str(exc), "code": "empty_jd"}) from exc
    experience_requirements = _job_experience_requirements(text, jd_json)

    source_url = _safe_source_url(jd_url)
    source_url = source_url or None
    linkedin_reference = is_linkedin_url(source_url)
    payload: dict[str, Any] = {
        "title": given_title or (jd_json.get("title") or None),
        "company": given_company or (jd_json.get("company") or None),
        "jd_text": text,
        "jd_json": jd_json,
        "source_url": source_url,
        "status": "saved",
        "field": _clean_field(field),
        "deadline": _clean_deadline(deadline),
        "confidence": _clean_confidence(confidence),
        "why_fit": _clean_why_fit(why_fit),
        "source_provider": "linkedin" if linkedin_reference else ("manual" if source_url is None else "other"),
        "source_type": "manual_paste" if isinstance(jd_text, str) and jd_text.strip() else "manual_url_reference",
        "source_metadata": {
            "terms_basis": "manual_user_supplied" if linkedin_reference else "user_supplied",
            "manual_verification_required": bool(linkedin_reference),
            # Stamped at write time because the jobs LIST select excludes
            # jd_text: _source_state's presence fallback would otherwise call
            # manual jobs "missing" in list/card shapes while the detail shape
            # says "reviewed" (plan 107, F17).
            "full_jd_status": "reviewed",
        },
        "verification_status": "user_provided" if not linkedin_reference else "needs_review",
        "content_hash": _content_hash(text),
        "experience_requirements": experience_requirements,
    }
    sb = _sb()
    resp = sb.table("saved_jobs").insert(payload).execute()
    rows = resp.data or []
    if not rows:
        raise JobError(500, {"error": "Failed to save the job.", "code": "insert_failed"})
    return _public_job(rows[0])


def list_import_runs() -> list[dict[str, Any]]:
    sb = _sb()
    try:
        resp = (
            sb.table("job_import_runs")
            .select("id, provider, query, location, status, started_at, finished_at, requested_limit, imported_count, skipped_count, error, metadata")
            .order("started_at", desc=True)
            .execute()
        )
        return [_public_import_run(row) for row in (resp.data or [])]
    except Exception as exc:  # noqa: BLE001 - demo fallback for unapplied local migrations
        if _can_use_demo_import_fallback(exc):
            rows = sorted(_DEMO_IMPORT_RUNS.values(), key=lambda r: r.get("started_at") or "", reverse=True)
            return [_public_import_run(row) for row in rows]
        raise


def start_import_run(
    *,
    provider: object,
    query: object = None,
    location: object = None,
    limit: object = None,
    filters: object = None,
    postings: object = None,
) -> dict[str, Any]:
    """Start an approved adapter import run.

    Default build deliberately has no blind LinkedIn crawler. LinkedIn requires
    an operator-audited partner/official adapter flag and config id; otherwise
    this fails closed before any outbound request can occur.
    """
    prov = _clean_field(provider) or ""
    if prov in {"manual", "linkedin"} and isinstance(postings, list):
        return _create_manual_import_run(provider=prov, query=query, location=location, limit=limit, filters=filters, postings=postings)
    if prov == "linkedin":
        enabled = os.environ.get("LINKEDIN_APPROVED_ADAPTER_ENABLED", "").lower() in {"1", "true", "yes"}
        config_id = os.environ.get("LINKEDIN_PARTNER_CONFIG_ID")
        if not enabled or not config_id:
            raise JobError(
                400,
                {
                    "error": "LinkedIn crawling is disabled. Use manual pasted job text or configure an approved adapter.",
                    "code": "linkedin_adapter_disabled",
                },
            )
    else:
        raise JobError(400, {"error": "No approved adapter is configured for that provider.", "code": "adapter_disabled"})

    raise JobError(501, {"error": "Approved adapter execution is not implemented in this build.", "code": "adapter_not_implemented"})


def _create_manual_import_run(
    *,
    provider: str,
    query: object,
    location: object,
    limit: object,
    filters: object,
    postings: list[Any],
) -> dict[str, Any]:
    if provider == "linkedin":
        for posting in postings:
            if not isinstance(posting, dict) or not isinstance(posting.get("jd_text"), str) or not posting.get("jd_text", "").strip():
                raise JobError(400, {"error": "LinkedIn preview requires pasted job text.", "code": "linkedin_manual_required"})
    cap = _clean_limit(limit)
    selected = [p for p in postings if isinstance(p, dict)][:cap]
    owner_id = _owner_id()
    sb = _server_sb()
    run_payload = {
        "owner_id": owner_id,
        "provider": provider,
        "query": _safe_source_scalar(query),
        "location": _safe_source_scalar(location),
        "status": "completed",
        "finished_at": _now_iso(),
        "requested_limit": cap,
        "imported_count": len(selected),
        "skipped_count": max(0, len(postings) - len(selected)),
        "metadata": {
            "source_registry": _public_source_registry(SOURCE_REGISTRY[provider]),
            "filters": _safe_run_filters(filters),
            "crawl_policy": "manual_user_supplied_no_fetch",
        },
    }
    try:
        run_rows = sb.table("job_import_runs").insert(run_payload).execute().data or []
        if not run_rows:
            raise JobError(500, {"error": "Failed to create import run.", "code": "import_run_failed"})
        run = run_rows[0]
        results = [
            {
                **_normalize_import_posting(run["id"], provider, p, filters if isinstance(filters, dict) else {}),
                "owner_id": owner_id,
            }
            for p in selected
        ]
        inserted_results: list[dict[str, Any]] = []
        for result in results:
            inserted_results.extend(sb.table("job_import_results").insert(result).execute().data or [])
        return {**_public_import_run(run), "results": [_public_import_result(r) for r in inserted_results]}
    except Exception as exc:  # noqa: BLE001 - demo fallback for unapplied local migrations
        if _can_use_demo_import_fallback(exc):
            return _create_demo_manual_import_run(run_payload, provider, selected, filters if isinstance(filters, dict) else {})
        raise


def _create_demo_manual_import_run(
    run_payload: dict[str, Any],
    provider: str,
    postings: list[dict[str, Any]],
    filters: dict[str, Any],
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    now = _now_iso()
    run = {
        **run_payload,
        "id": run_id,
        "started_at": now,
        "finished_at": run_payload.get("finished_at") or now,
        "demo_degraded": True,
    }
    results: list[dict[str, Any]] = []
    for posting in postings:
        row = {
            **_normalize_import_posting(run_id, provider, posting, filters),
            "id": str(uuid.uuid4()),
            "created_at": now,
            "updated_at": now,
            "selected": False,
            "saved_job_id": None,
            "demo_degraded": True,
        }
        _DEMO_IMPORT_RESULTS[row["id"]] = row
        results.append(row)
    _DEMO_IMPORT_RUNS[run_id] = run
    return {**_public_import_run(run), "results": [_public_import_result(r) for r in results]}


def _clean_limit(value: object) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 25
    return max(1, min(n, 50))


def _normalize_import_posting(run_id: str, provider: str, posting: dict[str, Any], filters: dict[str, Any]) -> dict[str, Any]:
    text = posting.get("jd_text")
    if not isinstance(text, str) or not text.strip():
        raise JobError(400, {"error": "Each import result needs pasted job text.", "code": "no_jd"})
    text = text.strip()
    if len(text.encode("utf-8")) > MAX_JD_BYTES:
        raise JobError(413, {"error": "job description too large", "max_bytes": MAX_JD_BYTES})
    title = _clean_field(posting.get("title") or filters.get("title"))
    company = _clean_field(posting.get("company"))
    jd_json = parse_jd(text, title=title, company=company)
    source_url = _safe_source_url(posting.get("source_url"))
    linkedin_reference = provider == "linkedin" or is_linkedin_url(source_url)
    source_external_id = _safe_source_external_id(posting.get("source_external_id"), fallback_text=text)
    posted_source_provider = _clean_field(posting.get("source_provider"))
    posted_source_type = _clean_field(posting.get("source_type"))
    posted_verification = _clean_field(posting.get("verification_status"))
    source_provider = "linkedin" if linkedin_reference else "manual"
    source_type = "manual_paste" if not posting.get("source_file") else "csv_import"
    verification_status = "needs_review" if linkedin_reference else "user_provided"
    if posted_source_type == "csv_import":
        source_provider = posted_source_provider if posted_source_provider in {"manual", "linkedin", "other"} else ("linkedin" if linkedin_reference else "other")
        source_type = "csv_import"
        verification_status = posted_verification if posted_verification in {"needs_review", "stale"} else "needs_review"
    posted_metadata = posting.get("source_metadata") if isinstance(posting.get("source_metadata"), dict) else {}
    safe_metadata = _safe_source_metadata(posted_metadata)
    search_criteria = _safe_search_criteria(posting.get("search_criteria"))
    match_reasons = [
        _safe_short_metadata_text(item)
        for item in (posting.get("match_reasons") if isinstance(posting.get("match_reasons"), list) else [])
        if _safe_short_metadata_text(item)
    ][:8]
    job_family = _safe_short_metadata_text(posting.get("job_family"))
    source_city = _safe_short_metadata_text(posting.get("source_city"))
    experience_requirements = _job_experience_requirements(text, jd_json)
    return {
        "import_run_id": run_id,
        "source_provider": source_provider,
        "source_type": source_type,
        "source_external_id": source_external_id,
        "title": title or (jd_json.get("title") or None),
        "company": company or (jd_json.get("company") or None),
        "jd_text": text,
        "jd_json": jd_json,
        "source_url": source_url,
        "source_metadata": {
            **safe_metadata,
            **({"job_family": job_family} if job_family else {}),
            **({"source_city": source_city} if source_city else {}),
            **({"search_criteria": search_criteria} if search_criteria else {}),
            **({"match_reasons": match_reasons} if match_reasons else {}),
            **({"broader_match": True} if bool(posting.get("broader_match")) else {}),
            "terms_basis": "manual_user_supplied" if linkedin_reference else "user_supplied",
            "manual_verification_required": bool(linkedin_reference),
            "crawl_policy": "manual_user_supplied_no_fetch",
        },
        "source_country": _safe_source_scalar(posting.get("source_country") or posting.get("country") or filters.get("country")),
        "source_region": _safe_source_scalar(posting.get("region") or filters.get("region")),
        "source_location": _safe_source_scalar(posting.get("source_location") or posting.get("location") or filters.get("location")),
        "remote_mode": _safe_source_scalar(posting.get("remote_mode") or filters.get("remote_mode")),
        "salary_text": _safe_source_scalar(posting.get("salary_text")),
        "seniority": _safe_source_scalar(posting.get("seniority") or filters.get("seniority")),
        "verification_status": verification_status,
        "content_hash": _content_hash(text),
        "experience_requirements": experience_requirements,
        "last_seen_at": _clean_field(posting.get("last_seen_at")),
        "expires_at": _clean_deadline(posting.get("expires_at")),
    }


def list_import_results(run_id: str) -> list[dict[str, Any]]:
    try:
        _assert_import_run_owned(run_id)
    except Exception as exc:  # noqa: BLE001 - demo fallback for unapplied local migrations
        if _can_use_demo_import_fallback(exc) and run_id in _DEMO_IMPORT_RUNS:
            return _demo_import_results(run_id)
        raise
    if _demo_mode_enabled() and run_id in _DEMO_IMPORT_RUNS:
        return _demo_import_results(run_id)
    sb = _sb()
    try:
        rows = (
            sb.table("job_import_results")
            .select("*")
            .eq("import_run_id", run_id)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        return sorted(
            [_public_import_result(r) for r in rows],
            key=lambda r: (_rank_sort_value(r), str(r.get("created_at") or "")),
        )
    except Exception as exc:  # noqa: BLE001 - demo fallback for unapplied local migrations
        if _can_use_demo_import_fallback(exc) and run_id in _DEMO_IMPORT_RUNS:
            return _demo_import_results(run_id)
        raise


def save_selected_import_results(run_id: str, result_ids: object) -> dict[str, Any]:
    try:
        _assert_import_run_owned(run_id)
    except Exception as exc:  # noqa: BLE001 - demo fallback for unapplied local migrations
        if _can_use_demo_import_fallback(exc) and run_id in _DEMO_IMPORT_RUNS:
            return _demo_save_selected_import_results(run_id, result_ids)
        raise
    if _demo_mode_enabled() and run_id in _DEMO_IMPORT_RUNS:
        return _demo_save_selected_import_results(run_id, result_ids)
    # Only genuine string ids count: a list of non-string / empty values (e.g.
    # [123, {}, null]) is a malformed selection, not a valid empty one.
    ids = [v.strip() for v in (result_ids if isinstance(result_ids, list) else []) if isinstance(v, str) and v.strip()]
    if not ids:
        raise JobError(400, {"error": "Select at least one import result.", "code": "no_selection"})
    read_sb = _sb()
    rows = (
        read_sb.table("job_import_results")
        .select("*")
        .eq("import_run_id", run_id)
        .in_("id", ids)
        .execute()
        .data
        or []
    )
    write_sb = _server_sb()
    saved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        row = _trusted_import_result_for_save(row)
        duplicate = _find_duplicate_job(row)
        if duplicate:
            skipped.append({"result_id": row.get("id"), "reason": "duplicate", "job_id": duplicate.get("id")})
            continue
        payload = _result_to_job_payload(row)
        inserted = write_sb.table("saved_jobs").insert(payload).execute().data or []
        if not inserted:
            skipped.append({"result_id": row.get("id"), "reason": "insert_failed"})
            continue
        job = inserted[0]
        write_sb.table("job_import_results").update(
            {"selected": True, "saved_job_id": job.get("id"), "updated_at": _now_iso()}
        ).eq("id", row.get("id")).eq("owner_id", _owner_id()).execute()
        saved.append(_public_job(job))
    return {"saved": saved, "skipped": skipped, "saved_count": len(saved), "skipped_count": len(skipped)}


def mark_import_results_stale(run_id: str, result_ids: object = None) -> dict[str, Any]:
    try:
        _assert_import_run_owned(run_id)
    except Exception as exc:  # noqa: BLE001 - demo fallback for unapplied local migrations
        if _can_use_demo_import_fallback(exc) and run_id in _DEMO_IMPORT_RUNS:
            return _demo_mark_import_results_stale(run_id, result_ids)
        raise
    if _demo_mode_enabled() and run_id in _DEMO_IMPORT_RUNS:
        return _demo_mark_import_results_stale(run_id, result_ids)
    ids = [str(v) for v in (result_ids if isinstance(result_ids, list) else []) if v]
    sb = _server_sb()
    query = (
        sb.table("job_import_results")
        .update({"verification_status": "stale", "updated_at": _now_iso()})
        .eq("import_run_id", run_id)
        .eq("owner_id", _owner_id())
    )
    if ids:
        query = query.in_("id", ids)
    rows = query.execute().data or []
    return {"stale_count": len(rows), "results": [_public_import_result(r) for r in rows]}


def revalidate_job_source(
    job_id: str,
    *,
    full_jd_text: object = None,
    mark_stale: object = False,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Manually revalidate a saved job source without fetching external sites."""
    job = get_job(job_id)
    if job is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    metadata = _safe_source_metadata(job.get("source_metadata"))
    now = now_iso or _now_iso()
    updates: dict[str, Any] = {"updated_at": now}
    if mark_stale is True:
        updates["verification_status"] = "stale"
    else:
        updates["verification_status"] = "user_provided"
        updates["last_seen_at"] = now
        metadata["verified_at"] = now[:10]

    if isinstance(full_jd_text, str) and full_jd_text.strip():
        text = full_jd_text.strip()
        if len(text.encode("utf-8")) > MAX_JD_BYTES:
            raise JobError(413, {"error": "job description too large", "max_bytes": MAX_JD_BYTES})
        try:
            jd_json = parse_jd(text, title=job.get("title"), company=job.get("company"))
        except ValueError as exc:
            raise JobError(400, {"error": str(exc), "code": "empty_jd"}) from exc
        metadata["full_jd_status"] = "reviewed"
        updates.update({
            "jd_text": text,
            "jd_json": jd_json,
            "content_hash": _content_hash(text),
            "experience_requirements": _job_experience_requirements(text, jd_json),
        })
    elif metadata.get("full_jd_status") == "missing":
        updates["verification_status"] = "needs_review"

    metadata["revalidation"] = "manual_review"
    updates["source_metadata"] = metadata
    rows = (
        _server_sb()
        .table("saved_jobs")
        .update(updates)
        .eq("id", job_id)
        .eq("owner_id", _owner_id())
        .execute()
        .data
        or []
    )
    if not rows:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    refreshed = get_job(job_id) or {**job, **updates}
    cover_by_job = _covers_for_jobs([job_id])
    return _public_job(
        refreshed,
        full=True,
        has_cover=job_id in cover_by_job,
        cover_id=cover_by_job.get(job_id),
    )


def _assert_import_run_owned(run_id: str) -> dict[str, Any]:
    sb = _sb()
    try:
        rows = sb.table("job_import_runs").select("id, provider").eq("id", run_id).limit(1).execute().data or []
    except Exception as exc:  # noqa: BLE001 - demo fallback for unapplied local migrations
        if _can_use_demo_import_fallback(exc) and run_id in _DEMO_IMPORT_RUNS:
            return _DEMO_IMPORT_RUNS[run_id]
        raise
    if not rows:
        raise JobError(404, {"error": "Import run not found", "code": "import_run_not_found"})
    return rows[0]


def _demo_import_results(run_id: str) -> list[dict[str, Any]]:
    rows = [r for r in _DEMO_IMPORT_RESULTS.values() if r.get("import_run_id") == run_id]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return [_public_import_result(r) for r in rows]


def _demo_save_selected_import_results(run_id: str, result_ids: object) -> dict[str, Any]:
    ids = [v.strip() for v in (result_ids if isinstance(result_ids, list) else []) if isinstance(v, str) and v.strip()]
    if not ids:
        raise JobError(400, {"error": "Select at least one import result.", "code": "no_selection"})
    saved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for result_id in ids:
        row = _DEMO_IMPORT_RESULTS.get(result_id)
        if not row or row.get("import_run_id") != run_id:
            skipped.append({"result_id": result_id, "reason": "not_found"})
            continue
        if row.get("selected") and row.get("saved_job_id"):
            skipped.append({"result_id": result_id, "reason": "duplicate", "job_id": row.get("saved_job_id")})
            continue
        duplicate = _demo_duplicate_job(row)
        if duplicate:
            row["selected"] = True
            row["saved_job_id"] = duplicate.get("id")
            row["updated_at"] = _now_iso()
            skipped.append({"result_id": result_id, "reason": "duplicate", "job_id": duplicate.get("id")})
            continue
        job_id = str(uuid.uuid4())
        row["selected"] = True
        row["saved_job_id"] = job_id
        row["updated_at"] = _now_iso()
        job = {**_result_to_job_payload(row), "id": job_id, "created_at": row.get("updated_at"), "updated_at": row.get("updated_at")}
        _DEMO_SAVED_JOBS[job_id] = job
        saved.append(_public_job(job))
    return {"saved": saved, "skipped": skipped, "saved_count": len(saved), "skipped_count": len(skipped)}


def _demo_duplicate_job(row: dict[str, Any]) -> dict[str, Any] | None:
    external_id = row.get("source_external_id")
    provider = row.get("source_provider")
    content_hash = row.get("content_hash")
    for job in _DEMO_SAVED_JOBS.values():
        if provider and external_id and job.get("source_provider") == provider and job.get("source_external_id") == external_id:
            return job
        if content_hash and job.get("content_hash") == content_hash:
            return job
    return None


def demo_saved_jobs() -> list[dict[str, Any]]:
    rows = sorted(_DEMO_SAVED_JOBS.values(), key=lambda r: r.get("created_at") or "", reverse=True)
    return [_public_job(row) for row in rows]


def _demo_mark_import_results_stale(run_id: str, result_ids: object = None) -> dict[str, Any]:
    ids = {str(v) for v in (result_ids if isinstance(result_ids, list) else []) if v}
    rows = [
        r for r in _DEMO_IMPORT_RESULTS.values()
        if r.get("import_run_id") == run_id and (not ids or r.get("id") in ids)
    ]
    now = _now_iso()
    for row in rows:
        row["verification_status"] = "stale"
        row["updated_at"] = now
    return {"stale_count": len(rows), "results": [_public_import_result(r) for r in rows]}


def _find_duplicate_job(result: dict[str, Any]) -> dict[str, Any] | None:
    sb = _sb()
    provider = result.get("source_provider")
    external_id = result.get("source_external_id")
    if provider and external_id:
        rows = (
            sb.table("saved_jobs")
            .select("id, source_provider, source_external_id")
            .eq("source_provider", provider)
            .eq("source_external_id", external_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows:
            return rows[0]
    content_hash = result.get("content_hash")
    if content_hash:
        rows = sb.table("saved_jobs").select("id, content_hash").eq("content_hash", content_hash).limit(1).execute().data or []
        if rows:
            return rows[0]
    return None


def _result_to_job_payload(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "import_run_id", "source_provider", "source_type", "source_external_id",
        "title", "company", "jd_text", "jd_json", "source_url", "source_metadata",
        "source_country", "source_region", "source_location", "remote_mode",
        "salary_text", "seniority", "verification_status", "content_hash",
        "experience_requirements", "last_seen_at", "expires_at",
    )
    payload = {k: row.get(k) for k in keys if k in row}
    payload["owner_id"] = _owner_id()
    payload["source_external_id"] = _safe_source_external_id(
        row.get("source_external_id"),
        fallback_text="\n".join(
            str(row.get(key) or "")
            for key in ("title", "company", "jd_text", "content_hash", "id")
        ),
    )
    payload["source_url"] = _safe_source_url(row.get("source_url"))
    payload["source_metadata"] = _safe_source_metadata(row.get("source_metadata"))
    for key in ("source_country", "source_region", "source_location", "remote_mode", "salary_text", "seniority"):
        payload[key] = _safe_source_scalar(row.get(key))
    payload["status"] = "saved"
    return payload


def _trusted_import_result_for_save(row: dict[str, Any]) -> dict[str, Any]:
    trusted = dict(row)
    source_type = _clean_field(trusted.get("source_type"))
    provider = _clean_field(trusted.get("source_provider"))
    verification = _clean_field(trusted.get("verification_status"))
    if source_type == "csv_import":
        trusted["source_provider"] = provider if provider in {"manual", "linkedin", "other"} else "other"
        trusted["verification_status"] = verification if verification in {"needs_review", "stale"} else "needs_review"
    else:
        trusted["source_provider"] = provider if provider in {"manual", "linkedin", "other"} else "manual"
        trusted["verification_status"] = verification if verification in {"user_provided", "needs_review", "stale"} else "user_provided"
    return trusted


def _public_import_result(row: dict[str, Any]) -> dict[str, Any]:
    metadata = _safe_source_metadata(row.get("source_metadata"))
    match_reasons = row.get("match_reasons") if isinstance(row.get("match_reasons"), list) else metadata.get("match_reasons")
    raw_search_criteria = (
        row.get("search_criteria")
        if isinstance(row.get("search_criteria"), dict)
        else metadata.get("search_criteria", {})
    )
    return {
        "id": row.get("id"),
        "import_run_id": row.get("import_run_id"),
        "title": row.get("title") or "",
        "company": row.get("company") or "",
        "source_provider": row.get("source_provider"),
        "source_type": row.get("source_type"),
        "source_external_id": _public_source_external_id(row.get("source_external_id")),
        "source_url": _safe_source_url(row.get("source_url")) or "",
        "source_url_redacted": _source_url_redacted(row.get("source_url")),
        "source_metadata": metadata,
        "source_state": _source_state({**row, "source_metadata": metadata}),
        "priority_rank": metadata.get("priority_rank"),
        "rank_bucket": metadata.get("rank_bucket"),
        "rank_badge": metadata.get("rank_badge"),
        "source_country": _safe_source_scalar(row.get("source_country")),
        "source_region": _safe_source_scalar(row.get("source_region")),
        "source_city": _safe_source_scalar(row.get("source_city") or metadata.get("source_city")),
        "source_location": _safe_source_scalar(row.get("source_location")),
        "job_family": _safe_source_scalar(row.get("job_family") or metadata.get("job_family")),
        "search_criteria": _safe_search_criteria(raw_search_criteria),
        "match_reasons": [
            item for item in (_safe_metadata_value(match_reasons) if isinstance(match_reasons, list) else [])
            if isinstance(item, str)
        ],
        "broader_match": bool(row.get("broader_match") or metadata.get("broader_match")),
        "remote_mode": _safe_source_scalar(row.get("remote_mode")),
        "salary_text": _safe_source_scalar(row.get("salary_text")),
        "seniority": _safe_source_scalar(row.get("seniority")),
        "verification_status": row.get("verification_status"),
        "content_hash": row.get("content_hash"),
        "experience_requirements": row.get("experience_requirements") if isinstance(row.get("experience_requirements"), dict) else {},
        "last_seen_at": row.get("last_seen_at"),
        "expires_at": row.get("expires_at"),
        "selected": bool(row.get("selected")),
        "saved_job_id": row.get("saved_job_id"),
    }


def list_jobs() -> list[dict[str, Any]]:
    """All of the caller's saved jobs, newest first, with a ``has_cover`` flag.

    Returns the catalog-grid shape: ``id, title, company, status, created_at,
    field, deadline, confidence, why_fit`` plus ``has_cover``. RLS scopes the
    SELECT to ``owner_id = auth.uid()``.

    ``has_cover`` is DERIVED (never denormalized) via ONE grouped, RLS-scoped
    count over the caller's own cover-letter documents — NEVER a SECURITY DEFINER
    function/view or a service-role query. A job is "covered" iff the caller owns
    a non-deleted ``cover_letter`` document stamped with that job's id.
    """
    sb = _sb()
    resp = (
        sb.table("saved_jobs")
        .select(
            "id, title, company, status, created_at, field, deadline, confidence, why_fit, "
            "import_run_id, source_provider, source_type, source_external_id, source_url, "
            "source_metadata, source_country, source_region, source_location, remote_mode, "
            "salary_text, seniority, verification_status, content_hash, experience_requirements, last_seen_at, expires_at"
        )
        .order("created_at", desc=True)
        .execute()
    )
    rows = resp.data or []
    cover_by_job = _covers_for_jobs([r.get("id") for r in rows])
    return [
        _public_job(r, has_cover=r.get("id") in cover_by_job, cover_id=cover_by_job.get(r.get("id")))
        for r in rows
    ]


def _covers_for_jobs(job_ids: list[str]) -> dict[str, str]:
    """Map each of ``job_ids`` the caller has a cover for → its cover doc id.

    ONE grouped query over the caller's own non-deleted ``cover_letter``
    documents whose ``job_id`` is in the set — RLS scopes it to the owner, so it
    can NEVER reflect another tenant's covers (never a SECURITY DEFINER
    function/view or a service-role count). Avoids an N+1 per job. When a job has
    multiple cover letters the most-recently-updated one wins (the grid's "Open"
    target). Returns ``{job_id: cover_document_id}``.
    """
    ids = [j for j in job_ids if j]
    if not ids:
        return {}
    sb = _sb()
    resp = (
        sb.table("documents")
        .select("id, job_id, updated_at")
        .eq("type", "cover_letter")
        .is_("deleted_at", "null")
        .in_("job_id", ids)
        .order("updated_at", desc=True)
        .execute()
    )
    out: dict[str, str] = {}
    # Rows are newest-first; keep the FIRST (most recent) cover per job.
    for r in resp.data or []:
        jid = r.get("job_id")
        if jid and jid not in out:
            out[jid] = r.get("id")
    return out


def _jobs_with_cover(job_ids: list[str]) -> set[str]:
    """The subset of ``job_ids`` the caller has a cover letter for (RLS-scoped)."""
    return set(_covers_for_jobs(job_ids).keys())


def get_job(job_id: str) -> dict[str, Any] | None:
    """The caller's full saved-job row by id, or None (→ 404).

    RLS hides another tenant's row, so ``None`` covers both "no such id" and
    "owned by someone else". The route surfaces both as 404.
    """
    sb = _sb()
    resp = (
        sb.table("saved_jobs")
        .select(
            "id, title, company, jd_text, jd_json, source_url, status, "
            "field, deadline, confidence, why_fit, created_at, updated_at, "
            "import_run_id, source_provider, source_type, source_external_id, "
            "source_metadata, source_country, source_region, source_location, remote_mode, "
            "salary_text, seniority, verification_status, content_hash, experience_requirements, last_seen_at, expires_at"
        )
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def assert_job_owned(job_id: str | None) -> None:
    """Verify the caller owns ``job_id`` before a document is stamped with it.

    The (SECURITY MUST-FIX) app-layer half of closing the cross-tenant ``job_id``
    hole (the DB ``with check`` in migration 0007 is the other half — defense in
    depth). ``None`` is allowed (an unlinked document). A non-null id that the
    caller doesn't own/can't see (RLS) raises :class:`JobError` 404, so a tailor
    / cover-letter request can never stamp another tenant's job onto a document.

    This runs UNDER the caller's JWT via :func:`get_job` (RLS-scoped), so a
    job owned by someone else is simply invisible → 404.
    """
    if job_id is None:
        return
    if get_job(job_id) is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})


def job_detail(job_id: str) -> dict[str, Any]:
    """A job plus its linked documents (CVs + cover letters), grouped by type.

    Reads the job (RLS), then the caller's documents whose ``job_id`` matches —
    the linkage written by the tailor / cover-letter flows. Raises
    :class:`JobError` 404 if the job isn't owned/visible. Returns::

        {
          "job": {id, title, company, status, jd_text, jd_json, source_url, …},
          "cvs": [{id, name, updated_at}, …],
          "cover_letters": [{id, name, company, role, updated_at}, …],
        }
    """
    job = get_job(job_id)
    if job is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})

    docs = _linked_documents(job_id)
    cvs = [
        {
            "id": d["id"],
            "name": d.get("name") or "Untitled CV",
            "updated_at": d.get("updated_at"),
            "content_hash": _content_hash(d.get("yaml_blob") or ""),
        }
        for d in docs
        if d.get("type") == "cv"
    ]
    cover_letters = []
    for d in docs:
        if d.get("type") != "cover_letter":
            continue
        blob = cl_service.parse_blob(d.get("yaml_blob"))
        cover_letters.append(
            {
                "id": d["id"],
                "name": d.get("name") or "Cover letter",
                "company": blob.get("company") or "",
                "role": blob.get("role") or "",
                "updated_at": d.get("updated_at"),
                "content_hash": _content_hash(d.get("yaml_blob") or ""),
            }
        )
    # has_cover + cover_id are free here — we already grouped the documents by
    # type (cover_letters is newest-first, so [0] is the "Open" target).
    return {
        "job": _public_job(
            job,
            full=True,
            has_cover=bool(cover_letters),
            cover_id=cover_letters[0]["id"] if cover_letters else None,
        ),
        "cvs": cvs,
        "cover_letters": cover_letters,
        "packets": _packet_rows(job_id, job=job),
    }


def set_status(job_id: str, status: str) -> dict[str, Any]:
    """Set a job's lifecycle status. Raises 404 if not owned.

    ``status`` must be one of :data:`JOB_STATUSES`
    (saved | open | speculative | applied | closed).
    Validates ``status`` against :data:`JOB_STATUSES` (a garbage value → 400),
    then UPDATEs the row under the user's JWT. An UPDATE that moves no row means
    the job isn't owned/visible → 404 (RLS).
    """
    if status not in JOB_STATUSES:
        raise JobError(
            400,
            {"error": f"status must be one of {list(JOB_STATUSES)}", "code": "bad_status"},
        )
    # Confirm ownership/visibility first so a no-op update is a clean 404.
    if get_job(job_id) is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    sb = _sb()
    resp = (
        sb.table("saved_jobs")
        .update({"status": status, "updated_at": _now_iso()})
        .eq("id", job_id)
        .execute()
    )
    if not (resp.data or []):
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    return {"id": job_id, "status": status}


# Fields a caller may edit on a saved job after creation (catalog curation).
# owner_id / id / created_at / jd_text / jd_json are NOT editable here — the
# curation surface only touches the advisory metadata + display fields.
_EDITABLE_FIELDS = ("title", "company", "field", "deadline", "confidence", "why_fit", "status", "starred")


def update_job(job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Edit a saved job's catalog metadata (RLS-scoped). 404 if not owned.

    Accepts a partial ``patch`` of the editable fields and sanitizes each with
    the same validators as :func:`create_job` (confidence → int 0-100 else None,
    deadline → ISO date else None, why_fit length-capped, status validated
    against :data:`JOB_STATUSES`). Only keys actually present in ``patch`` are
    written, so an edit of one field leaves the rest untouched. An empty/invalid
    patch raises 400. Confirms ownership first so a no-op update is a clean 404.
    """
    updates: dict[str, Any] = {}
    if "title" in patch:
        updates["title"] = _clean_field(patch["title"])
    if "company" in patch:
        updates["company"] = _clean_field(patch["company"])
    if "field" in patch:
        updates["field"] = _clean_field(patch["field"])
    if "deadline" in patch:
        updates["deadline"] = _clean_deadline(patch["deadline"])
    if "confidence" in patch:
        updates["confidence"] = _clean_confidence(patch["confidence"])
    if "why_fit" in patch:
        updates["why_fit"] = _clean_why_fit(patch["why_fit"])
    if "status" in patch:
        status = patch["status"]
        if status not in JOB_STATUSES:
            raise JobError(
                400, {"error": f"status must be one of {list(JOB_STATUSES)}", "code": "bad_status"}
            )
        updates["status"] = status
    if "starred" in patch:
        if not isinstance(patch.get("starred"), bool):
            raise JobError(400, {"error": "starred must be boolean", "code": "bad_starred"})
        current = get_job(job_id)
        if current is None:
            raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
        metadata = _safe_source_metadata(current.get("source_metadata"))
        metadata["starred"] = patch["starred"]
        updates["source_metadata"] = metadata

    if not updates:
        raise JobError(400, {"error": "no editable fields in request", "code": "empty_patch"})

    # Confirm ownership/visibility first so a no-op update is a clean 404.
    if get_job(job_id) is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    updates["updated_at"] = _now_iso()
    sb = _sb()
    resp = sb.table("saved_jobs").update(updates).eq("id", job_id).execute()
    if not (resp.data or []):
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    # Return the full enriched job (with has_cover + cover_id) so the grid can
    # re-render the row, including its Generate/Open split button.
    job = get_job(job_id)
    cover_by_job = _covers_for_jobs([job_id]) if job else {}
    return _public_job(
        job or resp.data[0],
        full=True,
        has_cover=job_id in cover_by_job,
        cover_id=cover_by_job.get(job_id),
    )


def delete_job(job_id: str) -> dict[str, Any]:
    """Delete a saved job (RLS-scoped). Raises 404 if not owned/visible.

    The schema sets ``documents.job_id`` to NULL on delete (ON DELETE SET NULL),
    so a job's tailored CVs / letters are NOT deleted — they're simply unlinked
    and remain in the user's library.
    """
    if get_job(job_id) is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    sb = _sb()
    resp = sb.table("saved_jobs").delete().eq("id", job_id).execute()
    if not (resp.data or []):
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    return {"deleted": job_id}


def archive_packet(job_id: str, packet_id: str, *, now_iso: str) -> dict[str, Any]:
    """Archive one packet row while keeping linked documents intact."""
    packet = next((row for row in _raw_packet_rows(job_id) if row.get("id") == packet_id), None)
    if not packet:
        raise JobError(404, {"error": "Application packet not found.", "code": "packet_not_found"})
    rows = (
        _server_sb()
        .table("application_packets")
        .update({"status": "archived", "archived_at": now_iso, "updated_at": now_iso})
        .eq("id", packet_id)
        .eq("owner_id", _owner_id())
        .execute()
        .data
        or []
    )
    if not rows:
        raise JobError(404, {"error": "Application packet not found.", "code": "packet_not_found"})
    return {"archived": packet_id, "packet": _public_packet(rows[0])}


def regenerate_packet(
    *,
    job_id: str,
    packet_id: str,
    now_iso: str,
    provider: str = "anthropic",
    model: str | None = None,
    profile: str = DEFAULT_SCORE_SYSTEM_ID,
) -> dict[str, Any]:
    """Prepare a fresh review token that replaces an existing packet on apply."""
    packet = next((row for row in _raw_packet_rows(job_id) if row.get("id") == packet_id), None)
    if not packet:
        raise JobError(404, {"error": "Application packet not found.", "code": "packet_not_found"})
    source_cv_id = packet.get("source_cv_id")
    if not _is_uuid_text(source_cv_id):
        raise JobError(
            409,
            {"error": "Packet source CV lineage is missing.", "code": "source_cv_missing", "packet": _public_packet(packet)},
        )
    review = prepare_packet_review(
        job_id=job_id,
        source_cv_id=str(source_cv_id),
        now_iso=now_iso,
        provider=provider,
        model=model,
        profile=profile,
        force=True,
        replaces_packet_id=packet_id,
    )
    review["regenerates_packet_id"] = packet_id
    review["packet"] = _public_packet(packet)
    return review


# ---------------------------------------------------------------------------
# One-click: tailor a CV to this job AND generate a grounded cover letter,
# then link BOTH produced documents to the job.
# ---------------------------------------------------------------------------
def generate_for_job(
    *,
    job_id: str,
    source_cv_id: str,
    now_iso: str,
    provider: str = "anthropic",
    model: str | None = None,
    profile: str = DEFAULT_SCORE_SYSTEM_ID,
    force: bool = False,
) -> dict[str, Any]:
    """Legacy direct packet creation is disabled behind the review gate.

    Orchestrates the two existing guarded generators against this job's
    ``jd_text`` and links BOTH new documents to the job (``documents.job_id``):

      1. :func:`tailor_service.tailor_cv` — duplicate-then-tailor; the
         anti-fabrication guard (422) and the 1-page gate (409) still apply, and
         the new CV is stamped with ``job_id``.
      2. :func:`cover_letter_service.generate_cover_letter` — grounded in the
         reviewed tailored CV + this job; the cliché / anti-fabrication guards
         (422) still apply, and the letter is stamped with ``job_id``.

    Guard-trip handling (NO half-linked orphan): the cover-letter step runs
    AFTER the CV is saved. If the letter is blocked (or any later failure
    occurs), the just-created tailored CV is rolled back (hard-deleted) so the
    job is never left with a CV but no letter. The original source CV is never
    touched. Returns ``{tailored_cv_id, cover_letter_id, ats}``.

    Raises :class:`JobError` 404 if the job isn't owned/visible. Lets the
    sub-steps' :class:`TailorError` / :class:`CoverLetterError` (404/409/422/502)
    and :class:`llm_gateway.LLMUnavailable` (402/503) propagate to their
    registered route handlers — surfacing exactly which step blocked.
    """
    job = get_job(job_id)
    if job is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    _require_full_jd(job)
    jd_text = job.get("jd_text") or ""
    if not jd_text.strip():
        raise JobError(
            422,
            {"error": "This job has no description to generate from.", "code": "empty_jd"},
        )
    if _document_yaml_hash(source_cv_id, "cv") is None:
        raise JobError(404, {"error": "CV not found", "code": "cv_not_found"})
    raise JobError(
        409,
        {
            "error": "Packet creation requires the review-first flow.",
            "code": "review_required",
            "hint": "Prepare the job review, inspect the CV changes, confirm cover-letter grounding, then create the packet.",
        },
    )


def prepare_packet_review(
    *,
    job_id: str,
    source_cv_id: str,
    now_iso: str,
    provider: str = "anthropic",
    model: str | None = None,
    profile: str = DEFAULT_SCORE_SYSTEM_ID,
    compare_profiles: list[str] | None = None,
    force: bool = False,
    replaces_packet_id: str | None = None,
) -> dict[str, Any]:
    """Prepare a job-scoped tailored-CV review without creating documents."""
    job = get_job(job_id)
    if job is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    _require_full_jd(job)
    jd_text = job.get("jd_text") or ""
    if not jd_text.strip():
        raise JobError(422, {"error": "This job has no description to tailor from.", "code": "empty_jd"})
    score_system_id = _normalize_score_system_id(profile)
    packet = _current_packet_for(
        job_id=job_id,
        source_cv_id=source_cv_id,
        source_cv_hash=_document_yaml_hash(source_cv_id, "cv"),
        job_content_hash=_content_hash(jd_text),
        score_system_id=score_system_id,
    )
    if packet and not force and _packet_documents_linked(packet, job_id):
        return {
            "tailored_cv_id": packet.get("tailored_cv_id"),
            "cover_letter_id": packet.get("cover_letter_id"),
            "ats": None,
            "duplicate": True,
            "packet_id": packet.get("id") if packet else None,
            "packet": packet,
        }

    review = tailor_svc.prepare_tailor_review(
        cv_id=source_cv_id,
        jd_text=jd_text,
        job_id=job_id,
        provider=provider,
        model=model,
        profile=score_system_id,
        compare_profiles=compare_profiles,
        packet_mode="regenerate" if force else None,
        replaces_packet_id=replaces_packet_id,
        now_iso=now_iso,
    )
    review["job_id"] = job_id
    review["company"] = job.get("company") or ""
    review["role"] = job.get("title") or ""
    if force:
        review["packet_mode"] = "regenerate"
    if replaces_packet_id:
        review["replaces_packet_id"] = replaces_packet_id
    return review


def apply_packet_review(
    *,
    job_id: str,
    source_cv_id: str,
    review_token: object,
    now_iso: str,
    provider: str = "anthropic",
    model: str | None = None,
) -> dict[str, Any]:
    """Apply a reviewed CV and generate a linked cover letter pending review."""
    job = get_job(job_id)
    if job is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    _require_full_jd(job)

    jd_text = job.get("jd_text") or ""
    if not jd_text.strip():
        raise JobError(422, {"error": "This job has no description to tailor from.", "code": "empty_jd"})
    cv_result = tailor_svc.apply_tailor_review(
        cv_id=source_cv_id,
        review_token=review_token,
        now_iso=now_iso,
        expected_jd_text=jd_text,
        expected_job_id=job_id,
    )
    tailored_cv_id = cv_result["tailored_cv_id"]
    review_id = str(cv_result.get("review_id") or "")
    why_fit = (job.get("why_fit") or "").strip()
    try:
        cl_result = cl_service.generate_cover_letter(
            cv_id=tailored_cv_id,
            jd_text=jd_text,
            tone="auto",
            company_context=why_fit,
            job_id=job_id,
            provider=provider,
            model=model,
            now_iso=now_iso,
        )
    except Exception:
        _rollback_document(tailored_cv_id, "cv")
        if review_id:
            tailor_svc.release_review_apply(review_id)
        raise

    try:
        packet = _ensure_pending_packet_row(
            job_id=job_id,
            source_cv_id=source_cv_id,
            tailored_cv_id=tailored_cv_id,
            cover_letter_id=cl_result["id"],
            provider=provider,
            model=model,
            ats=cv_result.get("ats"),
            source_hash=cv_result.get("source_hash"),
            job_content_hash=cv_result.get("jd_hash") or _content_hash(jd_text),
            tailored_hash=cv_result.get("tailored_hash"),
            score_system_id=_packet_score_system_id(cv_result.get("ats")),
            review_id=cv_result.get("review_id"),
            force=cv_result.get("packet_mode") == "regenerate",
            now_iso=now_iso,
        )
    except Exception:
        _rollback_document(cl_result["id"], "cover_letter")
        _rollback_document(tailored_cv_id, "cv")
        if review_id:
            tailor_svc.release_review_apply(review_id)
        raise

    return {
        "tailored_cv_id": tailored_cv_id,
        "cover_letter_id": cl_result["id"],
        "ats": cv_result.get("ats"),
        "packet_id": packet.get("id") if packet else None,
        "packet": packet,
        "review_id": cv_result.get("review_id"),
        "replaces_packet_id": cv_result.get("replaces_packet_id"),
        "cover_letter_hash": (packet or {}).get("cover_letter_hash"),
        "cover_letter_review_status": (packet or {}).get("cover_letter_review_status") or "pending",
        "requires_cover_letter_review": True,
    }


def confirm_cover_letter_review(
    *,
    job_id: str,
    packet_id: str,
    cover_letter_hash: object,
    now_iso: str,
) -> dict[str, Any]:
    """Promote a pending packet after the generated cover letter is reviewed."""
    expected_hash = str(cover_letter_hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise JobError(
            400,
            {
                "error": "A current cover-letter hash is required.",
                "code": "cover_letter_hash_required",
            },
        )
    job = get_job(job_id)
    if job is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    _require_full_jd(job)
    packet = next(
        (
            row
            for row in _raw_packet_rows(job_id)
            if row.get("id") == packet_id
            and row.get("owner_id") == _owner_id()
            and not row.get("archived_at")
        ),
        None,
    )
    if packet is None:
        raise JobError(404, {"error": "Packet not found", "code": "packet_not_found"})
    stored_hash = str(packet.get("cover_letter_hash") or "").strip().lower()
    if stored_hash != expected_hash:
        raise JobError(
            409,
            {
                "error": "The cover letter changed after this review prompt.",
                "code": "cover_letter_hash_mismatch",
            },
        )
    current_hash = _document_yaml_hash(str(packet.get("cover_letter_id") or ""), "cover_letter")
    if not current_hash:
        raise JobError(
            409,
            {"error": "The generated cover letter is missing.", "code": "cover_letter_missing"},
        )
    if current_hash != expected_hash:
        raise JobError(
            409,
            {
                "error": "The generated cover letter changed after this review prompt.",
                "code": "cover_letter_changed",
            },
        )
    if _is_completed_packet_row(packet):
        public = _public_packet(packet, job=job, derive_stale=True)
        return {
            "packet_id": public.get("id"),
            "packet": public,
            "cover_letter_review_status": public.get("cover_letter_review_status"),
            "cover_letter_review_confirmed_at": public.get("cover_letter_review_confirmed_at"),
        }
    if packet.get("status") != "review_applied" or packet.get("cover_letter_review_status") == "confirmed":
        raise JobError(
            409,
            {
                "error": "This packet is not waiting for cover-letter review.",
                "code": "cover_letter_review_not_pending",
            },
        )
    stale_reasons = _packet_stale_reasons(packet, job=job)
    if stale_reasons:
        raise JobError(
            409,
            {
                "error": "The packet changed before cover-letter review was confirmed.",
                "code": "packet_stale",
                "stale_reasons": stale_reasons,
            },
        )
    source_cv_id = str(packet.get("source_cv_id") or "")
    archived_rows = _archive_active_packet_rows(
        job_id,
        source_cv_id,
        now_iso=now_iso,
        exclude_id=packet_id,
        completed_only=True,
    )
    update_payload = {
        "status": "packet_created",
        "cover_letter_review_status": "confirmed",
        "cover_letter_review_confirmed_at": now_iso,
        "updated_at": now_iso,
    }
    try:
        updated = (
            _server_sb()
            .table("application_packets")
            .update(update_payload)
            .eq("id", packet_id)
            .eq("owner_id", _owner_id())
            .execute()
            .data
            or []
        )
    except Exception:
        _restore_archived_packets(archived_rows)
        raise
    if not updated:
        _restore_archived_packets(archived_rows)
        raise JobError(
            500,
            {"error": "Failed to confirm cover-letter review.", "code": "cover_letter_confirm_failed"},
        )
    public = _public_packet(updated[0], job=job, derive_stale=True)
    return {
        "packet_id": public.get("id"),
        "packet": public,
        "cover_letter_review_status": public.get("cover_letter_review_status"),
        "cover_letter_review_confirmed_at": public.get("cover_letter_review_confirmed_at"),
    }


def _rollback_document(doc_id: str, doc_type: str) -> None:
    """Best-effort hard-delete of a document created earlier in a failed chain.

    Used to undo the tailored CV when the cover-letter step blocks, so a
    guard-trip mid-chain never leaves a half-linked orphan. RLS scopes the
    delete to the caller. Swallows its own errors — the original failure is
    what the caller must see, and a cleanup hiccup must not mask it.
    """
    try:
        cl_service.delete_document(doc_id, doc_type)
    except Exception:  # noqa: BLE001 — cleanup is best-effort; surface the real error
        pass


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _linked_documents(job_id: str) -> list[dict]:
    """The caller's non-deleted documents linked to ``job_id`` (RLS-scoped)."""
    sb = _sb()
    resp = (
        sb.table("documents")
        .select("id, type, name, yaml_blob, updated_at")
        .eq("job_id", job_id)
        .is_("deleted_at", "null")
        .order("updated_at", desc=True)
        .execute()
    )
    return resp.data or []


def _document_yaml_hash(doc_id: str, expected_type: str | None = None) -> str | None:
    if not _is_uuid_text(doc_id):
        return None
    if local_demo_store.active() and (expected_type in (None, "cv")):
        demo_row = local_demo_store.demo_cv_row()
        if demo_row and demo_row.get("id") == doc_id:
            return _content_hash(demo_row.get("yaml_blob") or "")
    rows = (
        _sb()
        .table("documents")
        .select("id, type, yaml_blob")
        .eq("id", doc_id)
        .is_("deleted_at", "null")
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return None
    row = rows[0]
    if expected_type and row.get("type") != expected_type:
        return None
    return _content_hash(row.get("yaml_blob") or "")


def _is_uuid_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _packet_rows(job_id: str, *, job: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    sb = _sb()
    try:
        rows = (
            sb.table("application_packets")
            .select("*")
            .eq("job_id", job_id)
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 - additive migration may not be applied locally
        if _demo_mode_enabled() and ("application_packets" in str(exc) or "PGRST" in str(exc)):
            return []
        raise
    return [_public_packet(row, job=job, derive_stale=True) for row in rows]


def packet_lineage_for_documents(document_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Return safe packet lineage keyed by linked document id.

    The projection is RLS-scoped through the caller's Supabase client and never
    includes YAML blobs, review tokens, prompts, cookies, or auth material.
    """
    wanted = {str(doc_id) for doc_id in document_ids if _is_uuid_text(doc_id)}
    if not wanted:
        return {}
    try:
        rows = (
            _sb()
            .table("application_packets")
            .select("*")
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 - additive migration may not be applied locally
        if _demo_mode_enabled() and ("application_packets" in str(exc) or "PGRST" in str(exc)):
            return {}
        raise
    jobs: dict[str, dict[str, Any] | None] = {}
    out: dict[str, dict[str, Any]] = {}
    ranks: dict[str, tuple[int, int, str]] = {}
    for row in rows:
        job_id = str(row.get("job_id") or "")
        if job_id not in jobs:
            jobs[job_id] = get_job(job_id) if job_id else None
        public = _public_packet(row, job=jobs.get(job_id), derive_stale=True)
        role_ids = (
            ("source_cv", row.get("source_cv_id")),
            ("tailored_cv", row.get("tailored_cv_id")),
            ("cover_letter", row.get("cover_letter_id")),
        )
        for role, doc_id in role_ids:
            doc_key = str(doc_id or "")
            if doc_key not in wanted:
                continue
            rank = _packet_lineage_rank(public)
            if doc_key in ranks and rank <= ranks[doc_key]:
                continue
            ranks[doc_key] = rank
            out[doc_key] = _packet_lineage_projection(public, role=role, job=jobs.get(job_id))
    return out


def packet_lineage_for_document(document_id: str) -> dict[str, Any] | None:
    return packet_lineage_for_documents([document_id]).get(document_id)


def _packet_lineage_projection(
    packet: dict[str, Any],
    *,
    role: str,
    job: dict[str, Any] | None,
) -> dict[str, Any]:
    score_snapshot = packet.get("score_snapshot") if isinstance(packet.get("score_snapshot"), dict) else {}
    score = score_snapshot.get("score")
    return {
        "packet_id": packet.get("id"),
        "job_id": packet.get("job_id"),
        "job_title": (job or {}).get("title"),
        "job_company": (job or {}).get("company"),
        "document_role": role,
        "source_cv_id": packet.get("source_cv_id"),
        "tailored_cv_id": packet.get("tailored_cv_id"),
        "cover_letter_id": packet.get("cover_letter_id"),
        "source_cv_hash": packet.get("source_cv_hash"),
        "job_content_hash": packet.get("job_content_hash"),
        "tailored_cv_hash": packet.get("tailored_cv_hash"),
        "cover_letter_hash": packet.get("cover_letter_hash"),
        "score_system_id": packet.get("score_system_id"),
        "score": score if isinstance(score, (int, float)) else None,
        "prompt_version": packet.get("prompt_version"),
        "packet_version": packet.get("packet_version"),
        "review_id": packet.get("cover_letter_review_id"),
        "cover_letter_review_status": packet.get("cover_letter_review_status"),
        "cover_letter_review_confirmed_at": packet.get("cover_letter_review_confirmed_at"),
        "cover_letter_review_id": packet.get("cover_letter_review_id"),
        "status": packet.get("status"),
        "stored_status": packet.get("stored_status"),
        "review_status": packet.get("review_status"),
        "stale_reasons": packet.get("stale_reasons") if isinstance(packet.get("stale_reasons"), list) else [],
        "archived_at": packet.get("archived_at"),
        "document_links": packet.get("document_links") if isinstance(packet.get("document_links"), dict) else {},
        "created_at": packet.get("created_at"),
        "updated_at": packet.get("updated_at"),
    }


def _packet_lineage_rank(packet: dict[str, Any]) -> tuple[int, int, str]:
    status = str(packet.get("status") or "")
    if status == "packet_created":
        status_rank = 3
    elif status == "applied_external":
        status_rank = 2
    elif status == "stale":
        status_rank = 1
    else:
        status_rank = 0
    try:
        version = int(packet.get("packet_version") or 0)
    except (TypeError, ValueError):
        version = 0
    timestamp = str(packet.get("updated_at") or packet.get("created_at") or "")
    return status_rank, version, timestamp


def _public_packet(
    row: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
    derive_stale: bool = False,
) -> dict[str, Any]:
    doc_links = _packet_document_links(row)
    stale_reasons = _packet_stale_reasons(row, job=job) if derive_stale else []
    status = row.get("status")
    if row.get("archived_at") or status == "archived":
        status = "archived"
    elif stale_reasons:
        status = "stale"
    elif job and job.get("status") == "applied":
        status = "applied_external"
    return {
        "id": row.get("id"),
        "job_id": row.get("job_id"),
        "source_cv_id": row.get("source_cv_id"),
        "tailored_cv_id": row.get("tailored_cv_id"),
        "cover_letter_id": row.get("cover_letter_id"),
        "status": status,
        "stored_status": row.get("status"),
        "review_status": row.get("review_status"),
        "provider": row.get("provider"),
        "model": row.get("model"),
        "prompt_version": row.get("prompt_version"),
        "score_system_id": row.get("score_system_id") or _packet_score_system_id(row.get("score_snapshot"), fallback=None),
        "score_snapshot": _safe_packet_value(row.get("score_snapshot")) if isinstance(row.get("score_snapshot"), dict) else {},
        "source_cv_hash": row.get("source_cv_hash") or row.get("input_hash"),
        "job_content_hash": row.get("job_content_hash"),
        "tailored_cv_hash": row.get("tailored_cv_hash") or row.get("output_hash"),
        "cover_letter_hash": row.get("cover_letter_hash"),
        "cover_letter_review_status": row.get("cover_letter_review_status") or "pending",
        "cover_letter_review_confirmed_at": row.get("cover_letter_review_confirmed_at"),
        "cover_letter_review_id": row.get("cover_letter_review_id"),
        "packet_version": row.get("packet_version"),
        "profile_answers_complete": row.get("profile_answers_complete") is True,
        "legal_answers_confirmed": row.get("legal_answers_confirmed") is True,
        "visa_answers_confirmed": row.get("visa_answers_confirmed") is True,
        "document_links": doc_links,
        "stale_reasons": stale_reasons,
        "archived_at": row.get("archived_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _packet_stale_reasons(row: dict[str, Any], *, job: dict[str, Any] | None = None) -> list[str]:
    if row.get("archived_at") or row.get("status") == "archived":
        return []
    reasons: list[str] = []
    source_cv_id = str(row.get("source_cv_id") or "")
    expected_source_hash = row.get("source_cv_hash") or row.get("input_hash")
    current_source_hash = _document_yaml_hash(source_cv_id, "cv") if source_cv_id else None
    if not source_cv_id or current_source_hash is None:
        reasons.append("source_cv_missing")
    elif not expected_source_hash:
        reasons.append("source_cv_hash_missing")
    elif current_source_hash != expected_source_hash:
        reasons.append("source_cv_stale")
    expected_job_hash = row.get("job_content_hash")
    job_row = job if job is not None else get_job(str(row.get("job_id") or ""))
    current_job_hash = _content_hash((job_row or {}).get("jd_text") or "")
    if not expected_job_hash:
        reasons.append("job_content_hash_missing")
    elif current_job_hash != expected_job_hash:
        reasons.append("job_description_stale")
    expected_tailored_hash = row.get("tailored_cv_hash") or row.get("output_hash")
    current_tailored_hash = (
        _document_yaml_hash(str(row.get("tailored_cv_id") or ""), "cv")
        if row.get("tailored_cv_id")
        else None
    )
    if not row.get("tailored_cv_id") or current_tailored_hash is None:
        reasons.append("tailored_cv_missing")
    elif not expected_tailored_hash:
        reasons.append("tailored_cv_hash_missing")
    elif current_tailored_hash != expected_tailored_hash:
        reasons.append("tailored_cv_stale")
    expected_letter_hash = row.get("cover_letter_hash")
    current_letter_hash = (
        _document_yaml_hash(str(row.get("cover_letter_id") or ""), "cover_letter")
        if row.get("cover_letter_id")
        else None
    )
    if not row.get("cover_letter_id") or current_letter_hash is None:
        reasons.append("cover_letter_missing")
    elif not expected_letter_hash:
        reasons.append("cover_letter_hash_missing")
    elif current_letter_hash != expected_letter_hash:
        reasons.append("cover_letter_stale")
    return reasons


def _packet_document_links(row: dict[str, Any]) -> dict[str, str]:
    links: dict[str, str] = {}
    source = row.get("source_cv_id")
    tailored = row.get("tailored_cv_id")
    letter = row.get("cover_letter_id")
    if source:
        links["source_cv"] = f"/app?id={source}"
    if tailored:
        links["tailored_cv"] = f"/app?id={tailored}"
    if letter:
        links["cover_letter"] = f"/cover-letters?id={letter}"
    return links


def _normalize_score_system_id(value: object | None) -> str:
    try:
        from ats import profiles as ats_profiles
    except ImportError:  # pragma: no cover
        from engine.ats import profiles as ats_profiles

    try:
        return ats_profiles.normalize_profile(value)
    except ValueError:
        return DEFAULT_SCORE_SYSTEM_ID


def _packet_score_system_id(ats: object, *, fallback: str | None = DEFAULT_SCORE_SYSTEM_ID) -> str:
    if isinstance(ats, dict):
        summary = ats.get("summary") if isinstance(ats.get("summary"), dict) else {}
        profile = ats.get("profile") if isinstance(ats.get("profile"), dict) else {}
        for value in (summary.get("score_system"), profile.get("id"), ats.get("score_system_id")):
            if value:
                return _normalize_score_system_id(value)
    if fallback is None:
        return ""
    return _normalize_score_system_id(fallback)


def _is_completed_packet_row(row: dict[str, Any]) -> bool:
    """True only for completed packets, not standalone lineage rows."""
    return (
        not row.get("archived_at")
        and row.get("status") == "packet_created"
        and row.get("review_status") == "reviewed"
        and bool(row.get("source_cv_id"))
        and bool(row.get("tailored_cv_id"))
        and bool(row.get("cover_letter_id"))
        and row.get("cover_letter_review_status") == "confirmed"
        and bool(row.get("cover_letter_review_confirmed_at"))
        and bool(row.get("cover_letter_review_id"))
    )


def _current_packet_for(
    *,
    job_id: str,
    source_cv_id: str,
    source_cv_hash: str | None,
    job_content_hash: str | None,
    score_system_id: str,
) -> dict[str, Any] | None:
    rows = _raw_packet_rows(job_id)
    for row in rows:
        if row.get("archived_at"):
            continue
        if not _is_completed_packet_row(row):
            continue
        if row.get("source_cv_id") != source_cv_id:
            continue
        if (row.get("source_cv_hash") or row.get("input_hash")) != source_cv_hash:
            continue
        if row.get("job_content_hash") != job_content_hash:
            continue
        if row.get("prompt_version") != PACKET_PROMPT_VERSION:
            continue
        if int(row.get("packet_version") or 0) < PACKET_VERSION:
            continue
        if _packet_score_system_id(row.get("score_snapshot"), fallback=row.get("score_system_id") or score_system_id) != score_system_id:
            continue
        return _public_packet(row)
    return None


def _raw_packet_rows(job_id: str) -> list[dict[str, Any]]:
    try:
        return (
            _sb()
            .table("application_packets")
            .select("*")
            .eq("job_id", job_id)
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 - additive migration may not be applied locally
        if _demo_mode_enabled() and ("application_packets" in str(exc) or "PGRST" in str(exc)):
            return []
        raise


def _packet_documents_linked(packet: dict[str, Any], job_id: str) -> bool:
    docs = _linked_documents(job_id)
    ids_by_type = {
        "cv": {d.get("id") for d in docs if d.get("type") == "cv"},
        "cover_letter": {d.get("id") for d in docs if d.get("type") == "cover_letter"},
    }
    return (
        packet.get("tailored_cv_id") in ids_by_type["cv"]
        and packet.get("cover_letter_id") in ids_by_type["cover_letter"]
    )


def _next_packet_version(job_id: str, source_cv_id: str) -> int:
    versions = [
        int(row.get("packet_version") or 0)
        for row in _raw_packet_rows(job_id)
        if row.get("source_cv_id") == source_cv_id
    ]
    return max([PACKET_VERSION, *versions]) + (1 if versions else 0)


def _archive_active_packets(job_id: str, source_cv_id: str, *, now_iso: str) -> list[dict[str, Any]]:
    return _archive_active_packet_rows(job_id, source_cv_id, now_iso=now_iso)


def _archive_active_packet_rows(
    job_id: str,
    source_cv_id: str,
    *,
    now_iso: str,
    exclude_id: str | None = None,
    completed_only: bool = False,
    partial_only: bool = False,
) -> list[dict[str, Any]]:
    rows = []
    for row in _raw_packet_rows(job_id):
        if row.get("source_cv_id") != source_cv_id or row.get("archived_at"):
            continue
        if exclude_id is not None and row.get("id") == exclude_id:
            continue
        completed = _is_completed_packet_row(row)
        if completed_only and not completed:
            continue
        if partial_only and completed:
            continue
        rows.append(row)
    if not rows:
        return []
    snapshots = [dict(row) for row in rows]
    sb = _server_sb()
    owner_id = _owner_id()
    for row in rows:
        row_id = row.get("id")
        if row_id:
            updated = sb.table("application_packets").update({
                "status": "archived",
                "archived_at": now_iso,
                "updated_at": now_iso,
            }).eq("id", row_id).eq("owner_id", owner_id).execute().data or []
            if not updated:
                _restore_archived_packets(snapshots)
                raise JobError(
                    503,
                    {
                        "error": "Failed to archive the previous application packet.",
                        "code": "packet_archive_failed",
                    },
                )
    return snapshots


def _restore_archived_packets(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    sb = _server_sb()
    owner_id = _owner_id()
    for row in rows:
        row_id = row.get("id")
        if not row_id:
            continue
        sb.table("application_packets").update({
            "status": row.get("status") or "packet_created",
            "archived_at": row.get("archived_at"),
            "updated_at": row.get("updated_at"),
        }).eq("id", row_id).eq("owner_id", owner_id).execute()


def _insert_packet_payload(sb: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    return sb.table("application_packets").insert(payload).execute().data or []


def record_tailored_cv_packet_lineage(
    *,
    job_id: str,
    source_cv_id: str,
    tailored_cv_id: str,
    provider: str | None,
    model: str | None,
    ats: dict[str, Any] | None,
    source_hash: str | None = None,
    job_content_hash: str | None = None,
    tailored_hash: str | None = None,
    score_system_id: str | None = None,
    review_id: str | None = None,
    now_iso: str,
) -> dict[str, Any] | None:
    """Persist selected-job lineage for standalone Tailor-route CV saves."""
    return _record_incomplete_packet_lineage(
        job_id=job_id,
        source_cv_id=source_cv_id,
        tailored_cv_id=tailored_cv_id,
        provider=provider,
        model=model,
        ats=ats,
        source_hash=source_hash,
        job_content_hash=job_content_hash,
        tailored_hash=tailored_hash,
        score_system_id=score_system_id,
        review_id=review_id,
        now_iso=now_iso,
    )


def record_cover_letter_packet_lineage(
    *,
    job_id: str,
    source_cv_id: str,
    cover_letter_id: str,
    provider: str | None,
    model: str | None,
    ats: dict[str, Any] | None = None,
    tailored_cv_id: str | None = None,
    source_hash: str | None = None,
    job_content_hash: str | None = None,
    tailored_hash: str | None = None,
    cover_letter_hash: str | None = None,
    score_system_id: str | None = None,
    now_iso: str,
) -> dict[str, Any] | None:
    """Persist selected-job lineage for standalone Cover Letters saves."""
    return _record_incomplete_packet_lineage(
        job_id=job_id,
        source_cv_id=source_cv_id,
        tailored_cv_id=tailored_cv_id,
        cover_letter_id=cover_letter_id,
        provider=provider,
        model=model,
        ats=ats,
        source_hash=source_hash,
        job_content_hash=job_content_hash,
        tailored_hash=tailored_hash,
        cover_letter_hash=cover_letter_hash,
        score_system_id=score_system_id,
        now_iso=now_iso,
    )


def _record_incomplete_packet_lineage(
    *,
    job_id: str,
    source_cv_id: str,
    provider: str | None,
    model: str | None,
    ats: dict[str, Any] | None,
    tailored_cv_id: str | None = None,
    cover_letter_id: str | None = None,
    source_hash: str | None = None,
    job_content_hash: str | None = None,
    tailored_hash: str | None = None,
    cover_letter_hash: str | None = None,
    score_system_id: str | None = None,
    review_id: str | None = None,
    now_iso: str,
) -> dict[str, Any] | None:
    """Record packet lineage for job-linked artifacts that are not apply-ready.

    Standalone Tailor and Cover Letters paths produce legitimate job-linked
    documents, but they do not complete the review-first packet contract. These
    rows expose the same lineage surfaces while remaining non-usable because
    they are either stale by missing paired artifact or stored as
    ``review_applied`` rather than ``packet_created``.
    """
    job = get_job(job_id)
    if job is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    _require_full_jd(job)

    resolved_source_hash = source_hash or _document_yaml_hash(source_cv_id, "cv")
    if resolved_source_hash is None:
        raise JobError(404, {"error": "CV not found", "code": "cv_not_found"})
    resolved_job_hash = job_content_hash or _content_hash(job.get("jd_text") or "")
    resolved_tailored_hash = (
        tailored_hash
        if tailored_hash is not None
        else (_document_yaml_hash(tailored_cv_id, "cv") if tailored_cv_id else None)
    )
    resolved_cover_letter_hash = (
        cover_letter_hash
        if cover_letter_hash is not None
        else (_document_yaml_hash(cover_letter_id, "cover_letter") if cover_letter_id else None)
    )
    resolved_score_system = _normalize_score_system_id(score_system_id or _packet_score_system_id(ats))
    score_snapshot = ats if isinstance(ats, dict) else {}
    payload = {
        "owner_id": _owner_id(),
        "job_id": job_id,
        "source_cv_id": source_cv_id,
        "tailored_cv_id": tailored_cv_id,
        "cover_letter_id": cover_letter_id,
        "status": "review_applied",
        "review_status": "reviewed",
        "provider": provider,
        "model": model,
        "prompt_version": PACKET_PROMPT_VERSION,
        "score_system_id": resolved_score_system,
        "score_snapshot": score_snapshot,
        "source_cv_hash": resolved_source_hash,
        "job_content_hash": resolved_job_hash,
        "tailored_cv_hash": resolved_tailored_hash,
        "cover_letter_hash": resolved_cover_letter_hash,
        "cover_letter_review_status": "pending",
        "cover_letter_review_confirmed_at": None,
        "cover_letter_review_id": review_id,
        "input_hash": resolved_source_hash,
        "output_hash": resolved_tailored_hash,
        "packet_version": PACKET_VERSION,
        "updated_at": now_iso,
    }

    active_rows = [
        row
        for row in _raw_packet_rows(job_id)
        if row.get("source_cv_id") == source_cv_id and not row.get("archived_at")
    ]
    matching = next(
        (
            row
            for row in active_rows
            if (row.get("source_cv_hash") or row.get("input_hash")) == resolved_source_hash
            and row.get("job_content_hash") == resolved_job_hash
            and row.get("prompt_version") == PACKET_PROMPT_VERSION
            and _packet_score_system_id(row.get("score_snapshot"), fallback=row.get("score_system_id") or resolved_score_system)
            == resolved_score_system
        ),
        None,
    )

    if matching and matching.get("status") != "packet_created":
        same_tailored = not tailored_cv_id or matching.get("tailored_cv_id") in (None, tailored_cv_id)
        same_cover = not cover_letter_id or matching.get("cover_letter_id") in (None, cover_letter_id)
        if same_tailored and same_cover:
            update_payload = dict(payload)
            update_payload["tailored_cv_id"] = tailored_cv_id or matching.get("tailored_cv_id")
            update_payload["cover_letter_id"] = cover_letter_id or matching.get("cover_letter_id")
            update_payload["tailored_cv_hash"] = resolved_tailored_hash or matching.get("tailored_cv_hash") or matching.get("output_hash")
            update_payload["cover_letter_hash"] = resolved_cover_letter_hash or matching.get("cover_letter_hash")
            update_payload["output_hash"] = update_payload.get("tailored_cv_hash")
            update_payload["cover_letter_review_id"] = review_id or matching.get("cover_letter_review_id")
            if not isinstance(ats, dict):
                update_payload["score_snapshot"] = matching.get("score_snapshot") or {}
                update_payload["score_system_id"] = matching.get("score_system_id") or resolved_score_system
            updated = (
                _server_sb()
                .table("application_packets")
                .update(update_payload)
                .eq("id", matching.get("id"))
                .eq("owner_id", _owner_id())
                .execute()
                .data
                or []
            )
            if not updated:
                raise JobError(500, {"error": "Failed to update packet lineage.", "code": "packet_update_failed"})
            return _public_packet(updated[0], job=job, derive_stale=True)

    archived_partial_rows: list[dict[str, Any]] = []
    if matching and matching.get("status") == "packet_created":
        payload["status"] = "archived"
        payload["archived_at"] = now_iso
        payload["packet_version"] = _next_packet_version(job_id, source_cv_id)
    elif any(row.get("status") == "packet_created" for row in active_rows):
        payload["status"] = "archived"
        payload["archived_at"] = now_iso
        payload["packet_version"] = _next_packet_version(job_id, source_cv_id)
    else:
        partial_rows = [row for row in active_rows if row.get("status") != "packet_created"]
        if partial_rows:
            archived_partial_rows = _archive_active_packets(job_id, source_cv_id, now_iso=now_iso)

    try:
        inserted = _insert_packet_payload(_server_sb(), payload)
    except Exception as exc:  # noqa: BLE001 - additive migration may not be applied locally
        _restore_archived_packets(archived_partial_rows)
        if _demo_mode_enabled() and ("application_packets" in str(exc) or "PGRST" in str(exc)):
            return None
        raise
    if not inserted:
        _restore_archived_packets(archived_partial_rows)
        raise JobError(500, {"error": "Failed to save packet lineage.", "code": "packet_insert_failed"})
    return _public_packet(inserted[0], job=job, derive_stale=True)


def _ensure_pending_packet_row(
    *,
    job_id: str,
    source_cv_id: str,
    tailored_cv_id: str,
    cover_letter_id: str,
    provider: str | None,
    model: str | None,
    ats: dict[str, Any] | None,
    source_hash: str | None = None,
    job_content_hash: str | None = None,
    tailored_hash: str | None = None,
    cover_letter_hash: str | None = None,
    score_system_id: str | None = None,
    review_id: str | None = None,
    force: bool = False,
    now_iso: str,
) -> dict[str, Any] | None:
    """Persist generated packet artifacts before cover-letter review confirms."""
    job = get_job(job_id)
    if job is None:
        raise JobError(404, {"error": "Job not found", "code": "job_not_found"})
    _require_full_jd(job)
    resolved_source_hash = source_hash or _document_yaml_hash(source_cv_id, "cv")
    resolved_tailored_hash = tailored_hash or _document_yaml_hash(tailored_cv_id, "cv")
    resolved_cover_letter_hash = cover_letter_hash or _document_yaml_hash(cover_letter_id, "cover_letter")
    if not resolved_source_hash:
        raise JobError(404, {"error": "CV not found", "code": "cv_not_found"})
    if not resolved_tailored_hash:
        raise JobError(404, {"error": "Tailored CV not found", "code": "tailored_cv_not_found"})
    if not resolved_cover_letter_hash:
        raise JobError(404, {"error": "Cover letter not found", "code": "cover_letter_not_found"})
    resolved_job_hash = job_content_hash or _content_hash(job.get("jd_text") or "")
    resolved_score_system = _normalize_score_system_id(score_system_id or _packet_score_system_id(ats))
    packet_version = _next_packet_version(job_id, source_cv_id) if force else PACKET_VERSION
    payload = {
        "owner_id": _owner_id(),
        "job_id": job_id,
        "source_cv_id": source_cv_id,
        "tailored_cv_id": tailored_cv_id,
        "cover_letter_id": cover_letter_id,
        "status": "review_applied",
        "review_status": "reviewed",
        "provider": provider,
        "model": model,
        "prompt_version": PACKET_PROMPT_VERSION,
        "score_system_id": resolved_score_system,
        "score_snapshot": ats or {},
        "source_cv_hash": resolved_source_hash,
        "job_content_hash": resolved_job_hash,
        "tailored_cv_hash": resolved_tailored_hash,
        "cover_letter_hash": resolved_cover_letter_hash,
        "cover_letter_review_status": "pending",
        "cover_letter_review_confirmed_at": None,
        "cover_letter_review_id": review_id,
        "input_hash": resolved_source_hash,
        "output_hash": resolved_tailored_hash,
        "packet_version": packet_version,
        "updated_at": now_iso,
    }
    archived_partial_rows = _archive_active_packet_rows(
        job_id,
        source_cv_id,
        now_iso=now_iso,
        partial_only=True,
    )
    try:
        inserted = _insert_packet_payload(_server_sb(), payload)
    except Exception:
        _restore_archived_packets(archived_partial_rows)
        raise
    if not inserted:
        _restore_archived_packets(archived_partial_rows)
        raise JobError(500, {"error": "Failed to save pending application packet.", "code": "packet_insert_failed"})
    return _public_packet(inserted[0], job=job, derive_stale=True)


def _public_job(
    row: dict, *, full: bool = False, has_cover: bool = False, cover_id: str | None = None
) -> dict[str, Any]:
    """Shape a saved_jobs row for the API.

    The compact form (list / create response) carries the catalog-grid fields
    (field/deadline/confidence/why_fit) + ``has_cover`` + ``cover_id`` so the
    grid renders the Generate/Open split button without a detail fetch; the
    ``full`` form (detail view) also includes ``jd_text`` / ``jd_json`` / source
    URL. ``deadline`` is the ISO date string or None ("Rolling"); ``confidence``
    is an int 0-100 or None; ``cover_id`` is the doc id the "Open" button targets.
    """
    metadata = _safe_source_metadata(row.get("source_metadata"))
    raw_search_criteria = (
        row.get("search_criteria")
        if isinstance(row.get("search_criteria"), dict)
        else metadata.get("search_criteria", {})
    )
    raw_match_reasons = (
        row.get("match_reasons")
        if isinstance(row.get("match_reasons"), list)
        else metadata.get("match_reasons", [])
    )
    shortlist_rank = {
        "priority_rank": metadata.get("priority_rank"),
        "rank_bucket": metadata.get("rank_bucket"),
        "rank_badge": metadata.get("rank_badge"),
        "fit_note": metadata.get("fit_note"),
        "risk_note": metadata.get("risk_note"),
        "source_artifact": metadata.get("source_artifact"),
        "verified_at": metadata.get("verified_at"),
    }
    out: dict[str, Any] = {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "company": row.get("company") or "",
        "status": row.get("status") or "saved",
        "created_at": row.get("created_at"),
        "field": row.get("field") or "",
        "deadline": row.get("deadline"),
        "confidence": row.get("confidence"),
        "why_fit": row.get("why_fit") or "",
        "starred": metadata.get("starred") is True,
        "has_cover": bool(has_cover),
        "cover_id": cover_id,
        "source_provider": row.get("source_provider"),
        "source_type": row.get("source_type"),
        "source_external_id": _public_source_external_id(row.get("source_external_id")),
        "source_url": _safe_source_url(row.get("source_url")) or "",
        "source_url_redacted": _source_url_redacted(row.get("source_url")),
        "source_metadata": metadata,
        "source_state": _source_state({**row, "source_metadata": metadata}),
        "shortlist_rank": shortlist_rank,
        "priority_rank": shortlist_rank.get("priority_rank"),
        "rank_bucket": shortlist_rank.get("rank_bucket"),
        "rank_badge": shortlist_rank.get("rank_badge"),
        "verification_status": row.get("verification_status"),
        "import_run_id": row.get("import_run_id"),
        "source_country": _safe_source_scalar(row.get("source_country")),
        "source_region": _safe_source_scalar(row.get("source_region")),
        "source_city": _safe_source_scalar(row.get("source_city") or metadata.get("source_city")),
        "source_location": _safe_source_scalar(row.get("source_location")),
        "job_family": _safe_source_scalar(row.get("job_family") or metadata.get("job_family")),
        "search_criteria": _safe_search_criteria(raw_search_criteria),
        "match_reasons": [
            item for item in (_safe_metadata_value(raw_match_reasons) if isinstance(raw_match_reasons, list) else [])
            if isinstance(item, str)
        ],
        "broader_match": bool(row.get("broader_match") or metadata.get("broader_match")),
        "remote_mode": _safe_source_scalar(row.get("remote_mode")),
        "salary_text": _safe_source_scalar(row.get("salary_text")),
        "seniority": _safe_source_scalar(row.get("seniority")),
        "experience_requirements": row.get("experience_requirements") if isinstance(row.get("experience_requirements"), dict) else {},
        "experience_fit": _job_experience_fit(row),
        "last_seen_at": row.get("last_seen_at"),
        "expires_at": row.get("expires_at"),
    }
    if full:
        out["jd_text"] = row.get("jd_text") or ""
        out["jd_json"] = row.get("jd_json") or {}
        out["source_url"] = _safe_source_url(row.get("source_url")) or ""
        out["content_hash"] = row.get("content_hash") or ""
        out["updated_at"] = row.get("updated_at")
    return out


def _now_iso() -> str:
    """UTC timestamp for ``updated_at`` bumps (mirrors server._now_iso)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
