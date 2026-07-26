"""QA round 3 — tailor review diff, demo seed source, editor-chrome i18n.

Round-3 review findings, owned by the tailor/i18n lane:

  1. [P2] The tailor review diff was pure re-serialization noise: the stored
     (hand-written) YAML blob was diffed against the safe_dumped tailored
     model, so a ZERO-change review reported the whole document as changed
     (+84/-104). Both sides are now normalized through the same serializer
     before diffing → an empty change set yields an empty diff.
  2. [P2] The local demo shell seeded its primary CV from the operator's real
     ``content/cv.yaml`` whenever that file existed (real PII in a demo
     instance, machine-dependent starter content). In demo mode the seed is
     now pinned to the shipped public sample ``content/cv.example.yaml``
     (Jordan Avery); an explicit ``ONEPAGER_CV_FILE`` pin still wins and
     non-demo seeding is unchanged.
  3. [P2] Core editor-chrome aria-labels/titles/tooltips stayed English in
     German mode. The missing keys were added (EN+DE, parity preserved) and
     wired via ``data-i18n-attr`` in index.html; the JS-rendered form.js
     controls resolve through ``OnePagerI18n.t()`` (via a local ``tr()`` that
     mirrors app.js) and carry ``data-i18n-attr`` so they re-localise live.
  4. [VERIFY-ONLY] Round-2 reported that a tailored copy's "Open in editor"
     (``GET /api/cv?id=<tailored_cv_id>``) returned the SOURCE CV in demo
     mode. Verified FIXED by the local_demo_store reconciliation: the
     explicit id resolves to the tailored row; a random uuid 404s.
  5. [P3] ``X-OnePager-Demo-Write`` was only set on the CV-save fallback.
     A demo-owner after_request hook now marks every successful mutating
     response served from the in-memory demo store.

The demo tests run through the REAL ``/auth/demo-login`` token path (same
pattern as tests/test_qa_demo_multicv.py); the diff tests use the in-memory
``fake_supabase`` fixture with a mocked LLM.
"""
from __future__ import annotations

import io
import json
import re
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "tools" / "editor" / "static"

JD_TEXT = (
    "Title: Senior Data Engineer\n"
    "Company: Acme GmbH\n"
    "You must have experience with Spark and Airflow. 5+ years required.\n"
    "Responsibilities: build ETL pipelines and data models."
)

# A hand-written blob: comments, quoted scalars, 4-space list indentation —
# exactly the shape that made the raw-blob diff report +84/-104 on a
# zero-change review.
HANDWRITTEN_BLOB = """\
# Operator's hand-maintained CV — comments + custom indentation.
name: Tim Loehr
contact:
    - network: mail
      username: tim@example.com
experience:
    - role: Data Engineer
      company: Meta
      location: Warsaw
      start: "2023"
      end: Present
      bullets:
        - Cut pipeline latency by 40% across the ETL stack.
        - Built data models for analytics in Airflow.
      stack: Python, Spark, Airflow
skills:
    - label: Data
      items: Python, SQL, PyTorch, Spark
"""

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
#  Shared helpers (mirror tests/test_stage6_tailor.py + test_qa_demo_multicv.py)
# ---------------------------------------------------------------------------
def _structured_response(suggestions=None, summary=None, warnings=None) -> str:
    """A valid structured tailoring JSON the mock LLM returns."""
    return json.dumps(
        {
            "fit": {"score": 80, "verdict": "Strong match", "target_role": "Senior Data Engineer"},
            "keywords": {"present": [], "missing": [], "inferable": []},
            "suggestions": suggestions or [],
            "summary": summary or {"recommended": False, "text": "", "rationale": ""},
            "warnings": warnings or [],
        }
    )


def _mock_llm(monkeypatch, raw_text: str):
    """Patch tailor_service.make_client so .complete returns ``raw_text``."""
    import tailor_service as ts

    class _FakeClient:
        def __init__(self, api_key):
            pass

        def complete(self, *, system, user, model=None, max_tokens=1024, cache_system=True):
            return raw_text

    monkeypatch.setattr(ts, "make_client", lambda provider, api_key: _FakeClient(api_key))


