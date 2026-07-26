"""Regression contracts for the QA-verified frontend fixes (editor static assets).

Same grep-based style as test_ui_static_contracts.py: each test pins the
fixed snippet in tools/editor/static/{form.js,app.js,index.html,style.css}
and asserts the buggy snippet is gone, so the defects can't silently return.
"""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    rgb = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg: str, bg: str) -> float:
    hi = max(_luminance(fg), _luminance(bg))
    lo = min(_luminance(fg), _luminance(bg))
    return (hi + 0.05) / (lo + 0.05)


def test_yaml_parse_uses_core_schema_so_dates_stay_strings():
    """F1: js-yaml's default schema resolves `2024-01-01` into a Date, which a
    form-driven save re-dumps as `2024-01-01T00:00:00.000Z` — silent data
    corruption. CORE_SCHEMA keeps bool/int/float but treats timestamps as
    strings."""
    form = _read("tools/editor/static/form.js")

    assert "jsyaml.load(text, { schema: jsyaml.CORE_SCHEMA })" in form
    assert "jsyaml.load(text)" not in form


def test_required_form_gaps_never_block_a_save():
    """F2: the hasRequiredFormGaps gate inside saveNow returned false for
    autosave, Cmd+S, the build-first save AND the pagehide flush — every edit
    made while a required field was empty was lost, and Live preview froze.
    The form is deliberately permissive; gaps are a non-blocking hint now."""
    app = _read("tools/editor/static/app.js")

    assert "Complete required fields" not in app
    # The hint path remains, but without the blocking ``return false``.
    assert "if (hasRequiredFormGaps()) {\n      refreshIssuesPill();\n      refreshSectionHints();\n    }" in app
    save_now = app.split("async function saveNow", 1)[1].split("function save(", 1)[0]
    assert "hasRequiredFormGaps" in save_now
    hint_block = save_now.split("if (hasRequiredFormGaps())", 1)[1].split("}", 1)[0]
    assert "return false" not in hint_block


def test_list_section_remove_and_move_capture_item_not_stale_index():
    """F3: the Remove handler read a DOM index, spliced, then awaited ~160ms
    of exit animation before reindexing — a second click inside that window
    spliced whatever had shifted into the stale index. Handlers now resolve
    the item's live position and disable the card's buttons synchronously."""
    form = _read("tools/editor/static/form.js")

    assert "const i = parseInt(card.dataset.idx, 10);" not in form
    assert form.count("const i = items.indexOf(item);") == 3  # up, down, remove
    assert '$$(".form-icon-btn", card).forEach((b) => { b.disabled = true; });' in form
    remove = form.split('tr("form.remove", "Remove"),', 1)[1].split("onChange();", 1)[0]
    assert remove.index("b.disabled = true") < remove.index("await animateCardOut(card)")


def test_tour_is_a_modal_dialog_and_does_not_hijack_keys_globally():
    """F4: the tour overlay had no dialog semantics, never took focus, and its
    global keydown intercepted Enter/Arrows app-wide (Enter on #refresh-btn
    advanced the tour instead of activating the button)."""
    html = _read("tools/editor/static/index.html")
    app = _read("tools/editor/static/app.js")

    assert 'id="tour" class="tour" hidden role="dialog" aria-modal="true" aria-labelledby="tour-title"' in html
    assert '<h3 class="tour-title" id="tour-title"></h3>' in html

    # Round 2 (finding 9): focus moves to the tour CARD (tabindex=-1) on each
    # step change so the step content is announced, instead of yanking focus
    # to the Next button every time.
    assert "next.focus();" not in app
    assert 'focusCard.focus({ preventScroll: true });' in app
    assert "restoreFocusTo.focus();" in app
    keydown = app.split('document.addEventListener("keydown", (e) => {\n      if (overlay.hasAttribute("hidden")) return;', 1)[1]
    keydown = keydown.split("});", 1)[0]
    assert "if (!overlay.contains(document.activeElement)) return;" in keydown
    assert 'e.key === "Tab"' in keydown


def test_switch_cv_toasts_when_pre_switch_save_fails():
    """F5: switchCv aborted silently on save failure while newCv and
    OnePagerNavigate both toast — the user could think they switched CVs
    while their edits were still unsaved."""
    app = _read("tools/editor/static/app.js")

    switch = app.split("async function switchCv", 1)[1].split("async function dispatchDocumentAction", 1)[0]
    assert "if (!saved) return false;" not in switch
    assert '"CV was not switched because current edits are still unsaved."' in switch


def test_save_failure_surfaces_server_detail_and_skips_invalid_yaml_autosave():
    """F6: a 400 (e.g. "YAML invalid: <reason>") collapsed into a bare
    "Save failed" with the detail only in the console, and autosave kept
    shipping the mid-edit invalid YAML buffer."""
    app = _read("tools/editor/static/app.js")

    assert 'setStatus("error", `Save failed: ${detail}`);' in app
    assert 'setStatus("error", "Save failed");' not in app
    assert "let yamlBufferInvalid = false;" in app
    assert 'if (opts.auto === true && viewMode === "yaml" && yamlBufferInvalid) return false;' in app
    on_edit = app.split("function onEdit()", 1)[1].split("function onFormChange", 1)[0]
    assert "yamlBufferInvalid = true;" in on_edit
    assert "yamlBufferInvalid = false;" in on_edit


