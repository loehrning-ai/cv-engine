"""Regression tests: the editor save/preview path tolerates half-finished
form entries, while the CLI build stays strict.

Origin: e2e failure after two legitimate fixes collided —
  * the form's save gate was removed (saves always proceed, permissive by
    design per form.js), and
  * validation was tightened to treat whitespace-only required fields as
    missing.
A freshly added blank experience card then made every autosave 400
("experience[4] missing required field 'role'"), freezing the preview and
surfacing console errors — the same data-loss class the gate removal was
meant to fix, one layer down.

Resolution: ``content.load(..., strict_required=False)`` for the editor
routes (save validation, preview, outline); the renderers skip items whose
required fields are blank. ``engine/build.py`` keeps the default strict
load, so hand-authored CVs still fail the build on missing fields.
"""
from __future__ import annotations

import textwrap

import pytest
from render import content as content_mod
from render import templates

_BASE = """\
name: Jane Doe
contact:
  - label: Email
    username: jane@example.com
experience:
  - role: Engineer
    company: Acme
    start: "2020"
    end: Present
    bullets:
      - Shipped things.
"""

_BLANK_ENTRY = """\
  - role: ""
    company: ""
    start: ""
    end: ""
"""

_PARTIAL_ENTRY = """\
  - role: Intern
    company: ""
    start: ""
    end: ""
"""


def _write(tmp_path, body: str):
    p = tmp_path / "cv.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_strict_load_rejects_blank_required_fields(tmp_path):
    p = _write(tmp_path, _BASE.rstrip() + "\n" + _BLANK_ENTRY)
    with pytest.raises(ValueError, match="missing required field"):
        content_mod.load(p)


def test_strict_load_rejects_partial_entry(tmp_path):
    p = _write(tmp_path, _BASE.rstrip() + "\n" + _PARTIAL_ENTRY)
    with pytest.raises(ValueError, match="missing required field"):
        content_mod.load(p)


def test_lenient_load_accepts_half_finished_entry(tmp_path):
    p = _write(tmp_path, _BASE.rstrip() + "\n" + _BLANK_ENTRY)
    cv = content_mod.load(p, strict_required=False)
    assert len(cv["experience"]) == 2


def test_render_skips_incomplete_items(tmp_path):
    p = _write(tmp_path, _BASE.rstrip() + "\n" + _PARTIAL_ENTRY)
    cv = content_mod.load(p, strict_required=False)
    html = templates.render_body(cv)
    assert "Engineer" in html
    assert "Intern" not in html  # incomplete entry is not rendered


def test_render_omits_section_when_all_items_incomplete(tmp_path):
    body = _BASE + textwrap.dedent(
        """\
        education:
          - degree: ""
            school: ""
            start: ""
            end: ""
        """
    )
    p = _write(tmp_path, body)
    cv = content_mod.load(p, strict_required=False)
    html = templates.render_body(cv)
    assert "Education" not in html
    assert "Engineer" in html


def test_editor_save_accepts_blank_entry(client, fake_supabase):
    """POST /api/cv with a freshly added blank card must not 400."""
    res = client.post("/api/cvs", json={"name": "lenient-test"})
    assert res.status_code == 201, res.get_data(as_text=True)
    cid = res.get_json()["id"]
    try:
        res = client.post(
            "/api/cv",
            json={"id": cid, "content": _BASE.rstrip() + "\n" + _BLANK_ENTRY},
        )
        assert res.status_code == 200, res.get_data(as_text=True)
        assert res.get_json()["saved"] is True
    finally:
        client.delete(f"/api/cv/{cid}")


def test_editor_save_still_rejects_missing_top_level(client, fake_supabase):
    """Leniency is scoped to per-item required fields only."""
    res = client.post("/api/cvs", json={"name": "lenient-test-2"})
    assert res.status_code == 201, res.get_data(as_text=True)
    cid = res.get_json()["id"]
    try:
        res = client.post(
            "/api/cv",
            json={"id": cid, "content": "contact: []\nexperience: []\n"},
        )
        assert res.status_code == 400
    finally:
        client.delete(f"/api/cv/{cid}")