def _seed_cv_blob(store, blob: str, name="My CV") -> str:
    """Insert a CV document with an EXACT yaml_blob; return its id."""
    rows = store.tables.setdefault("documents", [])
    doc_id = str(uuid.uuid4())
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
    return doc_id


def _seed_profile(store, owner_id=FAKE_OWNER, *, credits=3):
    store.tables.setdefault("profiles", []).append(
        {"id": owner_id, "managed_credits_remaining": credits}
    )


@pytest.fixture
def demo_client(server_module, monkeypatch):
    """A test client running the REAL local-demo auth + storage path."""
    import local_demo_store

    monkeypatch.setenv("ONEPAGER_DEMO_MODE", "true")
    local_demo_store.reset()
    server_module.app.config.update(TESTING=True)
    client = server_module.app.test_client()
    token = client.post("/auth/demo-login").get_json()["access_token"]
    yield client, {"Authorization": f"Bearer {token}"}
    local_demo_store.reset()


# ===========================================================================
#  1. Tailor review diff — normalized on BOTH sides, zero noise.
# ===========================================================================
class TestTailorDiffNormalization:
    def test_zero_change_review_yields_empty_diff(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """The finding: with ZERO applied suggestions the diff must be EMPTY,
        even when the stored blob is hand-written (comments, custom indent)."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(monkeypatch, _structured_response([]))  # no suggestions at all
        cv_id = _seed_cv_blob(fake_supabase, HANDWRITTEN_BLOB)
        _seed_profile(fake_supabase)

        res = client.post(f"/api/cv/{cv_id}/tailor/review", json={"jd_text": JD_TEXT})
        assert res.status_code == 200, res.get_data(as_text=True)
        diff = res.get_json()["diff"]
        assert diff["changed"] is False
        assert diff["added"] == 0
        assert diff["removed"] == 0
        assert diff["lines"] == []

    def test_real_change_shows_only_the_changed_lines(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """A single rewritten bullet diffs as exactly +1/-1 — the surrounding
        re-serialization (comments stripped, indent restyled) is NOT reported."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {"id": "tighten", "op": "rewrite_bullet", "target": "experience", "index": 0,
             "bullet_index": 0, "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": "Cut ETL pipeline latency 40% with Spark and Airflow."},
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id = _seed_cv_blob(fake_supabase, HANDWRITTEN_BLOB)
        _seed_profile(fake_supabase)

        res = client.post(f"/api/cv/{cv_id}/tailor/review", json={"jd_text": JD_TEXT})
        assert res.status_code == 200, res.get_data(as_text=True)
        diff = res.get_json()["diff"]
        assert diff["changed"] is True
        assert diff["added"] == 1
        assert diff["removed"] == 1
        assert any("Cut ETL pipeline latency" in line for line in diff["lines"])
        # No hand-written-blob artifacts leak into the diff as pseudo-changes.
        assert not any("hand-maintained" in line for line in diff["lines"])

    def test_source_hash_still_tracks_the_raw_stored_blob(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Only the DIFF normalizes: source_hash must remain the raw-blob hash
        (apply_tailor_review re-hashes the stored row to detect drift)."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        _mock_llm(monkeypatch, _structured_response([]))
        cv_id = _seed_cv_blob(fake_supabase, HANDWRITTEN_BLOB)
        _seed_profile(fake_supabase)

        res = client.post(f"/api/cv/{cv_id}/tailor/review", json={"jd_text": JD_TEXT})
        assert res.status_code == 200, res.get_data(as_text=True)
        import hashlib

        assert res.get_json()["source_hash"] == hashlib.sha256(
            HANDWRITTEN_BLOB.encode("utf-8")
        ).hexdigest()


# ===========================================================================
#  2. Demo shell seeds the public sample CV — never the operator's cv.yaml.
# ===========================================================================
class TestDemoSeedSource:
    def test_demo_login_serves_example_cv_not_operator_cv(self, demo_client):
        """content/cv.yaml EXISTS in this repo (operator data); the demo shell
        must still serve the shipped public sample, byte-identically."""
        client, headers = demo_client
        example = (REPO / "content" / "cv.example.yaml").read_text(encoding="utf-8")
        assert "Jordan Avery" in example  # the fixture premise

        res = client.get("/api/cv", headers=headers)
        assert res.status_code == 200
        body = res.get_data(as_text=True)
        assert "Jordan Avery" in body
        assert body == example

    def test_demo_seed_is_deterministic_across_logins(self, demo_client, server_module):
        """A store reset (demo shell restart) re-seeds the SAME sample."""
        import local_demo_store

        client, headers = demo_client
        first = client.get("/api/cv", headers=headers).get_data(as_text=True)
        local_demo_store.reset()
        second = client.get("/api/cv", headers=headers).get_data(as_text=True)
        assert first == second

    def test_explicit_override_still_wins_in_demo_mode(self, server_module, monkeypatch):
        """ONEPAGER_CV_FILE is the documented explicit pin — it beats the
        demo-mode default (an operator-intentional demo seed)."""
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.setattr(server_module, "_CV_FILE_OVERRIDE", "cv.yaml")
        assert server_module._local_demo_seed_cv_path().name == "cv.yaml"

    def test_non_demo_seed_path_unchanged(self, server_module, monkeypatch):
        """Outside demo mode the seed path is exactly the module-level CV_FILE
        (cv.yaml when present) — no behavior change for real tenants."""
        monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)
        assert server_module._local_demo_seed_cv_path() == server_module.CV_FILE

    def test_internal_cv_startup_path_is_repo_relative(
        self,
        server_module,
        monkeypatch,
    ):
        internal = server_module.REPO / "content" / "cv.example.yaml"
        monkeypatch.setattr(server_module, "CV_FILE", internal)
        assert server_module._cv_file_display_path() == Path(
            "content/cv.example.yaml"
        )

    def test_absolute_cv_startup_path_is_displayed_without_crashing(
        self,
        server_module,
        monkeypatch,
        tmp_path,
    ):
        external = tmp_path / "cv.yaml"
        monkeypatch.setattr(server_module, "CV_FILE", external)
        assert server_module._cv_file_display_path() == external


