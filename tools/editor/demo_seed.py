"""DEV-ONLY demo content seeding (gated behind ``ONEPAGER_DEMO_MODE``).

The demo user (``demo@onepager.test``) should land on a *working* product, not
an empty skeleton. After a successful one-click demo login the server seeds two
documents into the demo user's own ``documents`` rows (RLS owner-scoped, written
under the demo user's JWT):

  1. ONE fully-populated CV — the public sample CV shipped at
     ``content/cv.example.yaml`` (Jordan Avery), named "Sample CV — Jordan Avery".
  2. ONE sample cover letter (``type='cover_letter'``) — a short, CV-grounded
     letter referencing that CV (recipient/company/role/body) so it renders.

Seeding is **idempotent**: each document is created only if one with its exact
name does not already exist for the caller. Repeated demo logins therefore do
NOT duplicate, and they do NOT clobber edits a tester made to the seeded docs.

Everything here is **best-effort and gated**: it only does anything when demo
mode is on, and it never raises — a seeding failure must never block the login
that triggered it. The whole feature defaults OFF (see engine/llm/client.py).
"""
from __future__ import annotations

from pathlib import Path

import cover_letter_service as cl_service

try:  # The gating helper (defaults OFF). Import both layouts like server.py.
    from llm.client import demo_mode_enabled
except ModuleNotFoundError:  # pragma: no cover - import-path fallback
    from engine.llm.client import demo_mode_enabled

# Stable document names — also the idempotency keys. Changing these would make
# a fresh copy seed alongside the old one on the next demo login, so keep them
# constant.
DEMO_CV_NAME = "Sample CV — Jordan Avery"
DEMO_COVER_LETTER_NAME = "Sample Cover Letter — Globex"

# Resolve the shipped example CV relative to the repo root (…/tools/editor →
# repo). Read at seed time (not import) so tests can monkeypatch the path.
_REPO = Path(__file__).resolve().parent.parent.parent
_EXAMPLE_CV_PATH = _REPO / "content" / "cv.example.yaml"

# A short, fully CV-grounded cover letter. Every proper noun and figure below is
# drawn from content/cv.example.yaml (Globex, Senior Data Engineer, the data-
# tooling SDK, the ~$180K/year compute reclaim), so the letter reads coherently
# and the cover-letter renderer produces a real PDF. Demo mode does not run the
# anti-fabrication guards on seeded content, but we keep it grounded anyway.
_DEMO_COVER_LETTER_BODY = (
    "Dear Hiring Team,\n\n"
    "I am writing to express my interest in the Senior Data Engineer role at "
    "Globex. As the engineer behind our internal data-tooling SDK — now used "
    "daily across three teams — I have seen first-hand how the right platform "
    "investment compounds, cutting routine pipeline work by roughly 10x and "
    "standardizing how jobs are authored.\n\n"
    "Beyond tooling, I care about the numbers behind the platform. I found and "
    "reclaimed about $180K a year of idle warehouse compute that nobody was "
    "tracking, right-sized it, and the savings held through the following two "
    "quarters. I also own the adoption and reliability dashboards leadership "
    "reviews weekly when prioritizing where to invest next.\n\n"
    "I would welcome the chance to bring that same blend of platform engineering "
    "and measurable impact to your team. Thank you for your consideration.\n\n"
    "Best regards,\n"
    "Jordan Avery"
)


def _example_cv_yaml() -> str:
    """The shipped example CV's raw YAML text, or ``None`` if unreadable.

    Returns the file contents verbatim so the seeded CV is byte-identical to the
    public sample (Jordan Avery). Never raises — a missing/locked file just means
    "skip the CV seed" rather than breaking the demo login.
    """
    try:
        return _EXAMPLE_CV_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _has_document_named(doc_type: str, name: str) -> bool:
    """True iff the caller already owns a non-deleted ``doc_type`` doc with ``name``.

    Runs under the demo user's JWT (via the service's RLS-scoped list), so it
    only ever sees the demo user's own rows — the idempotency check is therefore
    per-user, exactly as required.
    """
    rows = cl_service.list_documents(doc_type)
    return any((r.get("name") or "") == name for r in rows)


def _seed_demo_cv() -> str | None:
    """Insert the sample CV if absent; return its id (existing or new), or None.

    Idempotent: if a CV named :data:`DEMO_CV_NAME` already exists for the caller
    we return its id without inserting (so the cover letter can still reference
    it) and never create a second copy or overwrite tester edits.
    """
    rows = cl_service.list_documents("cv")
    for r in rows:
        if (r.get("name") or "") == DEMO_CV_NAME:
            return r.get("id")
    blob = _example_cv_yaml()
    if not blob.strip():
        return None
    row = cl_service.insert_document("cv", DEMO_CV_NAME, blob)
    return row.get("id")


def _seed_demo_cover_letter(source_cv_id: str | None) -> str | None:
    """Insert the sample cover letter if absent; return its id, or None.

    Idempotent on :data:`DEMO_COVER_LETTER_NAME`. ``source_cv_id`` links the
    letter to the seeded CV so the build route can resolve the signer's name;
    it may be None (the letter still renders with a blank sender).
    """
    if _has_document_named("cover_letter", DEMO_COVER_LETTER_NAME):
        return None
    blob = cl_service.serialize_blob(
        {
            "recipient": "Hiring Team",
            "company": "Globex",
            "role": "Senior Data Engineer",
            "tone": "formal",
            "body": _DEMO_COVER_LETTER_BODY,
            "language": "en",
            "source_cv_id": source_cv_id,
        }
    )
    row = cl_service.insert_document("cover_letter", DEMO_COVER_LETTER_NAME, blob)
    return row.get("id")


def seed_demo_documents() -> dict:
    """Idempotently seed the demo user's sample CV + cover letter.

    MUST be called only within an authenticated request context for the demo
    user (``g.access_token`` set), so the RLS-scoped inserts run as that user.
    Best-effort: any failure is swallowed and reported in the return value
    rather than raised, so it can never break the login that triggered it.

    Returns ``{"cv_id", "cover_letter_id", "seeded": bool, "error": str|None}``
    where the ids are present whether the doc was just created or already existed
    (``None`` only if that doc could not be resolved at all).
    """
    result: dict = {"cv_id": None, "cover_letter_id": None, "seeded": False, "error": None}
    # Defense-in-depth gate: the only caller (the demo-login route) is already
    # gated, but refuse to write anything if demo mode is off so a stray direct
    # call can never create rows in a production/default context.
    if not demo_mode_enabled():
        return result
    try:
        cv_id = _seed_demo_cv()
        result["cv_id"] = cv_id
        cover_id = _seed_demo_cover_letter(cv_id)
        result["cover_letter_id"] = cover_id
        result["seeded"] = True
    except Exception as exc:  # noqa: BLE001 — seeding must never break login
        result["error"] = type(exc).__name__
    return result
