"""Stage 8 — EN/DE i18n for the new surfaces + input-validation hardening.

This suite locks in the final stage of plan 002:

  1. **i18n EN/DE** — the server-side string table (the single source of truth,
     ``tools/editor/i18n.py``) carries an EN *and* a DE translation for every key
     the new surfaces use (account, jobs catalog, cover-letter library, shared
     sub-page chrome). The static pages carry the matching ``data-i18n`` keys so
     ``OnePagerI18n.apply`` can localise them, and each page loads ``i18n.js``
     (the plan flagged account.html as missing the loader).

  2. **Structured letter DE chrome** — the cover-letter renderer emits German
     business-letter chrome (Betreff / Sehr geehrte … / Mit freundlichen Grüßen /
     Anlagen) when ``language == "de"`` and the English equivalents otherwise.

  3. **Input-validation hardening at the new boundaries** (defense-in-depth):
       * ``/api/account/profile`` — provider ∈ {anthropic, gemini} and model ∈
         the provider allowlist; an unknown provider/model is a 400 (never
         persisted, never a 500).
       * ``/api/documents`` — the ``?type=`` filter is one of cv|cover_letter|all;
         anything else is a 400 (fail closed), never a 500.
       * job metadata — confidence > 100 / a malformed deadline / an oversize
         note coerce to a safe value (never a 500); a garbage status is a 400.

These are pure / fake-Supabase tests — the genuinely cross-tenant RLS matrix is
covered against the real project elsewhere (test_stage2_auth, the env-gated
blocks in stage 6/7).
"""
from __future__ import annotations

import sys
import re
import uuid as _uuid
from pathlib import Path

import pytest
import yaml as _yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

STATIC = REPO / "tools" / "editor" / "static"

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"
JD_TEXT = (
    "Title: Senior Data Engineer\nCompany: Acme GmbH\n"
    "Spark and Airflow required. Build ETL pipelines."
)


# ---------------------------------------------------------------------------
# Seed helpers (mirror the stage 6/7 shapes).
# ---------------------------------------------------------------------------
def _seed_profile(store, owner_id=FAKE_OWNER, *, credits=3):
    store.tables.setdefault("profiles", []).append(
        {
            "id": owner_id,
            "email": "stub@onepager.test",
            "display_name": None,
            "default_language": "en",
            "preferred_provider": "anthropic",
            "preferred_model": None,
            "managed_credits_remaining": credits,
            "deletion_requested_at": None,
        }
    )


def _profile_row(store, owner_id=FAKE_OWNER):
    return next(r for r in store.tables.get("profiles", []) if r.get("id") == owner_id)


def _seed_cv(store, *, name="My CV", ts=1):
    rows = store.tables.setdefault("documents", [])
    doc_id = str(_uuid.uuid4())
    rows.append(
        {
            "id": doc_id,
            "owner_id": store.owner_id,
            "type": "cv",
            "name": name,
            "yaml_blob": _yaml.safe_dump({"name": "X"}, sort_keys=False),
            "deleted_at": None,
            "updated_at": ts,
        }
    )
    return doc_id