# ===========================================================================
#  3. Editor-chrome i18n — keys exist (EN+DE) and are wired.
# ===========================================================================
def _i18n_strings():
    import sys

    if str(REPO / "tools" / "editor") not in sys.path:
        sys.path.insert(0, str(REPO / "tools" / "editor"))
    import i18n

    return i18n.STRINGS


class TestEditorChromeI18n:
    NEW_KEYS = [
        "app.save_changes",
        "app.build_tooltip",
        "app.appearance_tooltip",
        "app.doc_actions_tooltip",
        "app.rerender_preview",
        "app.toggle_sidebar",
        "sidebar.workspace",
        "sidebar.library_aria",
        "sidebar.outline_aria",
        "workflow.applications_tooltip",
        "form.move_up",
        "form.move_down",
        "form.remove",
        "form.section_metrics",
        "form.section_ats_aria",
        "form.photo_drop_aria",
        "form.custom_href_placeholder",
    ]

    def test_new_keys_exist_with_en_and_de(self):
        strings = _i18n_strings()
        for key in self.NEW_KEYS:
            entry = strings.get(key)
            assert entry, f"missing i18n key {key!r}"
            assert entry.get("en"), f"{key!r} missing EN"
            assert entry.get("de"), f"{key!r} missing DE"

    def test_full_table_parity_preserved(self):
        """The table had perfect EN/DE parity at 792 keys — keep it."""
        strings = _i18n_strings()
        en_only = [k for k, v in strings.items() if not v.get("de")]
        de_only = [k for k, v in strings.items() if not v.get("en")]
        assert en_only == []
        assert de_only == []

    def test_api_i18n_resolves_new_keys_in_german(self, client):
        res = client.get("/api/i18n?lang=de")
        assert res.status_code == 200
        strings = res.get_json()["strings"]
        assert strings["app.save_changes"] == "Änderungen speichern"
        assert strings["app.toggle_sidebar"] == "Seitenleiste umschalten"
        assert strings["app.rerender_preview"] == "Vorschau neu rendern"
        assert strings["sidebar.workspace"] == "Arbeitsbereich"
        assert strings["workflow.applications"] == "Bewerbungen"
        assert strings["app.build"] == "PDF erstellen"
        assert strings["mobile.document_actions"] == "Dokumentaktionen"
        assert strings["mobile.cv_appearance"] == "Lebenslauf-Design"
        assert strings["templates.title"] == "Vorlagen"
        assert strings["form.move_up"] == "Nach oben verschieben"
        assert strings["form.remove"] == "Entfernen"

    def test_index_html_chrome_wiring(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        # Landmarks.
        assert 'id="sidebar" aria-label="Workspace" data-i18n-attr="aria-label:sidebar.workspace"' in html
        assert 'data-i18n-attr="aria-label:sidebar.library_aria"' in html
        assert 'data-i18n-attr="aria-label:sidebar.outline_aria"' in html
        # Topbar chrome: every listed aria-label/title/data-tooltip is wired.
        for wiring in (
            "aria-label:app.toggle_sidebar,title:app.toggle_sidebar,data-tooltip:app.toggle_sidebar",
            "aria-label:app.save_changes,title:app.save_changes,data-tooltip:app.save_changes",
            "aria-label:app.build,title:app.build,data-tooltip:app.build_tooltip",
            "aria-label:mobile.cv_appearance,title:mobile.cv_appearance,data-tooltip:app.appearance_tooltip",
            "aria-label:mobile.document_actions,title:mobile.document_actions,data-tooltip:app.doc_actions_tooltip",
            "aria-label:workflow.applications,data-tooltip:workflow.applications_tooltip",
            "aria-label:app.rerender_preview,title:app.rerender_preview,data-tooltip:app.rerender_preview",
        ):
            assert f'data-i18n-attr="{wiring}"' in html, wiring
        # The visible Applications label localises too.
        assert 'class="workflow-btn-label" data-i18n="workflow.applications"' in html

    def test_every_index_html_i18n_reference_resolves(self):
        """No dangling data-i18n / data-i18n-attr key in the editor page."""
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        strings = _i18n_strings()
        keys = set(re.findall(r'data-i18n="([^"]+)"', html))
        keys.update(re.findall(r'data-i18n-html="([^"]+)"', html))
        for attr in re.findall(r'data-i18n-attr="([^"]+)"', html):
            for pair in attr.split(","):
                bits = pair.split(":")
                if len(bits) == 2:
                    keys.add(bits[1].strip())
        missing = sorted(k for k in keys if k not in strings)
        assert missing == []

    def test_form_js_controls_route_through_i18n(self):
        form = (STATIC / "form.js").read_text(encoding="utf-8")
        strings = _i18n_strings()
        # The helper mirrors app.js's tr() and every listed control uses it.
        assert "function tr(key, fallback)" in form
        for key in (
            "form.move_up",
            "form.move_down",
            "form.remove",
            "form.section_metrics",
            "form.section_ats_aria",
            "form.photo_drop_aria",
            "form.custom_href_placeholder",
        ):
            assert key in strings, f"{key!r} missing from the string table"
            assert f'"{key}"' in form, f"form.js does not reference {key!r}"
        # Created controls carry data-i18n-attr so OnePagerI18n.apply(document)
        # re-localises them live on a language switch.
        assert "`aria-label:${opts.i18n},title:${opts.i18n}`" in form
        assert '"data-i18n-attr": "aria-label:form.section_metrics"' in form
        assert '"data-i18n-attr": "aria-label:form.section_ats_aria"' in form
        assert '"data-i18n-attr": "aria-label:form.photo_drop_aria"' in form
        assert '`placeholder:${opts.placeholderKey}`' in form


# ===========================================================================
#  4. VERIFY-ONLY — tailored copy "Open in editor" resolves the TAILORED row.
# ===========================================================================
class TestTailoredCopyOpenInEditor:
    def test_get_cv_by_tailored_id_returns_tailored_yaml(self, demo_client, server_module):
        """Round-2 regression check: after a demo-mode tailor apply, the new
        id must resolve to the TAILORED row (not the source CV)."""
        pytest.importorskip("weasyprint")
        import local_demo_store

        client, headers = demo_client
        res = client.post(
            f"/api/cv/{server_module.LOCAL_DEMO_CV_ID}/tailor",
            json={"jd_text": JD_TEXT},
            headers=headers,
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        tailored_id = res.get_json()["tailored_cv_id"]
        assert tailored_id != server_module.LOCAL_DEMO_CV_ID

        got = client.get(f"/api/cv?id={tailored_id}", headers=headers)
        assert got.status_code == 200, got.get_data(as_text=True)
        stored = [
            r for r in local_demo_store.db().table("documents").rows if r.get("id") == tailored_id
        ]
        assert len(stored) == 1
        assert got.get_data(as_text=True) == stored[0]["yaml_blob"]

    def test_get_cv_by_random_id_404s(self, demo_client):
        """An explicit id that resolves to nothing is a 404, never the
        bundled demo CV (the round-2 bug shape)."""
        client, headers = demo_client
        res = client.get(f"/api/cv?id={uuid.uuid4()}", headers=headers)
        assert res.status_code == 404


# ===========================================================================
#  5. X-OnePager-Demo-Write — consistent ephemeral-write signal.
# ===========================================================================
class TestDemoWriteHeader:
    def test_jobs_mutation_carries_header_in_demo(self, demo_client):
        client, headers = demo_client
        res = client.post(
            "/api/jobs",
            json={"title": "Data Engineer", "company": "Acme", "jd_text": JD_TEXT},
            headers=headers,
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        assert res.headers.get("X-OnePager-Demo-Write") == "server-process-memory"

    def test_multi_cv_create_carries_header_in_demo(self, demo_client):
        client, headers = demo_client
        res = client.post("/api/cvs", json={"name": "Second CV"}, headers=headers)
        assert res.status_code == 201, res.get_data(as_text=True)
        assert res.headers.get("X-OnePager-Demo-Write") == "server-process-memory"

    def test_cv_save_fallback_still_carries_header(self, demo_client):
        client, headers = demo_client
        src = client.get("/api/cv", headers=headers).get_data(as_text=True)
        res = client.post("/api/cv", json={"content": src}, headers=headers)
        assert res.status_code == 200, res.get_data(as_text=True)
        assert res.headers.get("X-OnePager-Demo-Write") == "server-process-memory"

    def test_reads_and_failed_mutations_carry_no_header(self, demo_client):
        client, headers = demo_client
        got = client.get("/api/cv", headers=headers)
        assert got.status_code == 200
        assert "X-OnePager-Demo-Write" not in got.headers
        bad = client.post("/api/jobs", json={}, headers=headers)
        assert bad.status_code >= 400
        assert "X-OnePager-Demo-Write" not in bad.headers

    def test_demo_photo_and_remote_theme_writes_are_rejected(
        self, demo_client
    ):
        client, headers = demo_client
        photo = client.post(
            "/api/asset/photo",
            data={"file": (io.BytesIO(b"not-used"), "photo.png")},
            content_type="multipart/form-data",
            headers=headers,
        )
        theme = client.post(
            "/api/themes/import",
            json={"url": "https://example.com/theme.json"},
            headers=headers,
        )
        assert photo.status_code == 409
        assert photo.get_json()["code"] == "demo_photo_disabled"
        assert theme.status_code == 404
        assert theme.get_json()["code"] == "theme_import_disabled"
        assert "X-OnePager-Demo-Write" not in photo.headers
        assert "X-OnePager-Demo-Write" not in theme.headers

    def test_real_tenant_mutations_carry_no_header(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Non-demo writes (real Supabase path) must NOT claim ephemeral
        storage — the signal stays demo-owner-only."""
        monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)
        res = client.post(
            "/api/jobs",
            json={"title": "Data Engineer", "company": "Acme", "jd_text": JD_TEXT},
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        assert "X-OnePager-Demo-Write" not in res.headers
