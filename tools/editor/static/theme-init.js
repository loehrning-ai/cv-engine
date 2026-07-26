/* =========================================================================
   theme-init.js — single source of truth for editor-CHROME theming.

   Loaded SYNCHRONOUSLY in <head> BEFORE the stylesheet on every editor page
   (index + tailor / jobs / cover-letters / account). Because it is a classic,
   render-blocking <script src> (no defer/async) placed before the <link>, the
   IIFE below runs and sets <html data-theme="…"> on the FIRST frame — so the
   correct light/dark token set paints immediately and there is no wrong-mode
   flash on reload.

   Why an external file and not a literal inline <script>: the app's
   Content-Security-Policy is `script-src 'self' <cdns>` with NO 'unsafe-inline'
   (only the /api/preview document relaxes that). An inline <script> would be
   blocked. A same-origin external script satisfies 'self' while still running
   before paint — the no-flash guarantee is preserved without weakening the CSP.

   This module ALSO centralises the toggle logic so app.js and the sub-pages
   don't each reimplement it: window.OnePagerTheme exposes current/apply/cycle/
   wireToggle. The toggle cycles light ↔ dark and persists the choice.
   ========================================================================= */
(function () {
  "use strict";

  var KEY = "cv.editor.theme";
  var MODES = ["light", "dark"];

  function safeGet() {
    try {
      var v = window.localStorage.getItem(KEY);
      return MODES.indexOf(v) !== -1 ? v : "light";
    } catch (_) {
      return "light";
    }
  }

  function safeSet(theme) {
    try {
      window.localStorage.setItem(KEY, theme);
    } catch (_) {
      /* non-fatal: the in-memory <html> attribute still applies for this page */
    }
  }

  // ── No-flash restore: runs the instant this file is parsed, before the
  //    stylesheet paints. Keep this minimal and synchronous. ──────────────
  var initial = safeGet();
  document.documentElement.setAttribute("data-theme", initial);
  document.documentElement.style.colorScheme = initial;

  function resolved(theme) {
    var t = theme || document.documentElement.getAttribute("data-theme") || "light";
    return t === "dark" ? "dark" : "light";
  }

  function label(theme) {
    return resolved(theme) === "dark"
      ? "App theme: Dark"
      : "App theme: Light";
  }

  // Reflect the active mode onto a toggle button (icon swap + a11y label).
  // Safe to call with no button present (sub-pages without a toggle, tests).
  function paintButton(btn, theme) {
    if (!btn) return;
    var concrete = resolved(theme);
    btn.classList.remove("is-light", "is-dark");
    btn.classList.add("is-" + concrete);
    btn.setAttribute("aria-label", label(concrete));
    btn.dataset.tooltip = label(concrete);
    btn.setAttribute("aria-pressed", concrete === "dark" ? "true" : "false");
  }

  var _btns = [];

  // Apply a theme everywhere: <html> attribute + every wired toggle button.
  function apply(theme) {
    var concrete = resolved(theme);
    document.documentElement.setAttribute("data-theme", concrete);
    document.documentElement.style.colorScheme = concrete;
    for (var i = 0; i < _btns.length; i++) paintButton(_btns[i].el, concrete);
    return concrete;
  }

  function set(theme) {
    var concrete = apply(theme);
    safeSet(concrete);
    notify();
    return concrete;
  }

  function current() {
    return resolved(document.documentElement.getAttribute("data-theme"));
  }

  function next(theme) {
    var i = MODES.indexOf(resolved(theme || current()));
    return MODES[(i + 1) % MODES.length];
  }

  // Persist + apply the next mode in the cycle, notify listeners.
  function cycle() {
    var n = next(current());
    safeSet(n);
    apply(n);
    notify();
    return n;
  }

  var _listeners = [];
  function notify() {
    var t = current();
    for (var i = 0; i < _listeners.length; i++) {
      try {
        _listeners[i](t, resolved(t));
      } catch (_) {
        /* a listener throwing must not break theming */
      }
    }
  }

  // Wire a topbar toggle button: paint it, and cycle on click. The click
  // handler/paint bind once per button (so app.js's explicit call and the
  // DOM-ready auto-wire can't double-bind), but an onChange passed on a later
  // call is still registered. That's how app.js attaches its preview refresh
  // even if the auto-wire ran first.
  function wireToggle(btn, onChange) {
    if (!btn) return null;
    if (typeof onChange === "function") _listeners.push(onChange);
    if (btn._opThemeWired) return btn;
    btn._opThemeWired = true;
    _btns.push({ el: btn });
    paintButton(btn, current());
    btn.addEventListener("click", function () {
      cycle();
      // Icon micro-flip on switch — the animation itself lives in CSS and is
      // gated on prefers-reduced-motion (plan 108 stage 4).
      btn.classList.remove("is-switching");
      void btn.offsetWidth;
      btn.classList.add("is-switching");
      window.clearTimeout(btn._opSwitchT);
      btn._opSwitchT = window.setTimeout(function () {
        btn.classList.remove("is-switching");
      }, 450);
    });
    return btn;
  }

  // Auto-wire a #theme-toggle on DOM-ready for pages that have NO page script
  // of their own (the sub-pages: tailor / jobs / cover-letters / account).
  // The editor (app.js) wires it explicitly first; wireToggle is idempotent so
  // this never double-binds. Pages that want a re-render callback wire it
  // themselves before this fires.
  function autoWire() {
    var btn = document.getElementById("theme-toggle");
    if (btn) wireToggle(btn);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autoWire);
  } else {
    autoWire();
  }

  window.OnePagerTheme = {
    KEY: KEY,
    MODES: MODES,
    current: current,
    resolved: resolved,
    apply: apply,
    set: set,
    cycle: cycle,
    wireToggle: wireToggle,
    onChange: function (fn) {
      if (typeof fn === "function") _listeners.push(fn);
    },
  };
})();
