"""Regression tests for the engine hardening fixes (e2e hardening report).

Four independent fixes, one test class each:

  FIX 1 — Stored XSS via markdown link schemes (markdown_inline.inline).
  FIX 2 — CSS injection via the accent token (css_base.build_css).
  FIX 3 — Anti-fabrication grounding bypass (cover_letter.guards).
  FIX 4 — Sub-page content overflow escaping the 1-page gate (pdf_service).

Everything here is pure/offline — no LLM, no network. The PDF test (FIX 4)
needs WeasyPrint; it is skipped if the renderer isn't importable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from cover_letter import guards  # noqa: E402
from render import css_base  # noqa: E402
from render.markdown_inline import inline  # noqa: E402


# ---------------------------------------------------------------------------
# FIX 1 — Stored XSS: markdown link scheme whitelist.
# ---------------------------------------------------------------------------
class TestMarkdownLinkSchemeWhitelist:
    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "  javascript:alert(1)",  # leading whitespace
            "java\tscript:alert(1)",  # tab-split scheme
            "java\nscript:alert(1)",  # newline-split scheme
            "data:text/html,<script>alert(1)</script>",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "blob:https://evil.example/x",
            "about:blank",
            "j&#x61;vascript:alert(1)",  # hex entity obfuscation
            "j&#97;vascript:alert(1)",  # decimal entity obfuscation
            "\x01javascript:alert(1)",  # leading control char
        ],
    )
    def test_disallowed_schemes_render_inert(self, url: str) -> None:
        out = inline(f"[click me]({url})")
        # The security property: NO live anchor and NO href attribute is ever
        # emitted for a disallowed scheme. (Some payloads contain whitespace
        # and so never match the link regex at all — they pass through as inert
        # literal text, which a browser does not execute. Either way: no sink.)
        assert "<a" not in out
        assert "href" not in out
        # The visible label is preserved (either as the rendered link text or
        # as inert literal markdown).
        assert "click me" in out

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com",
            "http://example.com/path?q=1",
            "HTTPS://Example.com",
            "mailto:someone@example.com",
            "/relative/path",
            "#anchor",
            "?query=only",
            "page.html",  # scheme-less relative
            "//cdn.example.com/x",  # protocol-relative
        ],
    )
    def test_allowed_schemes_become_anchors(self, url: str) -> None:
        out = inline(f"[label]({url})")
        assert '<a href="' in out
        assert ">label</a>" in out

    def test_label_is_always_escaped(self) -> None:
        # Disallowed scheme: dangerous label still escaped, still no anchor.
        out = inline("[<img src=x onerror=alert(1)>](javascript:alert(1))")
        assert "<a" not in out
        assert "<img" not in out
        assert "&lt;img" in out

    def test_label_escaped_on_allowed_scheme(self) -> None:
        out = inline("[<b>x</b>](https://example.com)")
        assert '<a href="https://example.com">' in out
        assert "<b>" not in out
        assert "&lt;b&gt;x&lt;/b&gt;" in out

    def test_double_quote_in_href_is_neutralized(self) -> None:
        # A quote that could break out of the href attribute must be escaped.
        out = inline('[x](https://example.com/"onmouseover="alert(1))')
        assert 'onmouseover="alert' not in out
        # the raw double-quote must not appear unescaped inside the attribute
        assert '="https://example.com/"on' not in out

    def test_plain_text_unaffected(self) -> None:
        assert inline("just text") == "just text"

    def test_bold_and_code_still_work(self) -> None:
        out = inline("**bold** and `code`")
        assert "<strong>bold</strong>" in out
        assert '<code class="inline-code">code</code>' in out


# ---------------------------------------------------------------------------
# FIX 2 — CSS injection: accent must be a strict hex color.
# ---------------------------------------------------------------------------
class TestAccentCssInjection:
    DEFAULT = "#111111"

    @pytest.mark.parametrize(
        "evil",
        [
            "#fff;} body{display:none} .x{color:#000",
            "red; } html { background: url(http://evil) ",
            "#fff} *{visibility:hidden}",
            "expression(alert(1))",
            "url(javascript:alert(1))",
            "blue",  # named color, not hex
            "#12",  # too short
            "#1234567",  # 7 digits, invalid length
            "#gggggg",  # non-hex chars
            "",  # empty
            "  #fff  ",  # surrounded by space (strict regex rejects)
        ],
    )
    def test_malicious_accent_falls_back_to_default(self, evil: str) -> None:
        css = css_base.build_css("file:///tmp", evil, font="serif")
        # The raw payload must not be interpolated anywhere in the CSS.
        if evil:
            assert evil not in css
        assert "display:none" not in css
        assert "visibility:hidden" not in css
        assert "expression(" not in css
        assert "javascript:" not in css
        # The default accent is used instead (appears in --accent and --rule).
        assert f"--accent:        {self.DEFAULT};" in css

    @pytest.mark.parametrize(
        "good",
        ["#fff", "#FFF", "#ffffff", "#1a2b3c", "#11223344", "#AABBCCDD"],
    )
    def test_valid_hex_accent_passes_through(self, good: str) -> None:
        css = css_base.build_css("file:///tmp", good, font="serif")
        assert f"--accent:        {good};" in css

    def test_default_when_called_without_accent(self) -> None:
        css = css_base.build_css("file:///tmp")
        assert f"--accent:        {self.DEFAULT};" in css


# ---------------------------------------------------------------------------
# FIX 3 — Anti-fabrication grounding: case / homoglyph / number-word evasions.
# ---------------------------------------------------------------------------
# The CV is the SOLE source of candidate facts. The JD adds the employer/role
# the candidate may legitimately name.
_CV = {
    "name": "Tim Loehr",
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
        },
        {
            "role": "Data Scientist",
            "company": "Red Bull",
            "location": "Salzburg",
            "bullets": ["Built ML models for anomaly detection."],
        },
    ],
    "skills": [{"label": "Data", "items": "Python, SQL, PyTorch, Spark"}],
}

# A clean letter — every fact traces to the CV.
_CLEAN = (
    "At Meta I cut pipeline latency by 40% across the ETL stack using Spark "
    "and Airflow.\n\nEarlier, at Red Bull, I built ML models for anomaly "
    "detection in Python. I bring SQL and PyTorch depth too."
)


class TestAntiFabricationEvasions:
    def test_clean_letter_still_passes(self) -> None:
        # Regression guard: the legitimate letter must NOT be flagged.
        assert guards.cross_reference_facts(_CLEAN, _CV) == []

    def test_capitalized_fabrication_still_caught(self) -> None:
        # Baseline: the original (capitalized) bypass remains caught.
        out = guards.cross_reference_facts(
            "At Globex Industries I worked.", _CV
        )
        assert any("globex" in o.lower() for o in out)

    def test_lowercase_noun_evasion_caught(self) -> None:
        # The NEW catch: a fabricated employer written all-lowercase.
        out = guards.cross_reference_facts(
            "at globex industries i drove growth.", _CV
        )
        assert any("globex industries" == o.lower() for o in out)

    def test_homoglyph_evasion_caught(self) -> None:
        # "Glоbex" uses a Cyrillic 'о' (U+043E) to dodge a naive comparison.
        evil = "Glоbex Industries"
        assert "о" in evil  # sanity: the homoglyph is really there
        out = guards.cross_reference_facts(f"At {evil} I worked.", _CV)
        assert any("bex" in o.lower() for o in out)

    def test_spelled_out_number_evasion_caught(self) -> None:
        # "two hundred percent" (= 200%) is NOT grounded → must be caught.
        out = guards.cross_reference_facts(
            "At Meta I drove a two hundred percent revenue increase.", _CV
        )
        assert any("two hundred percent" == o.lower() for o in out)

    def test_spelled_out_bare_number_caught(self) -> None:
        # "fifty" engineers — a spelled metric not in the CV.
        out = guards.cross_reference_facts("At Meta I led fifty engineers.", _CV)
        assert any(o.lower() == "fifty" for o in out)

    def test_grounded_spelled_number_passes(self) -> None:
        # CV says "40%"; the letter spelling it "forty percent" is GROUNDED.
        out = guards.cross_reference_facts(
            "At Meta I improved latency by forty percent.", _CV
        )
        assert out == []

    def test_grounded_digit_number_still_passes(self) -> None:
        out = guards.cross_reference_facts(
            "At Meta I cut latency by 40% in the stack.", _CV
        )
        assert out == []

    def test_run_guards_blocks_lowercase_fabrication(self) -> None:
        # End-to-end through the orchestrated guard chain.
        with pytest.raises(guards.GroundingError):
            guards.run_guards(
                "at globex industries i shipped a fifty percent gain.", _CV
            )


# ---------------------------------------------------------------------------
# FIX 4 — Sub-page content overflow must fail the 1-page gate.
# ---------------------------------------------------------------------------
weasyprint = pytest.importorskip(
    "weasyprint", reason="WeasyPrint required for the PDF overflow gate"
)

import yaml  # noqa: E402

from render import pdf_service  # noqa: E402

_EXAMPLE_YAML = (REPO / "content" / "cv.example.yaml").read_text(encoding="utf-8")


class TestSubPageOverflowGate:
    def test_example_cv_fits_one_page(self) -> None:
        # Happy path: the shipped example must render as exactly one page with
        # PDF bytes and NOT be flagged as overflowed.
        result = pdf_service.render_cv_pdf(_EXAMPLE_YAML, density="normal")
        assert result.overflowed is False
        assert result.page_count == 1
        assert result.pdf_bytes is not None

    def test_subpage_overflow_is_rejected_even_at_one_page(self) -> None:
        # A CV whose content spills below the A4 edge but does NOT push
        # WeasyPrint to a 2nd page must STILL be rejected — this is the exact
        # gap the page-count gate alone missed (clipped, silently shipped).
        cv = yaml.safe_load(_EXAMPLE_YAML)
        cv["experience"] = (cv.get("experience") or []) + [
            {
                "role": f"Extra Role {i}",
                "company": f"Co {i}",
                "location": "Berlin",
                "start": "01/2020",
                "end": "Present",
                "bullets": [
                    f"a moderately long bullet line {j} adding vertical height"
                    for j in range(3)
                ],
            }
            for i in range(3)
        ]
        result = pdf_service.render_cv_pdf(
            yaml.safe_dump(cv, allow_unicode=True), density="normal"
        )
        assert result.overflowed is True
        assert result.pdf_bytes is None
        # The defining property: flagged as overflow while still a single page
        # (proves the sub-page height check, not just the page-count gate).
        assert result.page_count == 1

    def test_large_overflow_still_caught_by_page_count(self) -> None:
        # Sanity: gross overflow still trips the original page-count gate.
        cv = yaml.safe_load(_EXAMPLE_YAML)
        cv["experience"] = (cv.get("experience") or []) + [
            {
                "role": f"Role {i}",
                "company": f"Co {i}",
                "location": "Berlin",
                "start": "01/2020",
                "end": "Present",
                "bullets": [
                    f"long bullet number {j} that wraps and consumes space"
                    for j in range(6)
                ],
            }
            for i in range(10)
        ]
        result = pdf_service.render_cv_pdf(
            yaml.safe_dump(cv, allow_unicode=True), density="normal"
        )
        assert result.overflowed is True
        assert result.page_count >= 2


# ---------------------------------------------------------------------------
# Plan 107 Stage 5 — engine hygiene guards.
# ---------------------------------------------------------------------------
class TestAccentNormalizedAtLoad:
    """F13: a malformed accent is normalized (with a stderr note) at load
    time instead of persisting silently until css_base sanitizes it."""

    def _load(self, tmp_path, accent_line: str) -> dict:
        from render import content

        src = (
            "name: Test Person\n"
            "contact:\n"
            "  - label: mail\n"
            + accent_line
            + "experience:\n"
            "  - role: Engineer\n"
            "    company: Co\n"
            "    location: Berlin\n"
            "    start: 01/2020\n"
            "    end: Present\n"
        )
        p = tmp_path / "cv.yaml"
        p.write_text(src, encoding="utf-8")
        return content.load(p)

    def test_malformed_accent_normalizes_to_default(self, tmp_path, capsys) -> None:
        cv = self._load(tmp_path, "accent: 'red; } html { background: url(http://evil)'\n")
        assert cv["accent"] == "#111111"
        assert "is not a hex colour" in capsys.readouterr().err

    def test_valid_accent_survives_load_unchanged(self, tmp_path, capsys) -> None:
        cv = self._load(tmp_path, "accent: '#5e6ad2'\n")
        assert cv["accent"] == "#5e6ad2"
        assert capsys.readouterr().err == ""


class TestRailColumnDefault:
    """F12: a 7th+ section on the sidebar template stays in the wide main
    column by default instead of being auto-placed by the grid."""

    def test_rail_sections_have_default_grid_column(self) -> None:
        css = css_base.build_css("file:///tmp", "#111111", font="sans")
        base_rule_at = css.find(".template-sidebar .layout-rail section {")
        assert base_rule_at != -1
        block = css[base_rule_at : css.find("}", base_rule_at)]
        assert "grid-column: 2;" in block


class TestNonStringFontDensityGracefulError:
    """Plan 110 Stage 5: a non-string font/density (a list/dict/number slipped
    into the YAML) must raise the FRIENDLY 'not supported' ValueError, not a raw
    AttributeError from calling .strip() on a non-string."""

    def _write(self, tmp_path, extra: str):
        src = (
            "name: Tim\n"
            "contact:\n"
            "  - label: mail\n"
            "    value: t@e.com\n"
            + extra
            + "experience:\n"
            "  - role: Engineer\n"
            "    company: Co\n"
            "    start: 2020\n"
            "    end: Present\n"
        )
        p = tmp_path / "cv.yaml"
        p.write_text(src, encoding="utf-8")
        return p

    def test_list_font_raises_valueerror_not_attributeerror(self, tmp_path):
        import pytest
        from render import content
        p = self._write(tmp_path, "font:\n  - bad\n")
        with pytest.raises(ValueError, match="not supported"):
            content.load(p)

    def test_numeric_density_raises_valueerror_not_attributeerror(self, tmp_path):
        import pytest
        from render import content
        p = self._write(tmp_path, "density: 42\n")
        with pytest.raises(ValueError, match="not supported"):
            content.load(p)
