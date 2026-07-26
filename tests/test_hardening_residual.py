"""Residual hardening regression tests (adversarial re-verification round 2).

A fresh adversarial pass over the ALREADY-FIXED hardening areas found three
HIGH bugs where the prior fix was incomplete. Each evasion below was confirmed
to bypass the prior guard and is now neutralized:

  BUG 1 — Stored XSS via percent-encoded / Unicode-format-char scheme bypass
          (``render.markdown_inline._normalize_url``). ``%6aavascript:`` and a
          soft-hyphen-split ``java<U+00AD>script:`` slipped past the ASCII
          scheme check and became a live ``<a href>``.

  BUG 2 — CV-tailoring anti-fabrication still bypassable
          (``cover_letter.guards``). (2a) a single lowercase ungrounded skill
          in a comma list ("…, kubernetes") was never flagged; (2b) a spelled
          number that is a PREFIX of a longer word ("ninety" → "nine") was
          mis-parsed to a trivial small int and discarded.

  BUG 3 — Unauthenticated path traversal / source disclosure via ``/asset/``
          (``tools.editor.server._safe_repo_path``). ``app/tools/editor/
          server.py`` resolved to ``/app/tools/editor/server.py`` (REPO=/app)
          and was served 200 unauthenticated.

The engine tests (BUG 1, BUG 2 unit) are pure/offline. The end-to-end BUG 2
tests drive the real ``tailor_cv`` pipeline with a MOCK LLM and the in-memory
``fake_supabase`` fixture; they need WeasyPrint for the 1-page gate and skip if
it is unimportable. BUG 3 drives the auth-exempt Flask routes via the test
client.
"""
from __future__ import annotations

import json
import sys
import uuid as _uuid
from pathlib import Path

import pytest
import yaml as _yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

from render.markdown_inline import inline  # noqa: E402

_ANCHOR = "<a "


# ===========================================================================
#  BUG 1 — percent-encoding / Unicode-format-char scheme bypass.
# ===========================================================================
class TestMarkdownPercentAndFormatCharBypass:
    """The prior fix decoded HTML entities + stripped ASCII control chars but
    NOT percent-encoding or Unicode format chars, so the scheme check ran on a
    still-encoded string and missed a hidden ``javascript:``."""

    # Each of these MUST be neutralized: no live anchor in the rendered HTML
    # (the label survives as inert escaped text). The exact evasions from the
    # report plus close variants (mixed case, percent-encoded letters,
    # double-encoded, zero-width-injected).
    _EVASIONS = [
        "%6aavascript:alert(1)",                 # %6a -> j
        "%6A%61vascript:alert(1)",               # all letters percent-encoded
        "%256aavascript:alert(1)",               # double-encoded %256a -> %6a -> j
        "%2561vascript:alert(1)",                # double-encoded, no leading 'a'
        "java­script:alert(1)",             # soft hyphen mid-scheme
        "ja​vascript:alert(1)",             # zero-width space
        "java‍script:alert(1)",             # zero-width joiner
        "﻿javascript:alert(1)",             # BOM prefix
        "java%c2%adscript:alert(1)",             # percent-encoded soft hyphen
        "JaVa%53cript:alert(1)",                 # mixed case + %53 -> S
        "data%3atext/html,<x>",                  # percent-encoded scheme colon
        "vbscript%3amsgbox(1)",                  # vbscript via percent colon
    ]

    @pytest.mark.parametrize("payload", _EVASIONS)
    def test_evasion_produces_no_live_anchor(self, payload: str) -> None:
        out = inline(f"[click]({payload})")
        assert _ANCHOR not in out, f"live anchor leaked for {payload!r}: {out!r}"
        # The dangerous scheme keyword must never appear inside an href="...".
        assert 'href="' not in out

    @pytest.mark.parametrize("payload", _EVASIONS)
    def test_decoded_scheme_never_reaches_an_href(self, payload: str) -> None:
        """Even the decoded form (what the browser sees) must not be live."""
        from render.markdown_inline import _is_safe_url

        assert _is_safe_url(payload) is False

    # Legit URLs must remain LIVE links with their href intact.
    _LEGIT = [
        ("[x](https://example.com)", "https://example.com"),
        ("[x](http://a.b/c?d=1&e=2)", "http://a.b/c?d=1&amp;e=2"),
        ("[x](mailto:tim@example.com)", "mailto:tim@example.com"),
        ("[docs](/guide/page)", "/guide/page"),
        ("[anchor](#section)", "#section"),
        ("[proto](//cdn.example.com/x.js)", "//cdn.example.com/x.js"),
        # A legitimate %20 (space) in a path must be PRESERVED in the href and
        # the link must stay live — percent-decoding is for scheme inspection
        # only, never for the emitted href.
        ("[file](/my%20file.pdf)", "/my%20file.pdf"),
    ]

    @pytest.mark.parametrize("markdown,expected_href", _LEGIT)
    def test_legit_links_stay_live(self, markdown: str, expected_href: str) -> None:
        out = inline(markdown)
        assert _ANCHOR in out, f"legit link dropped: {markdown!r} -> {out!r}"
        assert f'href="{expected_href}"' in out, out


