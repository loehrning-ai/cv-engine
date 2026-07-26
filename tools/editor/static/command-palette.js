/* ============================================================================
   command-palette.js — ⌘K / Ctrl-K command palette (Stage 9 · discoverability).

   A small, framework-free overlay shared by EVERY editor page (index, jobs,
   tailor, cover-letters, account). It surfaces the app's main destinations and
   actions so nothing is hidden behind an unlabelled icon:

     • Go to: CV editor, Jobs catalog, Cover letters, ATS score & review, Account
     • Actions: Import a resume*, New CV*, Toggle light/dark (* editor-only hooks)

   Design / accessibility:
     • role="dialog" + aria-modal, labelled by its own heading;
     • opens on ⌘K / Ctrl-K (and the "/" key when not typing), ESC closes;
     • focus is trapped inside the dialog and restored to the opener on close;
     • full keyboard nav (↑/↓ to move, Enter to run, type to filter);
     • respects prefers-reduced-motion (CSS handles the entrance animation, and
       a global guard in style.css zeroes transitions/animations);
     • theme-aware — styled entirely with the existing CSS variable tokens.

   Wiring:
     • The module is self-contained and auto-injects its DOM + ⌘K listener on
       load. It reads localised labels from window.OnePagerI18n when present and
       falls back to built-in English otherwise.
     • Page-specific actions (e.g. the editor's "Import" modal and "New CV") are
       contributed by that page via window.OnePagerPalette.register({...}) — the
       palette stays generic and never reaches into another page's internals.

   Exposed as window.OnePagerPalette.
   ============================================================================ */
