/*
  Jobs catalog page — plan 002 Stage 6.

  A sortable per-user catalog grid for CV Copilot's Supabase multi-tenant
  stack. Reuses auth.js (patches window.fetch to attach the Supabase bearer on
  every /api/* call), icons.js and theme-init.js, so this file only owns the
  catalog UX:

    • save a job by pasting a JD / giving a URL, with catalog metadata
      (field / deadline / confidence / your-note)
    • a sortable, filterable, searchable GRID of cards: status badge, field pill,
      deadline pill (soon/closed), a confidence bar, a has-cover badge
    • a per-card split button: Generate (→ review-first packet flow via the
      detail pane) OR Open (→ /cover-letters?id=<cover_id>)
    • a detail pane: source-CV picker, status edit, linked tailored CVs + letters

  The server enforces the safety contract (SSRF guard on the URL; the
  anti-fabrication + 1-page guards in the one-click chain; the cross-tenant
  job_id check). This file surfaces results + blockers clearly. Framework-free,
  plain DOM, matching app.js's style.
*/
(function () {
  "use strict";

  var $ = function (sel) { return document.querySelector(sel); };
  var JOB_SOURCE_DEFAULTS_KEY = "cv.editor.job_source_defaults";
  var FRESHNESS_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;

  // i18n: resolve a key, falling back to the given default if the dict isn't
  // loaded or the key is missing. Used for the JS-rendered (dynamic) strings;
  // [data-i18n] elements are localised by OnePagerI18n.apply.
  function t(key, fallback) {
    if (!window.OnePagerI18n) return fallback;
    var v = window.OnePagerI18n.t(key);
    return v && v !== key ? v : fallback;
  }

  var state = {
    jobs: [],            // the catalog (immutable-replaced on changes)
    jobId: null,         // open detail
    detailReq: 0,
    navReq: 0,
    statusReq: 0,
    packetReq: 0,
    packetActionReq: 0,
    sourceReq: 0,
    importReq: 0,
    importPreviewReq: 0,
    localSearchReq: 0,
    importMutationReq: 0,
    applyReviewInFlight: false,
    loadError: null,
    filter: "all",       // active chip
    sort: "smart",       // active sort
    query: "",           // search box
    savedFilters: {
      family: "",
      searchMode: "flexible",
      country: "",
      city: "",
      region: "",
      provider: "",
      verification: "",
      remote: "",
      seniority: "",
      freshness: "",
      minYears: "",
      rankBucket: ""
    },
    loading: true,       // true until the first /api/jobs fetch resolves
    packetReview: null,
    pendingPacket: null,
    fullJdDrafts: {},
    detailJob: null,
    detailPackets: [],
    importRunId: null,
    importResults: [],
    jobDefaultsApplied: false,
    starReqs: {}
  };

  function requestedJobId() {
    try {
      return new URLSearchParams(window.location.search || "").get("id");
    } catch (_) {
      return null;
    }
  }

  function syncJobUrl(id) {
    if (!window.history || !window.history.replaceState) return;
    var url = new URL(window.location.href);
    if (id) url.searchParams.set("id", id);
    else url.searchParams.delete("id");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }

  function resetDetail() {
    state.jobId = null;
    $("#jb-detail").hidden = true;
    $("#jb-detail-empty").hidden = false;
    $("#jb-detail-title").textContent = "";
    $("#jb-detail-company").textContent = "";
    $("#jb-jd-text").textContent = "";
    $("#jb-detail-meta").innerHTML = "";
    $("#jb-cvs").innerHTML = "";
    $("#jb-letters").innerHTML = "";
    var sourceInput = $("#jb-source-full-jd");
    if (sourceInput) sourceInput.value = "";
    state.navReq++;
    state.applyReviewInFlight = false;
    resetJobActionControls();
    clearPacketReview();
    renderGrid();
    syncJobUrl(null);
  }

  // Status label for a status key — i18n-resolved live so it follows the toggle.
  function statusLabel(status) {
    var defaults = {
      saved: "Saved", open: "Open", speculative: "Speculative",
      applied: "Applied", closed: "Closed"
    };
    return t("jobs.status." + status, defaults[status] || status);
  }
  // Smart-sort precedence for the lifecycle (lower = earlier).
  var STATUS_ORDER = { open: 0, speculative: 1, saved: 2, applied: 3, closed: 4 };

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setFeedback(el, kind, html) {
    if (!el) return;
    if (!html) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;
    el.className = "cl-feedback cl-feedback-" + kind;
    el.innerHTML = html;
  }

  function safeLocalGet(key) {
    try { return window.localStorage.getItem(key); } catch (_) { return null; }
  }

  function safeLocalSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (_) {}
  }

  var JOB_PROFILE_SCHEMA = "onepager.job_profile.v1";
  var JOB_SOURCES = ["", "linkedin", "manual", "all"];
  var JOB_SEARCH_MODES = ["flexible", "strict"];
  var JOB_REMOTE_MODES = ["", "onsite", "hybrid", "remote"];
  var JOB_FRESHNESS = ["", "fresh", "stale"];

  function profileSecretShaped(value) {
    return /\b(li_at|jsessionid|session[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|secret[_-]?key|private[_-]?key|client[_-]?secret|authorization\s*[:=]|bearer\s+[A-Za-z0-9._~+/=-]+|cookie\s*[:=]|password\s*[:=]|secret\s*[:=]|key\s*[:=]\s*[^,\s;]+|token\s*[=:]\s*[^,\s;]+|sk-[A-Za-z0-9_-]+|AIza[0-9A-Za-z_-]{20,}|ya29\.[0-9A-Za-z_-]{20,})/i
      .test(String(value == null ? "" : value));
  }

  function cleanProfileText(value, limit) {
    var text = String(value == null ? "" : value).trim().slice(0, limit);
    return profileSecretShaped(text) ? "" : text;
  }

  function splitProfileList(value, limit, itemLimit) {
    return String(value == null ? "" : value)
      .split(/[,\n]/)
      .map(function (item) { return item.trim().slice(0, itemLimit); })
      .filter(function (item) { return !profileSecretShaped(item); })
      .filter(Boolean)
      .slice(0, limit);
  }

  function normalizeMinYears(value) {
    var raw = String(value == null ? "" : value).trim();
    if (!raw) return "";
    var parsed = Number(raw);
    if (!isFinite(parsed)) return "";
    parsed = Math.max(0, Math.min(40, Math.round(parsed * 2) / 2));
    return String(parsed).replace(/\.0$/, "");
  }

  function sanitizeRanking(ranking) {
    if (!ranking || typeof ranking !== "object") return {};
    var out = {};
    Object.keys(ranking).forEach(function (key) {
      var safeKey = cleanProfileText(key, 40).toLowerCase().replace(/[^a-z_]/g, "");
      var value = Number(ranking[key]);
      if (!safeKey || profileSecretShaped(safeKey) || /^(key|token|secret|password|cookie|authorization|bearer|session|li_at|jsessionid|csrf)$/.test(safeKey) || !isFinite(value)) return;
      out[safeKey] = Math.max(0, Math.min(1, value));
    });
    return out;
  }

  function normalizeJobProfile(defaults) {
    if (!defaults || typeof defaults !== "object") return {};
    var hasSource = Object.prototype.hasOwnProperty.call(defaults, "source");
    var validSource = hasSource && JOB_SOURCES.indexOf(defaults.source) >= 0;
    return {
      schema: defaults.schema === JOB_PROFILE_SCHEMA ? defaults.schema : "",
      source: validSource ? defaults.source : "manual",
      source_explicit: validSource,
      title: cleanProfileText(defaults.title, 300),
      family: cleanProfileText(defaults.family, 160),
      titles: Array.isArray(defaults.titles) ? splitProfileList(defaults.titles.join(","), 10, 120) : splitProfileList(defaults.titles || "", 10, 120),
      search_mode: JOB_SEARCH_MODES.indexOf(defaults.search_mode) >= 0 ? defaults.search_mode : "flexible",
      country: cleanProfileText(defaults.country, 80),
      region: cleanProfileText(defaults.region, 120),
      cities: Array.isArray(defaults.cities) ? splitProfileList(defaults.cities.join(","), 10, 120) : splitProfileList(defaults.cities || "", 10, 120),
      remote_mode: JOB_REMOTE_MODES.indexOf(defaults.remote_mode) >= 0 ? defaults.remote_mode : "",
      seniority: cleanProfileText(defaults.seniority, 120),
      freshness: JOB_FRESHNESS.indexOf(defaults.freshness) >= 0 ? defaults.freshness : "",
      min_years: normalizeMinYears(defaults.min_years),
      ranking: sanitizeRanking(defaults.ranking)
    };
  }

  function compactJobProfile(defaults) {
    var compact = { schema: JOB_PROFILE_SCHEMA };
    Object.keys(defaults || {}).forEach(function (key) {
      var value = defaults[key];
      if (key === "schema") return;
      if (key.charAt(0) === "_") return;
      if (key === "source_explicit") return;
      if (key === "source" && defaults.source_explicit === false) return;
      if (Array.isArray(value)) {
        if (value.length) compact[key] = value;
      } else if (value && typeof value === "object") {
        if (Object.keys(value).length) compact[key] = value;
      } else if (value || key === "source" || key === "search_mode") {
        compact[key] = value;
      }
    });
    return compact;
  }

  function cleanFilterValue(selector, limit) {
    var node = $(selector);
    return node ? cleanProfileText(node.value, limit) : "";
  }

  function enumFilterValue(selector, values, fallback) {
    var node = $(selector);
    var value = node ? node.value : fallback;
    return values.indexOf(value) >= 0 ? value : fallback;
  }

  function provenanceBadges(job) {
    var badges = [];
    var rank = shortlistRank(job);
    if (rank.bucket) {
      badges.push('<span class="jb-pill jb-rank-bucket">' + esc(rank.bucket) + "</span>");
    }
    if (rank.rank != null) {
      badges.push('<span class="jb-pill jb-rank-order">#' + esc(rank.rank) + "</span>");
    }
    if (job.source_provider) {
      badges.push('<span class="jb-pill">' + esc(job.source_provider) + "</span>");
    }
    if (job.verification_status) {
      badges.push('<span class="jb-pill jb-pill--verify">' + esc(verificationLabel(job.verification_status)) + "</span>");
    }
    if (job.source_location || job.source_region || job.source_country) {
      badges.push('<span class="jb-pill">' + esc([job.source_location, job.source_region, job.source_country].filter(Boolean).join(", ")) + "</span>");
    }
    if (job.job_family) badges.push('<span class="jb-pill">' + esc(job.job_family) + "</span>");
    var years = yearsLabel(job);
    if (years) badges.push('<span class="jb-pill jb-pill--years">' + esc(years) + "</span>");
    if (job.remote_mode) badges.push('<span class="jb-pill">' + esc(job.remote_mode) + "</span>");
    if (job.seniority) badges.push('<span class="jb-pill">' + esc(job.seniority) + "</span>");
    if (job.salary_text) badges.push('<span class="jb-pill">' + esc(job.salary_text) + "</span>");
    var fresh = freshnessState(job);
    if (fresh.label) badges.push('<span class="jb-pill jb-pill--verify">' + esc(fresh.label) + "</span>");
    return badges.join("");
  }

  function shortlistRank(job) {
    var sr = job && job.shortlist_rank && typeof job.shortlist_rank === "object" ? job.shortlist_rank : {};
    var meta = job && job.source_metadata && typeof job.source_metadata === "object" ? job.source_metadata : {};
    var rank = sr.priority_rank != null ? sr.priority_rank : (job.priority_rank != null ? job.priority_rank : meta.priority_rank);
    rank = Number(rank);
    return {
      rank: isFinite(rank) ? rank : null,
      bucket: String(sr.rank_bucket || job.rank_bucket || meta.rank_bucket || "").trim(),
      badge: String(sr.rank_badge || job.rank_badge || meta.rank_badge || "").trim()
    };
  }

  function rankBucketOrder(bucket) {
    var map = {
      "apply first": 0,
      "strong volume": 1,
      "frontier stretch": 2,
      "berlin backups": 3
    };
    var key = norm(bucket);
    return map[key] == null ? 9 : map[key];
  }

  function priorityRankValue(job) {
    var rank = shortlistRank(job);
    return rank.rank == null ? 999999 : rank.rank;
  }

  function sourceState(job) {
    var raw = job && job.source_state && typeof job.source_state === "object" ? job.source_state : {};
    var fresh = freshnessState(job || {});
    return {
      state: raw.state || fresh.state || "",
      stale: raw.stale === true || fresh.state === "stale",
      age_days: raw.age_days == null ? null : raw.age_days,
      last_seen_at: raw.last_seen_at || (job && job.last_seen_at) || "",
      verified_at: raw.verified_at || "",
      full_jd_status: raw.full_jd_status || ((job && job.jd_text) ? "reviewed" : "missing"),
      requires_full_jd_review: raw.requires_full_jd_review === true,
      revalidation_mode: raw.revalidation_mode || "manual_review_only"
    };
  }

  function fullJdBlocked(job) {
    return sourceState(job || state.detailJob || {}).requires_full_jd_review;
  }

  function assertFullJdReady() {
    if (!fullJdBlocked(state.detailJob)) return true;
    setFeedback($("#jb-gen-feedback"), "error", t("jobs.source.full_jd_blocked", "Paste and revalidate the reviewed full job description before tailoring this shortlist lead."));
    var input = $("#jb-source-full-jd");
    if (input) input.focus();
    return false;
  }

  function norm(value) {
    return String(value == null ? "" : value).trim().toLowerCase();
  }

  function containsValue(value, needle) {
    needle = norm(needle);
    if (!needle) return true;
    return norm(value).indexOf(needle) !== -1;
  }

  function containsAnyValue(value, needle) {
    var hay = norm(value);
    var parts = String(needle == null ? "" : needle)
      .split(/[,\n]/)
      .map(function (part) { return norm(part); })
      .filter(Boolean);
    if (!parts.length) return true;
    return parts.some(function (part) { return hay.indexOf(part) !== -1; });
  }

  function normTokens(value) {
    return norm(value).replace(/[^a-z0-9+]+/g, " ").replace(/\s+/g, " ").trim().split(" ").filter(Boolean);
  }

  function tokenPhraseMatch(needle, haystack) {
    var needleTokens = normTokens(needle);
    var hayTokens = normTokens(haystack);
    if (!needleTokens.length) return true;
    if (needleTokens.length > hayTokens.length) return false;
    var width = needleTokens.length;
    for (var i = 0; i <= hayTokens.length - width; i += 1) {
      var matched = true;
      for (var j = 0; j < width; j += 1) {
        if (hayTokens[i + j] !== needleTokens[j]) {
          matched = false;
          break;
        }
      }
      if (matched) return true;
    }
    return false;
  }

  function anyTokenPhraseMatch(needles, haystack) {
    return (needles || []).some(function (needle) {
      return norm(needle) && tokenPhraseMatch(needle, haystack);
    });
  }

  function cityLocationLabel(location) {
    return normTokens(String(location || "").split(",", 1)[0]).join(" ");
  }

  function strictCityMatch(cities, job) {
    var parts = String(cities == null ? "" : cities)
      .split(/[,\n]/)
      .map(function (part) { return normTokens(part).join(" "); })
      .filter(Boolean);
    if (!parts.length) return true;
    var sourceCity = normTokens(job.source_city).join(" ");
    var locationCity = cityLocationLabel(job.source_location);
    return parts.some(function (city) {
      return city === sourceCity || city === locationCity;
    });
  }

  function strictFamilyJobMatch(job, family) {
    var raw = String(family == null ? "" : family).trim();
    var normalized = normTokens(raw).join(" ");
    if (!normalized) return true;
    var hay = [job.title, job.job_family, job.field].filter(Boolean).join(" ");
    if (tokenPhraseMatch(normalized, hay)) return true;
    var terms = normTokens(raw).filter(function (part) { return part.length >= 3; });
    if (!terms.length) return true;
    if (raw.indexOf("/") >= 0 || raw.indexOf("|") >= 0) {
      var roleTerms = ["ai", "analyst", "analytics", "engineer", "learning", "machine", "platform", "scientist"];
      return anyTokenPhraseMatch(terms, hay) && anyTokenPhraseMatch(roleTerms, hay);
    }
    return terms.every(function (term) { return tokenPhraseMatch(term, hay); });
  }

  function safeMetaValue(value) {
    var text = String(value == null ? "" : value).trim();
    if (!text) return "";
    if (/(li_at|cookie|session|token|authorization|bearer|password|secret|csrf|auth)/i.test(text)) return "";
    return text;
  }

  function verificationLabel(value) {
    var raw = String(value == null ? "" : value).trim();
    if (!raw) return "";
    return t("jobs.verification." + raw, raw);
  }

  function freshnessState(job) {
    var verification = norm(job.verification_status);
    var expires = job.expires_at ? new Date(String(job.expires_at).slice(0, 10) + "T23:59:59Z") : null;
    var expired = expires && !isNaN(expires.getTime()) && expires.getTime() < Date.now();
    var lastSeen = job.last_seen_at ? new Date(job.last_seen_at) : null;
    var recentlySeen = lastSeen && !isNaN(lastSeen.getTime()) && (Date.now() - lastSeen.getTime()) <= FRESHNESS_WINDOW_MS;
    var stale = verification === "stale" || !!expired;
    if (stale) return { state: "stale", label: t("jobs.freshness.stale", "stale") };
    if (recentlySeen) return { state: "fresh", label: t("jobs.freshness.fresh", "fresh") };
    return { state: "", label: "" };
  }

  function experienceFit(job) {
    var fit = job && job.experience_fit && typeof job.experience_fit === "object" ? job.experience_fit : {};
    var required = Number(fit.required_years != null ? fit.required_years : fit.required_years_min);
    var candidate = Number(fit.candidate_years != null ? fit.candidate_years : fit.candidate_years_min);
    return {
      required: isFinite(required) ? required : null,
      candidate: isFinite(candidate) ? candidate : null,
      fit: String(fit.fit || "").trim(),
      uncertain: !!fit.uncertain
    };
  }

  function yearsLabel(job) {
    var years = experienceFit(job);
    if (years.required == null && years.candidate == null) return "";
    var required = years.required == null ? "?" : String(years.required).replace(/\.0$/, "");
    var candidate = years.candidate == null ? "?" : String(years.candidate).replace(/\.0$/, "");
    var fallback = window.OnePagerI18n && window.OnePagerI18n.getLang && window.OnePagerI18n.getLang() === "de" ? "Jahre" : "years";
    return candidate + "/" + required + " " + t("jobs.years", fallback);
  }

  function jsonFetch(url, opts) {
    return fetch(url, opts).then(function (r) {
      return r.json().then(
        function (body) { return { status: r.status, body: body }; },
        function () { return { status: r.status, body: {} }; }
      );
    });
  }

  // ── Credit badge (shared idiom with the other pages) ──────────────────────
  function loadCredit() {
    fetch("/api/extract/status")
      .then(function (r) { return r.json(); })
      .then(function (s) {
        var el = $("#jb-credit");
        if (!el) return;
        if (window.OnePagerCredit) window.OnePagerCredit.render(el, s);
      })
      .catch(function () {});
  }

  // ── Deadline formatting (pure, client-side) ───────────────────────────────
  function describeDeadline(deadline) {
    var rolling = { text: t("jobs.deadline.rolling", "Rolling"), cls: "" };
    if (!deadline) return rolling;
    var d = new Date(deadline + "T00:00:00");
    if (isNaN(d.getTime())) return rolling;
    var now = new Date();
    now.setHours(0, 0, 0, 0);
    var days = Math.round((d - now) / 86400000);
    // Locale-aware date; the CV Copilot language drives the month abbreviation.
    var locale = (window.OnePagerI18n && window.OnePagerI18n.getLang()) || undefined;
    var label = d.toLocaleDateString(locale, { day: "numeric", month: "short" });
    if (days < 0) return { text: t("jobs.deadline.closed", "Closed"), cls: "is-past" };
    if (days <= 14) return { text: t("jobs.deadline.due", "Due") + " " + label, cls: "is-soon" };
    return { text: label, cls: "" };
  }

  function deadlineSortKey(job) {
    // Rolling (no deadline) sorts last; past deadlines sort by their date too.
    if (!job.deadline) return Infinity;
    var t = new Date(job.deadline + "T00:00:00").getTime();
    return isNaN(t) ? Infinity : t;
  }

  // ── Sort + filter + search (all client-side) ──────────────────────────────
  function sortJobs(list) {
    var arr = list.slice();
    if (state.sort === "deadline") {
      arr.sort(function (a, b) { return deadlineSortKey(a) - deadlineSortKey(b); });
    } else if (state.sort === "confidence") {
      arr.sort(function (a, b) {
        return (b.confidence == null ? -1 : b.confidence) - (a.confidence == null ? -1 : a.confidence);
      });
    } else if (state.sort === "shortlist_rank") {
      arr.sort(function (a, b) {
        var ba = rankBucketOrder(shortlistRank(a).bucket);
        var bb = rankBucketOrder(shortlistRank(b).bucket);
        if (ba !== bb) return ba - bb;
        return priorityRankValue(a) - priorityRankValue(b);
      });
    } else if (state.sort === "created") {
      arr.sort(function (a, b) { return String(b.created_at || "").localeCompare(String(a.created_at || "")); });
    } else {
      // smart: starred first → has-cover first → status order → deadline soonest.
      arr.sort(function (a, b) {
        if (isJobStarred(a) !== isJobStarred(b)) return isJobStarred(b) ? 1 : -1;
        if (!!b.has_cover !== !!a.has_cover) return (b.has_cover ? 1 : 0) - (a.has_cover ? 1 : 0);
        var sa = STATUS_ORDER[a.status] == null ? 9 : STATUS_ORDER[a.status];
        var sb = STATUS_ORDER[b.status] == null ? 9 : STATUS_ORDER[b.status];
        if (sa !== sb) return sa - sb;
        return deadlineSortKey(a) - deadlineSortKey(b);
      });
    }
    return arr;
  }

  function matches(job) {
    if (state.filter === "has_cover" && !job.has_cover) return false;
    if (state.filter !== "all" && state.filter !== "has_cover" && job.status !== state.filter) return false;
    var q = state.query.trim().toLowerCase();
    if (q) {
      var strict = norm(state.savedFilters && state.savedFilters.searchMode) === "strict";
      if (strict) {
        if (!tokenPhraseMatch(q, job.title)) return false;
      } else {
        var hay = [
          job.title, job.company, job.field, job.job_family, job.source_country, job.source_city, job.source_region,
          job.source_location, job.source_provider, job.verification_status,
          job.remote_mode, job.seniority, job.salary_text,
        ].join(" ").toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
    }
    var f = state.savedFilters || {};
    var strictFilters = norm(f.searchMode) === "strict";
    if (f.family) {
      if (strictFilters) {
        if (!strictFamilyJobMatch(job, f.family)) return false;
      } else if (!containsValue(job.job_family || job.field, f.family)) return false;
    }
    if (!containsValue(job.source_country, f.country)) return false;
    if (strictFilters) {
      if (!strictCityMatch(f.city, job)) return false;
    } else if (!containsAnyValue([job.source_city, job.source_location].filter(Boolean).join(" "), f.city)) return false;
    if (!containsValue([job.source_region, job.source_location].filter(Boolean).join(" "), f.region)) return false;
    if (f.provider && norm(job.source_provider) !== norm(f.provider)) return false;
    if (f.verification && norm(job.verification_status) !== norm(f.verification)) return false;
    if (f.remote && norm(job.remote_mode) !== norm(f.remote)) return false;
    if (!containsValue(job.seniority, f.seniority)) return false;
    if (f.freshness) {
      var fresh = freshnessState(job).state;
      if (f.freshness === "fresh" && fresh !== "fresh") return false;
      if (f.freshness === "stale" && fresh !== "stale") return false;
    }
    if (f.rankBucket && norm(shortlistRank(job).bucket) !== norm(f.rankBucket)) return false;
    if (f.minYears) {
      var minYears = Number(f.minYears);
      var requiredYears = experienceFit(job).required;
      if (isFinite(minYears) && requiredYears != null && requiredYears < minYears) return false;
    }
    return true;
  }

  // ── Card rendering ────────────────────────────────────────────────────────
  function cardHtml(job) {
    var status = job.status || "saved";
    var dl = describeDeadline(job.deadline);
    var conf = (job.confidence == null) ? null : Math.max(0, Math.min(100, job.confidence));
    var starred = isJobStarred(job);

    var pills = [];
    if (job.field) {
      pills.push('<span class="jb-pill jb-pill--field">' + esc(job.field) + "</span>");
    }
    if (job.job_family) {
      pills.push('<span class="jb-pill">' + esc(job.job_family) + "</span>");
    }
    pills.push(
      '<span class="jb-pill jb-pill--deadline ' + dl.cls + '">' +
      '<span data-icon="target" data-icon-size="11"></span>' + esc(dl.text) + "</span>"
    );
    var years = yearsLabel(job);
    if (years) {
      pills.push('<span class="jb-pill jb-pill--years">' + esc(years) + "</span>");
    }

    var confHtml = "";
    if (conf != null) {
      confHtml =
        '<div class="jb-conf" title="Fit confidence ' + conf + '%">' +
        '<div class="jb-conf-track"><div class="jb-conf-fill" style="width:' + conf + '%"></div></div>' +
        '<span class="jb-conf-val">' + conf + "%</span></div>";
    }

    var why = job.why_fit
      ? '<p class="jb-why"><span class="jb-why-label">' + esc(t("jobs.note_label", "Your note")) + "</span>" + esc(job.why_fit) + "</p>"
      : "";
    var matchReasons = Array.isArray(job.match_reasons) ? job.match_reasons.filter(Boolean) : [];
    var matchHtml = matchReasons.length
      ? '<p class="jb-match-reasons"><span class="jb-why-label">' +
        esc(job.broader_match ? t("jobs.match.broader", "Broader match") : t("jobs.match.criteria", "Criteria match")) +
        "</span>" + esc(matchReasons.join(" · ")) + "</p>"
      : "";

    var coverBadge = job.has_cover
      ? '<span class="jb-badge jb-badge-has-cover"><span data-icon="check" data-icon-size="11"></span>' + esc(t("jobs.badge.cover", "Cover")) + "</span>"
      : "";
    var provenance = provenanceBadges(job);
    var starBusy = !!state.starReqs[job.id];

    // Split button: Open (has a cover) OR Generate (kicks off the guarded chain
    // via the detail pane). data-act drives the click handler.
    var primary = job.has_cover
      ? '<button type="button" class="cl-btn jb-card-btn jb-card-open" data-act="open">' +
        '<span data-icon="external-link" data-icon-size="13"></span><span>' + esc(t("jobs.card.open", "Open")) + "</span></button>"
      : '<button type="button" class="cl-btn cl-btn-primary jb-card-btn jb-card-gen" data-act="generate">' +
        '<span data-icon="sparkles" data-icon-size="13"></span><span>' + esc(t("jobs.card.generate", "Generate")) + "</span></button>";

    return (
      '<article class="jb-card' + (job.has_cover ? " has-cover" : "") +
        (starred ? " is-starred" : "") +
        (job.id === state.jobId ? " jb-active" : "") + '" data-id="' + esc(job.id) +
        '" data-starred="' + (starred ? "true" : "false") +
        '" data-rank-bucket="' + esc(shortlistRank(job).bucket) +
        '" data-priority-rank="' + esc(priorityRankValue(job)) +
        '" data-freshness-state="' + esc(sourceState(job).state) +
        '" data-full-jd-status="' + esc(sourceState(job).full_jd_status) + '">' +
        '<div class="jb-card-head">' +
          '<div class="jb-card-titles">' +
            '<h3 class="jb-card-title">' + esc(job.title || t("jobs.untitled", "Untitled job")) + "</h3>" +
            '<p class="jb-card-company">' + esc(job.company || t("jobs.company_missing", "Company not detected")) + "</p>" +
          "</div>" +
          '<div class="jb-card-head-actions">' +
            '<button type="button" class="jb-card-star" data-act="star" aria-pressed="' + (starred ? "true" : "false") +
              '" aria-label="' + esc(starred ? t("jobs.star.unstar", "Unstar job") : t("jobs.star.star", "Star job")) + '"' +
              (starBusy ? ' disabled aria-busy="true"' : "") + ">" +
              '<span data-icon="star" data-icon-size="13"></span>' +
            "</button>" +
            '<span class="jb-badge jb-badge-' + status + '">' + esc(statusLabel(status)) + "</span>" +
          "</div>" +
        "</div>" +
        '<div class="jb-card-meta">' + pills.join("") + coverBadge + provenance + "</div>" +
        confHtml +
        why +
        matchHtml +
        '<div class="jb-card-foot">' +
          '<button type="button" class="cl-btn jb-card-btn jb-card-detail" data-act="detail" aria-label="' +
            esc(t("jobs.card.open_detail", "Open job details") + ": " + (job.title || t("jobs.untitled", "Untitled job"))) + '">' +
            '<span data-icon="briefcase" data-icon-size="13"></span><span>' + esc(t("jobs.card.details", "Details")) + "</span></button>" +
          primary +
        "</div>" +
      "</article>"
    );
  }

  function isJobStarred(job) {
    return !!(job && (job.starred === true || (job.source_metadata && job.source_metadata.starred === true)));
  }

  function renderGrid() {
    var grid = $("#jb-grid");
    // First load: show skeleton cards rather than a premature "no jobs" message.
    if (state.loading) {
      grid.innerHTML =
        '<div class="jb-grid-skeleton" aria-hidden="true">' +
        '<span class="jb-skel-card"></span><span class="jb-skel-card"></span>' +
        '<span class="jb-skel-card"></span><span class="jb-skel-card"></span>' +
        "</div>";
      return;
    }
    var visible = sortJobs(state.jobs.filter(matches));
    if (state.loadError) {
      grid.innerHTML = '<p class="muted jb-empty jb-error">' + esc(state.loadError) + "</p>";
      return;
    }
    if (!state.jobs.length) {
      grid.innerHTML = '<p class="muted jb-empty">' +
        esc(t("jobs.empty.none", "No saved jobs yet. Use “Save a job” above to add one.")) + "</p>";
      return;
    }
    if (!visible.length) {
      grid.innerHTML = '<p class="muted jb-empty">' +
        esc(t("jobs.empty.filter", "No jobs match this filter.")) + "</p>";
      return;
    }
    grid.innerHTML = visible.map(cardHtml).join("");
    if (window.renderIcons) window.renderIcons(grid);
  }

  // ── Load the catalog ──────────────────────────────────────────────────────
  function loadJobs() {
    return jsonFetch("/api/jobs")
      .then(function (res) {
        state.loadError = null;
        state.jobs = (res.body && res.body.jobs) || [];
      })
      .catch(function () {
        state.loadError = t("jobs.error.load", "Could not load saved jobs.");
      })
      .then(function () {
        state.loading = false;
        renderGrid();
      });
  }

  // ── Add a job ─────────────────────────────────────────────────────────────
  function addJob() {
    var jd = $("#jb-jd").value;
    var url = $("#jb-url").value;
    if (!jd.trim() && !url.trim()) {
      setFeedback($("#jb-feedback"), "error", t("jobs.add.error.no_input", "Paste a job description or give a public non-LinkedIn URL."));
      return;
    }
    var btn = $("#jb-add");
    btn.disabled = true;
    btn.querySelector(".jb-add-label").textContent = t("jobs.add.saving", "Saving…");
    setFeedback($("#jb-feedback"), "info", t("jobs.add.parsing", "Parsing and saving the job…"));

    var confRaw = $("#jb-confidence").value;
    jsonFetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jd_text: jd,
        jd_url: url,
        title: $("#jb-title").value,
        company: $("#jb-company").value,
        field: $("#jb-field").value,
        deadline: $("#jb-deadline").value,
        confidence: confRaw === "" ? null : Number(confRaw),
        why_fit: $("#jb-whyfit").value
      })
    })
      .then(function (res) {
        if (res.status === 201) {
          ["#jb-jd", "#jb-url", "#jb-title", "#jb-company", "#jb-field", "#jb-deadline", "#jb-confidence", "#jb-whyfit"]
            .forEach(function (s) { $(s).value = ""; });
          setFeedback($("#jb-feedback"), "ok", t("jobs.add.saved", "Saved."));
          var addDetails = $("#jb-add-details");
          if (addDetails) addDetails.open = false;
          return loadJobs().then(function () { openJob(res.body.id); });
        }
        if (res.body && res.body.code === "ssrf_blocked") {
          setFeedback($("#jb-feedback"), "error",
            "<strong>" + esc(t("jobs.add.error.url_blocked_title", "That URL isn't allowed.")) + "</strong> " +
            esc(t("jobs.add.error.url_blocked_body", "For safety we don't fetch internal or private addresses — paste the text instead.")));
        } else if (res.body && res.body.code === "linkedin_manual_required") {
          setFeedback($("#jb-feedback"), "error",
            "<strong>" + esc(t("jobs.add.error.linkedin_title", "Paste the LinkedIn job text.")) + "</strong> " +
            esc(t("jobs.add.error.linkedin_body", "This app does not blind-fetch LinkedIn pages; approved adapters are disabled unless configured.")));
        } else {
          setFeedback($("#jb-feedback"), "error",
            esc((res.body && res.body.error) || ("Couldn't save the job (" + res.status + ").")));
        }
      })
      .catch(function () { setFeedback($("#jb-feedback"), "error", t("jobs.add.error.network", "Network error — please try again.")); })
      .finally(function () {
        btn.disabled = false;
        btn.querySelector(".jb-add-label").textContent = t("jobs.add.button", "Save job");
      });
  }

  function readJobSourceDefaults() {
    try {
      var raw = safeLocalGet(JOB_SOURCE_DEFAULTS_KEY);
      if (!raw) return {};
      var defaults = JSON.parse(raw);
      if (!defaults || typeof defaults !== "object" || !Object.keys(defaults).length) return {};
      var normalized = normalizeJobProfile(defaults);
      var compact = compactJobProfile(normalized);
      var compactJson = JSON.stringify(compact);
      if (compactJson !== raw) safeLocalSet(JOB_SOURCE_DEFAULTS_KEY, compactJson);
      return normalized;
    } catch (_) {
      return {};
    }
  }

  function importFilters() {
    var defaults = readJobSourceDefaults();
    var title = cleanFilterValue("#jb-import-title", 300);
    var defaultTitles = Array.isArray(defaults.titles) ? defaults.titles.filter(Boolean) : [];
    var defaultVisibleTitle = defaults.title || defaultTitles[0] || "";
    var titles = [];
    if (title) {
      titles.push(title);
      if (title === defaultVisibleTitle) {
        defaultTitles.forEach(function (item) {
          if (titles.indexOf(item) === -1) titles.push(item);
        });
      }
    }
    return {
      source: enumFilterValue("#jb-import-source", JOB_SOURCES, ""),
      title: title,
      titles: titles,
      family: cleanFilterValue("#jb-filter-family", 160),
      search_mode: enumFilterValue("#jb-search-mode", JOB_SEARCH_MODES, "flexible"),
      country: cleanFilterValue("#jb-import-country", 80),
      city: cleanFilterValue("#jb-filter-city", 200),
      region: cleanFilterValue("#jb-import-region", 120),
      remote_mode: enumFilterValue("#jb-import-remote", JOB_REMOTE_MODES, ""),
      seniority: cleanFilterValue("#jb-import-seniority", 120),
      freshness: enumFilterValue("#jb-import-freshness", JOB_FRESHNESS, ""),
      min_years: normalizeMinYears(cleanFilterValue("#jb-filter-min-years", 8)),
      rank_bucket: cleanFilterValue("#jb-filter-rank-bucket", 80),
      ranking: defaults.ranking && typeof defaults.ranking === "object" ? defaults.ranking : {}
    };
  }

  function applyJobSourceDefaults() {
    if (state.jobDefaultsApplied) return;
    state.jobDefaultsApplied = true;
    var defaults = readJobSourceDefaults();
    if (!Object.keys(defaults).length) {
      if ($("#jb-filter-provider")) $("#jb-filter-provider").value = "";
      syncSavedFilters();
      return;
    }
    try {
      if ($("#jb-import-source")) {
        $("#jb-import-source").value = defaults.source_explicit ? defaults.source : "manual";
      }
      if ($("#jb-filter-provider")) {
        $("#jb-filter-provider").value = defaults.source_explicit && defaults.source !== "all" ? (defaults.source || "") : "";
      }
      if ($("#jb-import-title")) $("#jb-import-title").value = defaults.title || (Array.isArray(defaults.titles) ? defaults.titles[0] || "" : "");
      if ($("#jb-search")) $("#jb-search").value = defaults.title || (Array.isArray(defaults.titles) ? defaults.titles[0] || "" : "");
      if ($("#jb-search-mode")) $("#jb-search-mode").value = defaults.search_mode || "flexible";
      if ($("#jb-filter-family")) $("#jb-filter-family").value = defaults.family || "";
      if ($("#jb-import-country")) $("#jb-import-country").value = defaults.country || "";
      if ($("#jb-filter-country")) $("#jb-filter-country").value = defaults.country || "";
      if ($("#jb-import-region")) $("#jb-import-region").value = defaults.region || "";
      if ($("#jb-filter-region")) $("#jb-filter-region").value = defaults.region || "";
      if ($("#jb-filter-city")) $("#jb-filter-city").value = Array.isArray(defaults.cities) ? defaults.cities.join(", ") : "";
      if ($("#jb-import-remote")) $("#jb-import-remote").value = defaults.remote_mode || "";
      if ($("#jb-filter-remote")) $("#jb-filter-remote").value = defaults.remote_mode || "";
      if ($("#jb-import-seniority")) $("#jb-import-seniority").value = defaults.seniority || "";
      if ($("#jb-filter-seniority")) $("#jb-filter-seniority").value = defaults.seniority || "";
      if ($("#jb-import-freshness")) $("#jb-import-freshness").value = defaults.freshness || "";
      if ($("#jb-filter-freshness")) $("#jb-filter-freshness").value = defaults.freshness || "";
      if ($("#jb-filter-min-years")) $("#jb-filter-min-years").value = defaults.min_years || "";
      state.query = $("#jb-search") ? cleanProfileText($("#jb-search").value, 300) : state.query;
      syncSavedFilters();
    } catch (_) {}
  }

  function renderImportPreview() {
    var list = $("#jb-import-preview-list");
    if (!list) return;
    if (!state.importResults.length) {
      list.innerHTML = "";
      $("#jb-import-save-selected").disabled = true;
      $("#jb-import-mark-stale").disabled = true;
      return;
    }
    list.innerHTML = state.importResults.map(function (r) {
      var checked = r.verification_status === "stale" ? "" : " checked";
      var matchReasons = Array.isArray(r.match_reasons) ? r.match_reasons.filter(Boolean) : [];
      var matchHtml = matchReasons.length
        ? '<p class="jb-match-reasons jb-match-reasons--import"><span class="jb-why-label">' +
          esc(r.broader_match ? t("jobs.match.broader", "Broader match") : t("jobs.match.criteria", "Criteria match")) +
          "</span>" + esc(matchReasons.join(" · ")) + "</p>"
        : "";
      return (
        '<label class="jb-import-row" data-result-id="' + esc(r.id) +
          '" data-rank-bucket="' + esc(r.rank_bucket || (r.source_metadata && r.source_metadata.rank_bucket) || "") +
          '" data-priority-rank="' + esc(r.priority_rank || (r.source_metadata && r.source_metadata.priority_rank) || "") +
          '" data-full-jd-status="' + esc((r.source_state && r.source_state.full_jd_status) || (r.source_metadata && r.source_metadata.full_jd_status) || "") + '">' +
          '<input type="checkbox" class="jb-import-check" value="' + esc(r.id) + '"' + checked + ' />' +
          '<span>' +
            '<strong class="jb-import-row-title">' + esc(r.title || "Untitled job") + "</strong>" +
            '<span class="jb-import-row-meta">' + esc([r.company, r.source_location, r.remote_mode, r.seniority].filter(Boolean).join(" · ")) + "</span>" +
            matchHtml +
          "</span>" +
          '<span class="jb-import-row-badges">' +
            '<span class="jb-pill">' + esc(r.source_provider || "manual") + "</span>" +
            '<span class="jb-pill jb-pill--verify">' + esc(verificationLabel(r.verification_status || "user_provided")) + "</span>" +
            (r.rank_bucket ? '<span class="jb-pill jb-rank-bucket">' + esc(r.rank_bucket) + "</span>" : "") +
            (r.priority_rank ? '<span class="jb-pill jb-rank-order">#' + esc(r.priority_rank) + "</span>" : "") +
          "</span>" +
        "</label>"
      );
    }).join("");
    $("#jb-import-save-selected").disabled = false;
    $("#jb-import-mark-stale").disabled = false;
  }

  function selectedImportResultIds() {
    return Array.from(document.querySelectorAll(".jb-import-check:checked")).map(function (node) { return node.value; });
  }

  function setImportMutationDisabled(disabled) {
    var save = $("#jb-import-save-selected");
    var stale = $("#jb-import-mark-stale");
    if (save) save.disabled = disabled;
    if (stale) stale.disabled = disabled;
  }

  function resetImportPreviewState() {
    state.importRunId = null;
    state.importResults = [];
    state.importMutationReq++;
    setImportMutationDisabled(true);
    renderImportPreview();
  }

  function previewImport() {
    var jd = $("#jb-import-jd").value;
    if (!jd.trim()) {
      setFeedback($("#jb-import-feedback"), "error", esc(t("jobs.import.feedback.paste_first", "Paste posting text before previewing.")));
      return;
    }
    var btn = $("#jb-import-preview");
    var importToken = ++state.importReq;
    var token = ++state.importPreviewReq;
    btn.disabled = true;
    resetImportPreviewState();
    setFeedback($("#jb-import-feedback"), "info", esc(t("jobs.import.feedback.previewing", "Preparing import preview...")));
    var filters = importFilters();
    jsonFetch("/api/jobs/import-runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: !filters.source || filters.source === "all" ? "manual" : filters.source,
        query: filters.title,
        location: [filters.region, filters.country].filter(Boolean).join(", "),
        filters: filters,
        postings: [{
          jd_text: jd,
          title: filters.title,
          country: filters.country,
          region: filters.region,
          remote_mode: filters.remote_mode,
          seniority: filters.seniority
        }]
    })
    }).then(function (res) {
      if (importToken !== state.importReq || token !== state.importPreviewReq) return;
      if (res.status !== 202) {
        setFeedback($("#jb-import-feedback"), "error", esc((res.body && res.body.error) || t("jobs.import.feedback.preview_failed", "Import preview failed.")));
        return;
      }
      state.importRunId = res.body.id;
      state.importResults = res.body.results || [];
      setFeedback($("#jb-import-feedback"), "ok", esc(t("jobs.import.feedback.preview_ready", "Preview ready. Rows are unverified; review before saving.")));
      renderImportPreview();
    }).catch(function () {
      if (importToken !== state.importReq || token !== state.importPreviewReq) return;
      setFeedback($("#jb-import-feedback"), "error", esc(t("jobs.import.feedback.network_error", "Network error - please try again.")));
    }).finally(function () {
      if (token === state.importPreviewReq) btn.disabled = false;
    });
  }

  function previewLocalSearch() {
    var filters = importFilters();
    if (!filters.source || filters.source === "manual") filters.source = "linkedin";
    var btn = $("#jb-local-search");
    var importToken = ++state.importReq;
    var token = ++state.localSearchReq;
    if (btn) btn.disabled = true;
    resetImportPreviewState();
    setFeedback($("#jb-import-feedback"), "info", esc(t("jobs.import.feedback.searching", "Searching local source…")));
    jsonFetch("/api/jobs/local-search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: filters.title || (Array.isArray(filters.titles) ? filters.titles[0] || "" : ""),
        limit: 25,
        filters: filters
      })
    }).then(function (searchRes) {
      if (importToken !== state.importReq || token !== state.localSearchReq) return null;
      if (searchRes.status !== 200) {
        setFeedback($("#jb-import-feedback"), "error", esc((searchRes.body && searchRes.body.error) || t("jobs.import.feedback.local_search_failed", "Local search failed.")));
        return null;
      }
      var results = (searchRes.body && searchRes.body.results) || [];
      if (!results.length) {
        state.importRunId = null;
        state.importResults = [];
        renderImportPreview();
        setFeedback($("#jb-import-feedback"), "info", esc(t("jobs.import.feedback.no_results", "No import rows returned.")));
        return null;
      }
      setFeedback($("#jb-import-feedback"), "info", esc(t("jobs.import.feedback.previewing", "Preparing import preview...")));
      return jsonFetch("/api/jobs/import-runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: "manual",
          query: filters.title,
          location: [filters.region, filters.country].filter(Boolean).join(", "),
          filters: filters,
          postings: results
        })
      });
    }).then(function (runRes) {
      if (importToken !== state.importReq || token !== state.localSearchReq) return;
      if (!runRes) return;
      if (runRes.status !== 202) {
        setFeedback($("#jb-import-feedback"), "error", esc((runRes.body && runRes.body.error) || t("jobs.import.feedback.local_preview_failed", "Local preview run failed.")));
        return;
      }
      state.importRunId = runRes.body.id;
      state.importResults = runRes.body.results || [];
      setFeedback($("#jb-import-feedback"), "ok", esc(t("jobs.import.feedback.preview_ready", "Preview ready. Rows are unverified; review before saving.")));
      renderImportPreview();
    }).catch(function () {
      if (importToken !== state.importReq || token !== state.localSearchReq) return;
      setFeedback($("#jb-import-feedback"), "error", esc(t("jobs.import.feedback.network_error", "Network error - please try again.")));
    }).finally(function () {
      if (token === state.localSearchReq && btn) btn.disabled = false;
    });
  }

  function saveSelectedImport() {
    var ids = selectedImportResultIds();
    if (!state.importRunId || !ids.length) {
      setFeedback($("#jb-import-feedback"), "error", esc(t("jobs.import.feedback.select_one", "Select at least one preview row.")));
      return;
    }
    var runId = state.importRunId;
    var importToken = state.importReq;
    var token = ++state.importMutationReq;
    setImportMutationDisabled(true);
    jsonFetch("/api/jobs/import-runs/" + encodeURIComponent(runId) + "/save-selected", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ result_ids: ids })
    }).then(function (res) {
      if (importToken !== state.importReq || runId !== state.importRunId || token !== state.importMutationReq) return;
      if (res.status !== 200) {
        setFeedback($("#jb-import-feedback"), "error", esc((res.body && res.body.error) || t("jobs.import.feedback.save_failed", "Save selected failed.")));
        return;
      }
      setFeedback($("#jb-import-feedback"), "ok", esc(t("jobs.import.feedback.saved_count", "Saved {n} posting(s).").replace("{n}", res.body.saved_count)));
      return loadJobs();
    }).catch(function () {
      if (importToken !== state.importReq || runId !== state.importRunId || token !== state.importMutationReq) return;
      setFeedback($("#jb-import-feedback"), "error", esc(t("jobs.import.feedback.network_error", "Network error - please try again.")));
    }).finally(function () {
      if (importToken === state.importReq && runId === state.importRunId && token === state.importMutationReq) setImportMutationDisabled(false);
    });
  }

  function markSelectedImportStale() {
    var ids = selectedImportResultIds();
    if (!state.importRunId || !ids.length) {
      setFeedback($("#jb-import-feedback"), "error", esc(t("jobs.import.feedback.select_one", "Select at least one preview row.")));
      return;
    }
    var runId = state.importRunId;
    var importToken = state.importReq;
    var token = ++state.importMutationReq;
    setImportMutationDisabled(true);
    jsonFetch("/api/jobs/import-runs/" + encodeURIComponent(runId) + "/mark-stale", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ result_ids: ids })
    }).then(function (res) {
      if (importToken !== state.importReq || runId !== state.importRunId || token !== state.importMutationReq) return;
      if (res.status !== 200) {
        setFeedback($("#jb-import-feedback"), "error", esc((res.body && res.body.error) || "Mark stale failed."));
        return;
      }
      state.importResults = res.body.results || state.importResults.map(function (r) {
        if (ids.indexOf(r.id) !== -1) r.verification_status = "stale";
        return r;
      });
      setFeedback($("#jb-import-feedback"), "ok", esc(t("jobs.import.feedback.stale", "Selected rows marked stale.")));
      renderImportPreview();
    }).catch(function () {
      if (importToken !== state.importReq || runId !== state.importRunId || token !== state.importMutationReq) return;
      setFeedback($("#jb-import-feedback"), "error", esc(t("jobs.import.feedback.network_error", "Network error - please try again.")));
    }).finally(function () {
      if (importToken === state.importReq && runId === state.importRunId && token === state.importMutationReq) setImportMutationDisabled(false);
    });
  }

  // ── Source-CV picker (for the one-click generate) ─────────────────────────
  function loadCvs() {
    return jsonFetch("/api/cvs").then(function (res) {
      var sel = $("#jb-source-cv");
      sel.innerHTML = "";
      var cvs = (res.body && res.body.cvs) || [];
      if (!cvs.length) {
        var empty = document.createElement("option");
        empty.value = "";
        empty.disabled = true;
        empty.selected = true;
        empty.textContent = t("cl.source.empty", "No CVs available");
        sel.appendChild(empty);
        var gen = $("#jb-generate");
        if (gen) gen.disabled = true;
        return;
      }
      cvs.forEach(function (cv) {
        var opt = document.createElement("option");
        opt.value = cv.id;
        opt.textContent = cv.name || t("lib.untitled.cv", "Untitled CV");
        sel.appendChild(opt);
      });
      var gen = $("#jb-generate");
      if (gen) gen.disabled = false;
    });
  }

  // ── Card actions (split button + detail) ──────────────────────────────────
  function onGridClick(e) {
    var btn = e.target.closest("[data-act]");
    var card = e.target.closest(".jb-card");
    if (!card || !btn) return;
    var id = card.dataset.id;
    var job = state.jobs.filter(function (j) { return j.id === id; })[0];
    var act = btn.dataset.act;
    if (act === "open") {
      if (job && job.cover_id) {
        window.location.href = "/cover-letters?id=" + encodeURIComponent(job.cover_id);
      } else {
        openJob(id); // fallback: surface the detail (cover should exist)
      }
      return;
    }
    if (act === "generate") {
      // Open the detail pane focused on the source-CV picker, then run the chain.
      openJob(id).then(function (opened) {
        if (!opened) return;
        $("#jb-source-cv").focus();
        generate();
      });
      return;
    }
    if (act === "star") {
      toggleStarredJob(job || { id: id });
      return;
    }
    if (act === "detail") openJob(id);
  }

  function onGridKey(e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    var btn = e.target.closest("[data-act]");
    if (!btn || !btn.closest(".jb-card")) return;
  }

  // ── Job detail ────────────────────────────────────────────────────────────
  function openJob(id) {
    state.navReq++;
    state.applyReviewInFlight = false;
    state.packetReview = null;
    clearPacketReview();
    disableJobActionControls();
    var token = ++state.detailReq;
    setFeedback($("#jb-gen-feedback"), null);
    return jsonFetch("/api/jobs/" + encodeURIComponent(id)).then(function (res) {
      if (token !== state.detailReq) return false;
      if (res.status !== 200) {
        resetDetail();
        setFeedback($("#jb-feedback"), "error", esc((res.body && res.body.error) || "Couldn't open that job."));
        return false;
      }
      state.jobId = id;
      state.packetReview = null;
      syncJobUrl(id);
      renderDetail(res.body);
      renderGrid(); // re-mark the active card
      resetJobActionControls();
      // On mobile (<=840px) the detail pane stacks BELOW the cards + forms, so
      // tapping "Details" otherwise scrolls nothing and looks like a no-op.
      // Bring the detail into view and move focus to its heading so the tap has
      // an obvious, announced result.
      if (window.matchMedia && window.matchMedia("(max-width: 840px)").matches) {
        var detail = document.getElementById("jb-detail");
        if (detail && detail.scrollIntoView) {
          detail.scrollIntoView({ behavior: "smooth", block: "start" });
          var heading = document.getElementById("jb-detail-title");
          if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus({ preventScroll: true }); }
        }
      }
      return true;
    }).catch(function () {
      if (token !== state.detailReq) return false;
      resetDetail();
      setFeedback($("#jb-feedback"), "error", "Couldn't open that job.");
      return false;
    });
  }

  function renderDetail(data) {
    var job = data.job || {};
    state.detailJob = job;
    state.detailPackets = data.packets || [];
    $("#jb-detail-empty").hidden = true;
    $("#jb-detail").hidden = false;

    $("#jb-detail-title").textContent = job.title || t("jobs.untitled", "Untitled job");
    $("#jb-detail-company").textContent = job.company || t("jobs.company_missing", "Company not detected");
    $("#jb-jd-text").textContent = job.jd_text || "";
    $("#jb-status-select").value = job.status || "saved";
    syncDetailStarButton(job);

    var dl = describeDeadline(job.deadline);
    var meta = [];
    if (job.field) meta.push('<span class="jb-pill jb-pill--field">' + esc(job.field) + "</span>");
    if (job.job_family) meta.push('<span class="jb-pill">' + esc(job.job_family) + "</span>");
    meta.push('<span class="jb-pill jb-pill--deadline ' + dl.cls + '">' +
      '<span data-icon="target" data-icon-size="11"></span>' + esc(dl.text) + "</span>");
    var years = yearsLabel(job);
    if (years) meta.push('<span class="jb-pill jb-pill--years">' + esc(years) + "</span>");
    if (job.confidence != null) {
      var c = Math.max(0, Math.min(100, job.confidence));
      meta.push(
        '<span class="jb-pill jb-pill--conf">' + esc(t("jobs.fit", "Fit")) + " " + c + "%</span>");
    }
    if (job.why_fit) {
      meta.push('<p class="jb-why jb-why--detail"><span class="jb-why-label">' +
        esc(t("jobs.note_label", "Your note")) + "</span>" + esc(job.why_fit) + "</p>");
    }
    $("#jb-detail-meta").innerHTML = meta.join("");
    syncDownstreamJobLinks(job);

    renderDetailProvenance(job);
    renderSourceStaleness(job);
    renderExperienceFitEvidence(job);
    renderPacketState(state.detailPackets, job);

    renderLinked("#jb-cvs", data.cvs || [], "cv", t("jobs.empty.cvs", "No tailored CVs yet."));
    renderLinked("#jb-letters", data.cover_letters || [], "cover_letter", t("jobs.empty.letters", "No cover letters yet."));
    clearPacketReview();
    if (window.renderIcons) window.renderIcons($("#jb-detail"));
  }

  function syncDownstreamJobLinks(job) {
    var id = job && job.id ? String(job.id) : "";
    var tailor = $("#jb-tailor-link");
    var cover = $("#jb-cover-link");
    if (tailor) {
      tailor.href = id ? "/tailor?job_id=" + encodeURIComponent(id) : "/tailor";
      tailor.setAttribute("aria-label", t("jobs.downstream.tailor_aria", "Run ATS check for this saved job"));
    }
    if (cover) {
      cover.href = id ? "/cover-letters?job_id=" + encodeURIComponent(id) : "/cover-letters";
      cover.setAttribute("aria-label", t("jobs.downstream.cover_aria", "Create a cover letter for this saved job"));
    }
  }

  function syncDetailStarButton(job) {
    var star = $("#jb-star");
    if (!star) return;
    var starred = isJobStarred(job);
    star.setAttribute("aria-pressed", starred ? "true" : "false");
    star.classList.toggle("is-active", starred);
    star.setAttribute("aria-label", starred ? t("jobs.star.unstar", "Unstar job") : t("jobs.star.star", "Star job"));
    star.setAttribute("data-tooltip", starred ? t("jobs.star.unstar", "Unstar job") : t("jobs.star.star", "Star job"));
    if (job && job.id && state.starReqs[job.id]) {
      star.disabled = true;
      star.setAttribute("aria-busy", "true");
    } else {
      star.disabled = false;
      star.removeAttribute("aria-busy");
    }
  }

  function renderDetailProvenance(job) {
    var detail = $("#jb-detail");
    if (!detail) return;
    var old = detail.querySelector(".jb-provenance-grid");
    if (old) old.remove();
    var sourceMeta = job.source_metadata && typeof job.source_metadata === "object" ? job.source_metadata : {};
    var displayProvider = job.source_provider || safeMetaValue(sourceMeta.site) || "";
    var fresh = freshnessState(job);
    var originParts = [sourceMeta.origin, sourceMeta.site, sourceMeta.crawl_policy].map(safeMetaValue).filter(Boolean);
    var rows = [
      [t("jobs.provenance.source", "Source"), displayProvider],
      [t("jobs.provenance.external_id", "External ID"), job.source_external_id],
      [t("jobs.provenance.verification", "Verification"), verificationLabel(job.verification_status) || fresh.label],
      [t("jobs.provenance.location", "Location"), [job.source_location, job.source_region, job.source_country].filter(Boolean).join(", ")],
      [t("jobs.provenance.remote", "Remote"), job.remote_mode],
      [t("jobs.provenance.seniority", "Seniority"), job.seniority],
      [t("jobs.provenance.salary", "Salary"), job.salary_text],
      [t("jobs.provenance.last_seen", "Last seen"), job.last_seen_at],
      [t("jobs.provenance.expires", "Expires"), job.expires_at],
      [t("jobs.provenance.origin", "Origin"), originParts.join(" · ")],
    ].filter(function (row) { return row[1] != null && String(row[1]).trim(); });
    if (!rows.length) return;
    var html = '<dl class="jb-provenance-grid" aria-label="' + esc(t("jobs.provenance.aria", "Job source provenance")) + '">' +
      rows.map(function (row) {
        return '<div class="jb-provenance-item"><dt>' + esc(row[0]) + '</dt><dd>' + esc(row[1]) + '</dd></div>';
      }).join("") +
      '</dl>';
    $("#jb-detail-meta").insertAdjacentHTML("afterend", html);
  }

  function renderSourceStaleness(job) {
    var panel = $("#jb-source-panel");
    if (!panel) return;
    var stateInfo = sourceState(job);
    var status = $("#jb-source-stale-state");
    var detail = $("#jb-source-revalidation-status");
    var full = $("#jb-full-jd-status");
    var gate = $("#jb-full-jd-gate");
    var input = $("#jb-source-full-jd");
    var label = stateInfo.stale
      ? t("jobs.source.state.stale", "Stale source")
      : (stateInfo.state === "fresh" ? t("jobs.source.state.fresh", "Fresh source") : t("jobs.source.state.review", "Source review"));
    if (stateInfo.requires_full_jd_review) label = t("jobs.source.state.full_jd_required", "Full JD review required");
    if (status) status.textContent = label;
    if (detail) {
      detail.innerHTML =
        '<span class="jb-source-last-seen">' + esc(t("jobs.source.last_seen", "Last seen")) + ": " + esc(stateInfo.last_seen_at || t("jobs.source.unknown", "unknown")) + "</span>" +
        ' · <span class="jb-source-verified-at">' + esc(t("jobs.source.verified", "Verified")) + ": " + esc(stateInfo.verified_at || t("jobs.source.not_recorded", "not recorded")) + "</span>" +
        " · " + esc(stateInfo.revalidation_mode);
    }
    if (full) {
      full.textContent = stateInfo.full_jd_status === "reviewed"
        ? t("jobs.source.full_jd_reviewed", "Full JD reviewed")
        : t("jobs.source.full_jd_missing", "Full JD missing");
      full.classList.toggle("jb-pill--verify", stateInfo.full_jd_status === "reviewed");
    }
    if (gate) gate.hidden = stateInfo.full_jd_status === "reviewed" && !stateInfo.stale;
    if (input && job && job.id) {
      input.value = state.fullJdDrafts[job.id] || "";
    }
    if (input && !input.value && job && job.jd_text && stateInfo.requires_full_jd_review) {
      input.placeholder = t("jobs.source.full_jd_placeholder_short", "Paste the reviewed full JD here before tailoring.");
    }
  }

  function renderExperienceFitEvidence(job) {
    var box = $("#jb-experience-fit");
    if (!box) return;
    var fit = job && job.experience_fit && typeof job.experience_fit === "object" ? job.experience_fit : {};
    var rank = fit.ranking_contribution && typeof fit.ranking_contribution === "object" ? fit.ranking_contribution : {};
    var years = experienceFit(job);
    if (years.required == null && years.candidate == null && !rank.reason) {
      box.innerHTML = "";
      return;
    }
    var required = years.required == null ? "?" : years.required;
    var candidate = years.candidate == null ? "?" : years.candidate;
    var missing = fit.missing_years == null ? "0" : fit.missing_years;
    var evidence = Array.isArray(rank.evidence) ? rank.evidence.slice(0, 3) : [];
    box.innerHTML =
      '<div class="jb-experience-row">' +
        '<span class="jb-experience-required-years">' + esc(t("jobs.experience.required", "Required")) + " " + esc(required) + "</span>" +
        '<span class="jb-experience-candidate-years">' + esc(t("jobs.experience.candidate", "Candidate")) + " " + esc(candidate) + "</span>" +
        '<span class="jb-experience-missing-years">' + esc(t("jobs.experience.missing", "Missing")) + " " + esc(missing) + "</span>" +
      "</div>" +
      '<div class="jb-ranking-contribution">' + esc(t("jobs.experience.rank_contribution", "Rank contribution")) + ": " +
        esc(rank.bucket || years.fit || "unknown") + " · " +
        esc(rank.weighted_score == null ? "0" : rank.weighted_score) +
        (rank.reason ? " · " + esc(rank.reason) : "") +
      "</div>" +
      (evidence.length ? '<ul class="jb-experience-evidence">' + evidence.map(function (item) { return "<li>" + esc(item) + "</li>"; }).join("") + "</ul>" : "");
  }

  function packetStateName(packet, job) {
    if (!packet) return "none";
    var status = String(packet.status || "").toLowerCase();
    var storedStatus = String(packet.stored_status || status || "").toLowerCase();
    if (packet.archived_at || status === "archived") return "archived";
    if (status === "stale") return "stale";
    if (status === "applied_external" || (job && job.status === "applied")) return "applied_external";
    if ((packet.packet_version || 1) > 1 && status === "packet_created") return "regenerated";
    if (status === "packet_created") return "packet_created";
    if (storedStatus === "review_applied") return "review_applied";
    if (storedStatus === "review_ready") return "review_prepared";
    return "review_prepared";
  }

  function packetUsableInLinkedRow(stateName) {
    return stateName === "packet_created" || stateName === "regenerated";
  }

  function packetChipLabel(stateName, packet) {
    var version = packet && packet.packet_version ? " v" + packet.packet_version : "";
    if (stateName === "archived") return t("jobs.packet.archived", "Archived packet") + version;
    if (stateName === "stale") return t("jobs.packet.stale", "Stale packet") + version;
    if (stateName === "applied_external") return t("jobs.packet.applied_external", "Applied externally") + version;
    if (stateName === "review_applied") return t("jobs.packet.incomplete", "Incomplete packet") + version;
    if (stateName === "regenerated") return t("jobs.packet.packet", "Packet") + version;
    if (stateName === "packet_created") return t("jobs.packet.packet", "Packet") + version;
    return stateName ? stateName.replace(/_/g, " ") + version : "";
  }

  function renderPacketState(packets, job) {
    var box = $("#jb-packet-state");
    if (!box) return;
    var rows = Array.isArray(packets) ? packets : [];
    if (!rows.length) {
      box.innerHTML = '<div class="jb-packet-row" data-packet-state="none"><span class="jb-packet-status">' +
        esc(t("jobs.packet.none", "No packet")) + "</span></div>";
      return;
    }
    box.innerHTML = rows.map(function (p) {
      var stateName = packetStateName(p, job);
      var score = p.score_snapshot && typeof p.score_snapshot.score === "number" ? p.score_snapshot.score + "%" : "";
      var links = p.document_links && typeof p.document_links === "object" ? Object.keys(p.document_links).join(", ") : "";
      var review = p.cover_letter_review_status === "confirmed" ? t("jobs.packet.cover_review_confirmed", "cover review confirmed") : "";
      var stateLabel = packetChipLabel(stateName, { packet_version: null }) || stateName.replace(/_/g, " ");
      var actions = stateName === "archived" ? "" :
        '<span class="jb-packet-actions">' +
          '<button type="button" class="jb-packet-action" data-packet-action="archive" data-packet-id="' + esc(p.id) + '">' + esc(t("jobs.packet.archive", "Archive")) + "</button>" +
          '<button type="button" class="jb-packet-action" data-packet-action="regenerate" data-packet-id="' + esc(p.id) + '">' + esc(t("jobs.packet.regenerate", "Regenerate")) + "</button>" +
        "</span>";
      return '<div class="jb-packet-row" data-packet-id="' + esc(p.id) + '" data-packet-state="' + esc(stateName) + '">' +
        '<span class="jb-packet-status">' + esc(stateLabel) + "</span>" +
        '<span class="jb-packet-version">v' + esc(p.packet_version || 1) + "</span>" +
        '<span class="jb-packet-lineage">' + esc([p.score_system_id, score, review, links].filter(Boolean).join(" · ")) + "</span>" +
        actions +
      "</div>";
    }).join("");
  }

  function clearPacketReview() {
    state.packetReview = null;
    var panel = $("#jb-review-panel");
    var summary = $("#jb-review-summary");
    var diff = $("#jb-review-diff");
    var lineage = $("#jb-review-lineage");
    var beforeAfter = $("#jb-review-before-after");
    var postAts = $("#jb-post-tailor-ats");
    var yearsFit = $("#jb-review-years-fit");
    var grounding = $("#jb-cover-grounding-status");
    var approve = $("#jb-approve-cover-review");
    var coverPanel = $("#jb-cover-review-panel");
    state.pendingPacket = null;
    if (panel) panel.hidden = true;
    if (summary) summary.textContent = "";
    if (diff) diff.textContent = "";
    if (lineage) lineage.innerHTML = "";
    if (beforeAfter) beforeAfter.innerHTML = "";
    if (postAts) postAts.innerHTML = "";
    if (yearsFit) yearsFit.innerHTML = "";
    if (grounding) grounding.innerHTML = "";
    if (approve) approve.checked = false;
    if (coverPanel) coverPanel.hidden = true;
  }

  function resetPacketReviewButton() {
    var review = $("#jb-review");
    if (!review) return;
    review.disabled = false;
    var reviewLabel = review.querySelector(".jb-review-label");
    if (reviewLabel) reviewLabel.textContent = t("jobs.review.first", "Review first");
  }

  function resetJobActionControls() {
    var source = $("#jb-source-revalidate");
    var review = $("#jb-review");
    var apply = $("#jb-apply-review");
    var generateBtn = $("#jb-generate");
    if (source) source.disabled = false;
    if (review) {
      review.disabled = false;
      var reviewLabel = review.querySelector(".jb-review-label");
      if (reviewLabel) reviewLabel.textContent = t("jobs.review.first", "Review first");
    }
    if (apply) {
      apply.disabled = false;
      var applyLabel = apply.querySelector(".jb-apply-label");
      if (applyLabel) applyLabel.textContent = t("jobs.packet.create", "Create packet");
    }
    if (generateBtn && selectedSourceCv()) generateBtn.disabled = false;
    document.querySelectorAll(".jb-packet-action").forEach(function (node) { node.disabled = false; });
  }

  function disableJobActionControls() {
    ["jb-source-revalidate", "jb-review", "jb-generate", "jb-apply-review"].forEach(function (id) {
      var node = document.getElementById(id);
      if (node) node.disabled = true;
    });
    document.querySelectorAll(".jb-packet-action").forEach(function (node) { node.disabled = true; });
  }

  function setPacketCreationControlsDisabled(disabled) {
    ["jb-source-revalidate", "jb-review", "jb-generate"].forEach(function (id) {
      var node = document.getElementById(id);
      if (node) node.disabled = disabled;
    });
    document.querySelectorAll(".jb-packet-action").forEach(function (node) { node.disabled = disabled; });
  }

  function renderPacketReview(review) {
    var panel = $("#jb-review-panel");
    var summary = $("#jb-review-summary");
    var diff = $("#jb-review-diff");
    if (!panel || !summary || !diff) return;
    if (!review || !review.review_token) {
      clearPacketReview();
      return;
    }
    var d = review.diff || {};
    var changed = d.changed
      ? (String(d.added || 0) + " " + t("jobs.review.additions", "additions") + ", " + String(d.removed || 0) + " " + t("jobs.review.removals", "removals"))
      : t("jobs.review.no_yaml_changes", "No YAML changes");
    var score = review.ats && typeof review.ats.score === "number" ? " ATS " + review.ats.score + "%." : "";
    summary.textContent = changed + "." + score + " " + t("jobs.review.apply_summary", "Apply creates the tailored CV and writes the linked cover letter.");
    diff.textContent = (d.lines || []).join("\n") || t("jobs.review.no_diff", "No diff returned.");
    renderReviewLineage(review);
    panel.hidden = false;
    if (window.renderIcons) window.renderIcons(panel);
  }

  function renderReviewLineage(review) {
    var ats = review.ats && typeof review.ats === "object" ? review.ats : {};
    var fit = ats.experience_fit && typeof ats.experience_fit === "object" ? ats.experience_fit : {};
    var profile = ats.profile && typeof ats.profile === "object" ? (ats.profile.label || ats.profile.id) : "";
    var lineage = $("#jb-review-lineage");
    if (lineage) {
      lineage.innerHTML =
        '<span class="jb-review-id">' + esc(t("jobs.review.review", "Review")) + " " + esc(review.review_id || "") + "</span>" +
        '<span class="jb-review-source-cv">' + esc(t("jobs.review.source_cv", "Source CV")) + " " + esc(review.source_cv_id || "") + "</span>" +
        '<span class="jb-review-source-hash">' + esc(t("jobs.review.source_hash", "source")) + " " + esc((review.source_hash || "").slice(0, 12)) + "</span>" +
        '<span class="jb-review-jd-hash">' + esc(t("jobs.review.jd_hash", "JD")) + " " + esc((review.jd_hash || "").slice(0, 12)) + "</span>" +
        '<span class="jb-review-job">' + esc(t("jobs.review.job", "Job")) + " " + esc(review.job_id || state.jobId || "") + "</span>" +
        '<span class="jb-review-score-system">' + esc(profile || "semantic_fit") + "</span>" +
        '<span class="jb-review-model">' + esc([review.provider, review.model].filter(Boolean).join(" / ")) + "</span>";
    }
    var beforeAfter = $("#jb-review-before-after");
    if (beforeAfter) {
      beforeAfter.innerHTML =
        '<div class="jb-review-before">' + esc(t("jobs.review.before", "Before")) + ": " + esc(t("jobs.review.source_revision", "source revision")) + " " + esc(review.source_revision || t("jobs.review.current", "current")) + "</div>" +
        '<div class="jb-review-after">' + esc(t("jobs.review.after", "After")) + ": " + esc(t("jobs.review.tailored_artifact", "reviewed tailored artifact")) + " " + esc((review.tailored_hash || "").slice(0, 12)) + "</div>";
    }
    var postAts = $("#jb-post-tailor-ats");
    if (postAts) {
      postAts.textContent = t("jobs.review.post_tailor_ats", "Post-tailor ATS") + ": " + (ats.score == null ? t("jobs.review.not_returned", "not returned") : ats.score + "%");
    }
    var yearsFit = $("#jb-review-years-fit");
    if (yearsFit) {
      yearsFit.textContent = t("jobs.review.years_fit", "Years fit") + ": " +
        [fit.candidate_years, fit.required_years].map(function (v) { return v == null ? "?" : v; }).join("/") +
        " · " + esc(fit.fit || t("jobs.review.unknown", "unknown"));
    }
    var grounding = $("#jb-cover-grounding-status");
    if (grounding) {
      grounding.innerHTML =
        '<span class="jb-cover-grounding-evidence">' + esc(t("jobs.review.grounding_evidence", "Cover letter will be generated from the reviewed tailored CV and current job JD.")) + "</span>";
    }
    var coverPanel = $("#jb-cover-review-panel");
    var approve = $("#jb-approve-cover-review");
    if (coverPanel) coverPanel.hidden = true;
    if (approve) approve.checked = false;
  }

  function renderPendingCoverReview(packet) {
    var panel = $("#jb-review-panel");
    var grounding = $("#jb-cover-grounding-status");
    var coverPanel = $("#jb-cover-review-panel");
    var approve = $("#jb-approve-cover-review");
    var btn = $("#jb-apply-review");
    var hash = (packet && packet.cover_letter_hash) || "";
    var letterId = (packet && packet.cover_letter_id) || "";
    if (panel) panel.hidden = false;
    if (grounding) {
      var link = letterId
        ? ' <a class="jb-linked-open" href="/cover-letters?id=' + encodeURIComponent(letterId) + '">' + esc(t("jobs.packet.open_cover", "Open cover letter")) + "</a>"
        : "";
      grounding.innerHTML =
        '<span class="jb-cover-grounding-evidence">' +
        esc(t("jobs.review.generated_cover_hash", "Generated cover-letter hash")) +
        " " + esc(hash.slice(0, 12)) + "</span>" + link;
    }
    if (coverPanel) coverPanel.hidden = false;
    if (approve) approve.checked = false;
    if (btn) {
      btn.disabled = false;
      var label = btn.querySelector(".jb-apply-label");
      if (label) label.textContent = t("jobs.packet.confirm_review", "Confirm review");
    }
  }

  function renderLinked(sel, items, kind, emptyMsg) {
    var box = $(sel);
    box.innerHTML = "";
    if (!items.length) {
      box.innerHTML = '<p class="muted cl-empty">' + esc(emptyMsg) + "</p>";
      return;
    }
    items.forEach(function (doc) {
      var row = document.createElement("div");
      row.className = "cl-list-row jb-linked-row";
      row.dataset.docId = doc.id || "";
      row.dataset.docType = kind;
      if (state.jobId) row.dataset.jobId = state.jobId;
      var packet = (state.detailPackets || []).filter(function (p) {
        return (kind === "cv" && p.tailored_cv_id === doc.id) ||
          (kind === "cover_letter" && p.cover_letter_id === doc.id);
      })[0];
      if (packet) {
        row.dataset.packetId = packet.id || "";
        var linkedPacketState = packetStateName(packet, state.detailJob);
        row.dataset.packetState = linkedPacketState;
        row.dataset.packetUsable = packetUsableInLinkedRow(linkedPacketState) ? "true" : "false";
        row.dataset.sourceCvId = packet.source_cv_id || "";
        row.dataset.tailoredCvId = packet.tailored_cv_id || "";
        row.dataset.coverLetterId = packet.cover_letter_id || "";
        row.dataset.sourceCvHash = packet.source_cv_hash || "";
        row.dataset.jdHash = packet.job_content_hash || "";
        row.dataset.tailoredCvHash = packet.tailored_cv_hash || "";
        row.dataset.coverLetterHash = packet.cover_letter_hash || "";
        row.dataset.scoreSystemId = packet.score_system_id || "";
        row.dataset.promptVersion = packet.prompt_version || "";
        row.dataset.packetVersion = String(packet.packet_version || "");
        row.dataset.reviewId = packet.cover_letter_review_id || "";
        row.dataset.coverLetterReviewStatus = packet.cover_letter_review_status || "";
        row.dataset.coverLetterReviewConfirmedAt = packet.cover_letter_review_confirmed_at || "";
      }
      var open = document.createElement("a");
      open.className = "cl-list-open jb-linked-open";
      // CVs open in the editor; cover letters open on the cover-letters page.
      open.href = (kind === "cv" ? "/app?id=" : "/cover-letters?id=") + encodeURIComponent(doc.id);
      var badgeLabel = kind === "cv" ? t("lib.badge.cv", "CV") : t("lib.badge.cover", "Cover letter");
      var badgeClass = kind === "cv" ? "cl-badge cl-badge-cv" : "cl-badge cl-badge-cover";
      open.innerHTML =
        '<span class="' + badgeClass + '">' + esc(badgeLabel) + "</span>" +
        '<span class="jb-linked-name">' + esc(doc.name || (kind === "cv" ? "Tailored CV" : "Cover letter")) + "</span>" +
        '<span class="jb-linked-meta">' +
          '<span class="jb-linked-chip">' + esc(t("lib.storage.saved", "Saved in Documents")) + "</span>" +
          '<span class="jb-linked-chip">' + esc(t("lib.storage.linked_job", "Linked to this job")) + "</span>" +
          (packet ? '<span class="jb-linked-chip">' + esc(packetChipLabel(row.dataset.packetState, packet)) + "</span>" : "") +
        "</span>";
      row.appendChild(open);
      var pdf = document.createElement("button");
      pdf.type = "button";
      pdf.className = "cl-btn jb-linked-pdf";
      pdf.innerHTML = '<span data-icon="download" data-icon-size="12"></span><span>' +
        esc(t("jobs.linked.pdf", "PDF")) + "</span>";
      pdf.addEventListener("click", function () { buildLinkedPdf(doc.id, kind, pdf); });
      row.appendChild(pdf);
      box.appendChild(row);
    });
  }

  function packetById(packetId) {
    return (state.detailPackets || []).filter(function (p) { return String(p.id || "") === String(packetId || ""); })[0] || null;
  }

  function onPacketAction(e) {
    var btn = e.target.closest("[data-packet-action]");
    if (!btn || !state.jobId) return;
    var jid = state.jobId;
    var navToken = state.navReq;
    var token = ++state.packetActionReq;
    var packetId = btn.dataset.packetId || "";
    var action = btn.dataset.packetAction || "";
    if (!packetId || (action !== "archive" && action !== "regenerate")) return;
    btn.disabled = true;
    var base = "/api/jobs/" + encodeURIComponent(jid) + "/packets/" + encodeURIComponent(packetId);
    var path = base + (action === "archive" ? "/archive" : "/regenerate");
    jsonFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    })
      .then(function (res) {
        if (token !== state.packetActionReq || navToken !== state.navReq || state.jobId !== jid) return;
        if (action === "regenerate" && (res.status === 200 || res.status === 201) && res.body && res.body.review_token) {
          var review = res.body;
          review.job_id = review.job_id || jid;
          state.packetReview = review;
          var sel = $("#jb-source-cv");
          if (sel && review.source_cv_id) sel.value = review.source_cv_id;
          renderPacketReview(review);
          setFeedback($("#jb-gen-feedback"), "ok", t("jobs.packet.feedback.regeneration_ready", "Regeneration review ready. Inspect the changes, then create the replacement packet."));
          return;
        }
        if (res.status === 200 || res.status === 201) {
          var msg = action === "archive" ? t("jobs.packet.feedback.archived", "Packet archived.") : t("jobs.packet.feedback.regenerated", "Packet regenerated.");
          openJob(jid).then(function (opened) {
            if (!opened || token !== state.packetActionReq || navToken + 1 !== state.navReq || state.jobId !== jid) return;
            setFeedback($("#jb-gen-feedback"), "ok", msg);
          });
          return;
        }
        var b = res.body || {};
        if (action === "regenerate" && res.status === 409 && b.code === "review_required") {
          var packet = packetById(packetId) || b.packet || {};
          var sel = $("#jb-source-cv");
          if (sel && packet.source_cv_id) sel.value = packet.source_cv_id;
          setFeedback($("#jb-gen-feedback"), "info", t("jobs.packet.feedback.review_required", "Regeneration requires a fresh review. Review the current CV changes before creating the packet."));
          prepareReview();
          return;
        }
        setFeedback($("#jb-gen-feedback"), "error", esc(b.error || (t("jobs.packet.feedback.action_failed", "Packet action failed") + " (" + res.status + ").")));
      })
      .catch(function () {
        if (token !== state.packetActionReq || navToken !== state.navReq || state.jobId !== jid) return;
        setFeedback($("#jb-gen-feedback"), "error", t("jobs.network_error", "Network error - please try again."));
      })
      .finally(function () {
        if (token === state.packetActionReq && navToken === state.navReq && state.jobId === jid) btn.disabled = false;
      });
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

  function buildLinkedPdf(id, kind, btn) {
    if (!id || (btn && btn.disabled)) return;
    var pdfTab = reservePdfWindow();
    var original = btn ? btn.innerHTML : "";
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span data-icon="loader" data-icon-size="12"></span><span>' +
        esc(t("jobs.linked.building", "Building")) + "</span>";
      if (window.renderIcons) window.renderIcons(btn);
    }
    var isCv = kind === "cv";
    var request = isCv
      ? fetch("/api/build", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: id })
        })
      : fetch("/api/cover-letter/" + encodeURIComponent(id) + "/build");
    request
      .then(function (res) {
        if (!res.ok) {
          return res.json().catch(function () { return {}; }).then(function (data) {
            throw new Error(data.hint || data.error || ("HTTP " + res.status));
          });
        }
        var ctype = res.headers.get("Content-Type") || "";
        if (ctype.indexOf("application/json") !== -1) {
          return res.json().then(function (data) {
            openPdfTarget(data.url, isCv ? "cv.pdf" : "cover-letter.pdf", pdfTab);
          });
        }
        return res.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          openPdfTarget(url, isCv ? "cv.pdf" : "cover-letter.pdf", pdfTab);
          setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
        });
      })
      .catch(function (err) {
        closeReservedWindow(pdfTab);
        setFeedback($("#jb-gen-feedback"), "error", esc((err && err.message) || "Couldn't render the PDF."));
      })
      .finally(function () {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = original;
          if (window.renderIcons) window.renderIcons(btn);
        }
      });
  }

  function scrollLinkedIntoView() {
    var row = document.querySelector(".jb-linked-row");
    if (!row || !row.scrollIntoView) return;
    row.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function changeStatus() {
    if (!state.jobId) return;
    var jid = state.jobId;
    var token = ++state.statusReq;
    var select = $("#jb-status-select");
    var current = state.jobs.find(function (j) { return j.id === jid; });
    var previous = (current && current.status) || (state.detailJob && state.detailJob.id === jid && state.detailJob.status) || "saved";
    var next = select.value;
    function restoreStatusSelect() {
      if (token === state.statusReq && state.jobId === jid && select) select.value = previous;
    }
    jsonFetch("/api/jobs/" + encodeURIComponent(jid) + "/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: next })
    })
      .then(function (res) {
        if (token !== state.statusReq) return;
        if (res.status === 200) {
          // Immutable-update the cached job + re-render the grid.
          state.jobs = state.jobs.map(function (j) {
            return j.id === jid ? Object.assign({}, j, { status: next }) : j;
          });
          if (state.detailJob && state.detailJob.id === jid) {
            state.detailJob = Object.assign({}, state.detailJob, { status: next });
          }
          renderGrid();
        } else {
          restoreStatusSelect();
          setFeedback($("#jb-gen-feedback"), "error", esc((res.body && res.body.error) || "Couldn't update status."));
        }
      })
      .catch(function () {
        restoreStatusSelect();
        setFeedback($("#jb-gen-feedback"), "error", "Network error updating status.");
      });
  }

  function toggleStarredJob(job) {
    if (!job || !job.id) return;
    var jid = job.id;
    if (state.starReqs[jid]) return;
    var next = !isJobStarred(job);
    state.starReqs[jid] = true;
    syncDetailStarButton(state.detailJob);
    renderGrid();
    jsonFetch("/api/jobs/" + encodeURIComponent(jid), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ "starred": next })
    })
      .then(function (res) {
        if (res.status !== 200) {
          setFeedback($("#jb-feedback"), "error", esc((res.body && res.body.error) || t("jobs.star.failed", "Couldn't update the star.")));
          return;
        }
        var updated = res.body || Object.assign({}, job, { starred: next });
        state.jobs = state.jobs.map(function (j) {
          return j.id === jid ? Object.assign({}, j, updated, { starred: updated.starred === true }) : j;
        });
        if (state.detailJob && state.detailJob.id === jid) {
          state.detailJob = Object.assign({}, state.detailJob, updated, { starred: updated.starred === true });
          syncDetailStarButton(state.detailJob);
        }
        renderGrid();
      })
      .catch(function () {
        setFeedback($("#jb-feedback"), "error", t("jobs.star.failed", "Couldn't update the star."));
      })
      .finally(function () {
        delete state.starReqs[jid];
        syncDetailStarButton(state.detailJob);
        renderGrid();
      });
  }

  function revalidateSource() {
    if (!state.jobId) return;
    var jid = state.jobId;
    var navToken = state.navReq;
    var token = ++state.sourceReq;
    var btn = $("#jb-source-revalidate");
    var fullJd = $("#jb-source-full-jd") ? $("#jb-source-full-jd").value : "";
    state.fullJdDrafts[jid] = fullJd;
    if (btn) btn.disabled = true;
    setFeedback($("#jb-gen-feedback"), "info", t("jobs.source.feedback.updating", "Updating source review state..."));
    jsonFetch("/api/jobs/" + encodeURIComponent(jid) + "/source/revalidate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ full_jd_text: fullJd })
    }).then(function (res) {
      if (token !== state.sourceReq || navToken !== state.navReq || state.jobId !== jid) return;
      if (res.status !== 200) {
        setFeedback($("#jb-gen-feedback"), "error", esc((res.body && res.body.error) || t("jobs.source.feedback.failed", "Source revalidation failed.")));
        return;
      }
      state.jobs = state.jobs.map(function (j) {
        return j.id === jid ? Object.assign({}, j, res.body) : j;
      });
      renderSourceStaleness(res.body);
      renderExperienceFitEvidence(res.body);
      setFeedback($("#jb-gen-feedback"), "ok", t("jobs.source.feedback.updated", "Source review state updated."));
      openJob(jid);
    }).catch(function () {
      if (token !== state.sourceReq || navToken !== state.navReq || state.jobId !== jid) return;
      setFeedback($("#jb-gen-feedback"), "error", t("jobs.source.feedback.network", "Network error updating source state."));
    }).finally(function () {
      if (token === state.sourceReq && navToken === state.navReq && state.jobId === jid && btn) btn.disabled = false;
    });
  }

  function deleteJob() {
    if (!state.jobId) return;
    if (!window.confirm(t("jobs.delete_confirm", "Delete this job? Your tailored CVs and letters are kept, just unlinked."))) return;
    var gone = state.jobId;
    jsonFetch("/api/jobs/" + encodeURIComponent(gone), { method: "DELETE" })
      .then(function (res) {
        if (res.status === 200) {
          state.jobId = null;
          state.jobs = state.jobs.filter(function (j) { return j.id !== gone; });
          resetDetail();
        } else {
          setFeedback($("#jb-gen-feedback"), "error", esc((res.body && res.body.error) || t("jobs.delete_failed", "Couldn't delete that job.")));
        }
      })
      .catch(function () { setFeedback($("#jb-gen-feedback"), "error", t("jobs.delete_network", "Network error deleting job.")); });
  }

  function selectedSourceCv() {
    var sel = $("#jb-source-cv");
    return sel ? sel.value : "";
  }

  function prepareReview() {
    if (!state.jobId) return;
    if (!assertFullJdReady()) return;
    var cvId = selectedSourceCv();
    if (!cvId) {
      setFeedback($("#jb-gen-feedback"), "error", t("jobs.generate.no_cv", "Add a CV first, then pick it as the source."));
      return;
    }
    var btn = $("#jb-review");
    var jid = state.jobId;
    var navToken = state.navReq;
    var token = ++state.packetReq;
    btn.disabled = true;
    btn.querySelector(".jb-review-label").textContent = t("jobs.review.busy", "Reviewing…");
    clearPacketReview();
    setFeedback($("#jb-gen-feedback"), "info", t("jobs.review.preparing", "Preparing a reviewed CV copy. Nothing is saved until you apply it."));
    jsonFetch("/api/jobs/" + encodeURIComponent(jid) + "/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_cv_id: cvId })
    })
      .then(function (res) {
        if (token !== state.packetReq || navToken !== state.navReq || state.jobId !== jid) return;
        if (res.status === 200 && res.body && res.body.duplicate) {
          clearPacketReview();
          setFeedback($("#jb-gen-feedback"), "ok", t("jobs.review.duplicate", "Current packet already exists for this source CV and job."));
          openJob(jid).then(scrollLinkedIntoView);
          return;
        }
        if (res.status === 200 && res.body && res.body.review_token) {
          res.body.job_id = res.body.job_id || jid;
          state.packetReview = res.body;
          renderPacketReview(res.body);
          setFeedback($("#jb-gen-feedback"), "ok", t("jobs.review.ready", "Review ready. Inspect the changes, then apply to create the packet."));
          return;
        }
        handlePacketError(res);
      })
      .catch(function () {
        if (token !== state.packetReq || navToken !== state.navReq || state.jobId !== jid) return;
        setFeedback($("#jb-gen-feedback"), "error", t("jobs.network_error", "Network error - please try again."));
      })
      .finally(function () {
        if (token === state.packetReq && navToken === state.navReq && state.jobId === jid) {
          btn.disabled = false;
          btn.querySelector(".jb-review-label").textContent = t("jobs.review.first", "Review first");
        }
      });
  }

  function applyPacketReview() {
    if (state.pendingPacket && state.pendingPacket.packet_id) {
      confirmPendingCoverLetterReview();
      return;
    }
    if (!state.jobId || !state.packetReview || !state.packetReview.review_token) return;
    if (state.applyReviewInFlight) return;
    var jid = state.jobId;
    var reviewToken = state.packetReview.review_token;
    if (state.packetReview.job_id && state.packetReview.job_id !== jid) {
      clearPacketReview();
      setFeedback($("#jb-gen-feedback"), "error", t("jobs.review.different_job", "Review belongs to a different job. Prepare a fresh review."));
      return;
    }
    var cvId = state.packetReview.source_cv_id || selectedSourceCv();
    if (!cvId) return;
    var navToken = state.navReq;
    var btn = $("#jb-apply-review");
    state.applyReviewInFlight = true;
    btn.disabled = true;
    setPacketCreationControlsDisabled(true);
    btn.querySelector(".jb-apply-label").textContent = t("jobs.packet.creating", "Creating...");
    setFeedback($("#jb-gen-feedback"), "info", t("jobs.packet.applying", "Applying the reviewed CV and writing the cover letter..."));
    jsonFetch("/api/jobs/" + encodeURIComponent(jid) + "/apply-review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_cv_id: cvId,
        review_token: reviewToken
      })
    })
      .then(function (res) {
        if (navToken !== state.navReq || state.jobId !== jid || !state.packetReview || state.packetReview.review_token !== reviewToken) return;
        if (res.status === 201) {
          if (res.body && res.body.requires_cover_letter_review && res.body.packet_id && res.body.cover_letter_hash) {
            state.pendingPacket = res.body;
            renderPendingCoverReview(res.body);
            setFeedback($("#jb-gen-feedback"), "info", t("jobs.packet.cover_pending", "Cover letter generated. Open it, review the grounded content, then confirm the displayed hash."));
          } else {
            clearPacketReview();
            setFeedback($("#jb-gen-feedback"), "ok", t("jobs.packet.created", "Packet created from the reviewed CV changes."));
            openJob(jid).then(scrollLinkedIntoView);
          }
          state.jobs = state.jobs.map(function (j) {
            return j.id === jid
              ? Object.assign({}, j, { has_cover: true, cover_id: res.body.cover_letter_id })
              : j;
          });
          loadCredit();
          return;
        }
        handlePacketError(res);
      })
      .catch(function () {
        if (navToken !== state.navReq || state.jobId !== jid) return;
        setFeedback($("#jb-gen-feedback"), "error", t("jobs.network_error", "Network error - please try again."));
      })
      .finally(function () {
        if (navToken === state.navReq && state.jobId === jid) {
          state.applyReviewInFlight = false;
          setPacketCreationControlsDisabled(false);
          btn.disabled = false;
          btn.querySelector(".jb-apply-label").textContent = state.pendingPacket
            ? t("jobs.packet.confirm_review", "Confirm review")
            : t("jobs.packet.create", "Create packet");
        }
      });
  }

  function confirmPendingCoverLetterReview() {
    if (!state.jobId || !state.pendingPacket || !state.pendingPacket.packet_id) return;
    if (state.applyReviewInFlight) return;
    var approve = $("#jb-approve-cover-review");
    if (!approve || approve.checked !== true) {
      setFeedback($("#jb-gen-feedback"), "error", t("jobs.review.confirm_grounding", "Review and confirm the cover-letter grounding before packet completion."));
      if (approve) approve.focus();
      return;
    }
    var jid = state.jobId;
    var packet = state.pendingPacket;
    var navToken = state.navReq;
    var btn = $("#jb-apply-review");
    state.applyReviewInFlight = true;
    if (btn) {
      btn.disabled = true;
      var label = btn.querySelector(".jb-apply-label");
      if (label) label.textContent = t("jobs.packet.confirming", "Confirming...");
    }
    setPacketCreationControlsDisabled(true);
    setFeedback($("#jb-gen-feedback"), "info", t("jobs.packet.confirming_review", "Confirming cover-letter review..."));
    jsonFetch("/api/jobs/" + encodeURIComponent(jid) + "/packets/" + encodeURIComponent(packet.packet_id) + "/confirm-cover-letter-review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cover_letter_hash: packet.cover_letter_hash })
    })
      .then(function (res) {
        if (navToken !== state.navReq || state.jobId !== jid || state.pendingPacket !== packet) return;
        if (res.status === 200) {
          clearPacketReview();
          setFeedback($("#jb-gen-feedback"), "ok", t("jobs.packet.created", "Packet created from the reviewed CV changes."));
          openJob(jid).then(scrollLinkedIntoView);
          return;
        }
        handlePacketError(res);
      })
      .catch(function () {
        if (navToken !== state.navReq || state.jobId !== jid) return;
        setFeedback($("#jb-gen-feedback"), "error", t("jobs.network_error", "Network error - please try again."));
      })
      .finally(function () {
        if (navToken === state.navReq && state.jobId === jid) {
          state.applyReviewInFlight = false;
          setPacketCreationControlsDisabled(false);
          if (btn) {
            btn.disabled = false;
            var label = btn.querySelector(".jb-apply-label");
            if (label) label.textContent = state.pendingPacket
              ? t("jobs.packet.confirm_review", "Confirm review")
              : t("jobs.packet.create", "Create packet");
          }
        }
      });
  }

  function handlePacketError(res) {
    var b = (res && res.body) || {};
    if (res.status === 422 && b.error === "unverified_claims") {
      setFeedback($("#jb-gen-feedback"), "error",
        "<strong>" + esc(t("jobs.packet.unverified_claims", "Blocked - couldn't verify these claims against your CV:")) + "</strong> " +
        (b.items || []).map(esc).join(", ") + ". " + esc(t("jobs.packet.nothing_saved", "Nothing was saved.")));
    } else if (res.status === 409 && b.error === "overflow") {
      setFeedback($("#jb-gen-feedback"), "error",
        "<strong>" + esc(t("jobs.packet.overflow", "The reviewed CV overflowed onto a second page")) + "</strong> (" + esc(b.heading || t("jobs.packet.a_section", "a section")) +
        "). " + esc(t("jobs.packet.nothing_saved", "Nothing was saved.")));
    } else if (res.status === 409 && b.code === "source_changed") {
      setFeedback($("#jb-gen-feedback"), "error", esc(b.hint || "The source CV changed. Prepare the review again."));
      clearPacketReview();
    } else if (res.status === 402 && b.code === "needs_key") {
      setFeedback($("#jb-gen-feedback"), "error",
        t("jobs.packet.needs_key", "You're out of free generations. Add your API key in the account settings to continue."));
    } else if (res.status === 503) {
      setFeedback($("#jb-gen-feedback"), "error", t("jobs.ai_unavailable", "AI is temporarily unavailable. Try again shortly."));
    } else {
      setFeedback($("#jb-gen-feedback"), "error", esc(b.error || (t("jobs.request_failed", "Request failed") + " (" + res.status + ").")));
    }
  }

  // ── One-click generate (tailor a CV + write a cover letter) ───────────────
  function generate() {
    if (!state.jobId) return;
    setFeedback($("#jb-gen-feedback"), "info",
      t("jobs.generate.review_required", "Preparing the reviewed CV changes. Packet creation is blocked until cover-letter grounding is confirmed."));
    prepareReview();
  }

  // ── Filter chips ──────────────────────────────────────────────────────────
  function setFilter(filter, btn) {
    state.filter = filter;
    document.querySelectorAll(".jb-chip").forEach(function (c) {
      var on = c === btn;
      c.classList.toggle("is-active", on);
      c.setAttribute("aria-pressed", on ? "true" : "false");
    });
    renderGrid();
  }

  function registerPaletteActions() {
    var P = window.OnePagerPalette;
    if (!P || typeof P.register !== "function") return;
    P.register({
      id: "jobs-add",
      group: "actions",
      labelKey: "palette.jobs.add",
      label: "Add job",
      aliases: ["job", "save job", "manual job"],
      icon: "plus-circle",
      run: function () {
        var details = $("#jb-add-details");
        if (details) details.open = true;
        var title = $("#jb-title");
        if (title) title.focus();
      },
      available: function () { return !!$("#jb-add"); }
    });
    P.register({
      id: "jobs-import-preview",
      group: "actions",
      labelKey: "palette.jobs.import_preview",
      label: "Preview job import",
      aliases: ["import", "preview", "local jobs"],
      icon: "search",
      run: previewImport,
      available: function () {
        var btn = $("#jb-import-preview");
        return !!btn && !btn.disabled;
      }
    });
    P.register({
      id: "jobs-generate-packet",
      group: "actions",
      labelKey: "palette.jobs.generate",
      label: "Generate reviewed packet",
      aliases: ["generate", "tailor", "cover letter", "packet"],
      icon: "sparkles",
      run: generate,
      available: function () { return !!state.jobId && !!$("#jb-generate"); }
    });
  }

  // ── Wire up ───────────────────────────────────────────────────────────────
  function init() {
    $("#jb-add").addEventListener("click", addJob);
    $("#jb-import-preview").addEventListener("click", previewImport);
    if ($("#jb-local-search")) $("#jb-local-search").addEventListener("click", previewLocalSearch);
    $("#jb-import-save-selected").addEventListener("click", saveSelectedImport);
    $("#jb-import-mark-stale").addEventListener("click", markSelectedImportStale);
    $("#jb-delete").addEventListener("click", deleteJob);
    if ($("#jb-star")) $("#jb-star").addEventListener("click", function () { toggleStarredJob(state.detailJob); });
    if ($("#jb-source-revalidate")) $("#jb-source-revalidate").addEventListener("click", revalidateSource);
    $("#jb-generate").addEventListener("click", generate);
    $("#jb-review").addEventListener("click", prepareReview);
    $("#jb-apply-review").addEventListener("click", applyPacketReview);
    $("#jb-packet-state").addEventListener("click", onPacketAction);
    $("#jb-status-select").addEventListener("change", changeStatus);
    $("#jb-source-cv").addEventListener("change", function () {
      state.packetReq++;
      clearPacketReview();
      resetPacketReviewButton();
      setFeedback($("#jb-gen-feedback"), null);
    });
    $("#jb-grid").addEventListener("click", onGridClick);
    $("#jb-grid").addEventListener("keydown", onGridKey);
    $("#jb-search").addEventListener("input", function (e) { state.query = cleanProfileText(e.target.value, 300); renderGrid(); });
    $("#jb-sort").addEventListener("change", function (e) { state.sort = e.target.value; renderGrid(); });
    document.querySelectorAll(".jb-filter-control").forEach(function (control) {
      control.addEventListener("input", syncSavedFilters);
      control.addEventListener("change", syncSavedFilters);
    });
    if ($("#jb-source-full-jd")) {
      $("#jb-source-full-jd").addEventListener("input", function (e) {
        if (state.jobId) state.fullJdDrafts[state.jobId] = e.target.value;
      });
    }
    document.querySelectorAll(".jb-chip").forEach(function (chip) {
      chip.addEventListener("click", function () { setFilter(chip.dataset.filter, chip); });
    });
    if (window.renderIcons) window.renderIcons(document);
    applyJobSourceDefaults();
    registerPaletteActions();

    // i18n: load + localise [data-i18n], mount the EN|DE toggle, and on switch
    // re-render the grid + open detail so the JS-rendered labels (status badges,
    // deadline pills, buttons, empty states) localise live too.
    if (window.OnePagerI18n) {
      window.OnePagerI18n.init($(".cl-topbar-right"), function () {
        renderGrid();
        if (state.jobId) openJob(state.jobId);
      });
    }

    if (window.OnePagerAuth && window.OnePagerAuth.ready) {
      window.OnePagerAuth.ready.then(function () {
        loadCredit();
        loadCvs();
        loadJobs().then(function () {
          applyJobSourceDefaults();
          var id = requestedJobId();
          if (id) openJob(id);
        });
      });
    }
  }

  function syncSavedFilters() {
    state.savedFilters = {
      family: cleanFilterValue("#jb-filter-family", 160),
      searchMode: enumFilterValue("#jb-search-mode", JOB_SEARCH_MODES, "flexible"),
      country: cleanFilterValue("#jb-filter-country", 80),
      city: cleanFilterValue("#jb-filter-city", 200),
      region: cleanFilterValue("#jb-filter-region", 120),
      provider: enumFilterValue("#jb-filter-provider", ["", "manual", "linkedin", "other"], ""),
      verification: enumFilterValue("#jb-filter-verification", ["", "user_provided", "needs_review", "stale"], ""),
      remote: enumFilterValue("#jb-filter-remote", JOB_REMOTE_MODES, ""),
      seniority: cleanFilterValue("#jb-filter-seniority", 120),
      freshness: enumFilterValue("#jb-filter-freshness", JOB_FRESHNESS, ""),
      minYears: normalizeMinYears(cleanFilterValue("#jb-filter-min-years", 8)),
      rankBucket: cleanFilterValue("#jb-filter-rank-bucket", 80),
    };
    renderGrid();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
