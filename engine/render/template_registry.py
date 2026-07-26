"""Built-in resume template registry.

Templates are layout choices, separate from color/font/density themes. They are
code-owned for now: the selected id is stored on the CV YAML as ``template`` and
the renderer maps it to a stable CSS class.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateSpec:
    id: str
    label: str
    description: str
    renderer: str = "standard"
    stylesheet: str = "cv"
    thumbnail: str = ""
    default_font: str = "serif"
    default_density: str = "normal"
    use_case: str = "General applications"
    supported_sections: tuple[str, ...] = ("experience", "education", "skills", "projects", "leadership", "others")
    one_page_risk: str = "normal"
    origin: str = "built-in"
    source_url: str = ""
    commit: str = ""
    license: str = "CV Copilot bundled"
    compatibility_version: str = "1"
    layout_family: str = "single_column"
    column_count: int = 1
    inspiration_source: str = "CV Copilot built-in renderer"
    ats_priority: str = "high"
    customization_axes: tuple[str, ...] = ("accent", "font", "density", "paper", "section_order")
    paper_sizes: tuple[str, ...] = ("A4",)
    import_export_notes: str = "YAML-native; JSON Resume import/export remains a reviewed adapter boundary."


TEMPLATES: tuple[TemplateSpec, ...] = (
    TemplateSpec(
        "classic",
        "Classic",
        "Centered academic structure with compact section rules.",
        use_case="Academic and conservative applications",
        inspiration_source="Resume-Matcher classic single-column layout idea",
        import_export_notes="Canonical YAML; stable for PDF export and future JSON Resume import mapping.",
    ),
    TemplateSpec(
        "modern",
        "Modern",
        "Left-aligned header with quieter section dividers.",
        default_font="sans",
        use_case="Modern product and operations roles",
        inspiration_source="Resume-Matcher modern single-column layout idea",
        import_export_notes="Canonical YAML; metadata mirrors portable template concepts without external CSS.",
    ),
    TemplateSpec(
        "sidebar",
        "Sidebar",
        "Compact rail layout for skills-heavy CVs.",
        renderer="rail",
        default_font="sans",
        default_density="tight",
        use_case="Technical CVs with dense skills evidence",
        one_page_risk="low",
        layout_family="rail",
        column_count=2,
        ats_priority="medium",
        customization_axes=("accent", "font", "density", "paper"),
        inspiration_source="Reviewed two-column/rail pattern from resume-gallery research",
        import_export_notes="Canonical YAML; rail layout is local CSS only and not an external template import.",
    ),
    TemplateSpec(
        "executive",
        "Executive",
        "Strong name block and restrained leadership-forward spacing.",
        use_case="Leadership and advisory roles",
        one_page_risk="medium",
        inspiration_source="Manual leadership-forward variant from internal renderer",
        import_export_notes="Canonical YAML; keeps sections linear for parser compatibility.",
    ),
    TemplateSpec(
        "technical",
        "Technical",
        "Dense mono-accented structure for engineering roles.",
        default_font="sans",
        default_density="tight",
        use_case="Engineering and data roles",
        one_page_risk="low",
        layout_family="compact",
        inspiration_source="Resume-Matcher keyword-density concept implemented in local renderer",
        import_export_notes="Canonical YAML; compact metadata supports future import/export adapters.",
    ),
    TemplateSpec(
        "ats-compact",
        "ATS Compact",
        "Keyword-forward single-column layout with tight spacing.",
        default_font="sans",
        default_density="tight",
        use_case="ATS-heavy applications with dense evidence",
        one_page_risk="low",
        layout_family="compact",
        inspiration_source="Manual ATS-first compact variant from internal renderer",
        import_export_notes="Canonical YAML; no external template assets or schema.",
    ),
    TemplateSpec(
        "consulting",
        "Consulting",
        "Impact-led structure with restrained headings and readable spacing.",
        use_case="Consulting, strategy, and program roles",
        one_page_risk="normal",
        inspiration_source="Manual consulting-oriented variant from internal renderer",
        import_export_notes="Canonical YAML; keeps parser-friendly single-column flow.",
    ),
    TemplateSpec(
        "minimal",
        "Minimal",
        "Quiet single-column layout for concise applications.",
        default_font="sans",
        use_case="General applications needing low visual noise",
        one_page_risk="low",
        inspiration_source="Manual minimal variant from internal renderer",
        import_export_notes="Canonical YAML; stable local CSS only.",
    ),
)

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,48}$")
_ALLOWED_RENDERERS = {"standard", "rail"}
_ALLOWED_LAYOUT_FAMILIES = {"single_column", "two_column", "rail", "compact"}
_ALLOWED_ATS_PRIORITIES = {"high", "medium", "low"}
_ALLOWED_CUSTOMIZATION_AXES = {"accent", "font", "density", "paper", "section_order"}
_ALLOWED_PAPER_SIZES = {"A4"}


def _validate_templates(templates: tuple[TemplateSpec, ...]) -> None:
    seen: set[str] = set()
    for spec in templates:
        if not _ID_RE.match(spec.id):
            raise ValueError(f"invalid template id: {spec.id!r}")
        if spec.id in seen:
            raise ValueError(f"duplicate template id: {spec.id!r}")
        seen.add(spec.id)
        if spec.renderer not in _ALLOWED_RENDERERS:
            raise ValueError(f"invalid template renderer: {spec.renderer!r}")
        if spec.layout_family not in _ALLOWED_LAYOUT_FAMILIES:
            raise ValueError(f"invalid template layout_family: {spec.layout_family!r}")
        if spec.column_count not in (1, 2):
            raise ValueError(f"invalid template column_count: {spec.column_count!r}")
        if spec.ats_priority not in _ALLOWED_ATS_PRIORITIES:
            raise ValueError(f"invalid template ats_priority: {spec.ats_priority!r}")
        if not spec.customization_axes or set(spec.customization_axes) - _ALLOWED_CUSTOMIZATION_AXES:
            raise ValueError(f"invalid template customization_axes: {spec.customization_axes!r}")
        if not spec.paper_sizes or set(spec.paper_sizes) - _ALLOWED_PAPER_SIZES:
            raise ValueError(f"invalid template paper_sizes: {spec.paper_sizes!r}")
        if "://" in spec.inspiration_source:
            raise ValueError("template inspiration_source must be a short note, not a URL")


_validate_templates(TEMPLATES)
_BY_ID = {t.id: t for t in TEMPLATES}
DEFAULT_TEMPLATE = "classic"


def available_template_ids() -> tuple[str, ...]:
    return tuple(t.id for t in TEMPLATES)


def normalize_template(value: object) -> str:
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _BY_ID:
            return key
    return DEFAULT_TEMPLATE


def template_spec(template_id: object) -> TemplateSpec:
    return _BY_ID[normalize_template(template_id)]


def public_templates() -> list[dict[str, object]]:
    """The ``/api/templates`` payload shape.

    ``thumbnail`` is empty for every built-in: the editor renders LIVE
    preview thumbnails via ``/api/preview?...&thumb=1`` instead. The field
    exists only for the (not yet wired) reviewed external-manifest intake
    path, whose pinned assets would supply a static image (plan 107, F11).
    """
    return [
        {
            "id": t.id,
            "label": t.label,
            "description": t.description,
            "renderer": t.renderer,
            "stylesheet": t.stylesheet,
            "thumbnail": t.thumbnail,
            "default_font": t.default_font,
            "default_density": t.default_density,
            "use_case": t.use_case,
            "supported_sections": list(t.supported_sections),
            "one_page_risk": t.one_page_risk,
            "origin": t.origin,
            "source_url": t.source_url,
            "commit": t.commit,
            "license": t.license,
            "compatibility_version": t.compatibility_version,
            "layout_family": t.layout_family,
            "column_count": t.column_count,
            "inspiration_source": t.inspiration_source,
            "ats_priority": t.ats_priority,
            "customization_axes": list(t.customization_axes),
            "paper_sizes": list(t.paper_sizes),
            "import_export_notes": t.import_export_notes,
        }
        for t in TEMPLATES
    ]
