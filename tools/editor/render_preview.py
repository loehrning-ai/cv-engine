"""Render the CV for the live editor preview.

Mirrors ``marketing/books/tools/editor/render_preview.py``: the same
HTML the engine produces, wrapped with mode-specific overrides.

  * ``continuous``: drop ``@page`` print rules, content scrolls naturally
    with the page rendered as a centred floating card.
  * ``paged``: keep ``@page`` rules, run paged.js inside the iframe so
    pagination matches what WeasyPrint will produce. A second
    ``.pagedjs_page`` (overflow) gets a red outline.

``file://`` URLs in the rendered HTML are rewritten to root-relative
``/asset/`` URLs so the browser can fetch fonts without trusting Host or
forwarded-proxy headers.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO = THIS_DIR.parent.parent

if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

import image_guard  # noqa: E402
from render import content as content_mod  # noqa: E402
from render import css_base, resource_policy, template_registry, templates  # noqa: E402


PAGEDJS_PATH = "/static/vendor/pagedjs-0.4.3/paged.polyfill.js"


def _inline_private_photo(cv: dict, *, owner_id: str | None) -> None:
    """Inline a validated file-backed photo into an authenticated preview.

    Preview HTML is injected into a sandboxed ``srcdoc`` iframe with an opaque
    origin. That frame cannot attach the bearer token required by the private
    photo endpoint, and the public ``/asset`` route intentionally rejects every
    ``design/photos/`` path. Read only the exact path accepted by the render
    resource policy, strip source metadata again, and embed bounded raster bytes
    so the photo renders without widening the private-file boundary.
    """
    source = cv.get("photo")
    if not source or resource_policy.is_data_resource(source):
        return
    relative = resource_policy.validate_relative_photo_source(
        source,
        owner_id=owner_id,
    )
    fetcher = resource_policy.make_url_fetcher(
        photo_source=relative,
        owner_id=owner_id,
        repo_root=REPO,
    )
    response = fetcher((REPO / relative).as_uri())
    try:
        raw = response.read(image_guard.MAX_PHOTO_BYTES + 1)
    finally:
        response.close()
    # Uploaded owner photos were already decoded and re-encoded at ingress.
    # Revalidate the bounded file but do not decode it again on every preview;
    # the template chooser can request several previews concurrently.
    info = image_guard.validate_image_bytes(raw)
    mime = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "WEBP": "image/webp",
    }[info.fmt]
    cv["photo"] = (
        f"data:{mime};base64,"
        + base64.b64encode(raw).decode("ascii")
    )


def render_cv_html(
    cv_path: Path,
    theme: str = "light",
    mode: str = "continuous",
    density: str = "normal",
    accent_override: str | None = None,
    font_override: str | None = None,
    template_override: str | None = None,
    paper_override: str | None = None,
    embedded: bool = False,
    thumbnail: bool = False,
    owner_id: str | None = None,
) -> str:
    """Build the iframe HTML for one preview render.

    Args:
        cv_path: Path to ``content/cv.yaml``.
        theme: ``"light"`` or ``"dark"``.
        mode: ``"continuous"`` or ``"paged"``.
        density: ``"tight"`` | ``"normal"`` | ``"airy"``.
        accent_override: editor-only override for ``cv.accent``. The YAML
            value remains the source of truth at build time.
        font_override: editor-only override for ``cv.font``. The YAML
            value remains the source of truth at build time.
        paper_override: editor-only rendered-paper theme override.
        owner_id: verified authenticated owner identity. When supplied, the
            photo path must be that exact owner's uploaded ``photo.*`` path.
    """
    # density="" or invalid → defer to the YAML's density field below
    if density and density not in {"tight", "normal", "airy"}:
        density = ""

    try:
        cv = content_mod.load(
            cv_path,
            strict_required=False,
            owner_id=owner_id,
        )
        if thumbnail:
            # The template chooser requests several thumbnails concurrently.
            # Omitting the private photo prevents duplicating a multi-megabyte
            # data URI across every response; the full preview still includes it.
            cv.pop("photo", None)
        else:
            _inline_private_photo(cv, owner_id=owner_id)
    except Exception as exc:
        return _error_html(str(exc))

    accent = accent_override or cv.get("accent", "#111111")
    font = font_override or cv.get("font", "serif")
    if template_override:
        cv["template"] = template_registry.normalize_template(template_override)
    if paper_override:
        cv["paper"] = css_base.normalize_paper(paper_override)
    # If the URL didn't specify a density, fall back to the YAML field
    # (so opening the preview without overrides honours cv.yaml).
    effective_density = density if density else cv.get("density", "normal")
    effective_paper = cv.get("paper", css_base.DEFAULT_PAPER)
    base_url = f"file://{REPO}"
    css = css_base.build_css(base_url, accent, font=font, paper=effective_paper)
    base_doc = templates.render_document(cv, css, density=effective_density)

    if thumbnail:
        overrides = _THUMBNAIL_EMBED_OVERRIDES
        scripts = ""
    elif mode == "paged":
        overrides = _PAGED_EMBED_OVERRIDES if embedded else _PAGED_OVERRIDES
        scripts = _paged_scripts()
    else:
        overrides = _CONTINUOUS_EMBED_OVERRIDES if embedded else _CONTINUOUS_OVERRIDES
        scripts = _continuous_scripts()

    dark_overrides = "" if thumbnail else ((_DARK_EMBED_OVERRIDES if embedded else _DARK_OVERRIDES) if theme == "dark" else "")
    embedded_cleanup = _EMBEDDED_FRAME_CLEANUP if embedded else ""

    head_inject = (
        f'<style id="preview-overrides">{overrides}</style>'
        f'{dark_overrides}'
        f'{embedded_cleanup}'
        f'{scripts}'
    )

    if "</head>" in base_doc:
        full = base_doc.replace("</head>", head_inject + "</head>", 1)
    else:
        full = base_doc.replace("<body>", "<body>" + head_inject, 1)

    full = full.replace(
        "<html lang=\"en\">",
        f'<html lang="en" data-preview-theme="{theme}" data-preview-mode="{mode}" data-density="{density}" data-preview-thumb="{str(thumbnail).lower()}">',
        1,
    )

    # Rewrite the engine's ``file://<REPO>/design/...`` font/asset URLs into
    # REPO-RELATIVE ``/asset/design/...`` URLs. Emitting the path RELATIVE to
    # REPO (no absolute prefix, no REPO-basename segment) means the URL is
    # identical in local dev and in deployed containers; ``server._safe_repo_path``
    # resolves ``design/...`` directly under ``REPO/design`` in both. The previous
    # form embedded the full absolute REPO path, which 404'd locally once /asset
    # was scoped to design/.
    full = full.replace(f"file://{REPO}/", "/asset/")
    # Fallback for any bare ``file://<REPO>`` with no trailing slash.
    full = full.replace(f"file://{REPO}", "/asset")
    return full


_CONTINUOUS_OVERRIDES = """
@page { size: auto; margin: 0; }
html, body {
  background: #e9e9ea;
  transition: background-color 240ms cubic-bezier(0.32, 0.72, 0, 1);
}
body { padding: 24pt 0; }
.page {
  margin: 0 auto;
  box-shadow: 0 8px 32px rgba(11, 9, 8, 0.10), 0 2px 6px rgba(11, 9, 8, 0.04);
}
"""


_CONTINUOUS_EMBED_OVERRIDES = """
@page { size: auto; margin: 0; }
html, body {
  background: transparent;
  overflow: hidden;
  transition: background-color 240ms cubic-bezier(0.32, 0.72, 0, 1);
}
body { padding: 0; margin: 0; }
.page {
  margin: 0 auto;
  box-shadow: none;
}
"""


_THUMBNAIL_EMBED_OVERRIDES = """
@page { size: auto; margin: 0; }
html, body {
  width: 420px;
  height: 560px;
  margin: 0 !important;
  padding: 0 !important;
  overflow: hidden !important;
  background: transparent !important;
}
body {
  position: relative;
}
.page, main.page {
  position: relative;
  left: 50%;
  margin: 0 !important;
  box-shadow: none !important;
  transform: translateX(-50%) scale(0.4988);
  transform-origin: top center;
}
"""


_PAGED_OVERRIDES = """
/* Hide content until paged.js finishes laying it out, otherwise the
   editor flashes the unpaginated single-document view first. */
