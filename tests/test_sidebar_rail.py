"""Sidebar template at real content length.

The rail used to place each section in its own grid row, alternating columns
by position. With a real CV that produced a staircase: education and skills
squeezed into a 45 mm column below the experience block, with school names,
places and dates wrapping word by word. The rail is now two real columns
(``rail-main`` and ``rail-side``) and sections are assigned by YAML key.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from render import content, css_base, resource_policy, templates  # noqa: E402

EXAMPLES = [
    REPO / "content" / "examples" / "mira-kessler.en.yaml",
    REPO / "content" / "examples" / "mira-kessler.de.yaml",
    REPO / "content" / "cv.example.yaml",
]
MM = 96 / 25.4  # CSS px per mm


def _sidebar(cv: dict) -> dict:
    return dict(cv, template="sidebar")


def _body(cv: dict) -> str:
    return templates.render_body(_sidebar(cv), "normal", renderer="rail")


def _section_ids(html_fragment: str) -> list[str]:
    import re

    return re.findall(r'<section id="sec-([^"]+)"', html_fragment)


def _columns(body: str) -> tuple[str, str]:
    rail = body.split('<div class="layout-rail" data-renderer="rail">', 1)[1]
    main = rail.split('<div class="rail-main">', 1)[1].split('<div class="rail-side">', 1)[0] if 'class="rail-main"' in rail else ""
    side = rail.split('<div class="rail-side">', 1)[1] if 'class="rail-side"' in rail else ""
    return main, side


class TestRailMarkup:
    def test_sections_are_assigned_by_key(self) -> None:
        cv = content.load(EXAMPLES[2])
        main, side = _columns(_body(cv))
        assert _section_ids(side) == ["education", "skills"]
        assert _section_ids(main) == ["experience", "projects", "leadership", "others"]

    def test_main_column_comes_first_in_reading_order(self) -> None:
        body = _body(content.load(EXAMPLES[0]))
        assert body.index('class="rail-main"') < body.index('class="rail-side"')
        assert body.index('id="sec-experience"') < body.index('id="sec-education"')

    def test_missing_section_does_not_shift_others_between_columns(self) -> None:
        cv = content.load(EXAMPLES[2])
        cv.pop("education")
        cv.pop("leadership")
        main, side = _columns(_body(cv))
        assert _section_ids(side) == ["skills"]
        assert _section_ids(main) == ["experience", "projects", "others"]

    def test_custom_sections_go_to_the_main_column(self) -> None:
        cv = dict(content.load(EXAMPLES[0]), awards=[{"title": "Best notebook", "date": "2025"}])
        main, side = _columns(_body(cv))
        assert "awards" in _section_ids(main)
        assert "awards" not in _section_ids(side)

    def test_empty_column_is_left_out(self) -> None:
        only_main = {"name": "T", "experience": [{"role": "R", "company": "C", "start": "1", "end": "2"}]}
        body = _body(only_main)
        assert 'class="rail-main"' in body and 'class="rail-side"' not in body
        only_side = {"name": "T", "skills": [{"label": "Data", "items": "SQL"}]}
        body = _body(only_side)
        assert 'class="rail-side"' in body and 'class="rail-main"' not in body

    def test_standard_renderer_is_untouched(self) -> None:
        body = templates.render_body(content.load(EXAMPLES[0]), "normal", renderer="standard")
        assert "rail-main" not in body and "rail-side" not in body


@pytest.fixture(scope="module")
def css() -> str:
    return css_base.build_css("file:///tmp", "#B73A15", font="sans")


class TestRailCss:
    def test_old_positional_rules_are_gone(self, css: str) -> None:
        assert ".layout-rail section:nth-child" not in css

    def test_columns_are_placed_explicitly(self, css: str) -> None:
        main = css.split(".template-sidebar .rail-main {", 1)[1].split("}", 1)[0]
        side = css.split(".template-sidebar .rail-side {", 1)[1].split("}", 1)[0]
        assert "grid-column: 2;" in main and "grid-row: 1;" in main
        assert "grid-column: 1;" in side and "grid-row: 1;" in side

    def test_accent_bar_covers_the_sheet(self, css: str) -> None:
        bar = css.split(".template-sidebar .page::before {", 1)[1].split("}", 1)[0]
        assert "height: var(--page-h);" in bar


def _render(cv: dict):
    weasyprint = pytest.importorskip("weasyprint")
    base_url = resource_policy.repo_base_url(REPO)
    css = css_base.build_css(base_url, cv.get("accent", "#111111"), font=cv.get("font", "sans"))
    html = templates.render_document(_sidebar(cv), css, density=cv.get("density", "normal"))
    return weasyprint.HTML(
        string=html,
        base_url=base_url,
        url_fetcher=resource_policy.make_url_fetcher(repo_root=REPO),
    ).render()


def _boxes_with_class(box, cls: str, found=None):
    found = [] if found is None else found
    element = getattr(box, "element", None)
    if element is not None and cls in (element.get("class") or "").split() and getattr(box, "element_tag", "") == "div":
        if not any(f.element is element for f in found):
            found.append(box)
    for child in getattr(box, "children", None) or []:
        _boxes_with_class(child, cls, found)
    return found


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.stem)
def test_columns_sit_side_by_side_at_real_width(example: Path) -> None:
    doc = _render(content.load(example))
    assert len(doc.pages) == 1
    page = doc.pages[0]._page_box
    [main] = _boxes_with_class(page, "rail-main")
    [side] = _boxes_with_class(page, "rail-side")
    # Both columns start on the same line, side on the left, main on the right.
    assert abs(main.position_y - side.position_y) < 1
    assert side.position_x < main.position_x
    assert side.width >= 49 * MM
    assert main.width >= 120 * MM


def test_accent_bar_reaches_the_bottom_of_the_sheet() -> None:
    doc = _render(content.load(EXAMPLES[0]))
    page = doc.pages[0]._page_box
    sheet_height = page.height

    def deepest_bar(box, best=0.0):
        if str(getattr(box, "element_tag", "")).endswith("::before") and box.width < 4 * MM:
            best = max(best, box.position_y + box.height)
        for child in getattr(box, "children", None) or []:
            best = deepest_bar(child, best)
        return best

    assert deepest_bar(page) >= sheet_height - 1


def test_overflow_gate_still_fires_on_sidebar() -> None:
    """The full-height accent bar must not mask a real overflow."""
    import copy

    import yaml
    from render.pdf_service import render_cv_pdf

    raw = yaml.safe_load(EXAMPLES[2].read_text(encoding="utf-8"))
    raw["experience"] = [copy.deepcopy(item) for item in raw["experience"] * 4]
    rendered = render_cv_pdf(yaml.safe_dump(raw, allow_unicode=True), template="sidebar")
    assert rendered.page_count > 1 or rendered.overflowed
