# Extending this CV

Quick recipes for common changes. For the repo layout, registries, and
invariants, see [ARCHITECTURE.md](./ARCHITECTURE.md).

---

## Add a section by typing it into the YAML (no code needed)

Switch the editor to **YAML** view and add a new top-level key:

```yaml
awards:
  - title: Best Paper Award
    date: 2024
    desc: ICML 2024 Honourable Mention
  - title: Germany Scholarship
    date: 2021
    desc: Deutschlandstipendium
```

Hit ⌘S. Three things happen:

1. The YAML is **auto-linted** — formatting normalised, header comment preserved.
2. The sidebar grows an **"Awards"** entry (with a green progress dot).
3. The Form view picks up the section. The PDF preview re-renders with the new heading.

The shape (compact / experience / publication / etc.) is **inferred from the
first item's keys**:

| Item shape | Inferred |
|---|---|
| `{title, date, desc}` | compact (one-liner with right-aligned date) |
| `{role, company, start, end, bullets}` | experience |
| `{degree, school, start, end}` | education |
| `{title, authors, venue, date}` | publication |
| `{label, items}` | skills (definition-list) |

The label is title-cased from the key (`speaking_engagements` → "Speaking Engagements").

### When you'd reach for the registry instead

Editing `cv.yaml` is the right move for **one-off** sections you'd put on
your own CV. If you want to ship a section as a default for everyone who
clones the repo (with a stable order, custom eyebrow, importer aliases,
etc.) — register it in code:

## Add a "Publications" section in 30 seconds (registered)

Open `engine/render/sections.py`, append to `DEFAULT_SECTIONS`:

```python
SectionDef(
    key="publications",
    label="Publications",
    eyebrow="Section 08 · Papers",
    singular="paper",
    shape="publication",
    required_fields=("title",),
    rendercv_aliases=("publications", "papers"),
    text_header_pattern=r"^\s*(papers|publications|talks)\s*$",
),
```

Restart the editor. The form now shows a Publications section. The PDF
renders it. The Claude AI extractor knows about it. Your YAML file
accepts:

```yaml
publications:
  - title: "Some Paper"
    authors: "A. Author, B. Author, C. Hartman"
    venue: "ICLR 2024"
    date: "04/2024"
    doi: "10.5555/abc"
    url: "https://..."
```

Available `shape` values:

| Shape | Best for | Required fields |
|---|---|---|
| `experience` | jobs | role, company, start, end |
| `education` | degrees | degree, school, start, end |
| `skills` | category lists | label |
| `compact` | one-liners with date | title |
| `publication` | papers / talks / press | title |

To add a new shape entirely, see [ARCHITECTURE.md](./ARCHITECTURE.md#add-a-new-visual-shape).

---

## Add or change schema-driven fields

Registered sections expose field metadata from `engine/render/sections.py`.
Use `FieldDef` entries on the relevant `SectionDef` instead of duplicating
field lists in `tools/editor/static/form.js`.

The same registry feeds:

- `/api/schema`
- the form editor
- validation/import behavior
- the AI extraction prompt comments
- browser tests for stored-but-not-rendered custom fields

Unsupported custom fields are preserved in YAML but must be presented as stored
data, not as guaranteed rendered output.

---

## Add a template

Templates are layout structures. Themes are colour/font/density presets. Keep
those responsibilities separate.

1. Add the template entry to `engine/render/template_registry.py`.
2. Add CSS under the template-specific selectors in `engine/render/css_base.py`.
3. Keep the renderer compatible with the existing one-page A4 gate.
4. Add or update tests in `tests/test_template_registry.py` and rendering tests.
5. Run `python3 engine/build.py`.

The current built-in templates are:

- `classic`
- `modern`
- `sidebar`
- `executive`
- `technical`

Do not add a user-owned `document_templates` table without RLS, pending-erasure
rules, column-scoped grants, and cross-tenant tests.

---

## Add a provider

Providers belong in `engine/llm/client.py` as registry entries. Do not add a
route or service that reads an SDK key directly from `os.environ`.

Provider additions need:

- id, label, auth type, default model, and model policy
- hosted or validated base URL policy
- key validation and redaction coverage
- gateway status handling in `tools/editor/llm_gateway.py`
- account UI status if the provider is user-visible
- provider-matrix tests for valid, invalid, no-key, and no ambient fallback

Managed credits are Anthropic-only unless a separate billing contract is added.

---

## Switch to a different theme

The editor's Appearance menu lists theme presets and templates separately.
Theme JSON files live in `themes/`. Click one to apply colour/font/density
without changing document structure.

To add a new one:

```json
// themes/midnight.json
{
  "name": "Midnight",
  "accent": "#7C3AED",
  "font": "serif",
  "density": "normal"
}
```

Refresh the editor; the new theme appears in the picker.

Themes set:

- `accent` — bullet dots, section rules, brand-icon colour, save button
- `font` — `serif` (Source Serif 4) | `sans` (Inter) | `mono`
- `density` — `tight` | `normal` | `airy`

---

## Fork it and own your CV pipeline

The whole thing is a small Python package + a Flask server + a few
hundred lines of CSS. Fork the repo, change the accent colour or the
density numbers in `engine/render/css_base.py`, add a section in
`sections.py`, add a theme in `themes/`. Everything load-bearing —
the one-page guarantee, the form-first flow, the live preview —
survives those changes.

To redistribute a custom theme: paste your `themes/<name>.json` into a
GitHub gist and tell people to drop it into their `themes/` folder.

---

## Import from anywhere

Three paths in the editor's **Import** modal:

1. **AI extract** — drop a PDF / DOCX / TXT / MD file (or paste text).
   The selected provider maps it into the schema. Without a valid provider
   path, deterministic parsers handle the basics.
2. **rendercv YAML** — paste a [rendercv](https://rendercv.com) YAML
   for a deterministic conversion.
3. **Plain text** — paste any resume text; heuristic regex parser
   tries its best.

For LinkedIn profile import: open your profile → **More → Save to PDF** → drop
the file into the AI extract tab.

For LinkedIn job import: paste user-visible posting text into Jobs. Blind
LinkedIn scraping, stored LinkedIn cookies, and stealth automation are out of
scope. Approved adapters must be explicitly configured and disabled by default.

---

## Common questions

**My CV overflows. What do I do?**
1. Toggle density to **tight** (`Density · Tight` in the topbar)
2. Trim adjectives — favour numbers (`50%+`, `100+ hours/week`)
3. Drop the oldest entry from a long-tail section
4. Last resort: drop the `stack:` line on older roles

**Can I change the section order?**
Yes. The editor saves `section_order` inside the active document's YAML blob,
and the renderer applies that order for registered sections. Add new registered sections in
`engine/render/sections.py`; unsupported custom list sections still render
after registered sections.

**Can I have two pages?**
This tool intentionally enforces one page. If you really need two,
fork it, edit `engine/build.py`, and remove the page-count guard.
You'll lose the live overflow indicator too — they're paired by design.

**Where's my file?**
The local CLI reads the gitignored `content/cv.yaml` when it exists and falls
back to `content/cv.example.yaml` in a fresh clone. The browser editor is not a
file editor: zero-config demo documents live in process memory and reset when
the server stops; durable self-hosted documents live in RLS-scoped Supabase
rows. Use the demo's PDF download before stopping it, or configure the hosted
storage path in `DEPLOY.md`.
