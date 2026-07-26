from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))


def test_template_registry_exposes_required_templates():
    from render import template_registry

    ids = template_registry.available_template_ids()
    required_ids = {"classic", "modern", "sidebar", "executive", "technical"}
    added_ids = {"ats-compact", "consulting", "minimal"}
    assert required_ids <= set(ids)
    assert added_ids <= set(ids)
    public = template_registry.public_templates()
    assert [t["id"] for t in public] == list(ids)
    assert all(t["renderer"] and t["stylesheet"] for t in public)
    assert {t["id"]: t["renderer"] for t in public}["sidebar"] == "rail"
    assert all(t["use_case"] for t in public)
    assert all(t["one_page_risk"] for t in public)
    assert all(t["origin"] == "built-in" for t in public)
    assert all(t["license"] for t in public)
    assert all(t["compatibility_version"] == "1" for t in public)
    required = {
        "layout_family",
        "column_count",
        "inspiration_source",
        "ats_priority",
        "customization_axes",
        "paper_sizes",
        "import_export_notes",
    }
    for tpl in public:
        assert required <= tpl.keys()
        assert tpl["layout_family"] in {"single_column", "two_column", "rail", "compact"}
        assert tpl["column_count"] in {1, 2}
        assert tpl["ats_priority"] in {"high", "medium", "low"}
        assert "A4" in tpl["paper_sizes"]
        assert set(tpl["customization_axes"]) <= {"accent", "font", "density", "paper", "section_order"}
        assert "://" not in tpl["inspiration_source"]
    by_id = {t["id"]: t for t in public}
    assert by_id["classic"]["renderer"] == "standard"
    assert by_id["modern"]["renderer"] == "standard"
    assert by_id["executive"]["renderer"] == "standard"
    assert by_id["technical"]["renderer"] == "standard"
    assert by_id["ats-compact"]["renderer"] == "standard"
    assert by_id["consulting"]["renderer"] == "standard"
    assert by_id["minimal"]["renderer"] == "standard"
    assert by_id["sidebar"]["renderer"] == "rail"
    assert by_id["classic"]["layout_family"] == "single_column"
    assert by_id["modern"]["layout_family"] == "single_column"
    assert by_id["sidebar"]["layout_family"] == "rail"


def test_registered_templates_have_css_variants():
    from render import css_base, template_registry

    css = css_base.build_css("file:///tmp")
    for template_id in template_registry.available_template_ids():
        if template_id == "classic":
            continue
        assert f".template-{template_id}" in css


def test_registered_templates_render_with_accent_override(tmp_path):
    from render import template_registry
    from render_preview import render_cv_html

    cv = tmp_path / "cv.yaml"
    accent = "#2266aa"
    cv.write_text(
        "name: Accent Template Test\n"
        "accent: '#111111'\n"
        "contact:\n"
        "  - label: Email\n"
        "    value: alex@example.com\n"
        "experience:\n"
        "  - role: Staff Engineer\n"
        "    company: Acme GmbH\n"
        "    start: '2021'\n"
        "    end: Present\n"
        "    bullets:\n"
        "      - Built reliable data platforms.\n"
        "education:\n"
        "  - degree: M.Sc. Computer Science\n"
        "    school: FAU Erlangen-Nuremberg\n"
        "    start: '2018'\n"
        "    end: '2020'\n"
        "skills:\n"
        "  - label: Data\n"
        "    items: Spark, Airflow\n",
        encoding="utf-8",
    )

    template_accent_hooks = {
        "classic": ".section-title {",
        "modern": ".template-modern .section-title",
        "sidebar": ".template-sidebar .page::before",
        "executive": ".template-executive .name",
        "technical": ".template-technical .section-title",
        "ats-compact": ".template-ats-compact .section-title",
        "consulting": ".template-consulting .section-title",
        "minimal": ".template-minimal .section-title",
    }
    assert set(template_accent_hooks) == set(template_registry.available_template_ids())

    for template_id in template_registry.available_template_ids():
        html = render_cv_html(cv, template_override=template_id, accent_override=accent)
        assert f'body class="template-{template_id} paper-white"' in html
        assert f'data-template="{template_id}"' in html
        assert f"--accent:        {accent};" in html
        assert f"--rule:          {accent};" in html
        assert f"--accent-rail:   {accent};" in html
        marker = template_accent_hooks[template_id]
        block = html.split(marker, 1)[1].split("}", 1)[0]
        assert "var(--accent)" in block or "var(--rule)" in block or "var(--accent-rail)" in block, template_id