def test_collapsible_section_heads_and_photo_drop_zone_have_visible_focus():
    """F7: keyboard users got no focus cue on collapsible section headers
    (outline: none + a ~1.1:1 background shift) or on the photo drop zone."""
    css = _read("tools/editor/static/style.css")

    head_block = css.split(".form-section-head.is-collapsible:focus-visible {", 1)[1].split("}", 1)[0]
    assert "outline: 2px solid var(--accent);" in head_block
    assert "outline: none;" not in head_block
    photo_block = css.split(".photo-drop-zone:focus-visible {", 1)[1].split("}", 1)[0]
    assert "outline: 2px solid var(--accent);" in photo_block


def test_text_faint_token_meets_wcag_aa_on_its_backgrounds():
    """F8: --text-faint drove ~11px status/statusbar text at ~3:1 contrast.
    The tokens must hold ≥4.5:1 on every surface they render on."""
    css = _read("tools/editor/static/style.css")

    assert "#8a93a4" not in css.split("--text-faint", 1)[1].split(";", 1)[0]
    assert css.count("--text-faint:    #686f80;") == 2  # :root + [data-theme="light"]
    assert css.count("--text-faint:    #8a91a0;") == 1  # [data-theme="dark"]
    for bg in ("#ffffff", "#f7f8fa", "#f3f4f7"):
        assert _contrast("#686f80", bg) >= 4.5, f"light --text-faint fails on {bg}"
    for bg in ("#0a0b0f", "#161922", "#08090c"):
        assert _contrast("#8a91a0", bg) >= 4.5, f"dark --text-faint fails on {bg}"


def test_chip_remove_buttons_meet_minimum_target_size():
    """F9: .chip-x was 16×16px — below the WCAG 2.5.8 24×24 minimum."""
    css = _read("tools/editor/static/style.css")

    block = css.split(".chip-x {", 1)[1].split("}", 1)[0]
    assert "width: 24px;" in block
    assert "height: 24px;" in block
    assert "width: 16px;" not in block


def test_segmented_preview_controls_meet_minimum_target_size():
    """F10: .seg-btn was ~20-21px tall (4px vertical padding)."""
    css = _read("tools/editor/static/style.css")

    block = css.split(".seg-btn {", 1)[1].split("}", 1)[0]
    assert "padding: 6px 9px;" in block
    assert "padding: 4px 9px;" not in block


def test_import_error_and_ai_key_message_are_announced():
    """F11: both error containers are written dynamically and must be live
    regions so screen readers announce them."""
    html = _read("tools/editor/static/index.html")

    assert 'id="import-error" class="modal-error" role="alert" hidden' in html
    assert 'id="ai-key-msg" class="ai-key-msg" role="alert" hidden' in html


def test_codemirror_input_field_has_an_accessible_name():
    """F12: the YAML source <textarea> is unlabeled; CodeMirror's real input
    field gets an aria-label after init."""
    app = _read("tools/editor/static/app.js")

    assert 'editor.getInputField().setAttribute("aria-label", "YAML source editor")' in app


def test_resizer_separator_exposes_name_and_value():
    """F13: a focusable role="separator" with arrow-key support must expose
    an accessible name and aria-valuenow/min/max (kept live by the handler)."""
    html = _read("tools/editor/static/index.html")
    app = _read("tools/editor/static/app.js")

    resizer = html.split('id="resizer"', 1)[1].split(">", 1)[0]
    assert 'role="separator"' in resizer
    assert 'aria-label="Resize editor and preview panes"' in resizer
    assert "aria-valuemin=" in resizer
    assert "aria-valuemax=" in resizer
    assert "aria-valuenow=" in resizer
    assert 'sep.setAttribute("aria-valuenow"' in app


def test_single_escapehtml_definition_in_app_scope():
    """F14: two escapeHtml definitions lived in the same IIFE scope; the
    second silently won. Exactly one must remain."""
    app = _read("tools/editor/static/app.js")

    assert app.count("function escapeHtml(s)") == 1


def test_undo_redo_uses_the_normal_refresh_and_autosave_schedule():
    """F15: applySnapshot called schedulePreview directly (bypassing Manual
    auto-render mode) and used an 800ms autosave vs 1500ms everywhere else."""
    app = _read("tools/editor/static/app.js")

    snap = app.split("function applySnapshot", 1)[1].split("function undo()", 1)[0]
    assert "scheduleContentRefresh(120);" in snap
    assert "schedulePreview(120)" not in snap
    assert "}, 1500);" in snap
    assert "}, 800);" not in snap


def test_noop_save_clears_conflict_pending():
    """F16: after a 409, reverting the buffer to the saved content and saving
    returned early (content === lastSavedContent) leaving conflictPending set
    — autosaves stayed suppressed indefinitely."""
    app = _read("tools/editor/static/app.js")

    branch = app.split("if (content === lastSavedContent) {", 1)[1].split("}", 1)[0]
    assert "conflictPending = false;" in branch
