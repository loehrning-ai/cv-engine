/*
  Cover-letter editor — Stage 5.

  A small, additive companion to the CV editor (app.js). It reuses auth.js
  (which patches window.fetch to attach the Supabase bearer token on every
  /api/* call) and icons.js, so this file only owns the cover-letter UX:

    • list the user's CVs into the source-CV picker
    • paste a JD + tone → POST /api/cover-letter/generate
    • surface the guard results: "removed N clichés", or the
      unverified-claims blocker (a hallucinated employer/metric)
    • show the letter in an editable textarea (editable after generation)
    • save edits (POST /api/cover-letter) and download a PDF (.../build)
    • recent cover letters (Stage 7/21): one RLS-scoped GET /api/documents
      filtered to cover letters with a type badge; broad CV discovery lives in
      the editor and jobs surfaces.

  Kept deliberately framework-free and in the same plain-DOM style as app.js.
*/
(function () {
  "use strict";

  var $ = function (sel) { return document.querySelector(sel); };
  var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  // i18n: resolve a key (fall back to the EN default if the dict isn't loaded or
  // the key is missing). For the JS-rendered (dynamic) strings; [data-i18n]
  // elements are localised by OnePagerI18n.apply.
  function t(key, fallback) {
    if (!window.OnePagerI18n) return fallback;
    var v = window.OnePagerI18n.t(key);
    return v && v !== key ? v : fallback;
  }

  function localText(en, de) {
    var lang = window.OnePagerI18n && typeof window.OnePagerI18n.getLang === "function"
      ? window.OnePagerI18n.getLang()
      : (document.documentElement.lang || "en");
    return String(lang || "en").toLowerCase().slice(0, 2) === "de" ? de : en;
  }

  function applyLocalMarkup(root) {
    var scope = root || document;
    var de = localText("en", "de") === "de";
    scope.querySelectorAll("[data-local-en]").forEach(function (node) {
      node.textContent = node.getAttribute(de ? "data-local-de" : "data-local-en") || "";
    });
    scope.querySelectorAll("[data-local-aria-en]").forEach(function (node) {
      node.setAttribute("aria-label", node.getAttribute(de ? "data-local-aria-de" : "data-local-aria-en") || "");
    });
  }

  function requestedContextId(name) {
    try {
      var id = new URLSearchParams(window.location.search || "").get(name);
      return UUID_RE.test(id || "") ? id : null;
    } catch (_) {
      return null;
    }
  }

  var state = {
    cvId: null,
    sourceCvId: requestedContextId("source_cv_id"),
    requestedCvId: requestedContextId("cv_id") || requestedContextId("source_cv_id"),
    letterId: null,
    letterData: null,
    cvs: [],
    docs: [],
    jobs: [],
    jobId: null,
    autoJdJobId: null,
    autoJdText: "",
    jdDirty: false,
    selectedJob: null,
    filter: "cover_letter",
    loading: true,
    dirty: false,
    letterRevision: null,
    selectionReq: 0,
    generationReq: 0,
    previewReq: 0,
    saveReq: 0,
    saveInFlight: null,
    saveLetterId: null,
    saveRequested: false,
    downloadReq: 0,
    jobReq: 0,
    packetLineage: null,
    previewWanted: false,
  };

  function requestedLetterId() {
    try {
      return new URLSearchParams(window.location.search || "").get("id");
    } catch (_) {
      return null;
    }
  }

  function requestedJobId() {
    try {
      var id = new URLSearchParams(window.location.search || "").get("job_id");
      return UUID_RE.test(id || "") ? id : null;
    } catch (_) {
      return null;
    }
  }

  function syncJobIdUrl(jobId) {
    try {
      var url = new URL(window.location.href);
      if (UUID_RE.test(jobId || "")) url.searchParams.set("job_id", jobId);
      else url.searchParams.delete("job_id");
      if (UUID_RE.test(state.cvId || "")) url.searchParams.set("cv_id", state.cvId);
      if (UUID_RE.test(state.sourceCvId || "")) url.searchParams.set("source_cv_id", state.sourceCvId);
      window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    } catch (_) {}
    syncLifecycleLinks();
  }

  function syncLetterIdUrl(letterId) {
    try {
      var url = new URL(window.location.href);
      if (UUID_RE.test(letterId || "")) url.searchParams.set("id", letterId);
      else url.searchParams.delete("id");
      if (UUID_RE.test(state.jobId || "")) url.searchParams.set("job_id", state.jobId);
      else url.searchParams.delete("job_id");
      if (UUID_RE.test(state.cvId || "")) url.searchParams.set("cv_id", state.cvId);
      if (UUID_RE.test(state.sourceCvId || "")) url.searchParams.set("source_cv_id", state.sourceCvId);
      window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    } catch (_) {}
  }

  function contextHref(path, options) {
    options = options || {};
    try {
      var url = new URL(path, window.location.origin);
      var jobId = options.jobId || state.jobId;
      var cvId = options.cvId || state.cvId;
      var sourceCvId = options.sourceCvId || state.sourceCvId || state.cvId;
      if (UUID_RE.test(jobId || "")) url.searchParams.set("job_id", jobId);
      if (UUID_RE.test(cvId || "")) url.searchParams.set("cv_id", cvId);
      if (UUID_RE.test(sourceCvId || "")) url.searchParams.set("source_cv_id", sourceCvId);
      if (options.id && UUID_RE.test(options.id)) url.searchParams.set("id", options.id);
      return url.pathname + url.search;
    } catch (_) {
      return path;
    }
  }

  function selectedCvName() {
    var sel = $("#cl-cv");
    return sel && sel.selectedOptions && sel.selectedOptions[0]
      ? sel.selectedOptions[0].textContent
      : "";
  }

  function ownsCvId(cvId) {
    return UUID_RE.test(cvId || "") && state.cvs.some(function (cv) {
      return cv && cv.id === cvId;
    });
  }

  function hasImmutableReviewEvidence(lineage) {
    if (!lineage || typeof lineage !== "object") return false;
    var reviewId = lineage.cover_letter_review_id || lineage.review_id;
    return lineage.document_role === "cover_letter" &&
      UUID_RE.test(lineage.packet_id || "") &&
      packetState(lineage) === "packet_created" &&
      lineage.review_status === "reviewed" &&
      lineage.cover_letter_review_status === "confirmed" &&
      typeof lineage.cover_letter_review_confirmed_at === "string" &&
      !!lineage.cover_letter_review_confirmed_at.trim() &&
      typeof reviewId === "string" && !!reviewId.trim() &&
      lineage.cover_letter_id === state.letterId &&
      ownsCvId(lineage.source_cv_id) &&
      ownsCvId(lineage.tailored_cv_id) &&
      lineage.source_cv_id !== lineage.tailored_cv_id;
  }

  function syncLifecycleLinks() {
    var job = $("#cl-flow-job");
    var source = $("#cl-flow-source");
    var review = $("#cl-flow-review");
    var backApplication = $("#cl-back-application");
    var sourceCvId = state.sourceCvId || state.cvId;
    var lifecycle = $("#cl-lifecycle");
    if (lifecycle) {
      var jobStep = lifecycle.querySelector('[data-lifecycle-stage="job"]');
      var sourceStep = lifecycle.querySelector('[data-lifecycle-stage="source-cv"]');
      var reviewStep = lifecycle.querySelector('[data-lifecycle-stage="review"]');
      var reviewed = !state.dirty && hasImmutableReviewEvidence(state.packetLineage);
      if (jobStep) jobStep.classList.toggle("is-complete", UUID_RE.test(state.jobId || ""));
      if (sourceStep) sourceStep.classList.toggle("is-complete", UUID_RE.test(sourceCvId || ""));
      if (reviewStep) reviewStep.classList.toggle("is-complete", reviewed);
    }
    if (job) job.href = UUID_RE.test(state.jobId || "")
      ? contextHref("/jobs", { id: state.jobId, jobId: state.jobId, cvId: state.cvId, sourceCvId: sourceCvId })
      : contextHref("/jobs", { cvId: state.cvId, sourceCvId: sourceCvId });
    if (source) source.href = UUID_RE.test(sourceCvId || "")
      ? contextHref("/app", { id: sourceCvId, jobId: state.jobId, cvId: sourceCvId, sourceCvId: sourceCvId })
      : contextHref("/app", { jobId: state.jobId });
    if (review) review.href = contextHref("/tailor", {
      jobId: state.jobId,
      cvId: sourceCvId,
      sourceCvId: sourceCvId,
    });
    if (backApplication) {
      if (UUID_RE.test(state.jobId || "")) {
        backApplication.href = contextHref("/jobs", {
          id: state.jobId,
          jobId: state.jobId,
          cvId: state.cvId,
          sourceCvId: sourceCvId,
        });
        backApplication.hidden = false;
      } else {
        backApplication.href = "/jobs";
        backApplication.hidden = true;
      }
    }
    var context = $("#cl-flow-context");
    if (context) {
      var jobName = state.selectedJob
        ? [state.selectedJob.title, state.selectedJob.company].filter(Boolean).join(" @ ")
        : "";
      var parts = [];
      if (jobName) parts.push(localText("Job: ", "Job: ") + jobName);
      if (selectedCvName()) parts.push(localText("CV for letter: ", "Lebenslauf für Anschreiben: ") + selectedCvName());
      context.textContent = parts.join(" · ");
    }
  }

  function setFeedback(kind, html) {
    var el = $("#cl-feedback");
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

  function shortHash(value) {
    return String(value || "").slice(0, 10);
  }

  function packetState(lineage) {
    if (!lineage) return "";
    if (lineage.archived_at || lineage.status === "archived") return "archived";
    if (lineage.status === "stale" || (Array.isArray(lineage.stale_reasons) && lineage.stale_reasons.length)) return "stale";
    return lineage.status || "";
  }

  function packetLabel(lineage) {
    var current = packetState(lineage);
    if (!current) return "";
    if (current === "packet_created") return t("cl.packet.state.packet_created", "packet created");
    if (current === "stale") return t("cl.packet.state.stale", "stale");
    if (current === "archived") return t("cl.packet.state.archived", "archived");
    return current.replace(/_/g, " ");
  }

  function renderPacketLineage(lineage) {
    state.packetLineage = lineage || null;
    var box = $("#cl-packet-lineage");
    if (!box) return;
    if (!lineage || !lineage.packet_id) {
      box.hidden = true;
      box.removeAttribute("data-packet-id");
      box.removeAttribute("data-packet-state");
      return;
    }
    var current = packetState(lineage);
    box.hidden = false;
    box.dataset.packetId = lineage.packet_id || "";
    box.dataset.packetState = current;
    box.dataset.packetVersion = lineage.packet_version || "";
    $("#cl-packet-state").textContent = packetLabel(lineage);
    $("#cl-packet-version").textContent = "v" + (lineage.packet_version || 1);
    $("#cl-packet-id").textContent = t("cl.packet.packet", "Packet") + " " + String(lineage.packet_id || "").slice(0, 8);
    var job = $("#cl-packet-job");
    if (job && lineage.job_id) {
      job.hidden = false;
      job.href = contextHref("/jobs", {
        id: lineage.job_id,
        jobId: lineage.job_id,
        cvId: lineage.tailored_cv_id || state.cvId,
        sourceCvId: lineage.source_cv_id || state.sourceCvId,
      });
      job.textContent = [lineage.job_title, lineage.job_company].filter(Boolean).join(" @ ") || t("cl.packet.linked_job", "Linked job");
    } else if (job) {
      job.hidden = true;
      job.removeAttribute("href");
      job.textContent = "";
    }
    var hashes = [
      t("cl.packet.hash_source", "source") + " " + shortHash(lineage.source_cv_hash),
      t("cl.packet.hash_jd", "JD") + " " + shortHash(lineage.job_content_hash),
      t("cl.packet.hash_cv", "cv") + " " + shortHash(lineage.tailored_cv_hash),
      t("cl.packet.hash_letter", "letter") + " " + shortHash(lineage.cover_letter_hash),
    ].filter(function (item) { return !item.endsWith(" "); }).join(" · ");
    var stale = Array.isArray(lineage.stale_reasons) && lineage.stale_reasons.length
      ? " · " + lineage.stale_reasons.join(", ")
      : "";
    $("#cl-packet-hashes").textContent = hashes + stale;
    $("#cl-packet-review").textContent = [
      lineage.cover_letter_review_status || lineage.review_status || "review",
      lineage.cover_letter_review_id || lineage.review_id || "",
      lineage.prompt_version || "",
      lineage.score_system_id || "",
    ].filter(Boolean).join(" · ");
  }

  function wordCount(text) {
    var m = String(text || "").match(/[\w'-]+/g);
    return m ? m.length : 0;
  }

  function updateWordCount() {
    var body = $("#cl-body");
    var n = wordCount(body ? body.value : "");
    $("#cl-wordcount").textContent = t(n === 1 ? "cl.word.one" : "cl.word.many", n === 1 ? "{n} word" : "{n} words").replace("{n}", String(n));
  }

  function syncSourceCvAvailability() {
    var btn = $("#cl-generate");
    if (!btn) return;
    btn.disabled = !state.cvId;
    btn.dataset.noSourceCv = state.cvId ? "false" : "true";
  }

  function resetGenerateButton() {
    var btn = $("#cl-generate");
    if (!btn) return;
    btn.disabled = false;
    syncSourceCvAvailability();
    var label = btn.querySelector(".cl-btn-label");
    if (label) label.textContent = t("cl.generate.button", "Generate letter");
  }

  function clearLetterSelection() {
    state.letterId = null;
    state.letterData = null;
    state.letterRevision = null;
    state.packetLineage = null;
    state.dirty = false;
    state.saveRequested = false;
    state.saveReq++;
    state.downloadReq++;
    $("#cl-body").value = "";
    $("#cl-result-name").textContent = t("lib.result.none", "No letter selected");
    $("#cl-result-name").classList.add("muted");
    var meta = $("#cl-letter-meta");
    if (meta) meta.textContent = "";
    $("#cl-save").hidden = true;
    $("#cl-download").hidden = true;
    $("#cl-preview-toggle").hidden = true;
    var body = $("#cl-body");
    if (body) body.disabled = true;
    clearEnvelope();
    state.previewWanted = false;
    clearPreviewFrame();
    resetGenerateButton();
    updateWordCount();
    renderPacketLineage(null);
    syncLetterIdUrl(null);
    syncLifecycleLinks();
  }

  function clearPreviewFrame(preserveIntent) {
    state.previewReq++;
    var preview = $("#cl-preview");
    if (preview) {
      preview.hidden = true;
      preview.removeAttribute("src");
      preview.removeAttribute("srcdoc");
    }
    if (!preserveIntent) state.previewWanted = false;
    syncPreviewToggle(state.previewWanted);
  }

  function syncPreviewToggle(open) {
    var btn = $("#cl-preview-toggle");
    if (!btn) return;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function describeLetterMeta(data) {
    var parts = [];
    if (data && data.subject) parts.push(data.subject);
    var roleLine = [data && data.role, data && data.company].filter(Boolean).join(" @ ");
    if (roleLine) parts.push(roleLine);
    if (data && data.recipient) parts.push(data.recipient);
    if (data && Array.isArray(data.attachments) && data.attachments.length) {
      parts.push(data.attachments.join(", "));
    }
    return parts.join(" · ");
  }

  var ENVELOPE_FIELDS = [
    "recipient", "company", "role", "tone", "subject", "salutation", "closing", "attachments",
  ];

  function envelopeControl(field) {
    return $("#cl-envelope-" + field.replace(/_/g, "-"));
  }

  function attachmentLines(value) {
    if (Array.isArray(value)) {
      return value.map(function (item) { return String(item == null ? "" : item).trim(); })
        .filter(Boolean);
    }
    return String(value || "").split(/\r?\n/).map(function (item) { return item.trim(); }).filter(Boolean);
  }

  function envelopeValues() {
    var out = {};
    ENVELOPE_FIELDS.forEach(function (field) {
      var control = envelopeControl(field);
      if (field === "attachments") out[field] = attachmentLines(control ? control.value : "");
      else out[field] = control ? control.value : "";
    });
    return out;
  }

  function envelopeSnapshot(body, letterId) {
    return JSON.stringify(Object.assign({ id: letterId || state.letterId, body: body == null ? ($("#cl-body") || {}).value || "" : body }, envelopeValues()));
  }

  function setEnvelope(data) {
    ENVELOPE_FIELDS.forEach(function (field) {
      var control = envelopeControl(field);
      if (!control) return;
      control.value = field === "attachments"
        ? attachmentLines(data && data[field]).join("\n")
        : String(data && data[field] != null ? data[field] : "");
    });
    var details = $("#cl-envelope");
    var fields = $("#cl-envelope-fields");
    if (details) details.hidden = false;
    if (fields) fields.disabled = false;
  }

  function clearEnvelope() {
    ENVELOPE_FIELDS.forEach(function (field) {
      var control = envelopeControl(field);
      if (control) control.value = "";
    });
    var details = $("#cl-envelope");
    var fields = $("#cl-envelope-fields");
    if (details) {
      details.hidden = true;
      details.open = false;
    }
    if (fields) fields.disabled = true;
  }

  function generatedEnvelopeDefaults(data, requestSnapshot) {
    requestSnapshot = requestSnapshot || {};
    var payload = requestSnapshot.payload || {};
    var language = String(data && data.language || "").toLowerCase();
    var role = String(data && data.role || "").trim();
    return {
      recipient: payload.recipient || "",
      company: data && data.company || "",
      role: role,
      tone: payload.tone || "auto",
      subject: role ? (language === "de" ? "Bewerbung als " : "Application for ") + role : "",
      salutation: "",
      closing: "",
      attachments: [language === "de" ? "Lebenslauf" : "CV"],
      source_cv_id: requestSnapshot.cvId || payload.cv_id || null,
      job_id: requestSnapshot.jobId || payload.job_id || null,
    };
  }

  function syncLetterContext(data) {
    data = data || {};
    var lineage = data.packet_lineage && typeof data.packet_lineage === "object"
      ? data.packet_lineage
      : {};
    var jobId = data.job_id || lineage.job_id;
    if (Object.prototype.hasOwnProperty.call(data, "job_id") || lineage.job_id) {
      var nextJobId = UUID_RE.test(jobId || "") ? jobId : null;
      var jobChanged = nextJobId !== state.jobId;
      state.jobId = nextJobId;
      if (jobChanged) {
        var jd = $("#cl-jd");
        if (jd) jd.value = "";
        state.jobReq++;
        state.autoJdJobId = null;
        state.autoJdText = "";
        state.jdDirty = false;
        state.selectedJob = null;
      } else {
        state.selectedJob = state.jobId
          ? state.jobs.find(function (job) { return job.id === state.jobId; }) || state.selectedJob
          : null;
      }
      populateJobSelect();
      renderJobFullJdWarning();
      if (jobChanged && state.jobId) loadSelectedJobDescription();
    }
    var sourceCvId = lineage.source_cv_id || data.source_cv_id || state.sourceCvId || state.cvId;
    var letterCvId = data.source_cv_id || lineage.tailored_cv_id || state.cvId;
    state.sourceCvId = ownsCvId(sourceCvId)
      ? sourceCvId
      : (ownsCvId(letterCvId) ? letterCvId : state.cvId);
    var cvSelect = $("#cl-cv");
    if (cvSelect && UUID_RE.test(letterCvId || "") && Array.prototype.some.call(cvSelect.options, function (opt) { return opt.value === letterCvId; })) {
      state.cvId = letterCvId;
      cvSelect.value = letterCvId;
    }
    syncJobIdUrl(state.jobId);
    syncLifecycleLinks();
  }

  function refreshLetterMeta() {
    if (!state.letterData) return;
    state.letterData = Object.assign({}, state.letterData, envelopeValues(), {
      body: $("#cl-body") ? $("#cl-body").value : "",
    });
    var meta = $("#cl-letter-meta");
    if (meta) meta.textContent = describeLetterMeta(state.letterData);
  }

  function markLetterDirty() {
    if (!state.letterId) return;
    state.dirty = true;
    state.previewReq++;
    state.downloadReq++;
    if (state.previewWanted) clearPreviewFrame(true);
    else clearPreviewFrame();
    refreshLetterMeta();
    updateWordCount();
    syncLifecycleLinks();
  }

  function refreshPreview() {
    var preview = $("#cl-preview");
    if (!preview || !state.letterId) return;
    if (state.dirty) {
      var dirtyLetterId = state.letterId;
      return saveEdits().then(function (saved) {
        if (saved && state.letterId === dirtyLetterId && !state.dirty) refreshPreview();
      });
    }
    var letterId = state.letterId;
    var bodySnapshot = $("#cl-body") ? $("#cl-body").value : "";
    var token = ++state.previewReq;
    fetch("/api/cover-letter/" + encodeURIComponent(letterId) + "/preview?t=" + Date.now())
      .then(function (r) {
        if (!r.ok) throw new Error("preview failed");
        return r.text();
      })
      .then(function (html) {
        if (token !== state.previewReq || state.letterId !== letterId || ($("#cl-body") && $("#cl-body").value !== bodySnapshot)) return;
        preview.removeAttribute("src");
        preview.srcdoc = html;
        preview.hidden = false;
        state.previewWanted = true;
        syncPreviewToggle(true);
      })
      .catch(function () {
        if (token !== state.previewReq || state.letterId !== letterId) return;
        preview.hidden = true;
        preview.removeAttribute("srcdoc");
        state.previewWanted = false;
        syncPreviewToggle(false);
        setFeedback("error", t("cl.feedback.preview_failed", "Couldn't render the preview."));
      });
  }

  function togglePreview() {
    var preview = $("#cl-preview");
    if (!preview || !state.letterId) return;
    if (!preview.hidden || state.previewWanted) {
      state.previewWanted = false;
      clearPreviewFrame();
      return;
    }
    state.previewWanted = true;
    refreshPreview();
  }

  // ── Data loads ────────────────────────────────────────────────────────────
  function loadCredit() {
    fetch("/api/extract/status")
      .then(function (r) { return r.json(); })
      .then(function (s) {
        var el = $("#cl-credit");
        if (!el) return;
        if (window.OnePagerCredit) window.OnePagerCredit.render(el, s);
      })
      .catch(function () {});
  }

  function loadCvs() {
    return fetch("/api/cvs")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var sel = $("#cl-cv");
        sel.innerHTML = "";
        var cvs = data.cvs || [];
        state.cvs = cvs;
        if (!cvs.length) {
          var opt = document.createElement("option");
          opt.value = "";
          opt.textContent = t("cl.source.empty", "No CVs available");
          opt.disabled = true;
          opt.selected = true;
          sel.appendChild(opt);
          state.cvId = null;
          state.sourceCvId = null;
          state.requestedCvId = null;
          syncSourceCvAvailability();
          setFeedback("info", t("cl.source.empty_note", "Create or import a CV before generating a cover letter."));
          return;
        }
        if (!ownsCvId(state.sourceCvId)) state.sourceCvId = null;
        cvs.forEach(function (cv) {
          var opt = document.createElement("option");
          opt.value = cv.id;
          if (cv.job_id && UUID_RE.test(cv.job_id)) opt.dataset.jobId = cv.job_id;
          if (cv.packet_lineage && UUID_RE.test(cv.packet_lineage.source_cv_id || "")) {
            opt.dataset.sourceCvId = cv.packet_lineage.source_cv_id;
          }
          opt.textContent = cv.name || t("lib.untitled.cv", "Untitled CV");
          sel.appendChild(opt);
        });
        state.cvId = state.requestedCvId && Array.prototype.some.call(sel.options, function (opt) { return opt.value === state.requestedCvId; })
          ? state.requestedCvId
          : sel.options[0].value;
        sel.value = state.cvId;
        var selected = sel.selectedOptions && sel.selectedOptions[0];
        if (!state.sourceCvId) {
          state.sourceCvId = selected && ownsCvId(selected.dataset.sourceCvId)
            ? selected.dataset.sourceCvId
            : state.cvId;
        }
        if (!state.jobId && selected && UUID_RE.test(selected.dataset.jobId || "")) {
          state.jobId = selected.dataset.jobId;
        }
        syncJobIdUrl(state.jobId);
        syncLifecycleLinks();
        syncSourceCvAvailability();
      })
      .catch(function () {
        state.cvs = [];
        state.cvId = null;
        state.sourceCvId = null;
        state.requestedCvId = null;
        var sel = $("#cl-cv");
        if (sel) {
          sel.innerHTML = "";
          var opt = document.createElement("option");
          opt.value = "";
          opt.textContent = t("cl.source.empty", "No CVs available");
          opt.disabled = true;
          opt.selected = true;
          sel.appendChild(opt);
        }
        syncSourceCvAvailability();
        syncLifecycleLinks();
        setFeedback("error", t("cl.feedback.load_cvs_failed", "Couldn't load your CVs."));
      });
  }

  function jobOptionLabel(job) {
    return [job && job.title || t("jobs.untitled", "Untitled job"), job && job.company || ""].filter(Boolean).join(" @ ");
  }

  function jobNeedsFullJd(job) {
    return !!(job && job.source_state && job.source_state.requires_full_jd_review === true);
  }

  function renderJobSelectEmptyHint() {
    var hint = $("#cl-job-empty");
    if (!hint) return;
    if (state.jobs.length) {
      hint.hidden = true;
      return;
    }
    hint.innerHTML = esc(t("jobs.selector.empty", "No saved jobs yet —")) +
      ' <a href="/jobs">' + esc(t("jobs.selector.empty_link", "open the Jobs repository")) + "</a>";
    hint.hidden = false;
  }

  function renderJobFullJdWarning() {
    var note = $("#cl-job-warning");
    if (!note) return;
    var job = state.selectedJob && state.selectedJob.id === state.jobId ? state.selectedJob : null;
    if (!jobNeedsFullJd(job)) {
      note.hidden = true;
      note.innerHTML = "";
      syncLifecycleLinks();
      return;
    }
    note.innerHTML = esc(t("jobs.selector.full_jd_warning", "This imported job still needs its full job description reviewed before scoring or generation.")) +
      ' <a href="/jobs?id=' + esc(job.id) + '">' + esc(t("jobs.selector.open_in_jobs", "Open in Jobs")) + "</a>";
    note.hidden = false;
    syncLifecycleLinks();
  }

  function attachedJobNeedsFullJd() {
    // Pre-flight for the {job_id} payload path only: a manual edit detaches
    // the job (jdDirty) and falls back to raw-text generation, which stays open.
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

  function populateJobSelect() {
    var sel = $("#cl-job");
    if (!sel) return;
    var selected = state.jobId || "";
    var knownSelected = selected && state.jobs.some(function (job) { return job.id === selected; });
    var selectedOption = selected && !knownSelected
      ? '<option value="' + esc(selected) + '">' + esc(t("cl.field.saved_job_loading", "Selected saved job")) + "</option>"
      : "";
    sel.innerHTML = '<option value="">' + esc(t("cl.field.saved_job_none", "Manual job description")) + "</option>" +
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

  function loadSelectedJobDescription() {
    if (!state.jobId) {
      state.autoJdJobId = null;
      state.autoJdText = "";
      state.jdDirty = false;
      state.selectedJob = null;
      renderJobFullJdWarning();
      return Promise.resolve();
    }
    var jd = $("#cl-jd");
    if (!jd) return Promise.resolve();
    var jid = state.jobId;
    var token = ++state.jobReq;
    return fetch("/api/jobs/" + encodeURIComponent(jid))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (token !== state.jobReq || state.jobId !== jid) return;
        var job = data && data.job ? data.job : null;
        state.selectedJob = job;
        renderJobFullJdWarning();
        if (!job || !job.jd_text) {
          if (state.jobId === jid) {
            state.jobId = null;
            state.autoJdJobId = null;
            state.autoJdText = "";
            state.jdDirty = false;
            state.selectedJob = null;
            syncJobIdUrl(null);
            populateJobSelect();
            renderJobFullJdWarning();
          }
          return;
        }
        jd.value = job.jd_text;
        state.autoJdJobId = jid;
        state.autoJdText = job.jd_text;
        state.jdDirty = false;
        syncLifecycleLinks();
      })
      .catch(function () {
        if (token !== state.jobReq || state.jobId !== jid) return;
        state.autoJdJobId = null;
        state.autoJdText = "";
        state.jdDirty = false;
        state.selectedJob = null;
        renderJobFullJdWarning();
        setFeedback("error", t("cl.feedback.job_load_failed", "Couldn't load the selected saved job. Select it again before generating."));
      });
  }

  function selectJobId(jobId) {
    var previousJobId = state.jobId;
    state.jobId = UUID_RE.test(jobId || "") ? jobId : null;
    syncJobIdUrl(state.jobId);
    var jd = $("#cl-jd");
    if (state.jobId !== previousJobId && jd) jd.value = "";
    state.autoJdJobId = null;
    state.autoJdText = "";
    state.jdDirty = false;
    state.selectedJob = null;
    renderJobFullJdWarning();
    invalidateGenerationInputs();
    populateJobSelect();
    loadSelectedJobDescription();
  }

  function selectedJobPayload() {
    var body = {
      cv_id: $("#cl-cv").value,
      jd_text: $("#cl-jd").value,
      tone: $("#cl-tone").value,
      recipient: $("#cl-recipient").value
    };
    if (state.jobId && state.autoJdJobId === state.jobId && state.autoJdText && !state.jdDirty) {
      body.job_id = state.jobId;
      delete body.jd_text;
    }
    return body;
  }

  function generationRequestSnapshot() {
    var payload = selectedJobPayload();
    return {
      payload: JSON.parse(JSON.stringify(payload)),
      cvId: state.cvId,
      sourceCvId: state.sourceCvId,
      jobId: state.jobId,
    };
  }

  function invalidateGenerationInputs() {
    // Tone and recipient are part of both the persisted generation request and
    // the displayed envelope. A response for older controls must not replace
    // the current form state.
    state.selectionReq++;
    setFeedback(null);
  }

  // ── Recent cover letters ──────────────────────────────────────────────────
  // The endpoint remains broad for compatibility; this page asks only for
  // cover letters so the generator form is not competing with a mixed library.
  function loadLibrary() {
    return fetch("/api/documents?type=cover_letter")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.docs = data.documents || [];
      })
      .catch(function () { state.docs = state.docs || []; })
      .then(function () {
        state.loading = false;
        renderLibrary();
      });
  }

  function renderLibrary() {
    var list = $("#cl-list");
    if (!list) return;
    list.innerHTML = "";
    // First load: skeleton rows instead of a premature "no documents" message.
    if (state.loading) {
      list.innerHTML =
        '<div class="cl-list-skeleton" aria-hidden="true">' +
        '<span class="cl-skel-row"></span><span class="cl-skel-row"></span>' +
        '<span class="cl-skel-row"></span>' +
        "</div>";
      return;
    }
    var items = state.docs.filter(function (d) {
      return d.type === "cover_letter";
    });
    if (!items.length) {
      var msg = t("lib.empty.cover", "No cover letters yet.");
      list.innerHTML = '<p class="muted cl-empty">' + esc(msg) + "</p>";
      return;
    }
    items.forEach(function (doc) {
      list.appendChild(libraryRow(doc));
    });
    if (window.renderIcons) window.renderIcons(list);
  }

  function libraryRow(doc) {
    var isCv = doc.type === "cv";
    var row = document.createElement("div");
    row.className = "cl-list-row cl-lib-row";
    row.dataset.docId = doc.id || "";
    row.dataset.docType = doc.type || "";
    if (doc.job_id) row.dataset.jobId = doc.job_id;
    if (doc.packet_lineage && doc.packet_lineage.packet_id) {
      row.dataset.packetId = doc.packet_lineage.packet_id;
      row.dataset.packetState = packetState(doc.packet_lineage);
      row.dataset.packetVersion = doc.packet_lineage.packet_version || "";
    }

    // Type badge + name + (for cover letters) the linked-job line.
    var open = document.createElement("button");
    open.type = "button";
    open.className = "cl-list-open cl-lib-open";
    var badgeCls = isCv ? "cl-badge cl-badge-cv" : "cl-badge cl-badge-cover";
    var badgeLabel = isCv ? t("lib.badge.cv", "CV") : t("lib.badge.cover", "Cover letter");
    var sub = "";
    if (!isCv) {
      var jobName = doc.job_title || doc.role || "";
      var company = doc.company || "";
      var line = jobName && company ? (jobName + " @ " + company)
        : jobName || company || "";
      if (line) sub = '<span class="cl-lib-sub muted">' + esc(line) + "</span>";
    }
    var meta = "";
    if (!isCv) {
      meta = '<span class="cl-lib-meta">' +
        '<span class="cl-lib-meta-chip">' + esc(t("lib.storage.saved", "Saved in Documents")) + "</span>" +
        '<span class="cl-lib-meta-chip">' +
          esc(doc.job_id ? t("lib.storage.linked_job", "Linked to this job") : t("lib.storage.standalone_letter", "Standalone letter")) +
        "</span>" +
        (doc.packet_lineage ? '<span class="cl-lib-meta-chip">' +
          esc(packetLabel(doc.packet_lineage) + (doc.packet_lineage.packet_version ? " v" + doc.packet_lineage.packet_version : "")) +
        "</span>" : "") +
      "</span>";
    }
    open.innerHTML =
      '<span class="' + badgeCls + '">' + esc(badgeLabel) + "</span>" +
      '<span class="cl-lib-name">' + esc(doc.name || badgeLabel) + "</span>" +
      sub +
      meta;
    open.addEventListener("click", function () { openDocument(doc); });

    // PDF download (both types build a PDF; CV via the editor's build route).
    var dl = document.createElement("button");
    dl.type = "button";
    dl.className = "cl-list-pdf";
    dl.setAttribute("aria-label", t("lib.action.download_pdf", "Download PDF"));
    dl.setAttribute("data-tooltip", t("lib.action.download_pdf", "Download PDF"));
    dl.innerHTML = '<span data-icon="download" data-icon-size="13"></span>';
    dl.addEventListener("click", function (e) {
      e.stopPropagation();
      downloadDocument(doc);
    });

    // Soft-delete.
    var del = document.createElement("button");
    del.type = "button";
    del.className = "cl-list-del";
    del.setAttribute("aria-label", t("lib.action.delete", "Delete"));
    del.setAttribute("data-tooltip", t("lib.action.delete", "Delete"));
    del.innerHTML = '<span data-icon="trash-2" data-icon-size="13"></span>';
    del.addEventListener("click", function (e) {
      e.stopPropagation();
      deleteDocument(doc);
    });

    row.appendChild(open);
    row.appendChild(dl);
    row.appendChild(del);
    return row;
  }

  function openDocument(doc) {
    if (doc.type === "cv") {
      // CV "Open" → the existing CV editor surface.
      window.location.href = "/app?id=" + encodeURIComponent(doc.id);
      return;
    }
    openLetter(doc.id);
  }

  async function downloadDocument(doc) {
    if (doc.type === "cv") {
      // The CV build route is POST /api/build with the doc id in the body
      // (it resolves the addressed CV, enforces the 1-page gate, R2-caches).
      openPdf("/api/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: doc.id })
      });
      return;
    }
    if (state.letterId === doc.id) {
      await downloadPdf();
      return;
    }
    var token = ++state.downloadReq;
    openPdf("/api/cover-letter/" + encodeURIComponent(doc.id) + "/build", null, { token: token });
  }

  function deleteDocument(doc) {
    if (doc.type === "cv") { deleteCv(doc.id, doc.name); return; }
    deleteLetter(doc.id, doc.name);
  }

  function deleteWasCommitted(res) {
    return !!(res && (res.ok || (res.status === 503 && res.body && res.body.document_deleted === true)));
  }

  function deleteCv(id, name) {
    var label = name || t("lib.untitled.cv", "Untitled CV");
    if (!window.confirm(t("cl.confirm.delete_cv", "Delete \"{name}\"? This can't be undone.").replace("{name}", label))) return;
    fetch("/api/cv/" + encodeURIComponent(id), { method: "DELETE" })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { status: r.status, ok: r.ok, body: body };
        });
      })
      .then(function (res) {
        if (!deleteWasCommitted(res)) {
          throw new Error(res.body.error || (t("cl.feedback.delete_cv_failed", "Couldn't delete that CV.") + " (" + res.status + ")."));
        }
        state.cvs = state.cvs.filter(function (cv) { return cv.id !== id; });
        loadLibrary();
        loadCvs();
        if (!res.ok) {
          setFeedback("info", t("cl.feedback.deleted_cleanup_pending", "Deleted. PDF cleanup is pending."));
        }
      })
      .catch(function (err) { setFeedback("error", esc((err && err.message) || t("cl.feedback.delete_cv_failed", "Couldn't delete that CV."))); });
  }

  // ── Generate ────────────────────────────────────────────────────────────
  async function generate() {
    var cvId = $("#cl-cv").value;
    var jd = $("#cl-jd").value;
    if (!cvId) { setFeedback("error", t("cl.feedback.pick_cv", "Pick a source CV first.")); return; }
    if (state.jobId && !(state.autoJdJobId === state.jobId && state.autoJdText && !state.jdDirty)) {
      setFeedback("error", t("cl.feedback.job_not_ready", "The selected saved job is not loaded. Select it again before generating."));
      return;
    }
    if (!state.jobId && !jd.trim()) { setFeedback("error", t("cl.feedback.paste_jd", "Paste the job description.")); return; }
    if (blockOnFullJdReview()) return;

    var btn = $("#cl-generate");
    btn.disabled = true;
    var selectionToken = state.selectionReq;
    var token = ++state.generationReq;
    state.downloadReq++;
    if (state.dirty && state.letterId) {
      var saved = await saveEdits();
      if (!saved || token !== state.generationReq || selectionToken !== state.selectionReq) {
        if (token === state.generationReq) resetGenerateButton();
        return;
      }
    }
    var requestSnapshot = generationRequestSnapshot();
    btn.querySelector(".cl-btn-label").textContent = t("cl.generate.busy", "Generating…");
    setFeedback("info", t("cl.generate.busy_note", "Writing your letter and checking every fact against your CV…"));

    fetch("/api/cover-letter/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestSnapshot.payload)
    })
      .then(function (r) {
        return r.json().then(function (body) { return { status: r.status, body: body }; });
      })
      .then(function (res) {
        if (token !== state.generationReq || selectionToken !== state.selectionReq) {
          if (res.status === 201) loadLibrary();
          return;
        }
        if (res.status === 201) {
          showLetter(res.body, generatedEnvelopeDefaults(res.body, requestSnapshot));
          var notes = [];
          var removed = res.body.cliches_removed || [];
          if (removed.length) {
            notes.push(t(
              removed.length === 1 ? "cl.feedback.removed_cliches" : "cl.feedback.removed_cliches_plural",
              removed.length === 1 ? "Removed {n} cliché" : "Removed {n} clichés"
            ).replace("{n}", String(removed.length)) + " (" +
              removed.map(function (c) { return esc(c.phrase); }).join(", ") + ")");
          }
          notes.push(t("cl.feedback.verified", "Every name and number was verified against your CV."));
          setFeedback("ok", notes.join(" · "));
          loadLibrary();
          loadCredit();
          return;
        }
        // Guard blockers & policy responses.
        if (res.status === 422 && res.body.error === "unverified_claims") {
          var items = (res.body.items || []).map(esc).join(", ");
          setFeedback(
            "error",
            "<strong>" + esc(t("cl.feedback.unverified", "Blocked - couldn't verify these claims against your CV:")) + "</strong> " +
              items +
              ". " + esc(t("cl.feedback.not_saved", "The letter was not saved.")) + " " +
              esc(t("cl.feedback.adjust_cv", "These look invented; regenerate or adjust your CV so they're grounded."))
          );
        } else if (res.status === 422 && res.body.code === "word_cap") {
          setFeedback("error", esc(res.body.error || "Letter too long."));
        } else if (res.status === 402 && res.body.code === "needs_key") {
          setFeedback(
            "error",
            t("cl.feedback.needs_key", "Connect the selected provider key in Account to continue.")
          );
        } else if (res.status === 503) {
          setFeedback("error", t("cl.feedback.ai_unavailable", "AI is temporarily unavailable. Try again shortly."));
        } else {
          setFeedback("error", esc(res.body.error || (t("cl.feedback.generation_failed", "Generation failed") + " (" + res.status + ").")));
        }
      })
      .catch(function () {
        if (token !== state.generationReq || selectionToken !== state.selectionReq) return;
        setFeedback("error", t("cl.feedback.network", "Network error - please try again."));
      })
      .finally(function () {
        if (token === state.generationReq) {
          resetGenerateButton();
        }
      });
  }

  function showLetter(data, envelopeFallback) {
    if (state.letterId && state.letterId !== data.id) clearPreviewFrame();
    state.letterId = data.id;
    var letterData = Object.assign({}, envelopeFallback || {}, data || {});
    ENVELOPE_FIELDS.forEach(function (field) {
      if (letterData[field] == null) letterData[field] = field === "attachments" ? [] : "";
    });
    state.letterData = letterData;
    state.letterRevision = data.updated_at || null;
    state.dirty = false;
    state.saveRequested = false;
    state.saveReq++;
    state.downloadReq++;
    var saveBtn = $("#cl-save");
    if (saveBtn) saveBtn.disabled = false;
    var downloadBtn = $("#cl-download");
    if (downloadBtn) downloadBtn.disabled = false;
    var previewBtn = $("#cl-preview-toggle");
    if (previewBtn) previewBtn.disabled = false;
    $("#cl-result-name").textContent = data.name || t("lib.badge.cover", "Cover letter");
    $("#cl-result-name").classList.remove("muted");
    $("#cl-body").value = letterData.body || "";
    $("#cl-body").disabled = false;
    setEnvelope(letterData);
    var meta = $("#cl-letter-meta");
    if (meta) meta.textContent = describeLetterMeta(letterData);
    renderPacketLineage(letterData.packet_lineage);
    syncLetterContext(letterData);
    syncLetterIdUrl(state.letterId);
    $("#cl-save").hidden = false;
    $("#cl-download").hidden = false;
    $("#cl-preview-toggle").hidden = false;
    updateWordCount();
  }

  function openLetter(id) {
    var token = ++state.selectionReq;
    state.downloadReq++;
    if (state.dirty && state.letterId) {
      return saveEdits().then(function (saved) {
        if (saved && token === state.selectionReq) openLetter(id);
      });
    }
    state.letterId = null;
    state.dirty = false;
    state.packetLineage = null;
    var save = $("#cl-save");
    var download = $("#cl-download");
    var previewToggle = $("#cl-preview-toggle");
    if (save) save.disabled = true;
    if (download) download.disabled = true;
    if (previewToggle) previewToggle.disabled = true;
    var body = $("#cl-body");
    if (body) body.disabled = true;
    var envelopeFields = $("#cl-envelope-fields");
    if (envelopeFields) envelopeFields.disabled = true;
    resetGenerateButton();
    clearPreviewFrame();
    fetch("/api/cover-letter?id=" + encodeURIComponent(id))
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { status: r.status, ok: r.ok, body: body };
        });
      })
      .then(function (data) {
        if (token !== state.selectionReq) return;
        if (!data.ok || data.body.error) {
          clearLetterSelection();
          setFeedback("error", esc(data.body.error || (t("cl.feedback.open_failed", "Couldn't open that letter.") + " (" + data.status + ").")));
          return;
        }
        showLetter(data.body);
        setFeedback(null);
      })
      .catch(function () {
        if (token !== state.selectionReq) return;
        clearLetterSelection();
        setFeedback("error", t("cl.feedback.open_failed", "Couldn't open that letter."));
      });
  }

  function markPacketLineageStaleAfterPartialSave() {
    if (!state.packetLineage) {
      renderPacketLineage(null);
      return;
    }
    var reasons = Array.isArray(state.packetLineage.stale_reasons)
      ? state.packetLineage.stale_reasons.slice()
      : [];
    if (reasons.indexOf("cover_letter_stale") === -1) reasons.push("cover_letter_stale");
    renderPacketLineage(Object.assign({}, state.packetLineage, {
      status: "stale",
      stale_reasons: reasons,
    }));
  }

  function saveCurrentSnapshot(letterId) {
    var body = $("#cl-body").value;
    var persistedPayload = JSON.parse(JSON.stringify({ id: letterId, body: body }));
    Object.assign(persistedPayload, envelopeValues());
    var snapshot = JSON.stringify(persistedPayload);
    var requestPayload = Object.assign({}, persistedPayload, { revision: state.letterRevision });
    var token = ++state.saveReq;
    return fetch("/api/cover-letter", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPayload)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (responseBody) {
          return { status: r.status, body: responseBody };
        });
      })
      .then(function (res) {
        if (token !== state.saveReq || state.letterId !== letterId) return false;
        var cleanupPending = res.status === 503 && res.body.document_updated === true && !!res.body.updated_at;
        if (res.status !== 200 && !cleanupPending) {
          setFeedback("error", esc(res.body.error || t("cl.feedback.save_failed", "Save failed.")));
          return false;
        }

        state.letterRevision = res.body.updated_at || state.letterRevision;
        state.letterData = Object.assign({}, state.letterData || {}, persistedPayload);
        var currentMatchesSnapshot = envelopeSnapshot() === snapshot;
        state.dirty = !currentMatchesSnapshot;
        refreshLetterMeta();
        if (res.body.packet_lineage) renderPacketLineage(res.body.packet_lineage);
        else if (cleanupPending) markPacketLineageStaleAfterPartialSave();
        else renderPacketLineage(null);
        syncLifecycleLinks();
        loadLibrary();

        if (currentMatchesSnapshot) {
          setFeedback(
            cleanupPending ? "info" : "ok",
            cleanupPending
              ? t("cl.feedback.saved_cleanup_pending", "Saved. PDF cleanup is pending; the document itself is current.")
              : t("cl.feedback.saved", "Saved.")
          );
          if (state.previewWanted) refreshPreview();
        }
        return true;
      })
      .catch(function () {
        if (token !== state.saveReq || state.letterId !== letterId) return false;
        setFeedback("error", t("cl.feedback.save_network", "Save failed - network error."));
        return false;
      });
  }

  function runSaveQueue(letterId) {
    if (state.letterId !== letterId) return Promise.resolve(false);
    if (!state.saveRequested) return Promise.resolve(!state.dirty);
    state.saveRequested = false;
    if (!state.dirty) return Promise.resolve(true);
    return saveCurrentSnapshot(letterId).then(function (committed) {
      if (!committed || state.letterId !== letterId) {
        state.saveRequested = false;
        return false;
      }
      if (state.saveRequested) return runSaveQueue(letterId);
      return !state.dirty;
    });
  }

  function saveEdits() {
    if (!state.letterId) return Promise.resolve(false);
    var letterId = state.letterId;
    if (state.saveInFlight) {
      if (state.saveLetterId === letterId) {
        state.saveRequested = true;
        return state.saveInFlight;
      }
      return state.saveInFlight.then(function () {
        return state.letterId === letterId ? saveEdits() : false;
      });
    }

    state.saveRequested = true;
    state.saveLetterId = letterId;
    var btn = $("#cl-save");
    if (btn) btn.disabled = true;
    var tracked = runSaveQueue(letterId).finally(function () {
      if (state.saveInFlight !== tracked) return;
      state.saveInFlight = null;
      state.saveLetterId = null;
      state.saveRequested = false;
      if (btn && state.letterId === letterId) btn.disabled = false;
    });
    state.saveInFlight = tracked;
    return tracked;
  }

  async function downloadPdf() {
    if (!state.letterId) return;
    var letterId = state.letterId;
    var token = ++state.downloadReq;
    var pdfTab = reservePdfWindow();
    if (state.dirty) {
      var saved = await saveEdits();
      if (!saved || token !== state.downloadReq || state.letterId !== letterId) {
        closeReservedWindow(pdfTab);
        return;
      }
    }
    openPdf("/api/cover-letter/" + encodeURIComponent(letterId) + "/build", null, { token: token, letterId: letterId }, pdfTab);
  }

  // Open a built PDF in a new tab. The bearer token is attached by auth.js's
  // fetch patch. Two delivery modes (Stage 8): R2 configured → JSON {url,…}
  // (open the signed R2 URL directly); R2 unset (dev) → an application/pdf blob
  // (a plain link wouldn't carry the token, so we fetch + object-URL).
  function pdfRequestStale(guard) {
    return guard && (
      (guard.token != null && guard.token !== state.downloadReq) ||
      (guard.letterId && guard.letterId !== state.letterId)
    );
  }

  function reservePdfWindow() {
    var pdfTab = window.open("about:blank", "_blank");
    if (pdfTab) {
      try {
        pdfTab.opener = null;
        pdfTab.document.title = t("jobs.pdf.building", "Building PDF");
        pdfTab.document.body.textContent = t("jobs.pdf.building_dots", "Building PDF...");
      } catch (_) {}
    }
    return pdfTab;
  }

  function openPdf(path, opts, guard, reservedWindow) {
    var pdfTab = reservedWindow || reservePdfWindow();
    fetch(path, opts)
      .then(function (r) {
        if (pdfRequestStale(guard)) {
          closeReservedWindow(pdfTab);
          return;
        }
        if (!r.ok) {
          closeReservedWindow(pdfTab);
          // A 1-page-overflow build returns a structured 400 — surface it.
          var ctype0 = r.headers.get("Content-Type") || "";
          if (ctype0.indexOf("application/json") !== -1) {
            return r.json().then(function (data) {
              if (pdfRequestStale(guard)) return;
              setFeedback("error", esc(data.hint || data.error || t("cl.feedback.pdf_failed", "Couldn't render the PDF.")));
            });
          }
          throw new Error("render failed");
        }
        var ctype = r.headers.get("Content-Type") || "";
        if (ctype.indexOf("application/json") !== -1) {
          return r.json().then(function (data) {
            if (pdfRequestStale(guard)) {
              closeReservedWindow(pdfTab);
              return;
            }
            openPdfTarget(data.url, "cover-letter.pdf", pdfTab);
          });
        }
        return r.blob().then(function (blob) {
          if (pdfRequestStale(guard)) {
            closeReservedWindow(pdfTab);
            return;
          }
          var url = URL.createObjectURL(blob);
          openPdfTarget(url, "cover-letter.pdf", pdfTab);
          setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
        });
      })
      .catch(function () {
        if (pdfRequestStale(guard)) {
          closeReservedWindow(pdfTab);
          return;
        }
        closeReservedWindow(pdfTab);
        setFeedback("error", t("cl.feedback.pdf_failed", "Couldn't render the PDF."));
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

  function deleteLetter(id, name) {
    var label = name || t("lib.badge.cover", "Cover letter");
    if (!window.confirm(t("cl.confirm.delete_letter", "Delete \"{name}\"?").replace("{name}", label))) return;
    fetch("/api/cover-letter/" + encodeURIComponent(id), { method: "DELETE" })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { status: r.status, ok: r.ok, body: body };
        });
      })
      .then(function (res) {
        if (!deleteWasCommitted(res)) {
          throw new Error(res.body.error || (t("cl.feedback.delete_letter_failed", "Couldn't delete that letter.") + " (" + res.status + ")."));
        }
        state.docs = state.docs.filter(function (doc) { return doc.id !== id; });
        if (state.letterId === id) {
          clearLetterSelection();
        }
        renderLibrary();
        loadLibrary();
        if (!res.ok) {
          setFeedback("info", t("cl.feedback.deleted_cleanup_pending", "Deleted. PDF cleanup is pending."));
        }
      })
      .catch(function (err) { setFeedback("error", esc((err && err.message) || t("cl.feedback.delete_letter_failed", "Couldn't delete that letter."))); });
  }

  // ── Library type filter (All / CVs / Cover letters) ───────────────────────
  function setFilter(filter, btn) {
    state.filter = filter;
    document.querySelectorAll(".cl-chip").forEach(function (c) {
      var on = c === btn;
      c.classList.toggle("is-active", on);
      c.setAttribute("aria-pressed", on ? "true" : "false");
    });
    renderLibrary();
  }

  function registerPaletteActions() {
    var P = window.OnePagerPalette;
    if (!P || typeof P.register !== "function") return;
    P.register({
      id: "letters-generate",
      group: "actions",
      labelKey: "palette.letters.generate",
      label: "Generate cover letter",
      aliases: ["cover", "letter", "generate", "write"],
      icon: "sparkles",
      run: generate,
      available: function () {
        var btn = $("#cl-generate");
        return !!btn && !btn.disabled;
      }
    });
    P.register({
      id: "letters-save",
      group: "actions",
      labelKey: "palette.letters.save",
      label: "Save cover letter",
      aliases: ["save", "cover", "letter"],
      icon: "save",
      run: saveEdits,
      available: function () {
        var btn = $("#cl-save");
        return !!state.letterId && !!btn && !btn.disabled;
      }
    });
    P.register({
      id: "letters-preview",
      group: "actions",
      labelKey: "palette.letters.preview",
      label: "Toggle cover letter preview",
      aliases: ["preview", "cover", "letter"],
      icon: "eye",
      run: togglePreview,
      available: function () {
        var btn = $("#cl-preview-toggle");
        return !!state.letterId && !!btn && !btn.disabled && !btn.hidden;
      }
    });
    P.register({
      id: "letters-download",
      group: "actions",
      labelKey: "palette.letters.download",
      label: "Download cover letter PDF",
      aliases: ["pdf", "download", "cover", "letter"],
      icon: "download",
      run: downloadPdf,
      available: function () {
        var btn = $("#cl-download");
        return !!state.letterId && !!btn && !btn.disabled && !btn.hidden;
      }
    });
  }

  // ── Wire up ────────────────────────────────────────────────────────────
  function init() {
    applyLocalMarkup();
    syncLifecycleLinks();
    $("#cl-cv").addEventListener("change", function (e) {
      state.cvId = e.target.value;
      var selected = e.target.selectedOptions && e.target.selectedOptions[0];
      state.sourceCvId = selected && ownsCvId(selected.dataset.sourceCvId)
        ? selected.dataset.sourceCvId
        : state.cvId;
      var linkedJobId = selected && UUID_RE.test(selected.dataset.jobId || "")
        ? selected.dataset.jobId
        : null;
      if (linkedJobId && linkedJobId !== state.jobId) selectJobId(linkedJobId);
      else {
        invalidateGenerationInputs();
        syncJobIdUrl(state.jobId);
      }
      syncSourceCvAvailability();
      syncLifecycleLinks();
    });
    if ($("#cl-job")) $("#cl-job").addEventListener("change", function (e) { selectJobId(e.target.value); });
    $("#cl-tone").addEventListener("change", invalidateGenerationInputs);
    $("#cl-recipient").addEventListener("input", invalidateGenerationInputs);
    if ($("#cl-jd")) $("#cl-jd").addEventListener("input", function () {
      if (state.autoJdText && $("#cl-jd").value === state.autoJdText) return;
      state.jdDirty = true;
      state.jobId = null;
      syncJobIdUrl(null);
      state.autoJdJobId = null;
      state.autoJdText = "";
      populateJobSelect();
      state.jobReq++;
      invalidateGenerationInputs();
    });
    $("#cl-generate").addEventListener("click", generate);
    $("#cl-save").addEventListener("click", saveEdits);
    $("#cl-download").addEventListener("click", downloadPdf);
    $("#cl-preview-toggle").addEventListener("click", togglePreview);
    $("#cl-body").addEventListener("input", markLetterDirty);
    document.querySelectorAll("[data-envelope-field]").forEach(function (control) {
      control.addEventListener("input", markLetterDirty);
      control.addEventListener("change", markLetterDirty);
    });
    document.querySelectorAll(".cl-chip").forEach(function (chip) {
      chip.addEventListener("click", function () { setFilter(chip.dataset.filter, chip); });
    });
    window.addEventListener("beforeunload", function (event) {
      if (!state.dirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
    document.addEventListener("click", function (event) {
      if (!state.dirty) return;
      var link = event.target.closest("#cl-lifecycle a, #cl-back-application");
      if (!link) return;
      if (!window.confirm(localText("Discard unsaved cover-letter edits?", "Ungespeicherte Anschreiben-Aenderungen verwerfen?"))) {
        event.preventDefault();
      } else {
        state.dirty = false;
      }
    });
    if (window.renderIcons) window.renderIcons(document);
    registerPaletteActions();

    // i18n: load + localise [data-i18n], mount the EN|DE toggle, and on switch
    // re-render the library so the JS-rendered type badges + empty states
    // localise live too.
    if (window.OnePagerI18n) {
      window.OnePagerI18n.init($(".cl-topbar-right"), function () {
        applyLocalMarkup();
        renderLibrary();
        populateJobSelect();
        syncLifecycleLinks();
      });
    }

    // Wait for auth (auth.js renders a login gate if there's no session, and
    // patches fetch so our /api calls carry the bearer token).
    if (window.OnePagerAuth && window.OnePagerAuth.ready) {
      window.OnePagerAuth.ready.then(function () {
        loadCredit();
        state.jobId = requestedJobId();
        Promise.all([loadCvs(), loadSavedJobs(), loadLibrary()]).then(function () {
          populateJobSelect();
          if (state.jobId) loadSelectedJobDescription();
          var id = requestedLetterId();
          if (id) openLetter(id);
        });
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