def test_template_is_reserved_and_validated(tmp_path):
    from render import content

    cv = tmp_path / "cv.yaml"
    cv.write_text(
        "name: Test\n"
        "template: technical\n"
        "contact:\n"
        "  - { label: x }\n"
        "experience:\n"
        "  - role: Eng\n"
        "    company: Acme\n"
        "    start: '2020'\n"
        "    end: 'Now'\n",
        encoding="utf-8",
    )
    assert content.load(cv)["template"] == "technical"
    cv.write_text(cv.read_text(encoding="utf-8").replace("technical", "garish"), encoding="utf-8")
    with pytest.raises(ValueError, match="template='garish' not supported"):
        content.load(cv)


def test_paper_theme_is_reserved_and_validated(tmp_path):
    from render import content

    cv = tmp_path / "cv.yaml"
    cv.write_text(
        "name: Test\n"
        "paper: creme\n"
        "contact:\n"
        "  - { label: x }\n"
        "skills:\n"
        "  - label: Core\n"
        "    items: Python\n",
        encoding="utf-8",
    )
    assert content.load(cv)["paper"] == "creme"
    cv.write_text(cv.read_text(encoding="utf-8").replace("creme", "hot-pink"), encoding="utf-8")
    with pytest.raises(ValueError, match="paper='hot-pink' not supported"):
        content.load(cv)


def test_render_document_adds_template_class(tmp_path):
    from render import content, css_base, templates

    cv = tmp_path / "cv.yaml"
    cv.write_text(
        "name: Test\n"
        "template: sidebar\n"
        "contact:\n"
        "  - { label: x }\n"
        "skills:\n"
        "  - label: Core\n"
        "    items: Python\n",
        encoding="utf-8",
    )
    model = content.load(cv)
    html = templates.render_document(model, css_base.build_css("file:///tmp"), density="tight")
    assert 'body class="template-sidebar paper-white"' in html
    assert 'class="page density-tight"' in html
    assert 'data-template="sidebar"' in html
    assert 'data-renderer="rail"' in html
    assert 'class="layout-rail" data-renderer="rail"' in html
    assert 'data-paper="white"' in html
    assert "sec-template" not in html
    assert "sec-paper" not in html


def test_templates_api_and_preview_override(client, fake_supabase):
    res = client.get("/api/templates")
    assert res.status_code == 200
    data = res.get_json()
    assert data["default_template"] == "classic"
    ids = [t["id"] for t in data["templates"]]
    assert {"classic", "modern", "sidebar", "executive", "technical"} <= set(ids)
    assert {"ats-compact", "consulting", "minimal"} <= set(ids)
    for tpl in data["templates"]:
        assert set(tpl) >= {
            "id",
            "renderer",
            "layout_family",
            "column_count",
            "inspiration_source",
            "ats_priority",
            "customization_axes",
            "paper_sizes",
            "import_export_notes",
        }
        assert isinstance(tpl["customization_axes"], list)
        assert isinstance(tpl["paper_sizes"], list)

    cv = (
        "name: Test\n"
        "contact:\n"
        "  - { label: x }\n"
        "skills:\n"
        "  - label: Core\n"
        "    items: Python\n"
    )
    assert client.post("/api/cv", json={"content": cv}).status_code == 200
    preview = client.get("/api/preview?template=executive&density=tight")
    assert preview.status_code == 200
    body = preview.get_data(as_text=True)
    assert "template-executive" in body
    assert 'data-template="executive"' in body
    assert 'data-renderer="standard"' in body
    assert "layout_family" not in body
    assert "inspiration_source" not in body
    assert "import_export_notes" not in body

    invalid = client.get("/api/preview?template=reactive-resume-modern-two-column")
    invalid_body = invalid.get_data(as_text=True)
    assert invalid.status_code == 200
    assert 'data-template="classic"' in invalid_body
    assert 'data-renderer="standard"' in invalid_body

    thumbnail = client.get("/api/preview?template=sidebar&embed=1&thumb=1&accent=%23dc2626")
    thumb_body = thumbnail.get_data(as_text=True)
    assert thumbnail.status_code == 200
    assert 'data-preview-thumb="true"' in thumb_body
    assert 'data-template="sidebar"' in thumb_body
    assert "preview-runtime.js" not in thumb_body
    assert "paged.polyfill.js" not in thumb_body
    assert "scale(0.4988)" in thumb_body
    assert "--accent-rail:   #dc2626" in thumb_body


