# Screenshot capture

Everything in `docs/screenshots/` is generated. `capture.py` regenerates it.

```bash
python3 tools/screenshots/capture.py
```

No arguments needed, nothing to set up by hand, and no files to move out of the
way first. The script starts its own editor, captures, verifies, and cleans up
after itself.

| Flag | Effect |
|------|--------|
| `--port N` | Preferred editor port (default `5567`). If it is busy the script walks upward to the next free one and prints what it bound. |
| `--card-only` | Skip the five README shots; produce only `cv.png` and `preview-card.webp`. |
| `--keep-staging` | Leave the staging directory on disk, including the acceptance render (see below). |

Requires `playwright` with Chromium installed, `Pillow`, and `pdftoppm` /
`pdftotext` from poppler. CodeMirror is loaded from its CDN by the editor page,
so the capture needs network access to render the YAML pane with syntax
highlighting.

## The source document is pinned, not borrowed

`content/cv.yaml` — the operator's own CV — is real personal data. It is
gitignored, but both the editor and the PDF engine fall back to it whenever it
exists, so a naive screenshot run would publish it.

This script never lets that happen. It pins its input explicitly and then
proves the output is clean:

1. **Pinned input.** A capture fixture is derived from `content/cv.example.yaml`
   into a temporary directory and pinned via `ONEPAGER_CV_FILE` for the editor
   and `--config` for `engine/build.py`. Neither can reach `content/cv.yaml`,
   whether or not that file exists. `ONEPAGER_CV_FILE` accepts a filename under
   `content/` or an absolute path; the script passes an absolute path.
2. **Proven output.** After capturing, every string of rendered text — the DOM
   of every page and every frame, plus the PDF's text layer — is checked
   against the identifying values in `content/cv.yaml` if it is present: the
   name, each name token, e-mail addresses, and phone numbers (matched again
   with all non-digits stripped, so reformatting does not evade it). A single
   hit aborts the run.

Nothing is written into `docs/screenshots/` until step 2 passes. The whole run
stages into a temporary directory, which is destroyed on any failure. A failed
run therefore cannot leave a half-published image behind.

The env var pins the input; the assertion proves the output. Either alone would
be enough on a good day. Both is the point.

## The run is hermetic

The child process gets Supabase pointed at a closed local port and every model
provider key blanked. That is deliberate:

- **Nothing from a cloud tenant can render into a published image.** Whatever
  documents happen to live in the operator's account are unreachable.
- **The result does not depend on whose machine it runs on.** The app takes its
  local demo path and reads the pinned fixture, so two operators get the same
  pictures.

Supabase has to *look* configured — the login UI only offers the one-click demo
button when `/auth/config` reports a URL and a key — so it is pointed at
`127.0.0.1:9` rather than emptied. The password grant fails fast against a
refused connection and the server issues its local demo token instead. Setting
these names in the environment also neutralises a checked-out `.env`:
python-dotenv will not override a name that is already present.

Known cosmetic consequence: the sidebar library reads `/api/documents`, which
returns an empty list rather than raising when Supabase is unreachable, so its
demo fallback never fires and the shots show *No saved documents.* The editor
itself loads the pinned fixture correctly — only the library list is empty.

## What the fixture changes

Two edits to `content/cv.example.yaml`, applied as text substitution rather than
a YAML round-trip so the file's leading comment block survives verbatim — those
three comment lines state the one-page guarantee, and the preview card exists to
quote them.

- **`accent: '#a5370f'`** — the brand copper. `accent` is a first-class
  user-configurable field, so this is a real supported configuration rather than
  a costume. Paper, font, density and template keep their example defaults.
- **Employers.** The shipped example uses the sitcom placeholder trio, which
  reads as a joke in a hero image. They are replaced with constructed names —
  each checked against company registries and matching no real firm — that read
  like ordinary European tech employers. The person stays *Jordan Avery* with
  `example.com` contact details, which are already safe.

The fixture is deleted when the run finishes, successfully or not.

## Outputs

| File | How |
|------|-----|
| `cv.png` | **Rasterised from the PDF**, never screenshotted: `engine/build.py` then `pdftoppm -r 150 -png -singlefile`. It is the real output, so it should look like the real output. |
| `editor.png`, `editor-dark.png`, `form-experience.png`, `yaml-view.png`, `theme-picker.png` | Full-window shots at 1440x900, device scale factor 2. |
| `preview-card.webp` | The project preview card. See below. |

`sidebar.png`, `topbar.png` and `logo-closeup.png` are chrome crops from an
earlier pass. They carry no document data and no README page embeds them, so
this script leaves them alone.

## The preview card

`preview-card.webp` is the image that represents this project externally, so it
is built to a fixed spec rather than cropped by eye:

- Captured at 1260x788 CSS px, device scale factor 2, sidebar collapsed via
  `Cmd+\`.
- Left pane in YAML view scrolled to line 1; right pane in Paged mode with the
  green **1 page** chip in frame.
- Cropped to 16:10 and encoded with Pillow at exactly **1696x1060**,
  `quality=82, method=6`.

1696x1060 is exactly twice the 848 CSS px at which the website detail page
renders the figure, so the capture width is what sets apparent type size on the
site: the effective factor is `848 / PREVIEW_CSS_WIDTH`.

**The width has a floor, and it is not legibility.** Below roughly 1220 CSS px
the preview toolbar's flex row squeezes the `1 page` chip and clips its label —
the one element the card exists to show. 1260 is the first width at which the
chip reaches its natural 72 px and holds it, which leaves room for a different
font stack on another machine without silently reintroducing the clip.

### Acceptance check

Every run writes `preview-card.acceptance-848w.png` into the staging directory:
the finished card downscaled to 848 px wide, which is how large it actually
renders on the site. **Look at it.** The YAML keys must still be readable. Run
with `--keep-staging` to keep it around:

```bash
python3 tools/screenshots/capture.py --keep-staging
```

If the keys are not readable, lower `PREVIEW_CSS_WIDTH` (and `PREVIEW_CSS_HEIGHT`
with it, to hold 16:10) and re-run — but respect the toolbar floor above. If
that cannot be reconciled, compose a terminal frame showing `python3
engine/build.py` emitting the overflow-guard failure that names the offending
heading; that reads well at small sizes and is the product's hook either way.

## Reporting

A successful run prints the SHA-256, byte size, and **decoded** dimensions of
`preview-card.webp` — decoded via Pillow, not assumed from the encode
parameters. Downstream consumers of the asset need all three.
