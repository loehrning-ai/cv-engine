"""Stage 8 tests — R2 PDF storage with content-hash cache + signed URLs.

Covers the feature's contract, with NO real R2 (creds unset) — an in-memory
FAKE S3 client is injected at ``pdf_store._S3_CLIENT`` (we deliberately do NOT
use moto / any network):

  * **R2-enabled cache**: a build renders ONCE, uploads, and returns a presigned
    URL; a SECOND build of UNCHANGED content is a CACHE HIT — the render fn is
    NOT called again (asserted via a spy counting renders across two builds);
    after a content change the prior object and row are deleted before the
    document changes, and the next build re-renders one replacement artifact.
  * **Graceful fallback** (R2 unset — the dev/test default): ``/api/build``
    streams ``application/pdf`` bytes exactly as before Stage 8 — the app is
    fully functional with no R2.
  * **Concurrency bound**: the module-level ``Semaphore(2)`` caps concurrent
    WeasyPrint renders at 2 (asserted via a counting spy that records the peak
    in-flight count under a burst of threads).
  * **Cover letters** go through the SAME store path (multi-page allowed): a
    second build of the same letter is a cache hit.
  * **Cross-tenant**: ``pdf_artifacts`` are RLS-scoped (B can't fetch A's
    artifact / URL) — a real-JWT test gated on env, skipping cleanly when unset.

The ``fake_supabase`` fixture (conftest) backs the ``pdf_artifacts`` table; its
``_Query`` already supports the ``eq``/``select``/``insert``/``update``/``limit``
chain the cache uses. ``r2_enabled()`` is toggled purely via the four ``R2_*``
env vars (monkeypatched), so no real bucket is ever touched.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid as _uuid
from pathlib import Path

import pytest
import yaml as _yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"

# A minimal valid CV (passes the content validator's "spine" check).
GROUNDED_CV = {
    "name": "Tim Loehr",
    "accent": "#1f6f43",
    "font": "sans",
    "contact": [{"network": "mail", "username": "tim@example.com"}],
    "experience": [
        {
            "role": "Data Engineer",
            "company": "Meta",
            "location": "Warsaw",
            "start": "2023",
            "end": "Present",
            "bullets": ["Cut pipeline latency by 40% across the ETL stack."],
            "stack": "Python, Spark, Airflow",
        }
    ],
}


# ===========================================================================
#  Fakes & helpers.
# ===========================================================================
class FakeS3:
    """In-memory stand-in for the boto3 S3 client (only the methods we call).

    Records every ``put_object`` under its key and mints a deterministic,
    distinguishable presigned URL per key — enough to assert "uploaded then
    served from cache" without any network.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.dispositions: dict[str, str | None] = {}
        self.presign_params: list[dict] = []
        self.put_calls = 0
        self.delete_calls = 0
        self.deleted_keys: list[str] = []
        self.presign_calls = 0
        self.fail_delete = False
        self.fail_head = False
        self.on_delete = None

    def put_object(self, *, Bucket, Key, Body, ContentType=None, ContentDisposition=None):  # noqa: N803
        self.put_calls += 1
        self.objects[Key] = Body
        self.dispositions[Key] = ContentDisposition
        return {"ETag": '"fake"'}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.delete_calls += 1
        self.deleted_keys.append(Key)
        if self.on_delete is not None:
            self.on_delete(Key)
        if self.fail_delete:
            raise RuntimeError("injected R2 delete failure")
        self.objects.pop(Key, None)
        self.dispositions.pop(Key, None)
        return {}

    def head_object(self, *, Bucket, Key):  # noqa: N803
        if self.fail_head:
            raise RuntimeError("injected R2 HEAD outage")
        if Key not in self.objects:
            error = RuntimeError("not found")
            error.response = {
                "Error": {"Code": "NoSuchKey"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            }
            raise error
        return {"ContentLength": len(self.objects[Key])}

    def generate_presigned_url(self, op, *, Params, ExpiresIn):  # noqa: N803
        self.presign_calls += 1
        self.presign_params.append(dict(Params))
        key = Params["Key"]
        return f"https://fake-r2.example/{key}?sig=abc&ttl={ExpiresIn}"


class RenderSpy:
    """Counts calls to the underlying CV render fn and records peak concurrency.

    Patched in over ``pdf_store.render_cv_pdf`` (the engine fn the semaphore
    wrapper delegates to), so it counts ACTUAL renders while the real
    ``pdf_store.render_cv`` semaphore still governs concurrency.
    """

    def __init__(self, page_count: int = 1, overflow: bool = False, hold: float = 0.0):
        self.calls = 0
        self.page_count = page_count
        self.overflow = overflow
        self.hold = hold
        self._lock = threading.Lock()
        self._in_flight = 0
        self.peak = 0

    def __call__(
        self,
        yaml_text,
        *,
        density=None,
        template=None,
        paper=None,
        base_url=None,
        owner_id=None,
    ):
        with self._lock:
            self.calls += 1
            self._in_flight += 1
            self.peak = max(self.peak, self._in_flight)
        try:
            if self.hold:
                time.sleep(self.hold)
            from render.pdf_service import RenderedPdf

            if self.overflow:
                return RenderedPdf(
                    overflowed=True, page_count=2, pdf_bytes=None, overflow_label="skills"
                )
            return RenderedPdf(
                overflowed=False,
                page_count=self.page_count,
                pdf_bytes=b"%PDF-1.7\nfake-cv-bytes\n%%EOF",
                density=density or "normal",
            )
        finally:
            with self._lock:
                self._in_flight -= 1


def _enable_r2(monkeypatch):
    """Set the four R2_* env vars so ``r2_enabled()`` is True for this test."""
    monkeypatch.setenv("R2_ACCOUNT_ID", "0123456789abcdef0123456789ABCDEF")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak123")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk123")
    monkeypatch.setenv("R2_BUCKET", "onepager-pdfs")


