"""Stage 7 — unified document LIBRARY (GET /api/documents).

The /cover-letters page is now a two-type library: it lists the caller's CVs
AND cover letters (one call to ``GET /api/documents``). This suite asserts:

  1. **Both types listed** — the payload carries a ``cv`` and a ``cover_letter``
     row, each with id/type/name/updated_at; cover letters additionally carry
     company/role/job_id and (when the caller owns the job) the linked job_title.
  2. **Soft-delete excluded** — a row with ``deleted_at`` set never appears
     (the ``deleted_at is null`` convention, enforced inside ``list_documents``).
  3. **Type filter** — the server returns both types; the page filters
     client-side (All / CVs / Cover letters). We assert the payload is complete
     enough for each filter and exercise the same filter predicate the JS uses.
  4. **STRICTLY RLS-scoped** — the listing path goes ONLY through the per-user
     Supabase client (anon client carrying the caller's JWT, ``user_supabase``);
     the handler imports/uses NO service-role client and NO SECURITY DEFINER
     RPC/view to aggregate across the table. We assert this two ways: (a) a
     source-level check that the handler + the two service functions it calls use
     ``user_supabase`` and never an ``.rpc(...)`` aggregate, and (b) the genuine
     cross-tenant isolation matrix against the REAL project with real user JWTs —
     user B's library never returns user A's CV or cover letter.

The in-memory ``fake_supabase`` is one tenant's view (rows are bound to the
stub owner), so the unit layer proves the SHAPE + soft-delete + filter; the
cross-tenant guarantee is proved by the env-gated live-JWT class, which SKIPS
cleanly without the provisioning vars (same pattern as Stage 2/4/5/6/7-jobs).
"""
from __future__ import annotations

import os
import re
import sys
import uuid as _uuid
from pathlib import Path

import pytest
import yaml as _yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

GROUNDED_CV = {
    "name": "Tim Loehr",
    "experience": [
        {"role": "Data Engineer", "company": "Meta", "bullets": ["Cut latency 40%."]}
    ],
}
JD_TEXT = (
    "Title: Senior Data Engineer\nCompany: Acme GmbH\n"
    "Spark and Airflow required. Build ETL pipelines."
)


# ---------------------------------------------------------------------------
# Seed helpers — write rows straight into the per-tenant fake store.
# ---------------------------------------------------------------------------
def _seed_cv(store, *, name="My CV", ts=1, deleted=False, job_id=None):
    rows = store.tables.setdefault("documents", [])
    doc_id = str(_uuid.uuid4())
    rows.append(
        {
            "id": doc_id,
            "owner_id": store.owner_id,
            "type": "cv",
            "name": name,
            "yaml_blob": _yaml.safe_dump(GROUNDED_CV, sort_keys=False),
            "job_id": job_id,
            "created_at": ts,
            "deleted_at": (ts if deleted else None),
            "updated_at": ts,
            "last_used_at": ts,
        }
    )
    return doc_id


def _seed_letter(store, *, name="Cover", company="Acme GmbH", role="Senior Data Engineer",
                 job_id=None, ts=2, deleted=False):
    rows = store.tables.setdefault("documents", [])
    cid = str(_uuid.uuid4())
    blob = {"company": company, "role": role, "body": "x"}
    if job_id is not None:
        blob["job_id"] = job_id
    rows.append(
        {
            "id": cid,
            "owner_id": store.owner_id,
            "type": "cover_letter",
            "name": name,
            "yaml_blob": _yaml.safe_dump(blob, sort_keys=False),
            "job_id": job_id,
            "created_at": ts,
            "deleted_at": (ts if deleted else None),
            "updated_at": ts,
            "last_used_at": ts,
        }
    )
    return cid


def _seed_job(store, *, title="Senior Data Engineer", company="Acme GmbH"):
    rows = store.tables.setdefault("saved_jobs", [])
    jid = str(_uuid.uuid4())
    rows.append(
        {
            "id": jid,
            "owner_id": store.owner_id,
            "title": title,
            "company": company,
            "jd_text": JD_TEXT,
            "status": "open",
            "deleted_at": None,
            "updated_at": 1,
        }
    )
    return jid


