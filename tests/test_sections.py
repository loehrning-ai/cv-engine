"""Tests for engine/render/sections.py registry + downstream wiring."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from render import ai_extract, content, importers, sections, templates  # noqa: E402


class TestRegistry:
    def test_default_sections_in_order(self):
        keys = [s.key for s in sections.DEFAULT_SECTIONS]
        assert keys == [
            "experience", "education", "skills",
            "projects", "leadership", "others",
        ]

    def test_all_shapes_known(self):
        for s in sections.DEFAULT_SECTIONS:
            assert s.shape in sections.SHAPES, (
                f"section {s.key!r} declares shape {s.shape!r}, "
                f"which isn't in SHAPES={sections.SHAPES}"
            )

    def test_all_have_required_fields(self):
        for s in sections.DEFAULT_SECTIONS:
            assert s.required_fields, f"{s.key} has no required fields"

    def test_to_json_dict_serialises_camelcase(self):
        out = sections.to_json_dict()
        assert all("requiredFields" in s for s in out)
        assert all("eyebrow" in s for s in out)
        assert all("fields" in s for s in out)
        # All entries from the registry are present
        assert {s["key"] for s in out} == {s.key for s in sections.DEFAULT_SECTIONS}
        exp = next(s for s in out if s["key"] == "experience")
        role = next(f for f in exp["fields"] if f["name"] == "role")
        assert role["label"] == "Role"
        assert role["type"] == "text"
        assert role["required"] is True
        compact = next(s for s in out if s["key"] == "projects")
        assert [f["name"] for f in compact["fields"]] == ["title", "date", "desc"]

    def test_by_key_returns_section_or_none(self):
        assert sections.by_key("experience").key == "experience"
        assert sections.by_key("nonexistent") is None


class TestContentDerivedFromRegistry:
    def test_section_keys_matches_registry(self):
        assert content.SECTION_KEYS == sections.section_keys()

    def test_section_outline_includes_header_and_all_registered(self):
        outline = content.section_outline({})
        slugs = [o["slug"] for o in outline]
        assert slugs[0] == "header"
        for s in sections.DEFAULT_SECTIONS:
            assert s.key in slugs


class TestTemplatesShapeDispatch:
    def test_renders_all_shapes_without_error(self):
        # Build a dummy CV that exercises every shape.
        cv = {
            "name": "Test User",
            "accent": "#000000",
            "font": "serif",
            "contact": [{"label": "x", "href": "mailto:x@example.com", "icon_svg": ""}],
            "experience": [{
                "role": "Engineer", "company": "Acme", "location": "Earth",
                "start": "2020", "end": "Present", "bullets": ["Did things."],
            }],
            "education": [{
                "degree": "BSc", "school": "MIT", "location": "MA",
                "start": "2015", "end": "2019",
            }],
            "skills": [{"label": "Languages", "items": "Python"}],
            "projects": [{"title": "X", "date": "2023", "desc": "y"}],
            "leadership": [{"title": "Y", "date": "2022", "desc": "z"}],
            "others": [{"title": "Z", "date": "2021", "desc": "w"}],
        }
        body = templates.render_body(cv)
        # Every section's id="sec-..." should appear.
        for s in sections.DEFAULT_SECTIONS:
            assert f'id="sec-{s.key}"' in body, f"{s.key} not in render_body output"

    def test_section_order_controls_render_order(self):
        cv = {
            "name": "Test User",
            "contact": [{"label": "x"}],
            "section_order": ["skills", "experience", "education"],
            "experience": [{
                "role": "Engineer", "company": "Acme",
                "start": "2020", "end": "Present",
            }],
            "education": [{
                "degree": "BSc", "school": "MIT",
                "start": "2015", "end": "2019",
            }],
            "skills": [{"label": "Languages", "items": "Python"}],
        }
        body = templates.render_body(cv)
        assert body.index('id="sec-skills"') < body.index('id="sec-experience"') < body.index('id="sec-education"')

    def test_header_contact_and_photo_layout_classes_render(self):
        cv = {
            "name": "Test User",
            "contact_layout": "grid",
            "header_layout": "left",
            "photo_layout": "square",
            "photo": "design/photos/photo.png",
            "contact": [{"label": "x"}],
        }
        body = templates.render_body(cv)
        assert "header-layout-left" in body
        assert "contact-layout-grid" in body
        assert "photo-layout-square" in body

    def test_layout_config_keys_are_reserved_not_custom_sections(self):
        cv = {
            "name": "Test User",
            "contact": [{"label": "x"}],
            "section_order": ["experience"],
            "optional_sections": ["projects"],
            "contact_layout": "stack",
            "header_layout": "left",
            "photo_layout": "hidden",
            "experience": [{
                "role": "Engineer", "company": "Acme",
                "start": "2020", "end": "Present",
            }],
        }
        body = templates.render_body(cv)
        assert "sec-section_order" not in body
        assert "sec-optional_sections" not in body
        assert "sec-contact_layout" not in body


class TestAIPromptFromRegistry:
    def test_system_prompt_mentions_every_section(self):
        prompt = ai_extract.SYSTEM_PROMPT
        for s in sections.DEFAULT_SECTIONS:
            # Each section key should appear in the prompt (as a YAML key).
            assert f"{s.key}:" in prompt or f"{s.key}\n" in prompt, (
                f"section {s.key} missing from SYSTEM_PROMPT"
            )

    def test_build_system_prompt_is_idempotent(self):
        a = ai_extract.build_system_prompt()
        b = ai_extract.build_system_prompt()
        assert a == b


class TestImportersFromRegistry:
    def test_header_patterns_built_from_registry(self):
        # The dict's keys should match every section that declares one.
        with_pattern = {
            s.key for s in sections.DEFAULT_SECTIONS if s.text_header_pattern
        }
        assert set(importers._HEADER_PATTERNS.keys()) == with_pattern

    def test_from_rendercv_handles_all_shapes(self):
        # An empty rendercv input still produces all registered keys.
        out = importers.from_rendercv("cv:\n  name: Test\n")
        for s in sections.DEFAULT_SECTIONS:
            # Either present (even if empty list) or pruned by _drop_empty
            # — both are valid behaviour.
            assert s.key not in out or isinstance(out[s.key], list)


class TestCustomSectionsFromYaml:
    """The user can add a section by typing it into cv.yaml — no code edit."""

    def _base_cv(self) -> dict:
        return {
            "name": "Test User",
            "accent": "#000000",
            "font": "serif",
            "contact": [{"label": "x", "href": "mailto:x@example.com", "icon_svg": ""}],
            "experience": [{
                "role": "Engineer", "company": "Acme", "location": "Earth",
                "start": "2020", "end": "Present", "bullets": ["Did things."],
            }],
        }

    def test_detect_shape_compact(self):
        items = [{"title": "X", "date": "2024", "desc": "y"}]
        assert templates.detect_shape(items) == "compact"

    def test_detect_shape_experience(self):
        items = [{"role": "X", "company": "Y", "start": "2020", "end": "2021"}]
        assert templates.detect_shape(items) == "experience"

    def test_detect_shape_education(self):
        items = [{"degree": "X", "school": "Y", "start": "2020", "end": "2021"}]
        assert templates.detect_shape(items) == "education"

    def test_detect_shape_publication(self):
        items = [{"title": "X", "authors": "A B", "venue": "Z", "date": "2024"}]
        assert templates.detect_shape(items) == "publication"

    def test_detect_shape_falls_back_to_compact(self):
        items = [{"random_field": "value"}]
        assert templates.detect_shape(items) == "compact"

    def test_humanize_key(self):
        assert templates.humanize_key("awards") == "Awards"
        assert templates.humanize_key("speaking_engagements") == "Speaking Engagements"
        assert templates.humanize_key("press_and_media") == "Press and Media"

    def test_render_body_emits_custom_section(self):
        cv = self._base_cv()
        cv["awards"] = [
            {"title": "Best Paper", "date": "2024", "desc": "ICML"},
        ]
        body = templates.render_body(cv)
        assert 'id="sec-awards"' in body
        assert "Awards" in body
        assert "Best Paper" in body

    def test_direct_contact_and_publication_links_drop_unsafe_schemes(self):
        cv = self._base_cv()
        cv["contact"] = [{"label": "site", "href": "javascript:alert(1)", "icon_svg": ""}]
        cv["publications"] = [{"title": "Paper", "url": "data:text/html,boom"}]
        body = templates.render_body(cv)
        assert "javascript:" not in body
        assert "data:text/html" not in body
        assert '<a href="javascript:' not in body

    def test_malformed_custom_sections_render_without_crashing(self):
        cv = self._base_cv()
        cv["custom_notes"] = ["plain scalar", {"desc": "description only"}, {"authors": "A B"}]
        body = templates.render_body(cv)
        assert 'id="sec-custom_notes"' in body
        assert "plain scalar" in body
        assert "description only" in body

    def test_render_body_skips_non_list_custom_keys(self):
        cv = self._base_cv()
        cv["random_string"] = "hello"
        cv["random_int"] = 42
        body = templates.render_body(cv)
        # Neither should appear as a section
        assert 'id="sec-random_string"' not in body
        assert 'id="sec-random_int"' not in body

    def test_section_outline_includes_custom(self):
        cv = self._base_cv()
        cv["awards"] = [{"title": "X", "date": "2024"}]
        outline = content.section_outline(cv)
        slugs = [o["slug"] for o in outline]
        assert "awards" in slugs
        # Custom marker
        awards_entry = next(o for o in outline if o["slug"] == "awards")
        assert awards_entry.get("custom") is True
        assert awards_entry["label"] == "Awards"

    def test_section_outline_without_cv_returns_registered_only(self):
        outline = content.section_outline()
        slugs = [o["slug"] for o in outline]
        assert "header" in slugs
        for s in sections.DEFAULT_SECTIONS:
            assert s.key in slugs


class TestRenderingCorrectness:
    """Plan 110 Stage 4 — render-layer correctness fixes."""

    def test_empty_bullets_are_pruned(self):
        """An in-progress role saved with blank bullet rows (``bullets: ['']``)
        must not render a stray empty <li> dot in the preview/PDF."""
        html = templates._PER_ITEM_RENDERERS["experience"](
            {"role": "DE", "company": "Meta", "bullets": ["Real bullet.", "", "   ", "Another."]}
        )
        assert "<li></li>" not in html
        assert html.count("<li>") == 2  # only the two non-empty bullets

    def test_role_with_only_blank_bullets_emits_no_list(self):
        html = templates._PER_ITEM_RENDERERS["experience"](
            {"role": "DE", "company": "Meta", "bullets": ["", "  "]}
        )
        assert "<ul" not in html and "<li>" not in html

    def test_bare_doi_becomes_resolvable_doi_org_link(self):
        """A bare or ``doi:``-prefixed DOI must resolve to https://doi.org/…,
        not a broken relative link (or get dropped as an unsafe scheme)."""
        assert templates._normalize_scholarly_url("10.1234/foo.bar") == "https://doi.org/10.1234/foo.bar"
        assert templates._normalize_scholarly_url("doi:10.5/x") == "https://doi.org/10.5/x"
        # already-qualified URLs and non-DOIs are left for _safe_href to vet.
        assert templates._normalize_scholarly_url("https://doi.org/10.1/y") == "https://doi.org/10.1/y"
        assert templates._normalize_scholarly_url("") == ""

    def test_publication_renders_absolute_doi_href(self):
        html = templates._PER_ITEM_RENDERERS["publication"](
            {"title": "A Paper", "venue": "ICML", "doi": "10.1234/foo"}
        )
        assert 'href="https://doi.org/10.1234/foo"' in html