def _disable_r2(monkeypatch):
    """Clear the R2_* env vars so the fallback (direct-stream) path is active."""
    for var in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(var, raising=False)


def _inject_fake_s3(monkeypatch) -> FakeS3:
    """Inject an in-memory FakeS3 as the lazily-built boto3 client."""
    import pdf_store

    fake = FakeS3()
    monkeypatch.setattr(pdf_store, "_S3_CLIENT", fake)
    return fake


def _seed_cv(store, name="My CV", cv_model=None):
    """Insert a CV document into the fake store; return (id, yaml_blob)."""
    blob = _yaml.safe_dump(cv_model or GROUNDED_CV, sort_keys=False, allow_unicode=True)
    rows = store.tables.setdefault("documents", [])
    doc_id = str(_uuid.uuid4())
    rows.append(
        {
            "id": doc_id,
            "owner_id": store.owner_id,
            "type": "cv",
            "name": name,
            "yaml_blob": blob,
            "accent": "#1f6f43",
            "font": "sans",
            "deleted_at": None,
            "updated_at": 1,
        }
    )
    return doc_id, blob


def _artifacts(store):
    return store.tables.get("pdf_artifacts", [])


# ===========================================================================
#  1. Storage abstraction + graceful fallback (no env coupling at import).
# ===========================================================================
class TestStorageAbstraction:
    def test_import_never_requires_r2(self, monkeypatch):
        """Importing pdf_store + reading config must NOT build a client / need
        boto3 when R2 is unconfigured (the client is built lazily on first use).
        """
        import pdf_store

        _disable_r2(monkeypatch)
        # No client has to exist for the module to be usable unconfigured.
        monkeypatch.setattr(pdf_store, "_S3_CLIENT", None)
        assert pdf_store.r2_enabled() is False
        # The render wrappers + hash helper work with zero R2 config.
        assert isinstance(pdf_store.content_hash("a", "b", "c", "d"), str)

    def test_r2_enabled_requires_all_four_vars(self, monkeypatch):
        import pdf_store

        _disable_r2(monkeypatch)
        assert pdf_store.r2_enabled() is False
        # Three of four → still disabled (no partial enablement).
        monkeypatch.setenv("R2_ACCOUNT_ID", "a")
        monkeypatch.setenv("R2_ACCESS_KEY_ID", "b")
        monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "c")
        assert pdf_store.r2_enabled() is False
        monkeypatch.setenv("R2_BUCKET", "d")
        assert pdf_store.r2_enabled() is True

    @pytest.mark.parametrize(
        "account_id",
        [
            "a" * 31,
            "g" * 32,
            "a" * 31 + "/",
            "a" * 31 + ".",
            "a" * 31 + "@",
            "a" * 31 + ":",
        ],
    )
    def test_client_rejects_account_id_endpoint_delimiters(
        self, monkeypatch, account_id
    ):
        import pdf_store

        _enable_r2(monkeypatch)
        monkeypatch.setenv("R2_ACCOUNT_ID", account_id)
        monkeypatch.setattr(pdf_store, "_S3_CLIENT", object())

        with pytest.raises(pdf_store.StorageError, match="32 hexadecimal"):
            pdf_store._client()

    def test_content_hash_is_stable_and_input_sensitive(self):
        import pdf_store

        a = pdf_store.content_hash("blob", "#111", "sans", "normal")
        b = pdf_store.content_hash("blob", "#111", "sans", "normal")
        assert a == b  # same inputs → same hash (the cache key)
        # Any input change → a different hash (forces a re-render).
        assert a != pdf_store.content_hash("blob2", "#111", "sans", "normal")
        assert a != pdf_store.content_hash("blob", "#222", "sans", "normal")
        assert a != pdf_store.content_hash("blob", "#111", "serif", "normal")
        assert a != pdf_store.content_hash("blob", "#111", "sans", "tight")
        assert a != pdf_store.content_hash("blob", "#111", "sans", "normal", paper="creme")

    def test_content_hash_includes_renderer_cache_version(self, monkeypatch):
        import pdf_store

        a = pdf_store.content_hash("blob", "#111", "sans", "normal")
        monkeypatch.setattr(pdf_store, "RENDER_CACHE_VERSION", "different-renderer")
        assert a != pdf_store.content_hash("blob", "#111", "sans", "normal")

    def test_persisted_cv_hash_matches_default_build_appearance_contract(self):
        import pdf_store

        yaml_blob = _yaml.safe_dump(
            {
                **GROUNDED_CV,
                "density": "TIGHT",
                "template": "sidebar",
                "paper": "creme",
            },
            sort_keys=False,
        )

        assert pdf_store.persisted_cv_content_hash(
            yaml_blob, "#1f6f43", "sans"
        ) == pdf_store.content_hash(
            yaml_blob,
            "#1f6f43",
            "sans",
            "tight",
            "sidebar",
            "creme",
        )

    def test_put_and_presign_through_fake_client(self, monkeypatch):
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        pdf_store.put_pdf("o/d/h.pdf", b"%PDF-bytes")
        assert fake.objects["o/d/h.pdf"] == b"%PDF-bytes"
        assert fake.dispositions["o/d/h.pdf"] == 'inline; filename="document.pdf"'
        url = pdf_store.presigned_get("o/d/h.pdf", ttl=900)
        assert "o/d/h.pdf" in url and "ttl=900" in url
        assert fake.presign_params[-1]["ResponseContentDisposition"] == 'inline; filename="document.pdf"'

    def test_delete_through_fake_client_is_idempotent(self, monkeypatch):
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        key = f"{FAKE_OWNER}/{_uuid.uuid4()}/{'a' * 64}.pdf"
        fake.objects[key] = b"%PDF"

        pdf_store.delete_pdf(key)
        pdf_store.delete_pdf(key)

        assert fake.objects == {}
        assert fake.deleted_keys == [key, key]

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("cv.pdf", 'inline; filename="cv.pdf"'),
            ('../../weird "name".pdf', 'inline; filename="weird-name-.pdf"'),
            ("résumé.pdf", 'inline; filename="r-sum-.pdf"'),
            ("cv", 'inline; filename="document.pdf"'),
        ],
    )
    def test_pdf_disposition_is_ascii_safe(self, name, expected):
        import pdf_store

        assert pdf_store._pdf_disposition(name) == expected


