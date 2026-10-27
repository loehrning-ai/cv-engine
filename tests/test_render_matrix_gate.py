"""Plan 108 stage 5 — the full render-matrix 1-page proof.

Prior gates proved every template at density=tight only. This pins the
COMPLETE template x density matrix on the example CV so a typography or
spacing change that silently pushes any combination over the 1-page
boundary fails loudly.

``sidebar x airy`` used to report a sub-page overflow flag (page_count
still 1). The two-column rail fixed it, so every combination is now held to
fully clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

DENSITIES = ("tight", "normal", "airy")

# (template, density) combos allowed a sub-page overflow flag. Empty: every
# combination must be fully clean.
KNOWN_OVERFLOW_FLAGS: set[tuple[str, str]] = set()


def _with_template(yaml_text: str, template_id: str) -> str:
    lines, replaced = [], False
    for line in yaml_text.splitlines():
        if line.startswith("template:"):
            lines.append(f"template: {template_id}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(f"template: {template_id}")
    return "\n".join(lines) + "\n"


def _template_ids() -> list[str]:
    from render import template_registry

    return list(template_registry.available_template_ids())


@pytest.mark.parametrize("density", DENSITIES)
def test_full_template_density_matrix_stays_one_page(density: str) -> None:
    from render.pdf_service import render_cv_pdf

    demo = (REPO / "content" / "cv.example.yaml").read_text(encoding="utf-8")
    failures = []
    for template_id in _template_ids():
        rendered = render_cv_pdf(_with_template(demo, template_id), density=density)
        if rendered.page_count != 1:
            failures.append(f"{template_id}/{density}: {rendered.page_count} pages")
            continue
        if rendered.overflowed and (template_id, density) not in KNOWN_OVERFLOW_FLAGS:
            failures.append(
                f"{template_id}/{density}: overflow flag ({rendered.overflow_label})"
            )
    assert not failures, failures


def test_paper_themes_render_and_keep_one_page() -> None:
    """Papers change only the background — prove they neither break the
    render nor the page gate, in a spot pair (white + a non-white theme)."""
    from render.css_base import PAPER_THEMES
    from render.pdf_service import render_cv_pdf

    demo = (REPO / "content" / "cv.example.yaml").read_text(encoding="utf-8")
    non_white = next(p for p in PAPER_THEMES if p != "white")
    for paper in ("white", non_white):
        yaml_text = demo + f"paper: {paper}\n"
        rendered = render_cv_pdf(yaml_text, density="normal")
        assert rendered.page_count == 1, paper
        assert rendered.overflowed is False, paper


def test_print_css_carries_stage5_typography_craft() -> None:
    from render.css_base import build_css

    css = build_css("file:///tmp", accent="#111111", font="serif")
    assert "font-kerning: normal" in css
    assert "font-variant-ligatures: common-ligatures" in css
    # Digit-grid alignment on BOTH date carriers (head + sub lines).
    assert css.count("font-variant-numeric: tabular-nums") >= 3
