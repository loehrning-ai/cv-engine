"""Hygiene / supply-chain / docs regression tests.

These guard the Docker packaging and dependency-pinning hardening done in the
HYGIENE batch. They are pure file-content assertions — no app import, no
network, no LLM — so they run anywhere the repo is checked out.

What they lock in:
  * .dockerignore excludes internal docs, agent state, reports, personal data,
    caches, photos, and generated screenshot failures — but
    KEEPS the runtime tree (engine/, tools/, themes/, design/, tests/, and
    content/cv.example.yaml).
  * Dockerfile pins its base image to a sha256 digest and avoids overstating
    Debian package reproducibility.
  * requirements.txt gives every pinned dependency an upper bound so a bad
    major release can't silently break a fresh build.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))


def _read(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def _dockerignore_patterns() -> list[str]:
    out: list[str] = []
    for raw in _read(".dockerignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


# --------------------------------------------------------------------------- #
# .dockerignore — internal artifacts must NOT ship in the image
# --------------------------------------------------------------------------- #

def test_dockerignore_excludes_internal_docs_and_personal_data():
    pats = set(_dockerignore_patterns())
    must_exclude = {
        "CLAUDE.md",
        "AGENTS.md",
        "plans/",
        "*.local.md",
        "content/cv.yaml",
        "design/photos/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".mypy_cache/",
        "htmlcov/",
        "supabase/.temp/",
        "artifacts/",
        "docs/screenshots/",
        "tests/e2e/baselines/*.actual.png",
        ".claude/",
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "*cert*.json",
        "*key*.json",
        "*credential*.json",
        "*token*.json",
    }
    missing = must_exclude - pats
    assert not missing, f".dockerignore missing required exclusions: {sorted(missing)}"
    assert "!.env.example" in pats


def test_dockerignore_keeps_runtime_tree_in_image():
    """The runtime + the test gate must stay in the image. Assert nothing in
    .dockerignore would drop them."""
    pats = _dockerignore_patterns()
    keep = [
        "engine",
        "tools",
        "themes",
        "design",
        "tests",
        "requirements.txt",
        "content/cv.example.yaml",
    ]
    for kept in keep:
        # No pattern may be an exact match or a parent-dir prefix of a kept path.
        for p in pats:
            norm = p.rstrip("/")
            assert norm != kept, f".dockerignore drops required path: {kept} (pattern {p!r})"
            assert not kept.startswith(norm + "/"), (
                f".dockerignore pattern {p!r} would exclude required path {kept}"
            )


def test_dockerignore_does_not_blanket_exclude_content_dir():
    """cv.yaml is excluded but the whole content/ dir (which holds the public
    cv.example.yaml the build + tests need) must not be."""
    pats = {p.rstrip("/") for p in _dockerignore_patterns()}
    assert "content" not in pats
    assert "content/cv.example.yaml" not in pats


# --------------------------------------------------------------------------- #
# Dockerfile — reproducible base image
# --------------------------------------------------------------------------- #

def test_dockerfile_base_image_is_digest_pinned():
    text = _read("Dockerfile")
    m = re.search(
        r"^FROM\s+cgr\.dev/chainguard/wolfi-base@sha256:([0-9a-f]{64})\s*$",
        text,
        flags=re.MULTILINE,
    )
    assert m, "Dockerfile FROM must pin the Wolfi base by a sha256 digest"


def test_dockerfile_keeps_tests_in_image():
    """The gate runs pytest inside the image, so tests/ must NOT be excluded
    and the build must copy the tree in."""
    assert "tests/" not in _dockerignore_patterns()
    assert "COPY --chown=onepager:onepager . ." in _read("Dockerfile")


def test_deploy_artifact_excludes_secret_and_internal_patterns():
    workflow = _read(".github/workflows/deploy.yml")
    assert "git archive --worktree-attributes --format=tar HEAD" in workflow
    assert "tar -xf - -C" in workflow
    assert "--exclude='.env.local'" not in workflow
    assert "--exclude='.env.*secret*'" not in workflow


def test_public_source_archives_exclude_internal_artifacts():
    attrs = _read(".gitattributes")
    for pattern in (
        "CLAUDE.md export-ignore",
        "AGENTS.md export-ignore",
        "plans/** export-ignore",
        "supabase/.temp/** export-ignore",
        "content/cv.yaml export-ignore",
        ".env export-ignore",
        ".env.* export-ignore",
        ".env.example -export-ignore",
        "*credential*.json export-ignore",
        "*cert*.json export-ignore",
        "*key*.json export-ignore",
        "*token*.json export-ignore",
    ):
        assert pattern in attrs

    forbidden = ("CLAUDE.md", "AGENTS.md", "plans/", "content/cv.yaml", ".env", "private-key.json", "client-cert.json")
    for path in forbidden:
        result = subprocess.run(
            ["git", "check-attr", "export-ignore", "--", path],
            cwd=REPO,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        assert "export-ignore: set" in result.stdout

    result = subprocess.run(
        ["git", "check-attr", "export-ignore", "--", ".env.example"],
        cwd=REPO,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert "export-ignore: unset" in result.stdout


def test_public_examples_use_reserved_email_domains():
    """Examples must not publish plausible third-party mailbox identities."""
    allowed_domains = {"example.com", "example.net", "example.org", "localhost.invalid"}
    for path in ("README.md", "content/cv.example.yaml", ".env.example"):
        addresses = re.findall(
            r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})",
            _read(path),
        )
        assert set(addresses) <= allowed_domains, (
            f"{path} contains a sample address on a non-reserved domain: "
            f"{sorted(set(addresses) - allowed_domains)}"
        )


def test_public_docs_disclose_editor_storage_and_egress_modes():
    readme = _read("README.md")
    data_flow = _read("docs/data-flow.md")
    architecture = _read("ARCHITECTURE.md")
    contributing = _read("CONTRIBUTING.md")
    extending = _read("EXTENDING.md")
    public_docs = "\n".join(
        (readme, data_flow, architecture, contributing, extending)
    )

    for required in (
        "process memory",
        "resets when the server stops",
        "RLS-scoped Supabase",
        "ONEPAGER_DEMO_MODE=true python3 tools/editor/server.py",
    ):
        assert required in public_docs

    for false_claim in (
        "single-file local editor with zero cloud setup",
        "reads and writes `content/cv.yaml` on disk",
        "preview route reads `content/cv.yaml` from disk",
        "The form view is just a UI on top.",
    ):
        assert false_claim not in public_docs

    assert "no remote theme-import path" in readme
    assert "neither imports" in data_flow
    assert "job URL ingestion" in data_flow
    assert "explicit actions, not background traffic" in data_flow


def test_editor_startup_reports_seed_and_actual_storage_mode():
    server = _read("tools/editor/server.py")
    assert 'print(f"  seed YAML: {_cv_file_display_path()}")' in server
    assert "storage: process memory (resets when this server stops)" in server
    assert "storage: Supabase documents (configuration required)" in server
    assert 'print(f"  cv.yaml: {_cv_file_display_path()}")' not in server


def test_browser_editor_does_not_claim_direct_cv_yaml_disk_persistence():
    app = _read("tools/editor/static/app.js")
    form = _read("tools/editor/static/form.js")
    index = _read("tools/editor/static/index.html")
    style = _read("tools/editor/static/style.css")
    browser_sources = "\n".join((app, form, index, style))

    for false_claim in (
        "cv.yaml (on disk) — canonical persistence",
        "Edits land in cv.yaml on save",
        "file on disk reflects the form/buffer",
        "pipeline expects (so the YAML on disk doesn't change)",
        '<span id="filepath-text">content/cv.yaml</span>',
    ):
        assert false_claim not in browser_sources

    assert "process memory in demo mode, Supabase" in app
    assert "Edits save as YAML in the current document" in app
    assert '<span id="filepath-text">Stored CV · YAML</span>' in index


def test_cv_undo_history_is_memory_only_and_legacy_snapshots_are_purged():
    app = _read("tools/editor/static/app.js")
    auth = _read("tools/editor/static/auth.js")
    account = _read("tools/editor/static/account.js")

    assert 'localStorage.setItem(historyStorageKey()' not in app
    assert "Intentionally memory-only" in app
    assert 'startsWith("cv.editor.history::")' in app
    assert 'key.indexOf("cv.editor.history::") === 0' in auth
    assert "clearSensitiveLocalState();" in auth
    assert "window.OnePagerAuth.clearSensitiveLocalState();" in account


def test_theme_presets_are_read_only_and_remote_import_is_disabled():
    server = _read("tools/editor/server.py")
    app = _read("tools/editor/static/app.js")
    index = _read("tools/editor/static/index.html")
    compose = _read("docker-compose.yml")

    assert '"code": "theme_import_disabled"' in server
    assert "ssrf.safe_fetch_url(url" not in server[
        server.index('def api_themes_import()'):server.index(
            '@app.route("/api/themes")'
        )
    ]
    assert "/api/themes/import" not in app
    assert 'id="theme-import-btn"' not in index
    assert "onepager_themes" not in compose


def test_personal_exports_and_resource_heavy_routes_have_per_user_budgets():
    server = _read("tools/editor/server.py")
    for budget in (
        '_LIMIT_RENDER = "12 per minute"',
        '_LIMIT_PREVIEW = "120 per minute"',
        '_LIMIT_PHOTO = "20 per hour"',
        '_LIMIT_EXPORT = "6 per hour"',
    ):
        assert budget in server
    for decorator in (
        "@_render_rate_limit\ndef api_build",
        "@_render_rate_limit\ndef api_cover_letter_build",
        "@_preview_rate_limit\ndef api_preview",
        "@_photo_rate_limit\ndef api_upload_photo",
        "@_photo_rate_limit\ndef api_delete_photo",
        "@_export_rate_limit\ndef api_account_export",
    ):
        assert decorator in server


def test_deploy_optional_operator_controls_are_serialized():
    workflow = _read(".github/workflows/deploy.yml")
    for key in (
        "ONEPAGER_OLLAMA_BASE_URL",
        "ONEPAGER_OPENROUTER_MODEL",
        "ONEPAGER_JOB_URL_ALLOWLIST",
        "ONEPAGER_JOB_URL_RATE_LIMIT_PER_MINUTE",
        "LINKEDIN_APPROVED_ADAPTER_ENABLED",
        "LINKEDIN_PARTNER_CONFIG_ID",
        "POSTHOG_HOST",
        "ONEPAGER_ATS_RUBRIC",
        "ONEPAGER_LOCAL_JOBS_DB",
        "ONEPAGER_LOCAL_JOBS_FRESH_DAYS",
        "ONEPAGER_EXTERNAL_APPLY_ENABLED",
        "ONEPAGER_EXTERNAL_APPLY_KILL_SWITCH",
        "ONEPAGER_EXTERNAL_APPLY_SAFE_FIXTURE_HOSTS",
    ):
        assert f'"{key}"' in workflow


def test_deploy_audit_toolchain_uses_hashed_lock_and_rollback_health_check():
    workflow = _read(".github/workflows/deploy.yml")
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    assert 'python-version: "3.13"' in workflow
    assert "--require-hashes -r requirements-audit.lock" in workflow
    assert "--require-hashes -r requirements-browser.lock" in workflow
    assert "pip-audit==2.10.1" in _read("requirements-audit.in")
    assert "bandit==1.9.4" in _read("requirements-audit.in")
    assert "ruff==0.15.22" in _read("requirements-audit.in")
    assert "pip-audit==2.10.1" in _read("requirements-audit.lock")
    assert "numpy==2.5.1" in _read("requirements-browser.in")
    assert "numpy==2.5.1" in _read("requirements-browser.lock")
    assert "playwright==1.61.0" in _read("requirements-browser.in")
    assert "playwright==1.61.0" in _read("requirements-browser.lock")
    assert "node --check" in workflow
    assert "ruff check engine tools tests" in workflow
    assert "bandit -q -lll -r engine tools" in workflow
    assert "find tools/editor/static -type f -name '*.js' -print0" in workflow
    assert "sort -z" in workflow
    assert "xargs -0 -n1 node --check" in workflow
    assert "Auth bootstrap browser gate" in workflow
    assert "ONEPAGER_DEMO_MODE=true python tools/editor/server.py" in workflow
    assert "curl -fsS http://127.0.0.1:5567/healthz" in workflow
    assert "tests/e2e/test_auth_bootstrap_contracts.py" in workflow
    assert "tests/test_account_purge_worker.py" in workflow
    assert "python engine/build.py" in workflow
    assert 'curl -fsS "$PUBLIC_BASE_URL/healthz"' in workflow
    assert "worker_health" in workflow
    assert 'test "$worker_health" = "healthy"' in workflow
    assert "onepager-erasure-keeper" in workflow
    assert "onepager-erasure-candidate" in workflow
    assert "--volume onepager_onepager_photos:/app/design/photos" in workflow
    assert '--env-file "$NEXT_DIR/.env.docker"' in workflow


def test_continuous_ci_runs_on_push_and_pull_requests_with_locked_dependencies():
    workflow = _read(".github/workflows/ci.yml")
    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "branches: [main]" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    assert "--require-hashes -r requirements.lock" in workflow
    assert "--require-hashes -r requirements-audit.lock" in workflow
    assert "pip_audit -r requirements.lock" in workflow
    assert "ruff check engine tools tests" in workflow
    assert "bandit -q -lll -r engine tools" in workflow
    assert "bandit==1.9.4" in _read("requirements-audit.in")
    assert "ruff==0.15.22" in _read("requirements-audit.in")
    assert "find tools/editor/static -type f -name '*.js' -print0" in workflow
    assert "sort -z" in workflow
    assert "xargs -0 -n1 node --check" in workflow
    assert "pytest tests --ignore=tests/e2e -q" in workflow
    assert "python engine/build.py" in workflow
    assert "docker compose --profile prod config --quiet" in workflow


def test_bundled_fonts_ship_with_their_license_notices():
    inter_license = _read("LICENSES/Inter-OFL.txt")
    source_serif_license = _read("LICENSES/SourceSerif4-OFL.txt")
    codemirror_license = _read("LICENSES/codemirror-5.65.21-MIT.txt")
    js_yaml_license = _read("LICENSES/js-yaml-4.3.2-MIT.txt")
    pagedjs_license = _read("LICENSES/pagedjs-0.4.3-MIT.txt")
    pagedjs_bundled = _read(
        "LICENSES/pagedjs-0.4.3-bundled-dependencies.txt"
    )
    notices = _read("THIRD_PARTY_NOTICES.md")
    readme = _read("README.md")

    assert "Copyright (c) 2016 The Inter Project Authors" in inter_license
    assert "SIL OPEN FONT LICENSE Version 1.1" in inter_license
    assert "Copyright 2014 - 2023 Adobe" in source_serif_license
    assert "Reserved Font Name" in source_serif_license
    assert "SIL OPEN FONT LICENSE Version 1.1" in source_serif_license
    assert "Copyright (C) 2017 by Marijn Haverbeke" in codemirror_license
    assert "Copyright (C) 2011-2015 by Vitaly Puzrin" in js_yaml_license
    assert "Copyright (c) 2018 Adam Hyde" in pagedjs_license
    assert "event-emitter 0.3.5" in pagedjs_bundled
    assert "clear-cut 2.0.2" in pagedjs_bundled
    assert "css-tree 1.1.3" in pagedjs_bundled
    assert "source-map 0.6.1" in pagedjs_bundled
    assert "Mozilla Foundation and contributors" in pagedjs_bundled
    assert "LICENSES/Inter-OFL.txt" in notices
    assert "LICENSES/SourceSerif4-OFL.txt" in notices
    assert "LICENSES/codemirror-5.65.21-MIT.txt" in notices
    assert "LICENSES/js-yaml-4.3.2-MIT.txt" in notices
    assert "LICENSES/pagedjs-0.4.3-MIT.txt" in notices
    assert "LICENSES/pagedjs-0.4.3-bundled-dependencies.txt" in notices
    assert "supabase-js-2.110.0.LICENSE.txt" in notices
    assert "browser-assets.lock.json" in notices
    assert "THIRD_PARTY_NOTICES.md" in readme


def test_icon_assets_are_neutral_pinned_and_licensed():
    lock = json.loads(_read("LICENSES/icon-assets.lock.json"))
    license_text = _read("LICENSES/lucide-1.26.0-ISC-and-Feather-MIT.txt")
    notices = _read("THIRD_PARTY_NOTICES.md")
    icons_js = _read("tools/editor/static/icons.js")
    contact_icons = _read("engine/render/brand_icons.py")

    assert lock["schema_version"] == 2
    assert lock["source"]["package"] == "lucide-static"
    assert lock["source"]["version"] == "1.26.0"
    assert (
        lock["source"]["commit"]
        == "4e50f0d759d1eda269aaeae846cd0149e807353e"
    )
    assert (
        lock["source"]["npm_integrity"]
        == "sha512-6yCpa2ONICjlE19BuneIi75ASd9cCZhqJlzhAlQBi+99m2aZd2cNzxFVbDgPu7JLBZR2uDYO/EpLYtnhGw5Niw=="
    )
    assert (
        lock["source"]["tarball_sha256"]
        == "79c411da11484d3a32584492c6b1ae1673623db4cd437bf9a2ed619abb6ea10d"
    )

    for path, metadata in lock["consumers"].items():
        digest = hashlib.sha256((REPO / path).read_bytes()).hexdigest()
        assert digest == metadata["sha256"]
    assert (
        hashlib.sha256(
            (REPO / lock["source"]["license"]).read_bytes()
        ).hexdigest()
        == lock["source"]["license_sha256"]
    )

    local_mappings = dict(
        re.findall(r'^    "([^"]+)": .*// lucide:([^\s]+)$', icons_js, re.MULTILINE)
    )
    locked_mappings = lock["consumers"][
        "tools/editor/static/icons.js"
    ]["local_key_to_upstream_icon"]
    assert local_mappings == locked_mappings
    assert len(local_mappings) == 65
    detached_sha256 = lock["detached_sha256"]
    assert all(len(digest) == 64 for digest in detached_sha256.values())
    resolved_icon_digests = [
        metadata.get("sha256") or detached_sha256[metadata["sha256_ref"]]
        for metadata in lock["upstream_icons"].values()
    ]
    assert all(len(digest) == 64 for digest in resolved_icon_digests)
    assert len(resolved_icon_digests) == len(set(resolved_icon_digests))

    assert "ISC License" in license_text
    assert "Copyright (c) 2026 Lucide Icons and Contributors" in license_text
    assert "Copyright (c) 2013-present Cole Bemis" in license_text
    assert "lucide-1.26.0-ISC-and-Feather-MIT.txt" in notices
    assert "icon-assets.lock.json" in notices
    assert "No affiliation, sponsorship, or endorsement is implied" in notices
    assert "No third-party brand marks are redistributed" in contact_icons
    assert "_BRAND_PATHS" not in contact_icons
    assert not list((REPO / "design" / "icons").glob("*.svg"))
    assert "M20.447 20.452" not in icons_js + contact_icons


def test_browser_yaml_runtime_is_patched_and_local():
    index = _read("tools/editor/static/index.html")

    assert "/static/vendor/js-yaml-4.3.2/js-yaml.min.js" in index
    assert "js-yaml@4.1.0" not in index
    assert "js-yaml@4.1.1" not in index
    assert (REPO / "tools/editor/static/vendor/js-yaml-4.3.2/js-yaml.min.js").is_file()
    assert not (REPO / "tests/e2e/vendor/js-yaml-4.3.0.min.js").exists()
    assert not (REPO / "tools/editor/static/vendor/js-yaml-4.3.0").exists()
    assert not (REPO / "tests/e2e/vendor/js-yaml-4.1.0.min.js").exists()
    assert not (REPO / "tests/e2e/vendor/js-yaml-4.1.1.min.js").exists()

    pollution_probe = """