html:not(.pagedjs-ready) > body { visibility: hidden; }
html.pagedjs-ready > body { visibility: visible; }

/* Force zero body padding — paged.js measures every direct body child
   for pagination, and any leading offset becomes a phantom blank
   page above the CV. */
body {
  background: #e9e9ea;
  padding: 0 !important;
  margin: 0 !important;
  transition: background-color 240ms cubic-bezier(0.32, 0.72, 0, 1);
}

/* Restore breathing room around the page stack via the paged.js wrapper. */
.pagedjs_pages {
  padding: 24pt 0 32pt 0;
  margin: 0 auto;
}

/* Each rendered page becomes a paper-white card with subtle shadow. */
.pagedjs_page {
  background: #ffffff !important;
  box-shadow: 0 8px 32px rgba(11, 9, 8, 0.10), 0 2px 6px rgba(11, 9, 8, 0.04);
  margin: 0 auto 28pt auto !important;
}

/* OVERFLOW HIGHLIGHT — any page after the first gets a red outline so
   the user immediately sees the CV no longer fits. */
.pagedjs_page:nth-child(n+2) {
  outline: 4px solid #ef4444;
  outline-offset: 4px;
}

/* Re-enable absolute positioning the page relies on inside paged.js. */
.page, main.page {
  margin: 0 !important;
  box-shadow: none !important;
}
"""


_PAGED_EMBED_OVERRIDES = """
/* Embedded editor preview: the parent #preview-wrap owns all canvas spacing.
   The iframe contains only the rendered paper box, so fit-mode math can prove
   the full page is visible without an internal gray band or iframe scrollbar. */
