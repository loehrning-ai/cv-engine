"""Polish / hardening tests (e2e hardening follow-up batch).

Three independent fixes, one test class each:

  FIX 1 — ``/api/preview`` brand fonts 404 after the ``/asset`` scoping fix.
          ``render_preview`` used to embed the ABSOLUTE repo path in the
          rewritten ``/asset/...`` URL, which 404'd locally once the asset
          route was scoped to ``design/``. It now emits REPO-RELATIVE
          ``/asset/design/...`` URLs that resolve in both local dev and the
          container, while the source-disclosure scope still holds.

  FIX 2 — markdown fullwidth-scheme gap. ``_normalize_url`` now NFKC-folds
          before the scheme regex, so a fullwidth ``ｊａｖａｓｃｒｉｐｔ:`` (or a
          mixed ``javaｓcript:``) is recognised as a disallowed scheme and
          neutralised. (Defense-in-depth — no current browser executes it.)

  FIX 3 — anti-fabrication: a lone lowercase ungrounded token that directly
          follows an employer/org preposition (``at``/``for``/``with``/``@``)
          is now flagged (``at datadog``, ``for terraform``), without
          flagging ordinary lowercase tech terms elsewhere or any grounded
          generation. The grounding remains best-effort defense-in-depth.

All offline / deterministic — no network, no live LLM.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))


# ===========================================================================
# FIX 1 — preview fonts load post-scoping (repo-relative /asset URLs).
# ===========================================================================
class TestPreviewFontUrlsResolve:
    """The fonts referenced by the preview HTML must be servable 200, AND the
    source-disclosure scope (only design/ servable) must still hold — in both
    the local-dev (REPO=abs path) and the container (REPO=/app) shapes."""

    @staticmethod
    def _font_urls(html: str) -> list[str]:
        # Strip the trailing quote/paren the url() wrapper leaves on the match.
        return sorted(
            {u.rstrip("'\")") for u in re.findall(r"/asset/[^\"')\s]+", html)}
        )

    def test_render_preview_emits_repo_relative_font_urls(self):
        """No absolute REPO prefix and no REPO-basename segment in the URL —
        the path is relative to REPO so it is identical local and in-container."""
        import render_preview as rp

        html = rp.render_cv_html(
            REPO / "content" / "cv.example.yaml",
            mode="continuous",
        )
        urls = self._font_urls(html)
        font_urls = [u for u in urls if "/asset/design/fonts/" in u]
        assert font_urls, f"no repo-relative font URLs emitted; got {urls!r}"
        abs_prefix = str(rp.REPO).lstrip("/")
        for u in urls:
            # Must NOT carry the absolute repo path (the old, broken form)...
            assert abs_prefix not in u, f"absolute repo path leaked into {u!r}"
            # ...and must NOT carry a leading "<repo-basename>/design" segment.
            assert not u.startswith(
                f"/asset/{rp.REPO.name}/"
            ), f"REPO-basename prefix leaked into {u!r}"

    def test_preview_font_url_resolves_200_local(self, server_module, client):
        """The exact URL render_preview emits must be served 200 by /asset
        when REPO is the real repo dir (local-dev shape)."""
        import render_preview as rp

        html = rp.render_cv_html(
            REPO / "content" / "cv.example.yaml",
            mode="continuous",
        )
        font_urls = [
            u for u in self._font_urls(html) if "/asset/design/fonts/" in u
        ]
        assert font_urls, "no font URL to resolve"
        for path in font_urls[:3]:
            res = client.get(path)
            assert res.status_code == 200, f"{path} → {res.status_code}"
            assert len(res.data) > 0

    def test_repo_relative_font_resolves_when_repo_is_app(
        self, server_module, client, monkeypatch, tmp_path
    ):
        """Container shape: REPO=/app-like temp tree. A repo-relative
        ``design/fonts/X.ttf`` asset URL must resolve under REPO/design."""
        # Build an /app-style tree with a real font file under design/fonts.
        app_repo = tmp_path / "app"
        fonts = app_repo / "design" / "fonts"
        fonts.mkdir(parents=True)
        (fonts / "Inter-Regular.ttf").write_bytes(b"FAKE-TTF-BYTES")
        monkeypatch.setattr(server_module, "REPO", app_repo)

        res = client.get("/asset/design/fonts/Inter-Regular.ttf")
        assert res.status_code == 200, res.get_data(as_text=True)
        assert res.data == b"FAKE-TTF-BYTES"

    def test_real_design_font_and_icon_served(self, server_module, client):
        """A real design font AND a real brand icon are served 200 via the
        repo-relative URL form (no basename prefix)."""
        fonts = sorted((server_module.REPO / "design" / "fonts").glob("*.ttf"))
        icons = sorted((server_module.REPO / "design" / "icons").glob("*.svg"))
        if not fonts or not icons:
            pytest.skip("design assets missing in this checkout")
        for asset in (fonts[0], icons[0]):
            rel = asset.relative_to(server_module.REPO).as_posix()
            res = client.get(f"/asset/{rel}")
            assert res.status_code == 200, f"/asset/{rel} → {res.status_code}"
            assert len(res.data) > 0

    def test_source_disclosure_still_blocked_after_fix(
        self, server_module, client
    ):
        """The scope guard must still 403/404 application source / secrets /
        config / themes — through BOTH the repo-relative form AND the legacy
        abs-path and basename-prefixed forms a cached client might still send."""
        base = server_module.REPO.name
        abs_prefix = str(server_module.REPO).lstrip("/")
        blocked = [
            # repo-relative (the new form) — source must NOT be reachable.
            "/asset/tools/editor/server.py",
            "/asset/.env",
            "/asset/content/cv.yaml",
            "/asset/themes/default.json",
            "/asset/engine/build.py",
            # legacy basename-prefixed form.
            f"/asset/{base}/tools/editor/server.py",
            f"/asset/{base}/.env",
            # legacy absolute-path form (the one render_preview used to emit).
            f"/asset/{abs_prefix}/tools/editor/server.py",
            f"/asset/{abs_prefix}/.env",
            # via the /repo route too.
            "/repo/tools/editor/server.py",
            "/repo/.env",
        ]
        for path in blocked:
            res = client.get(path)
            assert res.status_code in (403, 404), f"{path} leaked {res.status_code}"
            assert b"def _safe_repo_path" not in res.data
            assert b"SUPABASE" not in res.data
            assert b"def api_preview" not in res.data

    def test_design_traversal_to_source_blocked(self, server_module, client):
        """A ``design/../`` climb out of the asset root must not reach source."""
        for path in (
            "/asset/design/../tools/editor/server.py",
            "/asset/design/fonts/../../tools/editor/auth.py",
            "/repo/design/../../etc/passwd",
        ):
            res = client.get(path)
            assert res.status_code in (403, 404), f"{path} leaked {res.status_code}"
            assert b"def _safe_repo_path" not in res.data
            assert b"root:" not in res.data


# ===========================================================================
# FIX 2 — markdown fullwidth-scheme gap (NFKC before the scheme check).
# ===========================================================================
class TestMarkdownFullwidthScheme:
    """A fullwidth / compatibility-form scheme must be NFKC-folded and then
    rejected as a disallowed scheme — emitted as inert text, never a live
    ``<a href>``. Legit links and existing obfuscation handling are unchanged."""

    @staticmethod
    def _inline(raw: str) -> str:
        from render.markdown_inline import inline

        return inline(f"[x]({raw})")

    @pytest.mark.parametrize(
        "payload",
        [
            "ｊａｖａｓｃｒｉｐｔ:alert(1)",        # fullwidth letters, ASCII colon
            "ｊａｖａｓｃｒｉｐｔ：alert(1)",        # fullwidth letters + fullwidth colon
            "javaｓcript:alert(1)",            # one fullwidth letter mid-scheme
            "ＪＡＶＡＳＣＲＩＰＴ:alert(1)",        # fullwidth uppercase
            "ｄａｔａ:text/html,<script>x</script>",  # fullwidth data:
            "ｖｂｓｃｒｉｐｔ:msgbox(1)",          # fullwidth vbscript:
        ],
    )
    def test_fullwidth_scheme_neutralized(self, payload):
        out = self._inline(payload)
        assert "<a " not in out, f"live anchor for fullwidth scheme: {out!r}"
        assert "href" not in out

    def test_normalize_url_folds_fullwidth_to_ascii(self):
        from render.markdown_inline import _is_safe_url, _normalize_url

        norm = _normalize_url("ｊａｖａｓｃｒｉｐｔ：alert(1)")
        assert norm.startswith("javascript:"), norm
        assert _is_safe_url("ｊａｖａｓｃｒｉｐｔ：alert(1)") is False

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com",
            "http://x.io/path?q=1",
            "mailto:a@b.com",
            "/relative/path",
            "#anchor",
            "//cdn.example.com/lib.js",  # protocol-relative — safe
            "page.html",
        ],
    )
    def test_legit_links_unaffected(self, url):
        out = self._inline(url)
        assert "<a href=" in out, f"legit link dropped: {url!r} → {out!r}"

    @pytest.mark.parametrize(
        "payload",
        [
            "javascript:alert(1)",          # literal
            "java%73cript:alert(1)",        # percent-encoded
            "jav&#x61;script:alert(1)",     # numeric-entity-encoded
            "data:text/html,<b>x</b>",      # data:
            "vbscript:msgbox(1)",           # vbscript:
        ],
    )
    def test_existing_obfuscation_still_blocked(self, payload):
        """The NFKC addition must not regress the prior scheme defenses."""
        out = self._inline(payload)
        assert "<a " not in out, f"regression — live anchor: {out!r}"


# ===========================================================================
# FIX 3 — anti-fabrication: lone lowercase org after at/for/with/@.
# ===========================================================================
# A grounded source CV used as the SOLE fact corpus (mirrors stage5/6 fixtures).
_GROUNDED_CV = {
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
            "bullets": [
                "Cut pipeline latency by 40% across the ETL stack.",
                "Built data models for analytics in Airflow.",
            ],
            "stack": "Python, Spark, Airflow",
        },
        {
            "role": "Data Scientist",
            "company": "Red Bull",
            "location": "Salzburg",
            "start": "2021",
            "end": "2023",
            "bullets": ["Built ML models for anomaly detection in Python."],
        },
    ],
    "skills": [{"label": "Data", "items": "Python, SQL, PyTorch, Spark"}],
}

_JD_TEXT = (
    "Title: Senior Data Engineer\nCompany: Acme GmbH\n"
    "You must have experience with Spark and Airflow. 5+ years required.\n"
    "Responsibilities: build ETL pipelines and data models."
)


class TestLoneLowercaseOrgGrounding:
    """A lone ungrounded token directly after ``at``/``for``/``with``/``@`` is
    flagged as a fabricated employer even when lowercase — the targeted residual
    closed by FIX 3. Grounded mentions and common prose are NOT flagged."""

    @pytest.mark.parametrize(
        "text,bad",
        [
            ("At datadog I built the ingestion pipeline.", "datadog"),
            ("I shipped infra for terraform last quarter.", "terraform"),
            ("I owned the warehouse with snowflake daily.", "snowflake"),
            ("Find me at databricks where I led ETL.", "databricks"),
        ],
    )
    def test_lowercase_org_after_preposition_flagged(self, text, bad):
        from cover_letter.guards import cross_reference_facts

        offenders = cross_reference_facts(text, _GROUNDED_CV, jd_context=_JD_TEXT)
        joined = " ".join(offenders).lower()
        assert bad in joined, f"{bad!r} not flagged in {offenders!r}"

    @pytest.mark.parametrize(
        "text",
        [
            # Grounded employers/skills — must NOT be flagged.
            "At Meta I cut pipeline latency by 40% with Spark and Airflow.",
            "Earlier, at Red Bull, I built ML models with Python.",
            "I delivered ETL work with Airflow for analytics.",
            # Common prepositional objects — must NOT be flagged.
            "I shipped at scale for clients with confidence.",
            "Available for example with care and at speed.",
            "I built this for years with focus and at home.",
        ],
    )
    def test_grounded_and_common_prose_pass(self, text):
        # NOTE: the capitalization path may flag a sentence-initial capitalized
        # ungrounded word (pre-existing behavior, out of FIX 3 scope); we assert
        # specifically that the *lowercase-org* path adds no offender here.
        from cover_letter.guards import (
            _lone_org_after_preposition,
            _normalized_corpus_tokens,
            _cv_fact_corpus,
        )

        corpus = _cv_fact_corpus(_GROUNDED_CV) + "\n" + _cv_fact_corpus(
            {"_": _JD_TEXT}
        )
        words = _normalized_corpus_tokens(corpus)
        assert _lone_org_after_preposition(text, words) == [], text

    def test_clean_grounded_letter_still_passes_run_guards(self):
        """The canonical CLEAN cover letter (grounded against CV+JD) must pass
        the full guard chain unchanged — no new false positive from FIX 3."""
        from cover_letter.guards import run_guards

        clean = (
            "At Meta I cut pipeline latency by 40% across the ETL stack using "
            "Spark and Airflow, the exact stack this Senior Data Engineer role "
            "calls for.\n\n"
            "Earlier, at Red Bull, I built ML models for anomaly detection in "
            "Python. I bring the SQL and PyTorch depth your data-modeling work "
            "needs.\n\n"
            "I would welcome the chance to bring this to Acme GmbH."
        )
        result = run_guards(clean, _GROUNDED_CV, jd_context=_JD_TEXT)
        assert result.word_count > 0  # did not raise GroundingError

    def test_lowercase_org_letter_blocks_run_guards(self):
        """A cover letter naming a lowercase fabricated employer after 'at' is
        blocked by the guard chain (GroundingError)."""
        from cover_letter.guards import GroundingError, run_guards

        letter = (
            "At datadog I cut latency by 40% across the ETL stack with Spark "
            "and Airflow.\n\nI would welcome the chance to bring this to you."
        )
        with pytest.raises(GroundingError) as exc:
            run_guards(letter, _GROUNDED_CV, jd_context=_JD_TEXT)
        assert any("datadog" in i.lower() for i in exc.value.items)


class TestLoneLowercaseOrgThroughTailorRoute:
    """End-to-end: a ``rewrite_bullet`` that injects a lowercase fabricated
    employer flows through ``tailor_cv`` and is blocked (422 unverified_claims),
    persisting nothing. A grounded rewrite still succeeds."""

    FAKE_OWNER = "00000000-0000-0000-0000-000000000001"

    @staticmethod
    def _structured(suggestions):
        import json

        return json.dumps(
            {
                "fit": {"score": 80, "verdict": "ok", "target_role": "X"},
                "keywords": {"present": [], "missing": [], "inferable": []},
                "suggestions": suggestions,
                "summary": {"recommended": False, "text": "", "rationale": ""},
                "warnings": [],
            }
        )

    @staticmethod
    def _mock_llm(monkeypatch, raw_text):
        import tailor_service as ts

        class _FakeClient:
            def __init__(self, api_key):
                pass

            def complete(self, *, system, user, model=None, max_tokens=1024, cache_system=True):
                return raw_text

        monkeypatch.setattr(ts, "make_client", lambda provider, api_key: _FakeClient(api_key))

    def _seed_cv(self, store):
        import uuid as _uuid

        import yaml as _yaml

        blob = _yaml.safe_dump(_GROUNDED_CV, sort_keys=False, allow_unicode=True)
        doc_id = str(_uuid.uuid4())
        store.tables.setdefault("documents", []).append(
            {
                "id": doc_id, "owner_id": store.owner_id, "type": "cv",
                "name": "My CV", "yaml_blob": blob, "accent": "#1f6f43",
                "font": "sans", "deleted_at": None, "updated_at": 1,
            }
        )
        return doc_id, blob

    @staticmethod
    def _cv_docs(store):
        return [d for d in store.tables.get("documents", []) if d["type"] == "cv"]

    def test_lowercase_employer_rewrite_blocks_and_persists_nothing(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        pytest.importorskip("weasyprint")
        monkeypatch.setenv(
            "ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX"
        )
        # Lowercase fabricated employer in the exact at-<org> slot.
        suggestions = [
            {"id": "fab", "op": "rewrite_bullet", "target": "experience",
             "index": 0, "bullet_index": 0,
             "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": "at datadog I cut pipeline latency by 40%."},
        ]
        self._mock_llm(monkeypatch, self._structured(suggestions))
        cv_id, original_blob = self._seed_cv(fake_supabase)
        fake_supabase.tables.setdefault("profiles", []).append(
            {"id": self.FAKE_OWNER, "managed_credits_remaining": 3}
        )

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": _JD_TEXT})
        assert res.status_code == 422, res.get_data(as_text=True)
        body = res.get_json()
        assert body["error"] == "unverified_claims"
        assert any("datadog" in i.lower() for i in body["items"])

        # Nothing persisted; original byte-identical.
        docs = self._cv_docs(fake_supabase)
        assert len(docs) == 1
        assert docs[0]["yaml_blob"] == original_blob

    def test_grounded_rewrite_still_succeeds(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """A grounded 'at Meta' rewrite must NOT be blocked by FIX 3 — it
        tailors and persists a new doc (the original stays byte-identical)."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv(
            "ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX"
        )
        suggestions = [
            {"id": "ok", "op": "rewrite_bullet", "target": "experience",
             "index": 0, "bullet_index": 0,
             "before": "Cut pipeline latency by 40% across the ETL stack.",
             "after": "At Meta I cut pipeline latency by 40% with Spark."},
        ]
        self._mock_llm(monkeypatch, self._structured(suggestions))
        cv_id, original_blob = self._seed_cv(fake_supabase)
        fake_supabase.tables.setdefault("profiles", []).append(
            {"id": self.FAKE_OWNER, "managed_credits_remaining": 3}
        )

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": _JD_TEXT})
        # 201 CREATED — a new tailored document was persisted (route convention).
        assert res.status_code == 201, res.get_data(as_text=True)
        # A new tailored doc was created; the original is untouched.
        docs = self._cv_docs(fake_supabase)
        assert len(docs) == 2
        source = next(d for d in docs if d["id"] == cv_id)
        assert source["yaml_blob"] == original_blob


