"""``lang: de``: the words the engine adds itself follow the CV's language.

Covers the label table (engine/render/labels.py), validation in
``content.load``, the HTML the renderer emits, and the real PDF build for the
two invented example CVs in ``content/examples/``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from render import content, css_base, labels, sections, template_registry, templates  # noqa: E402

EXAMPLE_EN = REPO / "content" / "examples" / "mira-kessler.en.yaml"
EXAMPLE_DE = REPO / "content" / "examples" / "mira-kessler.de.yaml"

MINIMAL = (
    "name: Test Person\n"
    "contact:\n"
    "  - label: test@example.com\n"
    "experience:\n"
    "  - role: Engineer\n"
    "    company: Beispiel GmbH\n"
    "    start: 01/2020\n"
    "    end: heute\n"
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cv.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _html(cv: dict) -> str:
    return templates.render_document(cv, css_base.build_css("file:///tmp"), density="normal")


# ── the label table ────────────────────────────────────────────────────────


class TestLabelTable:
    def test_every_language_has_every_english_key(self) -> None:
        en = labels.LABELS["en"]
        for code, table in labels.LABELS.items():
            assert set(table) == set(en), code
            assert set(table["sections"]) == set(en["sections"]), code

    def test_every_registered_section_has_a_heading_in_every_language(self) -> None:
        for code, table in labels.LABELS.items():
            for section in sections.DEFAULT_SECTIONS:
                assert table["sections"].get(section.key), (code, section.key)

    def test_english_headings_match_the_section_registry(self) -> None:
        # The default must not change a single existing CV.
        for section in sections.DEFAULT_SECTIONS:
            assert labels.LABELS["en"]["sections"][section.key] == section.label

    def test_german_words(self) -> None:
        de = labels.LABELS["de"]
        assert de["date_connector"] == "bis"
        assert de["sections"]["experience"] == "Berufserfahrung"
        assert de["sections"]["education"] == "Ausbildung"
        assert de["sections"]["skills"] == "Kenntnisse"
        assert de["sections"]["projects"] == "Projekte"

    def test_no_dash_punctuation_in_added_german_words(self) -> None:
        def strings(node):
            if isinstance(node, str):
                yield node
            else:
                for value in node.values():
                    yield from strings(value)

        for text in strings(labels.LABELS["de"]):
            assert "\u2014" not in text and "\u2013" not in text, text

    def test_table_is_read_only(self) -> None:
        with pytest.raises(TypeError):
            labels.LABELS["de"]["sections"]["skills"] = "Skills"  # type: ignore[index]


class TestNormalizeLang:
    @pytest.mark.parametrize("value", ["de", "DE", " de ", "de-DE", "de_CH"])
    def test_german_spellings(self, value: str) -> None:
        assert labels.normalize_lang(value) == "de"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_missing_means_english(self, value) -> None:
        assert labels.normalize_lang(value) == "en"

    @pytest.mark.parametrize("value", ["fr", "deu", "german", 7, ["de"], {"de": 1}])
    def test_unsupported_raises_with_the_supported_list(self, value) -> None:
        with pytest.raises(ValueError, match="not supported"):
            labels.normalize_lang(value)

    def test_renderer_side_is_lenient(self) -> None:
        assert labels.resolve_lang("fr") == "en"
        assert labels.resolve_lang(["de"]) == "en"
        assert labels.resolve_lang("de-AT") == "de"


# ── content.load ───────────────────────────────────────────────────────────


class TestLoad:
    def test_lang_is_normalised(self, tmp_path) -> None:
        cv = content.load(_write(tmp_path, "lang: DE-de\n" + MINIMAL))
        assert cv["lang"] == "de"

    def test_unsupported_lang_fails_the_build_clearly(self, tmp_path) -> None:
        with pytest.raises(ValueError) as exc:
            content.load(_write(tmp_path, "lang: fr\n" + MINIMAL))
        message = str(exc.value)
        assert message.startswith("cv.yaml: lang=")
        assert "'de'" in message and "'en'" in message

    def test_cv_without_lang_loads_unchanged(self, tmp_path) -> None:
        cv = content.load(_write(tmp_path, MINIMAL))
        assert "lang" not in cv

    def test_lang_is_not_a_custom_section(self, tmp_path) -> None:
        cv = content.load(_write(tmp_path, "lang: de\n" + MINIMAL))
        slugs = [entry["slug"] for entry in content.section_outline(cv)]
        assert "lang" not in slugs
        assert 'id="sec-lang"' not in _html(cv)


# ── rendered HTML ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def html() -> str:
    return _html(content.load(EXAMPLE_DE))


class TestGermanHtml:
    def test_document_language_and_title(self, html: str) -> None:
        assert '<html lang="de">' in html
        assert "<title>Lebenslauf Mira Kessler</title>" in html

    def test_date_ranges_read_bis(self, html: str) -> None:
        assert "04/2024 bis heute" in html
        assert "10/2021 bis 03/2024" in html
        assert " to " not in html.split("<body class=", 1)[1]

    def test_section_headings_are_german(self, html: str) -> None:
        for heading in ("Berufserfahrung", "Ausbildung", "Kenntnisse", "Projekte"):
            assert f'<h2 class="section-title">{heading}</h2>' in html
        for english in ("Experience", "Education", "Skills", "Projects"):
            assert f'<h2 class="section-title">{english}</h2>' not in html

    def test_section_ids_stay_on_the_yaml_keys(self, html: str) -> None:
        # The editor's outline jump uses these; they must not be translated.
        for key in ("experience", "education", "skills", "projects"):
            assert f'id="sec-{key}"' in html

    def test_leadership_and_others_headings(self) -> None:
        cv = {
            "name": "Test",
            "lang": "de",
            "leadership": [{"title": "Mentorin"}],
            "others": [{"title": "Zertifikat"}],
        }
        html = _html(cv)
        assert '<h2 class="section-title">Führung und Engagement</h2>' in html
        assert '<h2 class="section-title">Sonstiges</h2>' in html

    def test_publication_fallback_title(self) -> None:
        item = {"authors": "M. Kessler", "date": "2025"}
        assert "<b>Veröffentlichung</b>" in templates._PER_ITEM_RENDERERS["publication"](item, "de")
        assert "<b>Publication</b>" in templates._PER_ITEM_RENDERERS["publication"](item)

    def test_custom_sections_keep_their_own_heading(self) -> None:
        cv = {"name": "Test", "lang": "de", "ehrenamt": [{"title": "Lesepatin, Stadtbibliothek Musterstadt"}]}
        assert '<h2 class="section-title">Ehrenamt</h2>' in _html(cv)


class TestEnglishUnchanged:
    def test_default_is_english(self) -> None:
        html = _html(content.load(EXAMPLE_EN))
        assert '<html lang="en">' in html
        assert "<title>Mira Kessler — CV</title>" in html
        assert "04/2024 to Present" in html
        assert '<h2 class="section-title">Experience</h2>' in html

    def test_explicit_en_renders_identically_to_no_lang(self, tmp_path) -> None:
        without = _html(content.load(_write(tmp_path, MINIMAL)))
        with_en = _html(content.load(_write(tmp_path, "lang: en\n" + MINIMAL)))
        assert without == with_en

    def test_unknown_lang_in_an_unvalidated_model_falls_back(self) -> None:
        html = _html({"name": "Test", "lang": "xx", "experience": []})
        assert '<html lang="en">' in html

    def test_date_range_single_side(self) -> None:
        assert templates._date_range("2020", "", "de") == "2020"
        assert templates._date_range("", "heute", "de") == "heute"
        assert templates._date_range("2020", "2021", "de") == "2020 bis 2021"
        assert templates._date_range("2020", "2021") == "2020 to 2021"

    def test_date_range_still_escapes(self) -> None:
        assert templates._date_range("<b>", "x", "de") == "&lt;b&gt; bis x"


# ── real PDFs ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("example", [EXAMPLE_EN, EXAMPLE_DE], ids=["en", "de"])
def test_example_cv_is_one_page_in_every_template(example: Path) -> None:
    from render.pdf_service import render_cv_pdf

    yaml_text = example.read_text(encoding="utf-8")
    failures = []
    for template_id in template_registry.available_template_ids():
        rendered = render_cv_pdf(yaml_text, template=template_id)
        if rendered.page_count != 1 or rendered.overflowed:
            failures.append(f"{template_id}: {rendered.page_count} pages {rendered.overflow_label}")
    assert not failures, failures


def test_german_pdf_carries_the_german_words() -> None:
    weasyprint = pytest.importorskip("weasyprint")
    from render import resource_policy

    cv = content.load(EXAMPLE_DE)
    base_url = resource_policy.repo_base_url(REPO)
    html = templates.render_document(
        cv, css_base.build_css(base_url, cv["accent"], font=cv["font"]), density="normal"
    )
    doc = weasyprint.HTML(
        string=html,
        base_url=base_url,
        url_fetcher=resource_policy.make_url_fetcher(repo_root=REPO),
    ).render()
    text = []

    def walk(box):
        if hasattr(box, "text"):
            text.append(box.text)
        for child in getattr(box, "children", None) or []:
            walk(child)

    walk(doc.pages[0]._page_box)
    joined = " ".join(text)
    assert "bis" in joined
    assert "BERUFSERFAHRUNG" in joined.upper()
    assert len(doc.pages) == 1