def test_template_picker_is_separate_from_theme_picker():
    html = (REPO / "tools" / "editor" / "static" / "index.html").read_text(encoding="utf-8")
    app = (REPO / "tools" / "editor" / "static" / "app.js").read_text(encoding="utf-8")
    form = (REPO / "tools" / "editor" / "static" / "form.js").read_text(encoding="utf-8")
    assert 'id="template-select"' not in html
    assert 'id="template-btn"' in html
    assert 'aria-controls="template-modal"' in html
    assert 'id="template-modal"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-labelledby="template-modal-title"' in html
    assert 'id="template-modal-title"' in html
    assert 'id="template-search"' in html
    assert 'id="template-current"' in html
    assert 'id="template-selected"' in html
    assert 'id="template-close"' in html
    assert 'id="template-gallery"' in html
    assert 'id="template-empty"' in html
    assert 'id="template-cancel"' in html
    assert 'id="template-apply"' in html
    assert "template-modal-gallery" in html
    assert 'data-i18n="templates.title"' in html
    assert 'data-i18n="templates.kicker"' in html
    assert 'data-i18n="templates.hint"' in html
    assert 'data-i18n-attr="aria-label:templates.close"' in html
    assert 'id="theme-popover"' in html
    assert html.index('id="template-btn"') < html.index('id="theme-popover"')
    assert "function renderTemplateGallery" in app
    assert "data-template-card" in app
    assert 'fetch("/api/templates")' in app
    assert "&template=${getTemplate()}" in app
    assert 'template: getTemplate()' in app
    assert '"template"' in form
    assert "tpl.layout_family" in app
    assert "tpl.column_count" in app
    assert "tpl.ats_priority" in app
    assert "tpl.inspiration_source" in app
    assert "tpl.customization_axes" in app
    assert "tpl.paper_sizes" in app
    assert "tpl.import_export_notes" in app
    assert 'tpl.renderer || "standard"' in app
    assert 'data-renderer="${escapeHtml(renderer)}"' in app
    assert 'modal.removeAttribute("hidden")' in app
    assert 'modal.setAttribute("hidden", "")' in app
    assert 'btn.setAttribute("aria-expanded", "true")' in app
    assert 'btn.setAttribute("aria-expanded", "false")' in app
    assert "stagedTemplate" in app
    assert "applyBtn" in app
    assert "cancelBtn" in app
    assert "searchInput" in app
    assert "filterTemplates" in app
    assert "trapPopoverTab(event, card)" in app
    assert 'event.key === "Escape"' in app
    assert "cv:appearance-changed" in app
    assert 'role="listitem"' not in app
    assert 'aria-label="${escapeHtml(aria)}"' in app
    assert "templates.ats_priority" in app
    assert 'tr("templates.ats_chip", "ATS {value}", { value: atsLabel })' in app
    assert 'tr("templates.columns", "{n} col", { n: String(columns) })' in app
    assert "template-card-chips" in app


