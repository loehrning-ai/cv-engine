# Template Inspiration Matrix

Created: 2026-07-01

This matrix records product ideas from external resume projects for manual review and porting into CV Copilot's existing renderer/template system. It is not an approval to import remote code, arbitrary CSS, assets, build tools, or app architecture.

## Source Constraints

- Resume-Matcher: useful ideas are template choice, keyword scoring, tailored-resume feedback, PDF export, cover-letter generation, and classic/modern single/two-column template families.
- Reactive Resume: useful ideas are privacy-first self-hosting, real-time preview, named templates, drag/reorder mental model, rich text/custom sections, paper size, colors, fonts, spacing, and JSON Resume import/export concepts.
- CV Copilot constraints: portable YAML stays canonical, renderers remain local/reviewed Python/HTML/CSS, one-page PDF gate remains mandatory, user-authored HTML/rich text is not trusted, and external manifests must stay pinned/reviewed.

## Candidate Ideas

| Candidate | Source inspiration | CV Copilot fit | Renderer impact | One-page risk | Decision |
|---|---|---|---|---|---|
| Classic single column metadata | Resume-Matcher | Already covered by `classic`; add metadata axes | Metadata only | Low | Keep and annotate |
| Modern single column metadata | Resume-Matcher | Already covered by `modern`; add metadata axes | Metadata only | Low | Keep and annotate |
| Classic two column | Resume-Matcher | Similar to current `sidebar` but less rail-heavy | New/reused rail renderer variant | Medium | Review after browser matrix |
| Modern two column | Resume-Matcher + Reactive Resume | Strong candidate for a distinct visual template | New/reused rail renderer variant | Medium | Candidate |
| Rich text sections | Reactive Resume | High user value, high XSS/PDF risk | Schema/editor work | High | Do not port directly |
| Custom sections | Reactive Resume | Fits YAML if constrained to typed fields | Schema/editor work | Medium | Separate plan |
| JSON Resume import/export | Reactive Resume | Useful import/export boundary | Import/export adapter | Medium | Separate plan |
| Paper size controls | Reactive Resume | Partially exists via paper theme; A4 is current PDF target | Renderer/build policy | High | Defer |
| Template gallery metadata axes | Both | Fits current gallery | Metadata only | Low | Implement first |

## Metadata Fields To Add

- `layout_family`: `single_column`, `two_column`, `rail`, or `compact`.
- `column_count`: integer `1` or `2`.
- `inspiration_source`: short human-readable source note, never a code import path.
- `ats_priority`: `high`, `medium`, or `low` to describe parser-friendly structure.
- `customization_axes`: list such as `accent`, `font`, `density`, `paper`, `section_order`.
- `paper_sizes`: allowed output sizes. Current default remains `A4`.
- `import_export_notes`: short note for JSON Resume or external import compatibility.

## Next Implementation Slice

1. Extend `TemplateSpec` and public template payload with the metadata fields above.
2. Add static tests proving registry, `app.js`, `account.js`, `index.html`, and `account.html` stay aligned on template IDs.
3. Add a metadata-only gallery display before adding a new renderer.
4. Add a new two-column renderer variant only after the metadata path and browser matrix pass.
