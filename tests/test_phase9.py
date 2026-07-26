"""Route tests for the editor gateway — photo, polish, theme import, repo
safety, and the per-user CV contract.

These exercise route LOGIC that is independent of which tenant is calling,
so they run with the auth guard stubbed (``bypass_auth``) and, where the
route touches the DB, an in-memory ``fake_supabase``. Real cross-tenant
isolation against Supabase + RLS lives in test_stage2_auth.py.
"""
from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))


@pytest.fixture
def disk_env(server_module, monkeypatch, tmp_path):
    """Redirect REPO/content/themes/design to a temp tree for disk routes."""
    repo_tmp = tmp_path / "cv-test"
    (repo_tmp / "content").mkdir(parents=True)
    (repo_tmp / "themes").mkdir(parents=True)
    (repo_tmp / "design").mkdir(parents=True)
    (repo_tmp / "output").mkdir(parents=True)
    (repo_tmp / "themes" / "default.json").write_text(
        json.dumps({"name": "Default", "accent": "#111111", "font": "serif", "density": "normal"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(server_module, "REPO", repo_tmp)
    monkeypatch.setattr(server_module, "CONTENT_DIR", repo_tmp / "content")
    monkeypatch.setattr(server_module, "THEMES_DIR", repo_tmp / "themes")
    return repo_tmp


# A valid CV body (passes content.load: name + contact + a spine section).
VALID_CV = (
    "name: Test\n"
    "contact:\n"
    "  - { label: x, href: 'mailto:x@example.com' }\n"
    "experience:\n"
    "  - role: Eng\n"
    "    company: Acme\n"
    "    location: x\n"
    "    start: '2020'\n"
    "    end: 'Present'\n"
    "    bullets:\n"
    "      - 'A bullet'\n"
)


class TestPrimaryCvEndpoints:
    """Stage 2 contract: single primary CV per user, stored in Postgres."""

    def test_get_seeds_skeleton_when_empty(self, client, fake_supabase):
        res = client.get("/api/cv")
        assert res.status_code == 200
        body = res.get_data(as_text=True)
        # A realistic first-run template was seeded so the editor is useful,
        # but it is still safe placeholder data and can be deleted.
        assert "name: John Doe" in body
        assert "john.doe@example.com" in body
        assert "Example Systems" in body
        assert len(fake_supabase.tables.get("documents", [])) == 1
        row = fake_supabase.tables["documents"][0]
        assert row["name"] == "Template CV - John Doe"
        assert row["accent"] == "#0f766e"
        assert row["font"] == "sans"
        assert row["density"] == "tight"

    def test_seeded_skeleton_builds_pdf(self, client, fake_supabase, monkeypatch):
        pytest.importorskip("weasyprint")
        for var in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
            monkeypatch.delenv(var, raising=False)
        assert client.get("/api/cv").status_code == 200

        res = client.post("/api/build", json={})

        assert res.status_code == 200, res.get_data(as_text=True)
        assert res.content_type.startswith("application/pdf")

    def test_first_run_seed_is_not_duplicated(self, client, fake_supabase):
        assert client.get("/api/cvs").status_code == 200
        assert client.get("/api/cv").status_code == 200
        docs = [d for d in fake_supabase.tables.get("documents", []) if d["type"] == "cv"]
        assert len(docs) == 1
        assert docs[0]["name"] == "Template CV - John Doe"

    def test_existing_user_does_not_receive_template(self, client, fake_supabase):
        existing = client.post("/api/cvs", json={"name": "Existing CV"}).get_json()["id"]
        data = client.get("/api/cvs").get_json()
        assert [c["id"] for c in data["cvs"]] == [existing]
        assert [d["name"] for d in fake_supabase.tables["documents"] if d["type"] == "cv"] == ["Existing CV"]

    def test_post_then_get_roundtrip(self, client, fake_supabase):
        post = client.post("/api/cv", json={"content": VALID_CV})
        assert post.status_code == 200
        assert post.get_json()["saved"] is True
        got = client.get("/api/cv", headers={})
        assert "company: Acme" in got.get_data(as_text=True)

    def test_post_with_stale_revision_is_conflict(self, client, fake_supabase):
        created = client.post("/api/cvs", json={"name": "Revisioned"}).get_json()
        first = client.post(f"/api/cv?id={created['id']}", json={"content": VALID_CV})
        assert first.status_code == 200
        revision = first.get_json()["updated_at"]

        changed = VALID_CV.replace("company: Acme", "company: Beta")
        second = client.post(
            f"/api/cv?id={created['id']}",
            json={"content": changed, "id": created["id"], "revision": revision},
        )
        assert second.status_code == 200

        stale = client.post(
            f"/api/cv?id={created['id']}",
            json={"content": VALID_CV.replace("company: Acme", "company: Gamma"), "id": created["id"], "revision": revision},
        )
        assert stale.status_code == 409
        assert stale.get_json()["code"] == "revision_conflict"
        got = client.get(f"/api/cv?id={created['id']}")
        assert "company: Beta" in got.get_data(as_text=True)

    def test_post_with_stale_revision_is_idempotent_for_same_content(self, client, fake_supabase):
        created = client.post("/api/cvs", json={"name": "Revisioned"}).get_json()
        first = client.post(f"/api/cv?id={created['id']}", json={"content": VALID_CV})
        assert first.status_code == 200
        revision = first.get_json()["updated_at"]

        changed = VALID_CV.replace("company: Acme", "company: Beta")
        second = client.post(
            f"/api/cv?id={created['id']}",
            json={"content": changed, "id": created["id"], "revision": revision},
        )
        assert second.status_code == 200

        duplicate = client.post(
            f"/api/cv?id={created['id']}",
            json={"content": changed, "id": created["id"], "revision": revision},
        )
        assert duplicate.status_code == 200
        assert duplicate.get_json()["saved"] is True
        got = client.get(f"/api/cv?id={created['id']}")
        assert "company: Beta" in got.get_data(as_text=True)

    def test_post_updates_normalized_appearance_metadata_columns(self, client, fake_supabase):
        body = (
            "name: Test\n"
            "accent: '  #654321  '\n"
            "font: Mono\n"
            "density: Tight\n"
            "contact:\n"
            "  - { label: x, href: 'mailto:x@example.com' }\n"
            "experience:\n"
            "  - role: Eng\n"
            "    company: Acme\n"
            "    location: x\n"
            "    start: '2020'\n"
            "    end: 'Present'\n"
            "    bullets:\n"
            "      - 'A bullet'\n"
        )
        post = client.post("/api/cv", json={"content": body})
        assert post.status_code == 200, post.get_data(as_text=True)
        row = fake_supabase.tables["documents"][0]
        assert row["yaml_blob"] == body
        assert row["accent"] == "#654321"
        assert row["font"] == "mono"
        assert row["density"] == "tight"

    def test_post_invalid_yaml_rejected(self, client, fake_supabase):
        res = client.post("/api/cv", json={"content": "name: OnlyName\n"})
        assert res.status_code == 400
        assert fake_supabase.tables.get("documents", []) == []

    def test_cvs_returns_seeded_cv(self, client, fake_supabase):
        # Stage 3 contract: /api/cvs lists CVs as {id,name,slug,updated_at}
        # and surfaces the free-tier soft cap. With no CVs yet, one is seeded.
        res = client.get("/api/cvs")
        assert res.status_code == 200
        data = res.get_json()
        assert len(data["cvs"]) == 1
        first = data["cvs"][0]
        assert "id" in first and "name" in first
        assert "path" not in first  # old single-tenant shape is gone
        assert data["count"] == 1
        assert data["limit"] >= 1
        assert data["at_limit"] is False


class TestMultiCvCrud:
    """Stage 3 route logic against the in-memory fake (auth-independent).

    Real cross-tenant RLS isolation lives in test_stage3_multicv.py; here we
    just prove the create / rename / duplicate / delete plumbing and the
    id-validation/404 paths behave.
    """

    def test_create_then_list(self, client, fake_supabase):
        created = client.post("/api/cvs", json={"name": "Research CV"})
        assert created.status_code == 201
        new_id = created.get_json()["id"]

        listing = client.get("/api/cvs").get_json()
        ids = {c["id"] for c in listing["cvs"]}
        assert new_id in ids

    def test_create_defaults_name(self, client, fake_supabase):
        res = client.post("/api/cvs", json={})
        assert res.status_code == 201
        assert res.get_json()["name"]  # non-empty default

    def test_get_specific_cv_by_id(self, client, fake_supabase):
        created = client.post("/api/cvs", json={"name": "Picked"}).get_json()
        # Overwrite that doc's body via ?id= and read it back.
        client.post(f"/api/cv?id={created['id']}", json={"content": VALID_CV})
        got = client.get(f"/api/cv?id={created['id']}")
        assert got.status_code == 200
        assert "company: Acme" in got.get_data(as_text=True)

    def test_rename(self, client, fake_supabase):
        created = client.post("/api/cvs", json={"name": "Before"}).get_json()
        res = client.post(f"/api/cv/{created['id']}/rename", json={"name": "After"})
        assert res.status_code == 200
        assert res.get_json()["name"] == "After"

    def test_rename_blank_is_400(self, client, fake_supabase):
        created = client.post("/api/cvs", json={"name": "X"}).get_json()
        res = client.post(f"/api/cv/{created['id']}/rename", json={"name": "   "})
        assert res.status_code == 400

    def test_duplicate(self, client, fake_supabase):
        created = client.post("/api/cvs", json={"name": "Original"}).get_json()
        client.post(f"/api/cv?id={created['id']}", json={"content": VALID_CV})
        dup = client.post(f"/api/cv/{created['id']}/duplicate")
        assert dup.status_code == 201
        dup_id = dup.get_json()["id"]
        assert dup_id != created["id"]
        assert dup.get_json()["name"].startswith("Copy of")
        # The clone carries the source body.
        body = client.get(f"/api/cv?id={dup_id}").get_data(as_text=True)
        assert "company: Acme" in body

    def test_delete(self, client, fake_supabase):
        created = client.post("/api/cvs", json={"name": "Doomed"}).get_json()
        res = client.delete(f"/api/cv/{created['id']}")
        assert res.status_code == 200
        # Gone from the listing.
        ids = {c["id"] for c in client.get("/api/cvs").get_json()["cvs"]}
        assert created["id"] not in ids

    def test_delete_only_cv_leaves_empty_library(self, client, fake_supabase):
        seeded = client.get("/api/cvs").get_json()["cvs"][0]
        assert seeded["name"] == "Template CV - John Doe"
        assert client.delete(f"/api/cv/{seeded['id']}").status_code == 200

        listing = client.get("/api/cvs").get_json()
        assert listing["cvs"] == []
        assert listing["count"] == 0

        got = client.get("/api/cv")
        assert got.status_code == 404
        assert "No active CVs" in got.get_data(as_text=True)

    def test_invalid_uuid_is_400(self, client, fake_supabase):
        for path in (
            "/api/cv/not-a-uuid/rename",
            "/api/cv/not-a-uuid/duplicate",
        ):
            assert client.post(path, json={"name": "z"}).status_code == 400
        assert client.delete("/api/cv/not-a-uuid").status_code == 400

    def test_unknown_uuid_is_404(self, client, fake_supabase):
        ghost = "11111111-1111-1111-1111-111111111111"
        assert client.post(f"/api/cv/{ghost}/rename", json={"name": "z"}).status_code == 404
        assert client.post(f"/api/cv/{ghost}/duplicate").status_code == 404
        assert client.delete(f"/api/cv/{ghost}").status_code == 404
        # GET /api/cv with an unowned id also 404s.
        assert client.get(f"/api/cv?id={ghost}").status_code == 404


def _real_jpeg(w: int = 40, h: int = 40) -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (w, h), (200, 100, 50)).save(buf, format="JPEG")
    return buf.getvalue()


class TestPhotoUpload:
    def test_upload_jpg(self, client, bypass_auth, fake_supabase, disk_env):
        # Hardened route validates real magic bytes + dimensions and stores the
        # photo owner-namespaced (design/photos/<owner_id>/photo.<ext>).
        res = client.post(
            "/api/asset/photo",
            data={"file": (BytesIO(_real_jpeg()), "test.jpg")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["path"] == f"design/photos/{bypass_auth}/photo.jpg"
        assert (disk_env / "design" / "photos" / bypass_auth / "photo.jpg").exists()

    def test_unsupported_extension_rejected(self, client, bypass_auth, disk_env):
        res = client.post(
            "/api/asset/photo",
            data={"file": (BytesIO(b"hello"), "test.txt")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 400

    def test_delete_photo(self, client, bypass_auth, fake_supabase, disk_env):
        owner_dir = disk_env / "design" / "photos" / bypass_auth
        owner_dir.mkdir(parents=True)
        (owner_dir / "photo.jpg").write_bytes(b"x")
        res = client.delete("/api/asset/photo")
        assert res.status_code == 200
        assert not (owner_dir / "photo.jpg").exists()


class TestThemeImport:
    def test_theme_list_is_public_read_only(self, client, disk_env):
        res = client.get("/api/themes")
        assert res.status_code == 200
        assert res.get_json()["themes"][0]["id"] == "default"

    def test_shipped_theme_presets_normalize(self, server_module):
        expected = {"default", "forest", "harvard", "linear", "monochrome"}
        themes_dir = REPO / "themes"
        seen = {}
        for path in themes_dir.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            theme = server_module._normalise_theme(data, fallback_name=path.stem.title())
            if theme:
                seen[path.stem] = theme
        assert set(seen) == expected
        assert seen["default"] == {
            "name": "Default",
            "accent": "#111111",
            "font": "serif",
            "density": "normal",
            "paper": "white",
        }

    def test_remote_theme_import_is_disabled_without_fetch_or_write(
        self, client, bypass_auth, disk_env, server_module, monkeypatch
    ):
        import ssrf

        fetched = []
        monkeypatch.setattr(
            ssrf,
            "safe_fetch_url",
            lambda *_a, **_k: fetched.append(True),
        )

        res = client.post(
            "/api/themes/import",
            json={"url": "https://example.com/theme.json"},
        )

        assert res.status_code == 404
        assert res.get_json()["code"] == "theme_import_disabled"
        assert fetched == []
        assert sorted(p.name for p in (disk_env / "themes").glob("*.json")) == [
            "default.json"
        ]

    def test_themes_list_skips_invalid_theme_files(self, client, bypass_auth, disk_env):
        themes = disk_env / "themes"
        (themes / "valid.json").write_text(
            json.dumps({"name": "Valid", "accent": "#123abc", "font": "mono", "density": "airy"}),
            encoding="utf-8",
        )
        (themes / "bad.json").write_text(
            json.dumps({"name": "Bad", "accent": "red", "font": "comic", "density": "huge"}),
            encoding="utf-8",
        )
        res = client.get("/api/themes")
        assert res.status_code == 200
        items = {t["id"]: t for t in res.get_json()["themes"]}
        assert items["valid"] == {
            "id": "valid",
            "name": "Valid",
            "accent": "#123abc",
            "font": "mono",
            "density": "airy",
            "paper": "white",
        }
        assert "bad" not in items


class TestPreviewDensityFallback:
    @pytest.mark.parametrize("query", ["", "?density=garbage"])
    def test_preview_missing_or_invalid_density_delegates_to_yaml(
        self, client, fake_supabase, server_module, monkeypatch, query
    ):
        body = VALID_CV + "density: airy\n"
        assert client.post("/api/cv", json={"content": body}).status_code == 200
        seen = {}

        def fake_render(_path, *, theme, mode, density, font_override, paper_override=None, embedded=False, **_kw):
            seen["density"] = density
            seen["paper"] = paper_override
            return "<!doctype html><html><body>ok</body></html>"

        monkeypatch.setattr(server_module, "render_cv_html", fake_render)
        res = client.get(f"/api/preview{query}")
        assert res.status_code == 200
        assert seen["density"] == ""
        assert seen["paper"] is None

    def test_render_preview_empty_density_honors_yaml_density(self, tmp_path):
        import render_preview as rp

        cv = tmp_path / "cv.yaml"
        cv.write_text(VALID_CV + "density: airy\npaper: creme\n", encoding="utf-8")
        html = rp.render_cv_html(
            cv,
            mode="paged",
            density="",
        )
        assert 'class="page density-airy"' in html
        assert 'data-paper="creme"' in html

    def test_preview_paper_query_overrides_yaml(self, client, fake_supabase, server_module, monkeypatch):
        body = VALID_CV + "paper: white\n"
        assert client.post("/api/cv", json={"content": body}).status_code == 200
        seen = {}

        def fake_render(_path, *, theme, mode, density, font_override, paper_override=None, embedded=False, **_kw):
            seen["paper"] = paper_override
            return "<!doctype html><html><body>ok</body></html>"

        monkeypatch.setattr(server_module, "render_cv_html", fake_render)
        res = client.get("/api/preview?paper=creme")
        assert res.status_code == 200
        assert seen["paper"] == "creme"


class TestPolishEndpoint:
    def test_needs_key_without_key_or_credits(self, client, fake_supabase, monkeypatch):
        # Stage 4 changed the contract: with no BYO key AND no managed
        # credits, polish gates with 402 needs_key (was a 503 in phase 9,
        # when the operator env key was the only gate). Seed a profile with
        # zero credits and no stored key.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        fake_supabase.tables.setdefault("profiles", []).append(
            {"id": fake_supabase.owner_id, "managed_credits_remaining": 0}
        )
        res = client.post("/api/polish/bullet", json={"text": "did stuff"})
        assert res.status_code == 402
        assert res.get_json()["code"] == "needs_key"

    def test_validates_input(self, client, fake_supabase, monkeypatch):
        # Input validation happens BEFORE key/credit resolution, so an empty
        # body is a clean 400 regardless of key state.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
        res = client.post("/api/polish/bullet", json={})
        assert res.status_code == 400

    def test_section_validates_input(self, client, fake_supabase, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
        res = client.post("/api/polish/section", json={"bullets": []})
        assert res.status_code == 400


class TestRepoRouteSafety:
    def test_repo_route_serves_existing_file(self, client, bypass_auth, disk_env):
        (disk_env / "design" / "test.txt").write_text("ok", encoding="utf-8")
        res = client.get("/repo/design/test.txt")
        assert res.status_code == 200
        assert res.get_data(as_text=True) == "ok"

    def test_repo_route_rejects_traversal(self, client, bypass_auth, disk_env):
        res = client.get("/repo/../etc/passwd")
        assert res.status_code in (400, 404)
