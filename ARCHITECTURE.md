# Architecture

This is a one-page CV editor. The pipeline is intentionally small, and each
extension point lives in **one file** — most changes are a matter of finding
the right registry and editing it.

## Repo orientation

```
content/cv.yaml             ← optional gitignored input for the local CLI build
content/cv.example.yaml     ← tracked synthetic fallback and demo seed
engine/build.py             ← CLI: cv.yaml → cv.html → cv.pdf (1 page or fail)
engine/render/sections.py   ← single source of truth for what sections exist
engine/render/templates.py  ← HTML renderers per shape (RENDERERS dispatch)
engine/render/content.py    ← schema validation (derives from sections.py)
engine/render/importers.py  ← rendercv + plain-text importers (derives from sections.py)
engine/render/ai_extract.py ← provider extraction prompt (built from sections.py)
engine/render/css_base.py   ← typography, accent token, density variants
themes/<name>.json          ← swappable accent/font/density presets
tools/editor/server.py      ← Flask :5567, document APIs + preview/PDF routes
tools/editor/local_demo_store.py ← ephemeral demo document store
tools/editor/static/        ← form-first editor (form.js, app.js, style.css)
tests/                      ← pytest (unit, API, security, browser contracts)
docs/screenshots/           ← README assets (regenerate via tools/screenshots/capture.py)
```

## Common changes — where to edit

### Add a CV section as a default (e.g. "Publications", "Talks", "Press")

Edit **only** `engine/render/sections.py`. Append a `SectionDef` to
`DEFAULT_SECTIONS`:

```python
SectionDef(
    key="publications",
    label="Publications",
    eyebrow="Section 08 · Papers",
    singular="paper",
    shape="publication",        # or experience | education | skills | compact
    required_fields=("title",),
    rendercv_aliases=("publications", "papers"),
    text_header_pattern=r"^\s*(papers|publications|talks)\s*$",
)
```

That's it. The PDF renderer, validator, importer, extraction prompt, form,
and outline all read from this registry.

### Add a new visual shape

For example, an academic award block:

1. Add a Python renderer in `engine/render/templates.py` and register it
   in `_PER_ITEM_RENDERERS` (or `_WHOLE_LIST_RENDERERS` if it takes the
   full list).
2. Add a JS renderer in `tools/editor/static/form.js:SHAPE_RENDERERS`.
3. Add a YAML schema fragment in
   `engine/render/ai_extract.py:_SHAPE_SCHEMAS`.
4. Add CSS hooks in `engine/render/css_base.py` if the shape needs new
   styling.

### Add a theme

Drop `themes/<name>.json`:

```json
{
  "name": "Linear Blue",
  "accent": "#5e6ad2",
  "font": "sans",
  "density": "normal"
}
```

The editor's Theme picker lists everything in `themes/` automatically.

### Change typography or density numbers

`engine/render/css_base.py:build_css()`. The density classes
`.density-tight | .density-normal | .density-airy` override CSS
variables; tweak the values there.

### Change contact-link SVGs or supported networks

`engine/render/brand_icons.py` maps supported network names to neutral
navigation symbols from the pinned Lucide source set. Do not add third-party
brand logos. Update `LICENSES/icon-assets.lock.json`, the bundled Lucide
license, and the provenance regression test whenever the icon source changes.

## Invariants

1. **The build refuses to ship a 2-page PDF.** `engine/build.py` uses
   WeasyPrint's bookmark tree to identify the overflow heading and exits
   non-zero. Don't bypass it.
2. **The form model is the source of truth before save.** Form view serialises
   the in-browser model to YAML. The YAML tab is a synced buffer, not a second
   document. Save posts that YAML to the active document backend: process
   memory for the zero-config demo or an RLS-scoped Supabase row for the
   durable editor.
3. **`refreshPreview()` saves first if dirty.** The preview route reads the
   selected backend document's `yaml_blob` and renders it through a short-lived
   temporary file. Calling it before save would render the previous stored
   revision.
4. **The list order in `DEFAULT_SECTIONS` is the section order on the
   PDF and in the form.** Reorder there to reorder everywhere.

## Storage modes

- `engine/build.py` is the durable local file path. It reads the gitignored
  `content/cv.yaml` when present and otherwise renders
  `content/cv.example.yaml`.
- `ONEPAGER_DEMO_MODE=true` uses `local_demo_store.py`. It is loopback-only,
  single-tenant, process-local, and resets when the server stops.
- The normal editor uses Supabase authentication and RLS-scoped document rows.
  It does not treat `content/cv.yaml` as the live editor database.
