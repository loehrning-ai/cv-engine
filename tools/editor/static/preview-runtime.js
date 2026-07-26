/* ============================================================================
   Preview runtime — the iframe-side glue that talks back to the editor.

   WHY THIS IS AN EXTERNAL FILE (not inline in render_preview.py):
   The editor frames this preview with ``iframe.srcdoc`` (see app.js
   refreshPreview). A srcdoc document INHERITS the parent /app page's
   Content-Security-Policy, whose ``script-src 'self'`` (no 'unsafe-inline')
   blocks any INLINE <script>. An external same-origin script IS allowed by
   ``'self'``, so the paged.js handshake + content-size reporting must live
   here. Loading it from /static keeps the /app CSP strict and untouched.

   It serves BOTH preview modes (read from <html data-preview-mode>):
     • paged       — register a paged.js after-hook that posts the page count
                     (red pill on overflow) + content dimensions, then flips
                     the ``pagedjs-ready`` class so the body becomes visible.
     • continuous  — post content height/width once layout + fonts settle so
                     the editor can size the zoom stage with no inner scroll.

   The iframe is sandboxed with ``allow-scripts`` but WITHOUT
   ``allow-same-origin``. Its effective origin is therefore opaque ("null"),
   which prevents either document from reading the other's DOM. postMessage
   targetOrigin must be "*" for that opaque target; each receiver validates
   the exact source WindowProxy, message type, and bounded payload.
   ============================================================================ */