# ===========================================================================
#  2. R2-enabled build: render once, upload, signed URL; unchanged → cache hit.
# ===========================================================================
class TestCacheHitAndStale:
    def test_build_uploads_then_second_build_is_cache_hit(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """First build renders + uploads + returns a signed URL; an identical
        second build is a CACHE HIT — the render fn is called ONCE across BOTH
        builds, and no second object is uploaded."""
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        spy = RenderSpy(page_count=1)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", spy)

        cv_id, _ = _seed_cv(fake_supabase)

        # Build #1 — a miss: render, upload, signed URL, JSON {url, cached:false}.
        r1 = client.post("/api/build", json={"id": cv_id})
        assert r1.status_code == 200, r1.get_data(as_text=True)
        assert "application/json" in r1.headers["Content-Type"]
        b1 = r1.get_json()
        assert b1["cached"] is False
        assert b1["page_count"] == 1
        assert b1["url"].startswith("https://fake-r2.example/")
        assert spy.calls == 1
        assert fake.put_calls == 1
        key = next(iter(fake.objects))
        assert fake.dispositions[key] == 'inline; filename="cv.pdf"'
        assert fake.presign_params[-1]["ResponseContentDisposition"] == 'inline; filename="cv.pdf"'
        # Exactly one non-stale artifact row recorded.
        arts = _artifacts(fake_supabase)
        assert len(arts) == 1 and arts[0]["stale"] is False

        # Build #2 — UNCHANGED content: a cache hit, NO re-render, NO new upload.
        r2 = client.post("/api/build", json={"id": cv_id})
        assert r2.status_code == 200
        b2 = r2.get_json()
        assert b2["cached"] is True
        assert b2["url"].startswith("https://fake-r2.example/")
        assert spy.calls == 1, "unchanged content must NOT be re-rendered"
        assert fake.put_calls == 1, "no second upload on a cache hit"

    def test_content_change_deletes_superseded_artifact_and_rerenders(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Editing removes the old object and row before the document changes."""
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        spy = RenderSpy(page_count=1)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", spy)

        cv_id, _ = _seed_cv(fake_supabase)
        assert client.post("/api/build", json={"id": cv_id}).status_code == 200
        assert spy.calls == 1

        # Save NEW content via /api/cv (marks prior artifacts stale).
        changed = dict(GROUNDED_CV)
        changed["experience"] = [
            {**GROUNDED_CV["experience"][0], "bullets": ["A different, edited bullet."]}
        ]
        new_yaml = _yaml.safe_dump(changed, sort_keys=False, allow_unicode=True)
        save = client.post("/api/cv", json={"id": cv_id, "content": new_yaml})
        assert save.status_code == 200, save.get_data(as_text=True)
        assert _artifacts(fake_supabase) == []
        assert len(fake.objects) == 1
        assert fake.delete_calls == 0
        assert len(fake_supabase.tables["pdf_artifact_cleanup_queue"]) == 1

        # Next build re-renders (content changed → new hash, no fresh hit).
        r = client.post("/api/build", json={"id": cv_id})
        assert r.status_code == 200
        assert r.get_json()["cached"] is False
        assert spy.calls == 2, "edited content must be re-rendered"
        assert fake.put_calls == 2
        arts = _artifacts(fake_supabase)
        assert len(arts) == 1
        assert arts[0]["stale"] is False
        assert len(fake.objects) == 2

    def test_overflow_not_uploaded_or_cached(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """A 2-page CV (R2 enabled) → 400 overflow, and NOTHING is uploaded or
        recorded (a 2-page CV is never cached)."""
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        spy = RenderSpy(overflow=True)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", spy)

        cv_id, _ = _seed_cv(fake_supabase)
        r = client.post("/api/build", json={"id": cv_id})
        assert r.status_code == 400
        assert r.get_json()["error"] == "overflow"
        assert fake.put_calls == 0
        assert _artifacts(fake_supabase) == []


# ===========================================================================
#  3. Artifact lifecycle: object-first deletion, rollback, hostile-row safety.
# ===========================================================================
class TestArtifactLifecycle:
    def test_document_delete_commits_guarded_row_delete_then_removes_object(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", RenderSpy())
        cv_id, _ = _seed_cv(fake_supabase)
        assert client.post("/api/build", json={"id": cv_id}).status_code == 200

        def assert_guarded_delete_committed(_key):
            assert not any(row["id"] == cv_id for row in fake_supabase.tables["documents"])
            assert not any(row["document_id"] == cv_id for row in _artifacts(fake_supabase))

        fake.on_delete = assert_guarded_delete_committed
        response = client.delete(f"/api/cv/{cv_id}")

        assert response.status_code == 200
        assert fake.objects == {}
        assert _artifacts(fake_supabase) == []
        assert fake_supabase.tables["pdf_artifact_cleanup_queue"] == []
        assert not any(
            row["id"] == cv_id for row in fake_supabase.tables["documents"]
        )

    def test_document_cleanup_is_idempotent(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import pdf_store
        from flask import g

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", RenderSpy())
        cv_id, _ = _seed_cv(fake_supabase)
        assert client.post("/api/build", json={"id": cv_id}).status_code == 200

        with server_module.app.test_request_context("/api/cv"):
            g.owner_id = FAKE_OWNER
            g.access_token = "test-token"
            assert pdf_store.delete_document_artifacts(cv_id, force=True) == 1
            assert pdf_store.delete_document_artifacts(cv_id) == 0

        assert fake.delete_calls == 1
        assert fake.objects == {}
        assert _artifacts(fake_supabase) == []
        assert any(row["id"] == cv_id for row in fake_supabase.tables["documents"])

    def test_remote_cleanup_failure_after_guarded_delete_is_truthful(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", RenderSpy())
        cv_id, _ = _seed_cv(fake_supabase)
        assert client.post("/api/build", json={"id": cv_id}).status_code == 200
        retained_key = next(iter(fake.objects))
        fake.fail_delete = True

        response = client.delete(f"/api/cv/{cv_id}")

        assert response.status_code == 503
        assert response.get_json() == {
            "error": "PDF artifact storage is temporarily unavailable.",
            "code": "artifact_cleanup_unavailable",
            "document_deleted": True,
        }
        assert not any(row["id"] == cv_id for row in fake_supabase.tables["documents"])
        assert _artifacts(fake_supabase) == []
        assert fake.objects[retained_key].startswith(b"%PDF")
        queued = fake_supabase.tables["pdf_artifact_cleanup_queue"]
        assert len(queued) == 1
        assert queued[0]["document_id"] == cv_id
        assert queued[0]["attempt_count"] == 1

        fake.fail_delete = False
        assert client.get("/api/cvs").status_code == 200
        assert fake_supabase.tables["pdf_artifact_cleanup_queue"] == []
        assert retained_key not in fake.objects

    def test_cv_change_commits_and_leases_remote_cleanup(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", RenderSpy())
        cv_id, _old_yaml = _seed_cv(fake_supabase)
        assert client.post("/api/build", json={"id": cv_id}).status_code == 200
        old_key = next(iter(fake.objects))
        changed = dict(GROUNDED_CV)
        changed["experience"] = [
            {
                **GROUNDED_CV["experience"][0],
                "bullets": ["This mutation must not commit."],
            }
        ]
        new_yaml = _yaml.safe_dump(changed, sort_keys=False, allow_unicode=True)
        fake.fail_delete = True

        response = client.post(
            "/api/cv",
            json={"id": cv_id, "content": new_yaml},
        )

        assert response.status_code == 200
        assert response.get_json()["cleanup_pending"] is True
        row = next(
            row for row in fake_supabase.tables["documents"] if row["id"] == cv_id
        )
        assert row["yaml_blob"] == new_yaml
        assert fake.objects[old_key].startswith(b"%PDF")
        assert _artifacts(fake_supabase) == []
        assert len(fake_supabase.tables["pdf_artifact_cleanup_queue"]) == 1

    def test_ambiguous_insert_failure_retains_candidate_for_durable_cleanup(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", RenderSpy())
        monkeypatch.setattr(
            pdf_store,
            "_insert_artifact",
            lambda *_a, **_k: (_ for _ in ()).throw(
                pdf_store.StorageError("injected insert failure")
            ),
        )
        cv_id, _ = _seed_cv(fake_supabase)

        response = client.post("/api/build", json={"id": cv_id})

        # Build storage failures retain the established direct-stream fallback.
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/pdf"
        assert fake.put_calls == 1
        assert fake.delete_calls == 0
        assert len(fake.objects) == 1
        assert _artifacts(fake_supabase) == []
        assert len(fake_supabase.tables["pdf_artifact_cleanup_queue"]) == 1

    def test_hostile_cache_row_is_never_presigned_or_deleted(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", RenderSpy())
        cv_id, yaml_blob = _seed_cv(fake_supabase)
        chash = pdf_store.content_hash(
            yaml_blob,
            "#1f6f43",
            "sans",
            "normal",
            "classic",
            "white",
        )
        other_owner = "00000000-0000-0000-0000-000000000002"
        hostile_key = f"{other_owner}/{cv_id}/{chash}.pdf"
        fake.objects[hostile_key] = b"%PDF-other-tenant"
        fake_supabase.tables.setdefault("pdf_artifacts", []).append(
            {
                "id": str(_uuid.uuid4()),
                "owner_id": FAKE_OWNER,
                "document_id": cv_id,
                "r2_key": hostile_key,
                "content_hash": chash,
                "page_count": 1,
                "stale": False,
            }
        )

        response = client.post("/api/build", json={"id": cv_id})

        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/pdf"
        assert fake.presign_calls == 0
        assert fake.delete_calls == 0
        assert fake.put_calls == 0
        assert fake.objects[hostile_key] == b"%PDF-other-tenant"

    def test_hostile_key_blocks_document_and_account_mutations_without_r2_call(
        self, client, fake_supabase, monkeypatch
    ):
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)
        cv_id, yaml_blob = _seed_cv(fake_supabase)
        chash = pdf_store.content_hash(yaml_blob, "", "", "normal")
        hostile_key = (
            f"00000000-0000-0000-0000-000000000002/{cv_id}/{chash}.pdf"
        )
        fake.objects[hostile_key] = b"%PDF-other-tenant"
        fake_supabase.tables.setdefault("pdf_artifacts", []).append(
            {
                "id": str(_uuid.uuid4()),
                "owner_id": FAKE_OWNER,
                "document_id": cv_id,
                "r2_key": hostile_key,
                "content_hash": chash,
                "page_count": 1,
                "stale": False,
            }
        )

        deleted = client.delete(f"/api/cv/{cv_id}")
        account = client.post(
            "/api/account/delete",
            json={"confirm": "DELETE"},
        )

        assert deleted.status_code == 503
        assert account.status_code == 200
        assert account.get_json()["invalid_artifact_count"] == 1
        assert account.get_json()["cleanup_pending"] is True
        assert fake.delete_calls == 0
        row = next(
            row for row in fake_supabase.tables["documents"] if row["id"] == cv_id
        )
        assert row["deleted_at"] is not None

    def test_preview_threads_verified_owner_to_renderer(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        seen = {}

        def fake_render(_path, **kwargs):
            seen["owner_id"] = kwargs.get("owner_id")
            return "<!doctype html><html><body>ok</body></html>"

        monkeypatch.setattr(server_module, "render_cv_html", fake_render)
        cv_id, _ = _seed_cv(fake_supabase)

        response = client.get(f"/api/preview?id={cv_id}")

        assert response.status_code == 200
        assert seen["owner_id"] == FAKE_OWNER


# ===========================================================================
#  4. Graceful fallback — R2 unset → /api/build streams application/pdf bytes.
# ===========================================================================
class TestFallbackStream:
    def test_build_streams_pdf_bytes_when_r2_unset(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """R2 unconfigured: /api/build renders + streams an application/pdf blob
        exactly as before Stage 8 (the dev/no-R2 path still downloads)."""
        import pdf_store

        _disable_r2(monkeypatch)
        spy = RenderSpy(page_count=1)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", spy)

        cv_id, _ = _seed_cv(fake_supabase)
        r = client.post("/api/build", json={"id": cv_id})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.headers["Content-Type"] == "application/pdf"
        assert r.get_data().startswith(b"%PDF")
        assert spy.calls == 1
        # Fallback path records NO artifact (no R2 → nothing to cache).
        assert _artifacts(fake_supabase) == []

    @pytest.mark.parametrize("payload_extra", [{}, {"density": "garbage"}])
    def test_build_missing_or_invalid_density_delegates_to_yaml(
        self, server_module, client, fake_supabase, monkeypatch, payload_extra
    ):
        import pdf_store
        from render.pdf_service import RenderedPdf

        _disable_r2(monkeypatch)
        seen = {}

        def fake_render(yaml_text, *, density=None, template=None, paper=None):
            seen["density"] = density
            seen["template"] = template
            seen["paper"] = paper
            return RenderedPdf(
                overflowed=False,
                page_count=1,
                pdf_bytes=b"%PDF-1.7\nfake\n%%EOF",
                density=density or "airy",
            )

        monkeypatch.setattr(pdf_store, "render_cv", fake_render)
        cv = dict(GROUNDED_CV)
        cv["density"] = "airy"
        cv_id, _ = _seed_cv(fake_supabase, cv_model=cv)
        payload = {"id": cv_id, **payload_extra}
        r = client.post("/api/build", json=payload)
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.headers["Content-Type"] == "application/pdf"
        assert seen["density"] is None

    def test_build_renderer_failure_returns_json_503(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import pdf_store

        _disable_r2(monkeypatch)
        monkeypatch.setattr(
            pdf_store,
            "render_cv",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("WeasyPrint missing")),
        )
        cv_id, _ = _seed_cv(fake_supabase)
        r = client.post("/api/build", json={"id": cv_id})
        assert r.status_code == 503
        body = r.get_json()
        assert "render failed: RuntimeError" in body["error"]
        assert "WeasyPrint missing" in body["error"]

    def test_storage_error_degrades_to_stream(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """If an R2 op fails mid-build, the route degrades to a direct stream
        rather than 500 (StorageError → fallback)."""
        import pdf_store

        _enable_r2(monkeypatch)
        spy = RenderSpy(page_count=1)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", spy)
        # Force the cache-aware build to raise StorageError (e.g. R2 down).
        monkeypatch.setattr(
            pdf_store,
            "build_cv_pdf",
            lambda **_k: (_ for _ in ()).throw(pdf_store.StorageError("R2 down")),
        )
        cv_id, _ = _seed_cv(fake_supabase)
        r = client.post("/api/build", json={"id": cv_id})
        assert r.status_code == 200
        assert r.headers["Content-Type"] == "application/pdf"
        assert r.get_data().startswith(b"%PDF")

    def test_r2_build_renderer_failure_returns_json_503(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        import pdf_store

        _enable_r2(monkeypatch)
        monkeypatch.setattr(
            pdf_store,
            "build_cv_pdf",
            lambda **_k: (_ for _ in ()).throw(RuntimeError("native render failed")),
        )
        cv_id, _ = _seed_cv(fake_supabase)
        r = client.post("/api/build", json={"id": cv_id})
        assert r.status_code == 503
        assert "native render failed" in r.get_json()["error"]


# ===========================================================================
#  4. Concurrency bound — the Semaphore(2) caps in-flight WeasyPrint renders.
# ===========================================================================
class TestSemaphoreBound:
    def test_semaphore_caps_concurrent_renders_at_two(self, monkeypatch):
        """Fire 8 renders through the semaphore-bounded wrapper concurrently;
        the peak number of renders in flight at once never exceeds 2."""
        import pdf_store

        # A spy that HOLDS during each render so overlap is observable, and
        # records the peak in-flight count.
        spy = RenderSpy(page_count=1, hold=0.05)
        monkeypatch.setattr(pdf_store, "render_cv_pdf", spy)

        def _worker():
            pdf_store.render_cv("name: X\nexperience: []\n", density="normal")

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert spy.calls == 8
        assert spy.peak <= 2, f"semaphore breached: {spy.peak} renders in flight"
        assert spy.peak == 2, "expected the bound to actually be reached under the burst"


# ===========================================================================
#  5. Cover letters go through the same store path (multi-page allowed).
# ===========================================================================
class TestCoverLetterStorePath:
    def _seed_letter(self, store, body="Dear team,\n\nI am keen.\n\nRegards"):
        import cover_letter_service as cl

        blob = cl.serialize_blob(
            {
                "recipient": "Hiring Team",
                "company": "Acme GmbH",
                "role": "Data Engineer",
                "tone": "auto",
                "body": body,
                "language": "en",
                "source_cv_id": None,
                "job_id": None,
            }
        )
        rows = store.tables.setdefault("documents", [])
        doc_id = str(_uuid.uuid4())
        rows.append(
            {
                "id": doc_id,
                "owner_id": store.owner_id,
                "type": "cover_letter",
                "name": "Data Engineer — Acme GmbH",
                "yaml_blob": blob,
                "deleted_at": None,
                "updated_at": 1,
            }
        )
        return doc_id

    def test_letter_build_uses_store_and_caches(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """A cover-letter build (R2 enabled) renders once, uploads, returns a
        signed URL; a second build of the same letter is a cache hit. Multi-page
        is allowed (the spy returns 3 pages — no 1-page gate on letters)."""
        import pdf_store

        _enable_r2(monkeypatch)
        fake = _inject_fake_s3(monkeypatch)

        calls = {"n": 0}

        def _fake_render_letter(doc, *, base_url=""):
            calls["n"] += 1
            return b"%PDF-1.7\nfake-letter\n%%EOF"

        monkeypatch.setattr(pdf_store, "render_cover_letter", _fake_render_letter)
        monkeypatch.setattr(pdf_store, "_count_pdf_pages", lambda _b: 3)

        letter_id = self._seed_letter(fake_supabase)

        r1 = client.get(f"/api/cover-letter/{letter_id}/build")
        assert r1.status_code == 200, r1.get_data(as_text=True)
        assert "application/json" in r1.headers["Content-Type"]
        b1 = r1.get_json()
        assert b1["cached"] is False
        assert b1["page_count"] == 3  # multi-page letter cached fine
        assert calls["n"] == 1
        assert fake.put_calls == 1
        key = next(iter(fake.objects))
        assert fake.dispositions[key] == 'inline; filename="cover-letter.pdf"'
        assert fake.presign_params[-1]["ResponseContentDisposition"] == 'inline; filename="cover-letter.pdf"'

        r2 = client.get(f"/api/cover-letter/{letter_id}/build")
        assert r2.get_json()["cached"] is True
        assert calls["n"] == 1, "unchanged letter must NOT be re-rendered"
        assert fake.put_calls == 1

    def test_letter_build_streams_when_r2_unset(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """R2 unset → the letter build streams application/pdf bytes (fallback)."""
        import pdf_store

        _disable_r2(monkeypatch)
        monkeypatch.setattr(
            pdf_store, "render_cover_letter", lambda doc, *, base_url="": b"%PDF-letter"
        )
        letter_id = self._seed_letter(fake_supabase)
        r = client.get(f"/api/cover-letter/{letter_id}/build")
        assert r.status_code == 200
        assert r.headers["Content-Type"] == "application/pdf"
        assert r.get_data().startswith(b"%PDF")
        assert _artifacts(fake_supabase) == []


# ===========================================================================
#  6. Cross-tenant — pdf_artifacts are RLS-scoped (B can't see A's artifact).
#  Real-JWT pattern; SKIPS cleanly when the provisioning env vars are absent.
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
class TestPdfArtifactsRLS:
    def test_b_cannot_read_a_artifact(self):
        """A inserts a pdf_artifacts row; B (real JWT) must not see it."""
        from supabase import create_client

        url = os.environ["SUPABASE_URL"]
        pub = os.environ["SUPABASE_PUBLISHABLE_KEY"]

        sb_a = create_client(url, pub)
        sb_a.postgrest.auth(A_TOKEN)
        # A needs a document to hang the artifact off (FK).
        doc = (
            sb_a.table("documents")
            .insert({"type": "cv", "name": "A-cv", "yaml_blob": "name: A\nexperience: []\n"})
            .execute()
            .data
        )
        a_doc_id = doc[0]["id"]
        art = (
            sb_a.table("pdf_artifacts")
            .insert(
                {
                    "document_id": a_doc_id,
                    "r2_key": f"{A_ID}/{a_doc_id}/hash.pdf",
                    "content_hash": "hash",
                    "page_count": 1,
                }
            )
            .execute()
            .data
        )
        a_art_id = art[0]["id"]
        try:
            sb_b = create_client(url, pub)
            sb_b.postgrest.auth(B_TOKEN)
            seen = (
                sb_b.table("pdf_artifacts").select("id").eq("id", a_art_id).execute().data
            )
            assert not (seen or []), "RLS leak: B can read A's pdf_artifact"
        finally:
            sb_a.table("pdf_artifacts").delete().eq("id", a_art_id).execute()
            sb_a.rpc(
                "delete_document_safely",
                {"p_document_id": a_doc_id, "p_document_type": "cv"},
            ).execute()
