"""One-page CV render service (Stage 6 consolidation).

Both the CLI (``engine/build.py``) and the editor's ``/api/build`` route had
their own copy of the same render-then-count-pages-then-name-the-overflow
logic. Stage 6 needs a THIRD caller — the CV-tailoring post-processor must
re-run the 1-page gate on the tailored YAML before it persists the new
document — so this is the moment to consolidate that logic into one place per
the plan's constraint.

:func:`render_cv_pdf` is the single function the in-process callers use::

    result = render_cv_pdf(yaml_text, density="normal")
    if result.overflowed:
        ... # result.page_count, result.overflow_label
    else:
        ... # result.pdf_bytes  (exactly one A4 page)

The 1-page gate (WeasyPrint bookmark-tree walk to name the first heading on
the overflow page) is byte-identical to the prior implementations — this is a
refactor, not a behaviour change. ``engine/build.py`` is intentionally left
untouched (it is the shell entrypoint and is verified independently); this
module is the in-process seam the gateway routes and the tailor service share.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:  # engine/ is on sys.path in every entrypoint.
    from render import content as content_mod
    from render import css_base, resource_policy, template_registry, templates
except ImportError:  # pragma: no cover - unusual import roots
    from engine.render import content as content_mod
    from engine.render import css_base, resource_policy, template_registry, templates

# The repo root — used as the WeasyPrint ``base_url`` so fonts/assets resolve
# exactly as the CLI build does (parameterizable per the plan, defaulted here).
_REPO = Path(__file__).resolve().parent.parent.parent

_VALID_DENSITIES = {"tight", "normal", "airy"}


class CvYamlError(ValueError):
    """Raised when the CV YAML can't be parsed/validated for rendering."""


@dataclass(frozen=True)
class RenderedPdf:
    """Outcome of a CV render attempt.

    ``overflowed`` is the 1-page gate verdict. When True, ``pdf_bytes`` is
    ``None`` and ``overflow_label`` names the first heading on page 2 (best
    effort). When False, ``pdf_bytes`` holds exactly one A4 page.
    """

    overflowed: bool
    page_count: int
    pdf_bytes: bytes | None = None
    overflow_label: str = ""
    density: str = "normal"


def _walk_bookmarks(tree):
    """Yield ``(label, page_index)`` from WeasyPrint's bookmark tree.

    Each entry is ``(label, destination, children, state)`` with
    ``destination = (page_index, x, y)`` — identical to build.py's walker.
    """
    if not tree:
        return
    for entry in tree:
        label, destination, children, _state = entry
        page_index = destination[0] if destination else None
        yield label, page_index
        yield from _walk_bookmarks(children)


def _find_overflow_label(doc) -> str:
    """The label of the first heading whose target page > 0 (page 2+)."""
    try:
        bookmarks = doc.make_bookmark_tree()
    except Exception:  # noqa: BLE001 - missing bookmark tree shouldn't crash
        return "(unknown — could not read bookmark tree)"
    for label, target_page in _walk_bookmarks(bookmarks):
        if target_page and target_page > 0:
            return label
    return "(unknown)"


# A sub-page overflow can be clipped below the A4 edge while WeasyPrint still
# reports a single page (e.g. a future CSS regression that fixes a height or
# adds overflow:hidden, or absolutely-positioned content). The page-count gate
# alone would silently ship the clipped CV. We defend in depth: re-render the
# same HTML onto a single, very tall sheet so NOTHING paginates or clips, then
# measure the natural content height. If it exceeds the real page box, the CV
# overflows regardless of the page count. A few px of slack absorbs rounding.
_OVERFLOW_SLACK_PX = 2.0
# Tall probe sheet (height only) — large enough that no realistic CV paginates.
_PROBE_PAGE_CSS = (
    "@page { size: 210mm 10000mm !important; margin: 0 !important; } "
    "main.page { min-height: 0 !important; }"
)


def _deepest_content_bottom(box) -> float:
    """Deepest descendant margin-box bottom under ``box`` (px from page top)."""
    bottom = 0.0
    for child in getattr(box, "children", None) or []:
        try:
            child_bottom = child.position_y + child.margin_height()
        except Exception:  # noqa: BLE001 - some boxes lack margin_height()
            child_bottom = getattr(child, "position_y", 0.0) or 0.0
        bottom = max(bottom, child_bottom, _deepest_content_bottom(child))
    return bottom