const yaml = require("./tools/editor/static/vendor/js-yaml-4.3.2/js-yaml.min.js");
delete Object.prototype.polluted;
yaml.load("base: &base\\n  __proto__:\\n    polluted: true\\ntarget:\\n  <<: *base\\n");
if (Object.prototype.polluted !== undefined || ({}).polluted !== undefined) {
  process.exit(1);
}
const chain = ["a0: &a0 { k0: 0 }"];
for (let i = 1; i < 300; i += 1) {
  chain.push(`a${i}: &a${i} { <<: *a${i - 1}, k${i}: ${i} }`);
}
chain.push("b: *a299");
try {
  yaml.load(chain.join("\\n"));
  process.exit(2);
} catch (error) {
  if (!String(error.message).includes("maxTotalMergeKeys")) {
    process.exit(3);
  }
}
"""
    subprocess.run(
        ["node", "-e", pollution_probe],
        cwd=REPO,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_browser_vendor_lock_covers_every_runtime_js_and_css_byte():
    manifest = json.loads(
        _read("tools/editor/static/vendor/browser-assets.lock.json")
    )
    assert manifest["schema"] == 1
    assert manifest["hash_algorithm"] == "sha256"
    assert {
        name: package["version"]
        for name, package in manifest["packages"].items()
    } == {
        "@supabase/supabase-js": "2.110.0",
        "codemirror": "5.65.21",
        "js-yaml": "4.3.2",
        "pagedjs": "0.4.3",
    }
    pagedjs = manifest["packages"]["pagedjs"]
    assert pagedjs["bundled_components"] == {
        "clear-cut": {"version": "2.0.2", "license": "MIT"},
        "css-tree": {"version": "1.1.3", "license": "MIT"},
        "event-emitter": {"version": "0.3.5", "license": "MIT"},
        "source-map": {"version": "0.6.1", "license": "BSD-3-Clause"},
    }
    assert (
        pagedjs["bundled_component_notices"]
        == "LICENSES/pagedjs-0.4.3-bundled-dependencies.txt"
    )

    locked_assets = {
        path: digest
        for package in manifest["packages"].values()
        for path, digest in package["assets"].items()
    }
    vendor_root = REPO / "tools" / "editor" / "static" / "vendor"
    shipped_assets = {
        str(path.relative_to(REPO))
        for path in vendor_root.rglob("*")
        if path.is_file() and path.suffix in {".js", ".css"}
    }
    assert set(locked_assets) == shipped_assets

    for relative, expected in sorted(locked_assets.items()):
        payload = (REPO / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected, relative


def test_nested_browser_vendor_javascript_passes_node_syntax_check():
    vendor_root = REPO / "tools" / "editor" / "static" / "vendor"
    scripts = sorted(vendor_root.rglob("*.js"))
    assert scripts
    assert any(path.parent != vendor_root for path in scripts)
    for script in scripts:
        subprocess.run(
            ["node", "--check", str(script)],
            cwd=REPO,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def test_production_browser_runtime_has_no_executable_or_stylesheet_cdn():
    index = _read("tools/editor/static/index.html")
    preview = _read("tools/editor/render_preview.py")
    server = _read("tools/editor/server.py")

    for source in (index, preview, server):
        assert "cdn.jsdelivr.net" not in source
        assert "unpkg.com" not in source

    assert "codemirror-5.65.21" in index
    assert "/static/vendor/js-yaml-4.3.2/js-yaml.min.js" in index
    assert 'PAGEDJS_PATH = "/static/vendor/pagedjs-0.4.3/paged.polyfill.js"' in preview

    remote_executable = re.compile(
        r"""<(?:script|link)\b[^>]*(?:src|href)=["']https?://""",
        re.IGNORECASE,
    )
    for html_path in (REPO / "tools" / "editor" / "static").glob("*.html"):
        assert not remote_executable.search(
            html_path.read_text(encoding="utf-8")
        ), html_path


