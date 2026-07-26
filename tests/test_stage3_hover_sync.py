"""Stage 3 — bidirectional hover-sync (form ↔ preview cross-highlight).

The editor wires a shared key contract between the two panes:

  * form section  : ``id="form-section-{key}"`` + ``data-section-key="{key}"``
  * preview section: ``id="sec-{key}"`` (engine/render/templates.py)

The key maps DIRECTLY — no transformation. Hovering a form section
cross-highlights both panes and scrolls the preview *if needed*; scrolling
the preview highlights the matching form section + drives the outline.

These are static guards (same style as test_stage2_preview_srcdoc.py) that
lock in the hard invariants so the interaction can't silently regress:

  * hover NEVER calls refreshPreview/schedulePreview (those save first),
  * smooth-scroll respects prefers-reduced-motion,
  * the highlight survives a srcdoc reload,
  * the scroll-spy listener is passive.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "tools" / "editor" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
FORM_JS = (STATIC / "form.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "style.css").read_text(encoding="utf-8")
TEMPLATES_PY = (REPO / "engine" / "render" / "templates.py").read_text(encoding="utf-8")


def _strip_line_comments(js: str) -> str:
    return "\n".join(
        ln for ln in js.splitlines() if not ln.lstrip().startswith("//")
    )


class TestSharedKeyContract:
    def test_form_emits_section_hover_with_key(self):
        # Form dispatches the bridge event carrying the section key.
        assert "cv:section-hover" in FORM_JS
        assert "sectionKey" in FORM_JS
        assert 'onmouseenter: () => emitSectionHover(' in FORM_JS
        assert 'onmouseleave: () => emitSectionHover(' in FORM_JS

    def test_form_sections_carry_data_section_key(self):
        # Both the list sections and the header section expose the key.
        assert '"data-section-key": key' in FORM_JS
        assert '"data-section-key": "header"' in FORM_JS

    def test_preview_sections_use_sec_prefix(self):
        # The preview-side id the app looks up must be what the engine emits.
        assert 'safe_slug = _section_id(slug)' in TEMPLATES_PY
        assert 'id="sec-{safe_slug}"' in TEMPLATES_PY
        runtime = (STATIC / "preview-runtime.js").read_text(encoding="utf-8")
        assert 'getElementById("sec-" + key)' in runtime

    def test_app_matches_form_key_to_preview_id(self):
        # No transformation: the same key indexes both panes. The opaque
        # iframe resolves the id inside its own trusted runtime.
        assert '.form-section[data-section-key="${sectionKey}"]' in APP_JS \
            or 'data-section-key="${key}"' in APP_JS
        runtime = (STATIC / "preview-runtime.js").read_text(encoding="utf-8")
        assert 'getElementById("sec-" + key)' in runtime


class TestHoverNeverSaves:
    """The cardinal rule: hover must not trigger a preview refresh (which
    saves the file if dirty). It uses the direct scroll path only."""

    def test_hover_handler_calls_scroll_if_needed(self):
        assert "scrollPreviewIntoViewIfNeeded" in APP_JS
        # The hover listener invokes the scroll-if-needed helper.
        block = APP_JS.split("function setupHoverSync()", 1)[1]
        block = block.split("function highlightActiveFormSection", 1)[0]
        assert "scrollPreviewIntoViewIfNeeded(sectionKey" in block

    def test_hover_block_does_not_refresh_or_schedule(self):
        code = _strip_line_comments(APP_JS)
        block = code.split("function setupHoverSync()", 1)[1]
        block = block.split("function highlightActiveFormSection", 1)[0]
        assert "refreshPreview(" not in block
        assert "schedulePreview(" not in block


class TestReducedMotion:
    def test_scroll_helpers_respect_reduced_motion(self):
        # The shared media query is read and used to downgrade to "auto".
        assert 'matchMedia("(prefers-reduced-motion: reduce)")' in APP_JS
        assert "PREFERS_REDUCED_MOTION.matches" in APP_JS
        # Both scroll helpers downgrade behaviour when motion is reduced.
        assert APP_JS.count('PREFERS_REDUCED_MOTION.matches ? "auto"') >= 2

    def test_preview_highlight_style_has_reduced_motion_guard(self):
        # The isolated child runtime's fixed stylesheet zeroes transition.
        runtime = (STATIC / "preview-runtime.js").read_text(encoding="utf-8")
        assert "prefers-reduced-motion: reduce" in runtime
        assert "[data-highlighted=" in runtime


class TestScrollIfNeeded:
    """Preview scrolls only when the target section is off-screen."""

    def test_returns_false_when_visible(self):
        block = APP_JS.split("function scrollPreviewIntoViewIfNeeded", 1)[1]
        block = block.split("function setupHoverSync", 1)[0]
        # The early-out: comfortably visible → no scroll.
        assert "if (!above && !below) return false" in block
        # The shared geometry helper accounts for the CSS transform scale.
        helper = APP_JS.split("function previewSectionScreenRect", 1)[1]
        helper = helper.split("function scrollPreviewToSection", 1)[0]
        assert "effectiveZoom()" in helper


class TestSrcdocReloadSurvival:
    def test_pending_highlight_reapplied_on_load(self):
        # State var exists and is re-applied on both reload paths.
        assert "pendingHighlightKey" in APP_JS
        assert "reapplyPendingHighlight" in APP_JS
        # refreshPreview onload re-applies.
        onload = APP_JS.split("iframe.onload = () =>", 1)[1]
        onload = onload[: onload.index("};")]
        assert "reapplyPendingHighlight()" in onload
        # paged-rendered branch re-applies too.
        assert APP_JS.count("reapplyPendingHighlight()") >= 2

    def test_highlight_styles_injected_after_load(self):
        # The iframe DOM is wiped on srcdoc reload; the child runtime
        # re-injects fixed styles without exposing same-origin DOM access.
        runtime = (STATIC / "preview-runtime.js").read_text(encoding="utf-8")
        assert "ensurePreviewHighlightStyles" in APP_JS
        assert 'document.createElement("style")' in runtime
        assert 'cv-hover-highlight' in runtime


class TestOpaqueOriginIsolation:
    def test_parent_never_reads_preview_dom(self):
        assert "contentDocument" not in APP_JS

    def test_parent_validates_source_origin_and_geometry(self):
        assert "e.source !== previewFrame.contentWindow" in APP_JS
        assert 'e.origin !== "null"' in APP_JS
        assert "Number.isFinite(top)" in APP_JS
        assert "data.sections.slice(0, 100)" in APP_JS

    def test_child_accepts_only_narrow_validated_commands(self):
        runtime = (STATIC / "preview-runtime.js").read_text(encoding="utf-8")
        assert "event.source !== window.parent" in runtime
        assert 'data.type === "preview-highlight"' in runtime
        assert "SECTION_KEY.test(key)" in runtime
        assert "eval(" not in runtime


class TestScrollSpy:
    def test_scroll_spy_is_passive_and_rafd(self):
        block = APP_JS.split("function setupHoverSync()", 1)[1]
        # Listens on the single scroll layer, passively.
        assert '#preview-wrap' in block or 'preview-wrap' in APP_JS
        assert "{ passive: true }" in block
        assert "requestAnimationFrame(runScrollSpy)" in block

    def test_scroll_spy_drives_outline_active(self):
        assert "highlightActiveFormSection" in APP_JS
        block = APP_JS.split("function highlightActiveFormSection", 1)[1]
        assert "data-scroll-active" in block
        # Same signal updates the outline's active item.
        assert "outline-item" in block


class TestHighlightCss:
    def test_form_highlight_styles_present(self):
        assert '.form-section[data-highlighted="true"]' in STYLE_CSS
        assert '[data-scroll-active="true"]' in STYLE_CSS

    def test_highlight_uses_accent_tokens(self):
        # Driven by the design-spec accent tokens, not hard-coded colours.
        block = STYLE_CSS.split('.form-section[data-highlighted="true"]', 1)[1]
        block = block[:400]
        assert "var(--accent" in block


class TestWiredIntoInit:
    def test_setup_hover_sync_called_in_init(self):
        init = APP_JS.split("function init()", 1)[1]
        init = init[: init.index("loadCv()")]
        assert "setupHoverSync();" in init