def _by_type(documents, t):
    return [d for d in documents if d["type"] == t]


# ===========================================================================
#  1. The library lists BOTH document types for the owner.
# ===========================================================================
class TestListsBothTypes:
    def test_returns_cv_and_cover_letter(self, server_module, client, fake_supabase):
        _seed_cv(fake_supabase, name="My CV", ts=1)
        _seed_letter(fake_supabase, name="Cover — Acme", ts=2)

        res = client.get("/api/documents")
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        docs = body["documents"]
        assert body["count"] == 2
        cvs = _by_type(docs, "cv")
        letters = _by_type(docs, "cover_letter")
        assert len(cvs) == 1 and len(letters) == 1

        cv = cvs[0]
        assert cv["name"] == "My CV"
        assert set(cv) >= {
            "id", "type", "name", "title", "created_at", "job_id", "job_title",
            "job_label", "provenance_summary", "updated_at", "last_used_at", "open_url", "actions",
        }
        assert cv["job_id"] is None
        assert cv["job_title"] is None
        assert cv["open_url"] == f"/app?id={cv['id']}"
        assert {"open", "duplicate", "download_yaml"}.issubset(set(cv["actions"]))
        assert "yaml_blob" not in cv

        letter = letters[0]
        assert letter["name"] == "Cover — Acme"
        assert letter["company"] == "Acme GmbH"
        assert letter["role"] == "Senior Data Engineer"
        assert set(letter) >= {
            "id", "type", "name", "title", "company", "role", "created_at",
            "job_id", "job_title", "job_label", "provenance_summary", "updated_at", "last_used_at",
            "open_url", "actions",
        }
        assert letter["open_url"] == f"/cover-letters?id={letter['id']}"
        assert "duplicate" not in letter["actions"]
        assert "yaml_blob" not in letter

    def test_empty_library_is_empty_list(self, server_module, client, fake_supabase):
        body = client.get("/api/documents").get_json()
        assert body["documents"] == [] and body["count"] == 0

    def test_last_used_first_across_types(self, server_module, client, fake_supabase):
        old = _seed_cv(fake_supabase, name="Old CV", ts=1)
        letter = _seed_letter(fake_supabase, name="Edited letter", ts=9)
        rows = fake_supabase.tables["documents"]
        for row in rows:
            if row["id"] == old:
                row["last_used_at"] = 20
            if row["id"] == letter:
                row["last_used_at"] = 5

        docs = client.get("/api/documents").get_json()["documents"]
        assert [d["name"] for d in docs] == ["Old CV", "Edited letter"]

    def test_cv_get_marks_only_that_cv_used(self, server_module, client, fake_supabase):
        selected = _seed_cv(fake_supabase, name="Selected CV", ts=1)
        other = _seed_cv(fake_supabase, name="Other CV", ts=2)

        res = client.get(f"/api/cv?id={selected}")
        assert res.status_code == 200
        rows = {r["id"]: r for r in fake_supabase.tables["documents"]}
        assert rows[selected]["last_used_at"] != 1
        assert rows[other]["last_used_at"] == 2

    def test_cover_letter_get_marks_only_that_letter_used(self, server_module, client, fake_supabase):
        selected = _seed_letter(fake_supabase, name="Selected letter", ts=1)
        other = _seed_letter(fake_supabase, name="Other letter", ts=2)

        res = client.get(f"/api/cover-letter?id={selected}")
        assert res.status_code == 200
        rows = {r["id"]: r for r in fake_supabase.tables["documents"]}
        assert rows[selected]["last_used_at"] != 1
        assert rows[other]["last_used_at"] == 2

    def test_linked_job_title_surfaced_for_cover(self, server_module, client, fake_supabase):
        jid = _seed_job(fake_supabase, title="Staff Data Engineer", company="Globex")
        _seed_letter(fake_supabase, name="Cover — Globex", job_id=jid, ts=2)

        letters = _by_type(client.get("/api/documents").get_json()["documents"], "cover_letter")
        assert letters[0]["job_id"] == jid
        assert letters[0]["job_title"] == "Staff Data Engineer"

    def test_linked_job_title_surfaced_for_cv(self, server_module, client, fake_supabase):
        jid = _seed_job(fake_supabase, title="Staff Data Engineer", company="Globex")
        _seed_cv(fake_supabase, name="CV — Globex", job_id=jid, ts=2)

        cvs = _by_type(client.get("/api/documents").get_json()["documents"], "cv")
        assert cvs[0]["job_id"] == jid
        assert cvs[0]["job_title"] == "Staff Data Engineer"

    def test_cover_without_job_has_null_title(self, server_module, client, fake_supabase):
        _seed_letter(fake_supabase, name="Standalone", job_id=None, ts=2)
        letters = _by_type(client.get("/api/documents").get_json()["documents"], "cover_letter")
        assert letters[0]["job_id"] is None
        assert letters[0]["job_title"] is None

    def test_non_string_cover_metadata_does_not_break_catalog(self, server_module, client, fake_supabase):
        rows = fake_supabase.tables.setdefault("documents", [])
        rows.append(
            {
                "id": str(_uuid.uuid4()),
                "owner_id": fake_supabase.owner_id,
                "type": "cover_letter",
                "name": "Malformed metadata",
                "yaml_blob": _yaml.safe_dump(
                    {
                        "company": {"name": "Acme"},
                        "role": ["Senior", "Engineer"],
                        "body": "x",
                    },
                    sort_keys=False,
                ),
                "job_id": None,
                "deleted_at": None,
                "updated_at": 3,
            }
        )

        res = client.get("/api/documents")
        assert res.status_code == 200
        letter = _by_type(res.get_json()["documents"], "cover_letter")[0]
        assert letter["company"] == ""
        assert letter["role"] == ""
        assert letter["provenance_summary"] == "Standalone document"

    def test_documents_use_fk_job_id_not_stale_blob_job_id(
        self, server_module, client, fake_supabase
    ):
        stale_job = str(_uuid.uuid4())
        rows = fake_supabase.tables.setdefault("documents", [])
        rows.append(
            {
                "id": str(_uuid.uuid4()),
                "owner_id": fake_supabase.owner_id,
                "type": "cover_letter",
                "name": "Stale blob link",
                "yaml_blob": _yaml.safe_dump(
                    {
                        "company": "Ghost Inc",
                        "role": "Data Engineer",
                        "job_id": stale_job,
                        "body": "x",
                    },
                    sort_keys=False,
                ),
                "job_id": None,
                "deleted_at": None,
                "updated_at": 3,
            }
        )

        docs = client.get("/api/documents").get_json()["documents"]
        letter = _by_type(docs, "cover_letter")[0]
        assert letter["job_id"] is None
        assert letter["job_title"] is None

        letters = client.get("/api/cover-letters").get_json()["cover_letters"]
        assert letters[0]["job_id"] is None