# --------------------------------------------------------------------------- #
# requirements.txt — every dep has an upper bound
# --------------------------------------------------------------------------- #

def _requirement_lines() -> list[str]:
    out: list[str] = []
    for raw in _read("requirements.txt").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def test_every_requirement_has_an_upper_bound():
    offenders: list[str] = []
    for line in _requirement_lines():
        # An upper bound is expressed as "<" or "<=" somewhere in the spec.
        if "<" not in line:
            offenders.append(line)
    assert not offenders, f"requirements without an upper bound: {offenders}"


# --------------------------------------------------------------------------- #
# External template manifests — pinned provenance, no executable intake
# --------------------------------------------------------------------------- #

def _valid_template_manifest() -> dict:
    sha = "0123456789abcdef0123456789abcdef01234567"
    return {
        "id": "github-classic",
        "label": "GitHub Classic",
        "description": "Reviewed external template metadata.",
        "renderer": "standard",
        "stylesheet": "cv",
        "thumbnail": "thumbnails/github-classic.png",
        "source_url": f"https://raw.githubusercontent.com/acme/cv-templates/{sha}/template_manifest.json",
        "commit": sha,
        "license": "MIT",
        "compatibility_version": "1",
        "layout_family": "single_column",
        "column_count": 1,
        "inspiration_source": "Reviewed external single-column layout note",
        "ats_priority": "high",
        "customization_axes": ["accent", "font", "density", "paper", "section_order"],
        "paper_sizes": ["A4"],
        "import_export_notes": "Metadata only; no external renderer code imported.",
        "assets": [
            {
                "path": "thumbnails/github-classic.png",
                "sha256": "sha256:" + "a" * 64,
            }
        ],
    }