# ===========================================================================
#  BUG 2 — anti-fabrication: unit checks (the two exact evasions).
# ===========================================================================
class TestSpelledNumberPrefixUnit:
    """BUG 2b — a spelled number that PREFIXES a longer word must parse as the
    WHOLE word (with its unit re-checked), not as the shorter prefix."""

    @pytest.mark.parametrize(
        "phrase,expected",
        [
            ("ninety percent", "90%"),   # was wrongly "9" (prefix "nine")
            ("seventy percent", "70%"),  # was wrongly "7"
            ("eighty percent", "80%"),   # was wrongly "8"
            ("sixty percent", "60%"),    # was wrongly "6"
            # The ones that already worked must keep working.
            ("fifty percent", "50%"),
            ("forty percent", "40%"),
            ("thirty percent", "30%"),
            ("twenty percent", "20%"),
            ("one hundred percent", "100%"),
            ("nineteen percent", "19%"),  # must NOT collapse to "nine"
            ("ninety", "90"),
        ],
    )
    def test_spelled_number_parses_whole_word_with_unit(self, phrase, expected):
        from cover_letter.guards import _spelled_numbers

        results = _spelled_numbers(phrase)
        norms = {norm for norm, _raw in results}
        assert expected in norms, f"{phrase!r} -> {results!r}"


class TestCommaListSkillGroundingUnit:
    """BUG 2a — a single ungrounded skill token inside a comma list must be
    flagged; grounded lists and prose must not be."""

    _CV = {
        "experience": [
            {
                "role": "Data Engineer",
                "company": "Meta",
                "stack": "Python, Spark, Airflow",
                "bullets": ["Cut pipeline latency by 40% across the ETL stack."],
            }
        ],
        "skills": [{"label": "Data", "items": "Python, SQL, PyTorch, Spark"}],
    }

    def test_lone_lowercase_skill_in_stack_is_flagged(self):
        from cover_letter.guards import cross_reference_facts

        blob = "Python, Spark, Airflow, kubernetes\nMeta\nData Engineer"
        offenders = cross_reference_facts(blob, self._CV)
        assert any(o.lower() == "kubernetes" for o in offenders), offenders

    def test_capitalized_skill_still_flagged(self):
        from cover_letter.guards import cross_reference_facts

        blob = "Python, Spark, Airflow, Kubernetes\nMeta\nData Engineer"
        offenders = cross_reference_facts(blob, self._CV)
        assert any(o == "Kubernetes" for o in offenders), offenders

    def test_fully_grounded_skill_list_passes(self):
        from cover_letter.guards import cross_reference_facts

        assert cross_reference_facts("Python, Spark, Airflow\nMeta", self._CV) == []
        assert (
            cross_reference_facts("Python, SQL, PyTorch, Spark\nData", self._CV) == []
        )

    def test_prose_with_commas_not_treated_as_skill_list(self):
        from cover_letter.guards import cross_reference_facts

        # An honest sentence using commas must NOT be split into skill tokens.
        blob = "I built data models, pipelines, and dashboards in Airflow.\nMeta"
        assert cross_reference_facts(blob, self._CV) == []


# ===========================================================================
#  BUG 2 — END-TO-END via tailor_cv() with a MOCK LLM + fake_supabase.
#  Mirrors tests/test_stage6_tailor.py: the LLM returns structured
#  suggestions, they are applied deterministically, and the anti-fabrication
#  guard runs against the SOURCE CV. A grounding trip → 422, nothing persists.
# ===========================================================================
_FAKE_OWNER = "00000000-0000-0000-0000-000000000001"

_E2E_CV = {
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
        }
    ],
    "skills": [{"label": "Data", "items": "Python, SQL, PyTorch, Spark"}],
}

_E2E_JD = (
    "Title: Senior Data Engineer\n"
    "Company: Acme GmbH\n"
    "You must have Spark and Airflow. Kubernetes is nice to have.\n"
)


def _structured_response(suggestions):
    return json.dumps(
        {
            "fit": {"score": 80, "verdict": "ok", "target_role": "Senior Data Engineer"},
            "keywords": {"present": [], "missing": [], "inferable": []},
            "suggestions": suggestions,
            "summary": {"recommended": False, "text": "", "rationale": ""},
            "warnings": [],
        }
    )


def _mock_llm(monkeypatch, raw_text):
    import tailor_service as ts

    class _FakeClient:
        def __init__(self, api_key):
            pass

        def complete(self, *, system, user, model=None, max_tokens=1024, cache_system=True):
            return raw_text

    monkeypatch.setattr(ts, "make_client", lambda provider, api_key: _FakeClient(api_key))


