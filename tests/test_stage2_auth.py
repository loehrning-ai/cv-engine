"""Stage 2 integration tests — Supabase auth + per-user CV storage + RLS.

These exercise the real gateway against the real EU Supabase project:

  (a) no Bearer token            → /api/cv 401
  (b) user A POSTs a CV          → GETs the same CV back
  (c) user B GET /api/cv         → B's own seeded skeleton, NOT A's data
  (d) user B reads A's doc by id → RLS denies (empty), 404 surfaced
  (e) oversized CV body          → 413

Real user JWTs are provisioned out-of-band (GoTrue signup → confirmed via
Supabase MCP execute_sql → password grant) and handed to this test via
environment variables, because a confirmed-user password grant can't happen
inside the sandbox without the MCP confirm step. The test SKIPS with a clear
message if those env vars are absent, so the rest of the suite still runs.

Required env (set by the harness that provisions the users):
    TEST_A_TOKEN, TEST_A_ID, TEST_B_TOKEN, TEST_B_ID
Plus the usual SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY / SUPABASE_JWKS_URL.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

A_TOKEN = os.environ.get("TEST_A_TOKEN")
A_ID = os.environ.get("TEST_A_ID")
B_TOKEN = os.environ.get("TEST_B_TOKEN")
B_ID = os.environ.get("TEST_B_ID")

_HAVE_USERS = all([A_TOKEN, A_ID, B_TOKEN, B_ID])
pytestmark = pytest.mark.skipif(
    not _HAVE_USERS,
    reason="TEST_A_TOKEN/TEST_B_TOKEN (+ ids) not set — provision real users first",
)

# A valid CV (passes the engine's content validator: name + contact + spine).
USER_A_CV = (
    "name: Alice Example\n"
    "accent: '#1f6f43'\n"
    "font: sans\n"
    "density: normal\n"
    "contact:\n"
    "  - network: mail\n"
    "    username: alice@onepager.test\n"
    "experience:\n"
    "  - role: Staff Engineer\n"
    "    company: AcmeCorp-A-UNIQUE-MARKER\n"
    "    location: Berlin\n"
    "    start: '2024'\n"
    "    end: Present\n"
    "    bullets:\n"
    "      - Shipped the thing.\n"
    "    stack: Python\n"
)


@pytest.fixture
def client():
    """Flask test client against the real server module (real Supabase)."""
    server = importlib.import_module("server")
    server.app.config.update(TESTING=True)
    return server.app.test_client()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAuthGate:
    def test_no_bearer_is_401(self, client):
        res = client.get("/api/cv")
        assert res.status_code == 401, res.get_data(as_text=True)
        assert res.get_json().get("error") == "unauthorized"

    def test_garbage_bearer_is_401(self, client):
        res = client.get("/api/cv", headers=_auth("not-a-real-jwt"))
        assert res.status_code == 401

    def test_healthz_open(self, client):
        res = client.get("/healthz")
        assert res.status_code == 200
        assert res.get_json() == {"ok": True}


class TestPerUserStorage:
    def test_user_a_save_then_get(self, client):
        post = client.post("/api/cv", json={"content": USER_A_CV}, headers=_auth(A_TOKEN))
        assert post.status_code == 200, post.get_data(as_text=True)
        assert post.get_json().get("saved") is True

        got = client.get("/api/cv", headers=_auth(A_TOKEN))
        assert got.status_code == 200
        body = got.get_data(as_text=True)
        assert "AcmeCorp-A-UNIQUE-MARKER" in body
        assert "Alice Example" in body

    def test_user_b_gets_own_skeleton_not_a(self, client):
        # Ensure A's data exists first.
        client.post("/api/cv", json={"content": USER_A_CV}, headers=_auth(A_TOKEN))

        got = client.get("/api/cv", headers=_auth(B_TOKEN))
        assert got.status_code == 200
        body = got.get_data(as_text=True)
        # Cross-tenant isolation: A's unique marker must NOT appear for B.
        assert "AcmeCorp-A-UNIQUE-MARKER" not in body
        assert "Alice Example" not in body
        # B should see their seeded skeleton instead.
        assert "Your Name" in body or "Your Role" in body


class TestCrossTenantById:
    def test_b_cannot_read_a_document_by_id(self, client):
        """RLS denies B reading A's row even with A's document id in hand."""
        from supabase import create_client

        url = os.environ["SUPABASE_URL"]
        pub = os.environ["SUPABASE_PUBLISHABLE_KEY"]

        # A creates/loads their primary doc, get its id via A's own JWT.
        client.post("/api/cv", json={"content": USER_A_CV}, headers=_auth(A_TOKEN))
        sb_a = create_client(url, pub)
        sb_a.postgrest.auth(A_TOKEN)
        a_rows = (
            sb_a.table("documents").select("id").eq("type", "cv").limit(1).execute().data
        )
        assert a_rows, "user A should own a CV document"
        a_doc_id = a_rows[0]["id"]

        # B tries to read A's row by id — RLS must return zero rows.
        sb_b = create_client(url, pub)
        sb_b.postgrest.auth(B_TOKEN)
        b_view = sb_b.table("documents").select("*").eq("id", a_doc_id).execute().data
        assert b_view == [], f"RLS leak: B saw A's row {b_view!r}"


class TestSizeCap:
    def test_oversized_cv_is_413(self, client):
        big = "name: X\ncontact: []\n" + ("# pad\n" * 120000)  # > 512 KB
        assert len(big.encode("utf-8")) > 512 * 1024
        res = client.post("/api/cv", json={"content": big}, headers=_auth(A_TOKEN))
        assert res.status_code == 413, res.status_code
        assert "max_bytes" in res.get_json()

    def test_invalid_yaml_is_400(self, client):
        # Missing required 'contact' → content validator rejects → 400.
        res = client.post(
            "/api/cv", json={"content": "name: OnlyName\n"}, headers=_auth(A_TOKEN)
        )
        assert res.status_code == 400
        assert "invalid" in res.get_json().get("error", "").lower()
