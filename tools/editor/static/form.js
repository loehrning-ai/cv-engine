/* ============================================================================
   form.js — structured form editor for CV YAML documents.

   Exposes a single global ``window.CvForm`` with ``mount(container, opts)``.
   The form mutates ``opts.model`` directly; on every change ``opts.onChange``
   fires (so the host can debounce a save + preview refresh).

   Schema mirrors ``engine/render/content.py``:

     name, accent, font, photo, contact[], experience[], education[],
     skills[], projects[], leadership[], others[]

   The form does NOT validate — that's the engine's job at build time. We
   keep the UI permissive so users can save half-finished entries.
   ============================================================================ */
(function () {
  "use strict";

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  const NETWORK_OPTIONS = [
    "mail", "phone", "web",
    "linkedin", "github", "gitlab", "x", "mastodon", "bluesky",
    "instagram", "youtube", "telegram", "whatsapp", "reddit",
    "stackoverflow", "leetcode", "orcid", "googlescholar",
    "researchgate", "imdb",
  ];

  const NETWORK_LABEL = {
    mail: "Email", phone: "Phone", web: "Website",
    linkedin: "LinkedIn", github: "GitHub", gitlab: "GitLab", x: "X / Twitter",
    mastodon: "Mastodon", bluesky: "Bluesky", instagram: "Instagram",
    youtube: "YouTube", telegram: "Telegram", whatsapp: "WhatsApp",
    reddit: "Reddit", stackoverflow: "Stack Overflow", leetcode: "LeetCode",
    orcid: "ORCID", googlescholar: "Google Scholar",
    researchgate: "ResearchGate", imdb: "IMDb",
  };

  // Sections in render order (matches engine/render/templates.py).
  const SECTIONS = [
    { key: "header",     label: "Header",     icon: "user" },
    { key: "experience", label: "Experience", icon: "briefcase" },
    { key: "education",  label: "Education",  icon: "graduation-cap" },
    { key: "skills",     label: "Skills",     icon: "wrench" },
    { key: "projects",   label: "Projects",   icon: "lightbulb" },
    { key: "leadership", label: "Leadership", icon: "trophy" },
    { key: "others",     label: "Other",      icon: "more" },
  ];

  // ──────────────────────────────────────────────────────────────────────
  //  MOTION HELPERS — Web Animations API, respect prefers-reduced-motion.
  // ──────────────────────────────────────────────────────────────────────
  const REDUCED_MOTION = matchMedia("(prefers-reduced-motion: reduce)");

  const ENTER_TIMING = { duration: 200, easing: "cubic-bezier(0.16, 1, 0.30, 1)", fill: "both" };
  const EXIT_TIMING  = { duration: 160, easing: "cubic-bezier(0.7, 0, 0.84, 0)",  fill: "both" };
  const STAGGER_MS = 22;
  const STAGGER_CAP = 6;

  function animateCardIn(card, delay = 0) {
    if (REDUCED_MOTION.matches) return Promise.resolve();
    card.style.overflow = "hidden";
    const h = card.getBoundingClientRect().height;
    const a = card.animate(
      [
        { height: "0px",      opacity: 0, transform: "translateY(-3px)" },
        { height: `${h}px`,   opacity: 1, transform: "translateY(0)" },
      ],
      { ...ENTER_TIMING, delay },
    );
    return a.finished
      .catch(() => {})
      .then(() => { card.style.overflow = ""; card.style.height = ""; });
  }

  function animateCardOut(card) {
    if (REDUCED_MOTION.matches) return Promise.resolve();
    const h = card.getBoundingClientRect().height;
    card.style.overflow = "hidden";
    const a = card.animate(
      [
        { height: `${h}px`, opacity: 1, transform: "translateY(0)" },
        { height: "0px",    opacity: 0, transform: "translateY(-2px)" },
      ],
      EXIT_TIMING,
    );
    return a.finished.catch(() => {});
  }

  // ──────────────────────────────────────────────────────────────────────
  //  DOM HELPERS
  // ──────────────────────────────────────────────────────────────────────
  function el(tag, props, ...children) {
    const n = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (k === "class") n.className = v;
        else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
        else if (k.startsWith("on") && typeof v === "function") {
          n.addEventListener(k.slice(2).toLowerCase(), v);
        } else if (k === "html") n.innerHTML = v;
        else if (v === false || v == null) continue;
        else if (v === true) n.setAttribute(k, "");
        else n.setAttribute(k, v);
      }
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return n;
  }

  function iconBtn(name, label, onClick, opts = {}) {
    const b = el("button", {
      type: "button",
      class: `form-icon-btn ${opts.tone || ""}`,
      "aria-label": label,
      title: label,
      // When an i18n key is provided, OnePagerI18n.apply(document) re-localises
      // the label live on every language switch (app.js re-applies on
      // "cv:i18n-changed"); the `label` arg is the already-resolved initial text.
      "data-i18n-attr": opts.i18n ? `aria-label:${opts.i18n},title:${opts.i18n}` : null,
      onClick,
    });
    b.innerHTML = `<span data-icon="${name}" data-icon-size="14"></span>`;
    if (window.renderIcons) window.renderIcons(b);
    return b;
  }

  // Resolve an i18n string by key, falling back to the English default. Safe
  // before OnePagerI18n has loaded (returns the fallback) — mirrors app.js's tr().
  function tr(key, fallback) {
    if (window.OnePagerI18n && key) {
      const v = window.OnePagerI18n.t(key);
      if (v && v !== key) return v;
    }
    return fallback != null ? fallback : key;
  }

  function autosizeTextarea(node, opts = {}) {
    if (!node) return;
    const maxRows = opts.maxRows || 7;
    const line = parseFloat(getComputedStyle(node).lineHeight) || 20;
    const padding =
      (parseFloat(getComputedStyle(node).paddingTop) || 0) +
      (parseFloat(getComputedStyle(node).paddingBottom) || 0);
    node.style.height = "auto";
    node.style.maxHeight = `${Math.round(line * maxRows + padding)}px`;
    node.style.height = `${Math.min(node.scrollHeight, line * maxRows + padding)}px`;
    node.style.overflowY = node.scrollHeight > line * maxRows + padding + 1 ? "auto" : "hidden";
  }

  function wireAutosizeTextarea(node, opts = {}) {
    if (!node) return node;
    node.classList.add("is-autosize");
    const resize = () => autosizeTextarea(node, opts);
    node.addEventListener("input", resize);
    requestAnimationFrame(resize);
    return node;
  }

  function notifyPolishControlsRendered() {
    window.dispatchEvent(new CustomEvent("cv:polish-controls-rendered"));
  }

  function field(label, type, value, onChange, opts = {}) {
    const id = `f-${Math.random().toString(36).slice(2, 8)}`;
    const startEmpty = value == null || value === "";
    const required = !!opts.required;
    const inputAttrs = {
      id,
      type,
      value: value == null ? "" : String(value),
      // opts.placeholderKey routes the placeholder through the i18n table (and
      // lets OnePagerI18n.apply re-localise it live on a language switch).
      placeholder: opts.placeholderKey ? tr(opts.placeholderKey, opts.placeholder) : (opts.placeholder || ""),
      "data-i18n-attr": opts.placeholderKey ? `placeholder:${opts.placeholderKey}` : null,
      spellcheck: opts.spellcheck === false ? "false" : "true",
      list: opts.list || null,
      oninput: (e) => {
        onChange(e.target.value);
        if (e.target.tagName === "TEXTAREA") autosizeTextarea(e.target, opts);
        if (required) {
          const empty = !e.target.value.trim();
          e.target.toggleAttribute("aria-invalid", empty);
          e.target.toggleAttribute("data-empty", empty);
        }
      },
    };
    if (required) {
      inputAttrs["data-required"] = "true";
      if (startEmpty) {
        // data-empty drives the visual "unfilled required" hint, but do NOT
        // mark a PRISTINE field aria-invalid — a screen reader would announce
        // every unfilled required field as "invalid" on load. The input handler
        // toggles aria-invalid only after the user actually edits the field.
        inputAttrs["data-empty"] = "true";
      }
    }
    const input =
      type === "textarea"
        ? el("textarea", {
            ...inputAttrs,
            rows: opts.rows || 3,
          })
        : el("input", inputAttrs);
    if (type === "textarea") {
      input.removeAttribute("type");
      input.value = value == null ? "" : String(value);
      wireAutosizeTextarea(input, opts);
    }
    return el(
      "label",
      { class: `form-field ${opts.compact ? "form-field-compact" : ""}` },
      el("span", { class: "form-field-label" }, label, opts.required ? el("span", { class: "form-field-required" }, " *") : null),
      input,
      opts.hint ? el("span", { class: "form-field-hint" }, opts.hint) : null,
    );
  }

  function selectField(label, value, options, onChange, opts = {}) {
    const sel = el(
      "select",
      { onchange: (e) => onChange(e.target.value) },
      ...options.map((o) => {
        const [val, lbl] = Array.isArray(o) ? o : [o, o];
        const node = el("option", { value: val }, lbl);
        if (val === value) node.selected = true;
        return node;
      }),
    );
    return el(
      "label",
      { class: `form-field ${opts.compact ? "form-field-compact" : ""}` },
      el("span", { class: "form-field-label" }, label),
      sel,
    );
  }

  function listSection(opts) {
    // opts: { key, label, icon, items, blank, render, model, onChange,
    //         eyebrow, singular }
    const { key, label, items, blank, render, onChange, eyebrow } = opts;
    const singular = opts.singular || label.toLowerCase().replace(/s$/, "");
    const list = el("div", { class: "form-list", id: `form-list-${key}` });
    const countLabel = (n) => `${n} ${n === 1 ? "item" : "items"}`;
    const countEl = el("span", {
      class: "form-metric form-section-count",
      "data-metric": "count",
    }, countLabel(items.length));

    function emptyState() {
      const empty = el(
        "div",
        { class: "form-empty" },
        el("span", { class: "form-empty-glyph", "data-icon": "plus-circle", "data-icon-size": "16" }),
        el("p", { class: "form-empty-title" }, `No ${label.toLowerCase()} yet`),
        el(
          "p",
          { class: "form-empty-sub", html:
            `Click <em>Add ${escapeHtml(singular)}</em> below — every entry becomes one block on your CV.`
          },
        ),
      );
      if (window.renderIcons) window.renderIcons(empty);
      return empty;
    }

    function makeCard(item, idx) {
      const card = el(
        "div",
        { class: "form-card", "data-idx": String(idx) },
        el(
          "div",
          { class: "form-card-actions" },
          iconBtn("chevron-up", tr("form.move_up", "Move up"), () => {
            // Capture the ITEM's live position, not the DOM index: a pending
            // Remove animation can leave data-idx stale for ~160ms.
            const i = items.indexOf(item);
            if (i <= 0) return;
            const prev = card.previousElementSibling;
            // Bail when the sibling is mid-removal: swapping past a card that
            // is about to disappear would desync the DOM order from `items`.
            if (prev && prev.dataset.removing) return;
            [items[i - 1], items[i]] = [items[i], items[i - 1]];
            // Swap DOM nodes without animating to keep ordering snappy.
            if (prev && prev.classList.contains("form-card")) list.insertBefore(card, prev);
            reindex();
            onChange();
          }, { i18n: "form.move_up" }),
          iconBtn("chevron-down", tr("form.move_down", "Move down"), () => {
            const i = items.indexOf(item);
            if (i < 0 || i >= items.length - 1) return;
            const next = card.nextElementSibling;
            if (next && next.dataset.removing) return;
            [items[i + 1], items[i]] = [items[i], items[i + 1]];
            if (next && next.classList.contains("form-card")) list.insertBefore(next, card);
            reindex();
            onChange();
          }, { i18n: "form.move_down" }),
          iconBtn(
            "trash",
            tr("form.remove", "Remove"),
            async () => {
              // Capture the item's live position, not the DOM index, and
              // disable this card's action buttons BEFORE the first await:
              // the exit animation leaves a ~160ms window in which a second
              // click (double-click, or Remove on another card) would splice
              // a stale index and delete the wrong entry.
              const i = items.indexOf(item);
              if (i < 0) return;
              // Mark the card as mid-removal so the Move handlers bail while
              // its exit animation is still in the DOM (they would otherwise
              // swap past a doomed sibling and desync the DOM from `items`).
              card.dataset.removing = "1";
              $$(".form-icon-btn", card).forEach((b) => { b.disabled = true; });
              // Capture a focus destination BEFORE removing so a keyboard/screen-
              // reader user isn't dumped onto <body> when the card (and the
              // Remove button they activated) disappears.
              const neighbor = card.nextElementSibling || card.previousElementSibling;
              items.splice(i, 1);
              await animateCardOut(card);
              card.remove();
              reindex();
              countEl.textContent = countLabel(items.length);
              if (!items.length) {
                const empty = emptyState();
                list.appendChild(empty);
                animateCardIn(empty);
              }
              const dest = neighbor && document.contains(neighbor) ? neighbor : list;
              const sel = "button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex]";
              let focusable =
                (dest.matches && dest.matches(sel) && dest) ||
                (dest.querySelector && dest.querySelector(sel));
              if (!focusable) {
                // Deleted the LAST card: the list now holds only the empty-state
                // (no focusable control). Make it programmatically focusable so
                // focus lands on the "no items" message rather than falling to
                // <body> — a screen reader then announces the empty state.
                const fallback = list.querySelector(".form-empty") || list;
                fallback.setAttribute("tabindex", "-1");
                focusable = fallback;
              }
              if (focusable && focusable.focus) focusable.focus({ preventScroll: true });
              onChange();
            },
            { tone: "danger", i18n: "form.remove" },
          ),
        ),
        render(item, () => onChange()),
      );
      if (window.renderIcons) window.renderIcons(card);
      return card;
    }

    function reindex() {
      $$(".form-card", list).forEach((c, i) => { c.dataset.idx = String(i); });
    }

    function rebuild() {
      list.innerHTML = "";
      if (!items.length) {
        list.appendChild(emptyState());
        countEl.textContent = countLabel(0);
        return;
      }
      items.forEach((item, idx) => list.appendChild(makeCard(item, idx)));
      countEl.textContent = countLabel(items.length);
    }

    rebuild();

    const addBtn = el(
      "button",
      {
        type: "button",
        class: "form-add-btn",
        onClick: () => {
          // Drop the empty-state DOM if it's there.
          const empty = list.querySelector(".form-empty");
          if (empty) empty.remove();
          const newItem = structuredClone(blank);
          items.push(newItem);
          const card = makeCard(newItem, items.length - 1);
          list.appendChild(card);
          countEl.textContent = countLabel(items.length);
          animateCardIn(card).then(() => {
            const firstInput = card.querySelector("input, textarea, select");
            if (firstInput) firstInput.focus();
          });
          onChange();
        },
      },
      el("span", { "data-icon": "plus-circle", "data-icon-size": "13" }),
      el("span", null, `Add ${singular}`),
    );
    if (window.renderIcons) window.renderIcons(addBtn);

    const eyebrowText = eyebrow || label;
    const head = el(
      "header",
      {
        class: "form-section-head is-collapsible",
        "data-eyebrow": eyebrowText,
        role: "button",
        tabindex: "0",
        "aria-expanded": "true",
      },
      el(
        "div",
        { class: "form-section-titlebar" },
        el("h3", { class: "form-section-title" }, label),
        el(
          "span",
          { class: "form-section-metrics", "aria-label": tr("form.section_metrics", "Section metrics"), "data-i18n-attr": "aria-label:form.section_metrics" },
        countEl,
        // Per-section completeness hint (Stage 7). Populated by app.js's
        // refreshSectionHints() from the required-field validation; hidden
        // until there's something to nudge about.
          el("span", { class: "form-metric form-section-hint", "data-metric": "hint", hidden: "" }),
          el("button", {
            type: "button",
            class: "form-metric form-section-ats",
            "data-metric": "ats",
            "aria-label": tr("form.section_ats_aria", "ATS advisory for this section"),
            "data-i18n-attr": "aria-label:form.section_ats_aria",
            hidden: "",
          }),
        el("span", { class: "form-section-chevron", "data-icon": "chevron-down", "data-icon-size": "14" }),
        ),
      ),
    );
    const notice = opts.notice
      ? el("p", { class: "form-section-notice" }, opts.notice)
      : null;
    const body = el("div", { class: "form-section-body" }, notice, list, addBtn);

    function toggle() {
      // aria-expanded="true" → currently expanded, so flip to collapsed.
      const isExpanded = head.getAttribute("aria-expanded") !== "false";
      const next = isExpanded; // we're collapsing if it WAS expanded
      head.setAttribute("aria-expanded", next ? "false" : "true");
      body.toggleAttribute("hidden", next);
      window.dispatchEvent(new CustomEvent("cv:section-collapsed", {
        detail: { key, collapsed: next },
      }));
    }
    head.addEventListener("click", (e) => {
      // Don't toggle when clicking on inputs inside the head (none today,
      // but defensive against future changes).
      if (e.target.closest("input, textarea, select, button")) return;
      toggle();
    });
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });

    return el(
      "section",
      {
        class: "form-section",
        id: `form-section-${key}`,
        "data-section-key": key,
        // Hover-sync (Stage 3): cross-highlight this section's twin in the
        // preview. app.js listens and scrolls-if-needed — it never refreshes.
        onmouseenter: () => emitSectionHover(key, true),
        onmouseleave: () => emitSectionHover(key, false),
      },
      head,
      body,
    );
  }

  /** Tell the host (app.js) the pointer entered/left a form section. */
  function emitSectionHover(key, hovering) {
    window.dispatchEvent(new CustomEvent("cv:section-hover", {
      detail: { sectionKey: key, hovering },
    }));
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ──────────────────────────────────────────────────────────────────────
  //  SECTION RENDERERS
  // ──────────────────────────────────────────────────────────────────────
  function renderHeaderSection(model, onChange) {
    // Appearance (accent / font / density) lives in the topbar — keep
    // the form section minimal. Just name + photo here.
    const grid = el(
      "div",
      { class: "form-grid form-grid-2" },
      field(
        "Full name",
        "text",
        model.name,
        (v) => { model.name = v; onChange(); },
        { required: true, placeholder: "Alex Hartman" },
      ),
      photoDropZone(model, onChange),
    );
    const layoutGrid = el(
      "div",
      { class: "form-grid form-grid-3" },
      selectField(
        "Contact layout",
        model.contact_layout || "inline",
        [["inline", "Inline"], ["stack", "Stacked"], ["grid", "Grid"]],
        (v) => { model.contact_layout = v; onChange(); },
        { compact: true },
      ),
      selectField(
        "Header layout",
        model.header_layout || "center",
        [["center", "Centered"], ["left", "Left aligned"], ["split", "Split"]],
        (v) => { model.header_layout = v; onChange(); },
        { compact: true },
      ),
      selectField(
        "Photo layout",
        model.photo_layout || "right",
        [["right", "Right circle"], ["left", "Left circle"], ["square", "Right square"], ["hidden", "Hidden"]],
        (v) => { model.photo_layout = v; onChange(); },
        { compact: true },
      ),
    );

    const head = el(
      "header",
      {
        class: "form-section-head is-collapsible",
        "data-eyebrow": "Identity",
        role: "button",
        tabindex: "0",
        "aria-expanded": "true",
      },
      el(
        "div",
        { class: "form-section-titlebar" },
        el("h3", { class: "form-section-title" }, "Header"),
        el(
          "span",
          { class: "form-section-metrics", "aria-label": tr("form.section_metrics", "Section metrics"), "data-i18n-attr": "aria-label:form.section_metrics" },
          el("span", { class: "form-metric form-section-hint", "data-metric": "hint", hidden: "" }),
          el("button", {
            type: "button",
            class: "form-metric form-section-ats",
            "data-metric": "ats",
            "aria-label": tr("form.section_ats_aria", "ATS advisory for this section"),
            "data-i18n-attr": "aria-label:form.section_ats_aria",
            hidden: "",
          }),
          el("span", { class: "form-section-chevron", "data-icon": "chevron-down", "data-icon-size": "14" }),
        ),
      ),
    );
    const body = el("div", { class: "form-section-body" }, grid, layoutGrid, contactSection(model, onChange));
    function toggle() {
      const collapsed = head.getAttribute("aria-expanded") === "false";
      const next = !collapsed;
      head.setAttribute("aria-expanded", next ? "false" : "true");
      body.toggleAttribute("hidden", next);
      window.dispatchEvent(new CustomEvent("cv:section-collapsed", {
        detail: { key: "header", collapsed: next },
      }));
    }
    head.addEventListener("click", (e) => {
      if (e.target.closest("input, textarea, select, button")) return;
      toggle();
    });
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });

    return el(
      "section",
      {
        class: "form-section",
        id: "form-section-header",
        "data-section-key": "header",
        onmouseenter: () => emitSectionHover("header", true),
        onmouseleave: () => emitSectionHover("header", false),
      },
      head,
      body,
    );
  }

  function contactSection(model, onChange) {
    if (!Array.isArray(model.contact)) model.contact = [];
    const items = model.contact;

    return listSection({
      key: "contact",
      label: "Contact",
      eyebrow: "Reach me",
      singular: "contact",
      items,
      blank: { network: "linkedin", username: "" },
      model,
      onChange,
      render: (item, fire) =>
        el(
          "div",
          { class: "form-grid form-grid-3" },
          selectField(
            "Network",
            item.network || "",
            [["", "— plain (no icon)"]].concat(NETWORK_OPTIONS.map((n) => [n, NETWORK_LABEL[n] || n])),
            (v) => { item.network = v; fire(); },
            { compact: true },
          ),
          field(
            "Username / value",
            "text",
            item.username || "",
            (v) => { item.username = v; fire(); },
            { compact: true, placeholder: "you@example.com or your-handle", spellcheck: false },
          ),
          field(
            "Display label (optional)",
            "text",
            item.label || "",
            (v) => { item.label = v; fire(); },
            { compact: true, placeholder: "GitHub" },
          ),
          field(
            "Custom href (optional)",
            "text",
            item.href || "",
            (v) => { item.href = v; fire(); },
            { compact: true, placeholder: "auto-resolved from username", placeholderKey: "form.custom_href_placeholder", spellcheck: false },
          ),
        ),
    });
  }

  // ──────────────────────────────────────────────────────────────────────
  //  PHOTO DROP-ZONE — drag a JPG/PNG anywhere on this label, or click
  //  "Choose file…" to open a picker. The chosen file is uploaded to
  //  /api/asset/photo, which saves it as design/photos/<owner>/photo.<ext>
  //  and returns the relative path. We then patch model.photo + fire onChange
  //  so the YAML picks it up.
  // ──────────────────────────────────────────────────────────────────────
  function photoDropZone(model, onChange) {
    const wrap = el("label", { class: "form-field photo-drop" });
    wrap.appendChild(el("span", { class: "form-field-label" }, "Photo (optional)"));

    const zone = el("div", {
      class: "photo-drop-zone" + (model.photo ? " has-photo" : ""),
      tabindex: "0",
      // A NAMED, focusable drag-drop target with an Enter/Space keyboard
      // alternative — but deliberately NOT role="button": the zone WRAPS its own
      // interactive controls (the "choose a file" button when empty, "Remove"
      // when a photo is set), and role="button" is a children-presentational
      // widget that would hide those nested buttons from assistive tech. The
      // aria-label names the region; the inner buttons remain the primary,
      // fully-exposed keyboard path.
      "aria-label": tr("form.photo_drop_aria", "Profile photo — drag an image here, or use the button below to choose a file"),
      "data-i18n-attr": "aria-label:form.photo_drop_aria",
    });
    const fileInput = el("input", {
      type: "file",
      accept: "image/jpeg,image/png,image/webp",
      hidden: true,
    });
    const status = el("div", { class: "photo-drop-status" });
    let thumbUrl = null;

    function clearThumbUrl() {
      if (thumbUrl) {
        URL.revokeObjectURL(thumbUrl);
        thumbUrl = null;
      }
    }

    async function loadPhotoThumb(img, photoPath) {
      const name = (photoPath || "").split(/[\\/]/).pop();
      if (!name) return;
      try {
        const res = await fetch(`/api/asset/photo/${encodeURIComponent(name)}?t=${Date.now()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        clearThumbUrl();
        thumbUrl = URL.createObjectURL(blob);
        img.src = thumbUrl;
      } catch (_) {
        status.textContent = "Photo unavailable";
        status.classList.add("is-error");
      }
    }

    function refreshUi() {
      zone.innerHTML = "";
      zone.appendChild(fileInput);
      if (model.photo) {
        zone.classList.add("has-photo");
        const img = el("img", {
          class: "photo-thumb",
          alt: "",
        });
        const meta = el("div", { class: "photo-meta" },
          el("span", { class: "photo-path" }, model.photo),
          el("button", {
            type: "button",
            class: "photo-remove",
            "data-i18n": "form.remove",
            onClick: async (e) => {
              e.preventDefault();
              try {
                const res = await fetch("/api/asset/photo", { method: "DELETE" });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
                clearThumbUrl();
                model.photo = "";
                status.textContent = "";
                status.classList.remove("is-error");
                onChange();
                refreshUi();
              } catch (err) {
                status.textContent = `Remove failed: ${err.message}`;
                status.classList.add("is-error");
              }
            },
          }, tr("form.remove", "Remove")),
        );
        zone.appendChild(img);
        zone.appendChild(meta);
        loadPhotoThumb(img, model.photo);
      } else {
        clearThumbUrl();
        zone.classList.remove("has-photo");
        const inner = el("div", { class: "photo-drop-empty" },
          el("span", { class: "photo-drop-icon", "data-icon": "user", "data-icon-size": "20" }),
          el("span", { class: "photo-drop-title" }, "Drop a JPG, PNG, or WebP here"),
          el("span", { class: "photo-drop-sub" },
            "or ",
            el("button", {
              type: "button",
              class: "photo-drop-pick",
              onClick: (e) => { e.preventDefault(); fileInput.click(); },
            }, "choose a file"),
          ),
        );
        zone.appendChild(inner);
        if (window.renderIcons) window.renderIcons(inner);
      }
      zone.appendChild(status);
    }

    async function handleFile(file) {
      if (!file) return;
      status.textContent = "Uploading…";
      status.classList.remove("is-error");
      const fd = new FormData();
      fd.append("file", file);
      try {
        const res = await fetch("/api/asset/photo", { method: "POST", body: fd });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        model.photo = data.path;
        status.textContent = "";
        onChange();
        refreshUi();
      } catch (err) {
        status.textContent = `Upload failed: ${err.message}`;
        status.classList.add("is-error");
      }
    }

    fileInput.addEventListener("change", (e) => {
      const file = e.target.files && e.target.files[0];
      if (file) handleFile(file);
    });
    ["dragenter", "dragover"].forEach((ev) =>
      zone.addEventListener(ev, (e) => {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.add("is-dragging");
      }),
    );
    ["dragleave", "drop"].forEach((ev) =>
      zone.addEventListener(ev, (e) => {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.remove("is-dragging");
      }),
    );
    zone.addEventListener("drop", (e) => {
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) handleFile(file);
    });
    zone.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
    });

    refreshUi();
    wrap.appendChild(zone);
    return wrap;
  }

  // ──────────────────────────────────────────────────────────────────────
  //  SHAPE_RENDERERS — shape → DOM builder.
  //
  //  Each renderer takes a SectionDef (from /api/schema) plus the model
  //  and onChange callback, and returns a complete <section> DOM tree.
  //  Internally they delegate to listSection() with a per-item render
  //  function.
  //
  //  # 🤖 ADD-A-SHAPE-HERE
  //  To add a new visual shape: add a function below, register it in
  //  SHAPE_RENDERERS at the bottom of this block, and add the matching
  //  Python renderer in engine/render/templates.py:_PER_ITEM_RENDERERS.
  // ──────────────────────────────────────────────────────────────────────

  function experienceShape(section, model, onChange) {
    const key = section.key;
    if (!Array.isArray(model[key])) model[key] = [];
    return listSection({
      key,
      label: section.label,
      eyebrow: section.eyebrow,
      singular: section.singular,
      items: model[key],
      blank: { role: "", company: "", location: "", start: "", end: "Present", bullets: [], stack: "" },
      model,
      onChange,
      render: (item, fire) => {
        return el(
          "div",
          { class: "form-card-grid" },
          el(
            "div",
            { class: "form-grid form-grid-2" },
            field("Role", "text", item.role, (v) => { item.role = v; fire(); }, {
              compact: true, placeholder: "Senior Software Engineer", required: true,
            }),
            field("Company", "text", item.company, (v) => { item.company = v; fire(); }, {
              compact: true, placeholder: "Acme Corp", required: true,
            }),
            field("Location", "text", item.location, (v) => { item.location = v; fire(); }, {
              compact: true, placeholder: "Berlin, Germany",
            }),
            field("Start", "text", item.start, (v) => { item.start = v; fire(); }, {
              compact: true, placeholder: "02/2024", required: true,
            }),
            field("End", "text", item.end, (v) => { item.end = v; fire(); }, {
              compact: true, placeholder: "Present", required: true,
            }),
            field("Stack (optional)", "text", item.stack, (v) => { item.stack = v; fire(); }, {
              compact: true, placeholder: "Python, SQL, AWS",
            }),
          ),
          bulletEditor(item, fire),
        );
      },
    });
  }

  /**
   * Per-bullet editor: each bullet gets its own input with reorder
   * arrows, char counter, and (when AI extract is available) a small
   * polish button. Replaces the multi-line textarea.
   */
  function bulletEditor(item, fire) {
    if (!Array.isArray(item.bullets)) item.bullets = [];
    const wrap = el("div", { class: "bullets-editor" });
    const polishBtn = el(
      "button",
      {
        type: "button",
        class: "section-polish",
        "data-tooltip": "Tighten all bullets in this role with Claude",
        onClick: (e) => {
          e.preventDefault();
          window.dispatchEvent(new CustomEvent("cv:polish-section", {
            detail: { item, fire, button: polishBtn },
          }));
        },
      },
      el("span", { "data-icon": "sparkles", "data-icon-size": "11" }),
      el("span", null, "Polish all"),
    );
    wrap.appendChild(el(
      "div",
      { class: "bullets-editor-head" },
      el("span", { class: "form-field-label" }, "Bullets"),
      el("span", { class: "form-field-hint" }, "**bold** · *italic* · `code` are supported"),
      polishBtn,
    ));
    const list = el("div", { class: "bullets-list" });
    wrap.appendChild(list);

    function rebuild() {
      list.innerHTML = "";
      item.bullets.forEach((text, idx) => list.appendChild(makeRow(text, idx)));
      list.appendChild(addRow());
      if (window.renderIcons) window.renderIcons(list);
      notifyPolishControlsRendered();
    }

    function makeRow(text, idx) {
      const input = el("textarea", {
        class: "bullet-input",
        rows: 2,
        placeholder: "Verb + what + impact (numbers > adjectives)",
        // Placeholder is NOT a label — give each bullet a programmatic name so
        // a screen reader announces "Bullet 1", "Bullet 2" rather than an
        // anonymous edit box.
        "aria-label": "Bullet point " + (idx + 1),
        spellcheck: "true",
      });
      input.value = text;
      wireAutosizeTextarea(input, { maxRows: 5 });
      input.addEventListener("input", () => {
        item.bullets[idx] = input.value;
        counter.textContent = `${input.value.length}`;
        counter.classList.toggle("is-warn", input.value.length > 180);
        fire();
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          item.bullets.splice(idx + 1, 0, "");
          rebuild();
          fire();
          // Focus the new row's input.
          const rows = list.querySelectorAll(".bullet-row .bullet-input");
          if (rows[idx + 1]) rows[idx + 1].focus();
        } else if (e.key === "Backspace" && input.value === "" && item.bullets.length > 1) {
          e.preventDefault();
          item.bullets.splice(idx, 1);
          rebuild();
          fire();
          const rows = list.querySelectorAll(".bullet-row .bullet-input");
          (rows[idx - 1] || rows[0])?.focus();
        }
      });
      const counter = el("span", { class: "bullet-count" }, String(text.length));
      if (text.length > 180) counter.classList.add("is-warn");

      const actions = el(
        "div",
        { class: "bullet-actions" },
        iconBtn("chevron-up", tr("form.move_up", "Move up"), () => {
          if (idx === 0) return;
          [item.bullets[idx - 1], item.bullets[idx]] = [item.bullets[idx], item.bullets[idx - 1]];
          rebuild();
          fire();
        }, { i18n: "form.move_up" }),
        iconBtn("chevron-down", tr("form.move_down", "Move down"), () => {
          if (idx === item.bullets.length - 1) return;
          [item.bullets[idx + 1], item.bullets[idx]] = [item.bullets[idx], item.bullets[idx + 1]];
          rebuild();
          fire();
        }, { i18n: "form.move_down" }),
        // AI polish (★) button — handler is attached lazily by app.js
        // when /api/extract/status reports Claude available. The button
        // is always rendered; it's a no-op until wired.
        el("button", {
          type: "button",
          class: "bullet-polish",
          "aria-label": "Tighten with Claude",
          "data-tooltip": "Tighten this bullet with Claude (✨)",
          "data-bullet-action": "polish",
          onClick: (e) => {
            e.preventDefault();
            const detail = { input, value: input.value, setValue: (v) => {
              input.value = v;
              item.bullets[idx] = v;
              counter.textContent = `${v.length}`;
              counter.classList.toggle("is-warn", v.length > 180);
              autosizeTextarea(input, { maxRows: 5 });
              fire();
            }};
            window.dispatchEvent(new CustomEvent("cv:polish-bullet", { detail }));
          },
        },
          el("span", { "data-icon": "sparkles", "data-icon-size": "12" }),
        ),
        iconBtn(
          "trash",
          "Remove bullet",
          () => {
            item.bullets.splice(idx, 1);
            if (!item.bullets.length) item.bullets.push("");
            rebuild();
            fire();
          },
          { tone: "danger" },
        ),
      );

      return el(
        "div",
        { class: "bullet-row" },
        input,
        counter,
        actions,
      );
    }

    function addRow() {
      const btn = el("button", {
        type: "button",
        class: "bullet-add-btn",
        onClick: () => {
          item.bullets.push("");
          rebuild();
          fire();
          const rows = list.querySelectorAll(".bullet-row .bullet-input");
          rows[rows.length - 1]?.focus();
        },
      },
        el("span", { "data-icon": "plus-circle", "data-icon-size": "12" }),
        el("span", null, "Add bullet"),
      );
      return btn;
    }

    if (item.bullets.length === 0) item.bullets.push("");
    rebuild();
    return wrap;
  }

  function educationShape(section, model, onChange) {
    const key = section.key;
    if (!Array.isArray(model[key])) model[key] = [];
    return listSection({
      key,
      label: section.label,
      eyebrow: section.eyebrow,
      singular: section.singular,
      items: model[key],
      blank: { degree: "", school: "", location: "", start: "", end: "", note: "" },
      model,
      onChange,
      render: (item, fire) =>
        el(
          "div",
          { class: "form-grid form-grid-2" },
          field("Degree", "text", item.degree, (v) => { item.degree = v; fire(); }, {
            compact: true, placeholder: "M.Sc. Computer Science", required: true,
          }),
          field("School", "text", item.school, (v) => { item.school = v; fire(); }, {
            compact: true, placeholder: "FAU Erlangen-Nuremberg", required: true,
          }),
          field("Location", "text", item.location, (v) => { item.location = v; fire(); }, {
            compact: true, placeholder: "Erlangen, Germany",
          }),
          field("Start", "text", item.start, (v) => { item.start = v; fire(); }, {
            compact: true, placeholder: "10/2018", required: true,
          }),
          field("End", "text", item.end, (v) => { item.end = v; fire(); }, {
            compact: true, placeholder: "07/2022", required: true,
          }),
          field("Note (optional)", "textarea", item.note, (v) => { item.note = v; fire(); }, {
            compact: true, rows: 2, maxRows: 5, placeholder: "Graduated with distinction.",
          }),
        ),
    });
  }

  function skillsShape(section, model, onChange) {
    const key = section.key;
    if (!Array.isArray(model[key])) model[key] = [];
    return listSection({
      key,
      label: section.label,
      eyebrow: section.eyebrow,
      singular: section.singular,
      items: model[key],
      blank: { label: "", items: "" },
      model,
      onChange,
      render: (item, fire) =>
        el(
          "div",
          { class: "form-card-grid" },
          field("Category", "text", item.label, (v) => { item.label = v; fire(); }, {
            compact: true, placeholder: "Languages", required: true,
          }),
          chipsField("Items", item.items, (v) => { item.items = v; fire(); }, {
            placeholder: "Type a skill, press Enter…",
          }),
        ),
    });
  }

  /**
   * Chip-list editor for a comma-separated string field.
   *
   * Stores the value as the same comma-separated string the rest of the
   * pipeline expects (so the YAML document shape doesn't change). Renders each
   * comma-split token as a removable pill, plus a "Type and press Enter"
   * input at the end.
   */
  function chipsField(labelText, value, onChange, opts = {}) {
    function parse(s) {
      return String(s || "")
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean);
    }
    function serialise(arr) {
      return arr.join(", ");
    }

    let items = parse(value);
    const wrap = el("label", { class: "form-field" });
    wrap.appendChild(el("span", { class: "form-field-label" }, labelText));
    const box = el("div", { class: "chips-box" });
    const input = el("input", {
      type: "text",
      class: "chips-input",
      placeholder: opts.placeholder || "",
      spellcheck: "true",
    });

    function rebuild() {
      box.innerHTML = "";
      items.forEach((it, idx) => {
        const chip = el(
          "span",
          { class: "chip" },
          el("span", { class: "chip-text" }, it),
          el("button", {
            type: "button",
            class: "chip-x",
            "aria-label": `Remove ${it}`,
            onClick: () => {
              items.splice(idx, 1);
              rebuild();
              onChange(serialise(items));
            },
          }, "×"),
        );
        box.appendChild(chip);
      });
      box.appendChild(input);
    }

    function commit() {
      const v = input.value.trim().replace(/,+$/, "").trim();
      if (!v) return;
      // Split on commas in case the user pasted a list.
      v.split(",").map((s) => s.trim()).filter(Boolean).forEach((t) => items.push(t));
      input.value = "";
      rebuild();
      onChange(serialise(items));
      input.focus();
    }

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        commit();
      } else if (e.key === "Backspace" && input.value === "" && items.length) {
        e.preventDefault();
        items.pop();
        rebuild();
        onChange(serialise(items));
      }
    });
    input.addEventListener("blur", () => {
      if (input.value.trim()) commit();
    });

    rebuild();
    wrap.appendChild(box);
    return wrap;
  }

  function publicationShape(section, model, onChange) {
    const key = section.key;
    if (!Array.isArray(model[key])) model[key] = [];
    return listSection({
      key,
      label: section.label,
      eyebrow: section.eyebrow,
      singular: section.singular,
      items: model[key],
      blank: { title: "", authors: "", venue: "", date: "", doi: "", url: "" },
      model,
      onChange,
      render: (item, fire) =>
        el(
          "div",
          { class: "form-card-grid" },
          field("Title", "textarea", item.title, (v) => { item.title = v; fire(); }, {
            compact: true, rows: 2, maxRows: 4, placeholder: "Paper / talk title",
          }),
          field("Authors (you first if applicable)", "textarea", item.authors, (v) => { item.authors = v; fire(); }, {
            compact: true, rows: 2, maxRows: 4, placeholder: "A. Author, B. Author, C. Hartman",
          }),
          el(
            "div",
            { class: "form-grid form-grid-2" },
            field("Venue", "text", item.venue, (v) => { item.venue = v; fire(); }, {
              compact: true, placeholder: "ICLR · Nature · Preprint",
            }),
            field("Date", "text", item.date, (v) => { item.date = v; fire(); }, {
              compact: true, placeholder: "04/2024",
            }),
            field("DOI (optional)", "text", item.doi, (v) => { item.doi = v; fire(); }, {
              compact: true, placeholder: "10.5555/abc",
            }),
            field("URL (optional)", "text", item.url, (v) => { item.url = v; fire(); }, {
              compact: true, placeholder: "https://…",
            }),
          ),
        ),
    });
  }

  function compactShape(section, model, onChange) {
    return _compactSectionImpl(section.key, section.label, model, onChange, section.eyebrow, section.singular);
  }

  // The compact-row renderer used by projects/leadership/others/etc.
  function _compactSectionImpl(key, label, model, onChange, eyebrow, singular) {
    if (!Array.isArray(model[key])) model[key] = [];
    return listSection({
      key,
      label,
      eyebrow,
      singular: singular || label.toLowerCase().replace(/s$/, ""),
      items: model[key],
      blank: { title: "", date: "", desc: "" },
      model,
      onChange,
      render: (item, fire) =>
        el(
          "div",
          { class: "form-card-grid" },
          el(
            "div",
            { class: "form-grid form-grid-21" },
            field("Title", "text", item.title, (v) => { item.title = v; fire(); }, {
              compact: true, placeholder: "OSS contribution", required: true,
            }),
            field("Date", "text", item.date, (v) => { item.date = v; fire(); }, {
              compact: true, placeholder: "08/2023",
            }),
          ),
          field("Description", "textarea", item.desc, (v) => { item.desc = v; fire(); }, {
            compact: true, rows: 3, maxRows: 7, placeholder: "Short description with concrete scope and impact.",
          }),
        ),
    });
  }

  function schemaBlank(fields, fallback) {
    if (!Array.isArray(fields) || !fields.length) return fallback;
    const out = {};
    fields.forEach((f) => {
      if (!f || !f.name) return;
      out[f.name] = f.type === "list" || f.type === "chips" ? [] : "";
    });
    return out;
  }

  function renderSchemaFields(section, item, fire) {
    const fields = Array.isArray(section.fields) ? section.fields : [];
    if (!fields.length) return null;
    return el(
      "div",
      { class: "form-card-grid" },
      el(
        "div",
        { class: "form-grid form-grid-21" },
        ...fields.filter((f) => f.type !== "textarea" && f.type !== "list" && f.type !== "chips").map((f) =>
          field(f.label || f.name, "text", item[f.name], (v) => { item[f.name] = v; fire(); }, {
            compact: true,
            placeholder: f.placeholder || "",
            required: !!f.required,
            hint: f.hint || "",
          })
        ),
      ),
      ...fields.filter((f) => f.type === "textarea").map((f) =>
        field(f.label || f.name, "textarea", item[f.name], (v) => { item[f.name] = v; fire(); }, {
          compact: true,
          rows: f.rows || 3,
          maxRows: f.maxRows || 7,
          placeholder: f.placeholder || "",
          required: !!f.required,
          hint: f.hint || "",
        })
      ),
      ...fields.filter((f) => f.type === "chips").map((f) =>
        chipsField(f.label || f.name, item[f.name], (next) => { item[f.name] = next; fire(); })
      ),
    );
  }

  function schemaDrivenCompactShape(section, model, onChange) {
    if (!Array.isArray(section.fields) || !section.fields.length) return compactShape(section, model, onChange);
    if (!Array.isArray(model[section.key])) model[section.key] = [];
    const blank = schemaBlank(section.fields, { title: "", date: "", desc: "" });
    return listSection({
      key: section.key,
      label: section.label,
      eyebrow: section.eyebrow,
      singular: section.singular || "entry",
      items: model[section.key],
      blank,
      model,
      onChange,
      render: (item, fire) => renderSchemaFields(section, item, fire),
      notice: section.custom
        ? "Custom sections are stored in YAML and rendered when list-shaped; unsupported extra fields are preserved but may not appear in the PDF."
        : "",
    });
  }

  // The shape registry — keys must match engine/render/sections.py shapes.
  const SHAPE_RENDERERS = {
    experience:  experienceShape,
    education:   educationShape,
    skills:      skillsShape,
    compact:     schemaDrivenCompactShape,
    publication: publicationShape,
  };

  // Fallback schema used only if /api/schema is unreachable. Mirrors the
  // Python registry's default for offline robustness.
  const FALLBACK_SCHEMA = [
    { key: "experience", label: "Experience", eyebrow: "Where you've worked", singular: "role",     shape: "experience" },
    { key: "education",  label: "Education",  eyebrow: "Where you studied",   singular: "degree",   shape: "education" },
    { key: "skills",     label: "Skills",     eyebrow: "The toolkit",         singular: "category", shape: "skills" },
    { key: "projects",   label: "Projects",   eyebrow: "Things you built",    singular: "project",  shape: "compact" },
    { key: "leadership", label: "Leadership", eyebrow: "How you led",         singular: "entry",    shape: "compact" },
    { key: "others",     label: "Other",      eyebrow: "Awards & extras",     singular: "entry",    shape: "compact" },
  ];

  // Top-level YAML keys that are NOT sections. Anything else with a list
  // value gets exposed as a custom section in the form + outline.
  const RESERVED_TOP_LEVEL = new Set([
    "name", "contact", "accent", "font", "density", "template", "paper", "photo",
    "section_order", "optional_sections", "contact_layout", "header_layout", "photo_layout",
  ]);

  function orderedSectionDefs(model, defs) {
    const byKey = new Map(defs.map((d) => [d.key, d]));
    const seen = new Set();
    const out = [];
    if (Array.isArray(model.section_order)) {
      model.section_order.forEach((key) => {
        if (byKey.has(key) && !seen.has(key)) {
          out.push(byKey.get(key));
          seen.add(key);
        }
      });
    }
    defs.forEach((def) => {
      if (!seen.has(def.key)) out.push(def);
    });
    model.section_order = out.filter((def) => !def.custom).map((def) => def.key);
    return out;
  }

  function renderStructureSection(model, schema, onChange) {
    const order = orderedSectionDefs(model, schema).filter((def) => !def.custom);
    const list = el("div", { class: "form-list form-structure-list" });
    const optionalKeys = new Set(Array.isArray(model.optional_sections) ? model.optional_sections : []);
    model.optional_sections = Array.from(optionalKeys);

    function syncOrder() {
      model.section_order = $$(".form-structure-row", list).map((row) => row.dataset.key);
    }

    function makeRow(def, idx) {
      const row = el(
        "div",
        { class: "form-card form-structure-row", "data-key": def.key, "data-idx": String(idx) },
        el("div", { class: "form-card-actions" },
          iconBtn("chevron-up", "Move section up", () => {
            if (!row.previousElementSibling) return;
            list.insertBefore(row, row.previousElementSibling);
            syncOrder();
            onChange();
          }),
          iconBtn("chevron-down", "Move section down", () => {
            if (!row.nextElementSibling) return;
            list.insertBefore(row.nextElementSibling, row);
            syncOrder();
            onChange();
          }),
        ),
        el("div", { class: "form-structure-main" },
          el("strong", null, def.label),
          el("span", { class: "form-field-hint" }, def.key),
        ),
      );
      return row;
    }

    order.forEach((def, idx) => list.appendChild(makeRow(def, idx)));

    const activeSectionKeys = new Set(order.map((def) => def.key));
    const presetButtons = schema
      .filter((def) => ["projects", "leadership", "others"].indexOf(def.key) !== -1)
      .filter((def) => !activeSectionKeys.has(def.key))
      .map((def) => el("button", {
        type: "button",
        class: "form-add-btn",
        onClick: () => {
          if (!Array.isArray(model[def.key])) model[def.key] = [];
          if (!model[def.key].length) {
            model[def.key].push(schemaBlank(def.fields, { title: "", date: "", desc: "" }));
          }
          if (model.optional_sections.indexOf(def.key) === -1) model.optional_sections.push(def.key);
          onChange();
        },
      }, el("span", { "data-icon": "plus-circle", "data-icon-size": "13" }), el("span", null, `Add ${def.label}`)));

    const head = el(
      "header",
      { class: "form-section-head", "data-eyebrow": "Document model" },
      el("div", { class: "form-section-titlebar" },
        el("h3", { class: "form-section-title" }, "Structure"),
        el("span", { class: "form-section-metrics" },
          el("span", { class: "form-metric form-section-count" }, `${order.length} sections`),
        ),
      ),
    );
    const body = el("div", { class: "form-section-body" },
      el("p", { class: "form-section-notice" }, "Section order and optional presets are saved in YAML and rendered when supported by the template."),
      list,
      presetButtons.length ? el("div", { class: "form-structure-presets" }, presetButtons) : null,
    );
    return el("section", { class: "form-section", id: "form-section-structure", "data-section-key": "structure" }, head, body);
  }

  /** Heuristic: pick the closest shape from the first item's keys. */
  function detectShape(items) {
    if (!Array.isArray(items) || !items.length) return "compact";
    const sample = items[0];
    if (!sample || typeof sample !== "object") return "compact";
    const keys = new Set(Object.keys(sample));
    if (keys.has("role") && keys.has("company")) return "experience";
    if (keys.has("degree") && keys.has("school")) return "education";
    if (keys.has("label") && keys.has("items") && keys.size <= 3) return "skills";
    if (keys.has("authors") || (keys.has("venue") && keys.has("title"))) return "publication";
    return "compact";
  }

  /** Turn ``speaking_engagements`` → ``Speaking Engagements`` (with English small-words rule). */
  function humanizeKey(key) {
    const SMALL = new Set(["and", "of", "the", "to", "in", "on", "at", "for"]);
    const cleaned = String(key).replace(/[_-]+/g, " ").trim();
    if (!cleaned) return key;
    return cleaned.split(/\s+/).map((w, i) => {
      if (i === 0) return w[0].toUpperCase() + w.slice(1).toLowerCase();
      return SMALL.has(w.toLowerCase()) ? w.toLowerCase() : w[0].toUpperCase() + w.slice(1).toLowerCase();
    }).join(" ");
  }

  /** Detect custom sections in the model that aren't in the registered schema. */
  function detectCustomSections(model, registeredKeys) {
    const out = [];
    let counter = 100;
    for (const [key, value] of Object.entries(model || {})) {
      if (RESERVED_TOP_LEVEL.has(key)) continue;
      if (registeredKeys.has(key)) continue;
      if (!Array.isArray(value)) continue;
      out.push({
        key,
        label: humanizeKey(key),
        eyebrow: `Custom · ${humanizeKey(key)}`,
        singular: humanizeKey(key).replace(/s$/, "").toLowerCase() || "entry",
        shape: detectShape(value),
        custom: true,
        // Sentinel "high index" so they appear after registered sections
        // but maintain insertion order among themselves.
        _order: counter++,
      });
    }
    return out;
  }

  // ──────────────────────────────────────────────────────────────────────
  //  PUBLIC API
  // ──────────────────────────────────────────────────────────────────────
  function mount(container, opts) {
    const { model, onChange } = opts;
    let schema = Array.isArray(opts.schema) && opts.schema.length ? opts.schema : FALLBACK_SCHEMA;
    if (!model.contact) model.contact = [];

    function rebuild() {
      container.innerHTML = "";
      const registeredKeys = new Set(schema.map((s) => s.key));
      const customDefs = detectCustomSections(model, registeredKeys);
      const allDefs = orderedSectionDefs(model, schema).concat(customDefs);

      const sectionEls = [];
      for (const def of allDefs) {
        const renderer = SHAPE_RENDERERS[def.shape] || compactShape;
        sectionEls.push(renderer(def, model, onChange));
      }
      const root = el(
        "div",
        { class: "form-root" },
        renderHeaderSection(model, onChange),
        renderStructureSection(model, schema, onChange),
        ...sectionEls,
      );
      container.appendChild(root);
      // Stagger entrance: each .form-section gets a --i index used by the
      // CSS animation-delay calculator. Cap so the cascade never exceeds
      // ~200 ms of perceived latency.
      $$(".form-section", container).forEach((s, i) => {
        s.style.setProperty("--i", String(Math.min(i, 5)));
      });
      if (window.renderIcons) window.renderIcons(container);
      notifyPolishControlsRendered();
      window.dispatchEvent(new CustomEvent("cv:form-rebuilt"));
    }

    function setAtsAdvisories(sections) {
      const bySection = sections && typeof sections === "object" ? sections : {};
      $$(".form-section[data-section-key]", container).forEach((section) => {
        const key = section.dataset.sectionKey;
        const slot = section.querySelector(".form-section-ats");
        if (!slot) return;
        const items = Array.isArray(bySection[key]) ? bySection[key] : [];
        if (!items.length) {
          slot.setAttribute("hidden", "");
          slot.textContent = "";
          slot.removeAttribute("data-tone");
          slot.removeAttribute("data-stale");
          return;
        }
        const stale = items.some((item) => item && item.stale);
        const gapCount = items.filter((item) => item && item.kind === "gap").length;
        const suggestionCount = Math.max(0, items.length - gapCount);
        const label = gapCount
          ? `${gapCount} ATS ${gapCount === 1 ? "gap" : "gaps"}`
          : `${suggestionCount} ATS ${suggestionCount === 1 ? "tip" : "tips"}`;
        slot.removeAttribute("hidden");
        slot.textContent = stale ? `${label} · saved CV` : label;
        slot.dataset.tone = gapCount ? "gap" : "suggestion";
        if (stale) slot.setAttribute("data-stale", "true");
        else slot.removeAttribute("data-stale");
        slot.setAttribute("data-tooltip", items[0].text || "ATS advisory based on the linked job");
        slot.onclick = () => {
          window.dispatchEvent(new CustomEvent("cv:ats-advisory-open", {
            detail: { sectionKey: key },
          }));
        };
      });
      if (window.renderIcons) window.renderIcons(container);
    }

    rebuild();

    return {
      rebuild,
      setAtsAdvisories,
      setSchema(next) {
        if (Array.isArray(next) && next.length) {
          schema = next;
          rebuild();
        }
      },
      scrollTo(slug, opts = {}) {
        const target = $(`#form-section-${slug}`, container);
        if (!target) return false;
        const behavior = opts.behavior || "smooth";
        const block = opts.block || "start";
        const ifNeeded = opts.ifNeeded === true;
        const host = container;
        const targetRect = target.getBoundingClientRect();
        const hostRect = host.getBoundingClientRect();
        const pad = 12;
        const above = targetRect.top < hostRect.top + pad;
        const below = targetRect.bottom > hostRect.bottom - pad;
        if (ifNeeded && !above && !below) return false;
        if (host && typeof host.scrollTo === "function") {
          const delta = block === "nearest" && below
            ? targetRect.bottom - (hostRect.bottom - pad)
            : targetRect.top - (hostRect.top + pad);
          const maxScroll = Math.max(0, host.scrollHeight - host.clientHeight);
          const top = Math.max(0, Math.min(maxScroll, host.scrollTop + delta));
          try {
            host.scrollTo({ top, behavior });
          } catch (_) {
            host.scrollTop = top;
          }
        } else {
          target.scrollIntoView({ behavior, block });
        }
        return true;
      },
    };
  }

  // ──────────────────────────────────────────────────────────────────────
  //  YAML SERIALISATION
  // ──────────────────────────────────────────────────────────────────────
  // Strip undefined / empty arrays / empty strings before YAML.dump so the
  // serialised document stays clean.
  function pruneEmpty(value) {
    if (Array.isArray(value)) {
      const out = value.map(pruneEmpty).filter((v) => v !== undefined);
      return out;
    }
    if (value && typeof value === "object") {
      const out = {};
      for (const [k, v] of Object.entries(value)) {
        const cleaned = pruneEmpty(v);
        if (cleaned === undefined) continue;
        if (cleaned === "" && k !== "label" && k !== "username") continue;
        if (Array.isArray(cleaned) && cleaned.length === 0) continue;
        out[k] = cleaned;
      }
      return out;
    }
    return value;
  }

  const HEADER_COMMENT = `# CV source — single source of truth for the renderer.
# Edit any field, save, and the editor preview re-renders within ~200ms.
# A4 hard-constrained to one page; the build fails if it overflows.
`;

  function modelToYaml(model) {
    const cleaned = pruneEmpty(model);
    // Use flow-style for the contact list entries (reads as one-liners) but
    // block-style for the section arrays. js-yaml's `flowLevel` is the
    // simplest way to control that.
    const body = jsyaml.dump(cleaned, {
      lineWidth: 120,
      noRefs: true,
      sortKeys: false,
    });
    return HEADER_COMMENT + "\n" + body;
  }

  function yamlToModel(text) {
    // CORE_SCHEMA keeps bool/int/float coercion but treats YAML timestamps
    // as plain strings. The default schema resolves `2024-01-01` into a Date,
    // which a later dump serialises as `2024-01-01T00:00:00.000Z` — silent
    // corruption of date fields on every form-driven save.
    const parsed = jsyaml.load(text, { schema: jsyaml.CORE_SCHEMA });
    if (parsed == null) return {};
    if (typeof parsed !== "object") {
      throw new Error("YAML root must be a mapping.");
    }
    return parsed;
  }

  // ──────────────────────────────────────────────────────────────────────
  //  EXPORT
  // ──────────────────────────────────────────────────────────────────────
  window.CvForm = {
    mount,
    modelToYaml,
    yamlToModel,
    detectCustomSections,
    detectShape,
    humanizeKey,
    SECTIONS,
    NETWORK_OPTIONS,
    NETWORK_LABEL,
  };
})();