# ===========================================================================
#  Stage 7 — /api/cvs surfaces each CV's linked job (id + title) so the
#  editor topbar can show a live ATS-fit badge. RLS-scoped via job_titles_for.
# ===========================================================================
class TestCvListJobLinkage:
    def test_linked_cv_carries_job_id_and_title(self, server_module, client, fake_supabase):
        jid = _seed_job(fake_supabase, title="Staff Data Engineer", company="Globex")
        _seed_cv(fake_supabase, name="Tailored for Globex", job_id=jid, ts=2)

        cvs = client.get("/api/cvs").get_json()["cvs"]
        row = next(c for c in cvs if c["name"] == "Tailored for Globex")
        assert row["job_id"] == jid
        assert row["job_title"] == "Staff Data Engineer"

    def test_unlinked_cv_has_null_job_fields(self, server_module, client, fake_supabase):
        _seed_cv(fake_supabase, name="Standalone CV", job_id=None, ts=1)

        cvs = client.get("/api/cvs").get_json()["cvs"]
        row = next(c for c in cvs if c["name"] == "Standalone CV")
        assert row["job_id"] is None
        assert row["job_title"] is None

    def test_job_id_present_but_title_none_when_job_missing(
        self, server_module, client, fake_supabase
    ):
        # CV stamped with a job_id whose job row no longer exists (deleted job):
        # the id is carried but the title resolves to None (never another
        # tenant's / a stale title).
        ghost = str(_uuid.uuid4())
        _seed_cv(fake_supabase, name="Orphan-linked CV", job_id=ghost, ts=1)

        cvs = client.get("/api/cvs").get_json()["cvs"]
        row = next(c for c in cvs if c["name"] == "Orphan-linked CV")
        assert row["job_id"] == ghost
        assert row["job_title"] is None


