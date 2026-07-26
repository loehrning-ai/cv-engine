"""Section registry — the single source of truth for what sections exist.

# 🤖 ADD-A-SECTION-HERE
#
# To add a new section to the CV (e.g. "Publications"), append a
# SectionDef to ``DEFAULT_SECTIONS`` below. That's it — every other file
# (templates, validation, form, importers, AI extract prompt) reads from
# this registry. Pick one of the existing ``shape`` values, or add a new
# shape via ``engine/render/templates.py:RENDERERS`` and the matching
# ``tools/editor/static/form.js:SHAPE_RENDERERS``.

The registry is consumed by:
  * ``engine/render/templates.py`` — picks a renderer per shape
  * ``engine/render/content.py`` — validates required fields per section
  * ``engine/render/importers.py`` — maps rendercv keys + plain-text headers
  * ``engine/render/ai_extract.py`` — builds the Claude system prompt
  * ``tools/editor/server.py:/api/schema`` — exposes the registry to the
    frontend
  * ``tools/editor/static/form.js`` — builds form sections from the
    fetched schema

Order matters — list order = render order on the page = form order in
the editor = outline order in the sidebar.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Available render shapes. To add a new one, register it in
# ``engine/render/templates.py:RENDERERS`` (Python) AND in
# ``tools/editor/static/form.js:SHAPE_RENDERERS`` (JavaScript) so both
# the printed CV and the editor form know how to render it.
SHAPES = ("experience", "education", "skills", "compact", "publication")


@dataclass(frozen=True)
class FieldDef:
    """One item field exposed to the editor form."""

    name: str
    label: str
    type: str = "text"
    required: bool = False
    placeholder: str = ""
    rows: int = 1
    max_rows: int = 7
    hint: str = ""


@dataclass(frozen=True)
class SectionDef:
    """One section's metadata, frozen so it can be cached freely."""

    # YAML key + URL slug (lowercase, identifier-safe).
    key: str

    # Display title (rendered in the section heading on the PDF and form).
    label: str

    # Eyebrow text shown above the section heading in the editor form.
    # Keep this short and descriptive — e.g. "Where you've worked".
    eyebrow: str

    # Singular form for the "Add <singular>" button in the editor form.
    # E.g. "role" → "Add role" (for experience), "paper" → "Add paper".
    singular: str

    # Visual shape — pick from SHAPES.
    shape: str

    # Field names every item in this section must have to validate.
    # Items missing any of these raise a clear error at build time.
    required_fields: tuple[str, ...] = ()

    # Aliases the rendercv importer accepts (their schema has a few
    # synonyms — we accept all of them and normalise to ``key``).
    rendercv_aliases: tuple[str, ...] = ()

    # Regex used by the plain-text importer to detect this section's
    # heading line (case-insensitive, whole line). Empty = not detected
    # in plain-text mode.
    text_header_pattern: str = ""

    # Optional metadata for the AI extract system prompt. If set,
    # overrides the default "auto" description Claude sees for this
    # section's shape.
    ai_hint: str = ""

    # Editor field metadata for one repeated item.
    fields: tuple[FieldDef, ...] = ()