(function () {
  "use strict";

  var SECTION_KEY = /^[A-Za-z0-9_-]{1,128}$/;
  var OPAQUE_ABOUT_URL = /^about:(?:srcdoc|blank)$/i;
  var HIGHLIGHT_STYLE = [
    '[data-highlighted="true"] {',
    "  outline: 2px solid var(--accent, #0866FF);",
    "  outline-offset: 3px;",
    "  border-radius: 2px;",
    "  transition: outline-color 200ms cubic-bezier(0.32, 0.72, 0, 1),",
    "              background-color 200ms cubic-bezier(0.32, 0.72, 0, 1);",
    "}",
    "@media (prefers-reduced-motion: reduce) {",
    '  [data-highlighted="true"] { transition: none; }',
    "}",
  ].join("\n");

  /*
   * Paged.js 0.4.3 indexes inline stylesheets by ``window.location.href`` and
   * feeds that value to ``new URL(...)``. A sandboxed srcdoc URL
   * (``about:srcdoc``) and the standalone preview's ``blob:`` URL are not
   * hierarchical URL bases. Keep the upstream Paged.js artifact byte-for-byte
   * intact and narrowly normalize only the current preview document URL to the
   * trusted <base> URL injected by app.js. All normal URL behavior and static
   * methods remain delegated to the browser's native constructor.
   */
  function installOpaqueDocumentUrlAdapter() {
    var documentUrl = window.location.href;
    var usesNonHierarchicalUrl =
      OPAQUE_ABOUT_URL.test(documentUrl) || window.location.protocol === "blob:";
    if (!usesNonHierarchicalUrl) return;
    var NativeURL = window.URL;
    if (typeof NativeURL !== "function") return;

    function isPreviewDocumentUrl(value) {
      return OPAQUE_ABOUT_URL.test(value) || value === documentUrl;
    }

    function PreviewURL(input, base) {
      var normalizedInput = String(input);
      var normalizedBase = base === undefined ? undefined : String(base);
      if (isPreviewDocumentUrl(normalizedInput)) {
        normalizedInput = document.baseURI;
      }
      if (normalizedBase !== undefined && isPreviewDocumentUrl(normalizedBase)) {
        normalizedBase = document.baseURI;
      }
      if (!new.target) {
        return normalizedBase === undefined
          ? NativeURL(normalizedInput)
          : NativeURL(normalizedInput, normalizedBase);
      }
      return normalizedBase === undefined
        ? new NativeURL(normalizedInput)
        : new NativeURL(normalizedInput, normalizedBase);
    }

    Object.setPrototypeOf(PreviewURL, NativeURL);
    PreviewURL.prototype = NativeURL.prototype;
    window.URL = PreviewURL;
  }

  installOpaqueDocumentUrlAdapter();

  function mode() {
    var m = (document.documentElement.getAttribute("data-preview-mode") || "").trim();
    return m === "paged" ? "paged" : "continuous";
  }

  function post(payload) {
    try {
      window.parent.postMessage(payload, "*");
    } catch (e) {
      /* cross-origin parent or detached frame — nothing we can do */
    }
  }

  function measuredSections() {
    var rootRect = document.documentElement.getBoundingClientRect();
    var seen = Object.create(null);
    var sections = [];
    Array.prototype.forEach.call(
      document.querySelectorAll("[id^='sec-']"),
      function (section) {
        var key = section.id.slice(4);
        if (!SECTION_KEY.test(key) || seen[key]) return;
        var rect = section.getBoundingClientRect();
        var top = rect.top - rootRect.top;
        var bottom = rect.bottom - rootRect.top;
        if (!Number.isFinite(top) || !Number.isFinite(bottom) || bottom < top) return;
        if (top < -10000 || bottom > 10000000) return;
        seen[key] = true;
        sections.push({
          key: key,
          top: Math.round(top * 100) / 100,
          bottom: Math.round(bottom * 100) / 100,
        });
      }
    );
    sections.sort(function (a, b) { return a.top - b.top; });
    return sections.slice(0, 100);
  }

  function ensureHighlightStyles() {
    if (!document.head || document.getElementById("cv-hover-highlight")) return;
    var style = document.createElement("style");
    style.id = "cv-hover-highlight";
    style.textContent = HIGHLIGHT_STYLE;
    document.head.appendChild(style);
  }

  function setHighlight(key, on) {
    if (typeof key !== "string" || !SECTION_KEY.test(key)) return;
    var section = document.getElementById("sec-" + key);
    if (!section) return;
    if (on === true) section.setAttribute("data-highlighted", "true");
    else section.removeAttribute("data-highlighted");
  }

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) return;
    var data = event.data;
    if (!data || typeof data !== "object") return;
    if (data.type === "preview-ensure-highlight-styles") {
      ensureHighlightStyles();
      return;
    }
    if (data.type === "preview-highlight") {
      ensureHighlightStyles();
      setHighlight(data.key, data.on);
    }
  });

  ensureHighlightStyles();

  function measuredPage() {
    var page = document.querySelector(".pagedjs_page") || document.querySelector(".page");
    if (!page) {
      return {
        width: document.documentElement.scrollWidth || 794,
        height: document.documentElement.scrollHeight || 1123,
      };
    }
    var rect = page.getBoundingClientRect();
    return {
      width: Math.ceil(rect.width || page.scrollWidth || 794),
      height: Math.ceil(rect.height || page.scrollHeight || 1123),
    };
  }

  function measuredContent() {
    var renderedPages = Array.prototype.slice.call(document.querySelectorAll(".pagedjs_page"));
    var bounds = null;
    if (renderedPages.length) {
      renderedPages.forEach(function (el) {
        var r = el.getBoundingClientRect();
        if (!bounds) {
          bounds = { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
        } else {
          bounds.left = Math.min(bounds.left, r.left);
          bounds.top = Math.min(bounds.top, r.top);
          bounds.right = Math.max(bounds.right, r.right);
          bounds.bottom = Math.max(bounds.bottom, r.bottom);
        }
      });
    }
    var page = measuredPage();
    return {
      width: Math.ceil(Math.max(
        page.width,
        bounds ? bounds.right - bounds.left : 0
      )),
      height: Math.ceil(Math.max(
        page.height,
        bounds ? bounds.bottom - bounds.top : 0
      )),
      pageWidth: page.width,
      pageHeight: page.height,
    };
  }

  function measuredContinuous() {
    var page = document.querySelector(".page");
    if (!page) {
      return {
        width: document.documentElement.scrollWidth || 794,
        height: document.documentElement.scrollHeight || 1123,
        pageWidth: 794,
        pageHeight: 1123,
      };
    }
    var r = page.getBoundingClientRect();
    return {
      width: Math.ceil(Math.max(r.width, page.scrollWidth, document.documentElement.scrollWidth || 0)),
      height: Math.ceil(Math.max(r.height, page.scrollHeight, document.documentElement.scrollHeight || 0)),
      pageWidth: Math.ceil(r.width || page.scrollWidth || 794),
      pageHeight: Math.ceil(r.height || page.scrollHeight || 1123),
    };
  }

  function installSizeObserver(send) {
    if (typeof ResizeObserver === "undefined") return;
    var resizeRaf = 0;
    var observer = new ResizeObserver(function () {
      if (resizeRaf) return;
      resizeRaf = requestAnimationFrame(function () {
        resizeRaf = 0;
        send();
      });
    });

    function attach() {
      var target = document.querySelector(".pagedjs_pages") || document.querySelector(".page") || document.body;
      if (!target) return false;
      observer.observe(target);
      return true;
    }

    if (attach()) return;
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", attach, { once: true });
    } else {
      setTimeout(attach, 0);
    }
  }

  // ── PAGED MODE ────────────────────────────────────────────────────────────
  // paged.js 0.4.3 reads ``window.PagedConfig`` once the document is ready and
  // calls ``after(done)`` with the rendered result after chunking. We MUST set
  // PagedConfig BEFORE the polyfill <script> executes — render_preview emits
  // this runtime first, so that ordering holds.
  if (mode() === "paged") {
    function sendPagedSize(pageCount) {
      var renderedPages = document.querySelectorAll(".pagedjs_page");
      var pages = pageCount || renderedPages.length || 1;
      var measured = measuredContent();
      post({
        type: "paged-rendered",
        pages: pages,
        contentHeight: measured.height,
        contentWidth: measured.width,
        pageHeight: measured.pageHeight,
        pageWidth: measured.pageWidth,
        sections: measuredSections(),
        path: location.pathname + location.search,
      });
    }

    window.PagedConfig = {
      auto: true,
      after: function (done) {
        var pages = 1;
        // ``done`` is the Chunker result; ``.total`` is the page count in
        // 0.4.3. Fall back to counting rendered page elements.
        if (done && typeof done.total === "number" && done.total > 0) {
          pages = done.total;
        } else {
          var els = document.querySelectorAll(".pagedjs_page");
          if (els.length > 0) pages = els.length;
        }
        document.documentElement.classList.add("pagedjs-ready");
        sendPagedSize(pages);
        installSizeObserver(sendPagedSize);
      },
    };

    // Safety net: if the polyfill failed to load (e.g. CDN blocked) the body
    // would stay hidden forever (html:not(.pagedjs-ready) > body is hidden).
    // Reveal it after a grace period so the CV is at least readable.
    window.addEventListener("load", function () {
      setTimeout(function () {
        if (!document.documentElement.classList.contains("pagedjs-ready")) {
          document.documentElement.classList.add("pagedjs-ready");
          var measured = measuredContinuous();
          post({
            type: "paged-error",
            message: "Pagination unavailable",
            contentHeight: measured.height,
            contentWidth: measured.width,
            pageHeight: measured.pageHeight,
            pageWidth: measured.pageWidth,
            sections: measuredSections(),
            path: location.pathname + location.search,
          });
        }
      }, 4000);
    });
    return;
  }

  // ── CONTINUOUS MODE ───────────────────────────────────────────────────────
  function pageWidth() {
    var page = document.querySelector(".page");
    if (!page) return document.documentElement.scrollWidth;
    var r = page.getBoundingClientRect();
    return Math.max(r.width, document.documentElement.scrollWidth);
  }

  function pageHeight() {
    var page = document.querySelector(".page");
    if (!page) return document.documentElement.scrollHeight;
    var r = page.getBoundingClientRect();
    return Math.max(r.height, page.scrollHeight, document.documentElement.scrollHeight);
  }

  function sendSize() {
    var measured = measuredContinuous();
    post({
      type: "cv-content-rendered",
      contentHeight: measured.height,
      contentWidth: measured.width || pageWidth(),
      pageHeight: measured.pageHeight || pageHeight(),
      pageWidth: measured.pageWidth || pageWidth(),
      sections: measuredSections(),
    });
  }

  if (document.readyState === "complete") sendSize();
  else window.addEventListener("load", sendSize);
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(function () {
      setTimeout(sendSize, 50);
    });
  }
  installSizeObserver(sendSize);
  window.addEventListener("resize", sendSize);
  setTimeout(sendSize, 200);
})();
