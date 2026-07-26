/* ============================================================================
   CV Copilot — Supabase auth gate (Stage 2).

   Responsibilities, kept deliberately separate from app.js:
     1. Boot the Supabase JS client from the publishable config (/auth/config).
     2. Patch window.fetch so EVERY same-origin /api/* request carries
        `Authorization: Bearer <session access_token>`. Centralising it here
        means app.js's many fetch() calls need no changes.
     3. Gate the editor: no session → show the login view; session → reveal
        the editor and resolve OnePagerAuth.ready so app.js boots.
     4. Provide a logout control.

    The Supabase JS client is loaded from a pinned local vendor asset. Google/GitHub
   sign-in additionally require the OPERATOR to configure each provider's
   OAuth client id + secret in the Supabase dashboard (Authentication →
   Providers). Magic-link email works out of the box with Supabase's default
   email sender. See the note in index.html.
   ============================================================================ */
(function () {
  "use strict";

  var supa = null; // Supabase client, once booted.
  var session = null; // current session (null = logged out).
  var demoMode = false; // DEV-ONLY: /auth/config reported demo_mode:true.
  var readyResolve;
  var LOCAL_DEMO_SESSION_KEY = "onepager.localDemoSession";
  var readyPromise = new Promise(function (res) {
    readyResolve = res;
  });

  function loadLocalDemoSession() {
    if (!demoMode) return null;
    try {
      var raw = window.sessionStorage.getItem(LOCAL_DEMO_SESSION_KEY);
      var legacy = window.localStorage.getItem(LOCAL_DEMO_SESSION_KEY);
      if (!raw && legacy) {
        raw = legacy;
        window.localStorage.removeItem(LOCAL_DEMO_SESSION_KEY);
        window.sessionStorage.setItem(LOCAL_DEMO_SESSION_KEY, legacy);
      }
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || !parsed.access_token || !parsed.refresh_token) return null;
      if (parsed.expires_at && Number(parsed.expires_at) <= Math.floor(Date.now() / 1000)) {
        clearLocalDemoSession();
        return null;
      }
      return parsed;
    } catch (_) {
      clearLocalDemoSession();
      return null;
    }
  }

  function saveLocalDemoSession(data) {
    var stored = {
      access_token: data.access_token,
      refresh_token: data.refresh_token,
      expires_at: data.expires_at || 0,
      local_demo: true,
    };
    window.sessionStorage.setItem(LOCAL_DEMO_SESSION_KEY, JSON.stringify(stored));
    try {
      window.localStorage.removeItem(LOCAL_DEMO_SESSION_KEY);
    } catch (_) {}
    return stored;
  }

  function clearLocalDemoSession() {
    try {
      window.sessionStorage.removeItem(LOCAL_DEMO_SESSION_KEY);
    } catch (_) {}
    try {
      window.localStorage.removeItem(LOCAL_DEMO_SESSION_KEY);
    } catch (_) {}
  }

  function clearSensitiveLocalState() {
    // CV undo snapshots used to persist complete documents in localStorage.
    // Remove every legacy per-document key plus account-specific selections
    // and job-search defaults on logout/account deletion.
    try {
      var doomed = [];
      for (var i = 0; i < window.localStorage.length; i += 1) {
        var key = window.localStorage.key(i);
        if (
          key === "cv.editor.activeId" ||
          key === "cv.editor.job_source_defaults" ||
          (key && key.indexOf("cv.editor.history::") === 0) ||
          (key && key.indexOf("cv.editor.collapsed::") === 0)
        ) {
          doomed.push(key);
        }
      }
      doomed.forEach(function (key) { window.localStorage.removeItem(key); });
    } catch (_) {}
  }

  // ── 1. Patch fetch IMMEDIATELY (synchronously, before app.js runs) so no
  //       /api/* request can ever go out without the bearer token. ──────────
  var nativeFetch = window.fetch.bind(window);
  function isApiRequest(url) {
    try {
      var u = new URL(url, window.location.origin);
      return u.origin === window.location.origin && u.pathname.indexOf("/api/") === 0;
    } catch (_) {
      return typeof url === "string" && url.indexOf("/api/") === 0;
    }
  }
  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : input && input.url;
    if (session && session.access_token && isApiRequest(url)) {
      init = init || {};
      var headers = new Headers(init.headers || (typeof input !== "string" && input.headers) || {});
      if (!headers.has("Authorization")) {
        headers.set("Authorization", "Bearer " + session.access_token);
      }
      init.headers = headers;
    }
    return nativeFetch(input, init);
  };

  // ── DOM helpers ───────────────────────────────────────────────────────────
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "class") node.className = attrs[k];
        else if (k === "html") node.innerHTML = attrs[k];
        else node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  function injectStyles() {
    if (document.getElementById("onepager-auth-style")) return;
    var css = [
      "#auth-gate{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;",
      "background:var(--bg,#16130f);color:var(--fg,#f3f0e9);font-family:system-ui,-apple-system,Segoe UI,sans-serif;}",
      "#auth-gate .auth-card{width:min(380px,calc(100vw - 32px));background:rgba(255,255,255,0.04);",
      "border:1px solid rgba(255,255,255,0.10);border-radius:16px;padding:28px 26px;box-shadow:0 20px 60px rgba(0,0,0,0.45);}",
      "#auth-gate h1{font-size:18px;margin:0 0 4px;letter-spacing:-0.01em;}",
      "#auth-gate p.sub{margin:0 0 20px;font-size:13px;opacity:0.65;line-height:1.5;}",
      "#auth-gate label{display:block;font-size:12px;opacity:0.7;margin:0 0 6px;}",
      "#auth-gate input[type=email]{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:10px;",
      "border:1px solid rgba(255,255,255,0.14);background:rgba(0,0,0,0.25);color:inherit;font-size:14px;outline:none;}",
      "#auth-gate input[type=email]:focus{border-color:#5e6ad2;}",
      "#auth-gate button{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:10px;border:0;",
      "font-size:14px;font-weight:600;cursor:pointer;margin-top:10px;transition:filter .15s;}",
      "#auth-gate button:hover{filter:brightness(1.08);}",
      "#auth-gate button:disabled{opacity:0.55;cursor:default;}",
      "#auth-gate .btn-primary{background:#5e6ad2;color:#fff;}",
      "#auth-gate .btn-oauth{background:rgba(255,255,255,0.08);color:inherit;border:1px solid rgba(255,255,255,0.14);}",
      /* DEV-ONLY demo button — visually distinct (dashed) so it reads as a sandbox affordance */
      "#auth-gate .btn-demo{background:rgba(140,224,168,0.10);color:#8ce0a8;border:1px dashed rgba(140,224,168,0.45);}",
      "#auth-gate .auth-divider{display:flex;align-items:center;gap:10px;margin:16px 0 4px;opacity:0.45;font-size:11px;text-transform:uppercase;letter-spacing:0.08em;}",
      "#auth-gate .auth-divider::before,#auth-gate .auth-divider::after{content:'';flex:1;height:1px;background:rgba(255,255,255,0.14);}",
      "#auth-gate .auth-msg{margin-top:14px;font-size:13px;line-height:1.5;}",
      "#auth-gate .auth-msg.err{color:#ff8c8c;}",
      "#auth-gate .auth-msg.ok{color:#8ce0a8;}",
      "#auth-gate .auth-check{text-align:center;}",
      "#auth-gate .auth-check .big{font-size:32px;margin-bottom:10px;}",
      "#auth-gate a.link{color:#9aa6ff;cursor:pointer;text-decoration:underline;}",
      /* logout control injected into the topbar */
      "#logout-btn{margin-left:2px;}",
    ].join("");
    document.head.appendChild(el("style", { id: "onepager-auth-style", html: css }));
  }

  // ── 3. Login view ──────────────────────────────────────────────────────────
  function renderLogin(opts) {
    opts = opts || {};
    injectStyles();
    var existing = document.getElementById("auth-gate");
    if (existing) existing.remove();

    var emailInput = el("input", {
      type: "email",
      id: "auth-email",
      placeholder: "you@example.com",
      autocomplete: "email",
    });
    var msg = el("div", { class: "auth-msg", id: "auth-msg" });

    var sendBtn = el("button", { class: "btn-primary", type: "button" }, ["Send magic link"]);
    var googleBtn = el("button", { class: "btn-oauth", type: "button" }, ["Continue with Google"]);
    var githubBtn = el("button", { class: "btn-oauth", type: "button" }, ["Continue with GitHub"]);

    function setMsg(text, kind) {
      msg.className = "auth-msg" + (kind ? " " + kind : "");
      msg.textContent = text;
    }

    sendBtn.addEventListener("click", function () {
      var email = (emailInput.value || "").trim();
      if (!email || email.indexOf("@") < 0) {
        setMsg("Enter a valid email address.", "err");
        return;
      }
      sendBtn.disabled = true;
      setMsg("Sending…", "");
      supa.auth
        .signInWithOtp({
          email: email,
          options: { emailRedirectTo: window.location.origin + "/app" },
        })
        .then(function (res) {
          if (res.error) throw res.error;
          renderCheckEmail(email);
        })
        .catch(function (e) {
          sendBtn.disabled = false;
          setMsg(e.message || "Could not send the magic link.", "err");
        });
    });

    function oauth(provider) {
      return function () {
        setMsg("Redirecting to " + provider + "…", "");
        supa.auth
          .signInWithOAuth({
            provider: provider,
            options: { redirectTo: window.location.origin + "/app" },
          })
          .then(function (res) {
            if (res.error) throw res.error;
          })
          .catch(function (e) {
            // Most common cause: the operator hasn't enabled this provider
            // in the Supabase dashboard yet.
            setMsg(
              (e.message || "OAuth failed.") +
                " (The operator must enable " +
                provider +
                " in the Supabase dashboard.)",
              "err"
            );
          });
      };
    }
    googleBtn.addEventListener("click", oauth("google"));
    githubBtn.addEventListener("click", oauth("github"));

    emailInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") sendBtn.click();
    });

    var cardChildren = opts.localOnly
      ? [
          el("h1", {}, ["CV Copilot — local demo"]),
          el("p", { class: "sub" }, [
            "Zero cloud setup: explore the editor with a local demo session. Documents stay in server-process memory and reset when the server stops.",
          ]),
          msg,
        ]
      : [
          el("h1", {}, ["Sign in to CV Copilot"]),
          el("p", { class: "sub" }, ["One-page CVs, stored to your account. Sign in with a magic link — no password."]),
          el("label", { for: "auth-email" }, ["Email"]),
          emailInput,
          sendBtn,
          el("div", { class: "auth-divider" }, ["or"]),
          googleBtn,
          githubBtn,
          msg,
        ];
    var card = el("div", { class: "auth-card" }, cardChildren);

    // ── DEV-ONLY demo login. Rendered ONLY when /auth/config reported
    //    demo_mode:true. The button posts to the gated /auth/demo-login route,
    //    which performs the password grant server-side and returns a session.
    //    When demo_mode is false this block never runs, so the button never
    //    appears in production. ──────────────────────────────────────────────
    if (opts.demoMode) {
      var demoBtn = el(
        "button",
        { class: "btn-demo", type: "button", id: "auth-demo-btn" },
        ["🧪 Demo login — explore without signing up"]
      );
      demoBtn.addEventListener("click", function () {
        demoBtn.disabled = true;
        setMsg("Signing in to the demo…", "");
        nativeFetch("/auth/demo-login", { method: "POST" })
          .then(function (r) {
            return r.json().then(function (data) {
              return { ok: r.ok, status: r.status, data: data };
            });
          })
          .then(function (res) {
            if (!res.ok || !res.data || !res.data.access_token || !res.data.refresh_token) {
              throw new Error((res.data && res.data.error) || "Demo login failed.");
            }
            if (res.data.local_demo) {
              session = saveLocalDemoSession(res.data);
              window.location.reload();
              return null;
            }
            return supa.auth
              .setSession({
                access_token: res.data.access_token,
                refresh_token: res.data.refresh_token,
              })
              .then(function (out) {
                if (out.error) throw out.error;
                // Reload into the editor; the persisted session boots logged-in.
                window.location.reload();
              });
          })
          .catch(function (e) {
            demoBtn.disabled = false;
            setMsg(e.message || "Demo login failed.", "err");
          });
      });
      card.appendChild(el("div", { class: "auth-divider", style: opts.localOnly ? "display:none" : "" }, ["demo"]));
      card.appendChild(demoBtn);
    }

    var gate = el("div", { id: "auth-gate" }, [card]);
    document.body.appendChild(gate);
    if (opts && opts.error) setMsg(opts.error, "err");
    setTimeout(function () {
      if (opts.localOnly) {
        var demoFocus = document.getElementById("auth-demo-btn");
        if (demoFocus) demoFocus.focus();
        return;
      }
      emailInput.focus();
    }, 50);
  }

  function renderCheckEmail(email) {
    injectStyles();
    var existing = document.getElementById("auth-gate");
    if (existing) existing.remove();
    var back = el("a", { class: "link" }, ["use a different email"]);
    back.addEventListener("click", function () {
      renderLogin();
    });
    var card = el("div", { class: "auth-card auth-check" }, [
      el("div", { class: "big" }, ["✉️"]),
      el("h1", {}, ["Check your email"]),
      el("p", { class: "sub" }, [
        "We sent a magic link to ",
      ]),
    ]);
    // Email + "different email" line, built to avoid HTML injection of email.
    var line = el("p", { class: "sub" });
    line.appendChild(el("strong", {}, [email]));
    line.appendChild(document.createTextNode(". Click it to finish signing in, or "));
    line.appendChild(back);
    line.appendChild(document.createTextNode("."));
    card.appendChild(line);
    document.body.appendChild(el("div", { id: "auth-gate" }, [card]));
  }

  function removeGate() {
    var gate = document.getElementById("auth-gate");
    if (gate) gate.remove();
  }

  // ── 4. Logout control, slotted into the topbar's right cluster ──────────────
  function installLogoutButton() {
    if (document.getElementById("logout-btn")) return;
    if (document.getElementById("ac-logout")) return;
    var right = document.querySelector(".topbar-cluster-global") ||
      document.querySelector(".topbar-right") ||
      document.querySelector(".cl-topbar-right");
    if (!right) return;
    var btn = el(
      "button",
      {
        id: "logout-btn",
        type: "button",
        class: "icon-btn",
        "aria-label": "Sign out",
        "data-tooltip": "Sign out",
      },
      []
    );
    btn.innerHTML = '<span data-icon="log-out" data-icon-size="15">⏻</span>';
    btn.addEventListener("click", function () {
      clearLocalDemoSession();
      clearSensitiveLocalState();
      (supa ? supa.auth.signOut() : Promise.resolve()).finally(function () {
        window.location.reload();
      });
    });
    right.appendChild(btn);
    if (window.renderIcons) window.renderIcons(btn);
  }

  // ── 2/3. Boot: load config, init client, decide login vs editor ─────────────
  function boot() {
    nativeFetch("/auth/config")
      .then(function (r) {
        return r.json();
      })
      .then(function (cfg) {
        // DEV-ONLY: only true when the server has ONEPAGER_DEMO_MODE enabled.
        demoMode = !!cfg.demo_mode;
        var localDemoSession = loadLocalDemoSession();
        if (localDemoSession) {
          session = localDemoSession;
          removeGate();
          installLogoutButton();
          readyResolve(session);
          return null;
        }
        if (!cfg.supabase_url || !cfg.supabase_publishable_key) {
          if (demoMode) {
            // Zero-config local run (fresh clone, no .env): /auth/demo-login
            // falls back to a local demo token — render a demo-only gate.
            renderLogin({ demoMode: true, localOnly: true });
            return null;
          }
          throw new Error("Auth is not configured on the server.");
        }
        if (!window.supabase || !window.supabase.createClient) {
          throw new Error("Supabase JS failed to load.");
        }
        supa = window.supabase.createClient(cfg.supabase_url, cfg.supabase_publishable_key, {
          auth: {
            persistSession: true,
            autoRefreshToken: true,
            detectSessionInUrl: true,
            storage: window.sessionStorage,
          },
        });

        // React to auth state (covers magic-link return via URL hash).
        supa.auth.onAuthStateChange(function (_event, sess) {
          var had = !!session;
          session = sess;
          if (sess && !had) {
            removeGate();
            installLogoutButton();
            readyResolve(sess);
          }
        });

        return supa.auth.getSession();
      })
      .then(function (res) {
        if (session) return;
        if (res && res.data && res.data.session) {
          session = res.data.session;
          removeGate();
          installLogoutButton();
          readyResolve(session);
        } else if (session) {
          // onAuthStateChange already handled it.
          removeGate();
          installLogoutButton();
        } else if (!document.getElementById("auth-gate")) {
          // A gate rendered by an earlier branch (e.g. the zero-config
          // localOnly demo gate) must not be replaced here.
          renderLogin({ demoMode: demoMode });
        }
      })
      .catch(function (e) {
        renderLogin({ error: e.message || String(e), demoMode: demoMode });
      });
  }

  // Expose a tiny API for app.js to await before booting the editor.
  window.OnePagerAuth = {
    ready: readyPromise,
    getSession: function () {
      return session;
    },
    logout: function () {
      clearLocalDemoSession();
      clearSensitiveLocalState();
      return supa ? supa.auth.signOut() : Promise.resolve();
    },
    clearSensitiveLocalState: clearSensitiveLocalState,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