def test_external_template_manifest_accepts_pinned_github_raw_source():
    from render import template_manifest

    parsed = template_manifest.validate_manifest(_valid_template_manifest())
    assert parsed.id == "github-classic"
    assert parsed.commit == "0123456789abcdef0123456789abcdef01234567"
    assert parsed.license == "MIT"
    assert parsed.layout_family == "single_column"
    assert parsed.column_count == 1
    assert parsed.ats_priority == "high"
    assert parsed.paper_sizes == ("A4",)


def test_external_template_manifest_rejects_unpinned_branch_url():
    from render import template_manifest

    data = _valid_template_manifest()
    data["source_url"] = "https://raw.githubusercontent.com/acme/cv-templates/main/template_manifest.json"
    data["commit"] = "main"
    try:
        template_manifest.validate_manifest(data)
    except ValueError as exc:
        assert "pinned" in str(exc)
    else:
        raise AssertionError("unpinned branch source was accepted")


def test_external_template_manifest_rejects_path_traversal_and_code_assets():
    from render import template_manifest

    data = _valid_template_manifest()
    data["thumbnail"] = "../secret.png"
    try:
        template_manifest.validate_manifest(data)
    except ValueError as exc:
        assert "traverse" in str(exc)
    else:
        raise AssertionError("path traversal thumbnail was accepted")

    data = _valid_template_manifest()
    data["assets"] = [{"path": "scripts/install.py", "sha256": "sha256:" + "b" * 64}]
    try:
        template_manifest.validate_manifest(data)
    except ValueError as exc:
        assert "executable" in str(exc)
    else:
        raise AssertionError("executable template asset was accepted")