def test_template_ids_are_aligned_across_static_fallbacks():
    from render import template_registry

    ids = template_registry.available_template_ids()
    index_html = (REPO / "tools" / "editor" / "static" / "index.html").read_text(encoding="utf-8")
    account_html = (REPO / "tools" / "editor" / "static" / "account.html").read_text(encoding="utf-8")
    account_js = (REPO / "tools" / "editor" / "static" / "account.js").read_text(encoding="utf-8")
    app_js = (REPO / "tools" / "editor" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="template-gallery"' in index_html
    assert 'id="template-select"' not in index_html
    for template_id in ids:
        assert f'<option value="{template_id}"' in account_html
        assert f'"{template_id}"' in app_js
    expected_js = '["' + '", "'.join(ids) + '"]'
    assert f"var DEFAULT_TEMPLATES = {expected_js};" in account_js
    assert f"const TEMPLATE_IDS = {expected_js};" in app_js
    assert "TEMPLATE_IDS.includes(tpl)" in app_js


def _with_template(yaml_text: str, template_id: str) -> str:
    lines = []
    replaced = False
    for line in yaml_text.splitlines():
        if line.startswith("template:"):
            lines.append(f"template: {template_id}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(f"template: {template_id}")
    return "\n".join(lines) + "\n"


def test_all_templates_preserve_one_page_gate_on_demo_cv():
    from render import template_registry
    from render.pdf_service import render_cv_pdf

    demo_cv = (REPO / "content" / "cv.example.yaml").read_text(encoding="utf-8")
    for template_id in template_registry.available_template_ids():
        rendered = render_cv_pdf(_with_template(demo_cv, template_id), density="tight")
        assert rendered.overflowed is False, f"{template_id}: {rendered.overflow_label}"
        assert rendered.page_count == 1, template_id


def test_all_templates_preserve_one_page_gate_on_dense_cv():
    from render import template_registry
    from render.pdf_service import render_cv_pdf

    dense_cv = (
        "name: Template Density Gate\n"
        "contact:\n"
        "  - { label: density@example.com }\n"
        "  - { label: Berlin, Germany }\n"
        "experience:\n"
        "  - role: Staff Data Platform Engineer\n"
        "    company: Acme Systems\n"
        "    location: Berlin, Germany\n"
        "    start: '2023'\n"
        "    end: Now\n"
        "    bullets:\n"
        "      - Built shared orchestration, observability, and warehouse controls for three product teams.\n"
        "      - Reduced daily failure triage by 42% by replacing notebook handoffs with typed deployment checks.\n"
        "    stack: Python, Postgres, Spark, Airflow, dbt, Docker\n"
        "  - role: Senior Analytics Engineer\n"
        "    company: Beta Data\n"
        "    location: Warsaw, Poland\n"
        "    start: '2020'\n"
        "    end: '2023'\n"
        "    bullets:\n"
        "      - Owned forecasting marts and executive dashboards used for weekly capacity planning.\n"
        "      - Migrated brittle SQL jobs into tested models with lineage, alerts, and release notes.\n"
        "    stack: SQL, Snowflake, dbt, Python, Looker\n"
        "education:\n"
        "  - degree: M.Sc. Computer Science\n"
        "    school: Technical University\n"
        "    location: Berlin, Germany\n"
        "    start: '2018'\n"
        "    end: '2020'\n"
        "skills:\n"
        "  - label: Data\n"
        "    items: Python, SQL, Spark, dbt, Airflow, Snowflake, Postgres\n"
        "  - label: Product\n"
        "    items: Experimentation, metrics design, reliability, stakeholder reviews\n"
        "projects:\n"
        "  - title: Internal Platform Migration\n"
        "    date: '2025'\n"
        "    desc: Rebuilt batch deployment flow with dry-run validation, owner routing, and rollback notes.\n"
        "leadership:\n"
        "  - title: Engineering Mentor\n"
        "    date: '2024'\n"
        "    desc: Mentored six engineers on analytics engineering, incident review, and system design.\n"
    )
    for template_id in template_registry.available_template_ids():
        rendered = render_cv_pdf(_with_template(dense_cv, template_id), density="tight")
        assert rendered.overflowed is False, template_id
        assert rendered.page_count == 1, template_id
