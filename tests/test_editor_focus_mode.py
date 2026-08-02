from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_focus_mode_controls_and_progress_are_structural_dom():
    html = _read("tools/editor/static/index.html")

    for contract in (
        'id="form-mode-toggle"',
        'data-form-mode="focus"',
        'data-form-mode="overview"',
        'id="focus-section-nav"',
        'id="focus-section-prev"',
        'id="focus-section-next"',
        'id="focus-section-progress"',
        'role="progressbar"',
    ):
        assert contract in html


def test_focus_mode_defaults_and_persists_per_cv_section():
    app = _read("tools/editor/static/app.js")

    assert 'const FORM_MODE_KEY = "cv.editor.formMode";' in app
    assert 'const ACTIVE_SECTION_KEY = "cv.editor.activeSection";' in app
    assert 'let formMode = localStorage.getItem(FORM_MODE_KEY) || "focus";' in app
    assert '`${ACTIVE_SECTION_KEY}::${activeCvId || "default"}`' in app
    assert 'section.toggleAttribute("hidden", formMode === "focus" && !selected);' in app
    assert 'activateFormSection(it.renderSlug, { scrollPreview: false });' in app
    assert 'localStorage.setItem(FORM_MODE_KEY, formMode);' in app


def test_overview_keeps_existing_sections_and_focus_reapplies_after_rebuild():
    app = _read("tools/editor/static/app.js")
    form = _read("tools/editor/static/form.js")

    assert 'section.toggleAttribute("hidden", formMode === "focus" && !selected);' in app
    assert 'if (formMode === "overview") applyCollapsedState();' in app
    assert 'body.removeAttribute("hidden");' in app
    assert 'window.addEventListener("cv:form-rebuilt", () => requestAnimationFrame(applyFormMode));' in app
    assert 'window.dispatchEvent(new CustomEvent("cv:form-rebuilt"));' in form


def test_editor_display_hierarchy_uses_local_source_serif_assets():
    css = _read("tools/editor/static/style.css")

    assert "url('/asset/design/fonts/SourceSerif4-Regular.ttf')" in css
    assert "url('/asset/design/fonts/SourceSerif4-Semibold.ttf')" in css
    assert '--font-display: "Source Serif 4"' in css
    assert 'body[data-form-mode="focus"] .form-section[data-focus-active="true"]' in css
