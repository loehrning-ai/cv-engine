"""Focused static guards for rendering/editor stability hardening."""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "tools" / "editor" / "static"


def _media_block(css: str, marker: str) -> str:
    start = css.index(marker)
    depth = 0
    for i in range(start, len(css)):
        ch = css[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return css[start : i + 1]
    raise AssertionError(f"unterminated media block: {marker}")


def test_narrow_stack_css_contract_present():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "@media (max-width: 920px)" in css
    narrow = _media_block(css, "@media (max-width: 920px)")
    assert "#panes.is-stacked" in narrow
    panes_block = css.split("#panes.is-stacked {", 1)[1].split("}", 1)[0]
    stack_pane_block = css.split("#panes.is-stacked #editor-pane,", 1)[1].split("}", 1)[0]
    assert "flex-direction: column" in panes_block
    assert "#panes.is-stacked #resizer { display: none; }" in css
    assert "#preview-pane" in stack_pane_block
    assert "min-width: 0" in stack_pane_block
    assert "width: 100%" in stack_pane_block


def test_resizer_js_has_narrow_noninteractive_branch():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    block = app.split("function setupResizer()", 1)[1].split("function setupSidebarToggle", 1)[0]
    assert 'matchMedia("(max-width: 920px)")' not in block
    assert "const stack = total <= Math.max(stackMin, editorMin + previewMin + sepW);" in block
    assert "aria-hidden" in block
    assert "tabIndex = -1" in block


def test_single_paged_preview_sizes_to_measured_content_without_iframe_canvas():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    block = app.split("function applyZoom()", 1)[1].split("function previewAnchorRatio", 1)[0]
    assert 'const pagedCount = previewMode === "paged" ? Math.max(1, lastPagedCount || 1) : 1;' in block
    assert "const naturalPagedH = Math.max(cvContentHeight || pageH, pageH * pagedCount);" in block
    assert 'const contentW = previewMode === "paged"' in block


def test_preview_paper_frame_is_parent_owned():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    zoom_block = app.split("function applyZoom()", 1)[1].split("function previewAnchorRatio", 1)[0]

    assert 'id="preview-paper-frame"' in html
    assert "#preview-paper-frame" in css
    assert "pointer-events: none" in css
    assert "box-shadow:" in css.split("#preview-paper-frame", 1)[1].split("}", 1)[0]
    assert 'const frame = $("#preview-paper-frame");' in zoom_block
    assert "frame.style.left = `${left}px`;" in zoom_block
    assert "frame.style.width = `${visualW}px`;" in zoom_block
    assert "iframe.style.padding" not in zoom_block


def test_editor_and_preview_panes_are_container_query_boundaries():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    editor_block = css.split("#editor-pane {", 1)[1].split("}", 1)[0]
    preview_block = css.split("#preview-pane {", 1)[1].split("}", 1)[0]

    assert "container-type: inline-size" in editor_block
    assert "container-name: editor-pane" in editor_block
    assert "container-type: inline-size" in preview_block
    assert "container-name: preview-pane" in preview_block
    assert "@container editor-pane (max-width: 520px)" in css
    assert "@container preview-pane (max-width: 480px)" in css
    assert ".form-section-metrics" in css.split("@container editor-pane (max-width: 520px)", 1)[1]
    assert "#preview-header" in css.split("@container preview-pane (max-width: 480px)", 1)[1]


def test_preview_fit_is_height_aware_with_single_dotted_canvas():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    zoom_block = app.split("function effectiveZoom()", 1)[1].split("function applyZoom()", 1)[0]
    assert "const fitW =" in zoom_block
    assert "const fitH =" in zoom_block
    assert "return clampFitZoom(Math.min(fitW, fitH));" in zoom_block
    assert "const FIT_ZOOM_MIN = 0.05" in app
    assert "const PREVIEW_PAD_PX = 18" in app
    assert "radial-gradient(circle at 1px 1px" in css
    assert "scrollbar-gutter" not in css


def test_app_shell_does_not_force_mobile_preview_clipping():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    app_block = css.split("#app {", 1)[1].split("}", 1)[0]
    assert "height: 100dvh" in app_block
    assert "width: 100%" in app_block
    pane_block = css.split("#panes.is-stacked #editor-pane,", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 0" in pane_block
    assert "min-width: 0" in pane_block
    assert "min-height: 260px" not in pane_block
    mobile = _media_block(css, "@container workspace (max-width: 600px)")
    assert "#cmdk-trigger" in mobile
    assert "#theme-btn" not in mobile


def test_form_chrome_removes_section_ordinals_and_switcher_wrap():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    form_js = (STATIC / "form.js").read_text(encoding="utf-8")
    sections_py = (REPO / "engine" / "render" / "sections.py").read_text(encoding="utf-8")
    switcher_block = css.split("#cv-switcher-label {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in switcher_block
    assert "overflow: hidden" in switcher_block
    assert "text-overflow: clip" in switcher_block
    mobile_switcher_block = css.split("@container workspace (max-width: 600px)", 1)[1].split("#cv-switcher-label {", 1)[1].split("}", 1)[0]
    assert "overflow: hidden" in mobile_switcher_block
    assert "text-overflow: ellipsis" in mobile_switcher_block
    assert "Section 01" not in form_js
    assert "Section 01b" not in form_js
    assert "Section 02" not in form_js
    assert "Section 02" not in sections_py
    assert "Where you've worked" in sections_py


def test_editor_topbar_preserves_switcher_and_compacts_status_chrome():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    topbar_block = css.split("#topbar {", 1)[1].split("}", 1)[0]
    crumbs_block = css.split(".crumbs {", 1)[1].split("}", 1)[0]
    right_block = css.split(".topbar-right {", 1)[1].split("}", 1)[0]
    switcher_block = css.split(".cv-switcher {", 1)[1].split("}", 1)[0]
    compact_block = _media_block(css, "@container workspace (max-width: 1380px)")
    narrow = _media_block(css, "@container workspace (max-width: 600px)")

    assert "overflow: hidden" in topbar_block
    assert "flex: 0 0 156px" in crumbs_block
    assert "max-width: 156px" in crumbs_block
    assert "width: 100%" in switcher_block
    assert "flex: 1 1 auto" in right_block
    assert "justify-content: flex-end" in right_block
    assert "overflow: hidden" in right_block
    assert "topbar-cluster-workflow" in html
    assert 'id="workflow-btn"' in html
    workflow_markup = html.split('<div class="topbar-cluster topbar-cluster-workflow">', 1)[1].split("</div>", 1)[0]
    assert 'id="theme-btn"' not in workflow_markup
    assert "function setupWorkflowMenu()" in app
    assert "setupWorkflowMenu();" in app
    assert "function setupAtsPopover()" in app
    assert "setupAtsPopover();" in app

    assert "#last-saved" in compact_block
    assert "display: none !important" in compact_block
    assert "#status," not in compact_block
    assert "#refresh-btn," not in compact_block

    assert "#build-btn," in narrow
    assert "#save-btn" in narrow
    assert "min-width: 30px" in narrow
    assert "#build-btn span:not([data-icon])," in narrow
    assert "#save-btn span:not([data-icon])" in narrow
    assert "#theme-btn" not in narrow
    assert "#jobs-btn" not in narrow
    assert "#cover-letters-btn" not in narrow
    assert "#tailor-btn" not in narrow


def test_editor_popovers_tooltips_and_sidebar_are_viewport_bound():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    cv_block = app.split("function setupCvSwitcher()", 1)[1].split("function downloadActiveYaml", 1)[0]
    tooltip_block = app.split("function setupTooltips()", 1)[1].split("document.addEventListener(\"mouseover\"", 1)[0]
    sidebar_block = app.split("function setupSidebarToggle()", 1)[1].split("function setupTooltips()", 1)[0]

    assert 'aria-controls="sidebar"' in html
    assert "function canRenderSidebar()" in sidebar_block
    assert "btn.disabled = !canRender" in sidebar_block
    assert 'class="modal cv-catalog-modal"' in html
    assert 'aria-modal="true"' in html
    assert "setCatalogModalInert(true)" in cv_block
    assert "setCatalogModalInert(false)" in cv_block
    assert "window.addEventListener(\"cv:layout-changed\"" in app
    assert "positionAnchoredPopover(anchor, docPop" in cv_block
    assert "const protectedMin = opts.minLeft == null" in app
    assert "const left = clamp(" in tooltip_block
    assert "window.innerWidth - width - margin" in tooltip_block
    assert "window.innerHeight - height - margin" in tooltip_block
    tooltip_css = css.split(".tooltip {", 1)[1].split(".tooltip.is-visible", 1)[0]
    assert "z-index: 260" in tooltip_css
    assert "white-space: normal" in tooltip_css
    assert "overflow-wrap: anywhere" in tooltip_css
    cv_card_css = css.split(".cv-catalog-card {", 1)[1].split("}", 1)[0]
    assert "width: min(920px, calc(100vw - 32px))" in cv_card_css
    assert "max-height: min(760px, calc(100vh - 48px))" in cv_card_css
    cv_list_css = css.split(".cv-catalog-list {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto" in cv_list_css
    assert "flex: 1 1 auto" in cv_list_css
    assert ".topbar-cluster-document #doc-actions-btn" in css
    assert ".topbar-cluster-document #theme-btn" in css


def test_ats_profile_comparison_and_tailor_profile_ui_are_visible_contracts():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    tailor_html = (STATIC / "tailor.html").read_text(encoding="utf-8")
    tailor_js = (STATIC / "tailor.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    tailor_css = (STATIC / "tailor.css").read_text(encoding="utf-8")

    assert "function atsCompareProfiles()" in app
    assert "compare_profiles: atsCompareProfiles()" in app
    assert "ats-profile-compare" in app
    assert "Simulations, not vendor-backed" in app
    assert "ats-system-details" in app
    assert "Selected score-system details" in app
    assert "profile.best_for" in app
    assert "profile.method" in app
    assert "profile.disclaimer" in app
    assert "Must-have" in app
    assert "Keyword fallback" in app
    assert ".ats-profile-row" in css
    assert ".ats-system-details" in css
    assert ".ats-detail-disclaimer" in css
    assert 'id="tl-profile"' in tailor_html
    assert 'id="tl-profile-comparison"' in tailor_html
    assert "function selectedProfile()" in tailor_js
    assert "compare_profiles: compareProfiles(profile)" in tailor_js
    assert "function renderProfileComparison" in tailor_js
    assert ".tl-profile-row" in tailor_css


def test_pdf_build_uses_reserved_window_and_download_fallback():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    cover = (STATIC / "cover-letters.js").read_text(encoding="utf-8")
    jobs = (STATIC / "jobs.js").read_text(encoding="utf-8")
    jobs_css = (STATIC / "jobs.css").read_text(encoding="utf-8")
    tailor_html = (STATIC / "tailor.html").read_text(encoding="utf-8")
    tailor_js = (STATIC / "tailor.js").read_text(encoding="utf-8")
    build_block = app.split("function setupBuildButton()", 1)[1].split("function closeReservedWindow", 1)[0]
    assert 'window.open("about:blank"' in build_block
    assert "pdfTab.opener = null" in build_block
    assert "closeReservedWindow(pdfTab)" in build_block
    assert "openPdfTarget(data.url, \"cv.pdf\", pdfTab)" in app
    assert "a.download = filename || \"document.pdf\"" in app
    assert "var pdfTab = window.open(\"about:blank\", \"_blank\")" in cover
    dirty_download_block = cover.split("async function downloadPdf()", 1)[1].split("// Open a built PDF", 1)[0]
    assert dirty_download_block.index("var pdfTab = reservePdfWindow()") < dirty_download_block.index("await saveEdits()")
    assert "closeReservedWindow(pdfTab)" in dirty_download_block
    assert 'openPdf("/api/cover-letter/" + encodeURIComponent(letterId) + "/build", null, { token: token, letterId: letterId }, pdfTab)' in dirty_download_block
    assert "function closeReservedWindow(win)" in cover
    assert "function openPdfTarget(url, filename, reservedWindow)" in cover
    assert "reservedWindow.location.href = url" in cover
    assert "a.download = filename || \"document.pdf\"" in cover
    assert "function buildLinkedPdf" in jobs
    assert "body: JSON.stringify({ id: id })" in jobs
    assert "/api/cover-letter/\" + encodeURIComponent(id) + \"/build" in jobs
    assert ".jb-linked-pdf" in jobs_css
    assert 'id="tl-build-tailored"' in tailor_html
    assert "function buildTailoredPdf()" in tailor_js
    assert "body: JSON.stringify({ id: cvId })" in tailor_js
    assert "openPdfTarget(data.url, \"tailored-cv.pdf\", pdfTab)" in tailor_js


def test_theme_picker_does_not_cache_failed_empty_loads():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    block = app.split("async function ensureLoaded()", 1)[1].split("function applyTheme", 1)[0]
    assert "if (!res.ok) throw new Error" in block
    assert "if (!themes.length) throw new Error" in block


def test_cv_appearance_exposes_document_scoped_paper_themes():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'id="paper-theme-options"' in html
    for paper in ("white", "creme", "soft-gray", "blue-tint"):
        assert f'data-paper-theme="{paper}"' in html
    assert ".paper-creme" in css
    assert "const getPaper" in app
    assert "&paper=${getPaper()}" in app
    assert "paper: getPaper()" in app


def test_app_theme_does_not_change_rendered_cv_preview_theme():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    preview_block = app.split("function previewUrl", 1)[1].split("function setupPreviewActions", 1)[0]
    refresh_block = app.split("async function refreshPreview", 1)[1].split("const iframe", 1)[0]
    theme_block = app.split("function setupTheme", 1)[1].split("function setupNav", 1)[0]

    assert 'const theme = opts.theme || "light";' in preview_block
    assert "effectiveTheme()" not in preview_block
    assert "previewUrl({ embedded: true })" in refresh_block
    assert "rendered CV colors stay owned by document appearance controls" in theme_block


def test_template_gallery_has_metadata_cards_and_structural_renderer():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'id="template-gallery"' in html
    assert ".template-card" in css
    assert ".template-preview-frame" in css
    assert 'data-preview-template="' in app
    assert 'role="radio"' in app
    assert "tpl.use_case" in app
    assert "tpl.one_page_risk" in app
    assert "schedulePreview(20, { saveBeforeRender: false })" in app


def test_form_section_metrics_and_ats_slots_are_separate():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    form_js = (STATIC / "form.js").read_text(encoding="utf-8")
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "form-section-titlebar" in form_js
    assert "form-section-metrics" in form_js
    assert "form-section-ats" in form_js
    assert "setAtsAdvisories" in form_js
    assert "cv:ats-advisory-open" in form_js
    assert "refreshAtsAdvisories" in app_js
    assert "markAtsStale()" in app_js
    assert ".form-section-titlebar" in css
    assert "grid-template-columns: minmax(0, 1fr) auto" in css
    assert ".form-section-metrics" in css
    assert ".form-section-ats" in css
    assert "data-required" in app_js
    assert ".issues-count" in app_js


def test_preview_iframe_keeps_pointer_events_when_not_scroll_proxying():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "#preview-wrap #preview { pointer-events: none; }" not in css
    assert "#preview-wrap.is-scrollable #preview" in css
    assert "#preview-wrap.is-zoomed-in #preview" in css


def test_embedded_preview_uses_no_internal_canvas_padding():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    preview_py = (REPO / "tools" / "editor" / "render_preview.py").read_text(encoding="utf-8")
    server_py = (REPO / "tools" / "editor" / "server.py").read_text(encoding="utf-8")
    assert "&embed=1" in app
    assert "embedded = request.args.get(\"embed\") == \"1\"" in server_py
    assert "embedded=embedded" in server_py
    assert "_PAGED_EMBED_OVERRIDES if embedded else _PAGED_OVERRIDES" in preview_py
    assert "_CONTINUOUS_EMBED_OVERRIDES if embedded else _CONTINUOUS_OVERRIDES" in preview_py
    assert "_DARK_EMBED_OVERRIDES if embedded else _DARK_OVERRIDES" in preview_py
    assert "_PAGED_EMBED_OVERRIDES" in preview_py
    embed_block = preview_py.split("_PAGED_EMBED_OVERRIDES = ", 1)[1].split("_EMBEDDED_FRAME_CLEANUP", 1)[0]
    assert "padding: 0 !important" in embed_block
    assert "background: transparent" in embed_block
    assert "box-shadow: none !important" in embed_block


def test_preview_renderer_splits_standalone_and_embedded_chrome():
    import sys

    sys.path.insert(0, str(REPO / "tools" / "editor"))
    import render_preview as rp

    cv = REPO / "content" / "cv.example.yaml"

    standalone_paged = rp.render_cv_html(cv, mode="paged", embedded=False)
    embedded_paged = rp.render_cv_html(cv, mode="paged", embedded=True)
    standalone_continuous = rp.render_cv_html(cv, mode="continuous", embedded=False)
    embedded_continuous = rp.render_cv_html(cv, mode="continuous", embedded=True)

    assert "background: #e9e9ea;" in standalone_paged
    assert "padding: 24pt 0 32pt 0;" in standalone_paged
    assert "box-shadow: 0 8px 32px" in standalone_paged

    assert "background: transparent;" in embedded_paged
    assert "padding: 0 !important;" in embedded_paged
    assert "box-shadow: none !important;" in embedded_paged

    assert "body { padding: 24pt 0; }" in standalone_continuous
    assert "body { padding: 0; margin: 0; }" in embedded_continuous
    assert "box-shadow: none;" in embedded_continuous


def test_preview_surface_resize_observer_recalculates_fit():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    block = app.split("function setupResponsiveLayout()", 1)[1].split("function setupViews()", 1)[0]
    assert "$(\"#preview-wrap\")" in block
    assert "const schedulePreviewResize" in block
    assert "applyZoom();" in block.split("const schedulePreviewResize", 1)[1].split("const onResize", 1)[0]
    assert "new ResizeObserver(schedulePreviewResize)" in block
    assert "previewRo.observe(previewWrap)" in block


def test_preview_runtime_observes_after_dom_and_paged_reflows():
    runtime = (STATIC / "preview-runtime.js").read_text(encoding="utf-8")
    assert "function installSizeObserver(send)" in runtime
    observer_block = runtime.split("function installSizeObserver(send)", 1)[1].split("// ── PAGED MODE", 1)[0]
    assert "if (!target) return false" in observer_block
    assert "DOMContentLoaded" in observer_block
    paged_block = runtime.split('if (mode() === "paged")', 1)[1].split("// ── CONTINUOUS MODE", 1)[0]
    assert "function sendPagedSize(pageCount)" in paged_block
    assert "var pages = pageCount || renderedPages.length || 1;" in paged_block
    assert "sendPagedSize(pages)" in paged_block
    assert "installSizeObserver(sendPagedSize)" in paged_block
    assert 'type: "paged-rendered"' in paged_block
    timeout_block = runtime.split("Safety net:", 1)[1]
    assert 'type: "paged-error"' in timeout_block
    assert "contentHeight: measured.height" in timeout_block


def test_idle_bullet_counts_are_hidden_until_useful():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".bullet-count" in css
    marker_block = css.split(".bullet-row::before {", 1)[1].split("}", 1)[0]
    assert 'content: ""' in marker_block
    assert "margin-top: 9px" in marker_block
    assert "box-shadow: 0 0 0 3px var(--accent-mist)" in marker_block
    count_block = css.split(".bullet-count {", 1)[1].split("}", 1)[0]
    assert "opacity: 0" in count_block
    assert "visibility: hidden" in count_block
    assert ".bullet-row:hover .bullet-count" in css
    assert ".bullet-row:focus-within .bullet-count" in css
    assert ".bullet-count.is-warn" in css
    form_js = (STATIC / "form.js").read_text(encoding="utf-8")
    assert 'const countLabel = (n) => `${n} ${n === 1 ? "item" : "items"}`;' in form_js
    assert ".form-section-count::after" not in css


def test_form_section_has_direct_hover_feedback():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".form-section:hover:not([data-highlighted=\"true\"])" in css
    assert ".form-section-head.is-collapsible:hover" not in css


def test_form_consumes_schema_fields_and_marks_custom_limits():
    js = (STATIC / "form.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "function renderSchemaFields(section, item, fire)" in js
    assert "schemaDrivenCompactShape" in js
    assert "section.fields" in js
    assert "Custom sections are stored in YAML and rendered when list-shaped" in js
    assert "function renderStructureSection(model, schema, onChange)" in js
    assert "section_order" in js
    assert "optional_sections" in js
    assert "contact_layout" in js
    assert "header_layout" in js
    assert "photo_layout" in js
    assert ".form-section-notice" in css
    assert ".form-structure-row" in css


def test_subpages_use_single_theme_mode_icon():
    # Subpage chrome (incl. the theme toggle) is injected at serve time from
    # the shared fragment (plan 108 stage 2), so subpages are checked as
    # page + fragment.
    fragment = (STATIC / "_chrome.html").read_text(encoding="utf-8")
    pages = [
        "index.html",
        "cover-letters.html",
        "jobs.html",
        "tailor.html",
        "account.html",
    ]
    for page in pages:
        html = (STATIC / page).read_text(encoding="utf-8")
        if page != "index.html":
            html += fragment
        assert 'data-icon-theme="mode"' in html
        assert 'data-icon="sun-moon"' in html
        assert 'data-icon-theme="sun"' not in html
        assert 'data-icon-theme="moon"' not in html
        assert 'data-icon-theme="monitor"' not in html
        assert 'data-icon="circle-half"' not in html