# ──────────────────────────────────────────────────────────────────────
#  DEFAULT_SECTIONS — the shipped registry.
#
#  Add / remove / reorder entries here. The frontend will pick up
#  changes automatically the next time it fetches /api/schema (Cmd+R
#  in the browser is enough — no rebuild required).
# ──────────────────────────────────────────────────────────────────────
DEFAULT_SECTIONS: tuple[SectionDef, ...] = (
    SectionDef(
        key="experience",
        label="Experience",
        eyebrow="Where you've worked",
        singular="role",
        shape="experience",
        required_fields=("role", "company", "start", "end"),
        rendercv_aliases=("experience", "work_experience", "professional_experience"),
        text_header_pattern=r"^\s*(work\s+)?(experience|employment|professional\s+experience)\s*$",
        fields=(
            FieldDef("role", "Role", required=True, placeholder="Senior Data Engineer"),
            FieldDef("company", "Company", required=True, placeholder="Acme GmbH"),
            FieldDef("location", "Location", placeholder="Berlin, Germany"),
            FieldDef("start", "Start", required=True, placeholder="2022"),
            FieldDef("end", "End", required=True, placeholder="Present"),
            FieldDef("bullets", "Bullets", type="list", placeholder="One impact per bullet"),
            FieldDef("stack", "Stack", type="textarea", rows=2, placeholder="Python, Spark, Airflow"),
        ),
    ),
    SectionDef(
        key="education",
        label="Education",
        eyebrow="Where you studied",
        singular="degree",
        shape="education",
        required_fields=("degree", "school", "start", "end"),
        rendercv_aliases=("education",),
        text_header_pattern=r"^\s*education\s*$",
        fields=(
            FieldDef("degree", "Degree", required=True, placeholder="MSc Computer Science"),
            FieldDef("school", "School", required=True, placeholder="ETH Zurich"),
            FieldDef("location", "Location", placeholder="Zurich, Switzerland"),
            FieldDef("start", "Start", required=True, placeholder="2018"),
            FieldDef("end", "End", required=True, placeholder="2020"),
            FieldDef("note", "Note", type="textarea", rows=2, placeholder="Relevant thesis, honors, coursework."),
        ),
    ),
    SectionDef(
        key="skills",
        label="Skills",
        eyebrow="The toolkit",
        singular="category",
        shape="skills",
        required_fields=("label",),
        rendercv_aliases=("skills", "technical_skills"),
        text_header_pattern=r"^\s*(technical\s+)?(skills|expertise)\s*$",
        fields=(
            FieldDef("label", "Category", required=True, placeholder="Data"),
            FieldDef("items", "Skills", type="chips", placeholder="Python, SQL, Spark"),
        ),
    ),
    SectionDef(
        key="projects",
        label="Projects",
        eyebrow="Things you built",
        singular="project",
        shape="compact",
        required_fields=("title",),
        rendercv_aliases=("projects", "personal_projects", "open_source"),
        text_header_pattern=r"^\s*(personal\s+)?projects\s*$",
        fields=(
            FieldDef("title", "Title", required=True, placeholder="OSS contribution"),
            FieldDef("date", "Date", placeholder="08/2023"),
            FieldDef("desc", "Description", type="textarea", rows=3, placeholder="Short description with concrete scope and impact."),
        ),
    ),
    SectionDef(
        key="leadership",
        label="Leadership",
        eyebrow="How you led",
        singular="entry",
        shape="compact",
        required_fields=("title",),
        rendercv_aliases=("leadership", "service", "mentoring", "volunteer"),
        text_header_pattern=r"^\s*(leadership|service|volunteering|community)\s*$",
        fields=(
            FieldDef("title", "Title", required=True, placeholder="Mentoring lead"),
            FieldDef("date", "Date", placeholder="2024"),
            FieldDef("desc", "Description", type="textarea", rows=3, placeholder="Scope, audience, and measurable result."),
        ),
    ),
    SectionDef(
        key="others",
        label="Other",
        eyebrow="Awards & extras",
        singular="entry",
        shape="compact",
        required_fields=("title",),
        rendercv_aliases=(
            "other", "others", "awards", "honors",
            "publications", "certifications", "volunteer",
        ),
        text_header_pattern=r"^\s*(awards|honors|publications|certifications|extras?|other|others|additional)\s*$",
        fields=(
            FieldDef("title", "Title", required=True, placeholder="Certification or award"),
            FieldDef("date", "Date", placeholder="2024"),
            FieldDef("desc", "Description", type="textarea", rows=3, placeholder="Issuer, venue, or context."),
        ),
    ),
)



# ──────────────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────────────
def all_sections() -> tuple[SectionDef, ...]:
    """Return the section list. (A function so callers can mock it in tests.)"""
    return DEFAULT_SECTIONS


def by_key(key: str) -> SectionDef | None:
    """Look up a section by its YAML key; returns None if absent."""
    for s in DEFAULT_SECTIONS:
        if s.key == key:
            return s
    return None


def section_keys() -> tuple[str, ...]:
    """All registered section keys, in render order."""
    return tuple(s.key for s in DEFAULT_SECTIONS)


def to_json_dict() -> list[dict[str, Any]]:
    """Serialisable shape for the /api/schema endpoint.

    The frontend uses this to build the form. Field names are camelCased
    to match JS conventions; on the Python side we keep snake_case.
    """
    return [
        {
            "key": s.key,
            "label": s.label,
            "eyebrow": s.eyebrow,
            "singular": s.singular,
            "shape": s.shape,
            "requiredFields": list(s.required_fields),
            "fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "type": f.type,
                    "required": f.required,
                    "placeholder": f.placeholder,
                    "rows": f.rows,
                    "maxRows": f.max_rows,
                    "hint": f.hint,
                }
                for f in s.fields
            ],
        }
        for s in DEFAULT_SECTIONS
    ]
