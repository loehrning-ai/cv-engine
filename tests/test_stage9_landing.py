"""Stage 9 tests — public landing page, SEO, onboarding, i18n, analytics.

These exercise the public, install-free front door and its supporting
scaffolds WITHOUT any network or real Supabase:

  * ``/`` serves the marketing landing page (logged-out) with the SEO meta
    tags present and the ``PUBLIC_BASE_URL`` canonical injected;
  * ``/robots.txt`` + ``/sitemap.xml`` serve 200 with the right content-type
    and reference the sitemap / base URL;
  * the landing + SEO + i18n routes do NOT require auth, but the editor's
    per-user API (``/api/cvs``) still 401s without a bearer token, and the
    editor shell at ``/app`` is reachable (auth is enforced client-side +
    on every ``/api/*`` call);
  * ``/api/i18n`` resolves for both ``en`` and ``de`` (EN-fallback for gaps);
  * the analytics hook is a guaranteed no-op when ``POSTHOG_KEY`` is unset
    (``is_enabled()`` False, ``capture`` returns without error / network).

The ``client`` fixture (conftest) runs the REAL auth gate — so the 401
assertion is meaningful — while the landing/SEO/i18n routes pass because they
are auth-exempt.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))


# ---------------------------------------------------------------------------
# Landing page (public, logged-out) + SEO meta tags
# ---------------------------------------------------------------------------
class TestLandingPage:
    def test_root_serves_landing_html(self, client):
        res = client.get("/")
        assert res.status_code == 200, res.get_data(as_text=True)
        assert "text/html" in res.headers.get("Content-Type", "")
        body = res.get_data(as_text=True)
        # Marketing front door, not the editor shell.
        assert "CV Copilot" in body
        assert 'href="/app"' in body  # "Get started" CTA → the editor app
        assert "/static/landing.js" in body

    def test_landing_has_seo_meta(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "<title>" in body
        assert 'name="description"' in body
        assert 'property="og:title"' in body
        assert 'property="og:url"' in body
        assert 'name="twitter:card"' in body
        assert 'rel="canonical"' in body

    def test_canonical_uses_public_base_url(self, client, monkeypatch):
        import importlib

        monkeypatch.setenv("PUBLIC_BASE_URL", "https://onepager.example.com")
        monkeypatch.setenv(
            "ONEPAGER_PRIVACY_CONTACT",
            "privacy-controller@example.com",
        )
        server = importlib.reload(importlib.import_module("server"))
        c = server.app.test_client()
        body = c.get("/").get_data(as_text=True)
        # The literal token must be substituted, not leaked.
        assert "__PUBLIC_BASE_URL__" not in body
        assert "https://onepager.example.com/" in body
        # Reload back to the default so other tests see a clean module.
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.delenv("ONEPAGER_PRIVACY_CONTACT", raising=False)
        importlib.reload(server)


# ---------------------------------------------------------------------------
# SEO routes (robots.txt / sitemap.xml)
# ---------------------------------------------------------------------------
class TestSeoRoutes:
    def test_robots_txt(self, client):
        res = client.get("/robots.txt")
        assert res.status_code == 200
        assert res.headers.get("Content-Type", "").startswith("text/plain")
        body = res.get_data(as_text=True)
        assert "User-agent: *" in body
        assert "Sitemap:" in body
        assert "/sitemap.xml" in body

    def test_sitemap_xml(self, client):
        res = client.get("/sitemap.xml")
        assert res.status_code == 200
        assert "xml" in res.headers.get("Content-Type", "")
        body = res.get_data(as_text=True)
        assert "<urlset" in body
        assert "<loc>" in body


# ---------------------------------------------------------------------------
# Auth gating — public routes open, editor API still gated
# ---------------------------------------------------------------------------
class TestAuthGating:
    @pytest.mark.parametrize("path", ["/", "/robots.txt", "/sitemap.xml", "/api/i18n", "/healthz"])
    def test_public_routes_do_not_require_auth(self, client, path):
        res = client.get(path)
        assert res.status_code == 200, f"{path} → {res.status_code}"

    def test_app_editor_shell_reachable_without_token(self, client):
        # The editor HTML shell is served (auth enforced client-side + on /api/*).
        res = client.get("/app")
        assert res.status_code == 200
        assert "text/html" in res.headers.get("Content-Type", "")

    def test_editor_api_still_requires_auth(self, client):
        # The per-user data API must 401 without a bearer token.
        res = client.get("/api/cvs")
        assert res.status_code == 401, res.get_data(as_text=True)

    def test_build_api_requires_auth(self, client):
        res = client.post("/api/build", json={})
        assert res.status_code == 401


# ---------------------------------------------------------------------------
# i18n — EN + DE resolve, EN-fallback, unknown → EN
# ---------------------------------------------------------------------------
class TestI18n:
    def test_i18n_endpoint_en(self, client):
        res = client.get("/api/i18n?lang=en")
        assert res.status_code == 200
        data = res.get_json()
        assert data["lang"] == "en"
        assert "en" in data["supported"] and "de" in data["supported"]
        assert data["strings"]["landing.hero.cta"]

    def test_i18n_endpoint_de(self, client):
        data = client.get("/api/i18n?lang=de").get_json()
        assert data["lang"] == "de"
        # A genuinely translated string differs from its English form.
        assert data["strings"]["landing.nav.signin"] == "Anmelden"

    def test_i18n_unknown_lang_falls_back_to_en(self, client):
        data = client.get("/api/i18n?lang=zz").get_json()
        assert data["lang"] == "en"

    def test_i18n_module_resolve_fills_gaps_with_en(self):
        import i18n

        en = i18n.resolve("en")
        de = i18n.resolve("de")
        # Every key present in both; no empty/missing values.
        assert set(en.keys()) == set(de.keys())
        assert all(v for v in en.values())
        assert all(v for v in de.values())
        # A DE-translated key really is German.
        assert de["tour.next"] == "Weiter"

    def test_i18n_normalize_lang(self):
        import i18n

        assert i18n.normalize_lang("DE") == "de"
        assert i18n.normalize_lang("en-US") == "en"
        assert i18n.normalize_lang(None) == "en"
        assert i18n.normalize_lang("fr") == "en"  # unsupported → default


# ---------------------------------------------------------------------------
# Analytics — NO-OP by default (no POSTHOG_KEY)
# ---------------------------------------------------------------------------
class TestAnalyticsNoop:
    def test_disabled_without_key(self, monkeypatch):
        import analytics

        monkeypatch.delenv("POSTHOG_KEY", raising=False)
        assert analytics.is_enabled() is False

    def test_capture_is_noop_without_key(self, monkeypatch):
        """capture() must not raise and must not touch the network when unset."""
        import analytics

        monkeypatch.delenv("POSTHOG_KEY", raising=False)

        called = {"posted": False}

        def _boom(*_a, **_k):
            called["posted"] = True
            raise AssertionError("network must not be touched when disabled")

        monkeypatch.setattr(analytics, "_post", _boom)
        # Must return cleanly (no exception, no dispatch).
        analytics.capture(analytics.EVENT_SIGNUP, "user-123")
        analytics.capture(analytics.EVENT_FIRST_EXPORT, "user-123", {"k": "v"})
        assert called["posted"] is False

    def test_enabled_with_key(self, monkeypatch):
        import analytics

        monkeypatch.setenv("POSTHOG_KEY", "phc_test")
        assert analytics.is_enabled() is True

    def test_capture_dispatches_when_enabled(self, monkeypatch):
        """With a key set, capture dispatches to _post on a thread (no real net)."""
        import threading

        import analytics

        monkeypatch.setenv("POSTHOG_KEY", "phc_test")
        done = threading.Event()
        seen = {}

        def _fake_post(payload):
            seen.update(payload)
            done.set()

        monkeypatch.setattr(analytics, "_post", _fake_post)
        analytics.capture(analytics.EVENT_SIGNUP, "user-123", {"source": "test"})
        assert done.wait(timeout=2.0), "capture did not dispatch to _post"
        assert seen.get("event") == analytics.EVENT_SIGNUP
        assert seen.get("distinct_id") == "user-123"
        assert seen["properties"]["$lib"] == "onepager"

    def test_analytics_event_route_noop_without_key(self, fake_supabase, client, monkeypatch):
        """The /api/analytics/event route returns 200 and is a no-op when unset."""
        import analytics

        monkeypatch.delenv("POSTHOG_KEY", raising=False)
        captured = []
        monkeypatch.setattr(analytics, "capture", lambda *a, **k: captured.append(a))
        res = client.post("/api/analytics/event", json={"event": "first_export"})
        assert res.status_code == 200
        assert res.get_json() == {"ok": True}
        # The route forwards to analytics.capture (which is the no-op gate).
        assert captured and captured[0][0] == "first_export"

    def test_analytics_event_route_ignores_unknown_event(self, fake_supabase, client, monkeypatch):
        import analytics

        captured = []
        monkeypatch.setattr(analytics, "capture", lambda *a, **k: captured.append(a))
        res = client.post("/api/analytics/event", json={"event": "evil_event"})
        assert res.status_code == 200
        assert captured == []  # unknown event names are dropped