# ===========================================================================
#  1. i18n string table — EN *and* DE for every new-surface key.
# ===========================================================================
class TestI18nStringTable:
    # The keys each new surface relies on. Every one must resolve to a non-empty
    # EN and a non-empty, DISTINCT-from-the-key DE string.
    ACCOUNT_KEYS = [
        "account.title",
        "account.profile.heading",
        "account.profile.name",
        "account.profile.language",
        "account.profile.save",
        "account.ai.heading",
        "account.ai.provider",
        "account.ai.model",
        "account.ai.connect",
        "account.ai.remove",
        "account.ai.byo_required",
        "account.data.heading",
        "account.delete.heading",
        "account.delete.button",
        "account.job_sources.heading",
        "account.job_sources.intro",
        "account.job_sources.policy.no_blind_fetch",
        "account.job_sources.policy.no_sessions",
        "account.job_sources.policy.no_cookies",
        "account.job_sources.policy.no_stealth",
        "account.job_sources.policy.read_only",
        "account.job_sources.defaults_heading",
        "account.job_sources.defaults_intro",
        "account.job_sources.source",
        "account.job_sources.source_aria",
        "account.job_sources.source.none",
        "account.job_sources.source.linkedin",
        "account.job_sources.source.manual",
        "account.job_sources.source.all",
        "jobs.import.source.all",
        "account.job_sources.title",
        "account.job_sources.family",
        "account.job_sources.titles",
        "account.job_sources.search_mode",
        "account.job_sources.search_mode_aria",
        "account.job_sources.country",
        "account.job_sources.region",
        "account.job_sources.cities",
        "account.job_sources.remote.label",
        "account.job_sources.remote.aria",
        "account.job_sources.remote.any",
        "account.job_sources.remote.onsite",
        "account.job_sources.remote.hybrid",
        "account.job_sources.remote.remote",
        "account.job_sources.seniority",
        "account.job_sources.min_years",
        "account.job_sources.freshness.label",
        "account.job_sources.freshness.aria",
        "account.job_sources.freshness.any",
        "account.job_sources.freshness.fresh_only",
        "account.job_sources.freshness.stale_only",
        "account.job_sources.ranking",
        "account.job_sources.save",
        "account.job_sources.reset",
        "account.job_sources.export",
        "account.job_sources.import",
        "account.job_sources.import_prompt",
        "account.job_sources.open_jobs",
        "account.job_sources.feedback.saved",
        "account.job_sources.feedback.reset",
        "account.job_sources.feedback.exported",
        "account.job_sources.feedback.imported",
        "account.job_sources.feedback.import_invalid",
        "account.job_sources.feedback.import_schema",
        "account.job_sources.status.checking",
        "account.job_sources.status.local_available",
        "account.job_sources.status.local_unavailable",
        "account.job_sources.status.rows",
        "account.job_sources.status.latest_seen",
        "account.job_sources.status.days_old",
        "account.job_sources.status.adapter_configured",
        "account.job_sources.status.adapter_disabled",
        "account.job_sources.status.no_blind_fetch",
        "account.job_sources.status.stale",
        "account.job_sources.status.unavailable",
    ]
    JOBS_KEYS = [
        "jobs.title",
        "jobs.page.sub",
        "jobs.add.summary",
        "jobs.add.button",
        "jobs.field.deadline",
        "jobs.field.confidence",
        "jobs.field.whyfit",
        "jobs.filter.all",
        "jobs.filter.search_mode",
        "jobs.filter.mode.flexible",
        "jobs.filter.mode.strict",
        "jobs.filter.family",
        "jobs.filter.city",
        "jobs.filter.min_years",
        "jobs.filter.rank.apply_first",
        "jobs.filter.rank.strong_volume",
        "jobs.filter.rank.frontier_stretch",
        "jobs.filter.rank.berlin_backups",
        "jobs.import.source.default",
        "jobs.source.full_jd_label",
        "jobs.status.saved",
        "jobs.status.open",
        "jobs.status.speculative",
        "jobs.status.applied",
        "jobs.status.closed",
        "jobs.card.details",
        "jobs.card.open",
        "jobs.card.generate",
        "jobs.note_label",
        "jobs.deadline.closed",
        "jobs.empty.none",
        "jobs.packet.title",
        "jobs.linked.pdf",
        "jobs.linked.building",
        "jobs.years",
    ]
    LIBRARY_KEYS = [
        "cl.title",
        "cl.page.sub",
        "cl.status.editable",
        "sidebar.library",
        "sidebar.sections",
        "sidebar.job_outputs",
        "sidebar.empty",
        "sidebar.error",
        "workflow.group.start",
        "workflow.group.documents",
        "workflow.group.review",
        "workflow.cvs.title",
        "lib.heading",
        "lib.filter.cover",
        "lib.badge.cv",
        "lib.badge.cover",
        "lib.storage.saved",
        "lib.storage.linked_job",
        "lib.storage.standalone_letter",
        "lib.empty.all",
        "lib.empty.cv",
        "lib.empty.cover",
    ]
    CREDIT_KEYS = [
        "credit.loading.short",
        "credit.loading.full",
        "credit.connected.short",
        "credit.connected.full",
        "credit.remaining.short",
        "credit.remaining.full.one",
        "credit.remaining.full.many",
        "credit.zero.short",
        "credit.zero.full",
        "credit.unavailable.short",
        "credit.unavailable.full",
    ]
    SHARED_NAV_KEYS = [
        "nav.cv_editor",
        "nav.applications",
        "nav.account",
        "nav.privacy",
        "mobile.settings",
        "templates.kicker",
        "templates.title",
        "templates.tooltip",
        "templates.close",
        "templates.hint",
        "app.collapse_all_short",
        "app.expand_all_short",
    ]

    @pytest.mark.parametrize(
        "keys",
        [ACCOUNT_KEYS, JOBS_KEYS, LIBRARY_KEYS, CREDIT_KEYS, SHARED_NAV_KEYS],
        ids=["account", "jobs", "library", "credit", "shared_nav"],
    )
    def test_keys_have_en_and_de(self, keys):
        import i18n

        for key in keys:
            assert key in i18n.STRINGS, f"missing i18n key {key!r}"
            entry = i18n.STRINGS[key]
            assert entry.get("en"), f"{key!r} has no EN string"
            assert entry.get("de"), f"{key!r} has no DE string"

    def test_every_key_in_table_has_both_languages(self):
        """No key may ship EN-only — Gate-1 requires DE first-class for all keys."""
        import i18n

        en_only = [k for k, v in i18n.STRINGS.items() if not v.get("de")]
        assert en_only == [], f"keys missing a DE translation: {en_only}"

    def test_de_differs_from_en_for_translated_labels(self):
        """A representative sample of new keys is actually translated (DE != EN)."""
        import i18n

        sample = [
            "account.profile.heading",  # Profile -> Profil
            "jobs.status.open",  # Open -> Offen
            "lib.badge.cover",  # Cover letter -> Anschreiben
            "nav.cv_editor",  # CV editor -> Lebenslauf-Editor
        ]
        for key in sample:
            en = i18n.STRINGS[key]["en"]
            de = i18n.STRINGS[key]["de"]
            assert de != en, f"{key!r} DE is identical to EN ({en!r})"

    def test_api_i18n_resolves_de_for_new_keys(self, client):
        """The /api/i18n endpoint serves the DE dictionary the pages fetch."""
        data = client.get("/api/i18n?lang=de").get_json()
        assert data["lang"] == "de"
        strings = data["strings"]
        assert strings["account.profile.heading"] == "Profil"
        assert strings["jobs.status.applied"] == "Beworben"
        assert strings["lib.badge.cover"] == "Anschreiben"

    def test_static_account_jobs_i18n_references_exist(self):
        import i18n

        keys: set[str] = set()
        for page in ("account.html", "jobs.html", "cover-letters.html", "tailor.html"):
            html = (STATIC / page).read_text(encoding="utf-8")
            keys.update(re.findall(r'data-i18n="([^"]+)"', html))
            keys.update(re.findall(r'data-i18n-html="([^"]+)"', html))
            for attr in re.findall(r'data-i18n-attr="([^"]+)"', html):
                for pair in attr.split(","):
                    if ":" in pair:
                        keys.add(pair.split(":", 1)[1])
        missing = sorted(key for key in keys if key not in i18n.STRINGS)
        assert missing == []
        for key in keys:
            assert i18n.STRINGS[key].get("en")
            assert i18n.STRINGS[key].get("de")


