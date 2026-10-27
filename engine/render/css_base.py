"""Build the CV's CSS string.

A4 single-page typography, three density modes (tight/normal/airy), an
accent token interpolated from the YAML source, and three font families
(serif / sans / mono) loaded as local @font-face files.

Visual conventions match ``cv-template/CV.html``:
  - Centered name + thin contact line, " · " separator
  - ALL-CAPS section headings with a hairline rule below
  - Job entries: bold company / italic location, then italic role / dates
  - Bullets: standard "•", hanging indent, justified
  - Brand icons rendered before each contact label (inline SVG)
"""
from __future__ import annotations

import re

# The accent value is interpolated raw into the generated CSS, so it must be a
# strict hex colour or a malicious value (e.g. ``#fff;} body{display:none}``)
# would inject arbitrary rules. We accept #RGB / #RRGGBB / #RRGGBBAA only and
# fall back to the default for anything else.
DEFAULT_ACCENT = "#111111"
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
PAPER_THEMES: dict[str, str] = {
    "white": "#ffffff",
    "creme": "#fff7e8",
    "soft-gray": "#f4f5f2",
    "blue-tint": "#eef6ff",
}
DEFAULT_PAPER = "white"


def _safe_accent(accent: str) -> str:
    """Return ``accent`` if it is a strict hex colour, else the default.

    Guards against CSS injection: the value is interpolated verbatim into the
    stylesheet, so anything that isn't a plain hex colour is rejected before it
    can break out of the declaration.
    """
    if isinstance(accent, str) and _HEX_COLOR_RE.match(accent):
        return accent
    return DEFAULT_ACCENT


def normalize_paper(value: object | None) -> str:
    paper = str(value or DEFAULT_PAPER).strip().lower()
    if paper not in PAPER_THEMES:
        raise ValueError(
            f"paper={paper!r} not supported. Use one of {tuple(PAPER_THEMES)}."
        )
    return paper


