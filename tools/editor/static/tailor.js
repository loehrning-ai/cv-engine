/*
  CV-tailoring page — Stage 6.

  A small, additive companion to the CV editor (app.js). It reuses auth.js
  (which patches window.fetch to attach the Supabase bearer token on every
  /api/* call) and icons.js, so this file only owns the tailoring UX:

    • list the user's CVs into the source-CV picker
    • "Score match" → POST /api/ats-score (free; LLM rubric when a key resolves,
      else the deterministic keyword scorer — see the `source` field)
    • "Review changes" / "Review against this job" → POST /api/cv/<id>/tailor/review
      (signed diff token; no copy is saved yet)
    • "Apply as new copy" → POST /api/cv/<id>/tailor/apply
      (duplicate-then-tailor; the original CV is never changed)
    • render the result: an animated score RING, the four JD-relative category
      sub-scores with evidence (rubric), strengths, gaps, and reframe-only
      suggestions — gaps/suggestions are clickable "fix" chips that copy their
      text, and (on the deterministic path) the matched-keyword chips
    • surface the blockers clearly: 422 unverified_claims (a fabricated
      skill/employer) and 409 overflow (tailored CV spilled past one page)
    • link to the newly created tailored CV in the editor

  Kept deliberately framework-free and in the same plain-DOM style as app.js.
  Animations are reduced-motion-safe; an in-flight request token makes late
  responses no-ops if the user starts another action or leaves the page.
*/
(function () {
  "use strict";

  var $ = function (sel) { return document.querySelector(sel); };
  var REDUCED_MOTION =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");

  // Human labels for the four JD-relative rubric categories (CATEGORY_KEYS in
  // engine/ats/prompts.py). Order matches the prompt's weighting (production
  // first as the dominant signal).
  var CATEGORY_LABELS = {
    production: "Production experience",
    technical_skills: "Technical skills",
    open_source: "Open-source",
    self_projects: "Side projects",
  };
  var CATEGORY_ORDER = ["production", "technical_skills", "open_source", "self_projects"];
  var ATS_PROFILE_KEY = "cv.editor.atsProfile";
  var ATS_PROFILES = {
    keyword_screen: { id: "keyword_screen", label: "Keyword screen" },
    strict_requirements: { id: "strict_requirements", label: "Strict requirements" },
    semantic_fit: { id: "semantic_fit", label: "Semantic fit" },
  };

  function profileLabel(id) {
    if (id === "keyword_screen") return t("tailor.profile.keyword_screen", "Keyword screen");
    if (id === "strict_requirements") return t("tailor.profile.strict_requirements", "Strict requirements");
    if (id === "semantic_fit") return t("tailor.profile.semantic_fit", "Semantic fit");
    return (ATS_PROFILES[id] && ATS_PROFILES[id].label) || id;
  }

  function populateProfileSelect(defaultProfile) {
    var sel = $("#tl-profile");
    if (!sel) return;
    var stored = null;
    try { stored = window.localStorage.getItem(ATS_PROFILE_KEY); } catch (_) {}
    var current = stored || defaultProfile || (ATS_PROFILES[sel.value] ? sel.value : "") || "semantic_fit";
    sel.innerHTML = Object.keys(ATS_PROFILES).map(function (key) {
      return '<option value="' + esc(key) + '">' + esc(profileLabel(key)) + '</option>';
    }).join("");
    sel.value = ATS_PROFILES[current] ? current : (defaultProfile || "semantic_fit");
    if (stored && !ATS_PROFILES[stored]) {
      try { window.localStorage.setItem(ATS_PROFILE_KEY, sel.value); } catch (_) {}
    }
  }

  function loadAtsProfiles() {
    return fetch("/api/ats-profiles")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !Array.isArray(data.profiles)) return;
        var next = {};
        data.profiles.forEach(function (profile) {
          if (profile && profile.id) next[profile.id] = profile;
        });
        if (!Object.keys(next).length) return;
        ATS_PROFILES = next;
        populateProfileSelect(data.default_profile || "semantic_fit");
      })
      .catch(function () {});
  }

  function syncJobIdUrl(jobId) {
    try {
      var url = new URL(window.location.href);
      if (UUID_RE.test(jobId || "")) url.searchParams.set("job_id", jobId);
      else url.searchParams.delete("job_id");
      window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    } catch (_) {}
  }

  // Ring geometry — must match the SVG circle r=52 in tailor.html.
  var RING_RADIUS = 52;
  var RING_CIRC = 2 * Math.PI * RING_RADIUS;

  // A monotonically-increasing token. Every request captures the value at
  // dispatch; when the response lands we ignore it if the token has since moved
  // (the user clicked again, or navigated away). Cheap "ignore late responses".
  var query = new URLSearchParams(window.location.search || "");
  var initialCvId = query.get("cv_id") || query.get("id") || "";
  var initialJobId = query.get("job_id") || "";
  var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  var state = {
    cvId: null,
    jobId: UUID_RE.test(initialJobId) ? initialJobId : null,
    reqToken: 0,
    jobReq: 0,
    animTimer: null,
    tailorInFlight: false,
    tailorAction: null,
    scoreInFlight: false,
    applyReviewInFlight: false,
    review: null,
    autoJdJobId: null,
    autoJdText: "",
    jdDirty: false,
    selectedJob: null,
    jobs: [],
  };

  function setFeedback(kind, html) {
    var el = $("#tl-feedback");
    if (!el) return;
    if (!html) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;
    el.className = "cl-feedback cl-feedback-" + kind;
    el.innerHTML = html;
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function t(key, fallback) {
    if (!window.OnePagerI18n) return fallback;
    var v = window.OnePagerI18n.t(key);
    return v && v !== key ? v : fallback;
  }
  function fmt(key, fallback, vars) {
    var text = t(key, fallback);
    Object.keys(vars || {}).forEach(function (name) {
      text = text.replace("{" + name + "}", String(vars[name]));
    });
    return text;
  }

  function shortHash(value) {
    return String(value || "").slice(0, 10);
  }

  // ── Pane state (empty / loading / error / report) ───────────────────────────
  function showOnly(which, message) {
    var empty = $("#tl-empty"), loading = $("#tl-loading"),
      err = $("#tl-error"), report = $("#tl-report");
    if (empty) empty.hidden = which !== "empty";
    if (loading) loading.hidden = which !== "loading";
    if (err) err.hidden = which !== "error";
    if (report) report.hidden = which !== "report";
    if (which === "loading") {
      var lt = $("#tl-loading-text");
      if (lt) lt.textContent = message || "Working…";
    } else if (which === "error") {
      var et = $("#tl-error-text");
      if (et) et.textContent = message || "Something went wrong. Please try again.";
    }
  }

  function clearReview() {
    state.review = null;
    var review = $("#tl-review");
    var diff = $("#tl-review-diff");
    var summary = $("#tl-review-summary");
    var lineage = $("#tl-review-lineage");
    if (review) review.hidden = true;
    if (diff) diff.textContent = "";
    if (summary) summary.textContent = "";
    if (lineage) lineage.innerHTML = "";
  }

  function renderSelectedJobContext(job) {
    var box = $("#tl-selected-job-context");
    if (!box) return;
    state.selectedJob = job || null;
    if (!job || !job.id) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    box.innerHTML = [
      t("tailor.selected_job", "Selected job"),
      [job.title, job.company].filter(Boolean).join(" @ "),
      job.source_provider || "",
      job.verification_status || "",
    ].filter(Boolean).map(function (item) {
      return "<span>" + esc(item) + "</span>";
    }).join("");
    if (jobNeedsFullJd(job)) {
      box.innerHTML += '<span class="tl-job-warning" role="alert">' +
        esc(t("jobs.selector.full_jd_warning", "This imported job still needs its full job description reviewed before scoring or generation.")) +
        ' <a href="/jobs?id=' + esc(job.id) + '">' + esc(t("jobs.selector.open_in_jobs", "Open in Jobs")) + "</a></span>";
    }
  }

  function attachedJobNeedsFullJd() {
    // Pre-flight for the {job_id} payload path only: a manual edit detaches
    // the job (jdDirty) and falls back to raw-text scoring, which stays open.
    if (!state.jobId || state.jdDirty) return false;
    if (state.autoJdJobId !== state.jobId) return false;
    var job = state.selectedJob && state.selectedJob.id === state.jobId ? state.selectedJob : null;
    return jobNeedsFullJd(job);
  }

  function blockOnFullJdReview() {
    if (!attachedJobNeedsFullJd()) return false;
    setFeedback("error",
      esc(t("jobs.selector.full_jd_warning", "This imported job still needs its full job description reviewed before scoring or generation.")) +
      ' <a href="/jobs?id=' + esc(state.jobId) + '">' + esc(t("jobs.selector.open_in_jobs", "Open in Jobs")) + "</a>");
    return true;
  }

  // ── Data loads ────────────────────────────────────────────────────────────
  function loadCredit() {
    fetch("/api/extract/status")
      .then(function (r) { return r.json(); })
      .then(function (s) {
        var el = $("#tl-credit");
        if (!el) return;
        if (window.OnePagerCredit) window.OnePagerCredit.render(el, s);
      })
      .catch(function () {});
  }

  function loadCvs() {
    return fetch("/api/cvs")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var sel = $("#tl-cv");
        var selectedJobId = null;
        sel.innerHTML = "";
        (data.cvs || []).forEach(function (cv) {
          var opt = document.createElement("option");
          opt.value = cv.id;
          if (cv.job_id && UUID_RE.test(cv.job_id)) opt.dataset.jobId = cv.job_id;
          opt.textContent = cv.name || t("lib.untitled.cv", "Untitled CV");
          sel.appendChild(opt);
        });
        if (sel.options.length) {
          state.cvId = initialCvId && Array.prototype.some.call(sel.options, function (opt) { return opt.value === initialCvId; })
            ? initialCvId
            : sel.options[0].value;
          sel.value = state.cvId;
          selectedJobId = sel.selectedOptions && sel.selectedOptions[0] ? sel.selectedOptions[0].dataset.jobId : null;
          if (!state.jobId && selectedJobId && UUID_RE.test(selectedJobId)) state.jobId = selectedJobId;
          syncJobIdUrl(state.jobId);
          populateJobSelect();
        }
      })
      .catch(function () { setFeedback("error", t("cl.feedback.load_cvs_failed", "Couldn't load your CVs.")); });
  }

  function jobOptionLabel(job) {
    return [job.title || t("jobs.untitled", "Untitled job"), job.company || ""].filter(Boolean).join(" @ ");
  }

  function jobNeedsFullJd(job) {
    return !!(job && job.source_state && job.source_state.requires_full_jd_review === true);
  }

  function renderJobSelectEmptyHint() {
    var hint = $("#tl-job-empty");
    if (!hint) return;
    if (state.jobs.length) {
      hint.hidden = true;
      return;
    }
    hint.innerHTML = esc(t("jobs.selector.empty", "No saved jobs yet —")) +
      ' <a href="/jobs">' + esc(t("jobs.selector.empty_link", "open the Jobs repository")) + "</a>";
    hint.hidden = false;
  }

  function populateJobSelect() {
    var sel = $("#tl-job");
    if (!sel) return;
    var selected = state.jobId || "";
    var knownSelected = selected && state.jobs.some(function (job) { return job.id === selected; });
    var selectedJob = state.selectedJob && state.selectedJob.id === selected ? state.selectedJob : null;
    var selectedOption = selected && !knownSelected
      ? '<option value="' + esc(selected) + '">' + esc(selectedJob ? jobOptionLabel(selectedJob) : t("tailor.field.saved_job_loading", "Selected saved job")) + "</option>"
      : "";
    sel.innerHTML = '<option value="">' + esc(t("tailor.field.saved_job_none", "Manual job description")) + "</option>" +
      selectedOption +
      state.jobs.map(function (job) {
        var prefix = job.starred ? "★ " : "";
        var suffix = jobNeedsFullJd(job) ? " " + t("jobs.selector.needs_full_jd", "(needs full JD)") : "";
        return '<option value="' + esc(job.id) + '">' + esc(prefix + jobOptionLabel(job) + suffix) + "</option>";
      }).join("");
    var hasSelected = selected && Array.prototype.some.call(sel.options, function (opt) { return opt.value === selected; });
    sel.value = hasSelected ? selected : "";
    renderJobSelectEmptyHint();
  }

  function loadSavedJobs() {
    return fetch("/api/jobs")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        state.jobs = (data && data.jobs) || [];
        state.jobs.sort(function (a, b) {
          return (b.starred === true) - (a.starred === true) || String(b.created_at || "").localeCompare(String(a.created_at || ""));
        });
        populateJobSelect();
      })
      .catch(function () {
        state.jobs = [];
        populateJobSelect();
      });
  }

  function loadLinkedJobDescription() {
    if (!state.jobId) {
      state.autoJdJobId = null;
      state.autoJdText = "";
      state.jdDirty = false;
      renderSelectedJobContext(null);
      return Promise.resolve();
    }
    var jd = $("#tl-jd");
    if (!jd) return Promise.resolve();
    var jid = state.jobId;
    var token = ++state.jobReq;
    if (jd.value && jd.value.trim() && state.autoJdJobId === jid) {
      return Promise.resolve();
    }
    return fetch("/api/jobs/" + encodeURIComponent(jid))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (token !== state.jobReq || state.jobId !== jid) return;
        var job = data && data.job ? data.job : null;
        renderSelectedJobContext(job);
        if (!job || !job.jd_text) {
          if (state.jobId === jid) {
            state.jobId = null;
            state.autoJdJobId = null;
            state.autoJdText = "";
            state.jdDirty = false;
            syncJobIdUrl(null);
            renderSelectedJobContext(null);
            populateJobSelect();
          }
          return;
        }
        jd.value = job.jd_text;
        state.autoJdJobId = jid;
        state.autoJdText = job.jd_text;
        state.jdDirty = false;
      })
      .catch(function () {});
  }

  function selectJobId(jobId) {
    var previousJobId = state.jobId;
    state.jobId = UUID_RE.test(jobId || "") ? jobId : null;
    syncJobIdUrl(state.jobId);
    var jd = $("#tl-jd");
    if (state.jobId !== previousJobId && jd) jd.value = "";
    state.autoJdJobId = null;
    state.autoJdText = "";
    state.jdDirty = false;
    state.reqToken++;
    resetActionControls();
    clearReview();
    clearReportState();
    renderSelectedJobContext(null);
    populateJobSelect();
    loadLinkedJobDescription();
  }

  // ── JD payload (text wins, else URL) ───────────────────────────────────────
  function jdPayload() {
    var text = $("#tl-jd").value;
    var url = $("#tl-jd-url").value;
    if (text && text.trim()) return { jd_text: text };
    if (url && url.trim()) return { jd_url: url.trim() };
    return null;
  }

  function selectedJobPayload() {
    if (state.jobId && state.autoJdJobId === state.jobId && state.autoJdText && !state.jdDirty) {
      return { job_id: state.jobId };
    }
    return jdPayload();
  }

  function invalidateJobDraftState(options) {
    options = options || {};
    if (options.clearAutoJdText) {
      var jd = $("#tl-jd");
      if (jd && state.autoJdText && jd.value === state.autoJdText) {
        jd.value = "";
      }
    }
    state.jdDirty = true;
    state.jobId = null;
    syncJobIdUrl(null);
    state.autoJdJobId = null;
    state.autoJdText = "";
    renderSelectedJobContext(null);
    populateJobSelect();
    state.jobReq++;
    state.reqToken++;
    resetActionControls();
    clearReview();
    clearReportState();
  }

  function selectedProfile() {
    var sel = $("#tl-profile");
    var value = sel && ATS_PROFILES[sel.value] ? sel.value : "semantic_fit";
    try { window.localStorage.setItem(ATS_PROFILE_KEY, value); } catch (_) {}
    return value;
  }

  function compareProfiles(profile) {
    return Object.keys(ATS_PROFILES).filter(function (key) { return key !== profile; });
  }

  // ── Score (free; rubric when a key resolves, else deterministic) ─────────────
  function scoreMatch() {
    var btn = $("#tl-score");
    if (btn.disabled) return; // double-click guard
    if (state.tailorInFlight) {
      setFeedback("info", t("tailor.feedback.finish_review", "Finish the reviewed copy before starting a new score."));
      return;
    }
    var cvId = $("#tl-cv").value;
    if (!cvId) { setFeedback("error", t("tailor.feedback.pick_cv", "Pick a source CV first.")); return; }
    var jd = selectedJobPayload();
    if (!jd) { setFeedback("error", t("tailor.feedback.paste_jd", "Paste a job description (or a URL).")); return; }
    if (blockOnFullJdReview()) return;

    var token = ++state.reqToken;
    state.scoreInFlight = true;
    clearReview();
    setBusy(btn, ".tl-score-label", t("tailor.score.busy", "Scoring..."), true);
    showOnly("loading", t("tailor.loading.matching", "Matching the job against your CV..."));
    setFeedback("info", t("tailor.loading.matching", "Matching the job against your CV..."));

    var profile = selectedProfile();
    var body = Object.assign({ cv_id: cvId, profile: profile, compare_profiles: compareProfiles(profile) }, jd);
    fetch("/api/ats-score", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (token !== state.reqToken) return; // superseded / navigated away
        if (res.status === 200) {
          renderReport(res.body, null);
          var src = res.body && res.body.source === "rubric"
            ? t("tailor.feedback.scored_ai", "Match scored by the AI rubric - no copy was created.")
            : t("tailor.feedback.scored_cv", "Match scored from your CV - no copy was created.");
          setFeedback("ok", src);
        } else {
          handleError(res);
        }
      })
      .catch(function () {
        if (token !== state.reqToken) return;
        showOnly("error", t("tailor.feedback.network", "Network error - please try again."));
        setFeedback("error", t("tailor.feedback.network", "Network error - please try again."));
      })
      .finally(function () {
        if (token === state.reqToken && !state.tailorInFlight && !state.applyReviewInFlight) {
          state.scoreInFlight = false;
          setBusy(btn, ".tl-score-label", t("tailor.score.button", "Score match (free)"), false);
        }
      });
  }

  // ── Tailor review / apply (LLM review first, explicit copy second) ──────────
  function tailor(fromOptimize) {
    var btn = fromOptimize ? $("#tl-optimize") : $("#tl-tailor");
    if (btn.disabled || state.tailorInFlight) return; // double-click guard
    var labelSel = fromOptimize ? ".tl-optimize-label" : ".tl-tailor-label";
    var idleLabel = fromOptimize ? t("tailor.optimize.button", "Optimize against this job") : t("tailor.review.button", "Review changes");

    var cvId = $("#tl-cv").value;
    if (!cvId) { setFeedback("error", t("tailor.feedback.pick_cv", "Pick a source CV first.")); return; }
    var jd = selectedJobPayload();
    if (!jd) { setFeedback("error", t("tailor.feedback.paste_jd", "Paste a job description (or a URL).")); return; }
    if (blockOnFullJdReview()) return;

    var token = ++state.reqToken;
    state.tailorInFlight = true;
    state.tailorAction = fromOptimize ? "optimize" : "review";
    setBusy(btn, labelSel, t("tailor.review.busy", "Reviewing..."), true);
    setBusy($("#tl-score"), ".tl-score-label", t("tailor.score.button", "Score match (free)"), true);
    clearReview();
    showOnly("loading", t("tailor.loading.review", "Preparing reviewed changes - reordering and rephrasing your real experience, then checking every claim and holding it to one page..."));
    setFeedback("info", t("tailor.feedback.reviewing", "Preparing reviewed changes. Nothing is saved until you apply the reviewed copy."));

    var profile = selectedProfile();
    var body = Object.assign({ cv_id: cvId, profile: profile, compare_profiles: compareProfiles(profile) }, jd);
    fetch("/api/cv/" + encodeURIComponent(cvId) + "/tailor/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (token !== state.reqToken) return; // superseded / navigated away
        if (res.status === 200) {
          state.review = res.body;
          renderReport(res.body.ats || {}, res.body);
          var note = t("tailor.feedback.review_ready", "Review ready - inspect the diff, then apply it as a new copy. Your original CV is unchanged.");
          if ((res.body.warnings || []).length) {
            note += " " + fmt(
              res.body.warnings.length === 1 ? "tailor.feedback.notes" : "tailor.feedback.notes_plural",
              res.body.warnings.length === 1 ? "{n} note below." : "{n} notes below.",
              { n: res.body.warnings.length }
            );
          }
          setFeedback("ok", note);
        } else {
          handleError(res);
        }
      })
      .catch(function () {
        if (token !== state.reqToken) return;
        showOnly("error", t("tailor.feedback.network", "Network error - please try again."));
        setFeedback("error", t("tailor.feedback.network", "Network error - please try again."));
      })
      .finally(function () {
        if (token === state.reqToken) {
          state.tailorInFlight = false;
          state.tailorAction = null;
          setBusy(btn, labelSel, idleLabel, false);
          if (!state.applyReviewInFlight) {
            setBusy($("#tl-score"), ".tl-score-label", t("tailor.score.button", "Score match (free)"), false);
          }
        }
      });
  }

  function setBusy(btn, labelSel, text, busy) {
    if (!btn) return;
    btn.disabled = busy;
    setButtonLabel(btn, labelSel, text);
  }

  function setButtonLabel(btn, labelSel, text) {
    if (!btn) return;
    var label = btn.querySelector(labelSel);
    if (label) label.textContent = text;
  }

  function setReviewApplyControlsDisabled(disabled) {
    ["tl-score", "tl-tailor", "tl-optimize", "tl-cv", "tl-profile", "tl-job", "tl-jd", "tl-jd-url"].forEach(function (id) {
      var node = document.getElementById(id);
      if (node) node.disabled = disabled;
    });
  }

  function restoreReviewApplyControls() {
    setReviewApplyControlsDisabled(false);
  }

  function clearReportState() {
    showOnly("empty");
    if (state.animTimer) { clearInterval(state.animTimer); state.animTimer = null; }
    setRing(null);
    var score = $("#tl-score-num");
    if (score) score.textContent = "—";
    var verdict = $("#tl-verdict");
    if (verdict) verdict.textContent = "";
    var optimize = $("#tl-optimize");
    if (optimize) optimize.hidden = true;
    var open = $("#tl-open-tailored");
    if (open) {
      open.hidden = true;
      open.removeAttribute("href");
    }
    var build = $("#tl-build-tailored");
    if (build) {
      build.hidden = true;
      delete build.dataset.cvId;
    }
    var warnings = $("#tl-warnings");
    if (warnings) {
      warnings.hidden = true;
      warnings.innerHTML = "";
    }
    var compare = $("#tl-profile-comparison");
    if (compare) {
      compare.hidden = true;
      compare.innerHTML = "";
    }
  }

  function resetActionControls() {
    state.scoreInFlight = false;
    state.tailorInFlight = false;
    state.tailorAction = null;
    state.applyReviewInFlight = false;
    setBusy($("#tl-score"), ".tl-score-label", t("tailor.score.button", "Score match (free)"), false);
    setBusy($("#tl-tailor"), ".tl-tailor-label", t("tailor.review.button", "Review changes"), false);
    setBusy($("#tl-optimize"), ".tl-optimize-label", t("tailor.optimize.button", "Optimize against this job"), false);
    setBusy($("#tl-apply-review"), ".tl-apply-label", t("tailor.apply.copy", "Apply as new copy"), false);
    restoreReviewApplyControls();
  }

  function relabelActionControls() {
    setButtonLabel($("#tl-score"), ".tl-score-label", state.scoreInFlight ? t("tailor.score.busy", "Scoring...") : t("tailor.score.button", "Score match (free)"));
    setButtonLabel($("#tl-tailor"), ".tl-tailor-label", state.tailorInFlight && state.tailorAction === "review" ? t("tailor.review.busy", "Reviewing...") : t("tailor.review.button", "Review changes"));
    setButtonLabel($("#tl-optimize"), ".tl-optimize-label", state.tailorInFlight && state.tailorAction === "optimize" ? t("tailor.review.busy", "Reviewing...") : t("tailor.optimize.button", "Optimize against this job"));
    setButtonLabel($("#tl-apply-review"), ".tl-apply-label", state.applyReviewInFlight ? t("tailor.apply.busy", "Applying...") : t("tailor.apply.copy", "Apply as new copy"));
    var build = $("#tl-build-tailored");
    if (build && !build.disabled) setButtonLabel(build, ".tl-build-tailored-label", t("cl.pdf", "PDF"));
    if (state.applyReviewInFlight) setReviewApplyControlsDisabled(true);
  }

  function applyReview() {
    var btn = $("#tl-apply-review");
    if (!state.review || !state.review.review_token || !state.review.source_cv_id || !btn || btn.disabled || state.applyReviewInFlight) return;
    var cvId = state.review.source_cv_id;
    var token = ++state.reqToken;
    state.applyReviewInFlight = true;
    setBusy(btn, ".tl-apply-label", t("tailor.apply.busy", "Applying…"), true);
    setReviewApplyControlsDisabled(true);
    setFeedback("info", t("tailor.feedback.applying", "Applying reviewed changes as a new CV copy..."));
    fetch("/api/cv/" + encodeURIComponent(cvId) + "/tailor/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review_token: state.review.review_token })
    })
      .then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (token !== state.reqToken) return;
        if (res.status === 201) {
          clearReview();
          renderReport(res.body.ats || {}, res.body);
          setFeedback("ok", t("tailor.feedback.applied", "Applied as a new reviewed copy - your original CV is unchanged."));
        } else {
          handleError(res);
        }
      })
      .catch(function () {
        if (token !== state.reqToken) return;
        setFeedback("error", t("tailor.feedback.network", "Network error - please try again."));
      })
      .finally(function () {
        if (token === state.reqToken) {
          state.applyReviewInFlight = false;
          setBusy(btn, ".tl-apply-label", t("tailor.apply.copy", "Apply as new copy"), false);
          restoreReviewApplyControls();
        }
      });
  }

  function handleError(res) {
    var b = res.body || {};
    var msg;
    if (res.status === 422 && b.error === "unverified_claims") {
      var items = (b.items || []).map(esc).join(", ");
      msg = t("tailor.feedback.unverified", "Blocked - the tailored CV introduced claims not in your original:") +
        " " + items + ". " + t("tailor.feedback.nothing_saved", "Nothing was saved.");
      setFeedback("error", "<strong>" + esc(t("tailor.feedback.unverified", "Blocked - the tailored CV introduced claims not in your original:")) + "</strong> " + items + ". " + esc(t("tailor.feedback.nothing_saved", "Nothing was saved.")));
    } else if (res.status === 409 && b.error === "overflow") {
      msg = t("tailor.feedback.overflow", "Blocked - the tailored CV ran onto a second page") + " (" +
        t("tailor.feedback.first_overflow", "first overflow section") + ": " + (b.heading || t("jobs.review.unknown", "unknown")) + "). " +
        t("tailor.feedback.nothing_saved", "Nothing was saved.");
      setFeedback("error", "<strong>" + esc(t("tailor.feedback.overflow", "Blocked - the tailored CV ran onto a second page")) + "</strong> (" + esc(t("tailor.feedback.first_overflow", "first overflow section")) + ": " + esc(b.heading || t("jobs.review.unknown", "unknown")) + "). " + esc(t("tailor.feedback.nothing_saved", "Nothing was saved.")));
    } else if (res.status === 409 && b.code === "source_changed") {
      msg = b.hint || t("tailor.feedback.source_changed", "The source CV changed after this review was prepared. Review the current CV before applying.");
      state.review = null;
      clearReview();
      setFeedback("error", esc(msg));
    } else if (res.status === 409 && b.code === "duplicate_review_apply") {
      msg = b.hint || t("tailor.feedback.duplicate", "This reviewed copy was already applied.");
      setFeedback("error", esc(msg));
    } else if (res.status === 409 && b.code === "review_expired") {
      msg = t("tailor.feedback.expired", "This review expired. Prepare the review again before applying.");
      setFeedback("error", msg);
    } else if (res.status === 400 && b.code === "invalid_review_token") {
      msg = t("tailor.feedback.invalid_review", "This review could not be verified. Prepare the review again before applying.");
      setFeedback("error", msg);
    } else if (res.status === 402 && b.code === "needs_key") {
      msg = t("tailor.feedback.needs_key", "Connect the selected provider key in Account to continue.");
      setFeedback("error", msg);
    } else if (res.status === 503) {
      msg = t("tailor.feedback.ai_unavailable", "AI is temporarily unavailable. Try again shortly.");
      setFeedback("error", msg);
    } else {
      msg = b.error || (t("tailor.feedback.request_failed", "Request failed") + " (" + res.status + ").");
      setFeedback("error", esc(msg));
    }
    showOnly("error", msg);
  }

  // ── Score ring (animated, reduced-motion-safe) ──────────────────────────────
  function setRing(score) {
    var arc = $("#tl-ring-arc");
    var gauge = $("#tl-gauge");
    if (!arc || !gauge) return;
    arc.style.strokeDasharray = RING_CIRC;

    var color = score == null ? "var(--border)"
      : (score >= 70 ? "var(--success)" : (score >= 45 ? "var(--warning, #b4780a)" : "var(--danger)"));
    gauge.style.setProperty("--tl-accent", color);

    var pct = score == null ? 0 : Math.max(0, Math.min(100, score)) / 100;
    var targetOffset = RING_CIRC * (1 - pct);

    if (REDUCED_MOTION && REDUCED_MOTION.matches) {
      arc.style.transition = "none";
      arc.style.strokeDashoffset = targetOffset;
      return;
    }
    // Start fully hidden, then transition to the target on the next frame so the
    // arc sweeps in. The CSS transition on stroke-dashoffset does the easing.
    arc.style.transition = "none";
    arc.style.strokeDashoffset = RING_CIRC;
    // Force reflow so the from-state is committed before we set the to-state.
    void arc.getBoundingClientRect();
    requestAnimationFrame(function () {
      arc.style.transition = "stroke-dashoffset .8s var(--ease, cubic-bezier(.32,.72,0,1))";
      arc.style.strokeDashoffset = targetOffset;
    });
  }

  // Count the headline number up to `score` (reduced-motion-safe).
  function animateScoreNum(score) {
    // Keep the role="img" gauge's accessible name in sync with the score.
    // role="img" makes the gauge a leaf, so the child #tl-score-num is hidden
    // from assistive tech — without this a screen reader only ever hears the
    // static "ATS match score" and never the actual number.
    var gauge = $("#tl-gauge");
    if (gauge) {
      gauge.setAttribute(
        "aria-label",
        score == null
          ? t("tailor.gauge.aria_empty", "ATS match score — not scored yet")
          : t("tailor.gauge.aria_scored", "ATS match score: {n} out of 100").replace("{n}", score)
      );
    }
    var num = $("#tl-score-num");
    if (!num) return;
    if (state.animTimer) { clearInterval(state.animTimer); state.animTimer = null; }
    if (score == null) { num.textContent = "—"; return; }
    if (REDUCED_MOTION && REDUCED_MOTION.matches) { num.textContent = score; return; }
    var start = performance.now(), dur = 800;
    state.animTimer = setInterval(function () {
      var t = Math.min(1, (performance.now() - start) / dur);
      // easeOutCubic
      var eased = 1 - Math.pow(1 - t, 3);
      num.textContent = Math.round(eased * score);
      if (t >= 1) { clearInterval(state.animTimer); state.animTimer = null; num.textContent = score; }
    }, 16);
  }

  // ── Report rendering (ring + breakdown + chips) ─────────────────────────────
  function renderReport(ats, tailored) {
    showOnly("report");

    var score = ats && typeof ats.score === "number" ? ats.score : null;
    var source = (ats && ats.source) || "deterministic";
    renderProfileComparison(ats || {});

    animateScoreNum(score);
    setRing(score);
    $("#tl-verdict").textContent = score === null
      ? ((ats && ats.notes) || t("tailor.report.no_keywords", "Not enough keywords in the job to score reliably."))
      : "";

    // Tier-1 (must-have keyword) coverage — present on both paths.
    var tier1 = $("#tl-tier1");
    if (ats && typeof ats.tier1_coverage === "number" && score !== null) {
      var cov = ats.tier1_coverage, target = ats.tier1_target || 70;
      tier1.innerHTML = esc(t("tailor.report.must_have", "Must-have coverage")) + ": <strong>" + cov + "%</strong> (" +
        esc(t("tailor.report.target", "target")) + " " + target + "%)";
      tier1.className = "tl-tier1 " + (cov >= target ? "tl-ok" : "tl-low");
      tier1.hidden = false;
    } else {
      tier1.hidden = true;
    }

    // "Optimize against this job" — offered after a score only. A pending
    // review or completed copy owns the action area.
    var optimize = $("#tl-optimize");
    optimize.hidden = !!(tailored && (tailored.tailored_cv_id || tailored.review_token)) || score === null;

    // Link to the tailored CV (only after a successful apply).
    var link = $("#tl-open-tailored");
    var build = $("#tl-build-tailored");
    if (tailored && tailored.tailored_cv_id) {
      link.href = "/app?id=" + encodeURIComponent(tailored.tailored_cv_id);
      link.hidden = false;
      if (build) {
        build.dataset.cvId = tailored.tailored_cv_id;
        build.hidden = false;
      }
    } else {
      link.hidden = true;
      if (build) {
        build.hidden = true;
        delete build.dataset.cvId;
      }
    }

    if (tailored && tailored.review_token) {
      renderReview(tailored);
    } else {
      clearReview();
    }

    // Model warnings (only after a tailor).
    var warn = $("#tl-warnings");
    var warnings = (tailored && tailored.warnings) || [];
    if (warnings.length) {
      warn.innerHTML = "<strong>" + esc(t("tailor.report.notes", "Notes")) + "</strong><ul>" +
        warnings.map(function (w) { return "<li>" + esc(w) + "</li>"; }).join("") + "</ul>";
      warn.hidden = false;
    } else {
      warn.hidden = true;
    }

    // Rubric-only breakdown: categories, strengths, suggestions.
    if (source === "rubric") {
      renderCategories(ats.categories || {});
      renderStrengths(ats.strengths || []);
      renderSuggestions(ats.suggestions || []);
      // Gaps are free-text strings on the rubric path.
      renderFixChips("#tl-gaps", (ats.gaps || []).map(function (g) { return { term: g }; }), "gap");
      $("#tl-matched-wrap").hidden = true;
    } else {
      // Deterministic path: hide the rubric-only sections, show matched keywords.
      $("#tl-categories-wrap").hidden = true;
      $("#tl-strengths-wrap").hidden = true;
      $("#tl-suggestions-wrap").hidden = true;
      renderFixChips("#tl-gaps", ats && ats.gaps, "gap");
      renderChips("#tl-matched", ats && ats.matched, "matched");
      $("#tl-matched-wrap").hidden = false;
    }

    if (window.renderIcons) window.renderIcons($("#tl-report"));
  }

  function renderProfileComparison(ats) {
    var box = $("#tl-profile-comparison");
    if (!box) return;
    var active = (ats && ats.profile && ats.profile.id) || selectedProfile();
    var results = (ats && ats.profile_results) || {};
    var profile = (ats && ats.profile) || ATS_PROFILES[active] || {};
    var note = document.querySelector(".tl-profile-note");
    if (note) {
      note.textContent = profile.disclaimer || t("tailor.profile.disclaimer", "CV Copilot score-system simulations only. No vendor-backed ATS certification.");
    }
    var explanation = [profile.purpose || profile.description, profile.explanation].filter(Boolean).join(" ");
    var facts = [
      [t("tailor.profile.best_for", "Best for"), profile.best_for],
      [t("tailor.profile.method", "Method"), profile.method],
      [t("tailor.profile.proof", "Proof"), profile.simulation === false ? t("tailor.profile.external", "External profile") : t("tailor.profile.simulation", "CV Copilot simulation")],
      [t("tailor.profile.vendor", "Vendor"), profile.vendor_backed ? t("tailor.profile.vendor_backed", "Vendor-backed") : t("tailor.profile.not_vendor_backed", "Not vendor-backed")],
    ].filter(function (item) { return item[1]; }).map(function (item) {
      return '<div><dt>' + esc(item[0]) + '</dt><dd>' + esc(String(item[1]).replace(/_/g, " ")) + '</dd></div>';
    }).join("");
    box.innerHTML =
      '<p class="tl-profile-detail-note">' + esc(explanation || t("tailor.profile.transparent", "Transparent CV Copilot ATS simulation.")) + '</p>' +
      '<dl class="tl-system-details" aria-label="' + esc(t("tailor.profile.details_aria", "Selected score-system details")) + '">' + facts + '</dl>' +
      '<p class="tl-profile-disclaimer">' + esc(profile.disclaimer || t("tailor.profile.transparent_disclaimer", "Transparent CV Copilot simulation; not certified by or backed by an ATS vendor.")) + '</p>' +
      '<div class="tl-profile-compare-head"><span>' + esc(t("tailor.profile.systems", "Score systems")) + '</span><small>' + esc(t("tailor.profile.simulations", "Simulations, not vendor-backed")) + '</small></div>' +
      Object.keys(ATS_PROFILES).map(function (key) {
        var data = key === active ? ats : results[key];
        var score = data && typeof data.score === "number" ? String(data.score) : "—";
        var sourceLabel = profileSourceLabel(key, data);
        return '<div class="tl-profile-row' + (key === active ? ' is-active' : '') + '">' +
          '<span>' + esc(profileLabel(key)) + '</span>' +
          '<strong>' + esc(score) + '</strong>' +
          '<small>' + esc(sourceLabel) + '</small>' +
        '</div>';
      }).join("");
    box.hidden = false;
  }

  function profileSourceLabel(key, data) {
    if (data && data.source === "rubric") return t("tailor.profile.source.ai", "AI rubric");
    if (key === "strict_requirements") return t("tailor.profile.source.must_have", "Must-have");
    if (key === "semantic_fit" && data && data.profile && data.profile.rubric_preferred) return t("tailor.profile.source.keyword_fallback", "Keyword fallback");
    if (key === "semantic_fit") return t("tailor.profile.source.keyword_fallback", "Keyword fallback");
    return t("tailor.profile.source.keyword", "Keyword");
  }

  function renderReview(review) {
    var wrap = $("#tl-review");
    var summary = $("#tl-review-summary");
    var diff = $("#tl-review-diff");
    if (!wrap || !summary || !diff) return;
    var data = review.diff || {};
    var added = Number(data.added || 0);
    var removed = Number(data.removed || 0);
    var lines = data.lines || [];
    var suffix = data.truncated ? t("tailor.review.truncated", " Diff is truncated for display.") : "";
    summary.textContent = data.changed
      ? fmt("tailor.review.changed", "Review token verified against the current source CV. +{added} / -{removed}.{suffix}", { added: added, removed: removed, suffix: suffix })
      : t("tailor.review.no_changes", "No text changes were proposed. Applying still creates a separate copy.");
    renderReviewLineage(review);
    diff.textContent = lines.length ? lines.join("\n") : t("tailor.review.no_diff", "No textual diff.");
    wrap.hidden = false;
  }

  function renderReviewLineage(review) {
    var box = $("#tl-review-lineage");
    if (!box || !review) return;
    var ats = review.ats && typeof review.ats === "object" ? review.ats : {};
    var fit = ats.experience_fit && typeof ats.experience_fit === "object" ? ats.experience_fit : {};
    var profile = ats.profile && typeof ats.profile === "object" ? (ats.profile.label || ats.profile.id) : "";
    var score = typeof ats.score === "number" ? "ATS " + ats.score + "%" : "";
    var years = fit.candidate_years != null || fit.required_years != null
      ? t("tailor.lineage.years", "Years") + " " + [fit.candidate_years == null ? "?" : fit.candidate_years, fit.required_years == null ? "?" : fit.required_years].join("/")
      : "";
    var parts = [
      ["data-lineage-review-id", t("tailor.lineage.review", "Review") + " " + (review.review_id || "")],
      ["data-lineage-source-cv", t("tailor.lineage.source_cv", "Source CV") + " " + (review.source_cv_id || state.cvId || "")],
      ["data-lineage-source-hash", t("tailor.lineage.source", "source") + " " + shortHash(review.source_hash)],
      ["data-lineage-jd-hash", t("tailor.lineage.jd", "JD") + " " + shortHash(review.jd_hash)],
      ["data-lineage-tailored-hash", t("tailor.lineage.tailored", "tailored") + " " + shortHash(review.tailored_hash)],
      ["data-lineage-job-id", t("tailor.lineage.job", "Job") + " " + (review.job_id || "")],
      ["data-lineage-score-system", profile || selectedProfile()],
      ["data-lineage-score", score],
      ["data-lineage-years", years],
      ["data-lineage-model", [review.provider, review.model].filter(Boolean).join(" / ")],
    ].filter(function (item) {
      return item[1] && !String(item[1]).endsWith(" ");
    });
    box.innerHTML = parts.map(function (item) {
      return '<span ' + item[0] + '="true">' + esc(item[1]) + '</span>';
    }).join("");
  }

  function reservePdfWindow() {
    var win = window.open("about:blank", "_blank");
    if (win) {
      try {
        win.opener = null;
        win.document.title = t("jobs.pdf.building", "Building PDF");
        win.document.body.textContent = t("jobs.pdf.building_dots", "Building PDF...");
      } catch (_) {}
    }
    return win;
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
    var opened = window.open("about:blank", "_blank");
    if (opened) {
      try {
        opened.opener = null;
        opened.location.href = url;
        return true;
      } catch (_) {}
    }
    var a = document.createElement("a");
    a.href = url;
    a.download = filename || "document.pdf";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    return false;
  }

  function buildTailoredPdf() {
    var btn = $("#tl-build-tailored");
    var cvId = btn && btn.dataset.cvId;
    if (!btn || !cvId || btn.disabled) return;
    var pdfTab = reservePdfWindow();
    var label = $(".tl-build-tailored-label");
    btn.disabled = true;
    if (label) label.textContent = t("tailor.pdf.building", "Building...");
    fetch("/api/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: cvId })
    })
      .then(function (res) {
        if (!res.ok) {
          return res.json().catch(function () { return {}; }).then(function (data) {
            throw new Error(data.hint || data.error || ("HTTP " + res.status));
          });
        }
        var ctype = res.headers.get("Content-Type") || "";
        if (ctype.indexOf("application/json") !== -1) {
          return res.json().then(function (data) {
            openPdfTarget(data.url, "tailored-cv.pdf", pdfTab);
          });
        }
        return res.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          openPdfTarget(url, "tailored-cv.pdf", pdfTab);
          setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
        });
      })
      .catch(function (err) {
        closeReservedWindow(pdfTab);
        setFeedback("error", esc((err && err.message) || t("tailor.pdf.failed", "Couldn't render the PDF.")));
      })
      .finally(function () {
        btn.disabled = false;
        if (label) label.textContent = "PDF";
      });
  }

  function renderCategories(categories) {
    var wrap = $("#tl-categories-wrap");
    var box = $("#tl-categories");
    box.innerHTML = "";
    var keys = CATEGORY_ORDER.filter(function (k) { return categories[k]; });
    if (!keys.length) { wrap.hidden = true; return; }
    wrap.hidden = false;

    keys.forEach(function (key) {
      var cat = categories[key] || {};
      var pct = typeof cat.score === "number" ? Math.max(0, Math.min(100, cat.score)) : 0;
      var label = CATEGORY_LABELS[key] || key;
      var evidence = (cat.evidence || []).filter(Boolean);

      var item = document.createElement("div");
      item.className = "tl-cat";

      var head = document.createElement("div");
      head.className = "tl-cat-head";
      head.innerHTML =
        '<span class="tl-cat-name">' + esc(label) + "</span>" +
        '<span class="tl-cat-score">' + pct + "</span>";
      item.appendChild(head);

      var bar = document.createElement("div");
      bar.className = "tl-cat-bar";
      var fill = document.createElement("div");
      fill.className = "tl-cat-fill";
      fill.setAttribute("data-pct", String(pct));
      bar.appendChild(fill);
      item.appendChild(bar);

      if (cat.reasoning) {
        var why = document.createElement("p");
        why.className = "tl-cat-why";
        why.textContent = cat.reasoning;
        item.appendChild(why);
      }
      if (evidence.length) {
        var ul = document.createElement("ul");
        ul.className = "tl-cat-evidence";
        evidence.forEach(function (e) {
          var li = document.createElement("li");
          li.textContent = e;
          ul.appendChild(li);
        });
        item.appendChild(ul);
      }
      box.appendChild(item);
    });

    // Animate the bars filling in (reduced-motion-safe).
    var reduce = REDUCED_MOTION && REDUCED_MOTION.matches;
    box.querySelectorAll(".tl-cat-fill").forEach(function (fill) {
      var pct = fill.getAttribute("data-pct") + "%";
      if (reduce) { fill.style.width = pct; return; }
      fill.style.width = "0%";
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { fill.style.width = pct; });
      });
    });
  }

  function renderStrengths(strengths) {
    var wrap = $("#tl-strengths-wrap");
    var ul = $("#tl-strengths");
    ul.innerHTML = "";
    strengths = (strengths || []).filter(Boolean);
    if (!strengths.length) { wrap.hidden = true; return; }
    wrap.hidden = false;
    strengths.forEach(function (s) {
      var li = document.createElement("li");
      li.textContent = s;
      ul.appendChild(li);
    });
  }

  function renderSuggestions(suggestions) {
    var wrap = $("#tl-suggestions-wrap");
    suggestions = (suggestions || []).filter(Boolean);
    if (!suggestions.length) { wrap.hidden = true; return; }
    wrap.hidden = false;
    renderFixChips("#tl-suggestions", suggestions.map(function (s) { return { term: s }; }), "fix");
  }

  // Clickable "fix" chips — clicking copies the chip text to the clipboard so the
  // user can paste it into the CV editor. (Gaps don't map to a single section,
  // and suggestions are sentences, so copy is the honest, reliable action.)
  function renderFixChips(sel, items, kind) {
    var box = $(sel);
    box.innerHTML = "";
    items = items || [];
    if (!items.length) {
      var p = document.createElement("p");
      p.className = "tl-chips-empty";
      p.textContent = kind === "gap"
        ? "No unmet requirements — your CV covers them all."
        : "No suggestions — your CV already reads well for this role.";
      box.appendChild(p);
      return;
    }
    items.forEach(function (it) {
      var text = it.term || it.keyword || "";
      var chip = document.createElement("button");
      chip.type = "button";
      var cls = "tl-chip tl-fix-chip";
      if (kind === "gap") cls += " tl-chip-gap";
      else cls += " tl-chip-fix";
      chip.className = cls;
      chip.setAttribute("data-copy", text);
      chip.setAttribute("title", "Click to copy");
      chip.innerHTML =
        '<span class="tl-chip-text">' + esc(text) + "</span>" +
        '<span class="tl-chip-act" data-icon="files" data-icon-size="12" aria-hidden="true"></span>';
      box.appendChild(chip);
    });
  }

  // Plain (non-interactive) chips — used for matched keywords on the
  // deterministic path.
  function renderChips(sel, items, kind) {
    var box = $(sel);
    box.innerHTML = "";
    items = items || [];
    if (!items.length) {
      var p = document.createElement("p");
      p.className = "tl-chips-empty";
      p.textContent = kind === "gap" ? "No unmet requirements — your CV covers them all." : "No matches yet.";
      box.appendChild(p);
      return;
    }
    items.forEach(function (it) {
      var chip = document.createElement("span");
      var cls = "tl-chip";
      if (kind === "matched") cls += " tl-chip-matched";
      else if (it.importance === "required") cls += " tl-chip-req";
      else if (it.importance === "preferred") cls += " tl-chip-pref";
      chip.className = cls;
      var label = esc(it.term || it.keyword || "");
      var tier = it.importance && it.importance !== "nice_to_have"
        ? ' <span class="tl-chip-tier">' + esc(it.importance.replace("_", " ")) + "</span>"
        : "";
      chip.innerHTML = label + tier;
      box.appendChild(chip);
    });
  }

  // Copy a fix-chip's text; flash a "copied" state on the chip itself.
  function copyChip(chip) {
    var text = chip.getAttribute("data-copy") || "";
    if (!text) return;
    var done = function () {
      chip.classList.add("is-copied");
      var act = chip.querySelector(".tl-chip-act");
      if (act) {
        act.setAttribute("data-icon", "check");
        if (window.renderIcons) window.renderIcons(chip);
      }
      setTimeout(function () {
        chip.classList.remove("is-copied");
        var a = chip.querySelector(".tl-chip-act");
        if (a) {
          a.setAttribute("data-icon", "files");
          if (window.renderIcons) window.renderIcons(chip);
        }
      }, 1400);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallbackCopy.bind(null, text, done));
    } else {
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "absolute";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      if (done) done();
    } catch (_) { /* clipboard blocked — silently no-op */ }
  }

  function registerPaletteActions() {
    var P = window.OnePagerPalette;
    if (!P || typeof P.register !== "function") return;
    P.register({
      id: "tailor-score",
      group: "actions",
      labelKey: "palette.tailor.score",
      label: "Score match",
      aliases: ["tailor", "ats", "score", "match"],
      icon: "target",
      run: scoreMatch,
      available: function () {
        var btn = $("#tl-score");
        return !!btn && !btn.disabled && !state.tailorInFlight;
      }
    });
    P.register({
      id: "tailor-review",
      group: "actions",
      labelKey: "palette.tailor.review",
      label: "Review changes",
      aliases: ["tailor", "review", "optimize", "job"],
      icon: "wand",
      run: function () { tailor(false); },
      available: function () {
        var btn = $("#tl-tailor");
        return !!btn && !btn.disabled && !state.tailorInFlight;
      }
    });
    P.register({
      id: "tailor-apply-copy",
      group: "actions",
      labelKey: "palette.tailor.apply",
      label: "Apply as new copy",
      aliases: ["apply", "copy", "tailored cv"],
      icon: "copy",
      run: applyReview,
      available: function () {
        var btn = $("#tl-apply-review");
        var section = $("#tl-review");
        return !!btn && !!section && !section.hidden && !btn.disabled && !btn.hidden &&
          !!state.review && !!state.review.review_token;
      }
    });
    P.register({
      id: "tailor-optimize",
      group: "actions",
      labelKey: "palette.tailor.optimize",
      label: "Optimize against this job",
      aliases: ["optimize", "tailor", "job"],
      icon: "scissors",
      run: function () { tailor(true); },
      available: function () {
        var btn = $("#tl-optimize");
        return !!btn && !btn.disabled && !btn.hidden && !state.tailorInFlight;
      }
    });
    P.register({
      id: "tailor-build-pdf",
      group: "actions",
      labelKey: "palette.tailor.pdf",
      label: "Build tailored PDF",
      aliases: ["pdf", "download", "tailored cv"],
      icon: "download",
      run: buildTailoredPdf,
      available: function () {
        var btn = $("#tl-build-tailored");
        return !!btn && !btn.disabled && !btn.hidden && !!btn.dataset.cvId;
      }
    });
  }

  // ── Wire up ────────────────────────────────────────────────────────────
  function init() {
    $("#tl-cv").addEventListener("change", function (e) {
      state.reqToken++;
      state.cvId = e.target.value;
      var previousJobId = state.jobId;
      var selected = e.target.selectedOptions && e.target.selectedOptions[0];
      var selectedJobId = selected ? selected.dataset.jobId : null;
      if (selectedJobId && UUID_RE.test(selectedJobId)) state.jobId = selectedJobId;
      else if (!UUID_RE.test(initialJobId)) state.jobId = null;
      syncJobIdUrl(state.jobId);
      populateJobSelect();
      if (state.jobId && state.jobId !== previousJobId) {
        var jd = $("#tl-jd");
        if (jd) jd.value = "";
        state.autoJdJobId = null;
        state.autoJdText = "";
      }
      state.jdDirty = false;
      resetActionControls();
      clearReview();
      clearReportState();
      renderSelectedJobContext(null);
      loadLinkedJobDescription();
    });
    if ($("#tl-profile")) {
      populateProfileSelect("semantic_fit");
      var stored = null;
      try { stored = window.localStorage.getItem(ATS_PROFILE_KEY); } catch (_) {}
      if (stored && ATS_PROFILES[stored]) $("#tl-profile").value = stored;
      $("#tl-profile").addEventListener("change", function () { selectedProfile(); });
    }
    if ($("#tl-job")) $("#tl-job").addEventListener("change", function (e) { selectJobId(e.target.value); });
    loadAtsProfiles();
    $("#tl-score").addEventListener("click", scoreMatch);
    $("#tl-tailor").addEventListener("click", function () { tailor(false); });
    $("#tl-optimize").addEventListener("click", function () { tailor(true); });
    $("#tl-apply-review").addEventListener("click", applyReview);
    $("#tl-build-tailored").addEventListener("click", buildTailoredPdf);
    registerPaletteActions();
    $("#tl-jd").addEventListener("input", function () { invalidateJobDraftState(); });
    $("#tl-jd-url").addEventListener("input", function () { invalidateJobDraftState({ clearAutoJdText: true }); });

    // Event-delegated copy for all fix-chips (gaps + suggestions).
    $("#tl-report").addEventListener("click", function (e) {
      var chip = e.target.closest && e.target.closest(".tl-fix-chip");
      if (chip) copyChip(chip);
    });

    // Bump the request token on navigate-away so any in-flight response is a
    // no-op (we don't try to render into a tearing-down page).
    window.addEventListener("pagehide", function () { state.reqToken++; });

    if (window.renderIcons) window.renderIcons(document);

    // i18n: localise [data-i18n] chrome (nav + palette trigger) and mount the
    // EN|DE toggle in the topbar, matching the other sub-pages.
    if (window.OnePagerI18n) {
      window.OnePagerI18n.init($(".cl-topbar-right"), function () {
        populateProfileSelect(selectedProfile());
        relabelActionControls();
        renderSelectedJobContext(state.selectedJob);
        if (state.review) renderReport((state.review && state.review.ats) || {}, state.review);
      });
    }

    if (window.OnePagerAuth && window.OnePagerAuth.ready) {
      window.OnePagerAuth.ready.then(function () {
        loadCredit();
        syncUrlFetchCapability();
        Promise.all([loadCvs(), loadSavedJobs()]).then(loadLinkedJobDescription);
      });
    }
  }

  // Hide the URL-fetch field when the server has no operator allowlist —
  // otherwise every submitted URL dead-ends in `source_not_allowed` with no
  // hint that the control cannot work (plan 107, F2). Fails open: if the
  // capability probe errors, the field stays visible and the server still
  // enforces the allowlist.
  function syncUrlFetchCapability() {
    fetch("/api/jobs/sources/status")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.url_fetch) return;
        var enabled = data.url_fetch.enabled === true;
        var input = $("#tl-jd-url");
        var label = document.querySelector('label[for="tl-jd-url"]');
        var note = $("#tl-url-disabled");
        if (input) input.hidden = !enabled;
        if (label) label.hidden = !enabled;
        if (note) note.hidden = enabled;
      })
      .catch(function () {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