def test_external_template_manifest_rejects_duplicate_ids():
    from render import template_manifest

    try:
        template_manifest.validate_manifest(_valid_template_manifest(), known_ids={"github-classic"})
    except ValueError as exc:
        assert "duplicates" in str(exc)
    else:
        raise AssertionError("duplicate template id was accepted")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("layout_family", "react-app", "layout_family"),
        ("column_count", 3, "column_count"),
        ("ats_priority", "vendor-certified", "ats_priority"),
        ("customization_axes", ["accent", "script"], "customization_axes"),
        ("paper_sizes", ["Letter"], "paper_sizes"),
    ],
)
def test_external_template_manifest_rejects_unapproved_metadata(field, value, message):
    from render import template_manifest

    data = _valid_template_manifest()
    data[field] = value
    try:
        template_manifest.validate_manifest(data)
    except ValueError as exc:
        assert message in str(exc)
    else:
        raise AssertionError(f"unapproved {field} was accepted")


def test_external_template_manifest_rejects_url_as_inspiration_source():
    from render import template_manifest

    data = _valid_template_manifest()
    data["inspiration_source"] = "https://example.com/template"
    try:
        template_manifest.validate_manifest(data)
    except ValueError as exc:
        assert "inspiration_source" in str(exc)
    else:
        raise AssertionError("URL inspiration source was accepted")


def test_every_requirement_has_a_lower_bound():
    """Sanity: we kept the existing >= floors while adding ceilings."""
    offenders = [ln for ln in _requirement_lines() if ">=" not in ln]
    assert not offenders, f"requirements missing a lower bound: {offenders}"
