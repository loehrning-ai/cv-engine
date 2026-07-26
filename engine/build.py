"""Build a one-page CV PDF from ``content/cv.yaml``.

Usage:
    python3 engine/build.py
    python3 engine/build.py --density tight
    python3 engine/build.py --config content/cv.yaml --output output/cv.pdf
    python3 engine/build.py --skip-pdf

Hard constraint: the output PDF MUST be exactly one A4 page. If WeasyPrint
produces more than one page, the build exits non-zero with a message that
identifies the first heading on the overflowing page (best effort, via the
PDF bookmark tree). To fix overflow, re-run with ``--density tight`` or
trim a bullet from ``content/cv.yaml``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from render import content, css_base, resource_policy, templates  # noqa: E402


# Use the owner's real CV if present, else fall back to the tracked example
# so a fresh clone (which has no content/cv.yaml — it's gitignored PII) still
# builds out of the box.
_REAL_CV = REPO / "content" / "cv.yaml"
DEFAULT_CONFIG = _REAL_CV if _REAL_CV.exists() else REPO / "content" / "cv.example.yaml"
DEFAULT_OUTPUT = REPO / "output" / "cv.pdf"
DEFAULT_HTML = REPO / "output" / "cv.html"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Render a one-page CV PDF from YAML.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    # Default None so the YAML's ``density:`` field wins when not specified.
    p.add_argument("--density", choices=("tight", "normal", "airy"), default=None)
    p.add_argument(
        "--owner-id",
        default=None,
        help=(
            "Enforce the authenticated-owner photo namespace "
            "design/photos/<owner>/photo.*."
        ),
    )
    p.add_argument("--skip-pdf", action="store_true", help="Write HTML only, skip PDF.")
    return p.parse_args()


def _walk_bookmarks(tree):
    """Yield (label, page_index) from WeasyPrint's bookmark tree.

    Each entry is a 4-tuple: ``(label, destination, children, state)`` where
    ``destination`` is ``(page_index, x, y)``.
    """
    if not tree:
        return
    for entry in tree:
        label, destination, children, _state = entry
        page_index = destination[0] if destination else None
        yield label, page_index
        yield from _walk_bookmarks(children)


def _find_overflow_label(doc) -> str:
    """Return the label of the first heading whose target page > 0."""
    try:
        bookmarks = doc.make_bookmark_tree()
    except Exception:
        return "(unknown — could not read bookmark tree)"
    for label, target_page in _walk_bookmarks(bookmarks):
        if target_page and target_page > 0:
            return label
    return "(unknown)"


def main() -> int:
    args = parse_args()

    try:
        cv = content.load(args.config, owner_id=args.owner_id)
    except Exception as exc:  # noqa: BLE001 — surface validation errors, not tracebacks
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    accent = cv.get("accent", "#111111")
    font = cv.get("font", "serif")
    paper = cv.get("paper", "white")
    # CLI --density overrides YAML's density: handy for testing tight/airy
    # without editing the file.
    density = args.density if args.density else cv.get("density", "normal")

    base_url = resource_policy.repo_base_url(REPO)
    css = css_base.build_css(base_url, accent, font=font, paper=paper)
    full_html = templates.render_document(cv, css, density=density)
    secure_fetcher = resource_policy.make_url_fetcher(
        photo_source=cv.get("photo"),
        owner_id=args.owner_id,
        repo_root=REPO,
    )

    # The HTML sibling follows --output (out.pdf → out.html); the repo's
    # output/cv.html is only written for the default output path.
    html_path = (
        DEFAULT_HTML if args.output == DEFAULT_OUTPUT else args.output.with_suffix(".html")
    )
    html_path.parent.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(full_html, encoding="utf-8")

    if args.skip_pdf:
        try:
            display = str(html_path.resolve().relative_to(REPO))
        except ValueError:
            # Output path is outside the repo — print as-is.
            display = str(html_path)
        print(f"✅ Wrote {display} (PDF skipped)")
        return 0

    try:
        from weasyprint import HTML
    except ImportError:
        print(
            "❌ WeasyPrint is not installed. Run: pip install weasyprint",
            file=sys.stderr,
        )
        return 2

    doc = HTML(
        string=full_html,
        base_url=base_url,
        url_fetcher=secure_fetcher,
    ).render()
    pages = list(doc.pages)
    page_count = len(pages)

    if page_count > 1:
        overflow_label = _find_overflow_label(doc)
        print(
            f"❌ CV overflowed to {page_count} pages.\n"
            f"   First section on the overflow page: {overflow_label!r}.\n"
            f"   Try: --density tight, or trim a bullet in {args.config}.",
            file=sys.stderr,
        )
        return 1

    # Sub-page overflow guard: content can be clipped below the A4 edge while
    # WeasyPrint still reports a single page. Measure the natural (unclipped)
    # content height and fail the build if it spills past the page box — this
    # closes the gap where a clipped CV would otherwise ship as "1 page".
    from render.pdf_service import _content_height_overflows  # noqa: E402

    page_height_px = pages[0]._page_box.height if pages else 0.0
    spills, natural_px = _content_height_overflows(
        full_html,
        base_url,
        page_height_px,
        url_fetcher=secure_fetcher,
    )
    if spills:
        print(
            "❌ CV content overflows one page (clipped below the A4 edge).\n"
            f"   Rendered content is {natural_px:.0f}px tall but the page is "
            f"{page_height_px:.0f}px.\n"
            f"   Try: --density tight, or trim a bullet in {args.config}.",
            file=sys.stderr,
        )
        return 1

    doc.write_pdf(args.output)
    try:
        rel = args.output.resolve().relative_to(REPO)
        display = str(rel)
    except ValueError:
        # Output path is outside the repo — print as-is.
        display = str(args.output)
    print(f"✅ Built {display} (1 page, density={density})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