# ===========================================================================
#  2. Soft-deleted documents are excluded (deleted_at is null convention).
# ===========================================================================
class TestSoftDeleteExcluded:
    def test_soft_deleted_cv_excluded(self, server_module, client, fake_supabase):
        _seed_cv(fake_supabase, name="Live CV", ts=1)
        _seed_cv(fake_supabase, name="Trashed CV", ts=2, deleted=True)

        docs = client.get("/api/documents").get_json()["documents"]
        names = {d["name"] for d in docs}
        assert "Live CV" in names
        assert "Trashed CV" not in names

    def test_soft_deleted_cover_excluded(self, server_module, client, fake_supabase):
        _seed_letter(fake_supabase, name="Live letter", ts=1)
        _seed_letter(fake_supabase, name="Trashed letter", ts=2, deleted=True)

        docs = client.get("/api/documents").get_json()["documents"]
        names = {d["name"] for d in docs}
        assert "Live letter" in names
        assert "Trashed letter" not in names

    def test_all_deleted_is_empty(self, server_module, client, fake_supabase):
        _seed_cv(fake_supabase, name="Gone CV", ts=1, deleted=True)
        _seed_letter(fake_supabase, name="Gone letter", ts=2, deleted=True)
        body = client.get("/api/documents").get_json()
        assert body["count"] == 0


# ===========================================================================
#  3. Type filter — server returns both; the page filters client-side. We
#     exercise the SAME predicate the JS uses (state.filter === "all" || type).
# ===========================================================================
class TestTypeFilter:
    @staticmethod
    def _filtered(docs, flt):
        return [d for d in docs if flt == "all" or d["type"] == flt]

    def test_filter_predicate_partitions_payload(self, server_module, client, fake_supabase):
        _seed_cv(fake_supabase, name="CV A", ts=1)
        _seed_cv(fake_supabase, name="CV B", ts=2)
        _seed_letter(fake_supabase, name="Letter A", ts=3)

        docs = client.get("/api/documents").get_json()["documents"]
        assert len(self._filtered(docs, "all")) == 3
        assert {d["name"] for d in self._filtered(docs, "cv")} == {"CV A", "CV B"}
        assert {d["name"] for d in self._filtered(docs, "cover_letter")} == {"Letter A"}

    def test_filter_chips_present_in_page_markup(self):
        html = (REPO / "tools" / "editor" / "static" / "cover-letters.html").read_text()
        assert 'data-filter="cover_letter"' in html
        assert 'data-filter="all"' not in html
        assert 'data-filter="cv"' not in html

    def test_frontend_filters_on_state(self):
        js = (REPO / "tools" / "editor" / "static" / "cover-letters.js").read_text()
        assert "/api/documents?type=cover_letter" in js
        assert 'window.location.href = "/app?id="' in js  # CV compatibility path, if supplied
        assert "openLetter(doc.id)" in js  # cover "Open" → cover-letter view