# ===========================================================================
#  2. The static pages carry the data-i18n keys + load the i18n loader.
# ===========================================================================
class TestPagesWired:
    @pytest.mark.parametrize(
        "page",
            ["account.html", "jobs.html", "cover-letters.html", "tailor.html"],
    )
    def test_page_loads_i18n_js(self, page):
        """Every new surface includes the i18n loader (account.html was flagged)."""
        html = (STATIC / page).read_text(encoding="utf-8")
        assert "/static/i18n.js" in html, f"{page} does not load i18n.js"

    def test_account_page_has_provider_and_profile_keys(self, client):
        # Fetch the SERVED page: the title (account.title) arrives with the
        # shared chrome injected at serve time (plan 108 stage 2).
        html = client.get("/account").get_data(as_text=True)
        for key in (
            "account.title",
            "account.profile.heading",
            "account.profile.name",
            "account.profile.language",
            "account.ai.heading",
            "account.ai.provider",
            "account.ai.model",
            "account.delete.button",
            "account.job_sources.family",
            "account.job_sources.titles",
            "account.job_sources.search_mode",
            "account.job_sources.cities",
            "account.job_sources.min_years",
            "account.job_sources.ranking",
        ):
            assert f'data-i18n="{key}"' in html, f"account.html missing data-i18n={key}"
        assert 'data-i18n-attr="aria-label:account.job_sources.search_mode_aria"' in html

    def test_cover_letters_page_has_accessible_i18n_keys(self):
        html = (STATIC / "cover-letters.html").read_text(encoding="utf-8")
        for key in (
            "cl.generate.heading",
            "cl.field.source_cv",
            "cl.field.tone",
            "cl.field.recipient",
            "cl.field.jd",
            "cl.packet.hashes",
            "cl.packet.review",
            "cl.body.aria",
            "cl.preview_title",
        ):
            assert key in html, f"cover-letters.html missing {key}"
        assert 'name="cover_letter_body"' in html
        assert 'disabled' in html.split('id="cl-body"', 1)[1].split("</textarea>", 1)[0]
        assert 'aria-controls="cl-preview"' in html
        assert 'aria-expanded="false"' in html

    def test_tailor_page_has_page_specific_i18n_keys(self):
        html = (STATIC / "tailor.html").read_text(encoding="utf-8")
        for key in (
            "tailor.form.heading",
            "tailor.form.intro",
            "tailor.field.source_cv",
            "tailor.field.profile",
            "tailor.field.jd",
            "tailor.score.button",
            "tailor.review.button",
            "tailor.optimize.button",
            "tailor.gaps.heading",
            "tailor.matched.heading",
        ):
            assert key in html, f"tailor.html missing {key}"

    def test_jobs_page_has_catalog_keys(self):
        html = (STATIC / "jobs.html").read_text(encoding="utf-8")
        for key in (
            "jobs.title",
            "jobs.page.sub",
            "jobs.add.summary",
            "jobs.field.deadline",
            "jobs.field.confidence",
            "jobs.filter.search_mode",
            "jobs.filter.mode.flexible",
            "jobs.filter.mode.strict",
            "jobs.filter.family",
            "jobs.filter.city",
            "jobs.filter.min_years",
            "jobs.status.open",
            "jobs.status.closed",
            "jobs.packet.title",
        ):
            assert f'data-i18n="{key}"' in html, f"jobs.html missing data-i18n={key}"

    def test_tool_pages_have_page_level_hierarchy(self):
        jobs_html = (STATIC / "jobs.html").read_text(encoding="utf-8")
        letters_html = (STATIC / "cover-letters.html").read_text(encoding="utf-8")
        css = (STATIC / "cover-letters.css").read_text(encoding="utf-8")

        assert 'class="tool-page-head"' in jobs_html
        assert 'id="jobs-page-heading"' in jobs_html
        assert 'class="tool-page-head"' in letters_html
        assert 'id="letters-page-heading"' in letters_html
        assert 'class="cl-library-panel"' in letters_html
        assert letters_html.index('for="cl-cv"') < letters_html.index('id="cl-generate"')
        assert letters_html.index('id="cl-generate"') < letters_html.index('id="cl-list"')
        assert ".tool-badge-filter" in css
        assert ".tool-badge-doc" in css
        assert ".tool-badge-counter" in css

    def test_library_page_has_type_badge_and_filter_keys(self):
        html = (STATIC / "cover-letters.html").read_text(encoding="utf-8")
        for key in (
            "cl.title",
            "lib.heading",
            "lib.filter.cover",
        ):
            assert f'data-i18n="{key}"' in html, f"cover-letters.html missing data-i18n={key}"

    @pytest.mark.parametrize(
        ("page", "route"),
        [("jobs.html", "/jobs"), ("cover-letters.html", "/cover-letters"), ("tailor.html", "/tailor")],
    )
    def test_generation_pages_load_credit_state_renderer(self, client, page, route):
        raw = (STATIC / page).read_text(encoding="utf-8")
        assert "/static/credit-state.js" in raw, f"{page} does not load credit-state.js"
        # The credit slot itself ships with the serve-time chrome (plan 108
        # stage 2), so assert against the SERVED page.
        html = client.get(route).get_data(as_text=True)
        assert 'class="cl-credit muted" role="status"' in html

    def test_credit_renderer_keeps_visible_text_compact(self):
        js = (STATIC / "credit-state.js").read_text(encoding="utf-8")
        assert '"{n} left"' in js
        assert '"AI key connected"' in js
        assert "aria-label" in js
        for page_js in ("jobs.js", "cover-letters.js", "tailor.js"):
            text = (STATIC / page_js).read_text(encoding="utf-8")
            assert "OnePagerCredit.render" in text
            assert "free generation\" +" not in text

    def test_dynamic_js_labels_resolve_via_t(self):
        """The JS-rendered (dynamic) labels go through the i18n t() helper, so
        they switch with the toggle rather than being hardcoded EN."""
        jobs_js = (STATIC / "jobs.js").read_text(encoding="utf-8")
        assert 'statusLabel(' in jobs_js  # status badges localised
        assert 'verificationLabel(' in jobs_js
        assert 't("jobs.verification.' in jobs_js
        assert 't("jobs.card.generate"' in jobs_js
        assert 't("jobs.deadline.closed"' in jobs_js
        assert 't("jobs.years"' in jobs_js
        assert 't("jobs.add.error.no_input"' in jobs_js
        assert 't("jobs.import.feedback.local_search_failed"' in jobs_js
        assert 't("lib.untitled.cv"' in jobs_js
        cl_js = (STATIC / "cover-letters.js").read_text(encoding="utf-8")
        assert 't("lib.badge.cover"' in cl_js
        assert 't("lib.badge.cv"' in cl_js
        assert 't("cl.feedback.pick_cv"' in cl_js
        assert 't("cl.feedback.preview_failed"' in cl_js
        assert "function togglePreview" in cl_js
        tailor_js = (STATIC / "tailor.js").read_text(encoding="utf-8")
        assert 't("tailor.score.button"' in tailor_js
        assert 't("tailor.feedback.review_ready"' in tailor_js
        assert 't("tailor.profile.systems"' in tailor_js
        ac_js = (STATIC / "account.js").read_text(encoding="utf-8")
        assert 't("account.ai.connect"' in ac_js
        command_js = (STATIC / "command-palette.js").read_text(encoding="utf-8")
        assert 't("mobile.settings"' in command_js

    def test_pages_mount_the_language_toggle(self):
        """Each new surface mounts the shared EN|DE toggle into its topbar."""
        for page in ("account.js", "jobs.js", "cover-letters.js", "tailor.js"):
            js = (STATIC / page).read_text(encoding="utf-8")
            assert "OnePagerI18n.init(" in js, f"{page} does not mount the i18n toggle"

    def test_navbar_labels_are_nowrap_for_stability(self):
        """The topbar labels are nowrap so the longer DE strings can't wrap the
        header onto a second line (the pixel-stable-navbar rule). The rule
        lives in the consolidated .cl-back/.cl-title block (plan 108 stage 3
        removed the triple-declared duplicates)."""
        css = (STATIC / "cover-letters.css").read_text(encoding="utf-8")
        block = css.split(".cl-back, .cl-title {", 1)[1].split("}", 1)[0]
        assert "white-space: nowrap" in block


