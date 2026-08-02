/* ============================================================================
   CV Editor — YAML document editor with paged.js preview.

   The layout patterns below are kept centralized because partial
   reimplementations can silently break the editor:
     • setupResponsiveLayout — without this, CodeMirror's lower half goes
       blank when the window is resized or the sidebar toggles.
     • setupZoomControls / applyZoom / effectiveZoom — fits the A4 sheet
       inside the available preview viewport without iframe-internal scroll.
     • paged.js postMessage handler — listens for "paged-rendered" from the
       iframe and updates the page-count pill (red on overflow).
   ============================================================================ */
(function () {
  "use strict";

  const $  = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const clamp = (n, min, max) => Math.max(min, Math.min(max, n));
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  // ──────────────────────────────────────────────────────────────────────
  //  CONSTANTS
  // ──────────────────────────────────────────────────────────────────────
  const A4_W_PX   = 794;     // 210 mm at 96 dpi
  const A4_H_PX   = 1123;    // 297 mm at 96 dpi
  const PREVIEW_PAD_PX = 18; // visual margin around the rendered paper
  const FIT_ZOOM_MIN = 0.05; // fit mode may need to show a full A4 page in short panes
  const ZOOM_MIN  = 0.30;
  const ZOOM_MAX  = 3.00;
  const ZOOM_STEP = 0.10;

  const SPLIT_KEY    = "cv.editor.split";
  const THEME_KEY    = "cv.editor.theme";
  const ZOOM_KEY     = "cv.editor.previewZoom";
  const MODE_KEY     = "cv.editor.previewMode";
  const SIDEBAR_KEY  = "cv.editor.sidebar";
  const DENSITY_KEY  = "cv.editor.density";
  const FONT_KEY     = "cv.editor.font";
  const NEW_CV_DEFAULTS_KEY = "cv.editor.new_cv_defaults";
  const TEMPLATE_IDS = ["classic", "modern", "sidebar", "executive", "technical", "ats-compact", "consulting", "minimal"];
  const VIEW_KEY     = "cv.editor.view";
  const FORM_MODE_KEY = "cv.editor.formMode";
  const ACTIVE_SECTION_KEY = "cv.editor.activeSection";
  const AUTO_KEY     = "cv.editor.autoRender";
  const LINT_KEY     = "cv.editor.lintOnSave";
  const CV_ID_KEY    = "cv.editor.activeId";  // active document UUID (Stage 3)
  const HISTORY_MAX  = 40;
  const COLLAPSED_KEY = "cv.editor.collapsed"; // namespaced per CV id
  const ATS_PROFILE_KEY = "cv.editor.atsProfile";

  // Typing → preview debounce. Long enough to feel calm, short enough to
  // feel alive when you stop typing. Only applies in Live mode.
  const CONTENT_DEBOUNCE_MS = 400;

  // ──────────────────────────────────────────────────────────────────────
  //  STATE
  // ──────────────────────────────────────────────────────────────────────
  let editor = null;
  let dirty = false;
  let lastSavedContent = "";
  let lastSavedAt = null;
  let activeCvRevision = null;
  // Set true after a 409 save-conflict. While set, AUTO saves (autosave timer,
  // preview save-first, pagehide/visibilitychange flush) are suppressed so a
  // background event can't silently last-write-wins over the other tab — only an
  // EXPLICIT Save (button / Cmd+S) overwrites. Cleared on the next successful
  // save or on loading a fresh document.
  let conflictPending = false;
  // Set by onEdit when the YAML buffer no longer parses. Autosaves skip a
  // buffer in this state (a mid-edit 400 would flash a misleading
  // "Save failed"); explicit saves still go through to surface the reason.
  let yamlBufferInvalid = false;
  let loadCvReq = 0;
  let readyStatusTimer = 0;
  let saveQueue = Promise.resolve();
  let buildInFlight = false;
  let previewMode = localStorage.getItem(MODE_KEY) || "paged";
  if (!["continuous", "paged"].includes(previewMode)) previewMode = "paged";
  let previewZoom = parseFloat(localStorage.getItem(ZOOM_KEY) || "0") || 0;
  // CV content dimensions (px) — reported by the iframe via postMessage.
  // applyZoom() uses these to size the stage tall and wide enough to fit
  // the entire CV without iframe-internal scrolling.
  let cvContentHeight = A4_H_PX;
  let cvContentWidth = A4_W_PX;
  let cvPageHeight = A4_H_PX;
  let cvPageWidth = A4_W_PX;
  // ``density`` and ``font`` are now canonical YAML fields on cvModel.
  // These two getters fall back to defaults so the URL builders below
  // can read them safely even before the model loads.
  const getDensity = () => {
    const d = (cvModel && cvModel.density) || "normal";
    return ["tight", "normal", "airy"].includes(d) ? d : "normal";
  };
  const getFont = () => {
    const f = (cvModel && cvModel.font) || "serif";
    return ["serif", "sans", "mono"].includes(f) ? f : "serif";
  };
  const getAccent = () => {
    const accent = String((cvModel && cvModel.accent) || "#111111").trim();
    return /^#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(accent) ? accent : "#111111";
  };
  const getTemplate = () => {
    const tpl = (cvModel && cvModel.template) || "classic";
    return TEMPLATE_IDS.includes(tpl) ? tpl : "classic";
  };
  const getPaper = () => {
    const paper = (cvModel && cvModel.paper) || "white";
    return ["white", "creme", "soft-gray", "blue-tint"].includes(paper) ? paper : "white";
  };

  function getNewCvDefaults() {
    let parsed = {};
    try {
      parsed = JSON.parse(localStorage.getItem(NEW_CV_DEFAULTS_KEY) || "{}") || {};
    } catch (_) {
      parsed = {};
    }
    const out = {};
    if (/^#[0-9a-f]{6}$/i.test(parsed.accent || "")) out.accent = parsed.accent;
    if (["serif", "sans", "mono"].includes(parsed.font)) out.font = parsed.font;
    if (["tight", "normal", "airy"].includes(parsed.density)) out.density = parsed.density;
    if (TEMPLATE_IDS.includes(parsed.template)) out.template = parsed.template;
    if (["white", "creme", "soft-gray", "blue-tint"].includes(parsed.paper)) out.paper = parsed.paper;
    return out;
  }

  function applyNewCvDefaultsToActiveModel() {
    const defaults = getNewCvDefaults();
    const keys = Object.keys(defaults);
    if (!keys.length) return false;
    keys.forEach((key) => { cvModel[key] = defaults[key]; });
    refreshAppearanceUi();
    return true;
  }

  let outlineItems = [];
  let outlineHoverTimer = null;
  let outlineHoverKey = null;
  let outlineHoverSuppressedUntil = 0;
  let pendingScrollTarget = null;
  // The preview iframe intentionally omits sandbox allow-same-origin. Its
  // document therefore has an opaque origin and MUST NOT be reached through
  // direct DOM access. The trusted preview runtime reports only validated
  // section geometry through postMessage; no CV content crosses the boundary.
  let previewSections = [];
  // Hover-sync: the form section currently cross-highlighted in the preview.
  // Tracked here so it can be re-applied after a srcdoc reload wipes the
  // isolated iframe document (see refreshPreview + setupPagedListener).
  let pendingHighlightKey = null;
  let previewTimer = null;
  let autosaveTimer = null;
  let lastPagedCount = null;
  let viewMode = localStorage.getItem(VIEW_KEY) || "form";
  if (!["form", "yaml"].includes(viewMode)) viewMode = "form";
  let formMode = localStorage.getItem(FORM_MODE_KEY) || "focus";
  if (!["focus", "overview"].includes(formMode)) formMode = "focus";
  let activeFormSection = null;

  // Auto-render toggle: Live (default) re-renders on debounced edits;
  // Manual waits for the user to click Refresh.
  const storedAuto = localStorage.getItem(AUTO_KEY);
  let autoRender = storedAuto == null ? true : storedAuto === "true";
  // pendingRefresh is set when content edits arrive in Manual mode.
  let pendingRefresh = false;

  // YAML auto-lint on save. ONLY runs on user-initiated saves (Cmd+S,
  // Save button), NEVER on the 1.5 s idle autosave — the cursor jump
  // mid-typing is unbearable. Toggle is visible only in YAML view.
  const storedLint = localStorage.getItem(LINT_KEY);
  let lintOnSave = storedLint == null ? true : storedLint === "true";

  // Active CV document (Stage 3). Each user owns many CVs stored in
  // Postgres; we address them by UUID. ``activeCvId`` is null until the
  // first /api/cvs load resolves it (null = "server picks most-recent").
  // The name is shown in the topbar picker. Both are kept in sync by the
  // document picker below.
  const requestedContextId = (name) => {
    try {
      const value = new URLSearchParams(window.location.search || "").get(name);
      return UUID_RE.test(value || "") ? value : null;
    } catch (_) {
      return null;
    }
  };
  let requestedCvId = requestedContextId("id");
  const applicationContextJobId = requestedContextId("job_id");
  const applicationContextSourceCvId = requestedContextId("source_cv_id") || requestedContextId("cv_id");
  let activeCvId = localStorage.getItem(CV_ID_KEY) || null;
  let activeCvName = "My CV";

  // Live ATS badge (Stage 7). The topbar shows the ATS fit of the active CV
  // against its LINKED saved job (``documents.job_id`` → surfaced on /api/cvs).
  // Only meaningful when the CV is linked to a job; hidden otherwise.
  //   • activeJob: {id, title} resolved from usable packet lineage in /api/cvs.
  //   • atsBadgeCache: contentHash → score, so re-opening an unchanged CV is free.
  //   • atsBadgeTimer / atsBadgeReq: debounce + stale-response guard.
  let activeJob = null;
  let activePacketLineage = null;
  const atsBadgeCache = new Map();
  let atsBadgeTimer = null;
  let atsBadgeReq = 0;
  const ATS_BADGE_DEBOUNCE_MS = 700;
  let ATS_PROFILES = {
    keyword_screen: {
      label: "Keyword screen",
      description: "Deterministic keyword and must-have coverage simulation.",
    },
    strict_requirements: {
      label: "Strict requirements",
      description: "Conservative must-have requirements simulation.",
    },
    semantic_fit: {
      label: "Semantic fit",
      description: "Rubric-preferred fit simulation; deterministic fallback when no AI key is available.",
    },
  };
  let atsProfile = localStorage.getItem(ATS_PROFILE_KEY) || "semantic_fit";
  if (!ATS_PROFILES[atsProfile]) atsProfile = "semantic_fit";
  let atsState = {
    status: "empty",
    cvId: null,
    jobId: null,
    contentHash: null,
    scoredAt: null,
    source: null,
    score: null,
    payload: null,
    sections: {},
    stale: false,
    message: "No linked job for this CV.",
    packetLineage: null,
  };

  function loadAtsProfiles() {
    return fetch("/api/ats-profiles")
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data || !Array.isArray(data.profiles)) return;
        const next = {};
        data.profiles.forEach((profile) => {
          if (profile && profile.id) next[profile.id] = profile;
        });
        if (!Object.keys(next).length) return;
        ATS_PROFILES = next;
        if (!ATS_PROFILES[atsProfile]) {
          atsProfile = data.default_profile || "semantic_fit";
          localStorage.setItem(ATS_PROFILE_KEY, atsProfile);
        }
        renderAtsDetail();
      })
      .catch(() => {});
  }

  // Build the id query-suffix shared by every per-document API call. Empty
  // when no id is known yet (server falls back to the most-recent CV).
  function idParam() {
    return activeCvId ? `&id=${encodeURIComponent(activeCvId)}` : "";
  }

  // Canonical CV model. Form view mutates this directly; YAML view writes
  // its parsed value back here on every change. The form module holds a
  // reference to this *same object*, so we replace its CONTENTS rather than
  // reassigning the variable — otherwise the form keeps rendering against
  // the original empty version.
  const cvModel = {};
  let formApi = null;
  let suppressEditorOnEdit = false;

  // Set when the page is being torn down (reload / navigation). The demo-login
  // flow calls window.location.reload() right after the session lands, which
  // can abort an in-flight first /api/cv fetch with "TypeError: Failed to
  // fetch". That's benign — the reloaded page boots cleanly — so we use this
  // flag to avoid logging a scary error for an aborted-by-unload request.
  let pageUnloading = false;

  function replaceModel(newModel) {
    for (const k of Object.keys(cvModel)) delete cvModel[k];
    Object.assign(cvModel, newModel || {});
  }

  // ──────────────────────────────────────────────────────────────────────
  //  HISTORY — in-memory undo/redo. Cmd+Z / Cmd+Shift+Z.
  //
  //  CV snapshots contain the user's complete document and therefore must
  //  never survive logout, account deletion, or a browser restart. Older
  //  releases persisted snapshots under cv.editor.history::*; purge those
  //  legacy values before holding any new history in process memory.
  //
  //  Pattern: ``presentSnapshot`` tracks the last "settled" state. On an
  //  edit, the debounced recordHistory pushes the OLD presentSnapshot
  //  to ``historyPast`` and updates presentSnapshot to the new state.
  //  Undo pops past → restores it; redo pops future. This avoids the
  //  "undo to current state" bug where the most recent record == now.
  // ──────────────────────────────────────────────────────────────────────
  let historyPast = [];   // older settled snapshots, newest at the end
  let historyFuture = []; // future for redo after an undo
  let presentSnapshot = ""; // last debounced canonical snapshot
  let historyTimer = null;

  function purgeLegacyHistoryStorage() {
    try {
      const doomed = [];
      for (let i = 0; i < localStorage.length; i += 1) {
        const key = localStorage.key(i);
        if (key && key.startsWith("cv.editor.history::")) doomed.push(key);
      }
      doomed.forEach((key) => localStorage.removeItem(key));
    } catch (_) {}
  }
  purgeLegacyHistoryStorage();

  function historyLoad() {
    historyPast = [];
    historyFuture = [];
    // Initial load = our first present snapshot. Reset the future since
    // a fresh load means the user hasn't undone anything yet.
    presentSnapshot = snapshot();
  }
  function historyPersist() {
    // Intentionally memory-only: never persist CV snapshots in browser storage.
  }
  function snapshot() {
    return JSON.stringify(cvModel);
  }
  function recordHistory() {
    if (historyTimer) clearTimeout(historyTimer);
    historyTimer = setTimeout(() => {
      historyTimer = null;
      const snap = snapshot();
      if (snap === presentSnapshot) return;
      // Push the OLD present (the state we're moving away from).
      if (presentSnapshot) historyPast.push(presentSnapshot);
      if (historyPast.length > HISTORY_MAX) historyPast.shift();
      presentSnapshot = snap;
      historyFuture = [];
      historyPersist();
    }, 500);
  }
  function applySnapshot(snap) {
    let parsed;
    try { parsed = JSON.parse(snap); } catch (_) { return; }
    if (!parsed || typeof parsed !== "object") return;
    replaceModel(parsed);
    if (formApi) formApi.rebuild();
    refreshAtsAdvisories();
    refreshAppearanceUi();
    refreshYamlBufferFromModel();
    renderOutline();
    refreshIssuesPill();
    refreshSectionHints();
    dirty = true;
    setStatus("modified", "Modified");
    // Same scheduling path/interval as normal edits: scheduleContentRefresh
    // respects the Manual auto-render mode (schedulePreview would bypass it),
    // and the autosave debounce matches onEdit/onFormChange (1500ms).
    scheduleContentRefresh(120);
    if (autosaveTimer) clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => { autosaveTimer = null; save({ auto: true }); }, 1500);
  }
  function undo() {
    // If there's a pending un-snapshotted edit, snapshot it first so
    // the very first Cmd+Z after typing reverts that edit.
    if (historyTimer) {
      clearTimeout(historyTimer);
      historyTimer = null;
      const snap = snapshot();
      if (snap !== presentSnapshot) {
        if (presentSnapshot) historyPast.push(presentSnapshot);
        presentSnapshot = snap;
        historyFuture = [];
      }
    }
    if (!historyPast.length) return;
    const prev = historyPast.pop();
    historyFuture.push(presentSnapshot);
    presentSnapshot = prev;
    historyPersist();
    applySnapshot(prev);
  }
  function redo() {
    if (!historyFuture.length) return;
    const next = historyFuture.pop();
    historyPast.push(presentSnapshot);
    presentSnapshot = next;
    historyPersist();
    applySnapshot(next);
  }

  // ──────────────────────────────────────────────────────────────────────
  //  THEME
  // ──────────────────────────────────────────────────────────────────────
  // Theme is owned by theme-init.js (window.OnePagerTheme): it does the
  // no-flash <head> restore, the light/dark cycle, persistence, and the
  // toggle-button paint — shared with the sub-pages so the logic lives in one
  // place. Rendered CV appearance stays document-scoped; app light/dark mode
  // never changes the CV preview or the exported PDF colors.
  function effectiveTheme() {
    const T = window.OnePagerTheme;
    if (T) return T.resolved();
    // Defensive fallback if the module failed to load.
    return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  }

  function setupTheme() {
    const T = window.OnePagerTheme;
    const btn = $("#theme-toggle");
    if (T) {
      // Wire the toggle through the shared module. The chrome flips via CSS;
      // rendered CV colors stay owned by document appearance controls.
      T.wireToggle(btn, () => applyZoom());
    } else if (btn) {
      // Fallback: minimal local cycle if the module didn't load.
      const apply = (theme) => {
        document.documentElement.dataset.theme = theme;
        btn.classList.remove("is-light", "is-dark");
        btn.classList.add(`is-${theme}`);
      };
      const stored = localStorage.getItem(THEME_KEY);
      apply(stored === "dark" ? "dark" : "light");
      btn.addEventListener("click", () => {
        const cur = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
        const nxt = cur === "light" ? "dark" : "light";
        try { localStorage.setItem(THEME_KEY, nxt); } catch (_) {}
        apply(nxt);
        schedulePreview(40, { saveBeforeRender: false });
      });
    }
  }

  // ──────────────────────────────────────────────────────────────────────
  //  EDITOR
  // ──────────────────────────────────────────────────────────────────────
  function setupEditor() {
    editor = CodeMirror.fromTextArea($("#editor"), {
      mode: "yaml",
      lineNumbers: true,
      lineWrapping: true,
      autoCloseBrackets: true,
      styleActiveLine: { nonEmpty: false },
      indentUnit: 2,
      tabSize: 2,
      extraKeys: {
        "Cmd-S": userInitiatedSave,
        "Ctrl-S": userInitiatedSave,
        "Cmd-F": "findPersistent",
        "Ctrl-F": "findPersistent",
        "Cmd-=": (cm) => { bumpZoom(+ZOOM_STEP); return false; },
        "Cmd--": (cm) => { bumpZoom(-ZOOM_STEP); return false; },
        "Cmd-0": (cm) => { setZoom(0); return false; },
        "Ctrl-=": (cm) => { bumpZoom(+ZOOM_STEP); return false; },
        "Ctrl--": (cm) => { bumpZoom(-ZOOM_STEP); return false; },
        "Ctrl-0": (cm) => { setZoom(0); return false; },
      },
    });

    // The underlying textarea is unlabeled in markup; give CodeMirror's
    // actual input field an accessible name.
    if (editor.getInputField) {
      editor.getInputField().setAttribute("aria-label", "YAML source editor");
    }

    // CodeMirror's fromTextArea() captures the textarea's offsetHeight
    // (default 300 px) at construction time and uses that for its viewport,
    // even after the .CodeMirror element is sized to fill the host. Tell
    // it to fill the parent explicitly.
    editor.setSize("100%", "100%");

    editor.on("change", onEdit);
    editor.on("cursorActivity", () => {
      updatePosition();
      updateOutlineActive();
    });
  }

  function onEdit() {
    // Programmatic value changes (loadCv, switchView, importer) shouldn't
    // trigger the modified/auto-save loop.
    if (suppressEditorOnEdit) return;

    // Only the YAML view can mutate the buffer interactively. In form view
    // the buffer is just a synced mirror — ignore stray edits.
    if (viewMode !== "yaml") return;

    // Try to parse the buffer back into the model. If it fails, leave the
    // last-good model in place so the preview / save still work — the user
    // can fix the YAML in this view, and the form view stays valid.
    try {
      const parsed = window.CvForm.yamlToModel(editor.getValue());
      if (parsed && typeof parsed === "object") replaceModel(parsed);
      yamlBufferInvalid = false;
    } catch (_) {
      /* keep last good model */
      yamlBufferInvalid = true;
    }

    if (editor.getValue() === lastSavedContent) {
      dirty = false;
      setStatus("ready", "Ready");
      scheduleAtsBadge(0);
    } else {
      dirty = true;
      setStatus("modified", "Modified");
      markAtsStale();
    }
    if (autosaveTimer) clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => { autosaveTimer = null; save({ auto: true }); }, 1500);
    scheduleContentRefresh();
    renderOutline();
    updateBytesInfo();
  }

  function onFormChange() {
    dirty = true;
    setStatus("modified", "Modified");
    if (autosaveTimer) clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => { autosaveTimer = null; save({ auto: true }); }, 1500);
    scheduleContentRefresh();
    renderOutline();
    refreshIssuesPill();
    refreshSectionHints();
    markAtsStale();
    recordHistory();
  }

  // ──────────────────────────────────────────────────────────────────────
  //  POLISH — listen for the form's polish-bullet event and call the selected
  //  AI provider.
  //  The form module is intentionally agnostic: it dispatches a custom
  //  event with { input, value, setValue } and we handle the API call
  //  here, where provider/key availability is also tracked.
  // ──────────────────────────────────────────────────────────────────────
  let polishAvailable = null;

  function setupPolish() {
    window.addEventListener("cv:polish-controls-rendered", applyPolishVisibility);

    // Probe availability in the background — same endpoint the import
    // modal uses.
    fetch("/api/extract/status")
      .then((r) => r.ok ? r.json() : null)
      .then((data) => { polishAvailable = !!(data && data.ai_available); applyPolishVisibility(); })
      .catch(() => { polishAvailable = false; applyPolishVisibility(); });

    window.addEventListener("cv:polish-bullet", async (e) => {
      const { input, value, setValue } = e.detail || {};
      if (!input || typeof setValue !== "function") return;
      if (polishAvailable === false) {
        showBuildToast(
          "AI polish needs a provider",
          "Connect a provider in Account.",
          "danger",
        );
        return;
      }
      const btn = input.parentElement && input.parentElement.querySelector(".bullet-polish");
      if (btn) btn.classList.add("is-busy");
      try {
        const res = await fetch("/api/polish/bullet", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: value }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        if (data.rewritten && data.rewritten !== value) {
          setValue(data.rewritten);
          showBuildToast("Polished ✦", `"${shortText(data.rewritten, 80)}"`, "success");
        } else {
          showBuildToast("Already tight", "The selected provider judged this bullet was already fine.", "success");
        }
      } catch (err) {
        showBuildToast("Polish failed", String(err.message || err), "danger");
      } finally {
        if (btn) btn.classList.remove("is-busy");
      }
    });

    // Section-level tighten: a button on each experience card. Wired
    // here too because it shares the polish API.
    window.addEventListener("cv:polish-section", async (e) => {
      const { item, fire, button } = e.detail || {};
      if (!item || !Array.isArray(item.bullets) || !item.bullets.length) return;
      if (polishAvailable === false) {
        showBuildToast("AI polish needs a provider", "Connect a provider in Account.", "danger");
        return;
      }
      if (button) button.classList.add("is-busy");
      try {
        const context = `Role: ${item.role || ""}\nCompany: ${item.company || ""}\nStack: ${item.stack || ""}`;
        const res = await fetch("/api/polish/section", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ bullets: item.bullets, context }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        if (Array.isArray(data.rewritten) && data.rewritten.length) {
          item.bullets = data.rewritten;
          if (typeof fire === "function") fire();
          if (formApi) formApi.rebuild();
          refreshAtsAdvisories();
          showBuildToast("Section polished ✦", `${data.rewritten.length} bullets rewritten.`, "success");
        }
      } catch (err) {
        showBuildToast("Polish failed", String(err.message || err), "danger");
      } finally {
        if (button) button.classList.remove("is-busy");
      }
    });
  }

  function applyPolishVisibility() {
    // Hide the polish buttons entirely when AI isn't available, to
    // avoid teasing users with non-functional UI.
    const hide = polishAvailable === false;
    $$(".bullet-polish").forEach((b) => b.toggleAttribute("hidden", hide));
    $$(".section-polish").forEach((b) => b.toggleAttribute("hidden", hide));
  }

  function shortText(s, n) {
    s = String(s || "");
    return s.length <= n ? s : s.slice(0, n - 1) + "…";
  }

  // ──────────────────────────────────────────────────────────────────────
  //  FIRST-RUN TOUR — shows once per browser, dismissable. Spotlight
  //  cuts out the target element so the user knows what each step is
  //  pointing at. Persisted via localStorage cv.editor.tourCompleted.
  // ──────────────────────────────────────────────────────────────────────
  const TOUR_KEY = "cv.editor.tourCompleted";
  // Each step references an i18n key (resolved at show-time via OnePagerI18n,
  // with the English fallback baked in here so the tour works even if the
  // string table fails to load). Stage 9 adds the AI-key + tailor-to-job
  // steps so the (previously under-explained) AI flow is covered end-to-end.
  const TOUR_STEPS = [
    {
      target: "#form-host",
      titleKey: "tour.form.title",
      bodyKey: "tour.form.body",
      title: "Edit your CV in the form",
      body: "Every field has a labelled input. Add, remove, and reorder entries — your changes are saved to your account.",
      placement: "right",
    },
    {
      target: "#import-btn",
      titleKey: "tour.import.title",
      bodyKey: "tour.import.body",
      title: "Import your existing resume",
      body: "Have a LinkedIn PDF or an old resume? Drop the PDF (or paste text) into <strong>Import</strong> and your selected AI provider maps it into the form in seconds.",
      placement: "left",
    },
    {
      target: "#import-btn",
      titleKey: "tour.aikey.title",
      bodyKey: "tour.aikey.body",
      title: "Connect your AI key",
      body: "AI extract, cover letters, and tailoring use the selected provider. Managed credits are Anthropic-only; provider keys are managed in <strong>Account</strong>, encrypted, and never logged.",
      placement: "left",
    },
    {
      target: "#tailor-btn",
      titleKey: "tour.tailor.title",
      bodyKey: "tour.tailor.body",
      title: "Tailor to a job",
      body: "Open <strong>Tailor to a job</strong> to paste a job description, see an ATS match score, and tailor a copy of your CV to it — your original stays untouched.",
      placement: "left",
    },
    {
      target: "#build-btn",
      titleKey: "tour.build.title",
      bodyKey: "tour.build.body",
      title: "Build the PDF",
      body: "<strong>Build PDF</strong> renders a 1-page A4 and opens it. The build refuses to ship a 2-pager, so you always know it'll fit.",
      placement: "left",
    },
  ];

  // Resolve an i18n string by key, falling back to a provided default. Safe
  // before OnePagerI18n has loaded (returns the fallback).
  function tr(key, fallback, vars) {
    if (window.OnePagerI18n && key) {
      const v = window.OnePagerI18n.t(key, vars);
      if (v && v !== key) return v;
    }
    if (fallback && vars) {
      let s = fallback;
      Object.keys(vars).forEach((k) => {
        s = s.replace(new RegExp("\\{" + k + "\\}", "g"), vars[k]);
      });
      return s;
    }
    return fallback != null ? fallback : key;
  }

  // ──────────────────────────────────────────────────────────────────────
  //  FORM MODE — Focus shows one section; Overview preserves the complete
  //  form DOM and the existing collapse/scroll contracts. Mode is global,
  //  while the active section is namespaced per CV variant.
  // ──────────────────────────────────────────────────────────────────────
  const SECTION_LABELS_DE = {
    header: "Kopfzeile",
    structure: "Aufbau",
    experience: "Berufserfahrung",
    education: "Ausbildung",
    skills: "Kompetenzen",
    projects: "Projekte",
    leadership: "Führung",
    others: "Weitere Angaben",
  };

  function editorLanguage() {
    if (window.OnePagerI18n && typeof window.OnePagerI18n.getLang === "function") {
      return window.OnePagerI18n.getLang();
    }
    return document.documentElement.getAttribute("lang") || "en";
  }

  function editorCopy(en, de) {
    return editorLanguage() === "de" ? de : en;
  }

  function localizedSectionLabel(key, fallback) {
    if (editorLanguage() === "de" && SECTION_LABELS_DE[key]) return SECTION_LABELS_DE[key];
    return fallback || key.charAt(0).toUpperCase() + key.slice(1);
  }

  function activeSectionStorageKey() {
    return `${ACTIVE_SECTION_KEY}::${activeCvId || "default"}`;
  }

  function loadActiveFormSection() {
    try {
      return localStorage.getItem(activeSectionStorageKey()) || null;
    } catch (_) {
      return null;
    }
  }

  function saveActiveFormSection(key) {
    if (!key) return;
    try {
      localStorage.setItem(activeSectionStorageKey(), key);
    } catch (_) {}
  }

  function formSections() {
    return $$("#form-host .form-section[data-section-key]");
  }

  function sectionDisplayLabel(section) {
    if (!section) return "";
    const key = section.dataset.sectionKey || "";
    const heading = section.querySelector(".form-section-title, .form-section-head h3");
    return localizedSectionLabel(key, heading ? heading.textContent.trim() : key);
  }

  function resolveActiveFormSection(sections = formSections()) {
    if (!sections.length) return null;
    const requested = activeFormSection || loadActiveFormSection();
    const active = sections.find((section) => section.dataset.sectionKey === requested) ||
      sections.find((section) => section.dataset.sectionKey === "header") ||
      sections[0];
    activeFormSection = active.dataset.sectionKey;
    saveActiveFormSection(activeFormSection);
    return active;
  }

  function updateFocusSectionNav(sections = formSections(), active = null) {
    const nav = $("#focus-section-nav");
    if (!nav) return;
    const isActive = viewMode === "form" && formMode === "focus" && sections.length > 0;
    nav.toggleAttribute("hidden", !isActive);

    const current = active || resolveActiveFormSection(sections);
    const index = current ? sections.indexOf(current) : -1;
    const currentNumber = index >= 0 ? index + 1 : 1;
    const total = Math.max(1, sections.length);
    const label = $("#focus-section-label");
    const count = $("#focus-section-count");
    const progress = $("#focus-section-progress");
    const fill = progress && progress.querySelector("span");
    const prev = $("#focus-section-prev");
    const next = $("#focus-section-next");

    if (label) label.textContent = current ? sectionDisplayLabel(current) : editorCopy("Section", "Abschnitt");
    if (count) count.textContent = `${currentNumber} / ${total}`;
    if (progress) {
      progress.setAttribute("aria-valuemax", String(total));
      progress.setAttribute("aria-valuenow", String(currentNumber));
      progress.setAttribute(
        "aria-valuetext",
        editorCopy(`Section ${currentNumber} of ${total}`, `Abschnitt ${currentNumber} von ${total}`),
      );
    }
    if (fill) fill.style.width = `${(currentNumber / total) * 100}%`;
    if (prev) prev.disabled = !isActive || index <= 0;
    if (next) next.disabled = !isActive || index < 0 || index >= sections.length - 1;
  }

  function updateFormModeCopy() {
    const toggle = $("#form-mode-toggle");
    if (toggle) toggle.setAttribute("aria-label", editorCopy("Form layout", "Formularansicht"));
    const focus = $('.form-mode-btn[data-form-mode="focus"]');
    const overview = $('.form-mode-btn[data-form-mode="overview"]');
    if (focus) focus.textContent = "Focus";
    if (overview) overview.textContent = editorCopy("Overview", "Übersicht");

    const nav = $("#focus-section-nav");
    if (nav) nav.setAttribute("aria-label", editorCopy("Section navigation", "Abschnittsnavigation"));
    const prev = $("#focus-section-prev");
    const next = $("#focus-section-next");
    if (prev) {
      prev.setAttribute("aria-label", editorCopy("Previous section", "Vorheriger Abschnitt"));
      const label = prev.querySelector(".focus-section-step-label");
      if (label) label.textContent = editorCopy("Previous", "Zurück");
    }
    if (next) {
      next.setAttribute("aria-label", editorCopy("Next section", "Nächster Abschnitt"));
      const label = next.querySelector(".focus-section-step-label");
      if (label) label.textContent = editorCopy("Next", "Weiter");
    }
    const progress = $("#focus-section-progress");
    if (progress) progress.setAttribute("aria-label", editorCopy("Form section progress", "Fortschritt der Formularabschnitte"));
  }

  function updateViewHint() {
    const hint = $("#view-tabs-hint");
    if (!hint) return;
    if (viewMode === "yaml") {
      hint.textContent = editorCopy(
        "Raw YAML — full power. Switch to Form for guided editing.",
        "YAML direkt bearbeiten. Für geführtes Bearbeiten zu Formular wechseln.",
      );
    } else if (formMode === "focus") {
      hint.textContent = editorCopy(
        "One section at a time. Use the outline or section controls to move.",
        "Ein Abschnitt nach dem anderen. Navigation über Gliederung oder Abschnittssteuerung.",
      );
    } else {
      hint.textContent = editorCopy(
        "All sections in one scrollable overview. Collapse sections to reduce detail.",
        "Alle Abschnitte in einer Übersicht. Details lassen sich einklappen.",
      );
    }
  }

  function applyFormMode() {
    const sections = formSections();
    const active = resolveActiveFormSection(sections);
    document.body.dataset.formMode = formMode;

    // Collapsed state belongs to Overview. Focus must always expose the active
    // section's fields, while returning to Overview restores the persisted
    // collapse choices without rewriting them.
    if (formMode === "overview") applyCollapsedState();

    const toggle = $("#form-mode-toggle");
    if (toggle) toggle.toggleAttribute("hidden", viewMode !== "form");
    $$(".form-mode-btn[data-form-mode]").forEach((button) => {
      const selected = button.dataset.formMode === formMode;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", selected ? "true" : "false");
    });

    sections.forEach((section) => {
      const selected = !!active && section === active;
      section.toggleAttribute("hidden", formMode === "focus" && !selected);
      if (formMode === "focus" && selected) {
        section.setAttribute("data-focus-active", "true");
        const head = section.querySelector(".form-section-head.is-collapsible");
        const body = section.querySelector(".form-section-body");
        if (head && body) {
          head.setAttribute("aria-expanded", "true");
          body.removeAttribute("hidden");
          section.classList.remove("is-collapsed");
          section.dataset.collapsed = "false";
        }
      } else {
        section.removeAttribute("data-focus-active");
      }
    });
    updateFormModeCopy();
    updateFocusSectionNav(sections, active);
    updateViewHint();
    updateCollapseAllButton();
    updateOutlineActive();
  }

  function activateFormSection(key, opts = {}) {
    const sections = formSections();
    const target = sections.find((section) => section.dataset.sectionKey === key);
    if (!target) return false;
    activeFormSection = key;
    saveActiveFormSection(key);
    applyFormMode();

    if (viewMode === "form" && formMode === "focus" && opts.resetScroll !== false) {
      const host = $("#form-host");
      requestAnimationFrame(() => {
        if (!host) return;
        const behavior = PREFERS_REDUCED_MOTION.matches ? "auto" : (opts.behavior || "smooth");
        try {
          host.scrollTo({ top: 0, behavior });
        } catch (_) {
          host.scrollTop = 0;
        }
      });
    }
    if (opts.scrollPreview !== false) {
      pendingScrollTarget = key;
      if (scrollPreviewToSection(key)) pendingScrollTarget = null;
    }
    highlightActiveFormSection(key);
    window.dispatchEvent(new CustomEvent("cv:layout-changed", { detail: { reason: "focus-section" } }));
    return true;
  }

  function stepFormSection(delta) {
    const sections = formSections();
    const active = resolveActiveFormSection(sections);
    const index = active ? sections.indexOf(active) : -1;
    const nextIndex = clamp(index + delta, 0, sections.length - 1);
    if (index < 0 || nextIndex === index) return;
    activateFormSection(sections[nextIndex].dataset.sectionKey);
  }

  function setFormMode(next) {
    if (!["focus", "overview"].includes(next)) return;
    cancelOutlineHover();
    formMode = next;
    try {
      localStorage.setItem(FORM_MODE_KEY, formMode);
    } catch (_) {}
    applyFormMode();
    window.dispatchEvent(new CustomEvent("cv:layout-changed", { detail: { reason: "form-mode" } }));
  }

  function setupFormModes() {
    $$(".form-mode-btn[data-form-mode]").forEach((button) => {
      button.addEventListener("click", () => setFormMode(button.dataset.formMode));
    });
    const prev = $("#focus-section-prev");
    const next = $("#focus-section-next");
    if (prev) prev.addEventListener("click", () => stepFormSection(-1));
    if (next) next.addEventListener("click", () => stepFormSection(1));
    window.addEventListener("cv:form-rebuilt", () => requestAnimationFrame(applyFormMode));
    applyFormMode();
  }

  // Fire-and-forget activation analytics. The server records it ONLY when
  // POSTHOG_KEY is configured (no-op otherwise); this never throws and never
  // blocks the UI. The server also captures first_export on the build route —
  // PostHog dedups, so the client ping is a belt-and-braces signal.
  function trackEvent(event) {
    try {
      fetch("/api/analytics/event", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event: event }),
        keepalive: true,
      }).catch(() => {});
    } catch (_) {
      /* analytics must never break a flow */
    }
  }

  // ──────────────────────────────────────────────────────────────────────
  //  COLLAPSED SECTIONS — each section can be folded; state persisted
  //  per CV variant in localStorage. The form module fires
  //  ``cv:section-collapsed`` whenever the user toggles; we listen and
  //  re-apply the saved state after every form rebuild.
  // ──────────────────────────────────────────────────────────────────────
  function collapsedKey() { return `${COLLAPSED_KEY}::${activeCvId || "default"}`; }
  const collapsedMemory = {};
  function loadCollapsed() {
    const key = collapsedKey();
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const parsed = JSON.parse(raw);
        collapsedMemory[key] = parsed && typeof parsed === "object" ? parsed : {};
        return collapsedMemory[key];
      }
    } catch (_) {}
    return Object.assign({}, collapsedMemory[key] || {});
  }
  function saveCollapsed(state) {
    const storageKey = collapsedKey();
    const keys = state && typeof state === "object"
      ? Object.keys(state).filter((key) => state[key])
      : [];
    const next = {};
    keys.forEach((key) => { next[key] = true; });
    collapsedMemory[storageKey] = next;
    try {
      if (!keys.length) {
        localStorage.removeItem(storageKey);
        return;
      }
      localStorage.setItem(storageKey, JSON.stringify(next));
    } catch (_) {}
  }
  function applyCollapsedState() {
    const state = loadCollapsed();
    $$(".form-section[data-section-key]").forEach((sec) => {
      const key = sec.dataset.sectionKey;
      const head = sec.querySelector(".form-section-head.is-collapsible");
      const body = sec.querySelector(".form-section-body");
      if (!head || !body) return;
      const collapsed = !!state[key];
      head.setAttribute("aria-expanded", collapsed ? "false" : "true");
      body.toggleAttribute("hidden", collapsed);
      sec.classList.toggle("is-collapsed", collapsed);
      sec.dataset.collapsed = collapsed ? "true" : "false";
    });
    updateCollapseAllButton();
  }
  function collapsibleSections() {
    return $$(".form-section[data-section-key]").filter((sec) => (
      sec.querySelector(".form-section-head.is-collapsible") &&
      sec.querySelector(".form-section-body")
    ));
  }
  function allSectionsCollapsed() {
    const sections = collapsibleSections();
    if (!sections.length) return false;
    const state = loadCollapsed();
    return sections.every((sec) => !!state[sec.dataset.sectionKey]);
  }
  function collapsedSummary() {
    const sections = collapsibleSections();
    const state = loadCollapsed();
    const collapsed = sections.filter((sec) => !!state[sec.dataset.sectionKey]).length;
    return { total: sections.length, collapsed };
  }
  function setAllSectionsCollapsed(collapsed) {
    if (viewMode !== "form" || formMode !== "overview") return;
    const sections = collapsibleSections();
    const state = {};
    if (collapsed) {
      sections.forEach((sec) => {
        if (sec.dataset.sectionKey) state[sec.dataset.sectionKey] = true;
      });
    }
    saveCollapsed(state);
    applyCollapsedState();
  }
  function updateCollapseAllButton() {
    const btn = $("#collapse-all-btn");
    if (!btn) return;
    const summary = collapsedSummary();
    const active = viewMode === "form" && formMode === "overview" && summary.total > 0;
    btn.hidden = !active;
    btn.disabled = !active;
    if (!active) {
      btn.setAttribute("aria-pressed", "false");
      btn.setAttribute("aria-label", tr("palette.act.collapse_all", "Collapse all sections"));
      return;
    }
    const collapsed = summary.total > 0 && summary.collapsed === summary.total;
    const mixed = summary.collapsed > 0 && summary.collapsed < summary.total;
    const nextAction = collapsed
      ? tr("palette.act.expand_all", "Expand all sections")
      : tr("palette.act.collapse_all", "Collapse all sections");
    btn.setAttribute("aria-pressed", mixed ? "mixed" : collapsed ? "true" : "false");
    btn.setAttribute("aria-label", nextAction);
    btn.dataset.tooltip = nextAction;
    const label = btn.querySelector(".collapse-all-label");
    if (label) label.textContent = collapsed
      ? tr("app.expand_all_short", "Expand all")
      : tr("app.collapse_all_short", "Collapse all");
  }
  function setupCollapsedSections() {
    const btn = $("#collapse-all-btn");
    if (btn) {
      btn.addEventListener("click", () => {
        if (viewMode !== "form" || formMode !== "overview") return;
        setAllSectionsCollapsed(!allSectionsCollapsed());
      });
    }
    window.addEventListener("cv:section-collapsed", (e) => {
      const { key, collapsed } = e.detail || {};
      if (!key) return;
      const state = loadCollapsed();
      if (collapsed) state[key] = true;
      else delete state[key];
      saveCollapsed(state);
      applyCollapsedState();
    });

    // Re-apply state after every form rebuild. The form module itself
    // doesn't know which sections were collapsed, so we hook the
    // MutationObserver path: any change inside #form-host re-applies.
    const host = $("#form-host");
    if (host && typeof MutationObserver !== "undefined") {
      const obs = new MutationObserver(() => {
        applyCollapsedState();
        applyFormMode();
      });
      obs.observe(host, { childList: true, subtree: false });
    }
    // Initial pass after the form has mounted.
    setTimeout(() => {
      applyCollapsedState();
      applyFormMode();
    }, 200);
  }

  function setupTour() {
    if (localStorage.getItem(TOUR_KEY) === "true") return;
    const overlay = $("#tour");
    if (!overlay) return;
    let step = 0;
    // Focused element before the tour opened — restored on close.
    let restoreFocusTo = null;

    function show() {
      const def = TOUR_STEPS[step];
      const target = def && document.querySelector(def.target);
      if (!target) {
        // Target not yet in DOM (e.g. form-host hidden). Try the next one.
        step++;
        if (step >= TOUR_STEPS.length) return done();
        return show();
      }
      overlay.removeAttribute("hidden");
      $(".tour-step", overlay).textContent = tr(
        "tour.step_of",
        `Step ${step + 1} of ${TOUR_STEPS.length}`,
        { n: step + 1, total: TOUR_STEPS.length }
      );
      $(".tour-title", overlay).textContent = tr(def.titleKey, def.title);
      $(".tour-body", overlay).innerHTML = tr(def.bodyKey, def.body);
      const prev = $("#tour-prev", overlay);
      const next = $("#tour-next", overlay);
      $("#tour-skip", overlay).textContent = tr("tour.skip", "Skip tour");
      prev.disabled = step === 0;
      prev.style.opacity = step === 0 ? "0.4" : "1";
      prev.textContent = tr("tour.back", "Back");
      next.textContent =
        step === TOUR_STEPS.length - 1 ? tr("tour.done", "Got it") : tr("tour.next", "Next");

      // Dialog semantics: capture the pre-tour focus once, then move focus
      // INTO the dialog so Enter/Tab/Escape act on the tour rather than on
      // background controls.
      if (!restoreFocusTo && document.activeElement instanceof HTMLElement) {
        restoreFocusTo = document.activeElement;
      }
      // Focus the CARD (not the Next button) on every step change: focusing
      // Next on each advance yanked keyboard focus and skipped announcing the
      // step's content. tabindex="-1" makes the card programmatically
      // focusable so a screen reader announces the step title + body.
      const focusCard = $(".tour-card", overlay);
      if (focusCard) {
        if (!focusCard.hasAttribute("tabindex")) focusCard.setAttribute("tabindex", "-1");
        focusCard.focus({ preventScroll: true });
      }

      // Spotlight: clip-path with a hole over the target's rectangle.
      requestAnimationFrame(() => {
        const r = target.getBoundingClientRect();
        const pad = 6;
        const x1 = Math.max(0, Math.round(r.left - pad));
        const y1 = Math.max(0, Math.round(r.top - pad));
        const x2 = Math.min(window.innerWidth, Math.round(r.right + pad));
        const y2 = Math.min(window.innerHeight, Math.round(r.bottom + pad));
        const spot = $(".tour-spotlight", overlay);
        // Cut a hole using even-odd: outer rect minus inner rect.
        spot.style.clipPath = `polygon(
          0% 0%, 100% 0%, 100% 100%, 0% 100%, 0% 0%,
          ${x1}px ${y1}px, ${x1}px ${y2}px, ${x2}px ${y2}px, ${x2}px ${y1}px, ${x1}px ${y1}px
        )`;

        // Position the card next to the target.
        const card = $(".tour-card", overlay);
        const margin = 14;
        let cx, cy;
        if (def.placement === "right") {
          cx = Math.min(window.innerWidth - card.offsetWidth - 16, x2 + margin);
          cy = Math.max(16, y1);
        } else if (def.placement === "left") {
          cx = Math.max(16, x1 - card.offsetWidth - margin);
          cy = Math.min(window.innerHeight - card.offsetHeight - 16, Math.max(16, y2 + margin));
        } else {
          cx = Math.max(16, (x1 + x2) / 2 - card.offsetWidth / 2);
          cy = Math.min(window.innerHeight - card.offsetHeight - 16, y2 + margin);
        }
        card.style.left = `${cx}px`;
        card.style.top = `${cy}px`;
      });
    }

    function done() {
      overlay.setAttribute("hidden", "");
      localStorage.setItem(TOUR_KEY, "true");
      if (restoreFocusTo && document.contains(restoreFocusTo)) {
        try { restoreFocusTo.focus(); } catch (_) {}
      }
      restoreFocusTo = null;
    }
    function next() {
      step++;
      if (step >= TOUR_STEPS.length) done();
      else show();
    }
    function prev() {
      step = Math.max(0, step - 1);
      show();
    }

    $("#tour-skip", overlay).addEventListener("click", done);
    $("#tour-next", overlay).addEventListener("click", next);
    $("#tour-prev", overlay).addEventListener("click", prev);
    document.addEventListener("keydown", (e) => {
      if (overlay.hasAttribute("hidden")) return;
      // Only while focus is INSIDE the dialog — a global intercept hijacked
      // Enter/Arrows from background controls (e.g. #refresh-btn).
      if (!overlay.contains(document.activeElement)) return;
      if (e.key === "Escape") done();
      else if (e.key === "ArrowRight" || e.key === "Enter") { e.preventDefault(); next(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); prev(); }
      else if (e.key === "Tab") {
        // Keep Tab cycling inside the modal dialog.
        const btns = Array.from(overlay.querySelectorAll("button")).filter((b) => !b.disabled);
        if (!btns.length) return;
        const first = btns[0];
        const last = btns[btns.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });

    // Wait for the form to be populated before showing — otherwise the
    // first step's target measurements are wrong.
    setTimeout(show, 1500);
  }

  // ──────────────────────────────────────────────────────────────────────
  //  i18n — EN|DE toggle (Stage 9). The language is persisted in localStorage
  //  by OnePagerI18n; this loads the active language's strings, localises any
  //  [data-i18n] element + a few key labels, and injects an EN|DE switch into
  //  the topbar. Default EN. Tooltips/labels for the core actions are covered;
  //  the toggle re-localises live without a reload.
  // ──────────────────────────────────────────────────────────────────────
  function applyEditorI18n() {
    if (!window.OnePagerI18n) return;
    const t = window.OnePagerI18n.t;
    window.OnePagerI18n.apply(document);
    // Localise the high-traffic chrome labels + tooltips by id.
    const set = (sel, key, fallback, attr) => {
      const node = $(sel);
      if (!node) return;
      const val = t(key) && t(key) !== key ? t(key) : fallback;
      if (attr) node.setAttribute(attr, val);
      else {
        const span = node.querySelector("span:last-child");
        if (span && !span.hasAttribute("data-icon")) span.textContent = val;
      }
    };
    set("#save-btn", "app.save", "Save");
    set("#build-btn", "app.build", "Build PDF");
    set("#import-btn", "app.import", "Import", "aria-label");
    set("#tailor-btn", "workflow.ats.aria", "Score and optimize CV", "aria-label");
    set("#cover-letters-btn", "workflow.cover.title", "Cover letters", "aria-label");
    set("#jobs-btn", "workflow.jobs.aria", "Start with a job", "aria-label");
    set("#cv-new-btn", "app.new_cv", "New CV");
    set("#cv-download-btn", "app.download_yaml", "Download YAML");
    syncCvSwitcherLabel($("#cv-switcher-label"), activeCvName);
    const accentTrigger = $("#accent-trigger");
    const accentValue = $("#accent-value");
    if (accentTrigger && accentValue) {
      const value = (accentValue.textContent || "#111111").trim().toUpperCase();
      accentTrigger.setAttribute("aria-label", tr("app.accent_picker_aria", "Choose accent colour {value}", { value }));
    }
    updateFormModeCopy();
    updateFocusSectionNav();
    updateViewHint();
    if (editor) renderOutline();
    updateCollapseAllButton();
    const logout = $("#logout-btn");
    if (logout) logout.setAttribute("aria-label", t("app.sign_out") !== "app.sign_out" ? t("app.sign_out") : "Sign out");
  }

  function setupLanguage() {
    if (!window.OnePagerI18n) return;
    const i18n = window.OnePagerI18n;

    function wireToggle() {
      const wrap = $("#lang-toggle");
      if (!wrap) return;
      $$("#lang-toggle .lang-btn[data-lang]").forEach((b) => {
        if (b.dataset.bound === "true") return;
        b.dataset.bound = "true";
        const code = b.dataset.lang;
        b.addEventListener("click", () => {
          i18n.setLang(code).then(() => {
            document.documentElement.setAttribute("lang", i18n.getLang());
            applyEditorI18n();
            window.dispatchEvent(new CustomEvent("cv:i18n-changed"));
          });
        });
      });
    }

    i18n.load(i18n.getLang()).then(() => {
      document.documentElement.setAttribute("lang", i18n.getLang());
      wireToggle();
      applyEditorI18n();
      window.dispatchEvent(new CustomEvent("cv:i18n-changed"));
    });
  }

  function setupIssuesPill() {
    const pill = $("#issues-pill");
    if (!pill) return;
    pill.addEventListener("click", () => {
      // Switch to form view (issues only visible there) and scroll to
      // the first empty required input.
      if (viewMode !== "form") switchView("form");
      requestAnimationFrame(() => {
        const first = $('input[data-required="true"][data-empty="true"], textarea[data-required="true"][data-empty="true"]');
        if (first) {
          const section = first.closest(".form-section[data-section-key]");
          if (formMode === "focus" && section) {
            activateFormSection(section.dataset.sectionKey, { scrollPreview: false });
          }
          requestAnimationFrame(() => {
            first.scrollIntoView({ behavior: "smooth", block: "center" });
            first.focus({ preventScroll: true });
          });
        }
      });
    });
    refreshIssuesPill();
  }

  function refreshIssuesPill() {
    const pill = $("#issues-pill");
    if (!pill) return;
    const empties = $$('input[data-required="true"][data-empty="true"], textarea[data-required="true"][data-empty="true"]');
    const count = empties.length;
    if (count === 0) {
      pill.setAttribute("hidden", "");
      return;
    }
    pill.removeAttribute("hidden");
    const countEl = $(".issues-count", pill);
    const labelEl = $(".issues-label", pill);
    if (countEl) countEl.textContent = String(count);
    if (labelEl) labelEl.textContent = count === 1 ? "issue" : "issues";
    if (window.renderIcons) window.renderIcons(pill);
  }

  function hasRequiredFormGaps() {
    if (viewMode !== "form") return false;
    return $$('input[data-required="true"][data-empty="true"], textarea[data-required="true"][data-empty="true"]').length > 0;
  }

  // ──────────────────────────────────────────────────────────────────────
  //  PER-SECTION COMPLETENESS HINTS (Stage 7)
  //
  //  A subtle, non-blocking nudge per form section, derived from the SAME
  //  required-field validation the issues pill uses (``data-required`` /
  //  ``data-empty`` markers set by form.js) plus a light metrics heuristic on
  //  bullets. Never an error — purely advisory ("missing dates", "add metrics
  //  to N bullets"). Re-run wherever ``refreshIssuesPill`` runs and after a
  //  form (re)build. The badge slot lives in each section head (form.js).
  // ──────────────────────────────────────────────────────────────────────

  // True when a bullet reads as "quantified" — has a digit (15, 3x, 40%, $2M,
  // 1,000) so we don't nudge bullets that already carry a metric. Deliberately
  // permissive: a false negative just shows a soft nudge, never blocks a save.
  function bulletHasMetric(text) {
    return /\d/.test(text || "");
  }

  // Build the hint list for one section element. Returns [] when complete.
  // NOTE: the page's $/$$ helpers always query `document` (no scope arg), so
  // these per-section reads use native scoped querySelectorAll deliberately.
  function sectionHintsFor(section) {
    const hints = [];
    // Empty required fields, classified by their label so the nudge is concrete.
    const empties = section.querySelectorAll(
      'input[data-required="true"][data-empty="true"], textarea[data-required="true"][data-empty="true"]',
    );
    let dateGaps = 0;
    let otherGaps = 0;
    empties.forEach((inp) => {
      const fieldLabel = inp.closest(".form-field");
      const labelEl = fieldLabel ? fieldLabel.querySelector(".form-field-label") : null;
      const label = (labelEl ? labelEl.textContent : "").toLowerCase();
      if (/start|end|date/.test(label)) dateGaps += 1;
      else otherGaps += 1;
    });
    if (dateGaps > 0) hints.push("missing dates");
    if (otherGaps > 0) hints.push(otherGaps === 1 ? "1 required field empty" : `${otherGaps} required fields empty`);
    // Bullets without a metric — only nudge non-empty bullets so a blank one
    // (which the user is still filling) doesn't generate noise.
    const bullets = Array.from(section.querySelectorAll(".bullet-input"));
    if (bullets.length) {
      const thin = bullets.filter((b) => b.value.trim() && !bulletHasMetric(b.value)).length;
      if (thin > 0) hints.push(thin === 1 ? "add a metric to 1 bullet" : `add metrics to ${thin} bullets`);
    }
    return hints;
  }

  function refreshSectionHints() {
    const host = $("#form-host");
    if (!host) return;
    const sections = host.querySelectorAll(".form-section");
    sections.forEach((section) => {
      const head = section.querySelector(".form-section-head");
      if (!head) return;
      let slot = head.querySelector(".form-section-hint");
      const hints = sectionHintsFor(section);
      if (!hints.length) {
        if (slot) slot.setAttribute("hidden", "");
        return;
      }
      // Lazily create the hint slot if form.js's version is absent (defensive).
      if (!slot) {
        slot = document.createElement("span");
        slot.className = "form-metric form-section-hint";
        slot.dataset.metric = "hint";
        const metrics = head.querySelector(".form-section-metrics");
        const chevron = metrics ? metrics.querySelector(".form-section-chevron") : null;
        if (metrics && chevron) metrics.insertBefore(slot, chevron);
        else if (metrics) metrics.appendChild(slot);
        else head.appendChild(slot);
      }
      slot.removeAttribute("hidden");
      slot.textContent = hints.join(" · ");
      slot.setAttribute("data-tooltip", "Completeness nudge — optional, never blocks your save");
    });
  }

  function refreshAtsAdvisories() {
    if (formApi && typeof formApi.setAtsAdvisories === "function") {
      formApi.setAtsAdvisories(atsState.sections || emptyAtsSections());
    }
  }

  function updateTailorLinks() {
    const params = new URLSearchParams();
    const contextJobId = (activeJob && activeJob.id) || applicationContextJobId;
    const sourceCvId = applicationContextSourceCvId || activeCvId;
    if (activeCvId) {
      params.set("id", activeCvId);
      params.set("cv_id", activeCvId);
    }
    if (contextJobId) params.set("job_id", contextJobId);
    if (sourceCvId) params.set("source_cv_id", sourceCvId);
    const href = `/tailor${params.toString() ? `?${params.toString()}` : ""}`;
    ["#tailor-btn", "#ats-popover .ats-detail-foot a[href^='/tailor']"].forEach((selector) => {
      const link = $(selector);
      if (link) link.setAttribute("href", href);
    });
  }

  // ──────────────────────────────────────────────────────────────────────
  //  LIVE ATS BADGE (Stage 7)
  //
  //  Shows the active CV's ATS fit against its LINKED saved job. The link is
  //  ``documents.job_id`` (surfaced per-row on /api/cvs); when present we fetch
  //  the job's JD, then POST /api/ats-score {cv_id, jd_text}. The result is
  //  cached by a hash of (cv content + job id) so re-opening an unchanged CV is
  //  free, and the call is debounced so typing doesn't hammer the endpoint.
  //  ATS scoring is FREE and degrades to the deterministic scorer with no key,
  //  so this never hard-fails the editor. Clicking the badge → /tailor.
  // ──────────────────────────────────────────────────────────────────────

  // Tiny stable string hash (djb2) for the content-addressed badge cache.
  function atsHash(str) {
    let h = 5381;
    for (let i = 0; i < str.length; i++) h = ((h << 5) + h + str.charCodeAt(i)) | 0;
    return String(h >>> 0);
  }

  function profileLabel() {
    const serverProfile = atsState && atsState.payload && atsState.payload.profile;
    return (serverProfile && serverProfile.label)
      || atsProfileLabel(atsProfile, ATS_PROFILES[atsProfile] || ATS_PROFILES.semantic_fit);
  }

  function atsProfileLabel(key, item) {
    if (key === "keyword_screen") return tr("tailor.profile.keyword_screen", "Keyword screen");
    if (key === "strict_requirements") return tr("tailor.profile.strict_requirements", "Strict requirements");
    if (key === "semantic_fit") return tr("tailor.profile.semantic_fit", "Semantic fit");
    return (item && item.label) || key;
  }

  function atsCompareProfiles() {
    return Object.keys(ATS_PROFILES).filter((key) => key !== atsProfile);
  }

  function emptyAtsSections() {
    return {
      header: [],
      experience: [],
      education: [],
      skills: [],
      projects: [],
      leadership: [],
      others: [],
    };
  }

  function sectionForAtsTerm(text) {
    const s = String(text || "").toLowerCase();
    if (/\b(degree|university|college|certification|certificate|academic|gpa|master|bachelor|phd)\b/.test(s)) return "education";
    if (/\b(year|years|production|migration|ownership|stakeholder|client|domain|process|operations|enterprise)\b/.test(s)) return "experience";
    if (/\b(open[- ]?source|portfolio|side project|github|repository|repo|published project)\b/.test(s)) return "projects";
    if (/\b(lead|mentor|manage|manager|team|stakeholder|hiring|roadmap|ownership)\b/.test(s)) return "leadership";
    if (/\b(publication|paper|award|patent|conference)\b/.test(s)) return "others";
    return "skills";
  }

  function normalizeAtsPayload(data, job, key) {
    const payload = data && typeof data === "object" ? data : {};
    const score = typeof payload.score === "number" ? payload.score : null;
    const source = payload.source || "deterministic";
    const summary = payload.summary || {};
    const returnedProfile = payload.profile && payload.profile.id && ATS_PROFILES[payload.profile.id]
      ? payload.profile.id
      : atsProfile;
    return {
      status: score == null ? "unscored" : "ready",
      cvId: activeCvId,
      jobId: job && job.id,
      contentHash: key || null,
      scoredAt: new Date(),
      source,
      score,
      payload,
      sections: mapAtsPayloadToSections(payload, source, false),
      stale: false,
      message: score == null
        ? (payload.notes || "Not enough job requirements to score reliably.")
        : `${source === "rubric" ? "Rubric" : "Deterministic"} score for ${job && job.title ? job.title : "the linked job"}`,
      summary,
      profile: returnedProfile,
    };
  }

  function withAtsStaleFlag(state, stale) {
    if (!state || !stale) return state;
    const payload = state.payload || {};
    const source = state.source || payload.source || "deterministic";
    return Object.assign({}, state, {
      stale: true,
      sections: mapAtsPayloadToSections(payload, source, true),
    });
  }

  function mapAtsPayloadToSections(payload, source, stale) {
    const sections = emptyAtsSections();
    const gaps = Array.isArray(payload && payload.gaps) ? payload.gaps : [];
    gaps.forEach((gap, idx) => {
      const term = typeof gap === "string" ? gap : gap && gap.term;
      if (!term) return;
      const sectionKey = sectionForAtsTerm(term);
      const importance = typeof gap === "object" && gap ? gap.importance || "" : "";
      sections[sectionKey].push({
        id: `gap:${sectionKey}:${idx}:${term}`,
        kind: "gap",
        source,
        sectionKey,
        confidence: "direct",
        term,
        importance,
        stale,
        text: importance
          ? `${term} is ${importance.replace("_", " ")} in the job but not evidenced in the saved CV.`
          : `${term} appears in the job but is not evidenced in the saved CV.`,
      });
    });
    const suggestions = Array.isArray(payload && payload.suggestions) ? payload.suggestions : [];
    suggestions.forEach((suggestion, idx) => {
      const text = String(suggestion || "").trim();
      if (!text) return;
      const sectionKey = sectionForAtsTerm(text);
      sections[sectionKey].push({
        id: `suggestion:${sectionKey}:${idx}`,
        kind: "suggestion",
        source,
        sectionKey,
        confidence: "inferred",
        term: text,
        importance: "",
        stale,
        text,
      });
    });
    return sections;
  }

  function setAtsState(next) {
    atsState = Object.assign({}, atsState, next || {});
    if (!atsState.sections) atsState.sections = emptyAtsSections();
    updateTailorLinks();
    renderAtsBadge(atsState.status, atsState.score, activeJob && activeJob.title);
    renderAtsDetail();
    if (formApi && typeof formApi.setAtsAdvisories === "function") {
      formApi.setAtsAdvisories(atsState.sections);
    }
  }

  function normalizeAtsPacket(lineage, row) {
    if (!lineage || typeof lineage !== "object") return null;
    const packet = Object.assign({}, lineage);
    if (!packet.packet_id && packet.id) packet.packet_id = packet.id;
    if (!packet.document_role && row && packet.tailored_cv_id === row.id) packet.document_role = "tailored_cv";
    if (!packet.status && packet.stored_status) packet.status = packet.stored_status;
    if (packet.score == null && packet.score_snapshot && typeof packet.score_snapshot.score === "number") {
      packet.score = packet.score_snapshot.score;
    }
    return packet;
  }

  function atsPacketBlockers(lineage, row) {
    const packet = normalizeAtsPacket(lineage, row);
    const blockers = [];
    if (!packet) return ["packet_lineage_missing"];
    const status = String(packet.status || "");
    const staleReasons = Array.isArray(packet.stale_reasons) ? packet.stale_reasons : [];
    if (packet.document_role !== "tailored_cv") blockers.push("packet_document_role_invalid");
    if (!row || packet.job_id !== row.job_id) blockers.push("packet_job_mismatch");
    if (packet.tailored_cv_id !== (row && row.id)) blockers.push("packet_tailored_cv_mismatch");
    if (packet.archived_at || status === "archived") blockers.push("packet_archived");
    if (status === "stale" || staleReasons.length) blockers.push.apply(blockers, staleReasons.length ? staleReasons : ["packet_stale"]);
    if (status && status !== "packet_created") blockers.push("packet_status_" + status);
    [
      ["source_cv_id", "source_cv_missing"],
      ["source_cv_hash", "source_cv_hash_missing"],
      ["job_content_hash", "job_content_hash_missing"],
      ["tailored_cv_id", "tailored_cv_missing"],
      ["tailored_cv_hash", "tailored_cv_hash_missing"],
      ["cover_letter_id", "cover_letter_missing"],
      ["cover_letter_hash", "cover_letter_hash_missing"],
      ["score_system_id", "score_system_missing"],
      ["score", "score_snapshot_missing"],
      ["prompt_version", "packet_prompt_version_missing"],
      ["packet_version", "packet_version_missing"],
      ["cover_letter_review_id", "cover_letter_review_id_missing"],
      ["cover_letter_review_confirmed_at", "cover_letter_review_timestamp_missing"],
    ].forEach(([field, code]) => {
      if (!packet[field]) blockers.push(code);
    });
    if (packet.prompt_version && packet.prompt_version !== "jobs_packet.v1") {
      blockers.push("packet_prompt_version_stale");
    }
    const version = Number(packet.packet_version || 0);
    if (!Number.isFinite(version) || version < 1) blockers.push("packet_version_stale");
    if (packet.cover_letter_review_status !== "confirmed") blockers.push("cover_letter_review_unconfirmed");
    return Array.from(new Set(blockers));
  }

  function isUsableAtsPacket(lineage, row) {
    return atsPacketBlockers(lineage, row).length === 0;
  }

  function packetBlockedAtsState(lineage, row) {
    const packet = normalizeAtsPacket(lineage, row);
    const status = packet && packet.status ? String(packet.status) : "unusable";
    const staleReasons = Array.isArray(packet && packet.stale_reasons) ? packet.stale_reasons : [];
    const blockers = atsPacketBlockers(packet, row);
    const archived = !!(packet && packet.archived_at) || status === "archived";
    const message = archived
      ? "Archived packet cannot be scored as apply-ready."
      : staleReasons.length
        ? "Packet is stale: " + staleReasons.join(", ")
        : "Packet lineage is not apply-ready for live ATS scoring: " + blockers.join(", ");
    return {
      status: archived ? "archived" : (status === "stale" || staleReasons.length ? "stale" : "blocked"),
      cvId: row && row.id,
      jobId: row && row.job_id,
      source: null,
      score: null,
      payload: { notes: message },
      sections: emptyAtsSections(),
      stale: false,
      message,
      packetLineage: packet || null,
      packetUsable: false,
    };
  }

  function markAtsStale() {
    if (atsState.status !== "ready" && atsState.status !== "unscored") return;
    const staleSections = {};
    Object.entries(atsState.sections || {}).forEach(([key, items]) => {
      staleSections[key] = (items || []).map((item) => Object.assign({}, item, { stale: true }));
    });
    setAtsState({ stale: true, sections: staleSections });
  }

  // Paint the badge in one of: empty, loading, ready, unscored, error.
  function renderAtsBadge(state, score, jobTitle) {
    const badge = $("#ats-badge");
    if (!badge) return;
    badge.removeAttribute("hidden");
    badge.dataset.state = state;
    const lineage = atsState.packetLineage || activePacketLineage || null;
    if (lineage && lineage.packet_id) {
      badge.dataset.packetId = lineage.packet_id;
      badge.dataset.packetState = lineage.status || "";
      badge.dataset.packetUsable = atsState.packetUsable === false ? "false" : "true";
    } else {
      delete badge.dataset.packetId;
      delete badge.dataset.packetState;
      delete badge.dataset.packetUsable;
    }
    const scoreEl = badge.querySelector(".ats-badge-score");
    const labelEl = badge.querySelector(".ats-badge-label");
    let band = "neutral";
    if (state === "ready" && typeof score === "number") {
      band = score >= 70 ? "good" : score >= 45 ? "fair" : "poor";
      if (scoreEl) scoreEl.textContent = String(score);
      if (labelEl) labelEl.textContent = atsState.stale ? "ATS stale" : "ATS";
    } else if (state === "error") {
      if (scoreEl) scoreEl.textContent = "—";
      if (labelEl) labelEl.textContent = "ATS";
    } else if (state === "empty") {
      if (scoreEl) scoreEl.textContent = "—";
      if (labelEl) labelEl.textContent = "ATS";
    } else if (state === "unscored") {
      if (scoreEl) scoreEl.textContent = "n/a";
      if (labelEl) labelEl.textContent = "ATS";
    } else if (state === "stale" || state === "archived" || state === "blocked") {
      if (scoreEl) scoreEl.textContent = "—";
      if (labelEl) labelEl.textContent = state === "archived" ? "ATS archived" : (state === "stale" ? "ATS stale" : "ATS blocked");
    } else {
      if (scoreEl) scoreEl.textContent = "··";
      if (labelEl) labelEl.textContent = "ATS";
    }
    badge.dataset.band = band;
    const title = jobTitle || (activeJob && activeJob.title) || "the linked job";
    badge.setAttribute(
      "data-tooltip",
      state === "ready"
        ? `CV Copilot ATS fit ${score} / 100 for ${title} — ${profileLabel()}`
        : state === "loading"
          ? `Scoring CV Copilot ATS fit for ${title}`
          : state === "empty"
            ? "No linked job — open the score workspace or generate a job-linked CV"
            : state === "stale"
              ? "Packet is stale; regenerate before relying on this ATS fit"
              : state === "archived"
                ? "Archived packet cannot be scored as apply-ready"
                : state === "blocked"
                  ? "Packet lineage is not apply-ready for live ATS scoring"
            : state === "unscored"
              ? "Job description is too thin to score reliably"
              : `Couldn't score ATS fit for ${title}`,
    );
    if (window.renderIcons) window.renderIcons(badge);
  }

  function renderAtsDetail() {
    const box = $("#ats-detail");
    const hint = $("#ats-popover-hint");
    if (!box) return;
    const payload = atsState.payload || {};
    const source = atsState.source || payload.source || "deterministic";
    const serverProfile = payload.profile && payload.profile.label ? payload.profile : null;
    const profile = serverProfile || ATS_PROFILES[atsState.profile || atsProfile] || ATS_PROFILES.semantic_fit;
    const jobTitle = activeJob && activeJob.title ? activeJob.title : tr("ats.no_linked_job", "No linked job");
    const scoreText = typeof atsState.score === "number" ? `${atsState.score}/100` : tr("ats.not_scored", "Not scored");
    const mustHave = typeof payload.tier1_coverage === "number"
      ? `${payload.tier1_coverage}% ${tr("tailor.report.must_have", "must-have coverage").toLowerCase()}`
      : tr("ats.coverage_unavailable", "Must-have coverage unavailable");
    const gaps = Array.isArray(payload.gaps) ? payload.gaps : [];
    const matched = Array.isArray(payload.matched) ? payload.matched : [];
    if (hint) {
      hint.textContent = atsState.status === "empty"
        ? tr("ats.no_linked_job_hint", "No linked job for this CV. The scorer needs a job description.")
        : atsState.status === "stale" || atsState.status === "archived" || atsState.status === "blocked"
          ? (atsState.message || tr("ats.lineage_not_ready", "Packet lineage is not apply-ready."))
        : `${jobTitle} · ${atsProfileLabel(atsState.profile || atsProfile, profile)} · ${source}`;
    }
    const profileOptions = Object.entries(ATS_PROFILES).map(([key, item]) =>
      `<option value="${escapeHtml(key)}"${key === atsProfile ? " selected" : ""}>${escapeHtml(atsProfileLabel(key, item))}</option>`,
    ).join("");
    const gapList = gaps.slice(0, 5).map((gap) => {
      const term = typeof gap === "string" ? gap : gap && gap.term;
      const importance = typeof gap === "object" && gap && gap.importance ? ` · ${gap.importance.replace("_", " ")}` : "";
      return `<li><span>${escapeHtml(term || "")}</span><small>${escapeHtml(importance)}</small></li>`;
    }).join("");
    const matchedList = matched.slice(0, 5).map((item) =>
      `<li><span>${escapeHtml(item.term || "")}</span><small>${escapeHtml(item.importance || "")}</small></li>`,
    ).join("");
    const comparison = payload.profile_results && typeof payload.profile_results === "object"
      ? payload.profile_results
      : {};
    const activeProfile = atsState.profile || atsProfile;
    const comparisonRows = Object.entries(ATS_PROFILES).map(([key, item]) => {
      const data = key === activeProfile ? payload : comparison[key];
      const rowScore = data && typeof data.score === "number" ? String(data.score) : "—";
      const rowSource = data && data.source === "rubric"
        ? tr("tailor.profile.source.ai", "AI rubric")
        : key === "strict_requirements"
          ? tr("tailor.profile.source.must_have", "Must-have")
          : key === "semantic_fit"
            ? tr("tailor.profile.source.keyword_fallback", "Keyword fallback")
            : tr("tailor.profile.source.keyword", "Keyword");
      const active = key === activeProfile ? " is-active" : "";
      return `
        <div class="ats-profile-row${active}">
          <span>${escapeHtml(atsProfileLabel(key, item))}</span>
          <strong>${escapeHtml(rowScore)}</strong>
          <small>${escapeHtml(rowSource)}</small>
        </div>
      `;
    }).join("");
    const profileExplanation = [
      profile.purpose || profile.description,
      profile.explanation,
    ].filter(Boolean).join(" ");
    const profileFacts = [
      [tr("tailor.profile.best_for", "Best for"), profile.best_for],
      [tr("tailor.profile.method", "Method"), profile.method],
      [tr("tailor.profile.proof", "Proof"), profile.simulation === false ? tr("tailor.profile.external", "External profile") : tr("tailor.profile.simulation", "CV Copilot simulation")],
      [tr("tailor.profile.vendor", "Vendor"), profile.vendor_backed ? tr("tailor.profile.vendor_backed", "Vendor-backed") : tr("tailor.profile.not_vendor_backed", "Not vendor-backed")],
    ].filter((item) => item[1]).map(([label, value]) =>
      `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value).replace(/_/g, " "))}</dd></div>`,
    ).join("");
    box.innerHTML = `
      <div class="ats-detail-grid">
        <div class="ats-detail-score" data-band="${escapeHtml(typeof atsState.score === "number" ? (atsState.score >= 70 ? "good" : atsState.score >= 45 ? "fair" : "poor") : "neutral")}">
          <span>${escapeHtml(scoreText)}</span>
          <small>${escapeHtml(tr("ats.onepager_fit", "CV Copilot ATS fit"))}</small>
        </div>
        <label class="ats-profile-field">
          <span>${escapeHtml(tr("tailor.field.profile", "ATS score system"))}</span>
          <select id="ats-profile-select" aria-label="${escapeHtml(tr("tailor.field.profile", "ATS score system"))}">${profileOptions}</select>
        </label>
      </div>
      <p class="ats-detail-note">${escapeHtml(profileExplanation || tr("tailor.profile.transparent", "Transparent CV Copilot ATS simulation."))}</p>
      <dl class="ats-system-details" aria-label="${escapeHtml(tr("tailor.profile.details_aria", "Selected score-system details"))}">
        ${profileFacts}
      </dl>
      <p class="ats-detail-disclaimer">${escapeHtml(profile.disclaimer || tr("tailor.profile.transparent_disclaimer", "Transparent CV Copilot simulation; not certified by or backed by an ATS vendor."))}</p>
      <div class="ats-profile-compare" aria-label="${escapeHtml(tr("tailor.profile.compare_aria", "ATS score-system comparison"))}">
        <div class="ats-profile-compare-head">
          <span>${escapeHtml(tr("tailor.profile.systems", "Score systems"))}</span>
          <small>${escapeHtml(tr("tailor.profile.simulations", "Simulations, not vendor-backed"))}</small>
        </div>
        ${comparisonRows}
      </div>
      <dl class="ats-detail-facts">
        <div><dt>${escapeHtml(tr("tailor.lineage.job", "Job"))}</dt><dd>${escapeHtml(jobTitle)}</dd></div>
        <div><dt>${escapeHtml(tr("ats.source", "Source"))}</dt><dd>${escapeHtml(source === "rubric" ? tr("ats.source_rubric", "AI rubric with deterministic signal") : tr("ats.source_deterministic", "Deterministic keyword scorer"))}</dd></div>
        <div><dt>${escapeHtml(tr("ats.coverage", "Coverage"))}</dt><dd>${escapeHtml(mustHave)}</dd></div>
        <div><dt>${escapeHtml(tr("ats.status", "Status"))}</dt><dd>${escapeHtml(atsState.stale ? tr("ats.unsaved_edits", "Based on saved CV; unsaved edits exist") : atsState.message || tr("ats.ready", "Ready"))}</dd></div>
      </dl>
      <div class="ats-detail-columns">
        <section>
          <h4>${escapeHtml(tr("ats.gaps", "Gaps"))}</h4>
          <ul>${gapList || `<li><span>${escapeHtml(tr("ats.no_gaps", "No gaps returned"))}</span></li>`}</ul>
        </section>
        <section>
          <h4>${escapeHtml(tr("ats.matched", "Matched"))}</h4>
          <ul>${matchedList || `<li><span>${escapeHtml(tr("ats.no_matched", "No matched terms returned"))}</span></li>`}</ul>
        </section>
      </div>
    `;
    const sel = $("#ats-profile-select");
    if (sel) {
      sel.addEventListener("change", () => {
        atsProfile = sel.value;
        if (!ATS_PROFILES[atsProfile]) atsProfile = "semantic_fit";
        localStorage.setItem(ATS_PROFILE_KEY, atsProfile);
        atsBadgeCache.clear();
        scheduleAtsBadge(0);
        renderAtsDetail();
      });
    }
  }

  // Debounced entry point. Hides the badge when the CV has no linked job;
  // otherwise (re)scores, reusing the content-hash cache when possible.
  function scheduleAtsBadge(delay) {
    if (atsBadgeTimer) { clearTimeout(atsBadgeTimer); atsBadgeTimer = null; }
    if (!activeJob || !activeJob.id || !activeCvId) {
      atsBadgeReq += 1;
      setAtsState({
        status: "empty",
        cvId: activeCvId,
        jobId: null,
        source: null,
        score: null,
        payload: null,
        sections: emptyAtsSections(),
        stale: false,
        message: "No linked job for this CV.",
        packetLineage: null,
        packetUsable: false,
      });
      return;
    }
    const row = { id: activeCvId, job_id: activeJob.id };
    const activeLineage = normalizeAtsPacket(activePacketLineage, row);
    if (activeLineage && !isUsableAtsPacket(activeLineage, row)) {
      atsBadgeReq += 1;
      setAtsState(packetBlockedAtsState(activeLineage, row));
      return;
    }
    const wait = delay == null ? ATS_BADGE_DEBOUNCE_MS : delay;
    atsBadgeTimer = setTimeout(refreshAtsBadge, wait);
  }

  async function refreshAtsBadge() {
    if (!activeJob || !activeJob.id || !activeCvId) { scheduleAtsBadge(0); return; }
    const job = activeJob;
    const cvId = activeCvId;
    // /api/ats-score reads the CV from the DB by id, so the badge reflects the
    // SAVED CV. Key the cache on the persisted content (``lastSavedContent``),
    // not the in-memory model — that way a re-score is requested only after a
    // save actually changes the stored YAML, and re-opening an unchanged CV is
    // a cache hit.
    const body = lastSavedContent || "";
    const token = ++atsBadgeReq;
    setAtsState({
      status: "loading",
      cvId,
      jobId: job.id,
      score: null,
      payload: null,
      sections: emptyAtsSections(),
      stale: false,
      message: `Scoring ${job.title || "the linked job"}.`,
      packetLineage: normalizeAtsPacket(activePacketLineage, { id: cvId, job_id: job.id }),
      packetUsable: true,
    });
    try {
      // The JD lives on the saved job; the ATS route needs its text.
      const jobRes = await fetch(`/api/jobs/${encodeURIComponent(job.id)}`);
      if (!jobRes.ok) throw new Error(`HTTP ${jobRes.status}`);
      const jobData = await jobRes.json();
      const detailPackets = Array.isArray(jobData && jobData.packets) ? jobData.packets : [];
      const detailPacket = normalizeAtsPacket(
        detailPackets.find((p) => p && p.tailored_cv_id === cvId) || activePacketLineage,
        { id: cvId, job_id: job.id },
      );
      if (!isUsableAtsPacket(detailPacket, { id: cvId, job_id: job.id })) {
        if (token === atsBadgeReq) setAtsState(packetBlockedAtsState(detailPacket, { id: cvId, job_id: job.id }));
        return;
      }
      const jdText = (jobData && jobData.job && jobData.job.jd_text) || "";
      const jdHash = atsHash(jdText);
      const jobUpdatedAt = (jobData && jobData.job && jobData.job.updated_at) || "";
      const packetKey = detailPacket && (detailPacket.packet_id || detailPacket.id)
        ? `${detailPacket.packet_id || detailPacket.id}:${detailPacket.packet_version || ""}:${detailPacket.status || ""}:${(detailPacket.stale_reasons || []).join(",")}`
        : "";
      const key = `${cvId}:${job.id}:${atsProfile}:${atsHash(body)}:${jdHash}:${jobUpdatedAt}:${packetKey}`;
      if (atsBadgeCache.has(key)) {
        if (token === atsBadgeReq) setAtsState(withAtsStaleFlag(atsBadgeCache.get(key), dirty));
        return;
      }
      if (!jdText.trim()) {
        if (token === atsBadgeReq) {
          setAtsState({
            status: "unscored",
            cvId,
            jobId: job.id,
            source: "deterministic",
            score: null,
            payload: { notes: "The linked job has no job description text." },
            sections: emptyAtsSections(),
            stale: false,
            message: "The linked job has no job description text.",
          });
        }
        return;
      }
      const res = await fetch("/api/ats-score", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cv_id: cvId,
          job_id: job.id,
          jd_text: jdText,
          profile: atsProfile,
          compare_profiles: atsCompareProfiles(),
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const score = data && typeof data.score === "number" ? data.score : null;
      // A stale response (CV/job switched mid-flight) must not paint.
      if (token !== atsBadgeReq) return;
      const nextState = normalizeAtsPayload(data, job, key);
      nextState.packetLineage = detailPacket || activePacketLineage;
      nextState.packetUsable = true;
      if (score === null) {
        setAtsState(nextState);
        return;
      }
      atsBadgeCache.set(key, nextState);
      setAtsState(nextState);
    } catch (_) {
      if (token === atsBadgeReq) {
        setAtsState({
          status: "error",
          cvId,
          jobId: job.id,
          score: null,
          payload: null,
          sections: emptyAtsSections(),
          stale: false,
          message: "ATS score failed.",
          packetLineage: activePacketLineage,
          packetUsable: false,
        });
      }
    }
  }

  function setStatus(state, label) {
    const el = $("#status");
    const prev = el.dataset.state;
    el.dataset.state = state;
    $("#status-label").textContent = label;
    // Motion: a one-shot pulse when first transitioning to "modified",
    // and a slide-in for the saved bytes count.
    el.classList.remove("is-pulse", "is-saved");
    if (state === "modified" && prev !== "modified") {
      // Force reflow so the animation re-fires.
      void el.offsetWidth;
      el.classList.add("is-pulse");
      el.addEventListener(
        "animationend",
        () => el.classList.remove("is-pulse"),
        { once: true },
      );
    }
    if (state === "saved") {
      void el.offsetWidth;
      el.classList.add("is-saved");
    }
  }

  function updatePosition() {
    if (!editor) return;
    const c = editor.getCursor();
    $("#position").textContent = `Ln ${c.line + 1}, Col ${c.ch + 1}`;
  }

  function updateBytesInfo() {
    if (!editor) return;
    const bytes = new Blob([editor.getValue()]).size;
    $("#bytes-info").textContent = formatBytes(bytes);
  }

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  }

  function refreshLastSaved() {
    const el = $("#last-saved");
    if (!lastSavedAt) { el.setAttribute("hidden", ""); return; }
    el.removeAttribute("hidden");
    const sec = Math.round((Date.now() - lastSavedAt.getTime()) / 1000);
    if (sec < 5)        el.textContent = "saved · just now";
    else if (sec < 60)  el.textContent = `saved · ${sec}s ago`;
    else                el.textContent = `saved · ${Math.round(sec / 60)}m ago`;
  }
  setInterval(refreshLastSaved, 8000);

  // ──────────────────────────────────────────────────────────────────────
  //  LOAD / SAVE
  //
  //  Source-of-truth model:
  //    1. Server document store — process memory in demo mode, Supabase
  //       documents in authenticated mode
  //    2. cvModel (browser memory) — what the form & YAML view both edit
  //    3. CodeMirror buffer — only authoritative while in YAML view
  //  Edits save as YAML in the current document store; the browser never
  //  claims that it writes directly to a repository file on disk.
  //
  //  Sync rules:
  //    • form change   → mutate cvModel → schedule save → save serialises
  //                       cvModel to YAML and writes it
  //    • YAML edit     → parse buffer → cvModel = parsed → schedule save
  //    • view switch   → form→yaml: serialise cvModel into the buffer
  //                      yaml→form: parse buffer into cvModel, rebuild form
  // ──────────────────────────────────────────────────────────────────────
  // Block until the Supabase session (and its bearer) is actually in place
  // before the FIRST /api/* fetch. auth.js gates init() behind
  // OnePagerAuth.ready, and it sets ``session`` before resolving — but on the
  // demo-login → reload path the bearer can still be a tick behind the editor
  // boot. Awaiting ready + confirming getSession() carries an access_token
  // closes that token-hydration race, so the very first /api/cvs + /api/cv
  // calls go out authenticated (a 401 here would leave the editor empty).
  async function awaitAuthReady() {
    const auth = window.OnePagerAuth;
    if (!auth || !auth.ready) return; // stripped local build — no auth gate.
    try {
      await auth.ready;
    } catch (_) {
      /* ready rejected — fetches will surface their own auth errors */
    }
    // Confirm the bearer is materialised. getSession() reads the live session
    // the fetch-patch will use; if it's somehow not ready yet, give it a few
    // short ticks rather than firing an unauthenticated request.
    if (typeof auth.getSession !== "function") return;
    for (let i = 0; i < 20; i++) {
      const s = auth.getSession();
      if (s && s.access_token) return;
      await new Promise((r) => setTimeout(r, 25));
    }
  }

  // Resolve the active document id before the first load: keep the stored
  // id if the user still owns that CV, otherwise fall back to their
  // most-recently-updated one. Also primes the topbar label/limit. Safe to
  // call repeatedly; failures leave activeCvId untouched so loadCv can
  // still default server-side.
  async function resolveActiveId() {
    try {
      const res = await fetch("/api/cvs");
      if (!res.ok) return;
      const data = await res.json();
      const cvs = Array.isArray(data.cvs) ? data.cvs : [];
      if (!cvs.length) {
        window.dispatchEvent(new CustomEvent("cv:list-changed", { detail: { data } }));
        return;
      }
      if (requestedCvId) {
        const requested = cvs.find((c) => c.id === requestedCvId);
        if (!requested) {
          setActiveCv(requestedCvId, "Unavailable CV");
          activeJob = null;
          window.dispatchEvent(new CustomEvent("cv:list-changed", { detail: { data } }));
          return;
        }
        setActiveCv(requested.id, requested.name);
        setActiveJobFromCvs(cvs);
        window.dispatchEvent(new CustomEvent("cv:list-changed", { detail: { data } }));
        return;
      }
      let match = activeCvId && cvs.find((c) => c.id === activeCvId);
      const chosen = match || cvs[0];
      setActiveCv(chosen.id, chosen.name);
      setActiveJobFromCvs(cvs);
      window.dispatchEvent(new CustomEvent("cv:list-changed", { detail: { data } }));
    } catch (_) { /* offline / boot race — loadCv defaults server-side */ }
  }

  // Resolve the active CV's linked job (id + title) from a fresh /api/cvs list
  // and refresh the ATS badge. ``job_id``/``job_title`` are surfaced per-row by
  // the server (RLS-scoped). No link → activeJob is null and the badge hides.
  function setActiveJobFromCvs(cvs) {
    const row = Array.isArray(cvs) ? cvs.find((c) => c.id === activeCvId) : null;
    const lineage = row && row.packet_lineage ? row.packet_lineage : null;
    const usable = isUsableAtsPacket(lineage, row);
    const next = row && row.job_id
      ? { id: row.job_id, title: row.job_title || "Linked job" }
      : null;
    const changed = (next && next.id) !== (activeJob && activeJob.id) ||
      (lineage && lineage.packet_id) !== (activePacketLineage && activePacketLineage.packet_id) ||
      (lineage && lineage.status) !== (activePacketLineage && activePacketLineage.status);
    activeJob = next;
    activePacketLineage = lineage;
    // A different (or removed) job link invalidates the badge immediately;
    // scheduleAtsBadge re-derives from the (cached) content hash.
    if (row && row.job_id && !usable) {
      atsBadgeReq += 1;
      setAtsState(packetBlockedAtsState(lineage, row));
    } else if (changed) scheduleAtsBadge(0);
    else scheduleAtsBadge();
  }

  function syncCvSwitcherLabel(label, name) {
    if (!label) return;
    const full = String(name || "My CV").trim() || "My CV";
    const actionLabel = tr("catalog.trigger_aria", "Open document catalog: {name}", { name: full });
    const tooltipLabel = tr("catalog.trigger_aria", "Open document catalog: {name}", { name: shortText(full, 80) });
    label.textContent = tr("catalog.trigger", "Document catalog");
    const btn = label.closest("#cv-switcher");
    if (btn) {
      btn.setAttribute("aria-label", actionLabel);
      btn.setAttribute("title", tooltipLabel);
      btn.dataset.tooltip = tooltipLabel;
    }
  }

  // Single place that updates the active-document pointer + topbar label
  // and persists the id so a reload reopens the same CV.
  function setActiveCv(id, name) {
    activeCvId = id || null;
    activePacketLineage = null;
    activeJob = null;
    atsBadgeReq += 1;
    if (name != null) activeCvName = name;
    if (activeCvId) {
      try {
        const defaultKey = `${COLLAPSED_KEY}::default`;
        const activeKey = `${COLLAPSED_KEY}::${activeCvId}`;
        const defaultState = localStorage.getItem(defaultKey);
        if (defaultState) {
          const current = JSON.parse(localStorage.getItem(activeKey) || "{}");
          const pending = JSON.parse(defaultState || "{}");
          localStorage.setItem(activeKey, JSON.stringify(Object.assign({}, current, pending)));
          localStorage.removeItem(defaultKey);
        }
        const defaultSectionKey = `${ACTIVE_SECTION_KEY}::default`;
        const activeSectionKey = `${ACTIVE_SECTION_KEY}::${activeCvId}`;
        const defaultSection = localStorage.getItem(defaultSectionKey);
        if (defaultSection) {
          if (!localStorage.getItem(activeSectionKey)) {
            localStorage.setItem(activeSectionKey, defaultSection);
          }
          localStorage.removeItem(defaultSectionKey);
        }
      } catch (_) {}
      localStorage.setItem(CV_ID_KEY, activeCvId);
    } else localStorage.removeItem(CV_ID_KEY);
    activeFormSection = loadActiveFormSection();
    const label = $("#cv-switcher-label");
    syncCvSwitcherLabel(label, activeCvName);
    applyCollapsedState();
    applyFormMode();
    window.dispatchEvent(new CustomEvent("cv:active-changed", { detail: { id: activeCvId, name: activeCvName } }));
  }

  function syncActiveCvUrl(id) {
    if (!window.history || !window.history.replaceState) return;
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("id", id);
    else url.searchParams.delete("id");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    requestedCvId = id || null;
  }

  async function saveBeforeLeavingEditor() {
    if (!dirty) return true;
    return save({ forceLint: false });
  }

  window.OnePagerNavigate = async function (href) {
    const saved = await saveBeforeLeavingEditor();
    if (!saved) {
      showBuildToast("Save failed", "Navigation was stopped so current edits are not lost.", "danger");
      return false;
    }
    if (href === "/tailor") {
      const tailor = $("#tailor-btn");
      href = tailor ? tailor.getAttribute("href") || href : href;
    }
    window.location.href = href;
    return true;
  };

  function enterEmptyCvState() {
    setActiveCv(null, "No active CV");
    syncActiveCvUrl(null);
    activeJob = null;
    activePacketLineage = null;
    activeCvRevision = null;
    document.body.dataset.emptyCv = "true";
    suppressEditorOnEdit = true;
    editor.setValue("");
    suppressEditorOnEdit = false;
    lastSavedContent = "";
    dirty = false;
    replaceModel({});
    if (formApi) formApi.rebuild();
    renderOutline();
    updateBytesInfo();
    updatePosition();
    refreshIssuesPill();
    refreshSectionHints();
    setStatus("ready", "No active CV");
    const preview = $("#preview");
    if (preview) preview.srcdoc = "";
    showBuildToast("No active CV", "Create or import a CV from Document actions.", "neutral");
  }

  async function loadCv() {
    await awaitAuthReady();
    await resolveActiveId();
    const requestSeq = ++loadCvReq;
    const targetCvId = activeCvId;
    const res = await fetch(`/api/cv?_t=${Date.now()}${idParam()}`);
    if (requestSeq !== loadCvReq || targetCvId !== activeCvId) return false;
    if (res.status === 404) {
      enterEmptyCvState();
      showBuildToast("CV not found", "The linked CV is no longer available.", "danger");
      return false;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    document.body.dataset.emptyCv = "false";
    activeCvRevision = res.headers.get("X-OnePager-Revision") || null;
    conflictPending = false;  // a fresh load resolves any prior conflict state
    yamlBufferInvalid = false;  // ...and the freshly loaded buffer parses cleanly
    const text = await res.text();
    if (requestSeq !== loadCvReq || targetCvId !== activeCvId) return false;
    suppressEditorOnEdit = true;
    editor.setValue(text);
    suppressEditorOnEdit = false;
    lastSavedContent = text;
    dirty = false;
    try {
      replaceModel(window.CvForm.yamlToModel(text) || {});
    } catch (err) {
      console.error("Initial YAML parse failed:", err);
      replaceModel({});
    }
    setStatus("ready", "Ready");
    if (formApi) formApi.rebuild();
    refreshAtsAdvisories();
    refreshAppearanceUi();
    renderOutline();
    updateBytesInfo();
    updatePosition();
    schedulePreview(60);
    refreshIssuesPill();
    refreshSectionHints();
    setAtsState({
      status: "empty",
      cvId: null,
      jobId: null,
      source: null,
      score: null,
      payload: null,
      sections: emptyAtsSections(),
      stale: false,
      message: "No linked job for this CV.",
      packetLineage: null,
      packetUsable: false,
    });
    // resolveActiveId already set activeJob from /api/cvs; score against the
    // freshly-parsed content (cache makes a re-open of an unchanged CV free).
    scheduleAtsBadge(0);
    historyLoad();
    return true;
  }

  function modelToYamlString() {
    return window.CvForm.modelToYaml(cvModel);
  }

  function refreshYamlBufferFromModel() {
    const text = modelToYamlString();
    suppressEditorOnEdit = true;
    editor.setValue(text);
    suppressEditorOnEdit = false;
  }

  /**
   * Auto-lint a raw YAML buffer:
   *   1. Parse via js-yaml (the buffer becomes a JS object)
   *   2. Drop empty arrays / undefined values via the form module's pruner
   *   3. Re-emit through ``CvForm.modelToYaml`` so the result has the same
   *      shape we'd save from Form view — consistent indentation, no
   *      stray quotes, header comment preserved.
   *
   * If parsing fails, return the original buffer untouched (the user is
   * mid-edit and the YAML is invalid; we don't want to clobber their work).
   */
  function lintYamlBuffer(raw) {
    if (!raw || !raw.trim()) return raw;
    try {
      const parsed = window.CvForm.yamlToModel(raw);
      if (!parsed || typeof parsed !== "object") return raw;
      return window.CvForm.modelToYaml(parsed);
    } catch (_) {
      return raw;
    }
  }

  /**
   * Save the current YAML document through the server API.
   *
   * @param {{forceLint?: boolean}} opts - When ``forceLint`` is true and
   *   the user has linting enabled, the YAML buffer is normalised
   *   through ``CvForm.modelToYaml`` before being sent. Pass ``false``
   *   from the autosave timer so the cursor doesn't jump mid-typing.
   */
  async function saveNow(opts = {}) {
    if (!editor) return false;
    // During an unresolved 409 conflict, suppress AUTO saves (autosave timer,
    // preview save-first, background flush). Only an explicit Save (opts.auto
    // not set) may overwrite — that is the "click Save to overwrite" the toast
    // promises. Without this, a background visibilitychange/pagehide flush would
    // silently last-write-wins over the other tab.
    if (conflictPending && opts.auto === true) return false;
    if (!activeCvId) {
      setStatus("error", "No active CV");
      showBuildToast("No active CV", "Create or import a CV before saving.", "neutral");
      return false;
    }
    const forceLint = opts.forceLint === true;
    // Autosave must not ship a YAML buffer that doesn't parse — the server
    // 400s and the user sees a misleading "Save failed" for a mid-edit state.
    // Explicit saves still go through so the server's reason can surface.
    if (opts.auto === true && viewMode === "yaml" && yamlBufferInvalid) return false;
    // Required-field gaps NEVER block a save — the form is deliberately
    // permissive (form.js: "users can save half-finished entries"). Blocking
    // here silently dropped autosaves, Cmd+S saves, and the pagehide flush
    // whenever a new entry had empty required fields. Surface the gaps as a
    // non-blocking hint instead.
    if (hasRequiredFormGaps()) {
      refreshIssuesPill();
      refreshSectionHints();
    }

    let content;
    if (viewMode === "form") {
      // Form view always emits a canonical YAML.
      content = modelToYamlString();
    } else if (forceLint && lintOnSave) {
      // User-initiated save in YAML view + lint enabled → normalise.
      content = lintYamlBuffer(editor.getValue());
    } else {
      // Autosave in YAML view, OR user-initiated with lint disabled.
      // Preserve the buffer exactly so the user can keep typing.
      content = editor.getValue();
    }

    // Reflect the lint back into the editor buffer ONLY when an explicit
    // user save reformatted things — never during typing.
    if (forceLint && lintOnSave && viewMode === "yaml" && content !== editor.getValue()) {
      suppressEditorOnEdit = true;
      const cursor = editor.getCursor();
      editor.setValue(content);
      try { editor.setCursor(cursor); } catch (_) {}
      suppressEditorOnEdit = false;
    }
    if (content === lastSavedContent) {
      // Reverting the buffer back to the saved content resolves a pending
      // 409 conflict too — without this, autosaves stayed suppressed forever.
      conflictPending = false;
      dirty = false;
      setStatus("ready", "Ready");
      scheduleAtsBadge();
      return true;
    }
    const targetCvId = activeCvId;
    setStatus("saving", "Saving");
    try {
      const res = await fetch("/api/cv", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, id: targetCvId, revision: activeCvRevision }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 409) {
        // Optimistic-lock conflict: another tab/device saved this CV since we
        // loaded it. Previously this was a dead-end — the stale revision made
        // every retry 409 too, so the user could only discard their edits.
        // Drop the revision so the NEXT explicit save is an unconditional
        // overwrite (the server treats a null revision as last-write-wins) and
        // keep the content dirty, turning the conflict into a recoverable
        // choice instead of silent data loss.
        activeCvRevision = null;
        conflictPending = true;
        setStatus("error", "Save conflict");
        showBuildToast(
          "This CV changed elsewhere",
          "Your edits are NOT saved yet. Click Save to overwrite with your version, or reload the page to discard yours.",
          "danger",
        );
        return false;
      }
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      const currentContent = viewMode === "form" ? modelToYamlString() : editor.getValue();
      if (activeCvId !== targetCvId || currentContent !== content) {
        if (currentContent !== lastSavedContent) {
          dirty = true;
          setStatus("modified", "Modified");
          markAtsStale();
        }
        return false;
      }
      // First save after a server-side default resolves the real id —
      // adopt it so subsequent calls target the same document.
      if (data && data.id && data.id !== activeCvId) {
        setActiveCv(data.id, activeCvName);
      }
      // Keep the YAML buffer in sync so a YAML-tab visit shows the saved document.
      if (viewMode === "form") {
        suppressEditorOnEdit = true;
        editor.setValue(content);
        suppressEditorOnEdit = false;
      }
      lastSavedContent = content;
      lastSavedAt = new Date();
      activeCvRevision = data.updated_at || activeCvRevision;
      conflictPending = false;  // an explicit overwrite resolved the conflict
      dirty = false;
      setStatus("saved", `Saved · ${formatBytes(data.bytes)}`);
      flashActionSuccess($("#save-btn"));
      refreshLastSaved();
      // A save may have changed the stored YAML → re-score the ATS badge
      // (debounced + content-hash-cached, so an unchanged save is a no-op).
      scheduleAtsBadge();
      // NOTE: save() does not trigger a preview re-render. The Live-mode
      // refresh path (refreshPreview → save-if-dirty → render) handles
      // that. Cmd+S / save-button users get a render via the wrapper
      // ``userInitiatedSave()`` below.
      if (readyStatusTimer) clearTimeout(readyStatusTimer);
      const savedRevision = activeCvRevision;
      readyStatusTimer = setTimeout(() => {
        if (!dirty && activeCvRevision === savedRevision) setStatus("ready", "Ready");
      }, 2200);
      return true;
    } catch (err) {
      // Surface the server's reason (e.g. "YAML invalid: <detail>", thrown
      // from data.error above) instead of a bare "Save failed".
      const detail = err && err.message ? err.message : "unknown error";
      setStatus("error", `Save failed: ${detail}`);
      console.error(err);
      return false;
    }
  }

  function save(opts = {}) {
    const run = saveQueue.catch(() => false).then(() => saveNow(opts));
    saveQueue = run;
    return run;
  }

  // ──────────────────────────────────────────────────────────────────────
  //  PREVIEW
  // ──────────────────────────────────────────────────────────────────────
  function schedulePreview(delay, opts = {}) {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(() => refreshPreview(opts), delay || 60);
  }

  /**
   * Content-driven refresh — typing in the form or YAML, saving, etc.
   * Respects the autoRender toggle. In Manual mode, sets pendingRefresh
   * and updates the refresh button to show a pending dot.
   */
  function scheduleContentRefresh(delay = CONTENT_DEBOUNCE_MS) {
    if (autoRender) {
      schedulePreview(delay);
    } else {
      markPending();
    }
  }

  function markPending() {
    pendingRefresh = true;
    const btn = $("#refresh-btn");
    if (btn) btn.classList.add("has-pending");
  }

  function clearPending() {
    pendingRefresh = false;
    const btn = $("#refresh-btn");
    if (btn) btn.classList.remove("has-pending");
  }

  /**
   * User-initiated save (Cmd+S, save button click).
   *
   * In Live mode this triggers a refresh as well — the user expects the
   * preview to catch up with the saved document. In Manual mode it just
   * saves; the preview waits for an explicit Refresh click. EITHER way,
   * we pass ``forceLint`` so the YAML buffer can be reformatted.
   */
  function userInitiatedSave() {
    if (autoRender) {
      // schedulePreview → refreshPreview, which calls save({forceLint: true})
      // before rendering. Mark intent so refreshPreview lints.
      pendingForceLint = true;
      schedulePreview(0);
    } else if (dirty) {
      save({ forceLint: true });
    }
  }
  // Flag the next refreshPreview should pass forceLint to its inner save.
  // Set by userInitiatedSave; consumed (and cleared) inside refreshPreview.
  let pendingForceLint = false;

  function showPreviewLoading(show) {
    const el = $("#preview-loading");
    if (show) el.removeAttribute("hidden");
    else el.setAttribute("hidden", "");
  }

  let dimTimer = null;
  let previewRequestSeq = 0;
  /**
   * Refresh the preview iframe.
   *
   * Content-driven preview refreshes save first because the preview endpoint
   * reads the stored CV. Chrome-only refreshes, such as light/dark mode, can
   * opt out so a dirty or conflicted edit does not block the preview theme
   * from updating.
   */
  async function refreshPreview(opts = {}) {
    const requestSeq = ++previewRequestSeq;
    // We save first when the stored document is out of sync (dirty) OR the user
    // explicitly asked for a lint pass via Cmd+S — even if the buffer
    // hasn't been edited since the last save, they may want the
    // formatter to canonicalise it now.
    const saveBeforeRender = opts.saveBeforeRender !== false;
    if (saveBeforeRender && (dirty || pendingForceLint)) {
      if (autosaveTimer) { clearTimeout(autosaveTimer); autosaveTimer = null; }
      const forceLint = pendingForceLint;
      pendingForceLint = false;
      // forceLint marks an EXPLICIT user save (Cmd+S / Save button routed through
      // userInitiatedSave); an auto edit-debounce refresh has forceLint=false.
      // Gate only the auto path during a 409 conflict so an explicit save can
      // still overwrite, but a background/auto refresh cannot silently clobber.
      const saved = await save({ forceLint, auto: !forceLint });
      if (requestSeq !== previewRequestSeq) return;
      if (!saved) {
        showPreviewLoading(false);
        undimPreview();
        return;
      }
    }
    clearPending();
    showPreviewLoading(true);
    const url = previewUrl({ embedded: true });
    const iframe = $("#preview");
    const wrap = $("#preview-wrap");
    const prevScrollTop = wrap ? wrap.scrollTop : 0;
    const prevScrollLeft = wrap ? wrap.scrollLeft : 0;
    lastPagedCount = null;
    previewSections = [];
    cvContentHeight = A4_H_PX;
    cvContentWidth = A4_W_PX;
    cvPageHeight = A4_H_PX;
    cvPageWidth = A4_W_PX;
    // Dim the iframe slightly while paged.js is recomputing — but only if
    // the request takes >80 ms so quick re-renders don't flash.
    if (dimTimer) clearTimeout(dimTimer);
    dimTimer = setTimeout(() => iframe.classList.add("is-recomputing"), 80);
    iframe.onload = () => {
      showPreviewLoading(false);
      applyZoom();
      if (previewMode !== "paged") undimPreview();
      // Restore scroll if we're not jumping to a specific section.
      if (pendingScrollTarget == null) {
        if (wrap) {
          wrap.scrollTop = Math.min(prevScrollTop, Math.max(0, wrap.scrollHeight - wrap.clientHeight));
          wrap.scrollLeft = Math.min(prevScrollLeft, Math.max(0, wrap.scrollWidth - wrap.clientWidth));
        }
      } else {
        if (scrollPreviewToSection(pendingScrollTarget, "auto")) {
          pendingScrollTarget = null;
        }
      }
      // srcdoc reload wiped the iframe DOM — restore highlight styles + any
      // hover highlight that was live before the refresh (Stage 3).
      ensurePreviewHighlightStyles();
      reapplyPendingHighlight();
    };

    // Load the preview HTML via the PATCHED window.fetch (auth.js attaches the
    // Bearer token to /api/* requests) and inject it with ``srcdoc`` instead of
    // navigating the iframe to the URL. A plain ``iframe.src = url`` is a
    // browser NAVIGATION, which the fetch-patch can't intercept → it sends no
    // bearer → 401, AND the /api/preview response carries
    // ``frame-ancestors 'none'`` + ``X-Frame-Options: DENY`` which forbid the
    // same-origin iframe from rendering even a 200. ``srcdoc`` sidesteps both:
    // there is no navigation response to authenticate or to frame-block. The
    // server security headers stay strict and UNCHANGED. (The preview's
    // paged.js/handshake scripts are loaded as EXTERNAL same-origin scripts —
    // see render_preview.py / preview-runtime.js — so they still run under the
    // /app page's strict ``script-src 'self'`` CSP that srcdoc inherits.)
    try {
      const res = await fetch(url, { headers: { Accept: "text/html" } });
      if (requestSeq !== previewRequestSeq) return;
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const html = await res.text();
      if (requestSeq !== previewRequestSeq) return;
      // Assigning srcdoc reloads the frame and fires ``onload`` above.
      iframe.removeAttribute("src");
      iframe.srcdoc = withTrustedPreviewBase(html);
    } catch (err) {
      if (requestSeq !== previewRequestSeq) return;
      console.error("Preview load failed:", err);
      showPreviewLoading(false);
      undimPreview();
      setStatus("error", "Preview failed");
    }
  }

  function undimPreview() {
    const iframe = $("#preview");
    if (dimTimer) { clearTimeout(dimTimer); dimTimer = null; }
    iframe.classList.remove("is-recomputing");
  }

  const PREFERS_REDUCED_MOTION = matchMedia("(prefers-reduced-motion: reduce)");

  function previewSectionGeometry(slug) {
    if (typeof slug !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(slug)) {
      return null;
    }
    return previewSections.find((section) => section.key === slug) || null;
  }

  function previewSectionScreenRect(slug) {
    const iframe = $("#preview");
    const section = previewSectionGeometry(slug);
    if (!iframe || !section) return null;
    const iframeRect = iframe.getBoundingClientRect();
    const z = effectiveZoom();
    return {
      top: iframeRect.top + section.top * z,
      bottom: iframeRect.top + section.bottom * z,
    };
  }

  function scrollPreviewToSection(slug, behavior = "smooth") {
    const wrap = $("#preview-wrap");
    const targetRect = previewSectionScreenRect(slug);
    if (!targetRect || !wrap) return false;
    const wrapRect = wrap.getBoundingClientRect();
    const pad = previewPad(wrap);
    const current = wrap.scrollTop;
    const next = clamp(
      current + targetRect.top - (wrapRect.top + pad),
      0,
      Math.max(0, wrap.scrollHeight - wrap.clientHeight),
    );
    const safe = PREFERS_REDUCED_MOTION.matches ? "auto" : behavior;
    try {
      wrap.scrollTo({ top: next, behavior: safe });
    } catch (_) {
      wrap.scrollTop = next;
    }
    return true;
  }

  /**
   * Scroll-if-needed: only scroll the preview to ``slug`` when that section
   * is (partly) outside the visible viewport. Stage 2 made #preview-wrap the
   * single scroll layer (the iframe is pointer-transparent and never scrolls
   * internally), so "visible" is measured as the section's box inside the
   * wrap's client rect. Returns true if a scroll was issued.
   *
   * NEVER calls refreshPreview/schedulePreview — those save first if dirty.
   */
  function scrollPreviewIntoViewIfNeeded(slug, behavior = "smooth") {
    const wrap = $("#preview-wrap");
    const targetRect = previewSectionScreenRect(slug);
    if (!targetRect || !wrap) return false;

    // The iframe is scaled by ``z`` via a CSS transform; the section's
    // validated child-reported geometry is projected into wrap (screen) space
    // before we compare against the wrap's viewport.
    const wrapRect = wrap.getBoundingClientRect();

    const pad = Math.max(10, Math.min(18, previewPad(wrap)));
    const above = targetRect.top < wrapRect.top + pad;
    const below = targetRect.bottom > wrapRect.bottom - pad;
    if (!above && !below) return false; // already comfortably visible

    const safe = PREFERS_REDUCED_MOTION.matches ? "auto" : behavior;
    // Desired wrap.scrollTop so the section's top sits ``pad`` below the
    // wrap's top edge. Clamp into the wrap's scroll range.
    const current = wrap.scrollTop;
    const delta = targetRect.top - (wrapRect.top + pad);
    const maxScroll = Math.max(0, wrap.scrollHeight - wrap.clientHeight);
    const next = Math.max(0, Math.min(maxScroll, current + delta));
    try {
      wrap.scrollTo({ top: next, behavior: safe });
    } catch (_) {
      wrap.scrollTop = next; // older engines: no options object
    }
    return true;
  }

  function scrollFormSectionToKey(key, behavior = "smooth") {
    if (!key || viewMode !== "form" || !formApi) return false;
    try {
      formApi.scrollTo(key, { behavior, ifNeeded: behavior === "auto", block: "nearest" });
      return true;
    } catch (_) {
      return false;
    }
  }

  function cancelOutlineHover() {
    if (outlineHoverTimer) {
      clearTimeout(outlineHoverTimer);
      outlineHoverTimer = null;
    }
    if (outlineHoverKey) {
      setPreviewHighlight(outlineHoverKey, false);
      if (pendingHighlightKey === outlineHoverKey) pendingHighlightKey = null;
    }
    outlineHoverKey = null;
  }

  function scheduleOutlineHover(slug, pointerType) {
    if (!slug || pointerType === "touch" || pointerType === "pen") return;
    cancelOutlineHover();
    outlineHoverKey = slug;
    outlineHoverTimer = setTimeout(() => {
      outlineHoverTimer = null;
      if (outlineHoverKey !== slug) return;
      if (Date.now() < outlineHoverSuppressedUntil) return;
      if (viewMode !== "form") return;
      if (formMode === "overview") scrollFormSectionToKey(slug, "auto");
      pendingHighlightKey = slug;
      ensurePreviewHighlightStyles();
      setPreviewHighlight(slug, true);
      scrollPreviewIntoViewIfNeeded(slug, "auto");
    }, 180);
  }

  function suppressOutlineHover(ms = 240) {
    outlineHoverSuppressedUntil = Date.now() + ms;
    cancelOutlineHover();
  }

  // ──────────────────────────────────────────────────────────────────────
  //  BIDIRECTIONAL HOVER-SYNC  (Stage 3)
  //
  //  Shared key contract: a form section carries data-section-key="{key}"
  //  and id="form-section-{key}"; the matching preview section is
  //  id="sec-{key}" (engine/render/templates.py). The key maps DIRECTLY —
  //  no transformation.
  //
  //  Form → preview : mouseenter cross-highlights both panes and
  //                   scroll-IF-NEEDED via scrollPreviewIntoViewIfNeeded
  //                   (NEVER refreshPreview/schedulePreview — those save).
  //  Preview → form : a passive, rAF-throttled scroll-spy on #preview-wrap
  //                   highlights the matching form section + drives the
  //                   outline active state.
  //
  //  The highlight styles live INSIDE the opaque-origin iframe document.
  //  The fixed, same-origin preview runtime owns them and accepts only narrow
  //  parent commands after validating the sender and section key.
  // ──────────────────────────────────────────────────────────────────────

  function postPreviewCommand(type, payload = {}) {
    const iframe = $("#preview");
    if (!iframe || !iframe.contentWindow) return false;
    iframe.contentWindow.postMessage({ type, ...payload }, "*");
    return true;
  }

  /** Ask the trusted child runtime to install its fixed highlight stylesheet. */
  function ensurePreviewHighlightStyles() {
    postPreviewCommand("preview-ensure-highlight-styles");
  }

  /** Ask the trusted child runtime to highlight (or clear) ``key``. */
  function setPreviewHighlight(key, on) {
    if (typeof key !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(key)) return;
    postPreviewCommand("preview-highlight", { key, on: on === true });
  }

  /** Highlight (or clear) the form section for ``key``. */
  function setFormHighlight(key, on) {
    if (!key) return;
    const formSec = document.querySelector(
      `.form-section[data-section-key="${key}"]`,
    );
    // Same value contract as above: CSS matches the literal "true".
    if (formSec) {
      if (on) formSec.setAttribute("data-highlighted", "true");
      else formSec.removeAttribute("data-highlighted");
    }
  }

  /** Re-apply any pending hover highlight after a srcdoc reload wiped it. */
  function reapplyPendingHighlight() {
    if (!pendingHighlightKey) return;
    ensurePreviewHighlightStyles();
    setPreviewHighlight(pendingHighlightKey, true);
  }

  function setupHoverSync() {
    // ── Form → preview ────────────────────────────────────────────────
    window.addEventListener("cv:section-hover", (e) => {
      const { sectionKey, hovering } = e.detail || {};
      if (!sectionKey) return;
      if (hovering) {
        pendingHighlightKey = sectionKey;
        ensurePreviewHighlightStyles();
        setPreviewHighlight(sectionKey, true);
        // Scroll the preview to the section ONLY if it's off-screen.
        scrollPreviewIntoViewIfNeeded(sectionKey, "smooth");
      } else {
        // Clear only if we're leaving the section we last marked.
        if (pendingHighlightKey === sectionKey) pendingHighlightKey = null;
        setPreviewHighlight(sectionKey, false);
      }
    });

    // ── Preview → form (scroll-spy) ───────────────────────────────────
    // Stage 2 makes #preview-wrap the single scroll layer; project the
    // child-reported section bounds against the wrap viewport on each scroll
    // (passive, rAF). The parent never receives or reads CV document content.
    const wrap = $("#preview-wrap");
    if (!wrap) return;
    let spyScheduled = false;

    function runScrollSpy() {
      spyScheduled = false;
      const iframe = $("#preview");
      if (!iframe || !previewSections.length) return;
      const wrapRect = wrap.getBoundingClientRect();
      const probe = wrapRect.top + Math.min(120, wrapRect.height * 0.25);
      const z = effectiveZoom();
      const iframeTop = iframe.getBoundingClientRect().top;

      let activeKey = null;
      let bestTop = -Infinity;
      // Pick the section whose top is the last one at/above the probe line.
      previewSections.forEach((section) => {
        const top = iframeTop + section.top * z;
        if (top <= probe && top > bestTop) {
          bestTop = top;
          activeKey = section.key;
        }
      });
      if (!activeKey) {
        // Above the first section → fall back to the first one if present.
        activeKey = previewSections[0].key;
      }
      highlightActiveFormSection(activeKey);
    }

    wrap.addEventListener(
      "scroll",
      () => {
        if (spyScheduled) return;
        spyScheduled = true;
        requestAnimationFrame(runScrollSpy);
      },
      { passive: true },
    );
  }

  /** Mark ``key`` as the scroll-active form section + sync the outline. */
  function highlightActiveFormSection(key) {
    const resolvedKey = viewMode === "form" && formMode === "focus"
      ? activeFormSection
      : key;
    $$(".form-section[data-section-key]").forEach((sec) => {
      // CSS matches [data-scroll-active="true"]; set the literal value
      // (toggleAttribute's force flag would set "" and never match).
      if (sec.dataset.sectionKey === resolvedKey) {
        sec.setAttribute("data-scroll-active", "true");
      } else {
        sec.removeAttribute("data-scroll-active");
      }
    });
    // Drive the outline active state from the same signal.
    if (resolvedKey) {
      $$(".outline-item").forEach((a) => {
        a.classList.toggle("active", a.dataset.slug === resolvedKey);
      });
    }
  }

  // Listen for paged.js render-complete posts from the iframe so we can
  // hide the spinner and update the page-count badge (red on overflow).
  // Also captures the iframe document height so applyZoom() can size the
  // preview stage to fit the entire content without internal scroll.
  function setupPagedListener() {
    function recordPreviewSections(data) {
      if (!Array.isArray(data && data.sections)) return;
      const seen = new Set();
      const next = [];
      data.sections.slice(0, 100).forEach((raw) => {
        if (!raw || typeof raw !== "object") return;
        const key = raw.key;
        const top = raw.top;
        const bottom = raw.bottom;
        if (typeof key !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(key)) return;
        if (!Number.isFinite(top) || !Number.isFinite(bottom)) return;
        if (top < -10000 || bottom < top || bottom > 10000000) return;
        if (seen.has(key)) return;
        seen.add(key);
        next.push({ key, top, bottom });
      });
      next.sort((a, b) => a.top - b.top);
      previewSections = next;
    }

    function recordPreviewMetrics(data) {
      const stage = $("#preview-stage");
      if (!stage || !data) return;
      recordPreviewSections(data);
      stage.dataset.previewMessageType = data.type || "";
      if (typeof data.contentHeight === "number" && data.contentHeight > 0) {
        stage.dataset.contentHeight = String(Math.round(data.contentHeight));
      }
      if (typeof data.contentWidth === "number" && data.contentWidth > 0) {
        stage.dataset.contentWidth = String(Math.round(data.contentWidth));
      }
      if (typeof data.pageHeight === "number" && data.pageHeight > 0) {
        stage.dataset.pageHeight = String(Math.round(data.pageHeight));
      }
      if (typeof data.pageWidth === "number" && data.pageWidth > 0) {
        stage.dataset.pageWidth = String(Math.round(data.pageWidth));
      }
    }
    window.addEventListener("message", (e) => {
      if (!e.data) return;
      const previewFrame = $("#preview");
      const previewMessageTypes = ["paged-error", "cv-content-rendered", "paged-rendered"];
      if (!previewMessageTypes.includes(e.data.type)) return;
      if (!previewFrame || e.source !== previewFrame.contentWindow) return;
      // sandbox="allow-scripts" without allow-same-origin forces an opaque
      // origin. Reject a same-origin message so a future sandbox relaxation
      // cannot silently reopen parent/child DOM access.
      if (e.origin !== "null") return;
      if (e.data.type === "paged-error") {
        recordPreviewMetrics(e.data);
        if (typeof e.data.contentHeight === "number" && e.data.contentHeight > 0) {
          cvContentHeight = e.data.contentHeight;
        }
        if (typeof e.data.contentWidth === "number" && e.data.contentWidth > 0) {
          cvContentWidth = e.data.contentWidth;
        }
        if (typeof e.data.pageHeight === "number" && e.data.pageHeight > 0) {
          cvPageHeight = e.data.pageHeight;
        }
        if (typeof e.data.pageWidth === "number" && e.data.pageWidth > 0) {
          cvPageWidth = e.data.pageWidth;
        }
        showPreviewLoading(false);
        undimPreview();
        lastPagedCount = null;
        updatePageCountError(e.data.message || "Pagination unavailable");
        applyZoom();
        if (pendingScrollTarget != null && scrollPreviewToSection(pendingScrollTarget, "auto")) {
          pendingScrollTarget = null;
        }
        ensurePreviewHighlightStyles();
        reapplyPendingHighlight();
        return;
      }
      if (e.data.type === "cv-content-rendered" || e.data.type === "paged-rendered") {
        recordPreviewMetrics(e.data);
        let dirty = false;
        if (typeof e.data.contentHeight === "number" && e.data.contentHeight > 0) {
          cvContentHeight = e.data.contentHeight;
          dirty = true;
        }
        if (typeof e.data.contentWidth === "number" && e.data.contentWidth > 0) {
          cvContentWidth = e.data.contentWidth;
          dirty = true;
        }
        if (typeof e.data.pageHeight === "number" && e.data.pageHeight > 0) {
          cvPageHeight = e.data.pageHeight;
          dirty = true;
        }
        if (typeof e.data.pageWidth === "number" && e.data.pageWidth > 0) {
          cvPageWidth = e.data.pageWidth;
          dirty = true;
        }
        if (dirty) applyZoom();
        if (e.data.type === "cv-content-rendered") {
          showPreviewLoading(false);
          undimPreview();
        }
        if (pendingScrollTarget != null && scrollPreviewToSection(pendingScrollTarget, "auto")) {
          pendingScrollTarget = null;
        }
        ensurePreviewHighlightStyles();
        reapplyPendingHighlight();
      }
      if (e.data.type !== "paged-rendered") return;
      const pages = e.data.pages | 0;
      const wasOverflow = lastPagedCount != null && lastPagedCount > 1;
      lastPagedCount = pages;
      updatePageCountUi(pages, wasOverflow);
      showPreviewLoading(false);
      undimPreview();
      if (pendingScrollTarget != null) {
        if (scrollPreviewToSection(pendingScrollTarget, "auto")) {
          pendingScrollTarget = null;
        }
      }
    });
  }

  function updatePageCountUi(pages, wasOverflow = false) {
    const pc = $("#page-count");
    const label = $("#page-count-text");
    if (previewMode !== "paged") {
      pc.setAttribute("hidden", "");
      return;
    }
    pc.removeAttribute("hidden");
    pc.classList.remove("is-fit", "is-overflow", "is-alarm");
    if (pages <= 0) {
      label.textContent = "…";
      return;
    }
    if (pages === 1) {
      label.textContent = "1 page";
      pc.classList.add("is-fit");
    } else {
      label.textContent = `Overflow · ${pages} pages`;
      pc.classList.add("is-overflow");
      // One-shot shake-and-glow only when transitioning into overflow,
      // not on every re-render.
      if (!wasOverflow) {
        // Force reflow so the animation re-fires.
        void pc.offsetWidth;
        pc.classList.add("is-alarm");
        pc.addEventListener(
          "animationend",
          () => pc.classList.remove("is-alarm"),
          { once: true },
        );
      }
    }
  }

  function updatePageCountError(message) {
    const pc = $("#page-count");
    const label = $("#page-count-text");
    if (!pc || !label || previewMode !== "paged") return;
    pc.removeAttribute("hidden");
    pc.classList.remove("is-fit", "is-alarm");
    pc.classList.add("is-overflow");
    label.textContent = message;
  }

  function setupModeToggle() {
    $$(".seg-btn[data-mode]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = btn.dataset.mode;
        if (m === previewMode) return;
        previewMode = m;
        localStorage.setItem(MODE_KEY, previewMode);
        applyModeUi();
        schedulePreview(20);
      });
    });
    applyModeUi();
  }

  function applyModeUi() {
    $$(".seg-btn[data-mode]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.mode === previewMode);
    });
    if (previewMode === "paged") {
      $("#page-count").removeAttribute("hidden");
      if (lastPagedCount != null) updatePageCountUi(lastPagedCount);
    } else {
      $("#page-count").setAttribute("hidden", "");
    }
  }

  // ──────────────────────────────────────────────────────────────────────
  //  APPEARANCE — accent / font / density.
  //
  //  All three are canonical YAML fields living on cvModel. The topbar
  //  is the single editing surface — the form's Header section
  //  intentionally does NOT include them.
  //
  //  Each control has a tiny refreshFrom...() helper so external code
  //  (theme picker, model reload) can sync the inputs.
  // ──────────────────────────────────────────────────────────────────────
  function setupSettingsButton() {
    const btn = $("#settings-btn");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      if (window.OnePagerNavigate) {
        await window.OnePagerNavigate("/account#settings");
        return;
      }
      window.location.href = "/account#settings";
    });
  }

  function setupAppearance() {
    const fontSel = $("#font-select");
    const densitySel = $("#density-select");
    const paperButtons = $$(".paper-theme-btn[data-paper-theme]");
    const accentInput = $("#accent-input");
    const accentTrigger = $("#accent-trigger");
    const accentValue = $("#accent-value");
    const swatch = $("#accent-swatch");

    function syncSwatchFrom(hex) {
      const safe = (hex || "#111111").trim();
      if (swatch) swatch.style.background = safe;
      if (accentValue) accentValue.textContent = safe.toUpperCase();
      if (accentTrigger) accentTrigger.setAttribute("aria-label", tr("app.accent_picker_aria", "Choose accent colour {value}", { value: safe.toUpperCase() }));
      if (accentInput && /^#[0-9a-f]{3,8}$/i.test(safe)) accentInput.value = safe;
    }
    function announceAppearanceChanged() {
      window.dispatchEvent(new CustomEvent("cv:appearance-changed"));
    }
    function patchAppearanceTarget(patch) {
      let target = cvModel;
      if (viewMode === "yaml" && editor) {
        try {
          const parsed = window.CvForm.yamlToModel(editor.getValue());
          if (!parsed || typeof parsed !== "object") throw new Error("invalid yaml");
          target = parsed;
        } catch (_) {
          setStatus("error", "Fix YAML before changing appearance");
          return false;
        }
      }
      if (patch.font && ["serif", "sans", "mono"].includes(patch.font)) target.font = patch.font;
      if (patch.density && ["tight", "normal", "airy"].includes(patch.density)) target.density = patch.density;
      if (patch.paper && ["white", "creme", "soft-gray", "blue-tint"].includes(patch.paper)) target.paper = patch.paper;
      if (patch.accent && /^#[0-9a-f]{3,8}$/i.test(patch.accent)) target.accent = patch.accent;
      if (target !== cvModel) {
        replaceModel(target);
        suppressEditorOnEdit = true;
        const cursor = editor.getCursor && editor.getCursor();
        editor.setValue(modelToYamlString());
        try { if (cursor && editor.setCursor) editor.setCursor(cursor); } catch (_) {}
        suppressEditorOnEdit = false;
      }
      return true;
    }
    applyAppearancePatch = function (patch) {
      if (!patchAppearanceTarget(patch || {})) return;
      refreshAppearanceUi();
      onFormChange();
      schedulePreview(20, { saveBeforeRender: false });
      announceAppearanceChanged();
    };

    if (fontSel) {
      fontSel.value = cvModel.font || "serif";
      fontSel.addEventListener("change", () => {
        applyAppearancePatch({ font: fontSel.value });
      });
    }
    if (densitySel) {
      densitySel.value = cvModel.density || "normal";
      densitySel.addEventListener("change", () => {
        applyAppearancePatch({ density: densitySel.value });
      });
    }
    if (paperButtons.length) {
      const syncPaper = () => {
        const active = getPaper();
        paperButtons.forEach((btn) => {
          btn.setAttribute("aria-pressed", btn.dataset.paperTheme === active ? "true" : "false");
        });
      };
      paperButtons.forEach((btn) => {
        btn.addEventListener("click", () => {
          applyAppearancePatch({ paper: btn.dataset.paperTheme || "white" });
          syncPaper();
        });
      });
      syncPaper();
    }
    if (accentInput) {
      syncSwatchFrom(cvModel.accent || "#111111");
      if (accentTrigger) {
        accentTrigger.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          accentInput.click();
        });
      }
      accentInput.addEventListener("click", (e) => e.stopPropagation());
      accentInput.addEventListener("input", () => {
        syncSwatchFrom(accentInput.value);
        applyAppearancePatch({ accent: accentInput.value });
      });
    }

    // Refresh helper — called after model load / theme apply / undo.
    refreshAppearanceUi = () => {
      if (fontSel) fontSel.value = cvModel.font || "serif";
      if (densitySel) densitySel.value = cvModel.density || "normal";
      if (paperButtons.length) {
        const active = getPaper();
        paperButtons.forEach((btn) => {
          btn.setAttribute("aria-pressed", btn.dataset.paperTheme === active ? "true" : "false");
        });
      }
      syncSwatchFrom(cvModel.accent || "#111111");
    };
  }
  let refreshAppearanceUi = () => { /* set by setupAppearance() */ };
  let applyAppearancePatch = () => { /* set by setupAppearance() */ };

  function setupTemplatePicker() {
    const btn = $("#template-btn");
    const modal = $("#template-modal");
    const closeBtn = $("#template-close");
    const gallery = $("#template-gallery");
    const searchInput = $("#template-search");
    const currentLabel = $("#template-current");
    const selectedLabel = $("#template-selected");
    const empty = $("#template-empty");
    const cancelBtn = $("#template-cancel");
    const applyBtn = $("#template-apply");
    if (!btn || !modal || !gallery) return;
    let items = fallbackTemplateItems();
    let opener = null;
    let modalSiblingState = [];
    let stagedTemplate = getTemplate();
    let filterQuery = "";
    let templatePreviewBatch = 0;
    const templatePreviewCache = new Map();

    function fallbackTemplateItems() {
      return TEMPLATE_IDS.map((id) => ({
        id,
        label: id.split("-").map((part) => part ? part[0].toUpperCase() + part.slice(1) : part).join(" "),
        use_case: "Built-in CV layout",
        renderer: id === "sidebar" ? "rail" : "standard",
        layout_family: id === "sidebar" ? "rail" : "single_column",
        column_count: id === "sidebar" ? 2 : 1,
        ats_priority: id === "sidebar" ? "medium" : "high",
        customization_axes: ["accent", "font", "density", "paper"],
        paper_sizes: ["A4"],
        one_page_risk: "normal",
        origin: "built-in",
      }));
    }

    function normalizeTemplateId(id) {
      return TEMPLATE_IDS.includes(id) ? id : "classic";
    }

    function templateField(tplOrId, field, fallback) {
      const tpl = typeof tplOrId === "object" && tplOrId ? tplOrId : items.find((item) => item.id === tplOrId);
      const id = normalizeTemplateId(tpl && tpl.id ? tpl.id : tplOrId);
      const key = id.replace(/-/g, "_");
      const raw = fallback || (tpl && tpl[field]) || (field === "label" ? id : "");
      return tr(`templates.${field}.${key}`, raw);
    }

    function templateLabel(id) {
      const tpl = items.find((item) => item.id === id);
      return templateField(tpl || id, "label", (tpl && (tpl.label || tpl.id)) || id);
    }

    function templatePreviewUrl(tplOrId) {
      const tpl = typeof tplOrId === "object" && tplOrId ? tplOrId : items.find((item) => item.id === tplOrId);
      const id = normalizeTemplateId(tpl && tpl.id ? tpl.id : tplOrId);
      const density = tpl && ["tight", "normal", "airy"].includes(tpl.default_density) ? tpl.default_density : getDensity();
      return `/api/preview?theme=light&mode=continuous&density=${density}&font=${getFont()}&accent=${encodeURIComponent(getAccent())}&template=${id}&paper=${getPaper()}&embed=1&thumb=1${idParam()}`;
    }

    function enumLabel(prefix, value, fallback) {
      const key = String(value || "").replace(/-/g, "_");
      return tr(`${prefix}.${key}`, fallback || String(value || ""));
    }

    function searchableTemplateText(tpl) {
      return [
        tpl.id,
        tpl.label,
        templateField(tpl, "label", tpl.label || tpl.id),
        tpl.use_case,
        templateField(tpl, "use_case", tpl.use_case || tpl.description || ""),
        tpl.description,
        tpl.renderer,
        tpl.layout_family,
        tpl.ats_priority,
        tpl.inspiration_source,
        tpl.import_export_notes,
      ].concat(Array.isArray(tpl.customization_axes) ? tpl.customization_axes : [])
        .concat(Array.isArray(tpl.paper_sizes) ? tpl.paper_sizes : [])
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
    }

    function filterTemplates() {
      const query = filterQuery.trim().toLowerCase();
      if (!query) return items;
      return items.filter((tpl) => searchableTemplateText(tpl).includes(query));
    }

    function updateTemplateSummary() {
      const active = getTemplate();
      if (currentLabel) currentLabel.textContent = tr("templates.current", "Current: {name}", { name: templateLabel(active) });
      if (selectedLabel) selectedLabel.textContent = tr("templates.selected", "Selected: {name}", { name: templateLabel(stagedTemplate) });
      if (applyBtn) applyBtn.disabled = normalizeTemplateId(stagedTemplate) === active;
    }

    function updateTemplateCardSelection() {
      const active = getTemplate();
      const cards = Array.from(gallery.querySelectorAll("[data-template-card]"));
      const hasSelectedVisible = cards.some((card) => (
        normalizeTemplateId(card.getAttribute("data-template-card") || "") === stagedTemplate
      ));
      cards.forEach((card, index) => {
        const id = card.getAttribute("data-template-card") || "";
        const isActive = id === active;
        const isSelected = id === stagedTemplate;
        card.classList.toggle("is-active-template", isActive);
        card.setAttribute("data-active-template", isActive ? "true" : "false");
        card.setAttribute("aria-checked", isSelected ? "true" : "false");
        card.tabIndex = isSelected || (!hasSelectedVisible && index === 0) ? 0 : -1;
        const badge = card.querySelector(".template-card-state");
        if (badge) {
          badge.textContent = isSelected
            ? tr("templates.selected_badge", "Selected")
            : (isActive ? tr("templates.current_badge", "Current template") : "");
          badge.hidden = !isSelected && !isActive;
        }
      });
      updateTemplateSummary();
    }

    function renderTemplateGallery() {
      // Render (and fetch 8 full CV previews) only while the modal is open.
      // openModal() always re-renders, so background appearance/i18n events
      // on a hidden modal cost nothing — before this guard, page load plus
      // those events fired 24 thumbnail renders per visit (plan 107, F5/F15).
      if (modal.hasAttribute("hidden")) return;
      const active = getTemplate();
      const visibleItems = filterTemplates();
      if (empty) empty.hidden = visibleItems.length > 0;
      const previewBatch = ++templatePreviewBatch;
      gallery.innerHTML = visibleItems.map((tpl) => {
        const id = escapeHtml(tpl.id);
        const label = templateField(tpl, "label", tpl.label || tpl.id);
        const checked = tpl.id === stagedTemplate ? "true" : "false";
        const activeClass = tpl.id === active ? " is-active-template" : "";
        const useCase = templateField(tpl, "use_case", tpl.use_case || tpl.description || "");
        const renderer = tpl.renderer || "standard";
        const risk = tpl.one_page_risk || "normal";
        const layout = tpl.layout_family || "single_column";
        const columns = tpl.column_count || 1;
        const atsPriority = tpl.ats_priority || "high";
        const layoutLabel = enumLabel("templates.layout", layout, layout.replace(/_/g, " "));
        const atsLabel = enumLabel("templates.ats_priority", atsPriority, atsPriority);
        const riskLabel = enumLabel("templates.risk", risk, risk);
        const columnsLabel = tr("templates.columns", "{n} col", { n: String(columns) });
        const atsChip = tr("templates.ats_chip", "ATS {value}", { value: atsLabel });
        // The aria description mirrors the VISIBLE chips verbatim — screen
        // readers used to hear "One-page Normal risk" where sighted users saw
        // "Normal risk" (plan 107, F6).
        const aria = [
          tr("templates.select_aria", "Select {name} template", { name: label }),
          tpl.id === active ? tr("templates.current_badge", "Current template") : "",
          tpl.id === stagedTemplate ? tr("templates.selected_badge", "Selected") : "",
          useCase,
          `${layoutLabel}, ${columnsLabel}, ${atsChip}, ${riskLabel}`,
        ].filter(Boolean).join(". ");
        return (
          `<div role="radio" tabindex="${checked === "true" ? "0" : "-1"}" class="template-card${activeClass}" data-template-card="${id}" data-active-template="${tpl.id === active ? "true" : "false"}" aria-checked="${checked}" aria-label="${escapeHtml(aria)}">` +
            `<span class="template-preview-shell" aria-hidden="true">` +
              `<iframe class="template-preview-frame" data-preview-template="${id}" data-template-preview-url="${escapeHtml(templatePreviewUrl(tpl))}" title="" tabindex="-1" loading="lazy" sandbox="allow-scripts" referrerpolicy="no-referrer" data-renderer="${escapeHtml(renderer)}"></iframe>` +
            `</span>` +
            `<span class="template-card-body">` +
              `<span class="template-card-state" ${tpl.id === active || tpl.id === stagedTemplate ? "" : "hidden"}>${escapeHtml(tpl.id === stagedTemplate ? tr("templates.selected_badge", "Selected") : tr("templates.current_badge", "Current template"))}</span>` +
              `<span class="template-card-title">${escapeHtml(label)}</span>` +
              `<span class="template-card-use">${escapeHtml(useCase)}</span>` +
              `<span class="template-card-chips">` +
                `<span>${escapeHtml(layoutLabel)}</span>` +
                `<span>${escapeHtml(columnsLabel)}</span>` +
                `<span>${escapeHtml(atsChip)}</span>` +
                `<span>${escapeHtml(riskLabel)}</span>` +
              `</span>` +
            `</span>` +
          `</div>`
        );
      }).join("");
      updateTemplateCardSelection();
      loadTemplatePreviewFrames(previewBatch);
    }

    function loadTemplatePreviewFrames(batch) {
      gallery.querySelectorAll(".template-preview-frame[data-template-preview-url]").forEach((frame) => {
        const url = frame.getAttribute("data-template-preview-url");
        if (!url) return;
        const shell = frame.closest(".template-preview-shell");
        // The cache maps preview URL (which encodes template, density, font,
        // accent, paper, and CV id) → rendered HTML, or the in-flight fetch
        // Promise so concurrent renders share one request (plan 107, F5).
        let entry = templatePreviewCache.get(url);
        if (typeof entry === "string") {
          frame.removeAttribute("src");
          frame.srcdoc = entry;
          frame.dataset.loaded = "true";
          if (shell) {
            shell.removeAttribute("data-preview-loading");
            shell.removeAttribute("data-preview-error");
          }
          return;
        }
        frame.dataset.loaded = "";
        if (shell) {
          shell.setAttribute("data-preview-loading", "");
          shell.removeAttribute("data-preview-error");
        }
        if (!entry) {
          entry = fetch(url, { headers: { Accept: "text/html" } })
            .then((res) => {
              if (!res.ok) throw new Error(`template preview ${res.status}`);
              return res.text();
            })
            .catch((err) => {
              templatePreviewCache.delete(url);
              throw err;
            });
          if (templatePreviewCache.size > 64) templatePreviewCache.clear();
          templatePreviewCache.set(url, entry);
        }
        entry
          .then((html) => {
            templatePreviewCache.set(url, html);
            if (batch !== templatePreviewBatch || !frame.isConnected) return;
            frame.removeAttribute("src");
            frame.srcdoc = html;
            frame.dataset.loaded = "true";
            if (shell) shell.removeAttribute("data-preview-loading");
          })
          .catch(() => {
            if (batch !== templatePreviewBatch || !frame.isConnected) return;
            frame.removeAttribute("src");
            frame.srcdoc = "<!doctype html><html><body></body></html>";
            frame.dataset.loaded = "false";
            // A failed thumbnail must say so, not sit as a blank white card;
            // the template stays selectable either way (plan 107, F4).
            if (shell) {
              shell.removeAttribute("data-preview-loading");
              shell.setAttribute("data-preview-error", tr("templates.preview_error", "Preview unavailable"));
            }
          });
      });
    }

    function setTemplateModalInert(active) {
      if (active) {
        const targets = [];
        let node = modal;
        while (node && node.parentElement && node.parentElement !== document.documentElement) {
          Array.from(node.parentElement.children).forEach((sibling) => {
            if (sibling !== node && !sibling.contains(modal)) targets.push(sibling);
          });
          node = node.parentElement;
          if (node === document.body) break;
        }
        modalSiblingState = targets.map((child) => ({
          child,
          inert: child.inert,
          ariaHidden: child.getAttribute("aria-hidden"),
        }));
        modalSiblingState.forEach(({ child }) => {
          child.inert = true;
          child.setAttribute("aria-hidden", "true");
        });
        return;
      }
      modalSiblingState.forEach(({ child, inert, ariaHidden }) => {
        child.inert = inert;
        if (ariaHidden == null) child.removeAttribute("aria-hidden");
        else child.setAttribute("aria-hidden", ariaHidden);
      });
      modalSiblingState = [];
    }

    function closeModal(restore) {
      if (modal.hasAttribute("hidden")) return;
      modal.setAttribute("hidden", "");
      setTemplateModalInert(false);
      btn.setAttribute("aria-expanded", "false");
      btn.classList.remove("is-open");
      if (restore !== false && opener && typeof opener.focus === "function") {
        try { opener.focus(); } catch (_) {}
      }
      opener = null;
    }

    function cancelModal() {
      stagedTemplate = getTemplate();
      closeModal(true);
    }

    function applyTemplate() {
      const next = normalizeTemplateId(stagedTemplate);
      if (next === getTemplate()) {
        closeModal(true);
        return;
      }
      const tpl = items.find((item) => item.id === next);
      cvModel.template = next;
      if (tpl && ["tight", "normal", "airy"].includes(tpl.default_density)) {
        cvModel.density = tpl.default_density;
      }
      refreshAppearanceUi();
      // In YAML view the CodeMirror buffer — not cvModel — is what save
      // serialises (saveNow: viewMode === "form" ? model : editor.getValue()).
      // applyTemplate only mutates cvModel, so without mirroring the change
      // into the buffer the new template/density is silently dropped on the
      // next save while preview/PDF (which read the model) diverge. Sync it.
      if (viewMode !== "form") refreshYamlBufferFromModel();
      onFormChange();
      schedulePreview(20, { saveBeforeRender: false });
      window.dispatchEvent(new CustomEvent("cv:appearance-changed"));
      closeModal(true);
    }

    function openModal() {
      announceTopbarPopoverOpen("template-modal");
      opener = document.activeElement;
      stagedTemplate = getTemplate();
      filterQuery = "";
      if (searchInput) searchInput.value = "";
      // Un-hide before rendering: renderTemplateGallery() no-ops on a hidden
      // modal (the F5 fetch-churn guard), so the order matters here.
      modal.removeAttribute("hidden");
      renderTemplateGallery();
      setTemplateModalInert(true);
      btn.setAttribute("aria-expanded", "true");
      btn.classList.add("is-open");
      window.requestAnimationFrame(() => {
        if (searchInput) searchInput.focus({ preventScroll: true });
        else {
          const active = gallery.querySelector('[aria-checked="true"]') || gallery.querySelector("[data-template-card]");
          if (active) active.focus({ preventScroll: true });
        }
      });
    }

    btn.addEventListener("click", openModal);
    function refreshTemplateI18n() {
      gallery.setAttribute("aria-label", tr("templates.title", "Templates"));
      renderTemplateGallery();
    }

    gallery.setAttribute("role", "radiogroup");
    gallery.setAttribute("aria-label", tr("templates.title", "Templates"));
    if (closeBtn) closeBtn.addEventListener("click", cancelModal);
    if (cancelBtn) cancelBtn.addEventListener("click", cancelModal);
    if (applyBtn) applyBtn.addEventListener("click", applyTemplate);
    if (searchInput) {
      let searchDebounce = 0;
      searchInput.addEventListener("input", () => {
        filterQuery = searchInput.value || "";
        // Debounced: rebuilding the gallery per keystroke re-created every
        // card iframe mid-word (plan 107, F5). Thumbnail HTML itself comes
        // from the cache, so a re-render after the pause costs no fetches.
        window.clearTimeout(searchDebounce);
        searchDebounce = window.setTimeout(renderTemplateGallery, 150);
      });
    }
    modal.addEventListener("click", (event) => {
      if (event.target === modal) cancelModal();
    });
    gallery.addEventListener("click", (event) => {
      const card = event.target.closest("[data-template-card]");
      if (!card) return;
      stagedTemplate = normalizeTemplateId(card.getAttribute("data-template-card") || "classic");
      updateTemplateCardSelection();
    });
    gallery.addEventListener("keydown", (event) => {
      const current = event.target.closest("[data-template-card]");
      if (!current) return;
      const cards = Array.from(gallery.querySelectorAll("[data-template-card]"));
      const index = cards.indexOf(current);
      if (index < 0) return;
      let nextIndex = index;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (index + 1) % cards.length;
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (index - 1 + cards.length) % cards.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = cards.length - 1;
      else if (event.key === " " || event.key === "Enter") nextIndex = index;
      else return;
      event.preventDefault();
      const nextCard = cards[nextIndex];
      if (!nextCard) return;
      stagedTemplate = normalizeTemplateId(nextCard.getAttribute("data-template-card") || "classic");
      updateTemplateCardSelection();
      nextCard.focus({ preventScroll: true });
    });
    document.addEventListener("keydown", (event) => {
      if (modal.hasAttribute("hidden")) return;
      const card = modal.querySelector(".modal-card");
      if (card && trapPopoverTab(event, card)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        cancelModal();
      }
    });
    bindTopbarPopoverClose("template-modal", closeModal);
    fetch("/api/templates")
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        items = data && Array.isArray(data.templates) && data.templates.length ? data.templates : fallbackTemplateItems();
        renderTemplateGallery();
      })
      .catch(() => {
        items = fallbackTemplateItems();
        renderTemplateGallery();
      });
    window.addEventListener("cv:appearance-changed", renderTemplateGallery);
    window.addEventListener("cv:i18n-changed", refreshTemplateI18n);
  }

  function previewUrl(opts = {}) {
    const theme = opts.theme || "light";
    const embedded = opts.embedded ? "&embed=1" : "";
    return `/api/preview?theme=${theme}&mode=${previewMode}&density=${getDensity()}&font=${getFont()}&accent=${encodeURIComponent(getAccent())}&template=${getTemplate()}&paper=${getPaper()}${embedded}${idParam()}&t=${Date.now()}`;
  }

  function withTrustedPreviewBase(html) {
    const baseTag = `<base href="${escapeHtml(`${window.location.origin}/`)}">`;
    if (/<head(?:\s[^>]*)?>/i.test(html)) {
      return html.replace(/<head(?:\s[^>]*)?>/i, (head) => `${head}${baseTag}`);
    }
    return `${baseTag}${html}`;
  }

  function setupPreviewActions() {
    $("#preview-open-btn").addEventListener("click", async () => {
      const popup = window.open("about:blank", "_blank");
      if (!popup) {
        setStatus("error", "Preview popup blocked");
        return;
      }
      popup.document.title = "Opening preview";
      popup.document.body.style.margin = "0";
      popup.document.body.style.font = "14px system-ui, sans-serif";
      popup.document.body.textContent = "Opening preview...";
      popup.opener = null;
      if (dirty || pendingForceLint) {
        const forceLint = pendingForceLint;
        pendingForceLint = false;
        const saved = await save({ forceLint });
        if (!saved) {
          popup.close();
          return;
        }
      }
      try {
        const res = await fetch(previewUrl({ embedded: false }), { headers: { Accept: "text/html" } });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const html = await res.text();
        const blob = new Blob([withTrustedPreviewBase(html)], { type: "text/html" });
        const objectUrl = URL.createObjectURL(blob);
        popup.location.replace(objectUrl);
        setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
      } catch (err) {
        console.error("Preview open failed:", err);
        popup.close();
        setStatus("error", "Preview failed");
      }
    });
    // Refresh button always forces an immediate render — bypasses the
    // autoRender toggle (that's the whole point of "Manual").
    $("#refresh-btn").addEventListener("click", () => schedulePreview(0));
    setupZoomControls();
  }

  // ──────────────────────────────────────────────────────────────────────
  //  AUTO-RENDER TOGGLE (Live ↔ Manual)
  //
  //  Segmented control — both states visible at once so it reads as a
  //  switch, not a status pill. Click either side to set that mode.
  // ──────────────────────────────────────────────────────────────────────
  function setupAutoRenderToggle() {
    const buttons = $$("#auto-render .seg-btn[data-auto]");
    if (!buttons.length) return;
    applyAutoRenderUi();
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const next = btn.dataset.auto === "live";
        if (next === autoRender) return;
        autoRender = next;
        localStorage.setItem(AUTO_KEY, String(autoRender));
        applyAutoRenderUi();
        // Flipping to Live with pending edits = "render now please".
        if (autoRender && pendingRefresh) {
          schedulePreview(0);
        }
      });
    });
  }

  function applyAutoRenderUi() {
    $$("#auto-render .seg-btn[data-auto]").forEach((b) => {
      const active = (b.dataset.auto === "live") === autoRender;
      b.classList.toggle("is-active", active);
      b.setAttribute("aria-pressed", active ? "true" : "false");
    });
    if (autoRender) clearPending();
  }

  // ──────────────────────────────────────────────────────────────────────
  //  LINT-ON-SAVE TOGGLE — visible only in YAML view. Default ON.
  //  Click flips. Lint runs only on Cmd+S / Save button — never on the
  //  1.5 s autosave that fires while you're typing.
  // ──────────────────────────────────────────────────────────────────────
  function setupLintToggle() {
    const btn = $("#lint-toggle");
    if (!btn) return;
    applyLintToggleUi();
    btn.addEventListener("click", () => {
      lintOnSave = !lintOnSave;
      localStorage.setItem(LINT_KEY, String(lintOnSave));
      applyLintToggleUi();
    });
  }

  function applyLintToggleUi() {
    const btn = $("#lint-toggle");
    if (!btn) return;
    btn.setAttribute("aria-pressed", lintOnSave ? "true" : "false");
    const state = $(".lint-toggle-state", btn);
    if (state) state.textContent = lintOnSave ? "on" : "off";
    btn.dataset.tooltip = lintOnSave
      ? "Format YAML on ⌘S. Click to disable — your formatting will be preserved."
      : "Save preserves your YAML exactly. Click to re-enable formatting on ⌘S.";
  }

  // ──────────────────────────────────────────────────────────────────────
  //  BUILD PDF — explicit "render the actual PDF and open it" button.
  //  Skips the live preview's paged.js path and goes through WeasyPrint
  //  via the same engine the CLI uses, so what you get is exactly the
  //  print artefact. Errors (overflow, validation) come back as a toast
  //  with the offending section name.
  // ──────────────────────────────────────────────────────────────────────
  // Success morph on the two most-used actions (plan 108 stage 4): the CSS
  // swaps the action icon for a spring check + success tint while
  // .is-success is present, then the button settles back.
  const actionSuccessTimers = new WeakMap();
  function flashActionSuccess(btn) {
    if (!btn) return;
    btn.classList.remove("is-success");
    void btn.offsetWidth; // restart the pop on back-to-back successes
    btn.classList.add("is-success");
    clearTimeout(actionSuccessTimers.get(btn));
    actionSuccessTimers.set(btn, setTimeout(() => btn.classList.remove("is-success"), 900));
  }

  function setupBuildButton() {
    const btn = $("#build-btn");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      if (buildInFlight) return;
      if (!activeCvId) {
        showBuildToast("No active CV", "Create or import a CV before building a PDF.", "neutral");
        return;
      }
      buildInFlight = true;
      btn.disabled = true;
      let builtOk = false;
      const originalHTML = btn.innerHTML;
      const pdfTab = window.open("about:blank", "_blank");
      if (pdfTab) {
        try {
          pdfTab.opener = null;
          pdfTab.document.title = "Building PDF";
          pdfTab.document.body.textContent = "Building PDF...";
        } catch (_) {}
      }
      // Always save first so the server-side document reflects the form/buffer.
      if (dirty) {
        const saved = await save({ forceLint: lintOnSave && viewMode === "yaml" });
        if (!saved) {
          closeReservedWindow(pdfTab);
          showBuildToast("Save failed", "Build stopped so the PDF cannot use stale YAML.", "danger");
          buildInFlight = false;
          btn.disabled = false;
          return;
        }
      }
      btn.classList.add("is-busy");
      btn.innerHTML =
        '<span data-icon="loader" data-icon-size="13"></span><span>Building…</span>';
      if (window.renderIcons) window.renderIcons(btn);
      try {
        const res = await fetch("/api/build", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ density: getDensity(), template: getTemplate(), paper: getPaper(), id: activeCvId }),
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          if (data.error === "overflow") {
            closeReservedWindow(pdfTab);
            showBuildToast(
              "Overflow",
              `${data.pages} pages — first overflow: ${data.section}. Switch density to tight or trim a bullet.`,
              "danger",
            );
          } else {
            closeReservedWindow(pdfTab);
            showBuildToast("Build failed", data.error || `HTTP ${res.status}`, "danger");
          }
          return;
        }
        // Success — open the PDF in a new tab. Two delivery modes (Stage 8):
        //   • R2 configured → JSON {url, cached, page_count}: open the signed
        //     R2 URL directly (the bytes never transit the app a second time).
        //   • R2 unset (dev) → an application/pdf blob: object-URL + open.
        const ctype = res.headers.get("Content-Type") || "";
        if (ctype.indexOf("application/json") !== -1) {
          const data = await res.json();
          const opened = openPdfTarget(data.url, "cv.pdf", pdfTab);
          showBuildToast(
            "Built",
            (data.cached ? "Served from cache" : "1 page") + (opened ? " · opened in a new tab." : " · downloaded."),
            "success",
          );
          builtOk = true;
        } else {
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const opened = openPdfTarget(url, "cv.pdf", pdfTab);
          // Free the blob URL after a beat (the new tab has already loaded).
          setTimeout(() => URL.revokeObjectURL(url), 30000);
          showBuildToast("Built", opened ? "1 page · opened in a new tab." : "1 page · downloaded.", "success");
          builtOk = true;
        }
        // Activation signal — a successful export (no-op unless configured).
        trackEvent("first_export");
      } catch (err) {
        closeReservedWindow(pdfTab);
        showBuildToast("Build failed", String(err.message || err), "danger");
      } finally {
        buildInFlight = false;
        btn.disabled = false;
        btn.classList.remove("is-busy");
        btn.innerHTML = originalHTML;
        if (window.renderIcons) window.renderIcons(btn);
        if (builtOk) flashActionSuccess(btn);
      }
    });
  }

  function closeReservedWindow(win) {
    if (!win) return;
    try { win.close(); } catch (_) {}
  }

  function openPdfTarget(url, filename, reservedWindow) {
    if (reservedWindow && !reservedWindow.closed) {
      try {
        reservedWindow.location.href = url;
        return true;
      } catch (_) {}
    }
    const opened = window.open("about:blank", "_blank");
    if (opened) {
      try {
        opened.opener = null;
        opened.location.href = url;
        return true;
      } catch (_) {}
    }
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "document.pdf";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    return false;
  }

  // ──────────────────────────────────────────────────────────────────────
  //  DOCUMENT PICKER (Stage 3) — lists the user's CVs (Postgres, RLS-scoped)
  //  by UUID. Switch reloads the form from that doc's yaml_blob. Per-row
  //  actions: rename (inline), duplicate, delete (with confirm). A "New CV"
  //  button seeds a fresh skeleton; "Download YAML" exports the active doc
  //  client-side (also seeds Stage 10's export). Replaces the old
  //  filename-based variant switcher — CVs are documents now, not files.
  // ──────────────────────────────────────────────────────────────────────
  let cvPickerRefresh = () => {};  // exposed so other code can refresh the list

  function setupCvSwitcher() {
    const btn = $("#cv-switcher");
    const pop = $("#cv-switcher-pop");
    const list = $("#cv-switcher-list");
    const closeBtn = $("#cv-catalog-close");
    const catalogSearch = $("#cv-catalog-search");
    const catalogNewBtn = $("#cv-catalog-new-btn");
    const docBtn = $("#doc-actions-btn");
    const docPop = $("#doc-actions-popover");
    const newBtn = $("#cv-new-btn");
    const dlBtn = $("#cv-download-btn");
    const renameActiveBtn = $("#cv-rename-active-btn");
    const duplicateActiveBtn = $("#cv-duplicate-active-btn");
    const deleteActiveBtn = $("#cv-delete-active-btn");
    const label = $("#cv-switcher-label");
    const limitNote = $("#cv-limit-note");
    const catalogLimitNote = $("#cv-catalog-limit-note");
    if (!btn || !pop) return;

    let isOpen = false;
    let docOpen = false;
    let lastCvs = [];
    let lastDocuments = [];
    let documentFilter = "all";
    let catalogQuery = "";
    let opener = null;
    let docOpener = null;
    let modalSiblingState = [];
    syncCvSwitcherLabel(label, activeCvName);

    function setCatalogModalInert(active) {
      if (active) {
        const targets = [];
        let node = pop;
        while (node && node.parentElement && node.parentElement !== document.documentElement) {
          Array.from(node.parentElement.children).forEach((sibling) => {
            if (sibling !== node && !sibling.contains(pop)) targets.push(sibling);
          });
          node = node.parentElement;
          if (node === document.body) break;
        }
        modalSiblingState = targets.map((child) => ({
          child,
          inert: child.inert,
          ariaHidden: child.getAttribute("aria-hidden"),
        }));
        modalSiblingState.forEach(({ child }) => {
          child.inert = true;
          child.setAttribute("aria-hidden", "true");
        });
        return;
      }
      modalSiblingState.forEach(({ child, inert, ariaHidden }) => {
        child.inert = inert;
        if (ariaHidden == null) child.removeAttribute("aria-hidden");
        else child.setAttribute("aria-hidden", ariaHidden);
      });
      modalSiblingState = [];
    }

    function positionDocActions() {
      if (!docBtn || !docPop) return;
      const docRect = docBtn.getBoundingClientRect();
      const docVisible = docRect.width > 0 && docRect.height > 0 && getComputedStyle(docBtn).display !== "none";
      const utilityBtn = $("#mobile-utility-btn");
      const anchor = docVisible ? docBtn : (utilityBtn || docBtn);
      positionAnchoredPopover(anchor, docPop, { align: "right" });
    }

    function fmtWhen(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d.getTime())) return "";
      const sec = Math.round((Date.now() - d.getTime()) / 1000);
      if (sec < 60) return tr("catalog.just_now", "just now");
      if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
      if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
      return `${Math.round(sec / 86400)}d ago`;
    }

    function docTypeLabel(type) {
      return type === "cover_letter" ? tr("lib.badge.cover", "Cover letter") : tr("lib.badge.cv", "CV");
    }

    function docTitle(doc) {
      return doc.title || doc.name || (doc.type === "cover_letter" ? tr("lib.badge.cover", "Cover letter") : "Untitled CV");
    }

    function docSearchText(doc) {
      return [
        docTitle(doc),
        docTypeLabel(doc.type),
        doc.company,
        doc.role,
        doc.job_label,
        doc.provenance_summary,
      ].filter(Boolean).join(" ").toLowerCase();
    }

    function docOpenUrl(doc) {
      const fallback = doc.type === "cover_letter"
        ? `/cover-letters?id=${encodeURIComponent(doc.id)}`
        : `/app?id=${encodeURIComponent(doc.id)}`;
      const raw = String(doc.open_url || "");
      if (!raw) return fallback;
      try {
        const url = new URL(raw, window.location.origin);
        const sameOrigin = url.origin === window.location.origin;
        const validPath = doc.type === "cover_letter"
          ? url.pathname === "/cover-letters"
          : url.pathname === "/app";
        return sameOrigin && validPath ? (url.pathname + url.search + url.hash) : fallback;
      } catch (_) {
        return fallback;
      }
    }

    function packetLineage(doc) {
      return doc && doc.packet_lineage && typeof doc.packet_lineage === "object" ? doc.packet_lineage : null;
    }

    function packetState(lineage) {
      if (!lineage) return "";
      if (lineage.archived_at || lineage.status === "archived") return "archived";
      if (lineage.status === "stale" || (Array.isArray(lineage.stale_reasons) && lineage.stale_reasons.length)) return "stale";
      return lineage.status || "";
    }

    function packetUsable(lineage) {
      return !!lineage &&
        lineage.document_role === "tailored_cv" &&
        packetState(lineage) === "packet_created" &&
        lineage.cover_letter_review_status === "confirmed" &&
        !!lineage.cover_letter_review_confirmed_at &&
        !!lineage.cover_letter_review_id &&
        !(Array.isArray(lineage.stale_reasons) && lineage.stale_reasons.length);
    }

    function packetLabel(lineage) {
      const state = packetState(lineage);
      if (!state) return "";
      const version = lineage && lineage.packet_version ? ` v${lineage.packet_version}` : "";
      if (state === "stale") return `Stale packet${version}`;
      if (state === "archived") return `Archived packet${version}`;
      if (state === "packet_created") return `Packet${version}`;
      if (state === "review_applied") return `Incomplete packet${version}`;
      return `${state.replace(/_/g, " ")}${version}`;
    }

    async function openDocument(doc) {
      if (!doc || !doc.id) return;
      if (doc.type === "cv") {
        await switchCv(doc.id, docTitle(doc));
        return;
      }
      const saved = await saveBeforeLeavingEditor();
      if (!saved) {
        showBuildToast("Save failed", "Cover letter was not opened because current CV edits are still unsaved.", "danger");
        return;
      }
      window.location.href = docOpenUrl(doc);
    }

    async function navigateFromCatalog(href) {
      const saved = await saveBeforeLeavingEditor();
      if (!saved) {
        showBuildToast("Save failed", "Navigation was stopped so current edits are not lost.", "danger");
        return;
      }
      window.location.href = href;
    }

    function openPdfUrl(url, filename, win) {
      if (win) {
        win.location.href = url;
        return;
      }
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      if (filename) a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    async function buildDocumentPdf(doc) {
      if (!doc || !doc.id) return;
      if (doc.type === "cv") {
        const switched = await switchCv(doc.id, docTitle(doc));
        if (!switched) {
          showBuildToast("Save failed", "PDF build was stopped because current edits are still unsaved.", "danger");
          return;
        }
        const build = $("#build-btn");
        if (build) build.click();
        return;
      }
      const win = window.open("about:blank", "_blank", "noopener");
      try {
        const res = await fetch(`/api/cover-letter/${encodeURIComponent(doc.id)}/build`, { method: "POST" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const ct = res.headers.get("content-type") || "";
        if (ct.includes("application/json")) {
          const data = await res.json();
          if (!data.url) throw new Error("PDF response did not include a URL");
          openPdfUrl(data.url, "cover-letter.pdf", win);
        } else {
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          openPdfUrl(url, "cover-letter.pdf", win);
          setTimeout(() => URL.revokeObjectURL(url), 60000);
        }
        showBuildToast("PDF ready", "Cover-letter PDF opened in a new tab.", "success");
      } catch (err) {
        if (win) win.close();
        showBuildToast("PDF failed", String(err.message || err), "danger");
      }
    }

    async function renameDocumentFromCatalog(doc) {
      if (!doc || !doc.id) return;
      showRenameDialog(
        tr("catalog.rename_doc", "Rename {type}", { type: docTypeLabel(doc.type) }),
        docTitle(doc),
        async (name) => {
          if (!name || name === docTitle(doc)) return;
          if (doc.type === "cv") {
            await renameCv(doc.id, name);
            return;
          }
          try {
            const res = await fetch(`/api/cover-letter/${encodeURIComponent(doc.id)}/rename`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ name }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
            await refreshList();
            focusCatalogRow(doc.id);
            showBuildToast("Renamed", `Updated "${shortText(data.name || name, 40)}".`, "success");
          } catch (err) {
            showBuildToast("Couldn't rename", String(err.message || err), "danger");
          }
        },
      );
    }

    async function deleteDocumentFromCatalog(doc) {
      if (!doc || !doc.id) return;
      if (doc.type === "cv") {
        confirmDelete({ id: doc.id, name: docTitle(doc) });
        return;
      }
      showConfirmDialog(
        tr("catalog.delete_cover", "Delete this cover letter?"),
        `"${docTitle(doc)}" will be permanently removed. This can't be undone.`,
        "Delete",
        async () => {
          try {
            const res = await fetch(`/api/cover-letter/${encodeURIComponent(doc.id)}`, { method: "DELETE" });
            const data = await res.json().catch(() => ({}));
            const committed = res.ok || (res.status === 503 && data.document_deleted === true);
            if (!committed) throw new Error(data.error || `HTTP ${res.status}`);
            await refreshList();
            showBuildToast(
              "Deleted",
              res.ok ? "The cover letter was removed." : "The cover letter was removed; PDF cleanup is pending.",
              res.ok ? "success" : "neutral",
            );
          } catch (err) {
            showBuildToast("Couldn't delete", String(err.message || err), "danger");
          }
        },
      );
    }

    function addCatalogAction(actions, label, onClick, name, actionName) {
      const action = document.createElement("button");
      action.type = "button";
      action.className = "cv-catalog-action";
      if (actionName) action.dataset.catalogAction = actionName;
      action.textContent = label;
      if (name) action.setAttribute("aria-label", `${label}: ${name}`);
      action.addEventListener("click", onClick);
      actions.appendChild(action);
    }

    function updateCatalogCounts(documents) {
      const counts = documents.reduce((acc, doc) => {
        acc.all += 1;
        if (doc.type === "cv") acc.cv += 1;
        if (doc.type === "cover_letter") acc.cover_letter += 1;
        return acc;
      }, { all: 0, cv: 0, cover_letter: 0 });
      Object.keys(counts).forEach((key) => {
        const el = pop.querySelector(`[data-doc-count="${key}"]`);
        if (el) el.textContent = String(counts[key]);
      });
    }

    function sectionLabel(type) {
      if (type === "job_artifact") return tr("catalog.section.job_artifacts", "Job artifacts");
      if (type === "cover_letter") return tr("lib.filter.cover", "Cover letters");
      return tr("lib.filter.cv", "CVs");
    }

    function catalogSection(doc) {
      if (doc.job_id) return "job_artifact";
      return doc.type === "cover_letter" ? "cover_letter" : "cv";
    }

    function makeCatalogRow(doc) {
      const row = document.createElement("div");
      row.className = "cv-row cv-catalog-row";
      if (doc.type === "cv" && doc.id === activeCvId) row.classList.add("is-active");
      row.dataset.id = doc.id;
      row.dataset.docType = doc.type;
      row.dataset.openable = "true";
      const lineage = packetLineage(doc);
      if (lineage && lineage.packet_id) {
        row.dataset.packetId = lineage.packet_id;
        row.dataset.packetState = packetState(lineage);
        row.dataset.packetVersion = lineage.packet_version || "";
        row.dataset.packetUsable = packetUsable(lineage) ? "true" : "false";
      }
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("title", docTitle(doc));
      row.setAttribute("aria-label", `${docTypeLabel(doc.type)}: ${docTitle(doc)}`);

      const main = document.createElement("div");
      main.className = "cv-catalog-main";

      const openRow = () => dispatchDocumentAction("catalog-open", { doc });
      row.addEventListener("click", (event) => {
        if (event.target.closest(".cv-catalog-action")) return;
        openRow();
      });
      row.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        if (event.target.closest(".cv-catalog-action")) return;
        event.preventDefault();
        openRow();
      });

      const titleLine = document.createElement("div");
      titleLine.className = "cv-catalog-title-line";
      const type = document.createElement("span");
      type.className = "cv-catalog-type";
      type.textContent = docTypeLabel(doc.type);
      const name = document.createElement("span");
      name.className = "cv-name";
      name.textContent = docTitle(doc);
      titleLine.append(type, name);

      const meta = document.createElement("div");
      meta.className = "cv-catalog-meta";
      const updated = fmtWhen(doc.updated_at);
      meta.innerHTML =
        `<span>${escapeHtml(updated ? tr("catalog.updated", "Updated {when}", { when: updated }) : tr("catalog.no_timestamp", "No update timestamp"))}</span>` +
        (doc.company || doc.role
          ? `<span>${escapeHtml([doc.company, doc.role].filter(Boolean).join(" - "))}</span>`
          : "");

      const provenance = document.createElement("div");
      provenance.className = "cv-catalog-provenance";
      const pState = packetState(lineage);
      const provenanceText = pState === "stale"
        ? tr("catalog.stale_job_artifact", "Stale job artifact: {job}", { job: doc.job_label || lineage.job_title || "linked job" })
        : pState === "archived"
          ? tr("catalog.archived_job_artifact", "Archived job artifact: {job}", { job: doc.job_label || lineage.job_title || "linked job" })
          : doc.provenance_summary || (doc.job_label
        ? tr("catalog.linked_job", "Linked to job: {job}", { job: doc.job_label })
        : tr("catalog.standalone", "Standalone document"));
      provenance.innerHTML =
        `<span>${escapeHtml(provenanceText)}</span>` +
        (doc.job_id ? `<span>${escapeHtml(tr("catalog.job_artifact", "Job artifact"))}</span>` : "") +
        (lineage ? `<span>${escapeHtml(packetLabel(lineage))}</span>` : "");

      main.append(titleLine, meta, provenance);

      const actions = document.createElement("div");
      actions.className = "cv-catalog-actions";
      const open = document.createElement("button");
      open.type = "button";
      open.className = "cv-catalog-action cv-catalog-open";
      open.dataset.catalogAction = "open";
      open.textContent = doc.type === "cover_letter"
        ? tr("catalog.open_letter", "Open letter")
        : (doc.id === activeCvId ? tr("catalog.current_cv", "Current CV") : tr("catalog.open_cv", "Open CV"));
      open.setAttribute("aria-label", `${open.textContent}: ${docTitle(doc)}`);
      open.addEventListener("click", (event) => {
        event.stopPropagation();
        openRow();
      });
      actions.appendChild(open);
      if (Array.isArray(doc.actions) && doc.actions.includes("build_pdf")) {
        addCatalogAction(actions, tr("catalog.pdf", "PDF"), (event) => { event.stopPropagation(); dispatchDocumentAction("catalog-build-pdf", { doc }); }, docTitle(doc), "pdf");
      }
      if (Array.isArray(doc.actions) && doc.actions.includes("rename")) {
        addCatalogAction(actions, tr("catalog.rename", "Rename"), (event) => { event.stopPropagation(); dispatchDocumentAction("catalog-rename", { doc }); }, docTitle(doc), "rename");
      }
      if (doc.type === "cv" && Array.isArray(doc.actions) && doc.actions.includes("duplicate")) {
        addCatalogAction(actions, tr("catalog.duplicate", "Duplicate"), (event) => { event.stopPropagation(); dispatchDocumentAction("duplicate-cv", { id: doc.id }); }, docTitle(doc), "duplicate");
      }
      if (doc.type === "cv" && doc.id === activeCvId && Array.isArray(doc.actions) && doc.actions.includes("download_yaml")) {
        addCatalogAction(actions, tr("catalog.yaml", "YAML"), (event) => { event.stopPropagation(); downloadActiveYaml(); }, docTitle(doc), "yaml");
      }
      if (Array.isArray(doc.actions) && doc.actions.includes("delete")) {
        addCatalogAction(actions, tr("catalog.delete", "Delete"), (event) => { event.stopPropagation(); dispatchDocumentAction("catalog-delete", { doc }); }, docTitle(doc), "delete");
      }
      if (doc.job_id) {
        const job = document.createElement("button");
        job.type = "button";
        job.className = "cv-catalog-action";
        job.dataset.catalogAction = "job";
        job.textContent = tr("catalog.job", "Job");
        job.setAttribute("aria-label", `${job.textContent}: ${docTitle(doc)}`);
        job.addEventListener("click", (event) => {
          event.stopPropagation();
          navigateFromCatalog(`/jobs?id=${encodeURIComponent(doc.job_id)}`);
        });
        actions.appendChild(job);
      }
      row.append(main, actions);
      return row;
    }

    function renderDocumentCatalog(documents) {
      lastDocuments = documents;
      list.innerHTML = "";
      updateCatalogCounts(documents);
      const query = catalogQuery.trim().toLowerCase();
      const visible = documents.filter((doc) => {
        const filterMatch = documentFilter === "all" || doc.type === documentFilter;
        const queryMatch = !query || docSearchText(doc).indexOf(query) !== -1;
        return filterMatch && queryMatch;
      });
      const groups = ["cv", "cover_letter", "job_artifact"].map((key) => ({
        key,
        rows: visible.filter((doc) => catalogSection(doc) === key),
      })).filter((group) => group.rows.length);
      groups.forEach((group) => {
        const section = document.createElement("section");
        section.className = "cv-catalog-section";
        section.dataset.catalogSection = group.key;
        const title = document.createElement("div");
        title.className = "cv-catalog-section-title";
        title.innerHTML = `<span>${escapeHtml(sectionLabel(group.key))}</span><span>${group.rows.length}</span>`;
        section.appendChild(title);
        group.rows.forEach((doc) => section.appendChild(makeCatalogRow(doc)));
        list.appendChild(section);
      });
      if (!visible.length) {
        const msg = query
          ? tr("catalog.empty_search", "No documents match this search.")
          : tr("catalog.empty_filter", "No documents match this filter.");
        list.innerHTML = `<div class="form-empty-sub cv-switcher-empty">${escapeHtml(msg)}</div>`;
      }
      if (window.renderIcons) window.renderIcons(list);
      syncDocActions();
    }

    function focusCatalogRow(id) {
      if (!id || !isOpen) return;
      const row = Array.from(pop.querySelectorAll(".cv-catalog-row[data-id]")).find((item) => item.dataset.id === id);
      if (row && typeof row.focus === "function") {
        try { row.focus(); } catch (_) {}
      }
    }

    function syncFilterButtons() {
      pop.querySelectorAll("[data-doc-filter]").forEach((control) => {
        const active = control.dataset.docFilter === documentFilter;
        control.classList.toggle("is-active", active);
        control.setAttribute("aria-pressed", active ? "true" : "false");
      });
    }

    function renderList(cvs) {
      lastCvs = cvs;
      const cvDocs = cvs.map((c) => ({
        ...c,
        type: "cv",
        title: c.name || "Untitled CV",
        open_url: `/app?id=${encodeURIComponent(c.id)}`,
        job_label: c.job_title || null,
        provenance_summary: c.job_id
          ? `Linked to job${c.job_title ? `: ${c.job_title}` : ""}`
          : "Standalone document",
        actions: ["open", "build_pdf", "rename", "duplicate", "delete", "download_yaml"],
      }));
      renderDocumentCatalog(lastDocuments.length ? lastDocuments : cvDocs);
    }

    function makeAction(kind, icon, tip, onClick, extraClass) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "cv-action" + (extraClass ? " " + extraClass : "");
      b.dataset.tooltip = tip;
      b.setAttribute("aria-label", tip);
      b.innerHTML = `<span data-icon="${icon}" data-icon-size="13"></span>`;
      b.addEventListener("click", onClick);
      return b;
    }

    function applyLimit(data) {
      const count = data.count || (data.cvs || []).length;
      const limit = data.limit;
      const targets = [limitNote, catalogLimitNote].filter(Boolean);
      if (!targets.length) return;
      const showLimit = (text, state) => {
        targets.forEach((target) => {
          target.textContent = text;
          target.dataset.state = state;
          target.removeAttribute("hidden");
        });
        if (catalogNewBtn) {
          catalogNewBtn.dataset.limitState = state;
          catalogNewBtn.setAttribute("aria-describedby", catalogLimitNote ? "cv-catalog-limit-note" : "cv-limit-note");
        }
      };
      if (data.at_limit && limit) {
        showLimit(`Free plan limit reached (${count}/${limit}). New CV remains available until plan enforcement.`, "limit");
      } else if (limit) {
        showLimit(`${count} of ${limit} CVs used`, "ok");
      } else {
        targets.forEach((target) => target.setAttribute("hidden", ""));
        if (catalogNewBtn) {
          delete catalogNewBtn.dataset.limitState;
          catalogNewBtn.removeAttribute("aria-describedby");
        }
      }
    }

    function activeCv() {
      return lastCvs.find((c) => c.id === activeCvId) || null;
    }

    function syncDocActions() {
      const hasActive = !!activeCvId;
      [renameActiveBtn, duplicateActiveBtn, deleteActiveBtn, dlBtn].forEach((control) => {
        if (control) control.disabled = !hasActive;
      });
    }

    async function refreshList() {
      try {
        const [cvResult, docResult] = await Promise.allSettled([fetch("/api/cvs"), fetch("/api/documents")]);
        let data = { cvs: [] };
        let cvOk = false;
        if (cvResult.status === "fulfilled") {
          const cvRes = cvResult.value;
          data = await cvRes.json().catch(() => ({ cvs: [] }));
          cvOk = cvRes.ok;
          if (!cvOk) data = { cvs: [] };
        }
        lastCvs = Array.isArray(data.cvs) ? data.cvs : [];
        const cvDocs = lastCvs.map((c) => ({
          ...c,
          type: "cv",
          title: c.name || "Untitled CV",
          open_url: `/app?id=${encodeURIComponent(c.id)}`,
          job_label: c.job_title || null,
          provenance_summary: c.job_id
            ? `Linked to job${c.job_title ? `: ${c.job_title}` : ""}`
            : "Standalone document",
          actions: ["open", "build_pdf", "rename", "duplicate", "delete", "download_yaml"],
        }));
        let docData = { documents: [] };
        let docOk = false;
        if (docResult.status === "fulfilled") {
          const docRes = docResult.value;
          const nextDocData = await docRes.json().catch(() => ({ documents: [] }));
          docOk = docRes.ok;
          if (docOk) docData = nextDocData;
        }
        const docRows = Array.isArray(docData.documents) ? docData.documents : [];
        if (!docRows.length && !cvDocs.length && !cvOk && !docOk) {
          throw cvResult.reason || new Error("Could not load documents");
        }
        lastDocuments = docRows.length ? docRows : cvDocs;
        renderDocumentCatalog(lastDocuments);
        applyLimit(data);
        // Keep the ATS badge's job link fresh — e.g. tailoring a CV stamps a
        // job_id, which appears on the next list refresh.
        setActiveJobFromCvs(data.cvs || []);
        window.dispatchEvent(new CustomEvent("cv:list-changed", { detail: { data, documents: docData } }));
      } catch (_) {
        list.innerHTML = '<div class="form-empty-sub" style="padding:14px">Could not load your CVs.</div>';
      }
    }
    cvPickerRefresh = refreshList;

    async function switchCv(id, name) {
      if (id === activeCvId) { close(); return; }
      // Persist pending edits to the CURRENT doc before switching away.
      const saved = await saveBeforeLeavingEditor();
      if (!saved) {
        showBuildToast("Save failed", "CV was not switched because current edits are still unsaved.", "danger");
        return false;
      }
      setActiveCv(id, name);
      syncActiveCvUrl(id);
      close();
      return loadCv();
    }

    async function dispatchDocumentAction(action, detail = {}) {
      if (action === "open-catalog") { open(detail.opener); return true; }
      if (action === "open-actions") { openDocActions(detail.opener); return true; }
      if (action === "new-cv") return newCv();
      if (action === "rename-active") return renameActiveCv();
      if (action === "duplicate-active") return duplicateCv(activeCvId);
      if (action === "duplicate-cv") return duplicateCv(detail.id);
      if (action === "delete-active") {
        const current = activeCv() || { id: activeCvId, name: activeCvName };
        closeDocActions(false);
        confirmDelete(current);
        return true;
      }
      if (action === "download-yaml") { downloadActiveYaml(); return true; }
      if (action === "import") {
        const importBtn = $("#import-btn");
        if (importBtn) importBtn.click();
        return !!importBtn;
      }
      if (action === "catalog-open") return openDocument(detail.doc);
      if (action === "catalog-build-pdf") return buildDocumentPdf(detail.doc);
      if (action === "catalog-rename") return renameDocumentFromCatalog(detail.doc);
      if (action === "catalog-delete") return deleteDocumentFromCatalog(detail.doc);
      return false;
    }

    async function newCv() {
      try {
        const saved = await saveBeforeLeavingEditor();
        if (!saved) {
          showBuildToast("Save failed", "New CV was not created because current edits are still unsaved.", "danger");
          return;
        }
        const res = await fetch("/api/cvs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: "Untitled CV" }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        setActiveCv(data.id, data.name);
        syncActiveCvUrl(data.id);
        const loaded = await loadCv();
        if (!loaded) {
          showBuildToast("CV created, but not loaded", "The new CV exists, but the editor could not open it. Reopen it from the document catalog.", "danger");
          await refreshList().catch(() => {});
          return false;
        }
        if (applyNewCvDefaultsToActiveModel()) {
          if (previewTimer) {
            clearTimeout(previewTimer);
            previewTimer = null;
          }
          refreshYamlBufferFromModel();
          dirty = true;
          const defaultsSaved = await save({ forceLint: false });
          if (!defaultsSaved) {
            showBuildToast("New CV defaults not saved", "The CV was created, but your browser defaults could not be applied. Edit Appearance manually.", "danger");
          } else {
            schedulePreview(0, { saveBeforeRender: false });
          }
        }
        try {
          await refreshList();
        } catch (err) {
          showBuildToast("CV created, list refresh failed", String(err.message || err), "danger");
          return false;
        }
        showBuildToast("New CV created", "Start from the skeleton or import a resume.", "success");
        return true;
      } catch (err) {
        showBuildToast("CV create failed", String(err.message || err), "danger");
        return false;
      }
    }

    async function duplicateCv(id) {
      try {
        const saved = await saveBeforeLeavingEditor();
        if (!saved) {
          showBuildToast("Save failed", "Duplicate was not created because current edits are still unsaved.", "danger");
          return;
        }
        const res = await fetch(`/api/cv/${encodeURIComponent(id)}/duplicate`, { method: "POST" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        setActiveCv(data.id, data.name);
        syncActiveCvUrl(data.id);
        await loadCv();
        await refreshList();
        showBuildToast("Duplicated", `Editing "${shortText(data.name, 40)}".`, "success");
      } catch (err) {
        showBuildToast("Couldn't duplicate", String(err.message || err), "danger");
      }
    }

    async function renameCv(id, name) {
      try {
        const res = await fetch(`/api/cv/${encodeURIComponent(id)}/rename`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        if (id === activeCvId) setActiveCv(id, data.name);
        await refreshList();
        focusCatalogRow(id);
      } catch (err) {
        showBuildToast("Couldn't rename", String(err.message || err), "danger");
        await refreshList();
      }
    }

    async function deleteCv(id) {
      try {
        const res = await fetch(`/api/cv/${encodeURIComponent(id)}`, { method: "DELETE" });
        const data = await res.json().catch(() => ({}));
        const committed = res.ok || (res.status === 503 && data.document_deleted === true);
        if (!committed) throw new Error(data.error || `HTTP ${res.status}`);
        // If we deleted the active CV, fall back to the most-recent one. If the
        // user deleted the last CV, the server returns an empty state instead
        // of silently recreating the first-run template.
        if (id === activeCvId) {
          setActiveCv(null, null);
          syncActiveCvUrl(null);
          await loadCv();
        }
        await refreshList();
        showBuildToast(
          "Deleted",
          res.ok ? "The CV was removed." : "The CV was removed; PDF cleanup is pending.",
          res.ok ? "success" : "neutral",
        );
      } catch (err) {
        showBuildToast("Couldn't delete", String(err.message || err), "danger");
      }
    }

    function confirmDelete(c) {
      showConfirmDialog(
        tr("catalog.delete_cv", "Delete this CV?"),
        `"${c.name || "Untitled CV"}" will be permanently removed. This can't be undone.`,
        "Delete",
        () => deleteCv(c.id),
      );
    }

    async function renameActiveCv() {
      const current = activeCv();
      showRenameDialog(
        tr("catalog.rename_active_cv", "Rename active CV"),
        (current && current.name) || activeCvName || "Untitled CV",
        async (cleaned) => {
          if (!cleaned || cleaned === activeCvName) return;
          await renameCv(activeCvId, cleaned);
          syncDocActions();
        },
      );
    }

    function open(openerOverride) {
      announceTopbarPopoverOpen("cv-switcher");
      opener = openerOverride || document.activeElement;
      pop.removeAttribute("hidden");
      setCatalogModalInert(true);
      isOpen = true;
      syncFilterButtons();
      btn.classList.add("is-open");
      btn.setAttribute("aria-expanded", "true");
      refreshList().then(() => {
        const first = catalogSearch || pop.querySelector(".cv-catalog-row, [data-doc-filter], button, a");
        if (first) first.focus();
      });
    }
    function close(restore = true) {
      pop.setAttribute("hidden", "");
      setCatalogModalInert(false);
      isOpen = false;
      btn.classList.remove("is-open");
      btn.setAttribute("aria-expanded", "false");
      if (restore && opener && typeof opener.focus === "function") {
        try { opener.focus(); } catch (_) {}
      }
      opener = null;
    }
    function toggle() { isOpen ? close() : open(); }

    function openDocActions(openerOverride) {
      if (!docBtn || !docPop) return;
      announceTopbarPopoverOpen("doc-actions");
      close(false);
      docOpener = openerOverride || document.activeElement;
      docPop.removeAttribute("hidden");
      docOpen = true;
      docBtn.classList.add("is-open");
      docBtn.setAttribute("aria-expanded", "true");
      syncDocActions();
      positionDocActions();
      const first = docPop.querySelector("button:not([disabled]), a");
      if (first) first.focus();
    }
    function closeDocActions(restore = true) {
      if (!docPop || !docOpen) return;
      docPop.setAttribute("hidden", "");
      docOpen = false;
      docBtn.classList.remove("is-open");
      docBtn.setAttribute("aria-expanded", "false");
      if (restore && docOpener && typeof docOpener.focus === "function") {
        try { docOpener.focus(); } catch (_) {}
      }
      docOpener = null;
    }
    function toggleDocActions() { docOpen ? closeDocActions() : openDocActions(); }

    window.OnePagerDocumentActions = {
      run: dispatchDocumentAction,
      refresh: refreshList,
    };

    btn.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
    if (closeBtn) closeBtn.addEventListener("click", () => close(true));
    if (catalogNewBtn) catalogNewBtn.addEventListener("click", (e) => { e.stopPropagation(); dispatchDocumentAction("new-cv"); });
    pop.querySelectorAll("[data-doc-filter]").forEach((control) => {
      control.addEventListener("click", () => {
        documentFilter = control.dataset.docFilter || "all";
        syncFilterButtons();
        renderDocumentCatalog(lastDocuments);
      });
    });
    if (catalogSearch) {
      catalogSearch.addEventListener("input", () => {
        catalogQuery = catalogSearch.value || "";
        renderDocumentCatalog(lastDocuments);
      });
    }
    if (docBtn && docPop) {
      docBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleDocActions(); });
    }
    bindTopbarPopoverClose("cv-switcher", close);
    bindTopbarPopoverClose("doc-actions", closeDocActions);
    window.addEventListener("cv:mobile-utility-action", (e) => {
      if (!e.detail || !["document", "document-catalog"].includes(e.detail.action)) return;
      e.preventDefault();
      dispatchDocumentAction(e.detail.action === "document-catalog" ? "open-catalog" : "open-actions", {
        opener: e.detail.opener || $("#mobile-utility-btn"),
      });
    });
    document.addEventListener("click", (e) => {
      if (isOpen) {
        if (e.target === pop) close(true);
        else if (e.target.closest("#cv-switcher-pop") || e.target.closest("#cv-switcher")) return;
      }
      if (docOpen) {
        if (
          e.target.closest("#doc-actions-popover") ||
          e.target.closest("#doc-actions-btn") ||
          e.target.closest("#mobile-utility-popover") ||
          e.target.closest("#mobile-utility-btn")
        ) return;
        closeDocActions(false);
      }
    });
    document.addEventListener("keydown", (e) => {
      if (isOpen && trapPopoverTab(e, pop)) return;
      if (docOpen && trapPopoverTab(e, docPop)) return;
      if (e.key === "Escape" && isOpen) close(true);
      if (e.key === "Escape" && docOpen) closeDocActions(true);
    });
    window.addEventListener("resize", () => {
      if (docOpen) positionDocActions();
    });
    window.addEventListener("cv:layout-changed", () => {
      if (docOpen) positionDocActions();
    });
    if (newBtn) newBtn.addEventListener("click", (e) => { e.stopPropagation(); dispatchDocumentAction("new-cv"); });
    if (dlBtn) dlBtn.addEventListener("click", (e) => { e.stopPropagation(); dispatchDocumentAction("download-yaml"); });
    if (renameActiveBtn) renameActiveBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      dispatchDocumentAction("rename-active");
    });
    if (duplicateActiveBtn) duplicateActiveBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      dispatchDocumentAction("duplicate-active");
    });
    if (deleteActiveBtn) deleteActiveBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      dispatchDocumentAction("delete-active");
    });
  }

  // Client-side "Download YAML" of the active document. Serialises the
  // current model (form is source of truth) and triggers a file download —
  // no server round-trip, and seeds Stage 10's export surface.
  function downloadActiveYaml() {
    const content = viewMode === "form" ? modelToYamlString() : editor.getValue();
    const safe = String(activeCvName || "cv")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "cv";
    const blob = new Blob([content], { type: "text/yaml;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${safe}.yaml`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  // Small modal confirm dialog (used by delete). Promise-free callback style
  // to match the rest of app.js. Builds DOM on first use.
  function showConfirmDialog(title, body, confirmLabel, onConfirm) {
    let host = document.getElementById("confirm-dialog");
    if (host) host.remove();
    const opener = document.activeElement;
    const overlay = document.createElement("div");
    overlay.id = "confirm-dialog";
    overlay.className = "confirm-overlay";
    const card = document.createElement("div");
    card.className = "confirm-card";
    card.innerHTML =
      `<h2 class="confirm-title">${escapeHtml(title)}</h2>` +
      `<p class="confirm-body">${escapeHtml(body)}</p>`;
    const row = document.createElement("div");
    row.className = "confirm-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "confirm-btn";
    cancel.textContent = "Cancel";
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "confirm-btn is-danger";
    ok.textContent = confirmLabel || "Confirm";
    row.appendChild(cancel);
    row.appendChild(ok);
    card.appendChild(row);
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    function dismiss() {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      if (opener && typeof opener.focus === "function") {
        try { opener.focus(); } catch (_) {}
      }
    }
    function onKey(e) {
      if (trapPopoverTab(e, card)) return;
      if (e.key === "Escape") dismiss();
      else if (e.key === "Enter") { ok.click(); }
    }
    cancel.addEventListener("click", dismiss);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) dismiss(); });
    ok.addEventListener("click", () => { dismiss(); try { onConfirm(); } catch (_) {} });
    document.addEventListener("keydown", onKey);
    setTimeout(() => ok.focus(), 30);
  }

  function showRenameDialog(title, currentName, onConfirm) {
    let host = document.getElementById("rename-dialog");
    if (host) host.remove();
    const opener = document.activeElement;
    const overlay = document.createElement("div");
    overlay.id = "rename-dialog";
    overlay.className = "confirm-overlay";
    const card = document.createElement("div");
    card.className = "confirm-card";
    const labelId = "rename-dialog-title";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", labelId);
    card.innerHTML =
      `<h2 id="${labelId}" class="confirm-title">${escapeHtml(title)}</h2>` +
      `<label class="confirm-body" for="rename-dialog-input">${escapeHtml(tr("catalog.rename_label", "Document name"))}</label>`;
    const input = document.createElement("input");
    input.id = "rename-dialog-input";
    input.className = "cv-rename-input";
    input.type = "text";
    input.maxLength = 120;
    input.value = currentName || "";
    const row = document.createElement("div");
    row.className = "confirm-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "confirm-btn";
    cancel.textContent = tr("common.cancel", "Cancel");
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "confirm-btn";
    ok.textContent = tr("catalog.rename", "Rename");
    row.appendChild(cancel);
    row.appendChild(ok);
    card.appendChild(input);
    card.appendChild(row);
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    function dismiss() {
      overlay.remove();
      overlay.removeEventListener("keydown", onOverlayKey, true);
      if (opener && typeof opener.focus === "function") {
        try { opener.focus(); } catch (_) {}
      }
    }
    function submit() {
      const value = input.value.trim();
      if (!value || value === (currentName || "")) { dismiss(); return; }
      dismiss();
      try { onConfirm(value); } catch (_) {}
    }
    function onOverlayKey(e) {
      if (trapPopoverTab(e, card)) { e.stopPropagation(); return; }
      if (e.key === "Escape") { e.stopPropagation(); dismiss(); }
      else if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); submit(); }
    }
    cancel.addEventListener("click", dismiss);
    ok.addEventListener("click", submit);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) dismiss(); });
    overlay.addEventListener("keydown", onOverlayKey, true);
    setTimeout(() => {
      input.focus();
      input.select();
    }, 30);
  }

  // Lightweight toast (top-centre, auto-dismisses).
  function showBuildToast(title, body, tone = "neutral") {
    let host = document.getElementById("build-toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "build-toast-host";
      document.body.appendChild(host);
    }
    const toast = document.createElement("div");
    toast.className = `build-toast tone-${tone}`;
    toast.innerHTML =
      `<span class="build-toast-title">${escapeHtml(title)}</span>` +
      `<span class="build-toast-body">${escapeHtml(body)}</span>`;
    host.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add("is-visible"));
    setTimeout(() => {
      toast.classList.remove("is-visible");
      setTimeout(() => toast.remove(), 240);
    }, tone === "danger" ? 6000 : 3500);
  }

  // ──────────────────────────────────────────────────────────────────────
  //  THEME PICKER — popover over the topbar palette button. Pulls themes
  //  from /api/themes (JSON files in themes/) and applies one with a
  //  click. The applied theme writes into cvModel.{accent,font} (+ density
  //  via the existing dropdown) and triggers a save + refresh.
  //
  //  # 🤖 ADD-A-THEME-HERE
  //  Drop a JSON into themes/ at the repo root: { name, accent, font, density }.
  // ──────────────────────────────────────────────────────────────────────
  function setupThemePicker() {
    const btn = $("#theme-btn");
    const pop = $("#theme-popover");
    const list = $("#theme-list");
    if (!btn || !pop || !list) return;

    let loaded = false;
    let isOpen = false;
    let opener = null;
    let loadedThemes = [];

    function position() {
      const br = btn.getBoundingClientRect();
      const btnVisible = br.width > 0 && br.height > 0 && getComputedStyle(btn).display !== "none";
      const utilityBtn = $("#mobile-utility-btn");
      positionAnchoredPopover(btnVisible ? btn : (utilityBtn || btn), pop, { align: "right" });
    }

    function renderList(themes) {
      list.innerHTML = "";
      const labelFor = (value) => {
        const raw = String(value || "").trim();
        if (!raw) return "";
        return raw.charAt(0).toUpperCase() + raw.slice(1);
      };
      themes.forEach((t) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "theme-row";
        const isActive = (cvModel.accent || "").toLowerCase() === (t.accent || "").toLowerCase()
          && (cvModel.font || "serif") === (t.font || "serif")
          && (cvModel.density || "normal") === (t.density || "normal")
          && getPaper() === (t.paper || "white");
        if (isActive) row.classList.add("is-active");
        row.setAttribute("aria-pressed", isActive ? "true" : "false");
        row.innerHTML =
          `<span class="theme-swatch" style="background:${escapeHtml(t.accent)}"></span>` +
          `<span class="theme-meta">` +
            `<span class="theme-name">${escapeHtml(t.name)}</span>` +
            `<span class="theme-sub">${escapeHtml(labelFor(t.font || "serif"))} · ${escapeHtml(labelFor(t.density || "normal"))} · ${escapeHtml(labelFor(t.paper || "white"))}</span>` +
          `</span>` +
          `<span class="theme-check" data-icon="check" data-icon-size="13"></span>`;
        row.addEventListener("click", () => applyTheme(t));
        list.appendChild(row);
      });
      if (window.renderIcons) window.renderIcons(list);
    }

    async function ensureLoaded() {
      if (loaded) return;
      try {
        const res = await fetch("/api/themes");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const themes = data.themes || [];
        if (!themes.length) throw new Error("No theme presets returned");
        loadedThemes = themes;
        renderList(themes);
        loaded = true;
      } catch (_) {
        list.innerHTML =
          '<div class="form-empty-sub" style="padding:14px">' +
          'Could not load theme presets. Check auth/session or themes/ files.' +
          '</div>';
      }
    }

    function applyTheme(theme) {
      // All three (accent / font / density) are now canonical YAML
      // fields on cvModel — patch them and the rest of the UI reads
      // back via refreshAppearanceUi() and the form rebuild.
      applyAppearancePatch({
        accent: theme.accent,
        font: theme.font,
        density: theme.density,
        paper: theme.paper,
      });
      if (formApi) formApi.rebuild();
      refreshAtsAdvisories();
      renderList(loadedThemes);
      close();
    }

    function open() {
      announceTopbarPopoverOpen("theme-picker");
      opener = document.activeElement;
      pop.removeAttribute("hidden");
      position();
      isOpen = true;
      btn.classList.add("is-active");
      btn.setAttribute("aria-expanded", "true");
      ensureLoaded().then(() => {
        if (!isOpen || pop.hidden) return;
        if (loadedThemes.length) renderList(loadedThemes);
        const first = pop.querySelector(".theme-row, #font-select, #density-select, button");
        if (first) first.focus();
      });
    }
    function close(restore = true) {
      pop.setAttribute("hidden", "");
      isOpen = false;
      btn.classList.remove("is-active");
      btn.setAttribute("aria-expanded", "false");
      if (restore && opener && typeof opener.focus === "function") {
        try { opener.focus(); } catch (_) {}
      }
      opener = null;
    }
    function toggle() { isOpen ? close() : open(); }

    btn.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
    bindTopbarPopoverClose("theme-picker", close);
    window.addEventListener("cv:appearance-changed", () => {
      if (loadedThemes.length) renderList(loadedThemes);
    });
    window.addEventListener("cv:mobile-utility-action", (e) => {
      if (!e.detail || e.detail.action !== "appearance") return;
      e.preventDefault();
      open();
    });
    document.addEventListener("click", (e) => {
      if (!isOpen) return;
      if (
        e.target.closest("#theme-popover") ||
        e.target.closest("#theme-btn") ||
        e.target.closest("#mobile-utility-popover") ||
        e.target.closest("#mobile-utility-btn")
      ) return;
      close(false);
    });
    document.addEventListener("keydown", (e) => {
      if (isOpen && trapPopoverTab(e, pop)) return;
      if (e.key === "Escape" && isOpen) close(true);
    });
    window.addEventListener("resize", () => { if (isOpen) position(); });

  }

  function positionAnchoredPopover(trigger, pop, opts = {}) {
    const margin = opts.margin || 8;
    const gap = opts.gap || 6;
    const r = trigger.getBoundingClientRect();
    const card = pop.firstElementChild || pop;
    const cr = card.getBoundingClientRect();
    const width = Math.min(cr.width || 320, window.innerWidth - margin * 2);
    const height = Math.min(cr.height || 240, window.innerHeight - margin * 2);
    const align = opts.align || "right";
    const rawLeft = align === "left" ? r.left : r.right - width;
    const maxLeft = Math.max(margin, window.innerWidth - width - margin);
    const protectedMin = opts.minLeft == null ? margin : Math.min(Math.max(margin, opts.minLeft), maxLeft);
    const left = clamp(Math.round(rawLeft), protectedMin, maxLeft);
    const top = clamp(Math.round(r.bottom + gap), margin, Math.max(margin, window.innerHeight - height - margin));
    pop.style.top = `${top}px`;
    pop.style.left = `${left}px`;
    pop.style.right = "auto";
  }

  function announceTopbarPopoverOpen(id) {
    window.dispatchEvent(new CustomEvent("cv:topbar-popover-open", { detail: { id } }));
  }

  function bindTopbarPopoverClose(id, close) {
    window.addEventListener("cv:topbar-popover-open", (e) => {
      if (!e.detail || e.detail.id !== id) close(false);
    });
  }

  function trapPopoverTab(e, pop) {
    if (e.key !== "Tab" || pop.hasAttribute("hidden")) return false;
    const nodes = Array.from(pop.querySelectorAll(
      "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
    )).filter((node) => {
      const rect = node.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && getComputedStyle(node).visibility !== "hidden";
    });
    if (!nodes.length) {
      e.preventDefault();
      return true;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
      return true;
    }
    if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
      return true;
    }
    return false;
  }

  function setupWorkflowMenu() {
    const btn = $("#workflow-btn");
    const pop = $("#workflow-popover");
    if (!btn || !pop) return;
    let isOpen = false;
    let opener = null;
    function position() {
      const btnRect = btn.getBoundingClientRect();
      const btnVisible = btnRect.width > 0 && btnRect.height > 0 && getComputedStyle(btn).display !== "none";
      const utilityBtn = $("#mobile-utility-btn");
      positionAnchoredPopover(btnVisible ? btn : (utilityBtn || btn), pop, { align: "right" });
    }
    function open() {
      announceTopbarPopoverOpen("workflow");
      opener = document.activeElement;
      pop.removeAttribute("hidden");
      position();
      isOpen = true;
      btn.classList.add("is-active");
      btn.setAttribute("aria-expanded", "true");
      if (window.renderIcons) window.renderIcons(pop);
      const first = pop.querySelector(".workflow-row, button, a");
      if (first) first.focus();
    }
    async function navigateWithDirtyGuard(link) {
      const saved = await saveBeforeLeavingEditor();
      if (!saved) {
        showBuildToast("Save failed", "Navigation was stopped so current edits are not lost.", "danger");
        return;
      }
      const href = link.getAttribute("href") === "/tailor"
        ? (($("#tailor-btn") && $("#tailor-btn").getAttribute("href")) || link.href)
        : link.href;
      window.location.href = href;
    }
    function close(restore = true) {
      pop.setAttribute("hidden", "");
      isOpen = false;
      btn.classList.remove("is-active");
      btn.setAttribute("aria-expanded", "false");
      if (restore && opener && typeof opener.focus === "function") {
        try { opener.focus(); } catch (_) {}
      }
      opener = null;
    }
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      isOpen ? close() : open();
    });
    bindTopbarPopoverClose("workflow", close);
    window.addEventListener("cv:mobile-utility-action", (e) => {
      if (!e.detail || e.detail.action !== "applications") return;
      e.preventDefault();
      open();
    });
    document.addEventListener("click", (e) => {
      if (!isOpen) return;
      if (
        e.target.closest("#workflow-popover") ||
        e.target.closest("#workflow-btn") ||
        e.target.closest("#mobile-utility-popover") ||
        e.target.closest("#mobile-utility-btn")
      ) return;
      close(false);
    });
    document.addEventListener("keydown", (e) => {
      if (isOpen && trapPopoverTab(e, pop)) return;
      if (e.key === "Escape" && isOpen) close(true);
    });
    pop.addEventListener("click", (e) => {
      const link = e.target.closest("a.workflow-row");
      if (!link) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button === 1) return;
      if (!dirty) return;
      e.preventDefault();
      close(false);
      navigateWithDirtyGuard(link);
    });
    window.addEventListener("resize", () => { if (isOpen) position(); });
  }

  function setupAtsPopover() {
    const btn = $("#ats-badge");
    const pop = $("#ats-popover");
    if (!btn || !pop) return;
    let isOpen = false;
    let opener = null;
    function position() {
      const btnRect = btn.getBoundingClientRect();
      const btnVisible = btnRect.width > 0 && btnRect.height > 0 && getComputedStyle(btn).display !== "none";
      const utilityBtn = $("#mobile-utility-btn");
      positionAnchoredPopover(btnVisible ? btn : (utilityBtn || btn), pop, { align: "right" });
    }
    function open(sectionKey) {
      announceTopbarPopoverOpen("ats");
      opener = document.activeElement;
      renderAtsDetail();
      pop.removeAttribute("hidden");
      position();
      isOpen = true;
      btn.classList.add("is-active");
      btn.setAttribute("aria-expanded", "true");
      if (sectionKey) {
        const target = pop.querySelector(`[data-section-key="${CSS.escape(sectionKey)}"]`);
        if (target) target.scrollIntoView({ block: "nearest" });
      }
      if (window.renderIcons) window.renderIcons(pop);
      const first = pop.querySelector("a, button, select, [tabindex]:not([tabindex='-1'])");
      if (first) first.focus();
    }
    function close(restore = true) {
      pop.setAttribute("hidden", "");
      isOpen = false;
      btn.classList.remove("is-active");
      btn.setAttribute("aria-expanded", "false");
      if (restore && opener && typeof opener.focus === "function") {
        try { opener.focus(); } catch (_) {}
      }
      opener = null;
    }
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      isOpen ? close() : open();
    });
    bindTopbarPopoverClose("ats", close);
    window.addEventListener("cv:mobile-utility-action", (e) => {
      if (!e.detail || e.detail.action !== "ats-detail") return;
      e.preventDefault();
      open();
    });
    window.addEventListener("cv:ats-advisory-open", (e) => {
      open(e.detail && e.detail.sectionKey);
    });
    document.addEventListener("click", (e) => {
      if (!isOpen) return;
      if (
        e.target.closest("#ats-popover") ||
        e.target.closest("#ats-badge") ||
        e.target.closest(".form-section-ats") ||
        e.target.closest("#mobile-utility-popover") ||
        e.target.closest("#mobile-utility-btn")
      ) return;
      close(false);
    });
    document.addEventListener("keydown", (e) => {
      if (isOpen && trapPopoverTab(e, pop)) return;
      if (e.key === "Escape" && isOpen) close(true);
    });
    window.addEventListener("resize", () => { if (isOpen) position(); });
  }

  // ──────────────────────────────────────────────────────────────────────
  //  IMPORT MODAL — three paths:
  //   * AI extract  → POST /api/extract (selected provider, w/ fallback)
  //   * rendercv    → POST /api/import   (deterministic conversion)
  //   * Plain text  → POST /api/import   (heuristic regex parse)
  // ──────────────────────────────────────────────────────────────────────
  let importTab = "ai";
  let importFile = null;
  let aiAvailable = null;  // null = unknown, true/false after status probe

  const HINTS = {
    ai:
      "Drop a <strong>PDF, DOCX, or text file</strong> below — or paste text directly. " +
      "Your selected AI provider maps it into the right schema. " +
      "<span class='hint-linkedin'>From LinkedIn? Open your profile → " +
      "<strong>More → Save to PDF</strong>, then drop the file here.</span> " +
      "<span id='ai-availability-text'></span>",
    rendercv:
      'Paste a <code>rendercv</code> YAML file (the format from <a href="https://rendercv.com" target="_blank" rel="noopener">rendercv.com</a>). ' +
      "We'll convert it to this tool's <code>cv.yaml</code> shape — including brand-icon hints for known networks.",
    text:
      "Paste any plain-text resume. We'll do a best-effort regex parse (section headers like " +
      "<code>EXPERIENCE</code>, date ranges, job lines). Review carefully.",
  };

  // Last fetched AI status (key connection + free-generation balance).
  let aiKeyStatus = {
    connected: false,
    last4: null,
    managed_credits_remaining: 0,
    provider: "anthropic",
    provider_label: "AI",
    model: "",
  };

  function providerLabel(id) {
    const labels = {
      anthropic: "Claude",
      gemini: "Gemini",
      ollama: "Ollama",
      openrouter: "OpenRouter",
      deepseek: "DeepSeek",
      groq: "Groq",
      mistral: "Mistral",
      openai_compatible: "OpenAI-compatible",
    };
    return labels[id] || "AI";
  }

  async function probeAiStatus() {
    try {
      const res = await fetch("/api/extract/status");
      const data = await res.json();
      aiAvailable = !!data.ai_available;
      aiKeyStatus = {
        connected: !!data.connected,
        last4: data.last4 || null,
        managed_credits_remaining: data.managed_credits_remaining || 0,
        provider: data.provider || "anthropic",
        provider_label: data.provider_label || providerLabel(data.provider || "anthropic"),
        model: data.model || "",
      };
    } catch (_) {
      aiAvailable = false;
    }
    updateAiBadge();
  }

  function updateAiBadge() {
    const badge = $("#ai-tab-badge");
    const txt = $("#ai-availability-text");
    if (!badge) return;
    if (aiKeyStatus.connected) {
      badge.removeAttribute("hidden");
      badge.textContent = aiKeyStatus.provider_label;
      badge.title = aiKeyStatus.model ? aiKeyStatus.provider_label + " · " + aiKeyStatus.model : aiKeyStatus.provider_label;
      badge.classList.remove("is-fallback");
      if (txt) {
        txt.innerHTML =
          "<strong>" + escapeHtml(aiKeyStatus.provider_label) + " connected.</strong>" +
          (aiKeyStatus.model ? " <span class=\"muted\">" + escapeHtml(aiKeyStatus.model) + "</span>" : "");
      }
    } else if ((aiKeyStatus.managed_credits_remaining || 0) > 0) {
      const n = aiKeyStatus.managed_credits_remaining;
      badge.removeAttribute("hidden");
      badge.textContent = "AI";
      badge.classList.remove("is-fallback");
      if (txt) txt.innerHTML = "<strong>" + n + " free generation" + (n === 1 ? "" : "s") + " left.</strong>";
    } else {
      badge.removeAttribute("hidden");
      badge.textContent = "fallback";
      badge.classList.add("is-fallback");
      if (txt) {
        txt.innerHTML =
          "No free generations left — connect a provider in Account for AI extraction. " +
          "Without one we fall back to a heuristic parse.";
      }
    }
  }

  // ── BYO-key panel: status line, Account handoff ─────────────────────────
  async function refreshKeyPanel() {
    try {
      const res = await fetch("/api/key");
      if (!res.ok) return;
      const data = await res.json();
      const provider = aiKeyStatus.provider || "anthropic";
      const providerState = (data.providers && data.providers[provider]) || {};
      aiKeyStatus = {
        connected: !!providerState.connected,
        last4: providerState.last4 || null,
        managed_credits_remaining: data.managed_credits_remaining || 0,
        provider,
        provider_label: providerLabel(provider),
        model: aiKeyStatus.model || "",
      };
    } catch (_) {
      /* leave last-known status */
    }
    renderKeyPanel();
    updateAiBadge();
  }

  function renderKeyPanel() {
    const statusEl = $("#ai-key-status");
    const entry = $("#ai-key-entry");
    const removeBtn = $("#ai-key-remove");
    if (!statusEl) return;
    if (aiKeyStatus.connected) {
      statusEl.innerHTML =
        '<span class="ai-key-dot ok"></span>' + escapeHtml(aiKeyStatus.provider_label) + " connected" +
        (aiKeyStatus.last4 ? ' <span class="muted">(key ···' + escapeHtml(aiKeyStatus.last4) + ")</span>" : "");
      if (entry) entry.setAttribute("hidden", "");
      if (removeBtn) removeBtn.removeAttribute("hidden");
    } else {
      const n = aiKeyStatus.managed_credits_remaining || 0;
      statusEl.innerHTML =
        '<span class="ai-key-dot"></span>' +
        (n > 0
          ? n + " free generation" + (n === 1 ? "" : "s") + " left, then connect a provider in Account"
          : "Connect a provider in Account to keep using AI");
      if (entry) entry.setAttribute("hidden", "");
      if (removeBtn) removeBtn.setAttribute("hidden", "");
    }
  }

  function setupKeyPanel() {
    const input = $("#ai-key-input");
    const saveBtn = $("#ai-key-save");
    const removeBtn = $("#ai-key-remove");
    const msg = $("#ai-key-msg");
    if (!saveBtn || !input) return;
    input.setAttribute("hidden", "");
    saveBtn.textContent = "Open Account";

    function showMsg(text, kind) {
      if (!msg) return;
      msg.textContent = text;
      msg.className = "ai-key-msg" + (kind ? " " + kind : "");
      msg.removeAttribute("hidden");
    }
    function hideMsg() {
      if (msg) msg.setAttribute("hidden", "");
    }

    saveBtn.addEventListener("click", async () => {
      hideMsg();
      window.location.href = "/account";
    });

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") saveBtn.click();
    });

    if (removeBtn) {
      removeBtn.addEventListener("click", async () => {
        hideMsg();
        window.location.href = "/account";
      });
    }
  }

  function setupImport() {
    const btn = $("#import-btn");
    const modal = $("#import-modal");
    const close = $("#import-close");
    const cancel = $("#import-cancel");
    const apply = $("#import-apply");
    const input = $("#import-input");
    const error = $("#import-error");
    const status = $("#import-status");
    const hint = $("#import-hint");
    const dropzone = $("#import-dropzone");
    const fileInput = $("#import-file-input");
    const fileBtn = $("#import-file-btn");
    const fileName = $("#import-file-name");
    if (!btn) return;
    let modalOpener = null;

    function open() {
      announceTopbarPopoverOpen("import-modal");
      modalOpener = document.activeElement;
      modal.classList.remove("closing");
      modal.removeAttribute("hidden");
      input.value = "";
      importFile = null;
      fileName.setAttribute("hidden", "");
      fileName.textContent = "";
      hideError();
      status.textContent = "";
      // Wait for the entrance animation to finish, then focus. This avoids
      // the focus ring jittering against a translating card.
      const card = modal.querySelector(".modal-card");
      if (card) {
        const onEnd = () => {
          card.removeEventListener("animationend", onEnd);
          input.focus();
        };
        card.addEventListener("animationend", onEnd, { once: true });
      } else {
        setTimeout(() => input.focus(), 220);
      }
    }
    function closeModal() {
      // If we're already closing or hidden, no-op.
      if (modal.hasAttribute("hidden") || modal.classList.contains("closing")) return;
      modal.classList.add("closing");
      const card = modal.querySelector(".modal-card");
      const finalize = () => {
        modal.setAttribute("hidden", "");
        modal.classList.remove("closing");
        if (modalOpener && typeof modalOpener.focus === "function") {
          try { modalOpener.focus(); } catch (_) {}
        }
        modalOpener = null;
      };
      if (card) {
        card.addEventListener("animationend", finalize, { once: true });
        // Safety net — if animationend doesn't fire (reduced motion edge),
        // hide on next frame anyway.
        setTimeout(finalize, 220);
      } else {
        finalize();
      }
    }
    function hideError() {
      error.setAttribute("hidden", "");
      error.textContent = "";
    }
    function showError(msg) {
      error.removeAttribute("hidden");
      error.textContent = msg;
    }
    function setTab(tab) {
      importTab = tab;
      $$(".modal-tab").forEach((t) => {
        t.classList.toggle("is-active", t.dataset.tab === tab);
      });
      hint.innerHTML = HINTS[tab] || "";
      const showDrop = tab === "ai";
      dropzone[showDrop ? "removeAttribute" : "setAttribute"]("hidden", "");
      // The BYO-key panel lives on the AI tab only.
      const keyPanel = $("#ai-key-panel");
      if (keyPanel) keyPanel[showDrop ? "removeAttribute" : "setAttribute"]("hidden", "");
      apply.textContent =
        tab === "ai" ? (importFile ? "Extract from file" : "Extract with AI") : "Convert & replace";
      if (tab === "ai") {
        updateAiBadge();
        refreshKeyPanel();
      }
    }

    function setFile(file) {
      importFile = file;
      if (file) {
        fileName.removeAttribute("hidden");
        fileName.textContent = `${file.name} · ${formatBytes(file.size)}`;
        apply.textContent = "Extract from file";
      } else {
        fileName.setAttribute("hidden", "");
        fileName.textContent = "";
      }
    }

    async function ensureImportTarget() {
      if (activeCvId) return;
      const res = await fetch("/api/cvs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "Imported CV" }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      setActiveCv(data.id, data.name);
      syncActiveCvUrl(data.id);
    }

    async function persistImportedYaml(yaml) {
      await ensureImportTarget();
      const res = await fetch("/api/cv", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: yaml, id: activeCvId }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      if (data.id && data.id !== activeCvId) {
        setActiveCv(data.id, activeCvName);
        syncActiveCvUrl(data.id);
      }
      lastSavedContent = yaml;
      lastSavedAt = new Date();
      activeCvRevision = data.updated_at || activeCvRevision;
      dirty = false;
      setStatus("saved", `Saved · ${formatBytes(data.bytes || yaml.length)}`);
      refreshLastSaved();
      scheduleAtsBadge(0);
      window.dispatchEvent(new CustomEvent("cv:list-changed"));
    }

    btn.addEventListener("click", open);
    close.addEventListener("click", closeModal);
    cancel.addEventListener("click", closeModal);
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeModal();
    });
    document.addEventListener("keydown", (e) => {
      if (modal.hasAttribute("hidden")) return;
      const card = modal.querySelector(".modal-card");
      if (card && trapPopoverTab(e, card)) return;
      if (e.key === "Escape") closeModal();
    });
    $$(".modal-tab").forEach((t) => {
      t.addEventListener("click", () => setTab(t.dataset.tab));
    });

    // File input + drag-drop
    fileBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) setFile(f);
    });
    ;["dragenter", "dragover"].forEach((ev) =>
      dropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.add("is-dragging");
      }),
    );
    ;["dragleave", "drop"].forEach((ev) =>
      dropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.remove("is-dragging");
      }),
    );
    dropzone.addEventListener("drop", (e) => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) setFile(f);
    });

    apply.addEventListener("click", async () => {
      hideError();
      apply.disabled = true;
      try {
        let url, body, headers;
        if (importTab === "ai") {
          url = "/api/extract";
          if (importFile) {
            const fd = new FormData();
            fd.append("file", importFile);
            body = fd;
            headers = undefined;
            status.textContent = "Reading file & extracting…";
          } else {
            const text = input.value.trim();
            if (!text) {
              showError("Drop a file or paste text.");
              apply.disabled = false;
              return;
            }
            url = "/api/extract";
            body = JSON.stringify({ content: text });
            headers = { "Content-Type": "application/json" };
            status.textContent = "Asking AI provider…";
          }
        } else {
          const text = input.value.trim();
          if (!text) {
            showError("Paste something first.");
            apply.disabled = false;
            return;
          }
          url = "/api/import";
          body = JSON.stringify({ format: importTab, content: text });
          headers = { "Content-Type": "application/json" };
          status.textContent = "Converting…";
        }

        const res = await fetch(url, { method: "POST", headers, body });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        if (!data.yaml) throw new Error("Empty response from server");

        suppressEditorOnEdit = true;
        editor.setValue(data.yaml);
        editor.setCursor({ line: 0, ch: 0 });
        suppressEditorOnEdit = false;
        try {
          const parsed = window.CvForm.yamlToModel(data.yaml);
          if (parsed && typeof parsed === "object") replaceModel(parsed);
        } catch (_) { /* ignore */ }
        if (formApi) formApi.rebuild();
        refreshAtsAdvisories();
        renderOutline();
        await persistImportedYaml(data.yaml);

        const sourceLabel =
          data.source === "heuristic" ? "fallback" : providerLabel(data.source || aiKeyStatus.provider);
        status.textContent = `Imported · ${sourceLabel} · ${data.bytes || data.yaml.length} chars`;
        setTimeout(() => {
          closeModal();
          // Import is an explicit user action — always render the new
          // content, regardless of the auto-render toggle.
          schedulePreview(0);
        }, 600);
      } catch (err) {
        console.error(err);
        showError(`Import failed: ${err.message || err}`);
        status.textContent = "";
      } finally {
        apply.disabled = false;
      }
    });

    setupKeyPanel();
    setTab("ai");
    probeAiStatus();
  }

  // ──────────────────────────────────────────────────────────────────────
  //  ZOOM
  // ──────────────────────────────────────────────────────────────────────
  function setupZoomControls() {
    $("#zoom-in").addEventListener("click", () => bumpZoom(+ZOOM_STEP));
    $("#zoom-out").addEventListener("click", () => bumpZoom(-ZOOM_STEP));
    $("#zoom-fit").addEventListener("click", () => setZoom(0));

    // Pinch to zoom: macOS trackpad pinch gestures (and Cmd/Ctrl + scroll)
    // arrive as wheel events with ctrlKey true. Convert deltaY into a smooth
    // zoom change and stop the browser from zooming the whole page.
    const wrap = $("#preview-wrap");
    if (wrap) {
      wrap.addEventListener("wheel", (e) => {
        if (!e.ctrlKey && !e.metaKey) return;
        e.preventDefault();
        const base = previewZoom > 0 ? previewZoom : effectiveZoom();
        const delta = -e.deltaY * 0.01;
        setZoom(clampZoom(base + delta));
      }, { passive: false });
    }

    applyZoom();
  }

  function clampZoom(z) {
    if (!isFinite(z) || z <= 0) return 1;
    return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));
  }

  function clampFitZoom(z) {
    if (!isFinite(z) || z <= 0) return 1;
    return Math.max(FIT_ZOOM_MIN, Math.min(ZOOM_MAX, z));
  }

  function previewPad(wrap) {
    if (!wrap) return PREVIEW_PAD_PX;
    return clamp(Math.round(Math.min(PREVIEW_PAD_PX, wrap.clientWidth * 0.04)), 10, PREVIEW_PAD_PX);
  }

  function effectiveZoom() {
    if (previewZoom > 0) return clampZoom(previewZoom);
    const wrap = $("#preview-wrap");
    if (!wrap) return 1;
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    if (!w || !h) return 1;
    const pad = previewPad(wrap);
    const targetW = cvPageWidth || A4_W_PX;
    const targetH = cvPageHeight || A4_H_PX;
    const fitW = (w - pad * 2) / targetW;
    const fitH = (h - pad * 2) / targetH;
    return clampFitZoom(Math.min(fitW, fitH));
  }

  function applyZoom() {
    const iframe = $("#preview");
    const stage = $("#preview-stage");
    const wrap = $("#preview-wrap");
    const frame = $("#preview-paper-frame");
    const label = $("#zoom-text");
    const fitBtn = $("#zoom-fit");
    if (!iframe || !stage || !wrap) return;
    const z = effectiveZoom();
    const wrapW = wrap.clientWidth || 600;
    const wrapH = wrap.clientHeight || 800;
    const pad = previewPad(wrap);
    const pageW = cvPageWidth || A4_W_PX;
    const pageH = cvPageHeight || A4_H_PX;
    const pagedCount = previewMode === "paged" ? Math.max(1, lastPagedCount || 1) : 1;
    const naturalPagedH = Math.max(cvContentHeight || pageH, pageH * pagedCount);
    const contentW = previewMode === "paged"
      ? pageW
      : Math.max(cvContentWidth || A4_W_PX, pageW);
    const contentH = previewMode === "paged"
      ? naturalPagedH
      : Math.max(cvContentHeight || A4_H_PX, pageH);
    const iframeW = Math.ceil(contentW);
    const iframeH = Math.ceil(contentH);
    iframe.style.width = `${iframeW}px`;
    iframe.style.height = `${iframeH}px`;
    iframe.style.transform = `scale(${z})`;
    const visualW = Math.ceil(iframeW * z);
    const visualH = Math.ceil(iframeH * z);
    const rawStageW = visualW + pad * 2;
    const rawStageH = visualH + pad * 2;
    const stageW = Math.abs(rawStageW - wrapW) <= 2 ? wrapW : Math.max(wrapW, rawStageW);
    const stageH = Math.abs(rawStageH - wrapH) <= 2 ? wrapH : Math.max(wrapH, rawStageH);
    stage.style.width = `${stageW}px`;
    stage.style.height = `${stageH}px`;
    const left = Math.round((stageW - visualW) / 2);
    const top = pad;
    iframe.style.left = `${left}px`;
    iframe.style.top = `${top}px`;
    if (frame) {
      frame.style.left = `${left}px`;
      frame.style.top = `${top}px`;
      frame.style.width = `${visualW}px`;
      frame.style.height = `${visualH}px`;
    }
    wrap.classList.toggle("is-zoomed-in", z > 1);
    // Unified scroll: the iframe becomes pointer-transparent (CSS) whenever the
    // scaled page overflows the wrap in EITHER axis, so all wheel/scroll lands
    // on the single wrap layer (bug #3) — not only when zoomed past fit.
    const overflows = stageH - wrapH > 1 || stageW - wrapW > 1;
    wrap.classList.toggle("is-scrollable", overflows);
    if (!overflows) {
      // Content fully fits: no scroll range, so park the wrap at the top-left
      // (a later zoom-in starts there). When it DOES overflow we leave the
      // scroll position alone so reflows/hover-sync don't yank the view.
      wrap.scrollTop = 0;
      wrap.scrollLeft = 0;
    }
    if (label) {
      const pct = `${Math.round(z * 100)}%`;
      label.textContent = previewZoom === 0
        ? (wrapW < 360 ? "Fit" : `Fit · ${pct}`)
        : pct;
    }
    if (fitBtn) fitBtn.classList.toggle("is-fit", previewZoom === 0);
  }

  function bumpZoom(delta) {
    const base = previewZoom > 0 ? previewZoom : effectiveZoom();
    setZoom(clampZoom(base + delta));
  }

  function zoomAnchor(wrap) {
    if (!wrap) return null;
    return {
      x: (wrap.scrollLeft + wrap.clientWidth / 2) / Math.max(1, wrap.scrollWidth),
      y: (wrap.scrollTop + wrap.clientHeight / 2) / Math.max(1, wrap.scrollHeight),
    };
  }

  function restoreZoomAnchor(wrap, anchor) {
    if (!wrap || !anchor) return;
    wrap.scrollLeft = clamp(
      Math.round(anchor.x * wrap.scrollWidth - wrap.clientWidth / 2),
      0,
      Math.max(0, wrap.scrollWidth - wrap.clientWidth),
    );
    wrap.scrollTop = clamp(
      Math.round(anchor.y * wrap.scrollHeight - wrap.clientHeight / 2),
      0,
      Math.max(0, wrap.scrollHeight - wrap.clientHeight),
    );
  }

  function setZoom(z) {
    const wrap = $("#preview-wrap");
    const anchor = zoomAnchor(wrap);
    previewZoom = z > 0 ? clampZoom(z) : 0;
    if (previewZoom === 0) localStorage.removeItem(ZOOM_KEY);
    else localStorage.setItem(ZOOM_KEY, String(previewZoom));
    applyZoom();
    if (previewZoom === 0) {
      if (wrap) {
        wrap.scrollTop = 0;
        wrap.scrollLeft = 0;
      }
      return;
    }
    restoreZoomAnchor(wrap, anchor);
  }

  // ──────────────────────────────────────────────────────────────────────
  //  OUTLINE — built from YAML top-level keys
  // ──────────────────────────────────────────────────────────────────────
  // Slug → human label. Anything not in this map shows up as a generic key.
  const SECTION_LABELS = {
    name: "Name",
    contact: "Contact",
    accent: "Accent",
    photo: "Photo",
    skills: "Skills",
    experience: "Experience",
    education: "Education",
    projects: "Projects",
    leadership: "Leadership",
    others: "Other",
  };

  // Subset that maps to a section in the rendered HTML (id="sec-{slug}").
  const RENDER_SECTIONS = new Set([
    "header", "experience", "education", "skills",
    "projects", "leadership", "others",
  ]);

  function renderOutline() {
    if (!editor) return;
    const list = $("#outline-list");
    const text = editor.getValue();
    const lines = text.split("\n");
    const lineByKey = new Map();
    lines.forEach((line, idx) => {
      const m = /^([a-zA-Z_][a-zA-Z0-9_]*):/.exec(line);
      if (m && !lineByKey.has(m[1])) lineByKey.set(m[1], idx);
    });

    // Outline = Header + registered sections + any custom YAML keys the
    // user added directly (so a freshly typed ``awards:`` shows up in
    // the sidebar immediately on save).
    const FALLBACK_ORDER = [
      ["name", "header", "Header"],
      ["experience", "experience", "Experience"],
      ["education", "education", "Education"],
      ["skills", "skills", "Skills"],
      ["projects", "projects", "Projects"],
      ["leadership", "leadership", "Leadership"],
      ["others", "others", "Other"],
    ];
    let sectionOrder;
    if (serverSchema && Array.isArray(serverSchema.sections)) {
      sectionOrder = [["name", "header", "Header"]];
      serverSchema.sections.forEach((s) => {
        sectionOrder.push([s.key, s.key, s.label]);
      });
    } else {
      sectionOrder = FALLBACK_ORDER;
    }
    // Append custom sections (any list-typed top-level YAML key not in
    // the schema or reserved). Uses the form module's helper for label
    // humanisation so naming stays consistent.
    if (window.CvForm && typeof window.CvForm.detectCustomSections === "function") {
      const registeredKeys = new Set(sectionOrder.map((row) => row[0]));
      const custom = window.CvForm.detectCustomSections(cvModel, registeredKeys);
      custom.forEach((c) => sectionOrder.push([c.key, c.key, c.label]));
    }
    // Show all sections regardless of population — the progress dot
    // indicates which are filled. Outline becomes a fill-the-dots habit
    // signal rather than a contents listing.
    const items = [];
    for (const [yamlKey, renderSlug, schemaLabel] of sectionOrder) {
      const fallbackLabel = schemaLabel || SECTION_LABELS[yamlKey] ||
        yamlKey.charAt(0).toUpperCase() + yamlKey.slice(1);
      const label = localizedSectionLabel(renderSlug || yamlKey, fallbackLabel);
      items.push({
        key: yamlKey,
        label,
        line: lineByKey.get(yamlKey) || 0,
        renderSlug,
      });
    }
    outlineItems = items;
    list.innerHTML = "";
    items.forEach((it, i) => {
      if (!it.renderSlug) return;
      const a = document.createElement("a");
      a.className = "outline-item";
      a.href = "#";
      a.dataset.idx = String(i);
      a.dataset.slug = it.renderSlug;
      // Progress dot — populated if the section has content, "empty" if
      // present but blank (only the header is always populated).
      const isHeader = it.renderSlug === "header";
      const sectionVal = cvModel[it.key];
      let populated;
      if (isHeader) {
        populated = "true";
      } else if (Array.isArray(sectionVal) && sectionVal.length) {
        populated = "true";
      } else if (sectionVal) {
        populated = "true";
      } else {
        populated = "empty";
      }
      a.dataset.populated = populated;
      a.innerHTML = `<span class="ol-num">${String(i + 1).padStart(2, "0")}</span><span class="ol-label">${escapeHtml(it.label)}</span>`;
      a.addEventListener("click", (e) => {
        e.preventDefault();
        suppressOutlineHover();
        if (viewMode === "form" && formApi) {
          if (formMode === "focus") {
            activateFormSection(it.renderSlug, { scrollPreview: false });
          } else {
            activeFormSection = it.renderSlug;
            saveActiveFormSection(activeFormSection);
            formApi.scrollTo(it.renderSlug, { behavior: "smooth", block: "start" });
          }
        } else {
          editor.setCursor({ line: it.line, ch: 0 });
          editor.scrollIntoView({ line: it.line, ch: 0 }, 80);
          editor.focus();
        }
        pendingScrollTarget = it.renderSlug;
        if (scrollPreviewToSection(it.renderSlug)) pendingScrollTarget = null;
      });
      a.addEventListener("pointerenter", (e) => {
        scheduleOutlineHover(it.renderSlug, e.pointerType);
      });
      a.addEventListener("pointerleave", cancelOutlineHover);
      a.addEventListener("pointercancel", cancelOutlineHover);
      list.appendChild(a);
    });
    updateOutlineActive();
  }

  function updateOutlineActive() {
    if (!outlineItems.length || !editor) return;
    if (viewMode === "form" && formMode === "focus") {
      $$(".outline-item").forEach((el) => {
        el.classList.toggle("active", el.dataset.slug === activeFormSection);
      });
      return;
    }
    const cur = editor.getCursor().line;
    let activeIdx = -1;
    outlineItems.forEach((it, i) => {
      if (it.line <= cur) activeIdx = i;
    });
    $$(".outline-item").forEach((el) => {
      el.classList.toggle("active", parseInt(el.dataset.idx, 10) === activeIdx);
    });
  }

  function setupOutlineHoverGuards() {
    const list = $("#outline-list");
    if (!list) return;
    list.addEventListener("wheel", () => suppressOutlineHover(), { passive: true });
    list.addEventListener("scroll", () => suppressOutlineHover(), { passive: true });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ──────────────────────────────────────────────────────────────────────
  //  PANE RESIZER
  // ──────────────────────────────────────────────────────────────────────
  function setupResizer() {
    const sep = $("#resizer");
    const editorPane = $("#editor-pane");
    const previewPane = $("#preview-pane");
    const panesEl = $("#panes");
    if (!sep || !editorPane || !previewPane || !panesEl) return;

    let dragging = false;
    let pendingClientX = null;
    let resizeRaf = 0;
    let lastStackState = false;

    function cssPxVar(name, fallback) {
      const raw = getComputedStyle(document.documentElement).getPropertyValue(name);
      const parsed = parseFloat(raw || "");
      return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
    }

    function constraints() {
      const panes = panesEl.getBoundingClientRect();
      const total = panes.width || 0;
      const sepW = sep.getBoundingClientRect().width || cssPxVar("--resizer-w", 5);
      const editorMin = viewMode === "yaml"
        ? cssPxVar("--editor-min-yaml", 360)
        : cssPxVar("--editor-min-form", 512);
      const previewMin = cssPxVar("--preview-min", 340);
      const stackMin = viewMode === "yaml"
        ? editorMin + previewMin + sepW
        : cssPxVar("--pane-stack-min", editorMin + previewMin + sepW);
      const stack = total <= Math.max(stackMin, editorMin + previewMin + sepW);
      const minPx = editorMin;
      const maxPx = Math.max(editorMin, total - previewMin - sepW);
      return {
        total,
        sepW,
        editorMin,
        previewMin,
        stack,
        minPx,
        maxPx,
        minPct: total ? (minPx / total) * 100 : 0,
        maxPct: total ? (maxPx / total) * 100 : 100,
      };
    }

    function storedSplitPx(total) {
      const raw = parseFloat(localStorage.getItem(SPLIT_KEY) || "");
      if (!Number.isFinite(raw) || raw < 5 || raw > 95) {
        localStorage.removeItem(SPLIT_KEY);
        return total / 2;
      }
      return (total * raw) / 100;
    }

    function finalizeResizeWork() {
      editor && editor.refresh();
      applyZoom();
    }

    function applySplitPx(px, opts = {}) {
      const c = constraints();
      panesEl.classList.toggle("is-stacked", c.stack);
      lastStackState = c.stack;
      if (!c.total || c.stack) {
        editorPane.style.flexBasis = "";
        sep.tabIndex = -1;
        sep.setAttribute("aria-hidden", "true");
        sep.setAttribute("aria-disabled", "true");
        if (!opts.deferHeavy) finalizeResizeWork();
        return;
      }
      const clampedPx = clamp(px, c.minPx, c.maxPx);
      const pct = (clampedPx / c.total) * 100;
      editorPane.style.flexBasis = `${Math.round(clampedPx)}px`;
      sep.tabIndex = 0;
      sep.removeAttribute("aria-hidden");
      sep.setAttribute("aria-disabled", "false");
      const ariaMin = Math.ceil(c.minPct);
      const ariaMax = Math.floor(c.maxPct);
      sep.setAttribute("aria-valuemin", String(ariaMin));
      sep.setAttribute("aria-valuemax", String(ariaMax));
      sep.setAttribute("aria-valuenow", String(clamp(Math.round(pct), ariaMin, ariaMax)));
      if (opts.persist) localStorage.setItem(SPLIT_KEY, String(pct));
      if (!opts.deferHeavy) finalizeResizeWork();
    }

    function reclampSplit(opts = {}) {
      const c = constraints();
      const current = editorPane.getBoundingClientRect().width;
      const wasStacked = panesEl.classList.contains("is-stacked") || lastStackState;
      const basis = (opts.preferStored || wasStacked || current <= 0)
        ? storedSplitPx(c.total)
        : current;
      applySplitPx(basis, opts);
    }

    function applyDrag(clientX) {
      const panes = panesEl.getBoundingClientRect();
      applySplitPx(clientX - panes.left, { persist: false, deferHeavy: true });
    }

    sep.addEventListener("pointerdown", (e) => {
      if (constraints().stack) return;
      dragging = true;
      sep.classList.add("dragging");
      document.body.classList.add("dragging-resize");
      sep.setPointerCapture && sep.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    sep.addEventListener("pointerup", (e) => {
      if (!dragging) return;
      dragging = false;
      sep.classList.remove("dragging");
      document.body.classList.remove("dragging-resize");
      sep.releasePointerCapture && sep.releasePointerCapture(e.pointerId);
      if (pendingClientX != null) {
        applyDrag(pendingClientX);
        pendingClientX = null;
      }
      reclampSplit({ persist: true });
    });
    sep.addEventListener("pointercancel", () => {
      dragging = false;
      pendingClientX = null;
      sep.classList.remove("dragging");
      document.body.classList.remove("dragging-resize");
      reclampSplit({ persist: true });
    });
    sep.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      e.preventDefault();
      pendingClientX = e.clientX;
      if (resizeRaf) return;
      resizeRaf = requestAnimationFrame(() => {
        resizeRaf = 0;
        if (pendingClientX != null) applyDrag(pendingClientX);
      });
    });

    sep.addEventListener("dblclick", () => {
      applySplitPx(constraints().total / 2, { persist: true });
    });
    sep.addEventListener("keydown", (e) => {
      const c = constraints();
      if (c.stack) return;
      const current = editorPane.getBoundingClientRect().width;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        applySplitPx(current - 24, { persist: true });
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        applySplitPx(current + 24, { persist: true });
      } else if (e.key === "Home") {
        e.preventDefault();
        applySplitPx(c.minPx, { persist: true });
      } else if (e.key === "End") {
        e.preventDefault();
        applySplitPx(c.maxPx, { persist: true });
      }
    });

    window.addEventListener("cv:layout-changed", () => {
      requestAnimationFrame(() => reclampSplit({ persist: false, preferStored: true }));
    });
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(() => {
        if (dragging) return;
        if (resizeRaf) cancelAnimationFrame(resizeRaf);
        resizeRaf = requestAnimationFrame(() => {
          resizeRaf = 0;
          reclampSplit({ persist: false, preferStored: true });
        });
      });
      ro.observe(panesEl);
    }
    requestAnimationFrame(() => reclampSplit({ persist: false, preferStored: true }));
  }

  // ──────────────────────────────────────────────────────────────────────
  //  SIDEBAR LIBRARY
  // ──────────────────────────────────────────────────────────────────────
  function setupSidebarLibrary() {
    const list = $("#sidebar-library-list");
    if (!list) return;
    let refreshTimer = 0;

    function getJson(url) {
      return fetch(url).then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      });
    }

    function sidebarPacketState(lineage) {
      if (!lineage) return "";
      if (lineage.archived_at || lineage.status === "archived") return "archived";
      if (lineage.status === "stale" || (Array.isArray(lineage.stale_reasons) && lineage.stale_reasons.length)) return "stale";
      return lineage.status || "";
    }

    function sidebarPacketLabel(lineage) {
      const state = sidebarPacketState(lineage);
      if (!state) return "";
      const version = lineage && lineage.packet_version ? ` v${lineage.packet_version}` : "";
      if (state === "stale") return `Stale packet${version}`;
      if (state === "archived") return `Archived packet${version}`;
      if (state === "packet_created") return `Packet${version}`;
      if (state === "review_applied") return `Incomplete packet${version}`;
      return `${state.replace(/_/g, " ")}${version}`;
    }

    function sidebarPacketUsable(lineage) {
      return !!lineage &&
        lineage.document_role === "tailored_cv" &&
        sidebarPacketState(lineage) === "packet_created" &&
        lineage.cover_letter_review_status === "confirmed" &&
        !!lineage.cover_letter_review_confirmed_at &&
        !!lineage.cover_letter_review_id &&
        !(Array.isArray(lineage.stale_reasons) && lineage.stale_reasons.length);
    }

    function row(kind, id, href, icon, name, meta, count, lineage) {
      const a = document.createElement("a");
      a.className = "sidebar-library-row";
      a.href = href;
      a.title = name || "Untitled";
      a.setAttribute("aria-label", `${name || "Untitled"}${meta ? `, ${meta}` : ""}`);
      a.dataset.libraryRow = kind;
      a.dataset.libraryKind = kind;
      if (id) a.dataset.id = id;
      if (kind === "cv" && id === activeCvId) a.classList.add("is-active");
      if (lineage && lineage.packet_id) {
        a.dataset.packetId = lineage.packet_id;
        a.dataset.packetState = sidebarPacketState(lineage);
        a.dataset.packetVersion = lineage.packet_version || "";
        a.dataset.packetUsable = sidebarPacketUsable(lineage) ? "true" : "false";
      }
      const packetText = sidebarPacketLabel(lineage);
      a.innerHTML =
        `<span data-icon="${icon}" data-icon-size="13"></span>` +
        '<span class="sidebar-library-main">' +
          `<span class="sidebar-library-name">${escapeHtml(name || "Untitled")}</span>` +
          `<span class="sidebar-library-meta">${escapeHtml([meta, packetText].filter(Boolean).join(" · "))}</span>` +
        "</span>" +
        (count == null ? "" : `<span class="sidebar-library-count">${escapeHtml(count)}</span>`);
      return a;
    }

    function catalogHandoff(hiddenCount) {
      const a = row(
        "catalog",
        "",
        "#",
        "folder-open",
        tr("sidebar.open_catalog", "Open document catalog"),
        tr("sidebar.hidden_count", "{count} more", { count: hiddenCount }),
        null,
      );
      a.addEventListener("click", (event) => {
        event.preventDefault();
        window.OnePagerDocumentActions?.run("open-catalog", { opener: a });
      });
      return a;
    }

    function group(title, rows) {
      if (!rows.length) return null;
      const wrap = document.createElement("div");
      wrap.className = "sidebar-library-group";
      wrap.innerHTML = `<span class="sidebar-library-heading">${escapeHtml(title)}</span>`;
      rows.forEach((item) => wrap.appendChild(item));
      return wrap;
    }

    function render(data) {
      const docs = Array.isArray(data.documents) ? data.documents : [];
      const jobs = Array.isArray(data.jobs) ? data.jobs : [];
      const frag = document.createDocumentFragment();

      const recency = (doc) => doc.last_used_at || doc.updated_at || doc.created_at || "";
      const byRecency = (a, b) => String(recency(b)).localeCompare(String(recency(a)));

      const cvDocs = docs.filter((d) => d.type === "cv").sort(byRecency);
      const coverDocs = docs.filter((d) => d.type === "cover_letter").sort(byRecency);

      const cvs = cvDocs
        .sort(byRecency)
        .slice(0, 3)
        .map((doc) => row(
          "cv",
          doc.id,
          `/app?id=${encodeURIComponent(doc.id)}`,
          "file-text",
          doc.name || "Untitled CV",
          doc.job_id && doc.job_title ? doc.job_title : tr("lib.storage.saved", "Saved in Documents"),
          null,
          doc.packet_lineage || null,
        ));

      if (cvDocs.length > 3) cvs.push(catalogHandoff(cvDocs.length - 3));

      const letters = coverDocs
        .sort(byRecency)
        .slice(0, 3)
        .map((doc) => row(
          "cover_letter",
          doc.id,
          `/cover-letters?id=${encodeURIComponent(doc.id)}`,
          "mail",
          doc.name || "Cover letter",
          doc.job_id && doc.job_title
            ? (doc.job_title || doc.role || tr("lib.storage.linked_job", "Linked to this job"))
            : tr("lib.storage.standalone_letter", "Standalone letter"),
          null,
          doc.packet_lineage || null,
        ));
      if (coverDocs.length > 3) letters.push(catalogHandoff(coverDocs.length - 3));

      const counts = new Map();
      docs.forEach((doc) => {
        if (!doc.job_id) return;
        const cur = counts.get(doc.job_id) || { total: 0, cv: 0, cover: 0, title: doc.job_title || "" };
        cur.total += 1;
        if (doc.type === "cv") cur.cv += 1;
        if (doc.type === "cover_letter") cur.cover += 1;
        if (!cur.title && doc.job_title) cur.title = doc.job_title;
        counts.set(doc.job_id, cur);
      });
      const jobIndex = new Map(jobs.map((job) => [job.id, job]));
      const jobRows = Array.from(counts.entries())
        .filter(([id]) => jobIndex.has(id))
        .map(([id, count]) => {
          const job = jobIndex.get(id) || {};
          const title = job.title || count.title || tr("lib.storage.linked_job", "Linked to this job");
          const company = job.company || "";
          const meta = company || `${count.cv} CV / ${count.cover} letter`;
          return row("job", id, `/jobs?id=${encodeURIComponent(id)}`, "briefcase", title, meta, count.total);
        })
        .sort((a, b) => a.textContent.localeCompare(b.textContent));

      [
        group(tr("lib.filter.cv", "CVs"), cvs),
        group(tr("lib.filter.cover", "Cover letters"), letters),
        group(tr("sidebar.job_outputs", "Job outputs"), jobRows),
      ]
        .filter(Boolean)
        .forEach((section) => frag.appendChild(section));

      list.innerHTML = "";
      if (!frag.childNodes.length) {
        list.innerHTML = '<p class="sidebar-library-empty">' +
          escapeHtml(tr("sidebar.empty", "No saved documents.")) + "</p>";
      } else {
        list.appendChild(frag);
      }
      if (window.renderIcons) window.renderIcons(list);
    }

    function refresh() {
      Promise.all([
        getJson("/api/documents"),
        getJson("/api/jobs"),
      ])
        .then(([docData, jobData]) => {
          render({
            documents: docData.documents || [],
            jobs: jobData.jobs || [],
          });
        })
        .catch(() => {
          list.innerHTML = '<p class="sidebar-library-error">' +
            escapeHtml(tr("sidebar.error", "Could not load library.")) + "</p>";
        });
    }

    function scheduleRefresh() {
      if (refreshTimer) clearTimeout(refreshTimer);
      refreshTimer = setTimeout(refresh, 80);
    }

    list.addEventListener("click", async (e) => {
      const link = e.target.closest("a.sidebar-library-row");
      if (!link) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button === 1) return;
      if (!dirty) return;
      e.preventDefault();
      const saved = await save({ forceLint: false });
      if (saved) window.location.href = link.href;
    });

    window.addEventListener("cv:list-changed", scheduleRefresh);
    window.addEventListener("cv:active-changed", scheduleRefresh);
    window.addEventListener("cv:i18n-changed", scheduleRefresh);
    window.addEventListener("focus", scheduleRefresh);
    refresh();
  }

  // ──────────────────────────────────────────────────────────────────────
  //  SIDEBAR TOGGLE
  // ──────────────────────────────────────────────────────────────────────
  function setupSidebarToggle() {
    const sidebar = $("#sidebar");
    const btn = $("#sidebar-toggle");
    if (!sidebar || !btn) return;
    function canRenderSidebar() {
      return getComputedStyle(sidebar).display !== "none";
    }
    function syncAvailability() {
      const canRender = canRenderSidebar();
      btn.hidden = !canRender;
      btn.disabled = !canRender;
      btn.setAttribute("aria-disabled", canRender ? "false" : "true");
      btn.setAttribute("aria-expanded", canRender && !sidebar.classList.contains("collapsed") ? "true" : "false");
    }
    function apply(collapsed) {
      if (!canRenderSidebar()) {
        syncAvailability();
        return;
      }
      sidebar.classList.toggle("collapsed", collapsed);
      btn.classList.toggle("is-collapsed", collapsed);
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
      if (collapsed) localStorage.setItem(SIDEBAR_KEY, "collapsed");
      else localStorage.removeItem(SIDEBAR_KEY);
      requestAnimationFrame(() => {
        window.dispatchEvent(new CustomEvent("cv:layout-changed", { detail: { reason: "sidebar-toggle" } }));
        editor && editor.refresh();
        applyZoom();
      });
    }
    if (localStorage.getItem(SIDEBAR_KEY) === "collapsed") apply(true);
    else btn.setAttribute("aria-expanded", "true");
    syncAvailability();
    sidebar.addEventListener("transitionend", () => {
      window.dispatchEvent(new CustomEvent("cv:layout-changed", { detail: { reason: "sidebar-transition" } }));
    });
    btn.addEventListener("click", () => apply(!sidebar.classList.contains("collapsed")));
    window.addEventListener("resize", () => {
      syncAvailability();
      if (canRenderSidebar()) apply(localStorage.getItem(SIDEBAR_KEY) === "collapsed");
    });
  }

  // ──────────────────────────────────────────────────────────────────────
  //  TOOLTIPS
  // ──────────────────────────────────────────────────────────────────────
  function setupTooltips() {
    const tip = $("#tooltip");
    let showTimer = null;
    let target = null;

    function showFor(el) {
      const txt = el.dataset.tooltip;
      const sc = el.dataset.shortcut;
      if (!txt) return;
      tip.innerHTML = `<span>${escapeHtml(txt)}</span>` +
        (sc ? `<span class="tip-shortcut">${escapeHtml(sc)}</span>` : "");
      const r = el.getBoundingClientRect();
      const margin = 8;
      const width = tip.offsetWidth || 160;
      const height = tip.offsetHeight || 30;
      const left = clamp(
        Math.round(r.left + r.width / 2 - width / 2),
        margin,
        Math.max(margin, window.innerWidth - width - margin),
      );
      let top = Math.round(r.bottom + 6);
      if (top + height > window.innerHeight - margin) top = Math.round(r.top - height - 6);
      top = clamp(top, margin, Math.max(margin, window.innerHeight - height - margin));
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
      tip.classList.add("is-visible");
    }

    document.addEventListener("mouseover", (e) => {
      const el = e.target.closest("[data-tooltip]");
      if (!el || el === target) return;
      target = el;
      if (showTimer) clearTimeout(showTimer);
      showTimer = setTimeout(() => { showFor(el); }, 600);
    });
    document.addEventListener("mouseout", (e) => {
      if (!target) return;
      if (e.relatedTarget && target.contains(e.relatedTarget)) return;
      if (showTimer) { clearTimeout(showTimer); showTimer = null; }
      tip.classList.remove("is-visible");
      target = null;
    });
    document.addEventListener("focusin", (e) => {
      const el = e.target.closest("[data-tooltip]");
      if (!el) return;
      if (el.closest(".popover, .mobile-utility-popover")) return;
      if (showTimer) { clearTimeout(showTimer); showTimer = null; }
      target = el;
      showFor(el);
    });
    document.addEventListener("focusout", (e) => {
      if (!target) return;
      if (e.relatedTarget && target.contains(e.relatedTarget)) return;
      if (showTimer) { clearTimeout(showTimer); showTimer = null; }
      tip.classList.remove("is-visible");
      target = null;
    });
    document.addEventListener("scroll", () => tip.classList.remove("is-visible"), true);
  }

  // ──────────────────────────────────────────────────────────────────────
  //  RESPONSIVE LAYOUT (verbatim port — critical for cold-load)
  // ──────────────────────────────────────────────────────────────────────
  let layoutResizeTimer = null;
  function setupResponsiveLayout() {
    const refresh = () => { try { editor && editor.refresh(); } catch (_) {} };
    let previewResizeRaf = 0;
    const schedulePreviewResize = () => {
      if (previewResizeRaf) return;
      previewResizeRaf = requestAnimationFrame(() => {
        previewResizeRaf = 0;
        applyZoom();
      });
    };
    const onResize = () => {
      if (layoutResizeTimer) clearTimeout(layoutResizeTimer);
      layoutResizeTimer = setTimeout(() => {
        layoutResizeTimer = null;
        refresh();
        applyZoom();
      }, 80);
    };
    window.addEventListener("resize", onResize);
    window.addEventListener("resize", () => requestAnimationFrame(refresh));

    const host = $("#editor-host");
    if (host && typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(() => { refresh(); applyZoom(); });
      ro.observe(host);
    }
    const previewWrap = $("#preview-wrap");
    if (previewWrap && typeof ResizeObserver !== "undefined") {
      const previewRo = new ResizeObserver(schedulePreviewResize);
      previewRo.observe(previewWrap);
    }
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(refresh).catch(() => {});
    }
    requestAnimationFrame(() => {
      refresh();
      requestAnimationFrame(refresh);
    });
  }

  // ──────────────────────────────────────────────────────────────────────
  //  VIEW TOGGLE (Form ↔ YAML)
  // ──────────────────────────────────────────────────────────────────────
  let serverSchema = null;  // { sections: [...] } from /api/schema

  function setupViews() {
    formApi = window.CvForm.mount($("#form-host"), {
      model: cvModel,
      schema: serverSchema && serverSchema.sections,
      onChange: onFormChange,
    });
    applyView();
    $$(".view-tab").forEach((tab) => {
      tab.addEventListener("click", () => switchView(tab.dataset.view));
    });
    // Fetch the schema in the background — once loaded, push it into the
    // form so the section list reflects the server registry. This lets
    // someone edit ``engine/render/sections.py`` and see the new section
    // appear after a browser refresh.
    fetch("/api/schema")
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (!data || !Array.isArray(data.sections)) return;
        serverSchema = data;
        formApi && formApi.setSchema(data.sections);
        refreshAtsAdvisories();
        renderOutline();
      })
      .catch(() => { /* fall back to FALLBACK_SCHEMA in form.js */ });
  }

  function applyView() {
    const formHost = $("#form-host");
    const yamlHost = $("#yaml-host");
    // Both panes stay in the DOM. The .is-active class drives the
    // opacity crossfade (CSS transition var(--t-fast)). CodeMirror's
    // measure cache is bogus while opacity:0 isn't a problem; what
    // breaks it is the prior `hidden` attribute. Now that both are
    // mounted, just refresh on the next rAF.
    formHost.classList.toggle("is-active", viewMode === "form");
    yamlHost.classList.toggle("is-active", viewMode === "yaml");
    document.body.dataset.view = viewMode;
    if (viewMode === "yaml") {
      requestAnimationFrame(() => editor && editor.refresh());
    } else {
      // Re-deriving the per-section completeness hints after a (re)build of the
      // form (e.g. a YAML→Form switch rebuilds every section's DOM).
      refreshSectionHints();
      refreshAtsAdvisories();
    }
    $$(".view-tab").forEach((t) => {
      const active = t.dataset.view === viewMode;
      t.classList.toggle("is-active", active);
      t.setAttribute("aria-selected", active ? "true" : "false");
    });
    applyFormMode();
    updateViewHint();
    // Lint toggle is only meaningful in YAML view.
    const lintBtn = $("#lint-toggle");
    if (lintBtn) {
      if (viewMode === "yaml") lintBtn.removeAttribute("hidden");
      else lintBtn.setAttribute("hidden", "");
    }
    updateCollapseAllButton();
  }

  function switchView(target) {
    if (target === viewMode) return;
    cancelOutlineHover();
    if (target === "yaml") {
      // Form is canonical: serialise it into the buffer first.
      refreshYamlBufferFromModel();
      viewMode = "yaml";
    } else {
      // YAML is canonical: parse the buffer into the model. If invalid,
      // alert the user and stay on YAML view.
      try {
        const parsed = window.CvForm.yamlToModel(editor.getValue());
        if (parsed && typeof parsed === "object") replaceModel(parsed);
      } catch (err) {
        setStatus("error", "Fix YAML first");
        console.error(err);
        return;
      }
      viewMode = "form";
      if (formApi) formApi.rebuild();
      refreshAtsAdvisories();
    }
    localStorage.setItem(VIEW_KEY, viewMode);
    applyView();
    window.dispatchEvent(new CustomEvent("cv:layout-changed", { detail: { reason: "view-switch" } }));
  }

  // ──────────────────────────────────────────────────────────────────────
  //  GLOBAL SHORTCUTS
  // ──────────────────────────────────────────────────────────────────────
  function setupShortcuts() {
    document.addEventListener("keydown", (e) => {
      const meta = e.metaKey || e.ctrlKey;
      if (meta && e.key === "s") {
        e.preventDefault();
        userInitiatedSave();
      } else if (meta && e.key === "\\") {
        e.preventDefault();
        $("#sidebar-toggle").click();
      } else if (meta && (e.key === "=" || e.key === "+")) {
        e.preventDefault();
        bumpZoom(+ZOOM_STEP);
      } else if (meta && e.key === "-") {
        e.preventDefault();
        bumpZoom(-ZOOM_STEP);
      } else if (meta && e.key === "0") {
        e.preventDefault();
        setZoom(0);
      } else if (meta && e.shiftKey && (e.key === "z" || e.key === "Z")) {
        // Don't intercept inside CodeMirror — it has its own undo/redo
        // for the YAML buffer.
        if (e.target.closest && e.target.closest(".CodeMirror")) return;
        e.preventDefault();
        redo();
      } else if (meta && (e.key === "z" || e.key === "Z")) {
        if (e.target.closest && e.target.closest(".CodeMirror")) return;
        e.preventDefault();
        undo();
      }
    });
    $("#save-btn").addEventListener("click", userInitiatedSave);
  }

  // ──────────────────────────────────────────────────────────────────────
  //  COMMAND PALETTE (⌘K) — editor-specific actions
  // ──────────────────────────────────────────────────────────────────────
  // The palette itself (open/close/nav/theme) lives in command-palette.js and
  // is shared by every page. Editor document commands run through the typed
  // document dispatcher so hidden button clicks do not create a second path.
  function setupCommandPalette() {
    const P = window.OnePagerPalette;
    if (!P) return;
    P.register({
      id: "act-import",
      group: "actions",
      labelKey: "palette.act.import",
      label: "Import a resume",
      icon: "upload",
      run: () => window.OnePagerDocumentActions?.run("import"),
      available: () => !!$("#import-btn"),
    });
    P.register({
      id: "act-new-cv",
      group: "actions",
      labelKey: "palette.act.new_cv",
      label: "New CV",
      icon: "plus-circle",
      run: () => window.OnePagerDocumentActions?.run("new-cv"),
      available: () => !!$("#cv-new-btn"),
    });
    // Template + CV Appearance were unreachable from ⌘K: the only built-in
    // matching "appearance" navigated to App theme settings on /account
    // (plan 107, F9/F16).
    P.register({
      id: "act-template",
      group: "actions",
      labelKey: "palette.act.template",
      label: "Choose template",
      aliases: ["template", "templates", "layout", "vorlage", "gallery"],
      icon: "layout",
      run: () => $("#template-btn")?.click(),
      available: () => !!$("#template-btn"),
    });
    P.register({
      id: "act-appearance",
      group: "actions",
      labelKey: "palette.act.appearance",
      label: "CV Appearance",
      aliases: ["appearance", "accent", "font", "density", "paper", "farbe", "design"],
      icon: "palette",
      run: () => $("#theme-btn")?.click(),
      available: () => !!$("#theme-btn"),
    });
    P.register({
      id: "act-collapse-all",
      group: "actions",
      labelKey: "palette.act.collapse_all",
      label: "Collapse all sections",
      icon: "chevrons-up-down",
      run: () => setAllSectionsCollapsed(true),
      available: () => viewMode === "form" && collapsibleSections().length > 0 && !allSectionsCollapsed() && formMode === "overview",
    });
    P.register({
      id: "act-expand-all",
      group: "actions",
      labelKey: "palette.act.expand_all",
      label: "Expand all sections",
      icon: "chevrons-up-down",
      run: () => setAllSectionsCollapsed(false),
      available: () => viewMode === "form" && collapsedSummary().collapsed > 0 && formMode === "overview",
    });
  }

  function consumeLaunchAction() {
    const url = new URL(window.location.href);
    const action = url.searchParams.get("action");
    if (!action) return "";
    url.searchParams.delete("action");
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    }
    return action;
  }

  async function runLaunchAction() {
    const action = consumeLaunchAction();
    if (action === "new-cv") {
      await window.OnePagerDocumentActions?.run("new-cv");
    }
  }

  function flushPendingSaveOnHide() {
    // A mobile browser can discard a BACKGROUNDED tab without ever firing a
    // reliable beforeunload, so the pending 1.5 s autosave never runs and the
    // edit is lost. When the page is hidden / unloading, flush the save
    // immediately instead of waiting for the idle timer. Best-effort: on a
    // normal app-switch the in-flight fetch completes; a hard kill may still
    // drop it, but this closes the common "typed, switched apps, lost it" case.
    if (!dirty) return;
    if (autosaveTimer) { clearTimeout(autosaveTimer); autosaveTimer = null; }
    // auto:true — a background flush must NOT overwrite during an unresolved
    // 409 conflict (that requires an explicit Save).
    save({ auto: true });
  }

  function setupBeforeUnload() {
    window.addEventListener("beforeunload", (e) => {
      pageUnloading = true;
      if (!dirty) return;
      flushPendingSaveOnHide();
      e.preventDefault();
      e.returnValue = "";
    });
    // pagehide fires for reload/bfcache too — set the flag as early as possible
    // so an in-flight fetch aborted by the demo-login reload stays quiet.
    window.addEventListener("pagehide", () => { pageUnloading = true; flushPendingSaveOnHide(); });
    // visibilitychange→hidden is the ONLY reliable "app is backgrounding me"
    // signal on mobile (beforeunload/pagehide are not guaranteed there), so
    // flush the pending save on it too.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") flushPendingSaveOnHide();
    });
  }

  // ──────────────────────────────────────────────────────────────────────
  //  BOOT
  // ──────────────────────────────────────────────────────────────────────
  function init() {
    if (window.renderIcons) window.renderIcons();
    setupTheme();
    setupEditor();
    setupViews();
    setupFormModes();
    setupResizer();
    setupSidebarToggle();
    setupTooltips();
    setupModeToggle();
    setupSettingsButton();
    setupAppearance();
    setupTemplatePicker();
    setupImport();
    setupPagedListener();
    setupPreviewActions();
    setupAutoRenderToggle();
    setupThemePicker();
    setupWorkflowMenu();
    setupAtsPopover();
    loadAtsProfiles();
    setupLintToggle();
    setupBuildButton();
    setupCvSwitcher();
    setupSidebarLibrary();
    setupIssuesPill();
    setupPolish();
    setupCollapsedSections();
    setupHoverSync();
    setupOutlineHoverGuards();
    setupLanguage();
    setupTour();
    setupShortcuts();
    setupCommandPalette();
    setupBeforeUnload();
    setupResponsiveLayout();
    loadCv().then(runLaunchAction).catch((err) => {
      // Suppress the benign "Failed to fetch" that the demo-login reload causes
      // by aborting the first in-flight /api/cv request — the reloaded page
      // loads the CV cleanly. Any other failure is a real load error.
      if (pageUnloading) return;
      setStatus("error", "Load failed");
      console.error(err);
    });
  }

  // Gate the editor boot behind a Supabase session. auth.js (loaded first)
  // shows the login view and resolves OnePagerAuth.ready once authenticated;
  // only then do we init() — so no /api/* call fires without a bearer token.
  function bootWhenAuthed() {
    if (window.OnePagerAuth && window.OnePagerAuth.ready) {
      window.OnePagerAuth.ready.then(init);
    } else {
      // auth.js absent (e.g. a stripped local build) — boot directly.
      init();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootWhenAuthed);
  } else {
    bootWhenAuthed();
  }
})();