# ===========================================================================
#  4a. The listing path is RLS-scoped (user-JWT client) — NO service-role,
#      NO SECURITY DEFINER aggregate. Source-level assertion (no live DB).
# ===========================================================================
class TestRlsScopedNoServiceRole:
    def test_handler_uses_only_list_documents(self):
        import inspect

        import server

        src = inspect.getsource(server.api_documents)
        # The handler reads via the type-parameterized, RLS-scoped service calls…
        assert 'cl_service.list_documents("cv")' in src
        assert 'cl_service.list_documents("cover_letter")' in src
        assert "cl_service.job_titles_for(" in src
        # …and NEVER reaches for a service-role client or a cross-table RPC.
        assert ".rpc(" not in src, "list handler must not call a SECURITY DEFINER RPC"
        assert "service_role" not in src and "service_supabase" not in src

    def test_list_documents_uses_user_supabase(self):
        import inspect

        import cover_letter_service as cl

        helper_src = inspect.getsource(cl._doc_sb)
        assert "local_demo_store.active()" in helper_src
        assert "auth_mod.user_supabase()" in helper_src
        for fn in (cl.list_documents, cl.job_titles_for):
            src = inspect.getsource(fn)
            assert "_doc_sb()" in src, f"{fn.__name__} must use the per-user/local-demo document client"
            assert ".rpc(" not in src, f"{fn.__name__} must not call an RPC"

    def test_service_role_client_is_confined_to_privileged_boundaries(self):
        # Defense-in-depth: regular reads still use auth.user_supabase. The only
        # privileged constructors allowed in editor code are
        # auth.server_supabase, used for narrow server-owned writes such as
        # reviewed packet lineage, and the standalone erasure worker's
        # _build_worker bootstrap. No request-serving feature module may read
        # service-role env material or build a second Supabase client directly.
        import ast

        editor = REPO / "tools" / "editor"
        bad_names = re.compile(r"service[_-]?role|service_supabase|admin_supabase", re.I)
        bad_env = re.compile(r"SUPABASE_SERVICE|SERVICE_ROLE", re.I)
        offenders: list[str] = []
        for py in editor.glob("*.py"):
            tree = ast.parse(py.read_text(), filename=py.name)
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent

            def enclosing_function(node: ast.AST) -> str | None:
                current = parents.get(node)
                while current is not None:
                    if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return current.name
                    current = parents.get(current)
                return None

            def is_allowed_service_role_boundary(node: ast.AST) -> bool:
                function_name = enclosing_function(node)
                if (
                    py.name == "account_purge_worker.py"
                    and function_name == "_build_worker"
                ):
                    return True
                if py.name != "auth.py":
                    return False
                if function_name == "server_supabase":
                    return True
                current = parents.get(node)
                while current is not None:
                    if isinstance(current, ast.Assign):
                        return any(
                            isinstance(target, ast.Name) and target.id == "SUPABASE_SERVICE_ROLE_KEY"
                            for target in current.targets
                        )
                    current = parents.get(current)
                return False

            for node in ast.walk(tree):
                # A service-role-named identifier used in code (Name / attribute).
                if isinstance(node, ast.Name) and bad_names.search(node.id) and not is_allowed_service_role_boundary(node):
                    offenders.append(f"{py.name}:{node.lineno}: name {node.id!r}")
                if isinstance(node, ast.Attribute) and bad_names.search(node.attr) and not is_allowed_service_role_boundary(node):
                    offenders.append(f"{py.name}:{node.lineno}: attr {node.attr!r}")
                # A secret/service-role env var read (os.environ[...] / .get(...)).
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and bad_env.search(node.value)
                    and not is_allowed_service_role_boundary(node)
                ):
                    offenders.append(f"{py.name}:{node.lineno}: env {node.value!r}")
                # ``create_client`` may appear only inside the two auth helpers
                # or the dedicated erasure-worker bootstrap.
                if isinstance(node, ast.Call):
                    fn = node.func
                    fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
                    if fname == "create_client" and not (
                        (
                            py.name == "auth.py"
                            and enclosing_function(node)
                            in {"user_supabase", "server_supabase"}
                        )
                        or (
                            py.name == "account_purge_worker.py"
                            and enclosing_function(node) == "_build_worker"
                        )
                    ):
                        offenders.append(
                            f"{py.name}:{node.lineno}: create_client outside "
                            "privileged boundaries"
                        )
        assert not offenders, "service-role / non-RLS client use found:\n" + "\n".join(offenders)


