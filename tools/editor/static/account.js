/*
  Account & data self-service page — Stage 10.

  A small, additive companion to the editor, consistent with jobs.js /
  cover-letters.js / tailor.js. It reuses auth.js (which patches window.fetch to
  attach the Supabase bearer token on every /api/* call) and icons.js, so this
  file only owns the account UX:

    • view + edit profile (display name, default language)  → /api/account/profile
    • manage BYO provider keys: status / connect / remove → /api/key
        (the full key is NEVER shown — only provider + last4 + credits left)
    • download the scoped live-data export (JSON)            → /api/account/export
    • request account deletion (soft-delete + 30-day grace)  → /api/account/delete
    • cancel a pending deletion                              → /api/account/cancel-delete

  The server enforces the real contracts (credit column is non-writable, key
  validation, RLS scoping, the deletion RPCs). This file just surfaces them.
  Framework-free, plain DOM, matching the other pages' style.
*/
(function () {
  "use strict";

  var $ = function (sel) { return document.querySelector(sel); };
  var NEW_CV_DEFAULTS_KEY = "cv.editor.new_cv_defaults";
  var JOB_SOURCE_DEFAULTS_KEY = "cv.editor.job_source_defaults";
  var DEFAULT_FONTS = ["serif", "sans", "mono"];
  var DEFAULT_DENSITIES = ["tight", "normal", "airy"];
  var DEFAULT_TEMPLATES = ["classic", "modern", "sidebar", "executive", "technical", "ats-compact", "consulting", "minimal"];
  var DEFAULT_PAPERS = ["white", "creme", "soft-gray", "blue-tint"];
  var JOB_SOURCES = ["", "linkedin", "manual", "all"];
  var JOB_REMOTE_MODES = ["", "onsite", "hybrid", "remote"];
  var JOB_FRESHNESS = ["", "fresh", "stale"];
  var JOB_SEARCH_MODES = ["flexible", "strict"];
  var JOB_PROFILE_SCHEMA = "onepager.job_profile.v1";
  var JOB_PROFILE_FIELDS = {
    source: true,
    title: true,
    family: true,
    titles: true,
    search_mode: true,
    country: true,
    region: true,
    cities: true,
    remote_mode: true,
    seniority: true,
    freshness: true,
    min_years: true,
    ranking: true
  };

  // i18n: resolve a key (fall back to the provided default if the dict is not
  // loaded or the key is missing). The dictionary + [data-i18n] application live
  // in OnePagerI18n; this wrapper is for the JS-rendered (dynamic) strings.
  function t(key, fallback) {
    if (!window.OnePagerI18n) return fallback;
    var v = window.OnePagerI18n.t(key);
    return v && v !== key ? v : fallback;
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function setFeedback(el, kind, html) {
    if (!el) return;
    if (!html) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;
    el.className = "cl-feedback cl-feedback-" + kind;
    el.innerHTML = html;
  }

  function jsonFetch(url, opts) {
    return fetch(url, opts).then(function (r) {
      return r.json().then(
        function (body) { return { status: r.status, body: body }; },
        function () { return { status: r.status, body: {} }; }
      );
    });
  }

  function validHex(value) {
    return /^#[0-9a-f]{6}$/i.test(String(value || "").trim());
  }

  function safeLocalGet(key) {
    try { return window.localStorage.getItem(key); } catch (_) { return null; }
  }

  function safeLocalSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (_) {}
  }

  function safeLocalRemove(key) {
    try { window.localStorage.removeItem(key); } catch (_) {}
  }

  function splitList(value, limit, itemLimit) {
    return String(value == null ? "" : value)
      .split(/[,\n]/)
      .map(function (item) { return item.trim().slice(0, itemLimit); })
      .filter(function (item) { return !profileSecretShaped(item); })
      .filter(Boolean)
      .slice(0, limit);
  }

  function profileSecretShaped(value) {
    return /\b(li_at|jsessionid|session[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|secret[_-]?key|private[_-]?key|client[_-]?secret|authorization\s*[:=]|bearer\s+[A-Za-z0-9._~+/=-]+|cookie\s*[:=]|password\s*[:=]|secret\s*[:=]|key\s*[:=]\s*[^,\s;]+|token\s*[=:]\s*[^,\s;]+|sk-[A-Za-z0-9_-]+|AIza[0-9A-Za-z_-]{20,}|ya29\.[0-9A-Za-z_-]{20,})/i
      .test(String(value == null ? "" : value));
  }

  function cleanProfileText(value, limit) {
    var text = String(value == null ? "" : value).trim().slice(0, limit);
    return profileSecretShaped(text) ? "" : text;
  }

  function joinList(items) {
    return Array.isArray(items) ? items.join(", ") : "";
  }

  function normalizeMinYears(value) {
    var raw = String(value == null ? "" : value).trim();
    if (!raw) return "";
    var parsed = Number(raw);
    if (!isFinite(parsed)) return "";
    parsed = Math.max(0, Math.min(40, Math.round(parsed * 2) / 2));
    return String(parsed).replace(/\.0$/, "");
  }

  function parseRanking(value) {
    var text = String(value == null ? "" : value).trim();
    if (!text) return {};
    var out = {};
    text.split(/[,\n]/).forEach(function (part) {
      var bits = part.split(/[=:]/);
      if (bits.length < 2) return;
      var rawKey = bits[0].trim();
      if (profileSecretShaped(part) || profileSecretShaped(rawKey)) return;
      var key = rawKey.toLowerCase().replace(/[^a-z_]/g, "");
      var val = Number(bits.slice(1).join(":").trim());
      if (!key || !isFinite(val)) return;
      out[key] = Math.max(0, Math.min(1, val));
    });
    return out;
  }

  function formatRanking(ranking) {
    if (!ranking || typeof ranking !== "object") return "";
    return Object.keys(ranking).map(function (key) {
      return key + "=" + ranking[key];
    }).join(", ");
  }

  function readNewCvDefaults() {
    var raw = safeLocalGet(NEW_CV_DEFAULTS_KEY);
    if (!raw) return {};
    try {
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return {};
      return {
        accent: validHex(parsed.accent) ? parsed.accent : "",
        font: DEFAULT_FONTS.indexOf(parsed.font) >= 0 ? parsed.font : "",
        density: DEFAULT_DENSITIES.indexOf(parsed.density) >= 0 ? parsed.density : "",
        template: DEFAULT_TEMPLATES.indexOf(parsed.template) >= 0 ? parsed.template : "",
        paper: DEFAULT_PAPERS.indexOf(parsed.paper) >= 0 ? parsed.paper : "",
      };
    } catch (_) {
      return {};
    }
  }

  function collectNewCvDefaults() {
    var accent = $("#ac-default-accent") ? $("#ac-default-accent").value : "";
    var font = $("#ac-default-font") ? $("#ac-default-font").value : "";
    var density = $("#ac-default-density") ? $("#ac-default-density").value : "";
    var template = $("#ac-default-template") ? $("#ac-default-template").value : "";
    var paper = $("#ac-default-paper") ? $("#ac-default-paper").value : "";
    return {
      accent: validHex(accent) ? accent : "",
      font: DEFAULT_FONTS.indexOf(font) >= 0 ? font : "",
      density: DEFAULT_DENSITIES.indexOf(density) >= 0 ? density : "",
      template: DEFAULT_TEMPLATES.indexOf(template) >= 0 ? template : "",
      paper: DEFAULT_PAPERS.indexOf(paper) >= 0 ? paper : "",
    };
  }

  function paintSettingsTheme() {
    var active = window.OnePagerTheme && window.OnePagerTheme.current
      ? window.OnePagerTheme.current()
      : (document.documentElement.getAttribute("data-theme") || "light");
    document.querySelectorAll("[data-theme-choice]").forEach(function (btn) {
      btn.setAttribute("aria-pressed", btn.getAttribute("data-theme-choice") === active ? "true" : "false");
    });
  }

  function applySettingsTheme(theme) {
    if (window.OnePagerTheme && window.OnePagerTheme.set) {
      window.OnePagerTheme.set(theme);
    } else if (window.OnePagerTheme && window.OnePagerTheme.apply) {
      window.OnePagerTheme.apply(theme);
      safeLocalSet("cv.editor.theme", theme);
    }
    paintSettingsTheme();
  }

  function loadSettingsControls() {
    paintSettingsTheme();
    var defaults = readNewCvDefaults();
    if ($("#ac-default-accent")) $("#ac-default-accent").value = defaults.accent || "#111111";
    if ($("#ac-default-font")) $("#ac-default-font").value = defaults.font || "";
    if ($("#ac-default-density")) $("#ac-default-density").value = defaults.density || "";
    if ($("#ac-default-template")) $("#ac-default-template").value = defaults.template || "";
    if ($("#ac-default-paper")) $("#ac-default-paper").value = defaults.paper || "";
  }

  function saveNewCvDefaults() {
    var fb = $("#ac-settings-feedback");
    var defaults = collectNewCvDefaults();
    var compact = {};
    Object.keys(defaults).forEach(function (key) {
      if (defaults[key]) compact[key] = defaults[key];
    });
    if (Object.keys(compact).length) safeLocalSet(NEW_CV_DEFAULTS_KEY, JSON.stringify(compact));
    else safeLocalRemove(NEW_CV_DEFAULTS_KEY);
    setFeedback(fb, "ok", "New-CV defaults saved. Existing CVs were not changed.");
  }

  function resetNewCvDefaults() {
    safeLocalRemove(NEW_CV_DEFAULTS_KEY);
    loadSettingsControls();
    setFeedback($("#ac-settings-feedback"), "ok", "New-CV defaults reset. Existing CVs were not changed.");
  }

  function readJobSourceDefaults() {
    var raw = safeLocalGet(JOB_SOURCE_DEFAULTS_KEY);
    if (!raw) return {};
    try {
      var parsed = JSON.parse(raw);
      return normalizeJobProfile(parsed);
    } catch (_) {
      return {};
    }
  }

  function normalizeJobProfile(parsed) {
    if (!parsed || typeof parsed !== "object") return {};
    return {
      schema: parsed.schema === JOB_PROFILE_SCHEMA ? parsed.schema : "",
      source: JOB_SOURCES.indexOf(parsed.source) >= 0 ? parsed.source : "",
      title: cleanProfileText(parsed.title, 300),
      family: cleanProfileText(parsed.family, 160),
      titles: Array.isArray(parsed.titles) ? splitList(parsed.titles.join(","), 10, 120) : splitList(parsed.titles || "", 10, 120),
      search_mode: JOB_SEARCH_MODES.indexOf(parsed.search_mode) >= 0 ? parsed.search_mode : "flexible",
      country: cleanProfileText(parsed.country, 80),
      region: cleanProfileText(parsed.region, 120),
      cities: Array.isArray(parsed.cities) ? splitList(parsed.cities.join(","), 10, 120) : splitList(parsed.cities || "", 10, 120),
      remote_mode: JOB_REMOTE_MODES.indexOf(parsed.remote_mode) >= 0 ? parsed.remote_mode : "",
      seniority: cleanProfileText(parsed.seniority, 120),
      freshness: JOB_FRESHNESS.indexOf(parsed.freshness) >= 0 ? parsed.freshness : "",
      min_years: normalizeMinYears(parsed.min_years),
      ranking: parseRanking(formatRanking(parsed.ranking))
    };
  }

  function collectJobSourceDefaults() {
    return {
      source: $("#ac-job-source-source") ? $("#ac-job-source-source").value : "",
      title: $("#ac-job-source-title") ? cleanProfileText($("#ac-job-source-title").value, 300) : "",
      family: $("#ac-job-profile-family") ? cleanProfileText($("#ac-job-profile-family").value, 160) : "",
      titles: $("#ac-job-profile-titles") ? splitList($("#ac-job-profile-titles").value, 10, 120) : [],
      search_mode: $("#ac-job-profile-mode") ? $("#ac-job-profile-mode").value : "flexible",
      country: $("#ac-job-source-country") ? cleanProfileText($("#ac-job-source-country").value, 80) : "",
      region: $("#ac-job-source-region") ? cleanProfileText($("#ac-job-source-region").value, 120) : "",
      cities: $("#ac-job-profile-cities") ? splitList($("#ac-job-profile-cities").value, 10, 120) : [],
      remote_mode: $("#ac-job-source-remote") ? $("#ac-job-source-remote").value : "",
      seniority: $("#ac-job-source-seniority") ? cleanProfileText($("#ac-job-source-seniority").value, 120) : "",
      freshness: $("#ac-job-source-freshness") ? $("#ac-job-source-freshness").value : "",
      min_years: $("#ac-job-profile-min-years") ? normalizeMinYears($("#ac-job-profile-min-years").value) : "",
      ranking: $("#ac-job-profile-ranking") ? parseRanking($("#ac-job-profile-ranking").value) : {},
    };
  }

  function loadJobSourceDefaults() {
    var defaults = readJobSourceDefaults();
    if ($("#ac-job-source-source")) $("#ac-job-source-source").value = defaults.source || "";
    if ($("#ac-job-source-title")) $("#ac-job-source-title").value = defaults.title || "";
    if ($("#ac-job-profile-family")) $("#ac-job-profile-family").value = defaults.family || "";
    if ($("#ac-job-profile-titles")) $("#ac-job-profile-titles").value = joinList(defaults.titles);
    if ($("#ac-job-profile-mode")) $("#ac-job-profile-mode").value = defaults.search_mode || "flexible";
    if ($("#ac-job-source-country")) $("#ac-job-source-country").value = defaults.country || "";
    if ($("#ac-job-source-region")) $("#ac-job-source-region").value = defaults.region || "";
    if ($("#ac-job-profile-cities")) $("#ac-job-profile-cities").value = joinList(defaults.cities);
    if ($("#ac-job-source-remote")) $("#ac-job-source-remote").value = defaults.remote_mode || "";
    if ($("#ac-job-source-seniority")) $("#ac-job-source-seniority").value = defaults.seniority || "";
    if ($("#ac-job-source-freshness")) $("#ac-job-source-freshness").value = defaults.freshness || "";
    if ($("#ac-job-profile-min-years")) $("#ac-job-profile-min-years").value = defaults.min_years || "";
    if ($("#ac-job-profile-ranking")) $("#ac-job-profile-ranking").value = formatRanking(defaults.ranking);
  }

  function saveJobSourceDefaults() {
    var defaults = collectJobSourceDefaults();
    var compact = { schema: "onepager.job_profile.v1" };
    Object.keys(defaults).forEach(function (key) {
      var value = defaults[key];
      if (Array.isArray(value)) {
        if (value.length) compact[key] = value;
      } else if (value && typeof value === "object") {
        if (Object.keys(value).length) compact[key] = value;
      } else if (value || key === "source" || key === "search_mode") compact[key] = value;
    });
    safeLocalSet(JOB_SOURCE_DEFAULTS_KEY, JSON.stringify(compact));
    setFeedback($("#ac-job-source-feedback"), "ok", esc(t("account.job_sources.feedback.saved", "Job source defaults saved for this browser.")));
  }

  function resetJobSourceDefaults() {
    safeLocalRemove(JOB_SOURCE_DEFAULTS_KEY);
    loadJobSourceDefaults();
    setFeedback($("#ac-job-source-feedback"), "ok", esc(t("account.job_sources.feedback.reset", "Job source defaults reset.")));
  }

  function compactJobProfile(defaults) {
    var compact = { schema: "onepager.job_profile.v1" };
    Object.keys(defaults || {}).forEach(function (key) {
      var value = defaults[key];
      if (!JOB_PROFILE_FIELDS[key]) return;
      if (key === "schema") return;
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

  function exportJobProfile() {
    var payload = compactJobProfile(collectJobSourceDefaults());
    safeLocalSet(JOB_SOURCE_DEFAULTS_KEY, JSON.stringify(payload));
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = "onepager-job-profile.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    setFeedback($("#ac-job-source-feedback"), "ok", esc(t("account.job_sources.feedback.exported", "Job profile exported.")));
  }

  function importJobProfileText(text) {
    var parsed;
    try { parsed = JSON.parse(text); } catch (_) {
      setFeedback($("#ac-job-source-feedback"), "error", esc(t("account.job_sources.feedback.import_invalid", "Import text is not valid JSON.")));
      return false;
    }
    if (!parsed || parsed.schema !== JOB_PROFILE_SCHEMA) {
      setFeedback($("#ac-job-source-feedback"), "error", esc(t("account.job_sources.feedback.import_schema", "Import text is not a CV Copilot job profile.")));
      return false;
    }
    safeLocalSet(JOB_SOURCE_DEFAULTS_KEY, JSON.stringify(compactJobProfile(normalizeJobProfile(parsed))));
    loadJobSourceDefaults();
    setFeedback($("#ac-job-source-feedback"), "ok", esc(t("account.job_sources.feedback.imported", "Job profile imported.")));
    return true;
  }

  function chooseJobProfileImport() {
    var raw = window.prompt(t("account.job_sources.import_prompt", "Paste the exported job profile JSON."));
    if (raw == null || !String(raw).trim()) return;
    importJobProfileText(String(raw));
  }

  function renderJobSourceStatus(status) {
    var box = $("#ac-job-source-status");
    if (!box) return;
    var linkedIn = status && status.sources && status.sources.linkedin ? status.sources.linkedin : {};
    var policy = status && status.policy ? status.policy : {};
    var state = !linkedIn.enabled ? "disabled" : linkedIn.stale ? "stale" : "ready";
    var parts = [];
    if (linkedIn.local_db_available) {
      parts.push(t("account.job_sources.status.local_available", "Local jobs DB available"));
      if (linkedIn.row_count != null) {
        parts.push(t("account.job_sources.status.rows", "{n} rows").replace("{n}", String(linkedIn.row_count).replace(/\B(?=(\d{3})+(?!\d))/g, ",")));
      }
      if (linkedIn.latest_seen_at) {
        parts.push(t("account.job_sources.status.latest_seen", "latest seen {date}").replace("{date}", linkedIn.latest_seen_at));
      }
      if (linkedIn.age_days != null) {
        parts.push(t("account.job_sources.status.days_old", "{n} days old").replace("{n}", linkedIn.age_days));
      }
    } else {
      parts.push(t("account.job_sources.status.local_unavailable", "Local jobs DB unavailable"));
    }
    parts.push(linkedIn.adapter_configured
      ? t("account.job_sources.status.adapter_configured", "approved adapter configured")
      : t("account.job_sources.status.adapter_disabled", "approved adapter disabled"));
    if (policy.no_blind_linkedin_fetch) parts.push(t("account.job_sources.status.no_blind_fetch", "no blind LinkedIn fetch"));
    if (linkedIn.stale) parts.push(t("account.job_sources.status.stale", "stale"));
    box.dataset.state = state;
    box.innerHTML = '<span class="ac-source-dot"></span><span>' + esc(parts.join(" · ")) + "</span>";
  }

  function loadJobSourceStatus() {
    var box = $("#ac-job-source-status");
    if (box) {
      box.dataset.state = "loading";
      box.innerHTML = '<span class="ac-source-dot"></span><span>' + esc(t("account.job_sources.status.checking", "Checking job source status...")) + "</span>";
    }
    return jsonFetch("/api/jobs/sources/status").then(function (res) {
      if (res.status === 200) renderJobSourceStatus(res.body);
      else if (box) {
        box.dataset.state = "disabled";
        box.innerHTML = '<span class="ac-source-dot"></span><span>' + esc(t("account.job_sources.status.unavailable", "Job source status unavailable.")) + "</span>";
      }
    }).catch(function () {
      if (box) {
        box.dataset.state = "disabled";
        box.innerHTML = '<span class="ac-source-dot"></span><span>' + esc(t("account.job_sources.status.unavailable", "Job source status unavailable.")) + "</span>";
      }
    });
  }

  // Provider/model catalog (from /api/models) + the user's saved preference.
  var MODELS = { providers: [], default_provider: "anthropic" };
  // Per-provider connection state from /api/key (GET).
  var KEY_STATE = { providers: {}, managed_credits_remaining: 0 };

  function modelsFor(provider) {
    var entry = (MODELS.providers || []).filter(function (p) { return p.id === provider; })[0];
    return entry || { id: provider, models: [], default: "" };
  }

  function selectedProvider() {
    var checked = document.querySelector('input[name="ac-provider"]:checked');
    return checked ? checked.value : "anthropic";
  }

  function providerIcon(provider) {
    return provider === "anthropic" || provider === "gemini" ? provider : "sparkles";
  }

  function providerMeta(provider, entry) {
    if (provider === "anthropic") return t("account.ai.provider_anthropic", "Anthropic · managed credits or BYO key");
    if (provider === "gemini") return t("account.ai.provider_gemini", "Google · your own API key");
    if (entry && entry.auth_type === "none") return "Local provider · no stored key";
    return "BYO key · managed credits disabled";
  }

  function renderProviderCatalog() {
    var wrap = $("#ac-provider");
    if (!wrap || !(MODELS.providers || []).length) return;
    var current = selectedProvider();
    wrap.innerHTML = (MODELS.providers || []).map(function (entry, idx) {
      var id = entry.id;
      var label = entry.label || id;
      var checked = id === current || (!current && idx === 0) ? " checked" : "";
      return (
        '<label class="ac-provider-card" data-provider="' + esc(id) + '">' +
          '<input type="radio" name="ac-provider" value="' + esc(id) + '"' + checked + ' />' +
          '<span class="ac-provider-glyph" data-icon="' + esc(providerIcon(id)) + '" data-icon-size="22"></span>' +
          '<span class="ac-provider-body">' +
            '<span class="ac-provider-head">' +
              '<span class="ac-provider-name">' + esc(label) + '</span>' +
              '<span class="ac-provider-check" data-icon="check" data-icon-size="13" aria-hidden="true"></span>' +
            '</span>' +
            '<span class="ac-provider-meta">' + esc(providerMeta(id, entry)) + '</span>' +
            '<span class="ac-provider-state" data-provider-state="' + esc(id) + '">' +
              '<span class="ac-state-dot"></span>' +
              '<span class="ac-state-text">' + esc(t("account.ai.checking", "Checking…")) + '</span>' +
            '</span>' +
          '</span>' +
        '</label>'
      );
    }).join("");
    if (window.renderIcons) window.renderIcons(wrap);
    var radios = document.querySelectorAll('input[name="ac-provider"]');
    Array.prototype.forEach.call(radios, function (r) {
      r.addEventListener("change", function () {
        renderModelOptions("");
        renderKeyFieldForProvider();
        savePreference();
      });
    });
  }

  // ── Profile ────────────────────────────────────────────────────────────────
  function loadProfile() {
    return jsonFetch("/api/account/profile").then(function (res) {
      if (res.status !== 200) return;
      var p = res.body || {};
      if ($("#ac-email")) $("#ac-email").value = p.email || "";
      if ($("#ac-name")) $("#ac-name").value = p.display_name || "";
      if ($("#ac-lang")) $("#ac-lang").value = p.default_language || "en";
      // Provider/model preference (default Anthropic).
      var prov = p.preferred_provider || "anthropic";
      var radio = document.querySelector('input[name="ac-provider"][value="' + prov + '"]');
      if (radio) radio.checked = true;
      renderModelOptions(p.preferred_model || "");
      renderKeyFieldForProvider();
      renderDeletionState(p.deletion_requested_at);
    });
  }

  // Populate the model dropdown for the selected provider; preselect `wanted`
  // (the saved preferred_model) if it's in the allowlist, else the default.
  function renderModelOptions(wanted) {
    var sel = $("#ac-model");
    if (!sel) return;
    var entry = modelsFor(selectedProvider());
    sel.innerHTML = "";
    (entry.models || []).forEach(function (m) {
      var opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m + (m === entry.default ? " (default)" : "");
      sel.appendChild(opt);
    });
    var has = (entry.models || []).indexOf(wanted) !== -1;
    sel.value = has ? wanted : (entry.default || "");
  }

  function saveProfile() {
    var btn = $("#ac-save-profile");
    var fb = $("#ac-profile-feedback");
    var payload = {
      display_name: ($("#ac-name") ? $("#ac-name").value : "").trim(),
      default_language: $("#ac-lang") ? $("#ac-lang").value : "en",
      preferred_provider: selectedProvider(),
      preferred_model: $("#ac-model") ? $("#ac-model").value : null
    };
    btn.disabled = true;
    setFeedback(fb, "info", "Saving…");
    jsonFetch("/api/account/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (res) {
      if (res.status === 200) {
        setFeedback(fb, "ok", "Profile saved.");
      } else {
        setFeedback(fb, "error", esc((res.body && res.body.error) || "Could not save."));
      }
    }).catch(function () {
      setFeedback(fb, "error", "Network error saving profile.");
    }).finally(function () {
      btn.disabled = false;
    });
  }

  // Save just the provider/model preference (used by the selector's own button
  // if present; saveProfile already includes them too).
  function savePreference() {
    var fb = $("#ac-provider-feedback");
    setFeedback(fb, "info", "Saving preference…");
    jsonFetch("/api/account/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        preferred_provider: selectedProvider(),
        preferred_model: $("#ac-model") ? $("#ac-model").value : null
      })
    }).then(function (res) {
      if (res.status === 200) {
        setFeedback(fb, "ok", "Default AI saved.");
      } else {
        setFeedback(fb, "error", esc((res.body && res.body.error) || "Could not save."));
      }
    });
  }

  // ── Provider/model catalog ─────────────────────────────────────────────────
  function loadModelsCatalog() {
    return jsonFetch("/api/models").then(function (res) {
      if (res.status === 200 && res.body) {
        MODELS = res.body;
        renderProviderCatalog();
      }
    });
  }

  // ── BYO key (per provider) ──────────────────────────────────────────────────
  // GET /api/key returns registry-shaped provider status plus managed credits.
  function loadKeyStatus() {
    return jsonFetch("/api/key").then(function (res) {
      KEY_STATE = res.body || { providers: {}, managed_credits_remaining: 0 };
      KEY_LOADED = true;
      renderKeyFieldForProvider();
    }).catch(function () {
      // Network/parse failure — show an honest error state instead of a perpetual
      // "Checking…" so the page never looks hung.
      KEY_LOADED = true;
      var wrap = $("#ac-key-status");
      var text = $("#ac-key-text");
      if (wrap) { wrap.classList.remove("is-connected"); wrap.setAttribute("data-state", "error"); }
      if (text) text.textContent = t("account.ai.status_error", "Couldn't load key status.");
    });
  }

  // Per-provider key-field metadata. The labels are i18n-resolved (with the EN
  // default baked in) so the field localises with the rest of the page.
  function keyMeta(prov) {
    var entry = modelsFor(prov);
    if (entry.auth_type === "none") {
      return { label: (entry.label || "Local provider") + " status", placeholder: "No API key required" };
    }
    if (prov === "openrouter") return { label: "OpenRouter API key", placeholder: "sk-or-v1-…" };
    if (prov === "groq") return { label: "Groq API key", placeholder: "gsk_…" };
    if (prov === "deepseek") return { label: "DeepSeek API key", placeholder: "sk-…" };
    if (prov === "mistral") return { label: "Mistral API key", placeholder: "sk-…" };
    if (prov === "openai_compatible") return { label: "OpenAI-compatible API key", placeholder: "sk-…" };
    if (prov === "gemini") {
      return { label: t("account.ai.key_label_gemini", "Google (Gemini) API key"), placeholder: "AIza…" };
    }
    return { label: t("account.ai.key_label_anthropic", "Anthropic API key"), placeholder: "sk-ant-…" };
  }

  // Whether the per-provider key status has been fetched yet (drives the
  // "Checking…" vs real-state copy on the cards + status pill).
  var KEY_LOADED = false;

  // The connection state for one provider, with safe defaults.
  function providerState(prov) {
    return (KEY_STATE.providers || {})[prov] || { connected: false, last4: null };
  }

  // The "managed credits" / "BYO required" sub-line for a provider.
  function creditLine(prov) {
    var credits = KEY_STATE.managed_credits_remaining || 0;
    return prov === "anthropic"
      ? (credits + " " + t("account.ai.credits_left", "managed credits left"))
      : t("account.ai.byo_required", "BYO key required");
  }

  // Paint the small connection chip on EACH provider card so the user can see,
  // at a glance, which providers already have a key — without selecting them.
  function renderProviderCards() {
    var cards = document.querySelectorAll(".ac-provider-card");
    Array.prototype.forEach.call(cards, function (card) {
      var prov = card.getAttribute("data-provider");
      var slot = card.querySelector('[data-provider-state="' + prov + '"]');
      if (!slot) return;
      var dot = slot.querySelector(".ac-state-dot");
      var txt = slot.querySelector(".ac-state-text");
      var st = providerState(prov);
      if (!KEY_LOADED) {
        slot.setAttribute("data-state", "loading");
        if (txt) txt.textContent = t("account.ai.checking", "Checking…");
        return;
      }
      if (st.connected) {
        slot.setAttribute("data-state", "connected");
        if (txt) txt.textContent = st.auth_type === "none"
          ? "No key required"
          : t("account.ai.key_connected", "Key connected") + " · …" + (st.last4 || "");
      } else {
        slot.setAttribute("data-state", "empty");
        if (txt) txt.textContent = t("account.ai.no_key", "No key connected");
      }
      if (dot) dot.title = txt ? txt.textContent : "";
    });
  }

  // Reflect the SELECTED provider in the key field + status pill. Managed
  // credits are shown for Claude only (Gemini is BYO-only). All visible strings
  // are i18n-resolved so they switch with the language toggle.
  function renderKeyFieldForProvider() {
    var prov = selectedProvider();
    var meta = keyMeta(prov);
    var label = $("#ac-key-label");
    var input = $("#ac-key-input");
    if (label) label.textContent = meta.label;
    if (input) {
      input.placeholder = meta.placeholder;
      input.value = "";
      input.type = "password";
    }
    // Reset the reveal toggle (hidden until the user types something).
    syncRevealToggle();

    var st = providerState(prov);
    var entry = modelsFor(prov);
    var wrap = $("#ac-key-status");
    var text = $("#ac-key-text");
    var remove = $("#ac-remove-key");
    var save = $("#ac-save-key");
    var saveLabel = $("#ac-save-key-label");
    var creditStr = creditLine(prov);

    // Selected-card affordance: mark the chosen card so CSS can highlight it
    // even on browsers without :has() (progressive enhancement).
    var cards = document.querySelectorAll(".ac-provider-card");
    Array.prototype.forEach.call(cards, function (c) {
      c.classList.toggle("is-selected", c.getAttribute("data-provider") === prov);
    });

    if (entry.auth_type === "none") {
      if (wrap) { wrap.classList.add("is-connected"); wrap.setAttribute("data-state", "connected"); }
      if (text) text.textContent = "No API key required · " + creditStr;
      if (remove) remove.hidden = true;
      if (saveLabel) saveLabel.textContent = "No key required";
      if (input) input.disabled = true;
      if (save) save.disabled = true;
      renderProviderCards();
      return;
    }

    if (input) input.disabled = false;
    if (save) save.disabled = false;

    if (!KEY_LOADED) {
      if (wrap) { wrap.classList.remove("is-connected"); wrap.setAttribute("data-state", "loading"); }
      if (text) text.textContent = t("account.ai.checking", "Checking…");
      if (remove) remove.hidden = true;
      if (saveLabel) saveLabel.textContent = t("account.ai.connect", "Connect key");
    } else if (st.connected) {
      if (wrap) { wrap.classList.add("is-connected"); wrap.setAttribute("data-state", "connected"); }
      if (text) text.innerHTML = t("account.ai.key_connected", "Key connected") +
        " · <strong>…" + esc(st.last4 || "") + "</strong> · " + esc(creditStr);
      if (remove) remove.hidden = false;
      if (saveLabel) saveLabel.textContent = t("account.ai.rotate", "Rotate key");
    } else {
      if (wrap) { wrap.classList.remove("is-connected"); wrap.setAttribute("data-state", "empty"); }
      if (text) text.innerHTML = t("account.ai.no_key", "No key connected") + " · " + esc(creditStr);
      if (remove) remove.hidden = true;
      if (saveLabel) saveLabel.textContent = t("account.ai.connect", "Connect key");
    }
    renderProviderCards();
  }

  // Show the reveal (show/hide) button only when the field has typed text, and
  // keep its label + pressed-state in sync with the input's masking.
  function syncRevealToggle() {
    var input = $("#ac-key-input");
    var btn = $("#ac-key-reveal");
    var lbl = $("#ac-key-reveal-label");
    if (!input || !btn) return;
    var hasText = !!input.value;
    btn.hidden = !hasText;
    var shown = input.type === "text";
    btn.setAttribute("aria-pressed", shown ? "true" : "false");
    if (lbl) {
      lbl.textContent = shown
        ? t("account.ai.hide", "Hide")
        : t("account.ai.reveal", "Show");
    }
  }

  function toggleReveal() {
    var input = $("#ac-key-input");
    if (!input) return;
    input.type = input.type === "password" ? "text" : "password";
    syncRevealToggle();
    input.focus();
  }

  // Swap the save button into a busy (validating) state and back. Reuses the
  // shared `.is-busy` + loader-icon convention from the editor so the spinner
  // and its reduced-motion guard come for free.
  function setSaveBusy(busy) {
    var btn = $("#ac-save-key");
    var lbl = $("#ac-save-key-label");
    if (!btn) return;
    btn.disabled = busy;
    btn.classList.toggle("is-busy", busy);
    var icon = btn.querySelector("[data-icon]");
    if (icon) {
      icon.setAttribute("data-icon", busy ? "loader" : "key");
      if (window.renderIcons) window.renderIcons(btn);
    }
    if (lbl && busy) lbl.textContent = t("account.ai.validating", "Validating…");
    // On !busy, renderKeyFieldForProvider() resets the label to Connect/Rotate.
  }

  function saveKey() {
    var prov = selectedProvider();
    var meta = keyMeta(prov);
    if (modelsFor(prov).auth_type === "none") return;
    var input = $("#ac-key-input");
    var fb = $("#ac-key-feedback");
    var key = (input ? input.value : "").trim();
    if (!key) {
      setFeedback(fb, "error",
        t("account.ai.paste_first", "Paste your") + " " + esc(meta.label) + ".");
      if (input) input.focus();
      return;
    }
    setSaveBusy(true);
    setFeedback(fb, "info",
      t("account.ai.validating_long", "Validating and encrypting your key…"));
    jsonFetch("/api/key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: prov, key: key })
    }).then(function (res) {
      setSaveBusy(false);
      if (res.status === 200) {
        if (input) { input.value = ""; input.type = "password"; }
        // Refresh state first so the success message + the card chip agree.
        loadKeyStatus().then(function () {
          var last4 = providerState(prov).last4 || (res.body && res.body.last4) || "";
          setFeedback(fb, "ok",
            t("account.ai.saved_ok",
              "Key connected. Stored encrypted, shown only as the last 4 characters.") +
            (last4 ? " <strong>…" + esc(last4) + "</strong>" : ""));
        });
      } else {
        setFeedback(fb, "error", esc((res.body && res.body.error) ||
          t("account.ai.save_fail", "Could not save the key.")));
      }
    }).catch(function () {
      setSaveBusy(false);
      setFeedback(fb, "error",
        t("account.ai.save_fail", "Could not save the key."));
    });
  }

  function removeKey() {
    var prov = selectedProvider();
    var meta = keyMeta(prov);
    var fb = $("#ac-key-feedback");
    var ask = t("account.ai.remove_confirm", "Remove your stored") + " " + meta.label + "?";
    if (!window.confirm(ask)) return;
    setFeedback(fb, "info", t("account.ai.removing", "Removing…"));
    jsonFetch("/api/key?provider=" + encodeURIComponent(prov), { method: "DELETE" })
      .then(function (res) {
        if (res.status === 200) {
          setFeedback(fb, "ok", t("account.ai.removed_ok", "Key removed."));
          loadKeyStatus();
        } else {
          setFeedback(fb, "error", t("account.ai.remove_fail", "Could not remove the key."));
        }
      }).catch(function () {
        setFeedback(fb, "error", t("account.ai.remove_fail", "Could not remove the key."));
      });
  }

  // ── Export ───────────────────────────────────────────────────────────────
  // The <a href="/api/account/export"> works directly (auth.js can't add a
  // bearer header to a plain navigation), so we fetch the blob in JS and force
  // a download — that way the token IS attached and the response saves cleanly.
  function exportData(ev) {
    if (ev) ev.preventDefault();
    var fb = $("#ac-export-feedback");
    setFeedback(fb, "info", "Preparing your export…");
    fetch("/api/account/export").then(function (r) {
      if (!r.ok) throw new Error("export failed");
      var name = "onepager-export.json";
      var cd = r.headers.get("Content-Disposition") || "";
      var m = cd.match(/filename="?([^"]+)"?/);
      if (m) name = m[1];
      return r.blob().then(function (blob) { return { blob: blob, name: name }; });
    }).then(function (out) {
      var url = URL.createObjectURL(out.blob);
      var a = document.createElement("a");
      a.href = url; a.download = out.name;
      document.body.appendChild(a); a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      setFeedback(fb, "ok", "Your data has been downloaded.");
    }).catch(function () {
      setFeedback(fb, "error", "Could not prepare your export. Try again.");
    });
  }

  // ── Deletion (soft-delete + 30-day grace) ────────────────────────────────
  function renderDeletionState(requestedAt) {
    var pending = !!requestedAt;
    var dflt = $("#ac-delete-default");
    var pend = $("#ac-delete-pending");
    if (dflt) dflt.hidden = pending;
    if (pend) pend.hidden = !pending;
    if (pending && $("#ac-pending-text")) {
      $("#ac-pending-text").textContent =
        "Your account is scheduled for deletion. Cancel strictly before the " +
        "30-day deadline. At D+30 cleanup becomes irrevocable; external " +
        "storage is removed before the account database rows.";
    }
  }

  function requestDelete() {
    var confirm = $("#ac-confirm");
    var fb = $("#ac-delete-feedback");
    if (!confirm || confirm.value.trim() !== "DELETE") {
      setFeedback(fb, "error", "Type DELETE to confirm.");
      return;
    }
    var btn = $("#ac-delete");
    var scheduled = false;
    btn.disabled = true;
    setFeedback(fb, "info", "Scheduling deletion…");
    jsonFetch("/api/account/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "DELETE" })
    }).then(function (res) {
      if (res.status === 200 && res.body && res.body.deletion_requested) {
        scheduled = true;
        if (window.OnePagerAuth && window.OnePagerAuth.clearSensitiveLocalState) {
          window.OnePagerAuth.clearSensitiveLocalState();
        }
        setFeedback(fb, "ok", esc(res.body.message || "Account scheduled for deletion."));
        renderDeletionState(res.body.requested_at || true);
      } else {
        setFeedback(fb, "error", esc((res.body && res.body.error) || "Could not delete."));
      }
    }).catch(function () {
      setFeedback(fb, "error", "Network error scheduling deletion.");
    }).finally(function () {
      if (!scheduled && btn) btn.disabled = confirm.value.trim() !== "DELETE";
    });
  }

  function cancelDelete() {
    var fb = $("#ac-delete-feedback");
    setFeedback(fb, "info", "Cancelling…");
    jsonFetch("/api/account/cancel-delete", { method: "POST" }).then(function (res) {
      if (res.status === 200 && res.body && res.body.cancelled) {
        setFeedback(fb, "ok", "Deletion cancelled. Your account and documents are restored.");
        renderDeletionState(null);
        var confirm = $("#ac-confirm");
        if (confirm) confirm.value = "";
        var del = $("#ac-delete");
        if (del) del.disabled = true;
      } else {
        setFeedback(
          fb,
          "error",
          "Nothing can be cancelled. The 30-day deadline may have passed."
        );
      }
    }).catch(function () {
      setFeedback(fb, "error", "Network error cancelling deletion.");
    });
  }

  // ── Wiring ───────────────────────────────────────────────────────────────
  function syncSettingsRouteChrome() {
    var path = (window.location && window.location.pathname) || "/account";
    if (path !== "/settings") return;
    document.querySelectorAll('.cl-utility-nav a[href="/account"]').forEach(function (link) {
      link.setAttribute("href", "/settings");
      link.setAttribute("aria-current", "page");
    });
  }

  function wire() {
    syncSettingsRouteChrome();
    document.querySelectorAll("[data-theme-choice]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        applySettingsTheme(btn.getAttribute("data-theme-choice"));
      });
    });
    if ($("#ac-save-defaults")) $("#ac-save-defaults").addEventListener("click", saveNewCvDefaults);
    if ($("#ac-reset-defaults")) $("#ac-reset-defaults").addEventListener("click", resetNewCvDefaults);
    if ($("#ac-save-job-sources")) $("#ac-save-job-sources").addEventListener("click", saveJobSourceDefaults);
    if ($("#ac-reset-job-sources")) $("#ac-reset-job-sources").addEventListener("click", resetJobSourceDefaults);
    if ($("#ac-export-job-profile")) $("#ac-export-job-profile").addEventListener("click", exportJobProfile);
    if ($("#ac-import-job-profile")) $("#ac-import-job-profile").addEventListener("click", chooseJobProfileImport);
    if (window.OnePagerTheme && window.OnePagerTheme.onChange) {
      window.OnePagerTheme.onChange(paintSettingsTheme);
    }
    if ($("#ac-save-profile")) $("#ac-save-profile").addEventListener("click", saveProfile);
    if ($("#ac-save-key")) $("#ac-save-key").addEventListener("click", saveKey);
    if ($("#ac-remove-key")) $("#ac-remove-key").addEventListener("click", removeKey);
    // Show/hide the typed key; the button only appears once there's text.
    if ($("#ac-key-reveal")) $("#ac-key-reveal").addEventListener("click", toggleReveal);
    if ($("#ac-key-input")) {
      var keyInput = $("#ac-key-input");
      keyInput.addEventListener("input", syncRevealToggle);
      // Enter in the key field submits (saves) for keyboard-first users.
      keyInput.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") { ev.preventDefault(); saveKey(); }
      });
    }
    // Provider switch → repopulate the model list + swap the key field/status,
    // and persist the new default provider so it sticks for generations.
    var radios = document.querySelectorAll('input[name="ac-provider"]');
    Array.prototype.forEach.call(radios, function (r) {
      r.addEventListener("change", function () {
        renderModelOptions("");
        renderKeyFieldForProvider();
        savePreference();
      });
    });
    if ($("#ac-model")) $("#ac-model").addEventListener("change", savePreference);
    if ($("#ac-export")) $("#ac-export").addEventListener("click", exportData);
    if ($("#ac-delete")) $("#ac-delete").addEventListener("click", requestDelete);
    if ($("#ac-cancel-delete")) $("#ac-cancel-delete").addEventListener("click", cancelDelete);
    document.querySelectorAll("#ac-logout, #ac-mobile-logout").forEach(function (btn) {
      if (!window.OnePagerAuth) return;
      btn.addEventListener("click", function () {
        window.OnePagerAuth.logout().finally(function () { window.location.href = "/"; });
      });
    });
    // Enable the delete button only when the confirm word matches.
    if ($("#ac-confirm")) {
      $("#ac-confirm").addEventListener("input", function () {
        if ($("#ac-delete")) $("#ac-delete").disabled = this.value.trim() !== "DELETE";
      });
    }
  }

  function boot() {
    if (window.renderIcons) window.renderIcons(document);
    wire();
    loadSettingsControls();
    loadJobSourceDefaults();
    // i18n: load the dictionary, localise [data-i18n] elements, and mount the
    // EN|DE toggle in the topbar. On switch, re-apply the dynamic (JS-rendered)
    // key-field / status strings so they localise too.
    if (window.OnePagerI18n) {
      window.OnePagerI18n.init($(".cl-topbar-right"), function () {
        renderKeyFieldForProvider();
      });
    }
    // Wait for the Supabase session (auth.js) before hitting /api/* so the
    // bearer token is attached; auth.js shows the login view if signed out.
    var ready = (window.OnePagerAuth && window.OnePagerAuth.ready) || Promise.resolve();
    ready.then(function () {
      // Load the provider/model catalog FIRST so the dropdown can populate, then
      // the profile (which preselects the saved provider/model) and key status.
      loadModelsCatalog().then(function () {
        loadProfile();
        loadKeyStatus();
      });
      loadJobSourceStatus();
    }).catch(function () {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