(function () {
  "use strict";

  // ── i18n + icon helpers (defensive: both modules may be absent in tests) ──
  function t(key, fallback) {
    if (window.OnePagerI18n && typeof window.OnePagerI18n.t === "function") {
      var v = window.OnePagerI18n.t(key);
      if (v != null && v !== key) return v;
    }
    return fallback;
  }

  function iconSvg(name, size) {
    if (window.ICONS && typeof window.ICONS === "object") {
      // Reuse the shared renderer via a throwaway element so we get the exact
      // same <svg> wrapper / stroke as the rest of the chrome.
      var span = document.createElement("span");
      span.setAttribute("data-icon", name);
      span.setAttribute("data-icon-size", String(size || 15));
      if (window.renderIcons) window.renderIcons(span);
      return span.innerHTML || "";
    }
    return "";
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Which page are we on? Used to mark the current destination + decide which
  // built-in navigation entries to offer (don't offer "go here" for here).
  function currentPath() {
    var p = (window.location && window.location.pathname) || "/";
    return p;
  }

  // ── Action registry ───────────────────────────────────────────────────────
  // Each action: { id, group, labelKey, label, icon, run(), available()? }.
  // Built-in navigation + theme always exist; pages add their own via register.
  var registered = [];
  function register(action) {
    if (!action || !action.id || typeof action.run !== "function") return;
    // De-dupe by id so a page re-registering (hot path / reload) is idempotent.
    registered = registered.filter(function (a) {
      return a.id !== action.id;
    });
    registered.push(action);
    if (built) rebuildList();
  }

  function go(path) {
    return function () {
      if (window.OnePagerNavigate) return window.OnePagerNavigate(path);
      window.location.href = path;
    };
  }

  function builtinActions() {
    var here = currentPath();
    var nav = [
      {
        id: "nav-editor",
        group: "navigate",
        labelKey: "palette.act.editor",
        label: "CV editor",
        aliases: ["app", "resume", "cv", "document"],
        icon: "file-text",
        run: go("/app"),
        available: function () {
          return here !== "/app";
        },
      },
      {
        id: "nav-jobs",
        group: "navigate",
        labelKey: "palette.act.jobs",
        label: "Jobs catalog",
        aliases: ["jobs", "job search", "applications", "catalog"],
        icon: "briefcase",
        run: go("/jobs"),
        available: function () {
          return here !== "/jobs";
        },
      },
      {
        id: "nav-letters",
        group: "navigate",
        labelKey: "palette.act.letters",
        label: "Cover letters",
        aliases: ["cover", "letter", "letters", "application letter"],
        icon: "mail",
        run: go("/cover-letters"),
        available: function () {
          return here !== "/cover-letters";
        },
      },
      {
        id: "nav-tailor",
        group: "navigate",
        labelKey: "palette.act.tailor",
        label: "ATS score & review",
        aliases: ["tailor", "tailoring", "ats", "score", "review", "prüfung"],
        icon: "scissors",
        run: go("/tailor"),
        available: function () {
          return here !== "/tailor";
        },
      },
      {
        id: "nav-account",
        group: "navigate",
        labelKey: "palette.act.account",
        label: "Settings & account",
        aliases: ["settings", "account", "profile", "konto", "einstellungen"],
        icon: "settings",
        run: go("/account#settings"),
        available: function () {
          return here !== "/account" && here !== "/settings";
        },
      },
    ];

    var theme = {
      id: "act-theme",
      group: "actions",
      labelKey: "palette.act.theme",
      label: "App theme settings",
      aliases: ["settings", "appearance", "theme", "dark", "light"],
      icon: "settings",
      run: function () {
        if (window.OnePagerNavigate) window.OnePagerNavigate("/account#settings");
        else window.location.href = "/account#settings";
      },
    };

    return nav.concat([theme]);
  }

  // The full, filterable action list (built-ins + page-registered), keeping
  // only the ones whose `available()` (if any) returns true.
  function allActions() {
    return builtinActions()
      .concat(registered)
      .filter(function (a) {
        return typeof a.available !== "function" || a.available();
      });
  }

  function actionLabel(a) {
    return t(a.labelKey, a.label || a.id);
  }

  // ── DOM ─────────────────────────────────────────────────────────────────
  var built = false;
  var overlay, dialog, input, listEl, emptyEl;
  var isOpen = false;
  var opener = null; // element focus is returned to on close
  var rows = []; // current rendered { el, action } in display order
  var activeIndex = -1;

  function build() {
    if (built) return;
    built = true;

    overlay = document.createElement("div");
    overlay.className = "cmdk";
    overlay.id = "cmdk";
    overlay.setAttribute("hidden", "");

    dialog = document.createElement("div");
    dialog.className = "cmdk-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "cmdk-title");

    var head = document.createElement("div");
    head.className = "cmdk-head";
    head.innerHTML =
      '<span class="cmdk-search-icon" aria-hidden="true">' +
      iconSvg("target", 15) +
      "</span>";

    input = document.createElement("input");
    input.className = "cmdk-input";
    input.type = "text";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "true");
    input.setAttribute("aria-controls", "cmdk-list");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("spellcheck", "false");
    input.setAttribute("aria-label", t("palette.title", "Commands"));
    input.placeholder = t("palette.placeholder", "Search commands…");
    head.appendChild(input);

    // Visually-hidden heading for the dialog's accessible name.
    var title = document.createElement("h2");
    title.id = "cmdk-title";
    title.className = "cmdk-title-sr";
    title.textContent = t("palette.title", "Commands");

    listEl = document.createElement("div");
    listEl.className = "cmdk-list";
    listEl.id = "cmdk-list";
    listEl.setAttribute("role", "listbox");

    emptyEl = document.createElement("div");
    emptyEl.className = "cmdk-empty";
    emptyEl.hidden = true;
    emptyEl.textContent = t("palette.empty", "No matching commands");

    var foot = document.createElement("div");
    foot.className = "cmdk-foot";
    foot.innerHTML =
      '<span class="cmdk-hint"><kbd>↑</kbd><kbd>↓</kbd> ' +
      esc(t("palette.hint.move", "move")) +
      '</span>' +
      '<span class="cmdk-hint"><kbd>↵</kbd> ' +
      esc(t("palette.hint.run", "select")) +
      '</span>' +
      '<span class="cmdk-hint"><kbd>esc</kbd> ' +
      esc(t("palette.hint.close", "close")) +
      '</span>';

    dialog.appendChild(head);
    dialog.appendChild(title);
    dialog.appendChild(listEl);
    dialog.appendChild(emptyEl);
    dialog.appendChild(foot);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);

    // Clicking the scrim (but not the dialog) closes.
    overlay.addEventListener("mousedown", function (e) {
      if (e.target === overlay) close();
    });

    input.addEventListener("input", function () {
      rebuildList();
    });
    input.addEventListener("keydown", onKeyInDialog);
  }

  // Group order + localised labels for section headers.
  var GROUPS = [
    { key: "recent", labelKey: "palette.group.recent", label: "Recent documents" },
    { key: "navigate", labelKey: "palette.group.navigate", label: "Go to" },
    { key: "actions", labelKey: "palette.group.actions", label: "Actions" },
  ];

  // ──────────────────────────────────────────────────────────────────────
  //  Recent documents (plan 108 stage 4) — the palette is the fastest way
  //  to jump back into a document from ANY page. Sourced lazily from
  //  /api/documents when the palette opens (auth.js patches fetch with the
  //  bearer token), cached for 60s. A failed fetch degrades to "no recent
  //  group" with a console.debug — navigation/actions keep working.
  // ──────────────────────────────────────────────────────────────────────
  var RECENT_LIMIT = 5;
  var RECENT_TTL_MS = 60000;
  var recentCache = { at: 0, loading: false };

  function docRecency(doc) {
    return String(doc.last_used_at || doc.updated_at || doc.created_at || "");
  }

  function docRoute(doc) {
    var id = encodeURIComponent(doc.id);
    var type = doc.type || doc.kind || "cv";
    return type === "cover_letter" ? "/cover-letters?id=" + id : "/app?id=" + id;
  }

  function refreshRecentDocuments() {
    // Never fire the authenticated fetch without a session — an anonymous
    // 401 would be silent console noise, not a usable recents group.
    var auth = window.OnePagerAuth;
    if (auth && typeof auth.getSession === "function" && !auth.getSession()) return;
    var now = Date.now();
    if (recentCache.loading || now - recentCache.at < RECENT_TTL_MS) return;
    recentCache.loading = true;
    fetch("/api/documents")
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        var docs = (data && data.documents) || [];
        docs
          .slice()
          .sort(function (a, b) { return docRecency(b).localeCompare(docRecency(a)); })
          .slice(0, RECENT_LIMIT)
          .forEach(function (doc, i) {
            if (!doc || !doc.id) return;
            var name = doc.name || doc.title || "Untitled";
            register({
              id: "recent-" + i,
              group: "recent",
              label: name,
              aliases: ["recent", "document", name],
              icon: (doc.type || doc.kind) === "cover_letter" ? "mail" : "file-text",
              meta: (doc.type || doc.kind) === "cover_letter"
                ? t("lib.badge.cover", "Cover letter")
                : t("lib.badge.cv", "CV"),
              run: go(docRoute(doc)),
            });
          });
        recentCache.at = Date.now();
      })
      .catch(function (err) {
        // Named degradation: the group is simply absent; commands still work.
        if (window.console && console.debug) console.debug("cmdk recent documents unavailable:", err);
      })
      .then(function () {
        recentCache.loading = false;
      });
  }

  // Wrap the first case-insensitive occurrence of the query inside the
  // visible label so the user sees WHY a row matched (plan 108 stage 4).
  function highlightLabel(label, q) {
    if (!q) return esc(label);
    var idx = label.toLowerCase().indexOf(q);
    if (idx === -1) return esc(label);
    return (
      esc(label.slice(0, idx)) +
      '<mark class="cmdk-match">' + esc(label.slice(idx, idx + q.length)) + "</mark>" +
      esc(label.slice(idx + q.length))
    );
  }

  function matches(action, q) {
    if (!q) return true;
    var haystack = [
      action.id,
      action.label,
      actionLabel(action),
      action.labelKey,
      (action.aliases || []).join(" ")
    ].join(" ").toLowerCase();
    return haystack.indexOf(q) !== -1;
  }

  function rebuildList() {
    if (!built) return;
    var q = (input.value || "").trim().toLowerCase();
    var actions = allActions().filter(function (a) {
      return matches(a, q);
    });

    listEl.innerHTML = "";
    rows = [];

    GROUPS.forEach(function (g) {
      var inGroup = actions.filter(function (a) {
        return (a.group || "actions") === g.key;
      });
      if (!inGroup.length) return;

      var header = document.createElement("div");
      header.className = "cmdk-group";
      header.setAttribute("role", "presentation");
      header.textContent = t(g.labelKey, g.label);
      listEl.appendChild(header);

      inGroup.forEach(function (a) {
        var row = document.createElement("button");
        row.type = "button";
        row.className = "cmdk-item";
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", "false");
        row.id = "cmdk-opt-" + a.id;
        row.innerHTML =
          '<span class="cmdk-item-icon" aria-hidden="true">' +
          iconSvg(a.icon || "arrow-right", 15) +
          "</span>" +
          '<span class="cmdk-item-label">' +
          highlightLabel(actionLabel(a), q) +
          "</span>" +
          (a.meta ? '<span class="cmdk-item-meta">' + esc(a.meta) + "</span>" : "");
        row.addEventListener("click", function () {
          run(a);
        });
        row.addEventListener("mousemove", function () {
          setActive(rows.indexOf(rowEntry));
        });
        listEl.appendChild(row);
        var rowEntry = { el: row, action: a };
        rows.push(rowEntry);
      });
    });

    var none = rows.length === 0;
    emptyEl.hidden = !none;
    listEl.hidden = none;

    // Reset the active row to the first result.
    setActive(rows.length ? 0 : -1);
  }

  function setActive(idx) {
    if (idx < 0) idx = -1;
    if (idx >= rows.length) idx = rows.length - 1;
    activeIndex = idx;
    rows.forEach(function (r, i) {
      var on = i === idx;
      r.el.classList.toggle("is-active", on);
      r.el.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (idx >= 0 && rows[idx]) {
      input.setAttribute("aria-activedescendant", rows[idx].el.id);
      // Keep the active row visible without scrolling the whole page.
      var r = rows[idx].el;
      var top = r.offsetTop;
      var bottom = top + r.offsetHeight;
      if (top < listEl.scrollTop) listEl.scrollTop = top - 6;
      else if (bottom > listEl.scrollTop + listEl.clientHeight)
        listEl.scrollTop = bottom - listEl.clientHeight + 6;
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function move(delta) {
    if (!rows.length) return;
    var idx = activeIndex < 0 ? 0 : activeIndex + delta;
    if (idx < 0) idx = rows.length - 1; // wrap
    if (idx >= rows.length) idx = 0;
    setActive(idx);
  }

  function run(action) {
    close();
    // Defer to the next frame so the close animation/focus-restore settles
    // before a navigation or a modal-open kicks in.
    requestAnimationFrame(function () {
      try {
        Promise.resolve(action.run()).catch(function (err) {
          console.error("Command action failed:", err);
        });
      } catch (err) {
        console.error("Command action failed:", err);
      }
    });
  }

  function onKeyInDialog(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      move(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      move(-1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (activeIndex >= 0 && rows[activeIndex]) run(rows[activeIndex].action);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "Tab") {
      // Focus trap: only the input + rows are focusable; keep focus inside.
      // The simplest robust trap is to keep focus on the input (rows are
      // driven by arrow keys), so swallow Tab.
      e.preventDefault();
    }
  }

  function open() {
    build();
    if (isOpen) return;
    isOpen = true;
    refreshRecentDocuments();
    opener = document.activeElement;
    overlay.removeAttribute("hidden");
    // Force reflow so the entrance transition runs from the hidden state.
    void overlay.offsetWidth;
    overlay.classList.add("is-open");
    document.documentElement.classList.add("cmdk-open");
    input.value = "";
    rebuildList();
    input.focus();
  }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    overlay.classList.remove("is-open");
    document.documentElement.classList.remove("cmdk-open");
    // Hide after the (short) exit transition; reduced-motion makes this ~instant.
    var hide = function () {
      if (!isOpen) overlay.setAttribute("hidden", "");
    };
    setTimeout(hide, 160);
    // Restore focus to whatever opened the palette.
    if (opener && typeof opener.focus === "function") {
      try {
        opener.focus();
      } catch (_) {}
    }
    opener = null;
  }

  function toggle() {
    if (isOpen) close();
    else open();
  }

  // Is the user currently typing in a field? Then "/" must type a slash, not
  // open the palette (⌘K still works everywhere).
  function isTypingTarget(el) {
    if (!el) return false;
    var tag = (el.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return true;
    if (el.isContentEditable) return true;
    if (el.closest && el.closest(".CodeMirror")) return true;
    return false;
  }

  // ── Global hotkey ─────────────────────────────────────────────────────────
  document.addEventListener("keydown", function (e) {
    var meta = e.metaKey || e.ctrlKey;
    if (meta && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      toggle();
      return;
    }
    // "/" opens the palette only when not typing and no modifier is held —
    // a familiar shortcut that never steals a real slash keystroke.
    if (!isOpen && e.key === "/" && !meta && !e.altKey && !isTypingTarget(e.target)) {
      e.preventDefault();
      open();
    }
  });

  // Wire any in-page "open palette" trigger buttons ([data-cmdk-open]).
  function wireTriggers(root) {
    (root || document).querySelectorAll("[data-cmdk-open]").forEach(function (btn) {
      if (btn._cmdkWired) return;
      btn._cmdkWired = true;
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        open();
      });
    });
  }

  function currentRouteLabel(path) {
    if (path === "/jobs") return t("nav.jobs", "Jobs");
    if (path === "/cover-letters") return t("nav.letters", "Cover letters");
    if (path === "/tailor") return t("app.tailor", "ATS score & review");
    if (path === "/account" || path === "/settings") return t("nav.account", "Settings");
    return t("nav.cv_editor", "CV editor");
  }

  function buildMobileUtility() {
    if (document.getElementById("mobile-utility-btn")) return;
    var host = document.querySelector(".topbar-cluster-global") ||
      document.querySelector(".topbar-right") ||
      document.querySelector(".cl-topbar-right");
    if (!host) return;

    var btn = document.createElement("button");
    btn.id = "mobile-utility-btn";
    btn.type = "button";
    btn.className = "icon-btn mobile-utility-btn";
    btn.setAttribute("aria-label", t("mobile.open_utilities", "Open global utilities"));
    btn.setAttribute("data-i18n-attr", "aria-label:mobile.open_utilities,data-tooltip:mobile.utilities");
    btn.setAttribute("aria-haspopup", "dialog");
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-controls", "mobile-utility-popover");
    btn.setAttribute("data-tooltip", t("mobile.utilities", "Utilities"));
    btn.innerHTML = '<span data-icon="more" data-icon-size="16"></span>';

    var pop = document.createElement("div");
    pop.id = "mobile-utility-popover";
    pop.className = "mobile-utility-popover";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", t("mobile.global_utilities", "Global utilities"));
    pop.setAttribute("data-i18n-attr", "aria-label:mobile.global_utilities");
    pop.setAttribute("hidden", "");

	    var path = (window.location && window.location.pathname) || "/app";
	    var settingsRouteActive = path === "/account" || path === "/settings";
	    function settingsHref() {
	      return path === "/settings" ? "/settings#settings" : "/account#settings";
	    }
    var nav = [
      { href: "/app", icon: "file-text", label: t("nav.cv_editor", "CV editor"), key: "nav.cv_editor", active: path === "/app" },
      { href: "/jobs", icon: "briefcase", label: t("nav.jobs", "Jobs"), key: "nav.jobs", active: path === "/jobs" },
      { href: "/cover-letters", icon: "mail", label: t("nav.letters", "Cover letters"), key: "nav.letters", active: path === "/cover-letters" },
      { href: "/tailor", icon: "target", label: t("app.tailor", "ATS score & review"), key: "app.tailor", active: path === "/tailor" },
    ];
    pop.innerHTML =
      '<div class="mobile-utility-head">' +
        '<span class="mobile-utility-kicker" data-i18n="mobile.current">' + esc(t("mobile.current", "Current")) + '</span>' +
        '<strong data-mobile-current-route>' + esc(currentRouteLabel(path)) + '</strong>' +
      '</div>' +
      '<nav class="mobile-utility-list" aria-label="' + esc(t("mobile.destinations", "Global destinations")) + '" data-i18n-attr="aria-label:mobile.destinations">' +
        nav.map(function (item) {
          return '<a class="mobile-utility-row' + (item.active ? ' is-active' : '') + '" href="' + item.href + '" data-mobile-nav="' + item.href + '"' +
            (item.active ? ' aria-current="page"' : '') + '>' +
            '<span data-icon="' + item.icon + '" data-icon-size="15"></span><span data-i18n="' + item.key + '">' + item.label + '</span></a>';
        }).join("") +
      '</nav>' +
      '<div class="mobile-utility-list" role="group" aria-label="' + esc(t("mobile.utilities", "Utilities")) + '" data-i18n-attr="aria-label:mobile.utilities">' +
        '<button type="button" class="mobile-utility-row" data-mobile-action="document-catalog">' +
          '<span data-icon="folder-open" data-icon-size="15"></span><span data-i18n="mobile.document_catalog">' + esc(t("mobile.document_catalog", "Document catalog")) + '</span></button>' +
	        '<button type="button" class="mobile-utility-row' + (settingsRouteActive ? ' is-active' : '') + '" data-mobile-action="settings"' +
	          (settingsRouteActive ? ' aria-current="page"' : '') + '>' +
	          '<span data-icon="settings" data-icon-size="15"></span><span data-i18n="mobile.settings">' + esc(t("mobile.settings", "Settings & account")) + '</span></button>' +
        '<button type="button" class="mobile-utility-row" data-mobile-action="document">' +
          '<span data-icon="file-text" data-icon-size="15"></span><span data-i18n="mobile.document_actions">' + esc(t("mobile.document_actions", "Document actions")) + '</span></button>' +
        '<button type="button" class="mobile-utility-row" data-mobile-action="applications">' +
          '<span data-icon="briefcase" data-icon-size="15"></span><span data-i18n="mobile.applications">' + esc(t("mobile.applications", "Applications")) + '</span></button>' +
        '<button type="button" class="mobile-utility-row" data-mobile-action="ats-detail">' +
          '<span data-icon="target" data-icon-size="15"></span><span data-i18n="mobile.ats_detail">' + esc(t("mobile.ats_detail", "ATS details")) + '</span></button>' +
        '<button type="button" class="mobile-utility-row" data-mobile-action="save">' +
          '<span data-icon="save" data-icon-size="15"></span><span data-i18n="app.save">' + esc(t("app.save", "Save")) + '</span></button>' +
        '<button type="button" class="mobile-utility-row" data-mobile-action="build">' +
          '<span data-icon="file-text" data-icon-size="15"></span><span data-i18n="app.build">' + esc(t("app.build", "Build PDF")) + '</span></button>' +
        '<button type="button" class="mobile-utility-row" data-mobile-action="refresh">' +
          '<span data-icon="refresh-cw" data-icon-size="15"></span><span data-i18n="mobile.refresh_preview">' + esc(t("mobile.refresh_preview", "Refresh preview")) + '</span></button>' +
        '<button type="button" class="mobile-utility-row" data-mobile-action="appearance">' +
          '<span data-icon="palette" data-icon-size="15"></span><span data-i18n="mobile.cv_appearance">' + esc(t("mobile.cv_appearance", "CV Appearance")) + '</span></button>' +
        '<button type="button" class="mobile-utility-row" data-mobile-action="commands">' +
          '<span data-icon="sparkles" data-icon-size="15"></span><span data-i18n="mobile.commands">' + esc(t("mobile.commands", "Commands")) + '</span></button>' +
        '<button type="button" class="mobile-utility-row" data-mobile-action="signout">' +
          '<span data-icon="log-out" data-icon-size="15"></span><span data-i18n="app.sign_out">' + esc(t("app.sign_out", "Sign out")) + '</span></button>' +
        '<div class="mobile-utility-lang" role="group" aria-label="' + esc(t("mobile.language", "Language")) + '">' +
          '<button type="button" data-mobile-lang="en">EN</button>' +
          '<button type="button" data-mobile-lang="de">DE</button>' +
        '</div>' +
      '</div>';

    var isOpen = false;
    var opener = null;

    function syncLang() {
      var active = window.OnePagerI18n && window.OnePagerI18n.getLang ? window.OnePagerI18n.getLang() : "en";
      var current = pop.querySelector("[data-mobile-current-route]");
      if (current) current.textContent = currentRouteLabel(path);
      pop.querySelectorAll("[data-mobile-lang]").forEach(function (langBtn) {
        langBtn.setAttribute("aria-pressed", langBtn.getAttribute("data-mobile-lang") === active ? "true" : "false");
      });
      if (window.OnePagerI18n) window.OnePagerI18n.apply(pop);
    }

    function syncAvailability() {
      function visible(el) {
        if (!el) return false;
        var cs = getComputedStyle(el);
        var rect = el.getBoundingClientRect();
	        return !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" &&
	          rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
      }
      var targets = {
        document: "doc-actions-btn",
        applications: "workflow-btn",
        "ats-detail": "ats-badge",
        save: "save-btn",
        build: "build-btn",
        refresh: "refresh-btn",
        appearance: "theme-btn",
      };
      Object.keys(targets).forEach(function (kind) {
        var row = pop.querySelector('[data-mobile-action="' + kind + '"]');
        if (row) row.hidden = !document.getElementById(targets[kind]);
      });
      var catalog = pop.querySelector('[data-mobile-action="document-catalog"]');
      if (catalog) {
        catalog.hidden = !(window.OnePagerDocumentActions || document.getElementById("cv-switcher"));
      }
      var signout = pop.querySelector('[data-mobile-action="signout"]');
      if (signout) {
        var accountMobileSignout = document.getElementById("ac-mobile-logout");
        signout.hidden = visible(accountMobileSignout) ||
          !(window.OnePagerAuth || document.getElementById("logout-btn") || document.getElementById("ac-logout"));
      }
    }

    function position() {
      var r = btn.getBoundingClientRect();
      var gap = 8;
      var width = Math.min(280, Math.max(220, window.innerWidth - gap * 2));
      var left = Math.min(
        Math.max(gap, r.right - width),
        Math.max(gap, window.innerWidth - width - gap)
      );
      var top = Math.round(r.bottom + 6);
      var maxHeight = Math.max(220, window.innerHeight - top - gap);
      pop.style.width = width + "px";
      pop.style.maxHeight = Math.round(maxHeight) + "px";
      pop.style.top = top + "px";
      pop.style.left = Math.round(left) + "px";
      pop.style.right = "auto";
    }

    function visibleFocusable() {
      return Array.prototype.filter.call(pop.querySelectorAll("a[href], button:not([disabled])"), function (el) {
        var cs = getComputedStyle(el);
        var rect = el.getBoundingClientRect();
        return !el.hidden && cs.display !== "none" && cs.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
      });
    }

    function firstFocusable() {
      return visibleFocusable()[0] || null;
    }

    function openMenu() {
      if (isOpen) return;
      opener = document.activeElement;
      isOpen = true;
      btn.setAttribute("aria-expanded", "true");
      pop.removeAttribute("hidden");
      syncLang();
      syncAvailability();
      position();
      var first = firstFocusable();
      if (first) first.focus();
    }

    function closeMenu(restore) {
      if (!isOpen) return;
      isOpen = false;
      btn.setAttribute("aria-expanded", "false");
      pop.setAttribute("hidden", "");
      if (restore !== false && opener && typeof opener.focus === "function") {
        try { opener.focus(); } catch (_) {}
      }
      opener = null;
    }

    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      isOpen ? closeMenu(true) : openMenu();
    });
    pop.addEventListener("click", function (e) {
      var action = e.target.closest("[data-mobile-action]");
      if (action) {
        e.preventDefault();
        var kind = action.getAttribute("data-mobile-action");
        closeMenu(true);
        var event = new CustomEvent("cv:mobile-utility-action", {
          cancelable: true,
          detail: { action: kind, opener: btn },
        });
        window.dispatchEvent(event);
        if (event.defaultPrevented) return;
        if (kind === "document-catalog") {
          if (window.OnePagerDocumentActions) window.OnePagerDocumentActions.run("open-catalog", { opener: btn });
          else { var catalogBtn = document.getElementById("cv-switcher"); if (catalogBtn) catalogBtn.click(); }
        }
        if (kind === "document") {
          if (window.OnePagerDocumentActions) window.OnePagerDocumentActions.run("open-actions", { opener: btn });
          else { var doc = document.getElementById("doc-actions-btn"); if (doc) doc.click(); }
        }
        if (kind === "applications") { var workflow = document.getElementById("workflow-btn"); if (workflow) workflow.click(); }
        if (kind === "ats-detail") { var ats = document.getElementById("ats-badge"); if (ats) ats.click(); }
        if (kind === "save") { var save = document.getElementById("save-btn"); if (save) save.click(); }
        if (kind === "build") { var build = document.getElementById("build-btn"); if (build) build.click(); }
        if (kind === "refresh") { var refresh = document.getElementById("refresh-btn"); if (refresh) refresh.click(); }
        if (kind === "appearance") { var appearance = document.getElementById("theme-btn"); if (appearance) appearance.click(); }
        if (kind === "commands") open();
	        if (kind === "settings") {
	          var target = settingsHref();
	          if (window.OnePagerNavigate) window.OnePagerNavigate(target);
	          else window.location.href = target;
	        }
        if (kind === "signout") {
          if (window.OnePagerAuth && window.OnePagerAuth.logout) {
            window.OnePagerAuth.logout().finally(function () { window.location.href = "/"; });
          } else {
            var signout = document.getElementById("logout-btn") || document.getElementById("ac-logout");
            if (signout) signout.click();
          }
        }
      }
      var nav = e.target.closest("[data-mobile-nav]");
      if (nav) {
        e.preventDefault();
        closeMenu(false);
        var href = nav.getAttribute("href");
        if (window.OnePagerNavigate) window.OnePagerNavigate(href);
        else window.location.href = href;
      }
      var lang = e.target.closest("[data-mobile-lang]");
      if (lang && window.OnePagerI18n) {
        e.preventDefault();
        window.OnePagerI18n.setLang(lang.getAttribute("data-mobile-lang")).then(function () {
          document.documentElement.setAttribute("lang", window.OnePagerI18n.getLang());
          window.OnePagerI18n.apply(document);
          window.dispatchEvent(new CustomEvent("cv:i18n-changed"));
          syncLang();
        });
      }
    });
    document.addEventListener("click", function (e) {
      if (!isOpen) return;
      if (e.target.closest("#mobile-utility-popover") || e.target.closest("#mobile-utility-btn")) return;
      closeMenu(true);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && isOpen) {
        e.preventDefault();
        closeMenu(true);
      } else if (e.key === "Tab" && isOpen) {
        var focusables = visibleFocusable();
        if (!focusables.length) {
          e.preventDefault();
          btn.focus();
          return;
        }
        var first = focusables[0];
        var last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        } else if (focusables.indexOf(document.activeElement) === -1) {
          e.preventDefault();
          first.focus();
        }
      }
    });
    window.addEventListener("resize", function () { if (isOpen) position(); });

    host.appendChild(btn);
    document.body.appendChild(pop);
    if (window.renderIcons) window.renderIcons(document);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      wireTriggers(document);
      buildMobileUtility();
    });
  } else {
    wireTriggers(document);
    buildMobileUtility();
  }

  window.OnePagerPalette = {
    open: open,
    close: close,
    toggle: toggle,
    register: register,
    wireTriggers: wireTriggers,
  };
})();