def _content_height_overflows(
    full_html: str,
    base_url: str,
    page_height_px: float,
    *,
    url_fetcher=None,
):
    """Return ``(overflows, natural_px)`` for ``full_html`` vs one page.

    Renders onto a tall, unclipped sheet (see :data:`_PROBE_PAGE_CSS`) and
    measures the natural content bottom. ``overflows`` is True when that
    exceeds ``page_height_px`` (plus a small slack). Best-effort: any failure
    in the probe returns ``(False, 0.0)`` so a measurement hiccup never blocks
    a legitimately-fitting CV (the page-count gate is the primary guard).
    """
    try:
        from weasyprint import HTML

        probe_html = full_html.replace("</style>", _PROBE_PAGE_CSS + "</style>", 1)
        secure_fetcher = url_fetcher or resource_policy.make_url_fetcher()
        probe_doc = HTML(
            string=probe_html,
            base_url=base_url,
            url_fetcher=secure_fetcher,
        ).render()
        probe_pages = list(probe_doc.pages)
        if not probe_pages:
            return False, 0.0
        natural = _deepest_content_bottom(probe_pages[0]._page_box)
        return natural > page_height_px + _OVERFLOW_SLACK_PX, natural
    except Exception:  # noqa: BLE001 - never let the probe crash the render
        return False, 0.0


def render_cv_pdf(
    yaml_text: str,
    *,
    density: str | None = None,
    template: str | None = None,
    paper: str | None = None,
    base_url: str | None = None,
    owner_id: str | None = None,
) -> RenderedPdf:
    """Render CV ``yaml_text`` to a 1-page PDF, enforcing the overflow gate.

    Args:
        yaml_text: the CV YAML (the ``documents.yaml_blob`` for the cloud
            path, or file contents for the CLI). Validated via the engine's
            ``content.load`` (same validator the editor uses).
        density: ``tight`` | ``normal`` | ``airy`` overriding the YAML's
            ``density:`` field; ``None`` (default) lets the YAML decide.
        paper: constrained rendered-paper theme overriding the YAML's
            ``paper:`` field; ``None`` lets the YAML decide.
        base_url: WeasyPrint base URL for asset resolution; defaults to the
            repo root (matches the CLI build).
        owner_id: authenticated owner identity. When supplied, a file-backed
            photo must be that exact owner's uploaded ``photo.*`` path.

    Returns:
        A :class:`RenderedPdf`. ``overflowed=True`` means the CV spilled past
        one page — the caller surfaces a 409/clear error and does NOT ship the
        PDF. ``overflowed=False`` carries the one-page ``pdf_bytes``.

    Raises:
        CvYamlError: the YAML is malformed (caller → 400).
        RuntimeError: WeasyPrint is not installed (caller → 500/503).
    """
    chosen_density = (density or "").strip().lower()
    if chosen_density and chosen_density not in _VALID_DENSITIES:
        chosen_density = ""

    # content.load takes a path; write a temp file rather than touch the
    # renderer (no engine change). Clean it up regardless of outcome.
    import tempfile

    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    try:
        tmp.write(yaml_text or "")
        tmp.flush()
        tmp.close()
        try:
            cv = content_mod.load(Path(tmp.name), owner_id=owner_id)
        except Exception as exc:  # noqa: BLE001 - surface a clean message
            raise CvYamlError(str(exc)) from exc
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    accent = cv.get("accent", "#111111")
    font = cv.get("font", "serif")
    effective_density = chosen_density or cv.get("density", "normal")
    if template:
        cv["template"] = template_registry.normalize_template(template)
    if paper:
        cv["paper"] = css_base.normalize_paper(paper)
    effective_paper = cv.get("paper", css_base.DEFAULT_PAPER)

    resolved_base = base_url or resource_policy.repo_base_url(_REPO)
    css = css_base.build_css(resolved_base, accent, font=font, paper=effective_paper)
    full_html = templates.render_document(cv, css, density=effective_density)
    secure_fetcher = resource_policy.make_url_fetcher(
        photo_source=cv.get("photo"),
        owner_id=owner_id,
        repo_root=_REPO,
    )

    try:
        from weasyprint import HTML
    except ImportError as exc:  # pragma: no cover - import guard
        raise RuntimeError("WeasyPrint is not installed.") from exc

    doc = HTML(
        string=full_html,
        base_url=resolved_base,
        url_fetcher=secure_fetcher,
    ).render()
    pages = list(doc.pages)
    page_count = len(pages)

    if page_count > 1:
        return RenderedPdf(
            overflowed=True,
            page_count=page_count,
            pdf_bytes=None,
            overflow_label=_find_overflow_label(doc),
            density=effective_density,
        )

    # Sub-page overflow guard: a single page can still hide content clipped
    # below the A4 edge. Measure the natural (unclipped) content height and
    # reject if it spills past the page box — even though page_count == 1.
    page_height_px = pages[0]._page_box.height if pages else 0.0
    spills, natural_px = _content_height_overflows(
        full_html,
        resolved_base,
        page_height_px,
        url_fetcher=secure_fetcher,
    )
    if spills:
        return RenderedPdf(
            overflowed=True,
            page_count=page_count,
            pdf_bytes=None,
            overflow_label=(
                f"(content clipped — {natural_px:.0f}px of "
                f"{page_height_px:.0f}px page)"
            ),
            density=effective_density,
        )

    return RenderedPdf(
        overflowed=False,
        page_count=page_count,
        pdf_bytes=doc.write_pdf(),
        overflow_label="",
        density=effective_density,
    )