def _seed_cv(store, cv_model):
    blob = _yaml.safe_dump(cv_model, sort_keys=False, allow_unicode=True)
    rows = store.tables.setdefault("documents", [])
    doc_id = str(_uuid.uuid4())
    rows.append(
        {
            "id": doc_id,
            "owner_id": store.owner_id,
            "type": "cv",
            "name": "My CV",
            "yaml_blob": blob,
            "accent": "#1f6f43",
            "font": "sans",
            "deleted_at": None,
            "updated_at": 1,
        }
    )
    return doc_id, blob


def _seed_profile(store, owner_id, *, credits):
    store.tables.setdefault("profiles", []).append(
        {"id": owner_id, "managed_credits_remaining": credits}
    )


def _cv_docs(store):
    return [d for d in store.tables.get("documents", []) if d["type"] == "cv"]


class TestTailorAntiFabricationE2E:
    """End-to-end through tailor_cv(): the exact persisted-evasion repros."""

    def test_lowercase_single_skill_in_stack_blocks_422(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """BUG 2a repro: retag_stack appends a LONE lowercase 'kubernetes' the
        source CV never names → 422 unverified_claims, nothing persisted."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {
                "id": "retag",
                "op": "retag_stack",
                "target": "experience",
                "index": 0,
                "before": "Python, Spark, Airflow",
                "after": "Python, Spark, Airflow, kubernetes",
            }
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, original_blob = _seed_cv(fake_supabase, _E2E_CV)
        _seed_profile(fake_supabase, _FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": _E2E_JD})
        assert res.status_code == 422, res.get_data(as_text=True)
        body = res.get_json()
        assert body["error"] == "unverified_claims"
        assert any("kubernetes" in str(i).lower() for i in body["items"]), body

        docs = _cv_docs(fake_supabase)
        assert len(docs) == 1, "fabrication must NOT persist a tailored copy"
        assert docs[0]["yaml_blob"] == original_blob

    def test_capitalized_skill_still_blocks_422(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """The capitalized form must keep blocking (no regression)."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {
                "id": "retag",
                "op": "retag_stack",
                "target": "experience",
                "index": 0,
                "before": "Python, Spark, Airflow",
                "after": "Python, Spark, Airflow, Kubernetes",
            }
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, _blob = _seed_cv(fake_supabase, _E2E_CV)
        _seed_profile(fake_supabase, _FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": _E2E_JD})
        assert res.status_code == 422
        assert any("Kubernetes" in str(i) for i in res.get_json()["items"])
        assert len(_cv_docs(fake_supabase)) == 1

    def test_spelled_ninety_percent_metric_blocks_422(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """BUG 2b repro: a rewrite claims 'ninety percent' but the CV only
        evidences 40% → the spelled number now parses as 90% and is ungrounded
        → 422, nothing persisted."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {
                "id": "rewrite",
                "op": "rewrite_bullet",
                "target": "experience",
                "index": 0,
                "bullet_index": 0,
                "before": "Cut pipeline latency by 40% across the ETL stack.",
                "after": "Cut pipeline latency by ninety percent across the ETL stack.",
            }
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, original_blob = _seed_cv(fake_supabase, _E2E_CV)
        _seed_profile(fake_supabase, _FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": _E2E_JD})
        assert res.status_code == 422, res.get_data(as_text=True)
        body = res.get_json()
        assert body["error"] == "unverified_claims"
        # The offending item surfaces the fabricated spelled metric.
        assert any("ninety" in str(i).lower() for i in body["items"]), body

        docs = _cv_docs(fake_supabase)
        assert len(docs) == 1
        assert docs[0]["yaml_blob"] == original_blob

    def test_grounded_forty_percent_and_real_skill_pass_201(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """The legit case must STILL pass: a rewrite using 'forty percent'
        (== CV '40%') and a stack reorder of genuinely-grounded skills →
        201, a tailored copy is created, original untouched."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-XXXXXXXXXXXXXXXXXXXXXXXX")
        suggestions = [
            {
                "id": "rewrite",
                "op": "rewrite_bullet",
                "target": "experience",
                "index": 0,
                "bullet_index": 0,
                "before": "Cut pipeline latency by 40% across the ETL stack.",
                "after": "Cut ETL pipeline latency by forty percent with Spark.",
            },
            {
                "id": "retag",
                "op": "retag_stack",
                "target": "experience",
                "index": 0,
                # Reorder of grounded skills only — no new skill introduced.
                "before": "Python, Spark, Airflow",
                "after": "Airflow, Spark, Python",
            },
        ]
        _mock_llm(monkeypatch, _structured_response(suggestions))
        cv_id, original_blob = _seed_cv(fake_supabase, _E2E_CV)
        _seed_profile(fake_supabase, _FAKE_OWNER, credits=3)

        res = client.post(f"/api/cv/{cv_id}/tailor", json={"jd_text": _E2E_JD})
        assert res.status_code == 201, res.get_data(as_text=True)
        docs = _cv_docs(fake_supabase)
        assert len(docs) == 2, "a grounded tailoring must create the copy"
        source = next(d for d in docs if d["id"] == cv_id)
        assert source["yaml_blob"] == original_blob, "original must be untouched"


# ===========================================================================
#  BUG 3 — unauthenticated path traversal / source disclosure via /asset/.
#  The auth-exempt /asset/ and /repo/ routes resolved a request path by
#  prepending "/", so "app/tools/editor/server.py" (REPO=/app) became
#  "/app/tools/editor/server.py" and was served 200. The fix resolves RELATIVE
#  to REPO and scopes serving to the design/ asset dirs only. These routes need
#  NO auth (they're exempt), so the test client hits them directly.
# ===========================================================================
class TestAssetRouteSourceDisclosure:
    # Real REPO basename so the CSS-style "<base>/design/..." prefix is exercised
    # exactly as the browser sends it.
    @staticmethod
    def _base(server_module) -> str:
        return server_module.REPO.name  # "app" in the container, repo dir locally

    # Every one of these is application source / secrets / config that must
    # NEVER be served — through either route, with or without the REPO-basename
    # prefix, and via traversal.
    def test_server_source_not_served_via_asset(self, server_module, client):
        base = self._base(server_module)
        for path in (
            f"/asset/{base}/tools/editor/server.py",
            f"/asset/{base}/tools/editor/auth.py",
            f"/asset/{base}/.env",
            f"/asset/{base}/content/cv.yaml",
            f"/asset/{base}/themes/default.json",
            "/asset/tools/editor/server.py",  # no basename prefix
        ):
            res = client.get(path)
            assert res.status_code in (403, 404), f"{path} leaked: {res.status_code}"
            # Never the file's bytes.
            assert b"def _safe_repo_path" not in res.data
            assert b"SUPABASE" not in res.data

    def test_server_source_not_served_via_repo(self, server_module, client):
        for path in (
            "/repo/tools/editor/server.py",
            "/repo/.env",
            "/repo/engine/build.py",
        ):
            res = client.get(path)
            assert res.status_code in (403, 404), f"{path} leaked: {res.status_code}"
            assert b"def _safe_repo_path" not in res.data

    @pytest.mark.parametrize(
        "path",
        [
            "/asset/../etc/passwd",
            "/asset/app/../etc/passwd",
            "/asset/app/design/../tools/editor/server.py",
            "/repo/../etc/passwd",
            "/repo/design/../../etc/passwd",
        ],
    )
    def test_traversal_blocked(self, server_module, client, path):
        res = client.get(path)
        assert res.status_code in (403, 404), f"{path} leaked: {res.status_code}"
        assert b"root:" not in res.data  # no /etc/passwd content

    def test_absolute_path_embedding_blocked(self, server_module, client):
        """The exact prior root cause: an absolute /etc path or a REPO-absolute
        path embedded after /asset/ must not resolve.

        A leading double slash makes Flask emit a 308 slash-merge redirect; we
        follow it so the assertion covers what the attacker's browser actually
        lands on (which must be blocked, not the file bytes)."""
        for path in ("/asset//etc/passwd", "/repo//etc/passwd"):
            res = client.get(path, follow_redirects=True)
            assert res.status_code in (403, 404), f"{path} leaked: {res.status_code}"
            assert b"root:" not in res.data  # never /etc/passwd content

    def test_legit_font_still_served(self, server_module, client):
        """A real design/ font must STILL be served 200 (editor/renderer dep)."""
        base = self._base(server_module)
        # Pick a font that exists in the repo's design/fonts dir.
        fonts_dir = server_module.REPO / "design" / "fonts"
        ttfs = sorted(fonts_dir.glob("*.ttf"))
        if not ttfs:
            pytest.skip("no design/fonts/*.ttf present to assert against")
        name = ttfs[0].name
        # Browser-style path (with REPO-basename prefix) — exactly what the
        # preview CSS emits.
        res = client.get(f"/asset/{base}/design/fonts/{name}")
        assert res.status_code == 200, res.get_data(as_text=True)
        assert len(res.data) > 0

    def test_legit_icon_still_served(self, server_module, client):
        base = self._base(server_module)
        icons_dir = server_module.REPO / "design" / "icons"
        svgs = sorted(icons_dir.glob("*.svg"))
        if not svgs:
            pytest.skip("no design/icons/*.svg present to assert against")
        res = client.get(f"/asset/{base}/design/icons/{svgs[0].name}")
        assert res.status_code == 200
        assert len(res.data) > 0
