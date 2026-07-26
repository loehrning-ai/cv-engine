/* ============================================================================
   CV Copilot — landing page behaviour (Stage 9).

     1. If a Supabase session already exists, redirect to /app (the editor) —
        a logged-in visitor should land in the app, not the marketing page.
     2. Localise the page via OnePagerI18n and wire the EN|DE toggle.

   The Supabase JS client + i18n helper are loaded before this script in
   landing.html. Both are best-effort: if auth config is missing the page
   still renders fully (it's a public marketing page).
   ============================================================================ */
(function () {
  "use strict";

  // ── 1. Redirect logged-in users to the editor app. ───────────────────────
  function maybeRedirectToApp() {
    if (!window.supabase || !window.supabase.createClient) return;
    fetch("/auth/config")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (cfg) {
        if (!cfg || !cfg.supabase_url || !cfg.supabase_publishable_key) return;
        var supa = window.supabase.createClient(cfg.supabase_url, cfg.supabase_publishable_key, {
          auth: {
            persistSession: true,
            autoRefreshToken: false,
            detectSessionInUrl: false,
            storage: window.sessionStorage,
          },
        });
        return supa.auth.getSession().then(function (res) {
          if (res && res.data && res.data.session) {
            window.location.replace("/app");
          }
        });
      })
      .catch(function () {
        /* public page — never block render on an auth check */
      });
  }

  // ── 2. i18n: localise + wire the language toggle. ─────────────────────────
  function setupI18n() {
    if (!window.OnePagerI18n) return;
    var i18n = window.OnePagerI18n;
    i18n.load(i18n.getLang()).then(function () {
      document.documentElement.setAttribute("lang", i18n.getLang());
      i18n.apply(document);
    });
    document.querySelectorAll("[data-lang]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        i18n.setLang(btn.getAttribute("data-lang")).then(function () {
          document.documentElement.setAttribute("lang", i18n.getLang());
          i18n.apply(document);
        });
      });
    });
  }

  function boot() {
    maybeRedirectToApp();
    setupI18n();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