html:not(.pagedjs-ready) > body { visibility: hidden; }
html.pagedjs-ready > body { visibility: visible; }

html, body {
  background: transparent;
  padding: 0 !important;
  margin: 0 !important;
  overflow: hidden;
  transition: background-color 240ms cubic-bezier(0.32, 0.72, 0, 1);
}

.pagedjs_pages {
  padding: 0 !important;
  margin: 0 auto !important;
}

.pagedjs_page {
  background: #ffffff !important;
  box-shadow: none !important;
  margin: 0 auto !important;
}

.pagedjs_page:nth-child(n+2) {
  outline: 4px solid #ef4444;
  outline-offset: -4px;
}

.page, main.page {
  margin: 0 !important;
  box-shadow: none !important;
}
"""


_EMBEDDED_FRAME_CLEANUP = """
<style id="embedded-preview-frame-cleanup">
  html, body { background: transparent !important; }
  .pagedjs_pages { background: transparent !important; }
</style>
"""


def _paged_scripts() -> str:
    """Scripts for paged mode.

    The preview-runtime.js (external, same-origin) sets ``window.PagedConfig``
    and MUST load BEFORE the paged.js polyfill reads it — hence the ordering
    below. Both are loaded as EXTERNAL scripts so they run even when the
    document is framed via ``srcdoc`` under the parent's strict
    ``script-src 'self'`` CSP (inline <script> would be blocked there). The
    Root-relative URLs resolve against the embedding document for ``srcdoc``
    and against the current origin for a standalone preview. This avoids
    mixed-content failures and Host-header origin injection behind proxies.
    """
    return (
        '<script src="/static/preview-runtime.js"></script>\n'
        f'<script src="{PAGEDJS_PATH}"></script>'
    )


def _continuous_scripts() -> str:
    """Scripts for continuous mode — just the external preview runtime, which
    reports content height/width to the editor (see preview-runtime.js)."""
    return '<script src="/static/preview-runtime.js"></script>'


_DARK_OVERRIDES = """
<style id="dark-preview-overrides">
  html, body { background: #1a1714 !important; color: #f3f0e9 !important; }
  .page {
    background: #221d18 !important;
    color: #f3f0e9 !important;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45), 0 2px 6px rgba(0, 0, 0, 0.25) !important;
  }
  .pagedjs_page {
    background: #221d18 !important;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45), 0 2px 6px rgba(0, 0, 0, 0.25) !important;
  }
  .name, .section-title,
  .item-head .left, .item-head .right,
  .item-sub .left, .item-sub .right,
  .row .left b, .row .right,
  .skills-grid dt {
    color: #f3f0e9 !important;
  }
  .contact, .skills-grid dd, ul.bullets li, .stack {
    color: #c0b8b0 !important;
  }
  .contact .sep { color: #8a807a !important; }
  .section-title { border-bottom-color: #f3f0e9 !important; }
  ul.bullets li::before { color: #f3f0e9 !important; }
</style>
"""


_DARK_EMBED_OVERRIDES = """
<style id="dark-preview-overrides">
  html, body { background: transparent !important; color: #f3f0e9 !important; }
  .page {
    background: #221d18 !important;
    color: #f3f0e9 !important;
    box-shadow: none !important;
  }
  .pagedjs_page {
    background: #221d18 !important;
    box-shadow: none !important;
  }
  .name, .section-title,
  .item-head .left, .item-head .right,
  .item-sub .left, .item-sub .right,
  .row .left b, .row .right,
  .skills-grid dt {
    color: #f3f0e9 !important;
  }
  .contact, .skills-grid dd, ul.bullets li, .stack {
    color: #c0b8b0 !important;
  }
  .contact .sep { color: #8a807a !important; }
  .section-title { border-bottom-color: #f3f0e9 !important; }
  ul.bullets li::before { color: #f3f0e9 !important; }
</style>
"""


def _error_html(message: str) -> str:
    safe = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body {{ font-family: ui-monospace, Menlo, monospace;
          background: #1a1714; color: #f3f0e9;
          padding: 32px; margin: 0; line-height: 1.5; }}
  h1 {{ color: #ef4444; font-size: 16px; margin: 0 0 12px; letter-spacing: .04em; }}
  pre {{ white-space: pre-wrap; word-break: break-word; }}
</style></head>
<body>
<h1>CV.YAML — VALIDATION ERROR</h1>
<pre>{safe}</pre>
</body></html>"""
