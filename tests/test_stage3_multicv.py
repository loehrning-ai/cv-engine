"""Stage 3 integration tests — multi-CV management under RLS.

These run the real gateway against the real EU Supabase project with two
real, confirmed users (A and B). They prove the end-to-end MVP loop:

  Owner A
    (a) creates 3 CVs and lists them            → 3 distinct ids
    (b) renames a CV                             → new name sticks
    (c) duplicates a CV                          → "Copy of …", own id, same body
    (d) deletes a CV                             → gone from the list
    (e) each remaining CV exports a 1-page PDF   → /api/build 200, application/pdf

  Tenant isolation (B must NOT be able to touch A's CVs)
    (f) B's /api/cvs never contains A's ids
    (g) B GET /api/cv?id=<A's id>                → 404 (RLS hides the row)
    (h) B rename / duplicate / delete A's CV     → 404 each

Real user JWTs are provisioned out-of-band (auth.users + auth.identities via
the Supabase MCP, tokens via the GoTrue password-grant) and handed in via
environment variables, exactly like test_stage2_auth.py. The test SKIPS with
a clear message if those env vars are absent, so the rest of the suite still
runs in CI without live credentials.

Required env (set by the provisioning harness):
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


def _cv_yaml(marker: str) -> str:
    """A valid 1-page CV whose company is a unique marker per document."""
    return (
        "name: Tester\n"
        "accent: '#1f6f43'\n"
        "font: sans\n"
        "density: normal\n"
        "contact:\n"
        "  - network: mail\n"
        "    username: tester@onepager.test\n"
        "experience:\n"
        "  - role: Engineer\n"
        f"    company: {marker}\n"
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


def _list_ids(client, token: str) -> set[str]:
    res = client.get("/api/cvs", headers=_auth(token))
    assert res.status_code == 200, res.get_data(as_text=True)
    return {c["id"] for c in res.get_json().get("cvs", [])}


def _create(client, token: str, name: str) -> str:
    res = client.post("/api/cvs", json={"name": name}, headers=_auth(token))
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["id"]


def _delete_all_for(client, token: str) -> None:
    """Best-effort: delete every CV the user currently owns (test cleanup)."""
    for cid in _list_ids(client, token):
        client.delete(f"/api/cv/{cid}", headers=_auth(token))


@pytest.fixture
def clean_slate(client):
    """Ensure both users start and end each test owning zero leftover CVs."""
    _delete_all_for(client, A_TOKEN)
    _delete_all_for(client, B_TOKEN)
    yield
    _delete_all_for(client, A_TOKEN)
    _delete_all_for(client, B_TOKEN)


class TestOwnerLifecycle:
    def test_create_three_and_list(self, client, clean_slate):
        ids = {_create(client, A_TOKEN, f"CV {i}") for i in range(3)}
        assert len(ids) == 3
        listed = _list_ids(client, A_TOKEN)
        assert ids <= listed
        # The list response also surfaces the free-tier flag.
        data = client.get("/api/cvs", headers=_auth(A_TOKEN)).get_json()
        assert data["limit"] >= 3
        assert data["count"] >= 3
        assert "at_limit" in data

    def test_rename(self, client, clean_slate):
        cid = _create(client, A_TOKEN, "Old name")
        res = client.post(
            f"/api/cv/{cid}/rename", json={"name": "Brand new name"}, headers=_auth(A_TOKEN)
        )
        assert res.status_code == 200
        assert res.get_json()["name"] == "Brand new name"
        # Reflected in the list.
        names = {
            c["id"]: c["name"]
            for c in client.get("/api/cvs", headers=_auth(A_TOKEN)).get_json()["cvs"]
        }
        assert names[cid] == "Brand new name"

    def test_duplicate_copies_body(self, client, clean_slate):
        cid = _create(client, A_TOKEN, "Source")
        # Give it a recognisable body.
        client.post(
            f"/api/cv?id={cid}", json={"content": _cv_yaml("DUP-MARKER-XZ")}, headers=_auth(A_TOKEN)
        )
        dup = client.post(f"/api/cv/{cid}/duplicate", headers=_auth(A_TOKEN))
        assert dup.status_code == 201
        dup_id = dup.get_json()["id"]
        assert dup_id != cid
        assert dup.get_json()["name"].startswith("Copy of")
        body = client.get(f"/api/cv?id={dup_id}", headers=_auth(A_TOKEN)).get_data(as_text=True)
        assert "DUP-MARKER-XZ" in body

    def test_delete_removes_from_list(self, client, clean_slate):
        keep = _create(client, A_TOKEN, "Keeper")
        doomed = _create(client, A_TOKEN, "Doomed")
        res = client.delete(f"/api/cv/{doomed}", headers=_auth(A_TOKEN))
        assert res.status_code == 200
        ids = _list_ids(client, A_TOKEN)
        assert doomed not in ids
        assert keep in ids

    def test_each_cv_exports_one_page_pdf(self, client, clean_slate):
        # Create two CVs, give each a valid body, and confirm each builds to
        # a single-page PDF via the (unchanged) 1-page gate.
        ids = []
        for i in range(2):
            cid = _create(client, A_TOKEN, f"Buildable {i}")
            client.post(
                f"/api/cv?id={cid}",
                json={"content": _cv_yaml(f"BUILD-{i}")},
                headers=_auth(A_TOKEN),
            )
            ids.append(cid)
        for cid in ids:
            res = client.post(
                "/api/build", json={"id": cid, "density": "normal"}, headers=_auth(A_TOKEN)
            )
            assert res.status_code == 200, res.get_data(as_text=True)
            assert res.headers["Content-Type"] == "application/pdf"
            assert res.get_data()[:4] == b"%PDF"


class TestCrossTenantIsolation:
    def test_b_list_excludes_a(self, client, clean_slate):
        a_ids = {_create(client, A_TOKEN, f"A's CV {i}") for i in range(3)}
        b_ids = _list_ids(client, B_TOKEN)
        # B may have a seeded skeleton, but NONE of A's ids.
        assert a_ids.isdisjoint(b_ids), f"RLS leak: B saw A's ids {a_ids & b_ids!r}"

    def test_b_cannot_read_a_cv_by_id(self, client, clean_slate):
        a_id = _create(client, A_TOKEN, "A private")
        res = client.get(f"/api/cv?id={a_id}", headers=_auth(B_TOKEN))
        assert res.status_code == 404, res.get_data(as_text=True)

    def test_b_cannot_rename_a_cv(self, client, clean_slate):
        a_id = _create(client, A_TOKEN, "A rename target")
        res = client.post(
            f"/api/cv/{a_id}/rename", json={"name": "hijacked"}, headers=_auth(B_TOKEN)
        )
        assert res.status_code == 404
        # And A's name is untouched.
        names = {
            c["id"]: c["name"]
            for c in client.get("/api/cvs", headers=_auth(A_TOKEN)).get_json()["cvs"]
        }
        assert names[a_id] == "A rename target"

    def test_b_cannot_duplicate_a_cv(self, client, clean_slate):
        a_id = _create(client, A_TOKEN, "A dup target")
        res = client.post(f"/api/cv/{a_id}/duplicate", headers=_auth(B_TOKEN))
        assert res.status_code == 404

    def test_b_cannot_delete_a_cv(self, client, clean_slate):
        a_id = _create(client, A_TOKEN, "A delete target")
        res = client.delete(f"/api/cv/{a_id}", headers=_auth(B_TOKEN))
        assert res.status_code == 404
        # A still owns it.
        assert a_id in _list_ids(client, A_TOKEN)