# ===========================================================================
#  3. Structured cover-letter renderer — DE chrome vs EN chrome.
# ===========================================================================
class TestLetterChrome:
    def _doc(self, language):
        return {
            "body": "At Meta I cut pipeline latency by 40%.",
            "role": "Data Engineer",
            "language": language,
            "attachments": ["CV"] if language == "en" else ["Lebenslauf"],
        }

    def test_de_chrome(self):
        from cover_letter.renderer import render_cover_letter_html

        html = render_cover_letter_html(self._doc("de"))
        # Betreff (subject), salutation, closing, Anlagen (enclosures).
        assert "Betreff:" in html
        assert "Bewerbung als Data Engineer" in html
        assert "Sehr geehrte Damen und Herren," in html
        assert "Mit freundlichen Grüßen," in html
        assert "Anlagen" in html
        # No English chrome leaked into the German letter.
        assert "Enclosures" not in html
        assert "Dear " not in html
        assert "Best regards" not in html

    def test_en_chrome(self):
        from cover_letter.renderer import render_cover_letter_html

        html = render_cover_letter_html(self._doc("en"))
        assert "Application for Data Engineer" in html
        assert "Dear Hiring Manager," in html
        assert "Best regards," in html
        assert "Enclosures" in html
        # No German chrome in the English letter.
        assert "Betreff" not in html
        assert "Anlagen" not in html
        assert "Mit freundlichen" not in html

    def test_explicit_subject_is_betreff_labelled_in_de(self):
        """An explicit subject still gets the German Betreff label (but only once)."""
        from cover_letter.renderer import render_cover_letter_html

        html = render_cover_letter_html(
            {"body": "x", "subject": "Bewerbung als Senior Data Engineer", "language": "de"}
        )
        assert html.count("Betreff:") == 1
        assert "Betreff: Bewerbung als Senior Data Engineer" in html


