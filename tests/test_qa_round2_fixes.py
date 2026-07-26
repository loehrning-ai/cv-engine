"""Regression tests for the round-2 QA polish batch (P2/P3 findings).

Each class maps to one independently-verified finding and pins the fixed
behavior:

  1. jd.py: German modal/year cues ("Muss …", "5 Jahre …") mark a requirement
     line, so a German JD pasted at its requirements section doesn't guess the
     first requirement as the role title.
  2. ats/experience.py: a trailing capitalized "Experience" is part of the
     skill ("User Experience"), while lowercase " experience" and German
     "Erfahrung" remain cue words that get stripped.
  3. guards.py: the cliché-strip a/an repair never makes text worse —
     "a results-driven university graduate" keeps "a", "…honest…" gets "an".
  4. guards.py: word_count counts Ext-B ideographs and Hangul Jamo per
     codepoint, not as one ``\\w+`` run.
  5. cover_letter/renderer.py: accent accepts exactly 3/4/6/8 hex digits
     (#rgba was rejected, 5-digit was accepted).
  6. llm/client.py: the BYO Authorization header is stripped on cross-host
     redirects (key exfiltration via a malicious 302 endpoint).
  7. apply_service.py: a malformed/out-of-range port no longer crashes
     classify_apply_url, and rate-limit buckets don't grow unboundedly.
  8. form.js: Move handlers bail while the sibling card is mid-removal
     (model/DOM desync), and the post-remove focus target can't be a
     disabled button.
  9. app.js: loadCv resets yamlBufferInvalid alongside conflictPending; the
     tour focuses the step card (tabindex=-1) instead of the Next button.
 10. llm_gateway.py: demo-mode key save fails loudly (LLMUnavailable 400)
     instead of fake {ok:true,last4} that contradicted GET /api/key.
 11. vault.py: module docstring matches the deliberately lazy KEK loading.
 12. render/content.py: the image_guard fallback appends to sys.path (no
     process-wide shadowing) and raises a clear ValueError when tools/editor
     is absent.

Frontend fixes are pinned grep-style (same convention as
test_qa_frontend_fixes.py); LLM HTTP calls use local stub servers.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

from ats.experience import extract_experience_requirements  # noqa: E402
from cover_letter import guards  # noqa: E402
from cover_letter.jd import parse_jd  # noqa: E402
from cover_letter.renderer import _safe_accent  # noqa: E402
from llm import client as client_mod  # noqa: E402


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1 — German requirement cues in _guess_title
# ---------------------------------------------------------------------------
class TestJdGermanRequirementCues:
    def test_muss_line_is_not_a_title(self):
        out = parse_jd("Muss Python können\nWir sind ein tolles Team.")
        assert out["title"] == ""

    def test_muessen_sollen_lines_are_not_titles(self):
        assert parse_jd("Müssen Java beherrschen\nWir sind ein Team.")["title"] == ""
        assert parse_jd("Sollen Docker kennen\nWir sind ein Team.")["title"] == ""

    def test_jahre_line_is_not_a_title(self):
        out = parse_jd("Wir suchen dich. Du hast 5 Jahre Erfahrung\nAcme GmbH")
        assert "5 Jahre Erfahrung" not in out["title"]

    def test_real_title_still_detected(self):
        out = parse_jd("Senior Backend Engineer\nWir sind ein tolles Team.")
        assert out["title"] == "Senior Backend Engineer"


# ---------------------------------------------------------------------------
# 2 — trailing "Experience" is part of the skill name
# ---------------------------------------------------------------------------
class TestSkillTrailingExperience:
    @pytest.mark.parametrize("skill", ["User Experience", "Customer Experience", "Developer Experience"])
    def test_capitalized_experience_is_kept(self, skill):
        out = extract_experience_requirements(f"- 4 years of {skill}")
        assert out["skill_years"][0]["skill"] == skill

    def test_lowercase_experience_still_stripped(self):
        out = extract_experience_requirements("- 4 years of Go experience")
        assert out["skill_years"][0]["skill"] == "Go"

    def test_german_erfahrung_still_stripped(self):
        # German capitalizes nouns — "Erfahrung" must strip case-insensitively.
        out = extract_experience_requirements("- 4 years of Kubernetes Erfahrung")
        assert out["skill_years"][0]["skill"] == "Kubernetes"


# ---------------------------------------------------------------------------
# 3 — cliché-strip a/an repair never makes text worse
# ---------------------------------------------------------------------------
class TestClicheArticleRepair:
    def test_university_keeps_a(self):
        cleaned, _ = guards.cliche_scan("a results-driven university graduate")
        assert cleaned == "a university graduate"

    def test_honest_gets_an(self):
        cleaned, _ = guards.cliche_scan("a results-driven honest broker")
        assert cleaned == "an honest broker"

    def test_plain_vowel_still_repaired(self):
        cleaned, _ = guards.cliche_scan("a results-driven engineer")
        assert cleaned == "an engineer"

    def test_plain_consonant_still_repaired(self):
        cleaned, _ = guards.cliche_scan("an results-driven plumber")
        assert cleaned == "a plumber"

    def test_wrong_an_before_uni_exception_repaired(self):
        cleaned, _ = guards.cliche_scan("an results-driven university graduate")
        assert cleaned == "a university graduate"

    def test_no_following_word_keeps_original_article(self):
        cleaned, _ = guards.cliche_scan("an results-driven")
        assert cleaned == "an"


# ---------------------------------------------------------------------------
# 4 — CJK Ext-B + Hangul Jamo count as one word per codepoint
# ---------------------------------------------------------------------------
class TestCjkWordCount:
    def test_ext_b_ideographs_counted(self):
        assert guards.word_count("\U00020000" * 100) == 100

    def test_hangul_jamo_counted(self):
        assert guards.word_count("ᄀ" * 100) == 100

    def test_bmp_cjk_unchanged(self):
        assert guards.word_count("中" * 5) == 5
        assert guards.word_count("hello world") == 2


# ---------------------------------------------------------------------------
# 5 — accent hex lengths are exactly 3/4/6/8
# ---------------------------------------------------------------------------
class TestAccentHexLengths:
    @pytest.mark.parametrize("value", ["#abc", "#abcd", "#aabbcc", "#aabbccdd"])
    def test_valid_lengths_accepted(self, value):
        assert _safe_accent(value) == value

    @pytest.mark.parametrize("value", ["#12345", "#1234567", "#ab", "#aabbccddeeff"])
    def test_invalid_lengths_rejected(self, value):
        assert _safe_accent(value) == "#111111"


# ---------------------------------------------------------------------------
# 6 — BYO Authorization header stripped on cross-host redirects
# ---------------------------------------------------------------------------
class _StubHandler(BaseHTTPRequestHandler):
    """302 redirector for the exact request path + header-capturing target."""

    captured: dict = {}
    redirect_to: str | None = None

    def _handle(self):
        if self.redirect_to and self.path == "/chat/completions":
            # Drain the POST body before responding or the unread bytes RST
            # the connection.
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        _StubHandler.captured = dict(self.headers.items())
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _handle

    def log_message(self, *args):  # silence test output
        pass


class _TargetHandler(_StubHandler):
    redirect_to = None


class _RedirectHandler(_StubHandler):
    pass


@pytest.fixture
def redirect_servers():
    target = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
    _StubHandler.captured = {}
    _RedirectHandler.redirect_to = (
        f"http://127.0.0.1:{target.server_address[1]}/chat/completions"
    )
    redirector = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    threads = [
        threading.Thread(target=s.serve_forever, daemon=True) for s in (target, redirector)
    ]
    for t in threads:
        t.start()
    yield target, redirector
    for s in (target, redirector):
        s.shutdown()
        s.server_close()


class TestRedirectAuthorizationStripped:
    def _client(self, monkeypatch, base_url):
        monkeypatch.setattr(
            client_mod, "validate_provider_base_url", lambda provider, url=None: url
        )
        return client_mod.OpenAICompatibleClient("custom", "sk-byo-secret", base_url=base_url)

    def test_cross_host_redirect_drops_authorization(self, monkeypatch, redirect_servers):
        _, redirector = redirect_servers
        client = self._client(monkeypatch, f"http://127.0.0.1:{redirector.server_address[1]}")
        assert client.complete(system="s", user="u") == "ok"
        assert "Authorization" not in _StubHandler.captured

    def test_same_origin_redirect_keeps_authorization(self, monkeypatch, redirect_servers):
        target, redirector = redirect_servers
        # Redirect to a different path on the SAME origin → header kept.
        _RedirectHandler.redirect_to = (
            f"http://127.0.0.1:{redirector.server_address[1]}/redirected/chat/completions"
        )
        try:
            client = self._client(monkeypatch, f"http://127.0.0.1:{redirector.server_address[1]}")
            assert client.complete(system="s", user="u") == "ok"
            assert _StubHandler.captured.get("Authorization") == "Bearer sk-byo-secret"
        finally:
            _RedirectHandler.redirect_to = (
                f"http://127.0.0.1:{target.server_address[1]}/chat/completions"
            )


# ---------------------------------------------------------------------------
# 7 — apply_service: bad ports + unbounded rate-limit buckets
# ---------------------------------------------------------------------------
class TestApplyServiceHardening:
    def test_bad_port_classifies_invalid_not_crash(self):
        import apply_service

        for url in ("https://example.com:badport/apply", "https://example.com:99999/apply"):
            out = apply_service.classify_apply_url(url)
            assert out["classification"] == "invalid"
            assert out["open_allowed"] is False

    def test_valid_port_still_classified(self):
        import apply_service

        out = apply_service.classify_apply_url("https://jobs.lever.co/acme/123")
        assert out["classification"] == "known_ats"
        assert out["open_allowed"] is True

    def test_rate_limit_buckets_do_not_grow_unboundedly(self, monkeypatch):
        import apply_service

        monkeypatch.setattr(apply_service, "_APPLY_ACTION_BUCKETS", {})
        stale = time.time() - 3600
        apply_service._APPLY_ACTION_BUCKETS["open:job-1"] = [stale, stale]
        apply_service._enforce_apply_action_rate_limit("open", "job-1")
        # Stale timestamps were pruned rather than accumulating forever.
        bucket = apply_service._APPLY_ACTION_BUCKETS["open:job-1"]
        assert len(bucket) == 1
        assert all(ts >= time.time() - 60 for ts in bucket)


# ---------------------------------------------------------------------------
# 8 — form.js: move vs mid-removal desync + disabled focus target
# ---------------------------------------------------------------------------
class TestFormJsMoveRemovalDesync:
    def test_remove_marks_card_and_moves_bail(self):
        form = _read("tools/editor/static/form.js")
        assert 'card.dataset.removing = "1";' in form
        assert "if (prev && prev.dataset.removing) return;" in form
        assert "if (next && next.dataset.removing) return;" in form
        # The marker is set before the exit animation begins (the desync window).
        remove = form.split('tr("form.remove", "Remove"),', 1)[1].split("onChange();", 1)[0]
        assert remove.index('dataset.removing = "1"') < remove.index("await animateCardOut(card)")

    def test_focus_target_excludes_disabled_buttons(self):
        form = _read("tools/editor/static/form.js")
        assert "button:not(:disabled)" in form
        assert '"button, input, textarea, select, a[href], [tabindex]"' not in form


# ---------------------------------------------------------------------------
# 9 — app.js: yamlBufferInvalid reset + tour focuses the step card
# ---------------------------------------------------------------------------
class TestAppJsStateAndTourFocus:
    def test_load_cv_resets_yaml_buffer_invalid(self):
        app = _read("tools/editor/static/app.js")
        load = app.split("async function loadCv()", 1)[1].split("async function", 1)[0]
        conflict = load.index("conflictPending = false;")
        assert "yamlBufferInvalid = false;" in load
        assert load.index("yamlBufferInvalid = false;") - conflict < 120

    def test_tour_focuses_card_not_next_button(self):
        app = _read("tools/editor/static/app.js")
        assert "next.focus();" not in app
        assert 'focusCard.setAttribute("tabindex", "-1")' in app
        assert "focusCard.focus({ preventScroll: true });" in app


# ---------------------------------------------------------------------------
# 10 — demo-mode key save fails loudly instead of fake success
# ---------------------------------------------------------------------------
class TestDemoModeKeySave:
    def test_save_key_raises_clear_error_in_demo_mode(self, monkeypatch):
        import llm_gateway
        import local_demo_store

        monkeypatch.setattr(local_demo_store, "active", lambda: True)
        with pytest.raises(llm_gateway.LLMUnavailable) as excinfo:
            llm_gateway.save_key("demo-owner", "anthropic", "sk-ant-demo-abcd1234")
        exc = excinfo.value
        assert exc.status == 400
        assert exc.payload["code"] == "demo_mode_key_not_stored"
        assert "not stored in demo mode" in exc.payload["error"]


# ---------------------------------------------------------------------------
# 11 — vault docstring matches the lazy KEK loading
# ---------------------------------------------------------------------------
class TestVaultDocstring:
    def test_docstring_documents_lazy_loading(self):
        import vault

        doc = vault.__doc__ or ""
        assert "fails fast at import" not in doc
        assert "lazily" in doc.lower()


# ---------------------------------------------------------------------------
# 12 — image_guard fallback: sys.path.append + clear standalone error
# ---------------------------------------------------------------------------
class TestPhotoValidationFallback:
    def test_source_appends_never_inserts(self):
        src = _read("engine/render/content.py")
        assert "sys.path.append(str(editor_dir))" in src
        assert "sys.path.insert(0" not in src

    def test_missing_editor_dir_raises_clear_value_error(self, monkeypatch, tmp_path):
        from render import content

        # Force the ImportError fallback, then pretend tools/editor is absent.
        monkeypatch.setitem(sys.modules, "image_guard", None)
        monkeypatch.setattr(content.Path, "is_dir", lambda self: False)
        with pytest.raises(ValueError, match="photo validation is unavailable"):
            content._validate_photo("data:image/png;base64,AAAA", "cv.yaml")
