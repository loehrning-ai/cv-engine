/* ============================================================================
   CV Copilot — client i18n helper (Stage 9).

   A tiny, framework-free layer used by BOTH the landing page and the editor:
     • the chosen language is persisted in localStorage ("onepager.lang"),
       default "en";
     • strings come from /api/i18n?lang=xx (server is the source of truth —
       see tools/editor/i18n.py), cached in-memory after first fetch;
     • t(key, vars) resolves a key (with {n}-style interpolation);
     • apply(root) fills every [data-i18n] element's text and every
       [data-i18n-attr="attr:key"] attribute.

   Exposed as window.OnePagerI18n.
   ============================================================================ */
(function () {
  "use strict";

  var LANG_KEY = "onepager.lang";
  var SUPPORTED = ["en", "de"];
  var DEFAULT_LANG = "en";

  var strings = {}; // { key: text } for the active language
  var current = DEFAULT_LANG;

  function normalize(lang) {
    var code = (lang || "").toString().trim().toLowerCase().slice(0, 2);
    return SUPPORTED.indexOf(code) >= 0 ? code : DEFAULT_LANG;
  }

  function getLang() {
    try {
      var stored = localStorage.getItem(LANG_KEY);
      if (stored) return normalize(stored);
    } catch (_) {}
    // Fall back to the browser language, then EN.
    return normalize((navigator.language || DEFAULT_LANG).slice(0, 2));
  }

  function setLang(lang) {
    current = normalize(lang);
    try {
      localStorage.setItem(LANG_KEY, current);
    } catch (_) {}
    return load(current);
  }

  // Fetch the resolved dictionary for a language. /api/i18n is auth-exempt so
  // it works on the landing page (pre-session) too.
  function load(lang) {
    current = normalize(lang || getLang());
    return fetch("/api/i18n?lang=" + encodeURIComponent(current))
      .then(function (r) {
        return r.ok ? r.json() : {};
      })
      .then(function (data) {
        strings = (data && data.strings) || {};
        return strings;
      })
      .catch(function () {
        strings = {};
        return strings;
      });
  }

  function t(key, vars) {
    var s = strings[key];
    if (s == null) return key; // never blank — fall back to the key
    if (vars) {
      Object.keys(vars).forEach(function (k) {
        s = s.replace(new RegExp("\\{" + k + "\\}", "g"), vars[k]);
      });
    }
    return s;
  }

  // Fill the DOM. [data-i18n="key"] sets textContent; [data-i18n-html="key"]
  // sets innerHTML (only for our own trusted strings).
  function apply(root) {
    var scope = root || document;
    scope.querySelectorAll("[data-i18n]").forEach(function (el) {
      var key = el.getAttribute("data-i18n");
      var val = strings[key];
      if (val != null) el.textContent = val;
    });
    scope.querySelectorAll("[data-i18n-html]").forEach(function (el) {
      var key = el.getAttribute("data-i18n-html");
      var val = strings[key];
      if (val != null) el.innerHTML = val;
    });
    // [data-i18n-attr="attr:key"] localises an arbitrary attribute (e.g. an
    // input placeholder or an aria-label). Multiple pairs may be comma-separated.
    scope.querySelectorAll("[data-i18n-attr]").forEach(function (el) {
      el.getAttribute("data-i18n-attr").split(",").forEach(function (pair) {
        var bits = pair.split(":");
        if (bits.length !== 2) return;
        var attr = bits[0].trim();
        var val = strings[bits[1].trim()];
        if (attr && val != null) el.setAttribute(attr, val);
      });
    });
    // Reflect the active language on any language toggle buttons.
    scope.querySelectorAll("[data-lang]").forEach(function (btn) {
      var on = normalize(btn.getAttribute("data-lang")) === current;
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  // Mount an EN|DE toggle into a topbar container (the sub-pages' shared idiom,
  // mirroring app.js's editor toggle). `container` is a topbar-right element;
  // `onChange` is called after the language is switched + persisted so the page
  // can re-apply its dynamic (JS-rendered) labels. The toggle is a fixed-size
  // segmented control (each button is a 2-char language code), so adding it and
  // switching languages never reflows the navbar.
  function mountToggle(container, onChange) {
    if (!container || container.querySelector(".lang-toggle")) return;
    var wrap = document.createElement("div");
    wrap.className = "lang-toggle";
    wrap.setAttribute("role", "group");
    wrap.setAttribute("aria-label", "Language");
    SUPPORTED.forEach(function (code) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "lang-btn";
      b.setAttribute("data-lang", code);
      b.textContent = code.toUpperCase();
      b.addEventListener("click", function () {
        setLang(code).then(function () {
          document.documentElement.setAttribute("lang", getLang());
          apply(document);
          if (typeof onChange === "function") onChange();
        });
      });
      wrap.appendChild(b);
    });
    container.insertBefore(wrap, container.firstChild);
  }

  // Convenience: load the active language, apply [data-i18n], mount the toggle
  // into `container`, and call `onChange` once now (so dynamic labels localise
  // on first paint too). Sub-pages call this from boot.
  function init(container, onChange) {
    return load(getLang()).then(function () {
      document.documentElement.setAttribute("lang", getLang());
      apply(document);
      mountToggle(container, onChange);
      if (typeof onChange === "function") onChange();
      return strings;
    });
  }

  window.OnePagerI18n = {
    getLang: getLang,
    setLang: setLang,
    load: load,
    t: t,
    apply: apply,
    mountToggle: mountToggle,
    init: init,
    SUPPORTED: SUPPORTED,
  };
})();