def build_css(
    base_url: str,
    accent: str = "#111111",
    font: str = "serif",
    paper: str = DEFAULT_PAPER,
) -> str:
    """Return the full CSS string for the CV.

    Args:
        base_url: Either ``file://...`` (for WeasyPrint) or
            ``http://127.0.0.1:5567/asset/...`` (rewritten by the editor).
        accent: Hex colour for bullet dots, the section rule, and brand icons.
        font: ``"serif"`` (Source Serif 4), ``"sans"`` (Inter), or
            ``"mono"`` (system mono, kept for completeness).
        paper: constrained rendered-paper theme id.
    """
    accent = _safe_accent(accent)
    paper = normalize_paper(paper)
    paper_color = PAPER_THEMES[paper]
    if font == "serif":
        font_stack = (
            "'Source Serif 4', 'EB Garamond', 'Hoefler Text', "
            "Georgia, 'Times New Roman', serif"
        )
    elif font == "mono":
        font_stack = (
            "ui-monospace, 'JetBrains Mono', 'Menlo', 'Consolas', monospace"
        )
    else:
        font_stack = (
            "'Inter', -apple-system, BlinkMacSystemFont, "
            "'Helvetica Neue', Arial, sans-serif"
        )

    return f"""
/* ------------ Inter (sans) ------------ */
@font-face {{
  font-family: 'Inter';
  src: url('{base_url}/design/fonts/Inter-Light.ttf') format('truetype');
  font-weight: 300;
  font-style: normal;
}}
@font-face {{
  font-family: 'Inter';
  src: url('{base_url}/design/fonts/Inter-Regular.ttf') format('truetype');
  font-weight: 400;
  font-style: normal;
}}
@font-face {{
  font-family: 'Inter';
  src: url('{base_url}/design/fonts/Inter-Medium.ttf') format('truetype');
  font-weight: 500;
  font-style: normal;
}}
@font-face {{
  font-family: 'Inter';
  src: url('{base_url}/design/fonts/Inter-SemiBold.ttf') format('truetype');
  font-weight: 600;
  font-style: normal;
}}
@font-face {{
  font-family: 'Inter';
  src: url('{base_url}/design/fonts/Inter-Bold.ttf') format('truetype');
  font-weight: 700;
  font-style: normal;
}}

/* ------------ Source Serif 4 (serif) ------------ */
@font-face {{
  font-family: 'Source Serif 4';
  src: url('{base_url}/design/fonts/SourceSerif4-Regular.ttf') format('truetype');
  font-weight: 400;
  font-style: normal;
}}
@font-face {{
  font-family: 'Source Serif 4';
  src: url('{base_url}/design/fonts/SourceSerif4-It.ttf') format('truetype');
  font-weight: 400;
  font-style: italic;
}}
@font-face {{
  font-family: 'Source Serif 4';
  src: url('{base_url}/design/fonts/SourceSerif4-Semibold.ttf') format('truetype');
  font-weight: 600;
  font-style: normal;
}}
@font-face {{
  font-family: 'Source Serif 4';
  src: url('{base_url}/design/fonts/SourceSerif4-SemiboldIt.ttf') format('truetype');
  font-weight: 600;
  font-style: italic;
}}
@font-face {{
  font-family: 'Source Serif 4';
  src: url('{base_url}/design/fonts/SourceSerif4-Bold.ttf') format('truetype');
  font-weight: 700;
  font-style: normal;
}}

:root {{
  --accent:        {accent};
  --ink:           #111111;
  --body:          #1a1a1a;
  --muted:         #555555;
  --rule:          {accent};
  --accent-rail:   {accent};
  --paper:         {paper_color};

  --font-head:     {font_stack};
  --font-body:     {font_stack};
  --font-mono:     ui-monospace, 'JetBrains Mono', Menlo, Consolas, monospace;

  --fs-name:       20pt;
  --fs-section:    9.5pt;
  --fs-role:       9pt;
  --fs-body:       8.75pt;
  --fs-meta:       8.75pt;
  --lh-body:       1.22;

  /* Density font-size multiplier (composed with each template's base --fs-*
     via calc). 1.0 = the template/root base is untouched; the density-*
     classes on .page override it. Kept RELATIVE so density never inverts a
     template that sets a smaller/larger base than the app default. */
  --density-scale: 1.0;

  --gap-section:   6px;
  --gap-item:      4px;

  --page-w:        210mm;
  --page-h:        297mm;
  --page-pad-x:    14mm;
  --page-pad-y:    11mm;
}}

* {{ box-sizing: border-box; }}

@page {{
  size: A4;
  margin: 0;
}}

html, body {{
  margin: 0;
  padding: 0;
  background: var(--paper);
  color: var(--body);
  font-family: var(--font-body);
  font-size: var(--fs-body);
  line-height: var(--lh-body);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-rendering: optimizeLegibility;
  /* Explicit OpenType craft (plan 108 stage 5) — kerned pairs + common
     ligatures in both renderers (WeasyPrint honours these), no metrics
     change that could move the 1-page boundary. */
  font-kerning: normal;
  font-variant-ligatures: common-ligatures;
}}

.page {{
  width: var(--page-w);
  min-height: var(--page-h);
  padding: var(--page-pad-y) var(--page-pad-x);
  background: var(--paper);
  color: var(--ink);
  position: relative;
  /* Body font-size lives here, on the density-carrying element, scaled by
     --density-scale. Density used to set ABSOLUTE --fs-* on <main> while the
     TEMPLATE sets its base --fs-* on the parent <body>; reading them on <body>
     made density inert, and reading absolute density values on <main> would
     INVERT the template (e.g. "tight" 8.25pt > sidebar's 7.75pt base). A
     relative multiplier composes correctly: normal=1.0 leaves every template's
     base untouched (one-page baseline preserved); tight shrinks and airy grows
     RELATIVE to whatever base the template set. line-height stays template/
     root-controlled (unitless, already inherited) — density only scales size. */
  font-size: calc(var(--fs-body) * var(--density-scale));
}}
.template-sidebar .page::before {{
  content: "";
  position: absolute;
  top: 0;
  left: 0;
  width: 3mm;
  /* The full sheet, not height: 100%: .page has only a min-height, and
     WeasyPrint resolves a percentage (or top + bottom) against the CONTENT
     height, so the bar used to stop wherever the text stopped. The CV is
     exactly one A4 sheet by contract, so the sheet height is the right
     length in the PDF and in the browser preview alike. */
  height: var(--page-h);
  background: var(--accent-rail);
  opacity: 0.78;
}}

/* ------------ Header (centered, classic) ------------ */
.header {{
  text-align: center;
  margin-bottom: 4px;
}}
.name {{
  font-family: var(--font-head);
  font-size: calc(var(--fs-name) * var(--density-scale));
  font-weight: 600;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ink);
  line-height: 1;
  margin: 0 0 5px;
}}
.contact {{
  font-size: calc(var(--fs-meta) * var(--density-scale));
  color: var(--body);
  line-height: 1.35;
}}
.contact a {{
  color: inherit;
  text-decoration: none;
  /* Keep icon and label on the same line — never break between them. */
  white-space: nowrap;
}}
.contact a:hover {{ text-decoration: underline; }}
.contact .sep {{ color: var(--muted); margin: 0 5px; user-select: none; }}
.contact-item {{ display: inline; }}
.contact-layout-stack .contact {{
  display: grid;
  justify-content: center;
  gap: 1px;
}}
.contact-layout-stack .contact-item {{ display: block; }}
.contact-layout-grid .contact {{
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 2px 10px;
}}
.header-layout-left {{
  text-align: left;
}}
.header-layout-left .name {{
  letter-spacing: 0.08em;
}}
.header-layout-left.contact-layout-stack .contact,
.header-layout-left.contact-layout-grid .contact {{
  justify-content: flex-start;
}}

/* Body links (markdown [label](url) inside bullets, descriptions, etc.) */
main a {{
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 1.5px;
}}
.contact a, .header a {{ color: inherit; text-decoration: none; }}

/* Brand icon — inline SVG sized to ~85% of the font size, sitting on the
   text baseline. No flexbox: the span stays a normal inline element so it
   doesn't change the line-box height vs. plain text. */
.contact .brand-icon {{
  display: inline;
  width: 0.85em;
  height: 0.85em;
  vertical-align: -0.12em;
  color: var(--ink);
  margin-right: 3px;
}}

.header.has-photo {{
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: center;
  gap: 14mm;
  text-align: left;
}}
.header.has-photo .header-text {{ text-align: center; }}
.header.has-photo.header-layout-left .header-text {{ text-align: left; }}
.header.has-photo.photo-layout-left {{ grid-template-columns: auto 1fr; }}
.header.has-photo.header-layout-split .header-text {{ text-align: left; }}
.header.has-photo .photo {{
  width: 22mm;
  height: 22mm;
  border-radius: 50%;
  object-fit: cover;
  border: 0.6pt solid var(--rule);
}}
.header.has-photo.photo-layout-square .photo {{
  border-radius: 2mm;
}}

/* ------------ Section chrome ------------ */
section {{ margin-bottom: var(--gap-section); }}
section:first-of-type {{ margin-top: 6px; }}
section:last-child {{ margin-bottom: 0; }}

.section-title {{
  font-family: var(--font-head);
  font-size: calc(var(--fs-section) * var(--density-scale));
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink);
  margin: 0 0 4px;
  padding-bottom: 1px;
  border-bottom: 0.6pt solid var(--rule);
}}

/* ------------ Items ------------ */
.item {{ margin-bottom: var(--gap-item); break-inside: avoid; }}
.item:last-child {{ margin-bottom: 0; }}

.item-head, .item-sub {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
}}
.item-head .left  {{ font-weight: 700; color: var(--ink); }}
.item-head .right {{
  font-style: italic;
  color: var(--ink);
  /* Dates align on a digit grid like .item-sub .right below. */
  font-variant-numeric: tabular-nums;
}}
.item-sub  .left  {{ font-style: italic; color: var(--ink); font-size: calc(var(--fs-role) * var(--density-scale)); }}
.item-sub  .right {{
  color: var(--ink);
  font-variant-numeric: tabular-nums;
  font-size: calc(var(--fs-role) * var(--density-scale));
  white-space: nowrap;
}}

ul.bullets {{
  list-style: none;
  margin: 2px 0 0;
  padding: 0;
}}
ul.bullets li {{
  position: relative;
  padding-left: 14px;
  margin-bottom: 1px;
  color: var(--body);
  text-align: justify;
  hyphens: auto;
  -webkit-hyphens: auto;
  overflow-wrap: break-word;
}}
ul.bullets li::before {{
  content: "\\2022";
  position: absolute;
  left: 2px;
  top: 0;
  color: var(--accent);
  font-weight: 700;
}}

/* Skills (definition-list style — flat, dense) */
.skills-grid {{
  display: grid;
  grid-template-columns: max-content 1fr;
  row-gap: 1px;
  column-gap: 12px;
}}
.skills-grid dt {{
  font-weight: 700;
  font-style: italic;
  color: var(--ink);
}}
.skills-grid dd {{
  margin: 0;
  color: var(--body);
}}

/* Compact rows for projects/leadership/others */
.row {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 1px;
}}
.row .left b {{ color: var(--ink); font-weight: 700; }}
.row .right {{
  color: var(--ink);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}}

/* Stack line — italic, muted */
.stack {{
  margin-top: 2px;
  font-size: calc(var(--fs-meta) * var(--density-scale));
  color: var(--muted);
  font-style: italic;
}}

/* Publication entry — title/date on top row, authors/venue on row 2.
   Tighter than experience.item — academics need to fit many on a page. */
.publication-item {{
  margin-bottom: var(--gap-item);
  break-inside: avoid;
}}
.publication-item .item-head .left a {{
  color: var(--ink);
  text-decoration: none;
  font-weight: 700;
}}
.publication-item .item-head .left a:hover {{
  text-decoration: underline;
}}
.publication-item .pub-sub {{
  color: var(--body);
  font-style: italic;
  font-size: 0.94em;
  line-height: 1.3;
  margin-top: 1px;
  text-align: justify;
  hyphens: auto;
}}

code.inline-code {{
  font-family: var(--font-mono);
  font-size: 0.92em;
  padding: 0 2px;
  background: rgba(0,0,0,0.04);
  border-radius: 2px;
}}

p, li {{ orphans: 3; widows: 3; }}

/* ------------ Density variants ------------ */
/* Density is a RELATIVE font-size scale (+ absolute gap steps), so it composes
   with any template's base --fs-* instead of overriding it. tight shrinks,
   airy grows, normal is a no-op (== the template base, preserving one-page). */
.density-tight  {{
  --gap-section: 4px;
  --gap-item:    2px;
  --density-scale: 0.955;
}}
.density-normal {{
  --gap-section: 6px;
  --gap-item:    4px;
  --density-scale: 1.0;
}}
.density-airy   {{
  --gap-section: 10px;
  --gap-item:    7px;
  --density-scale: 1.05;
}}

/* ------------ Template variants ------------ */
.template-modern {{
  --page-pad-x: 15mm;
  --page-pad-y: 12mm;
  --fs-name: 21pt;
}}
.template-modern .header {{
  text-align: left;
  border-bottom: 1.2pt solid var(--rule);
  padding-bottom: 4px;
  margin-bottom: 7px;
}}
.template-modern .name {{ letter-spacing: 0.06em; }}
.template-modern .contact .sep {{ margin: 0 7px; }}
.template-modern .section-title {{
  border-bottom: 0;
  color: var(--accent);
  letter-spacing: 0.04em;
  margin-bottom: 3px;
}}

.template-sidebar {{
  --page-pad-x: 10.5mm;
  --page-pad-y: 8mm;
  --fs-name: 16.5pt;
  --fs-body: 7.75pt;
  --fs-meta: 7.65pt;
  --fs-role: 7.9pt;
  --fs-section: 8.45pt;
  --gap-section: 2px;
  --gap-item: 1px;
  --lh-body: 1.09;
}}
.template-sidebar .header {{
  text-align: left;
  padding: 4px 6px;
  border-left: 4pt solid var(--rule);
  background: rgba(0,0,0,0.025);
}}
.template-sidebar .name {{ letter-spacing: 0.04em; }}
.template-sidebar .section-title {{
  border-bottom: 0.4pt solid rgba(0,0,0,0.18);
  padding-left: 5px;
  border-left: 2pt solid var(--rule);
}}
/* Two real columns (templates.render_body, RAIL_SIDE_KEYS): .rail-main holds
   experience, projects and every other section; .rail-side holds education
   and skills. Each column is its own block flow, so a long main column never
   stretches the side column's rows. The main column comes first in the
   source (reading order for ATS parsers); grid-row/column place it right. */
.template-sidebar .layout-rail {{
  display: grid;
  grid-template-columns: 50mm minmax(0, 1fr);
  column-gap: 5mm;
  align-items: start;
}}
.template-sidebar .rail-main {{ grid-column: 2; grid-row: 1; min-width: 0; }}
.template-sidebar .rail-side {{ grid-column: 1; grid-row: 1; min-width: 0; }}
/* Only one column has content: let it use the whole width. */
.template-sidebar .layout-rail > :only-child {{ grid-column: 1 / -1; }}
.template-sidebar .layout-rail .section-title {{
  margin-top: 0;
}}
/* The side column is too narrow for "school ... location" on one line:
   stack name, degree, place and dates instead of squeezing them. */
.template-sidebar .rail-side .item-head,
.template-sidebar .rail-side .item-sub {{
  display: block;
}}
.template-sidebar .rail-side .item-head .right,
.template-sidebar .rail-side .item-sub .right {{
  display: block;
  font-style: normal;
  color: var(--muted);
  white-space: normal;
}}
.template-sidebar .rail-side .item {{ margin-bottom: 4px; }}
.template-sidebar .rail-side section + section {{ margin-top: 6px; }}
.template-sidebar .rail-side .skills-grid {{
  display: block;
}}
.template-sidebar .rail-side .skills-grid dt {{
  margin-top: 3px;
}}
.template-sidebar .rail-side .skills-grid dt:first-child {{ margin-top: 0; }}

.template-executive {{
  --page-pad-x: 17mm;
  --page-pad-y: 13mm;
  --fs-name: 23pt;
  --fs-section: 9.2pt;
}}
.template-executive .header {{
  text-align: left;
  margin-bottom: 8px;
}}
.template-executive .name {{
  letter-spacing: 0.02em;
  text-transform: none;
  border-bottom: 1.4pt solid var(--rule);
  padding-bottom: 3px;
}}
.template-executive .section-title {{
  letter-spacing: 0.12em;
  border-bottom-color: rgba(0,0,0,0.28);
}}

.template-technical {{
  --page-pad-x: 13mm;
  --page-pad-y: 10mm;
  --fs-name: 18.5pt;
  --fs-body: 8.35pt;
  --fs-meta: 8.1pt;
  --gap-section: 4px;
  --gap-item: 2px;
}}
.template-technical .header {{
  text-align: left;
  border: 0.7pt solid rgba(0,0,0,0.18);
  border-left: 3pt solid var(--rule);
  padding: 5px 7px;
}}
.template-technical .name {{
  font-family: var(--font-mono);
  letter-spacing: 0;
  text-transform: none;
}}
.template-technical .section-title {{
  font-family: var(--font-mono);
  letter-spacing: 0;
  color: var(--accent);
  border-bottom-style: dashed;
}}

.template-ats-compact {{
  --page-pad-x: 12mm;
  --page-pad-y: 9mm;
  --fs-name: 17.5pt;
  --fs-body: 8.2pt;
  --fs-meta: 8pt;
  --fs-role: 8.35pt;
  --fs-section: 8.8pt;
  --gap-section: 3px;
  --gap-item: 2px;
  --lh-body: 1.14;
}}
.template-ats-compact .header {{
  text-align: left;
  border-bottom: 0.8pt solid var(--rule);
  padding-bottom: 3px;
  margin-bottom: 4px;
}}
.template-ats-compact .name {{
  letter-spacing: 0.02em;
}}
.template-ats-compact .section-title {{
  color: var(--accent);
  letter-spacing: 0.03em;
  margin-bottom: 2px;
}}
.template-ats-compact .item {{
  break-inside: avoid;
}}

.template-consulting {{
  --page-pad-x: 16mm;
  --page-pad-y: 12mm;
  --fs-name: 20pt;
  --fs-section: 9.4pt;
}}
.template-consulting .header {{
  text-align: left;
  border-bottom: 1pt solid rgba(0,0,0,0.24);
  padding-bottom: 5px;
}}
.template-consulting .name {{
  letter-spacing: 0.04em;
}}
.template-consulting .section-title {{
  border-bottom: 0;
  color: var(--accent);
  letter-spacing: 0.1em;
}}
.template-consulting .role {{
  font-weight: 700;
}}

.template-minimal {{
  --page-pad-x: 15mm;
  --page-pad-y: 12mm;
  --fs-name: 19pt;
  --fs-body: 8.7pt;
  --fs-meta: 8.4pt;
  --gap-section: 5px;
}}
.template-minimal .header {{
  text-align: left;
  border-bottom: 0.5pt solid rgba(0,0,0,0.14);
  padding-bottom: 4px;
}}
.template-minimal .name {{
  letter-spacing: 0;
  text-transform: none;
}}
.template-minimal .section-title {{
  border-bottom: 0;
  color: var(--accent);
  letter-spacing: 0.02em;
}}
"""
