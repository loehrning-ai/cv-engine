# External Template Intake

External CV templates are not fetched or executed at runtime. Intake is a manual review process.

## Required Manifest

Each candidate template needs a `template_manifest.json` with:

- `id`: stable slug, unique against built-in templates.
- `label` and `description`: user-facing metadata.
- `renderer`: approved renderer id, currently `standard` or `rail`.
- `stylesheet`: approved stylesheet id, currently `cv`.
- `thumbnail`: reviewed local thumbnail path.
- `source_url`: pinned `https://raw.githubusercontent.com/.../<40-char commit>/...` URL.
- `commit`: matching 40-character lowercase Git commit SHA.
- `license`: SPDX-style license id or reviewed license label.
- `compatibility_version`: currently `1`.
- `assets`: optional reviewed image assets with `sha256:<64 hex>` hashes.

## Review Rules

- Do not fetch branch URLs such as `main`, `master`, or tags that can move.
- Do not execute Python, JavaScript, shell scripts, install hooks, build scripts, or template-provided dependencies.
- Do not import raw HTML/CSS that weakens the preview CSP, asset allowlist, one-page gate, or tenant isolation.
- Manually port visual ideas into approved renderer APIs and tokenized CSS.
- Keep CV and cover-letter data in the canonical YAML/form model.
- Prove every imported template with `python3 engine/build.py` and the template test matrix before exposing it in `/api/templates`.

## Storage Boundary

Only code-reviewed bundled templates are supported now. User/private template catalogs require separate RLS, deletion, export, and cross-tenant tests before implementation.
