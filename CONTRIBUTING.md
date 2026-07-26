# Contributing

Contributions that improve correctness, accessibility, output quality, tests, or documentation are welcome.

## Before changing code

- Read [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md), and [EXTENDING.md](EXTENDING.md).
- Keep security reports private and follow [SECURITY.md](SECURITY.md).
- Open a focused change. Do not combine unrelated formatting, content, and dependency work.
- Do not commit credentials, personal data, generated output, internal plans, or local machine paths.

This tool processes a real CV. `content/cv.yaml` is ignored by git for that reason. Develop against `content/cv.example.yaml`, and never attach a real CV, a rendered PDF, or a screenshot containing personal data to an issue or pull request.

## Local verification

WeasyPrint links against system Pango and Cairo. Install those first, or the renderer will not import:

```bash
brew install pango                                              # macOS
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2     # Debian or Ubuntu
```

Then install with the locked toolchain and run the gate:

```bash
python3 -m pip install --require-hashes -r requirements.lock
python3 -m pytest tests/ --ignore=tests/e2e
python3 engine/build.py
```

`python3 engine/build.py` is the one-page gate. It exits non-zero and names the offending heading when the PDF overflows, so a green run is part of the acceptance signal, not a convenience check.

The `--ignore=tests/e2e` flag is required. The browser suite fails rather than skips when no editor is reachable, which is deliberate: a silent skip would let a broken browser contract pass CI. Run it explicitly against a live server when changing routes, templates, editor JavaScript, responsive layout, or accessibility behavior:

```bash
python3 -m pip install --require-hashes -r requirements-browser.lock
python3 -m playwright install chromium
ONEPAGER_DEMO_MODE=1 python3 tools/editor/server.py &
python3 -m pytest tests/e2e/
```

Check that port 5567 is free before binding it. `ONEPAGER_DEMO_MODE=1` is
loopback-only; the server refuses public origins and non-loopback peers. The
enforced boundary is documented in [SECURITY.md](SECURITY.md).

## What reviewers check

Four invariants hold across the pipeline. A change that breaks one is a regression regardless of what else it improves.

- **The build refuses to ship a two-page PDF.** `engine/build.py` reads WeasyPrint's bookmark tree to identify the overflow heading and exits non-zero. Raising the threshold, catching the failure, or bypassing the check is not an accepted fix; reduce the content or the density instead.
- **The form model is the source of truth before save.** In Form view, saving serialises the in-browser model to YAML. The YAML tab is a synced buffer, not a second document. The API writes that YAML to process memory in the zero-config demo or to the caller's RLS-scoped Supabase document in durable mode. A change that introduces a third editor model will be rejected.
- **`refreshPreview()` saves first when the document is dirty.** The preview route reads the selected backend document's stored `yaml_blob`, materializes it in a temporary file, and removes that file after rendering. Preserve the save-then-preview ordering so preview never uses the previous stored revision.
- **`DEFAULT_SECTIONS` ordering is the rendered ordering.** The list order in `engine/render/sections.py` determines section order in the PDF and in the form. Reorder there rather than sorting downstream, so the two never diverge.

Extension points are single-file by design. Adding a section, a shape, or a theme should touch the registry described in [ARCHITECTURE.md](ARCHITECTURE.md), not the renderer's control flow.

## Documentation changes

- Keep claims verifiable against the code in the same change. Do not state test counts, timings, or percentages that nothing in the repository produces.
- Update the section registry, the schema, the importers, and the extraction prompt together. They derive from one another and drift silently when edited apart.
- Preserve the distinction between what runs locally and what leaves the machine. Any change to the AI paths must keep the data-flow documentation accurate.

## License of contributions

Code and repository-documentation contributions are accepted under the MIT License. Contributions to `CODE_OF_CONDUCT.md` are accepted under CC BY 4.0. Do not submit editorial, font, screenshot, logo, or third-party material unless redistribution rights are documented in the same change.