# ===========================================================================
#  4a. /api/account/profile — provider + model allowlist (400, not 500).
# ===========================================================================
class TestProviderValidation:
    def test_valid_provider_and_model_persist(self, client, fake_supabase):
        _seed_profile(fake_supabase)
        res = client.post(
            "/api/account/profile",
            json={"preferred_provider": "gemini", "preferred_model": "gemini-2.5-pro"},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        row = _profile_row(fake_supabase)
        assert row["preferred_provider"] == "gemini"
        assert row["preferred_model"] == "gemini-2.5-pro"

    def test_unknown_provider_is_400_not_persisted(self, client, fake_supabase):
        _seed_profile(fake_supabase)
        res = client.post("/api/account/profile", json={"preferred_provider": "skynet"})
        assert res.status_code == 400
        # Nothing junk persisted — the row keeps its seeded provider.
        assert _profile_row(fake_supabase)["preferred_provider"] == "anthropic"

    def test_unknown_model_is_400_not_persisted(self, client, fake_supabase):
        _seed_profile(fake_supabase)
        res = client.post(
            "/api/account/profile",
            json={"preferred_provider": "gemini", "preferred_model": "gemini-9.9-ultra"},
        )
        assert res.status_code == 400
        assert _profile_row(fake_supabase)["preferred_model"] is None

    def test_cross_vendor_model_is_400(self, client, fake_supabase):
        """A Claude model under the Gemini provider is incoherent → rejected."""
        _seed_profile(fake_supabase)
        res = client.post(
            "/api/account/profile",
            json={"preferred_provider": "gemini", "preferred_model": "claude-sonnet-4-5"},
        )
        assert res.status_code == 400

    def test_bad_provider_never_500s(self, client, fake_supabase):
        """A range of junk provider/model inputs all fail closed (4xx, never 5xx)."""
        _seed_profile(fake_supabase)
        for payload in (
            {"preferred_provider": ""},
            {"preferred_provider": "openai"},
            {"preferred_provider": 123},
            {"preferred_model": "whatever"},
            {"preferred_provider": "anthropic", "preferred_model": "gpt-4o"},
        ):
            res = client.post("/api/account/profile", json=payload)
            assert res.status_code < 500, (payload, res.get_data(as_text=True))


# ===========================================================================
#  4b. /api/documents — type filter allowlist (400, not 500).
# ===========================================================================
class TestDocumentFilterValidation:
    def test_default_returns_all_types(self, client, fake_supabase):
        _seed_cv(fake_supabase, name="My CV", ts=1)
        res = client.get("/api/documents")
        assert res.status_code == 200
        assert res.get_json()["count"] == 1

    @pytest.mark.parametrize("value", ["all", "cv", "cover_letter"])
    def test_valid_type_filters_accepted(self, client, fake_supabase, value):
        _seed_cv(fake_supabase, name="My CV", ts=1)
        res = client.get(f"/api/documents?type={value}")
        assert res.status_code == 200, res.get_data(as_text=True)

    def test_type_filter_partitions_payload(self, client, fake_supabase):
        _seed_cv(fake_supabase, name="My CV", ts=1)
        # ?type=cover_letter must not return the CV.
        body = client.get("/api/documents?type=cover_letter").get_json()
        assert body["count"] == 0
        # ?type=cv returns it.
        body = client.get("/api/documents?type=cv").get_json()
        assert body["count"] == 1 and body["documents"][0]["type"] == "cv"

    @pytest.mark.parametrize("value", ["resume", "CV; DROP TABLE", "all,cv", "1", " "])
    def test_bad_type_filter_is_400_not_500(self, client, fake_supabase, value):
        res = client.get("/api/documents", query_string={"type": value})
        assert res.status_code == 400, (value, res.get_data(as_text=True))
        assert res.status_code < 500


# ===========================================================================
#  4c. Job metadata bounds — coerce-or-reject, never a 500.
# ===========================================================================
class TestJobBoundsHardening:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (150, None),  # over the 0-100 bound → dropped
            (-5, None),  # under the bound → dropped
            (1000000, None),  # absurd oversize → dropped
            ("not-a-number", None),  # non-numeric → dropped
            (87, 87),  # in range → kept
            (0, 0),
            (100, 100),
        ],
    )
    def test_confidence_coerces_in_bounds(self, client, fake_supabase, raw, expected):
        body = client.post("/api/jobs", json={"jd_text": JD_TEXT, "confidence": raw}).get_json()
        assert body["confidence"] == expected

    @pytest.mark.parametrize("raw", ["2026-13-99", "yesterday", "", "2026/08/15", "31-08-2026"])
    def test_bad_deadline_drops_to_none_not_500(self, client, fake_supabase, raw):
        res = client.post("/api/jobs", json={"jd_text": JD_TEXT, "deadline": raw})
        assert res.status_code == 201, res.get_data(as_text=True)
        assert res.get_json()["deadline"] is None

    def test_valid_deadline_is_kept(self, client, fake_supabase):
        body = client.post("/api/jobs", json={"jd_text": JD_TEXT, "deadline": "2026-08-15"}).get_json()
        assert body["deadline"] == "2026-08-15"

    def test_oversize_why_fit_is_capped_not_500(self, client, fake_supabase):
        import jobs_service

        huge = "x" * 9000
        res = client.post("/api/jobs", json={"jd_text": JD_TEXT, "why_fit": huge})
        assert res.status_code == 201, res.get_data(as_text=True)
        assert len(res.get_json()["why_fit"]) <= jobs_service.MAX_WHY_FIT_LEN

    def test_garbage_status_is_400_not_500(self, client, fake_supabase):
        jid = client.post("/api/jobs", json={"jd_text": JD_TEXT}).get_json()["id"]
        res = client.post(f"/api/jobs/{jid}/status", json={"status": "ghosted"})
        assert res.status_code == 400
        assert res.status_code < 500

    def test_pile_of_garbage_metadata_does_not_500(self, client, fake_supabase):
        res = client.post(
            "/api/jobs",
            json={
                "jd_text": JD_TEXT,
                "confidence": "lots",
                "deadline": [],
                "why_fit": {"oops": True},
                "field": 12345,
            },
        )
        assert res.status_code == 201
        assert res.status_code < 500