# ===========================================================================
# FIX 4 — editor icon registry (plan 002 stage 1): the topbar/back/delete
#         glyphs that templates reference must be DEFINED in icons.js, else
#         ``i()`` returns "" and the span renders empty (no glyph). Also
#         the Tailor action uses the ``scissors`` tailoring glyph, and every
#         glyph stays theme-aware via ``currentColor``.
# ===========================================================================
class TestEditorIconRegistry:
    _JS = (REPO / "tools" / "editor" / "static" / "icons.js").read_text(
        encoding="utf-8"
    )

    @pytest.mark.parametrize(
        "name", ["mail", "arrow-left", "scissors", "trash-2", "palette"]
    )
    def test_icon_is_defined(self, name: str) -> None:
        """Each referenced glyph has a registry entry (non-empty inner SVG)."""
        m = re.search(
            rf'"{re.escape(name)}":\s*\n?\s*\'(.*?)\',', self._JS, re.DOTALL
        )
        assert m, f"icons.js is missing a definition for {name!r}"
        assert m.group(1).strip(), f"icons.js entry for {name!r} is empty"

    def test_icons_are_theme_aware(self) -> None:
        """The wrapper paints with currentColor so glyphs follow the theme."""
        assert 'stroke="currentColor"' in self._JS

    def test_referenced_glyphs_have_no_empty_renders(self) -> None:
        """Every ``data-icon="X"`` used in the editor templates resolves to a
        defined registry entry (or the special ``brand`` glyph)."""
        static = REPO / "tools" / "editor" / "static"
        referenced: set[str] = set()
        for html in static.glob("*.html"):
            referenced |= set(
                re.findall(r'data-icon="([a-z0-9-]+)"', html.read_text("utf-8"))
            )
        defined = set(re.findall(r'"([a-z0-9-]+)":\s', self._JS)) | {"brand"}
        missing = referenced - defined
        assert not missing, f"templates reference undefined icons: {sorted(missing)}"