# ===========================================================================
#  4b. Cross-tenant matrix against the REAL project (RLS isolation).
#      SKIPS cleanly without the provisioning env vars (same as Stage 2/4/5/6).
#      user B's library must NEVER return user A's CV or cover letter.
# ===========================================================================
A_TOKEN = os.environ.get("TEST_A_TOKEN")
A_ID = os.environ.get("TEST_A_ID")
B_TOKEN = os.environ.get("TEST_B_TOKEN")
B_ID = os.environ.get("TEST_B_ID")
_HAVE_USERS = all([A_TOKEN, A_ID, B_TOKEN, B_ID])


@pytest.mark.skipif(
    not _HAVE_USERS,
    reason="TEST_A_TOKEN/TEST_B_TOKEN (+ ids) not set — provision real users first",
)
class TestCrossTenantLibrary:
    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_b_library_never_returns_a_documents(self):
        """A owns a CV + a cover letter. B's GET /api/documents must contain
        NEITHER — RLS scopes the list to ``owner_id = auth.uid()``."""
        import importlib

        from supabase import create_client

        server = importlib.import_module("server")
        server.app.config.update(TESTING=True)
        client = server.app.test_client()

        url = os.environ["SUPABASE_URL"]
        pub = os.environ["SUPABASE_PUBLISHABLE_KEY"]
        sb_a = create_client(url, pub)
        sb_a.postgrest.auth(A_TOKEN)

        # A creates a CV and a cover letter (both owned by A).
        a_cv_id = (
            sb_a.table("documents")
            .insert({"type": "cv", "name": "A-secret-CV",
                     "yaml_blob": _yaml.safe_dump(GROUNDED_CV, sort_keys=False)})
            .execute()
            .data[0]["id"]
        )
        a_cover_id = (
            sb_a.table("documents")
            .insert({"type": "cover_letter", "name": "A-secret-cover",
                     "yaml_blob": _yaml.safe_dump({"company": "ACorp", "body": "x"})})
            .execute()
            .data[0]["id"]
        )
        b_cv_id = None
        try:
            # A's own library DOES contain both (sanity — RLS lets A see A).
            a_docs = client.get("/api/documents", headers=self._auth(A_TOKEN)).get_json()["documents"]
            a_ids = {d["id"] for d in a_docs}
            assert a_cv_id in a_ids and a_cover_id in a_ids

            # B creates one CV of their own, then lists.
            b_cv_id = (
                client.post("/api/cv", json={"yaml": _yaml.safe_dump(GROUNDED_CV, sort_keys=False)},
                            headers=self._auth(B_TOKEN)).get_json().get("id")
            )
            b_docs = client.get("/api/documents", headers=self._auth(B_TOKEN)).get_json()["documents"]
            b_ids = {d["id"] for d in b_docs}

            assert a_cv_id not in b_ids, "RLS leak: B's library returned A's CV"
            assert a_cover_id not in b_ids, "RLS leak: B's library returned A's cover letter"
            # B's own CV is present (no over-block).
            if b_cv_id:
                assert b_cv_id in b_ids, "B's own CV missing from B's library"
        finally:
            sb_a.table("documents").delete().eq("owner_id", A_ID).execute()
            if b_cv_id:
                sb_b = create_client(url, pub)
                sb_b.postgrest.auth(B_TOKEN)
                sb_b.table("documents").delete().eq("owner_id", B_ID).execute()
