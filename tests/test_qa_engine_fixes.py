"""Regression tests for QA-verified importer + render/build engine fixes.

Covers:
  * importers: regex-DoS blob cap, YAML alias-bomb rejection, education
    ``note`` precedence, non-mapping ``sections`` error.
  * ai_extract: DOCX decompression cap (zip bomb), extracted-text caps.
  * content: empty required-field values, temp-path leak in error messages,
    ``photo:`` data-URI validation, case-duplicate outline keys.
  * templates: case-duplicate custom sections render one heading.
  * build.py: ``--output``/``--skip-pdf`` no longer clobbers output/cv.html,
    friendly validation errors instead of tracebacks.
"""
from __future__ import annotations

import base64
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

from render import ai_extract, content, importers, templates  # noqa: E402


VALID_CV = """\
name: QA Person
contact:
  - label: mail
experience:
  - role: Engineer
    company: Acme
    location: Berlin
    start: '2020'
    end: Present
    bullets:
      - Did a thing.
"""


def _load(tmp_path, src: str):
    p = tmp_path / "cv.yaml"
    p.write_text(src, encoding="utf-8")
    return content.load(p)


def _make_docx(body_xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", body_xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# P1 — quadratic regex DoS in _guess_contact (blob is capped before regexing)
# ---------------------------------------------------------------------------
class TestContactBlobCap:
    def test_huge_unbroken_line_parses_fast(self):
        # A 1MB single \w line with no @ used to take >15s (O(n^2) backtracking).
        import time

        start = time.time()
        out = importers.from_plain_text("Jane Doe\n" + "x" * 1_000_000)
        assert time.time() - start < 5
        assert out["name"] == "Jane Doe"

    def test_blob_is_capped(self):
        # Deterministic proof of the cap: an email sitting past the 5000-char
        # cap is no longer seen (falls back to the placeholder contact).
        out = importers.from_plain_text("Jane Doe\n" + "x" * 6000 + " jane@example.com")
        usernames = [c.get("username") for c in out["contact"]]
        assert "jane@example.com" not in usernames

    def test_normal_contact_block_still_found(self):
        out = importers.from_plain_text("Jane Doe\njane@example.com\n")
        assert any(c.get("username") == "jane@example.com" for c in out["contact"])


# ---------------------------------------------------------------------------
# P1 — YAML alias bomb rejected after safe_load
# ---------------------------------------------------------------------------
class TestYamlAliasBomb:
    def _bomb(self, depth: int = 12, fanout: int = 10) -> str:
        doc = "l0: &l0 [a]\n"
        prev = "l0"
        for i in range(1, depth + 1):
            doc += f"l{i}: &l{i} [*{prev}" + f", *{prev}" * (fanout - 1) + "]\n"
            prev = f"l{i}"
        doc += f"cv:\n  name: X\n  sections:\n    other: *l{depth}\n"
        return doc

    def test_deep_alias_chain_rejected(self):
        with pytest.raises(ValueError, match="aliases"):
            importers.from_rendercv(self._bomb())

    def test_cyclic_structure_rejected(self):
        with pytest.raises(ValueError, match="aliases"):
            importers.from_rendercv("a: &x {self: *x}\ncv:\n  name: Y\n")

    def test_anchors_free_doc_unaffected(self):
        src = (
            "cv:\n  name: Bob\n  sections:\n"
            "    experience:\n"
            "      - company: A\n        position: Eng\n        highlights: [x, x, x]\n"
            "      - company: A\n        position: Eng\n        highlights: [x, x, x]\n"
        )
        cv = importers.from_rendercv(src)
        assert cv["name"] == "Bob"
        assert len(cv["experience"]) == 2

    def test_scalar_alias_unaffected(self):
        # Only shared MUTABLE graphs are rejected; a scalar alias is harmless.
        cv = importers.from_rendercv("cv:\n  name: &n Bob\n  accent: '#111111'\n")
        assert cv["name"] == "Bob"


# ---------------------------------------------------------------------------
# P2 — DOCX decompression cap
# ---------------------------------------------------------------------------
class TestDocxDecompressionCap:
    NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def test_zip_bomb_rejected(self):
        para = f'<w:p><w:r><w:t>{"x" * 1000}</w:t></w:r></w:p>'.encode()
        big = (
            f'<w:document xmlns:w="{self.NS}"><w:body>'.encode()
            + para * 38000  # ~37MB decompressed, well under 100KB compressed
            + b"</w:body></w:document>"
        )
        bomb = _make_docx(big)
        assert len(bomb) < 1024 * 1024  # sanity: small on the wire
        with pytest.raises(ValueError, match="too large"):
            ai_extract.extract_text_from_docx(bomb)

    def test_valid_docx_still_extracts(self):
        small = (
            f'<w:document xmlns:w="{self.NS}"><w:body>'
            f"<w:p><w:r><w:t>Alice Example</w:t></w:r></w:p>"
            f"<w:p><w:r><w:t>Engineer</w:t></w:r></w:p>"
            f"</w:body></w:document>"
        ).encode()
        text = ai_extract.extract_text_from_docx(_make_docx(small))
        assert text == "Alice Example\n\nEngineer"

    @pytest.mark.parametrize(
        "declaration",
        [
            '<!ENTITY hidden "expanded">',
            '<!ENTITY hidden SYSTEM "file:///etc/passwd">',
        ],
    )
    def test_xml_entities_are_rejected(self, declaration):
        malicious = (
            f'<!DOCTYPE w:document [{declaration}]>'
            f'<w:document xmlns:w="{self.NS}"><w:body>'
            f"<w:p><w:r><w:t>&hidden;</w:t></w:r></w:p>"
            f"</w:body></w:document>"
        ).encode()
        with pytest.raises(ValueError, match="forbidden XML"):
            ai_extract.extract_text_from_docx(_make_docx(malicious))

    def test_malformed_xml_has_safe_error(self):
        malformed = f'<w:document xmlns:w="{self.NS}"><w:body>'.encode()
        with pytest.raises(ValueError, match="malformed"):
            ai_extract.extract_text_from_docx(_make_docx(malformed))

    def test_extracted_text_capped(self, monkeypatch):
        monkeypatch.setattr(ai_extract, "_MAX_EXTRACTED_CHARS", 10)
        small = (
            f'<w:document xmlns:w="{self.NS}"><w:body>'
            f"<w:p><w:r><w:t>abcdefghij</w:t></w:r></w:p>"
            f"<w:p><w:r><w:t>klmnopqrst</w:t></w:r></w:p>"
            f"</w:body></w:document>"
        ).encode()
        text = ai_extract.extract_text_from_docx(_make_docx(small))
        assert len(text) <= 10

    def test_pdf_text_capped(self, monkeypatch):
        class _Page:
            def __init__(self, text):
                self._text = text

            def extract_text(self):
                return self._text

        class _Reader:
            def __init__(self, _stream):
                self.pages = [_Page("abcdefghij"), _Page("klmnopqrst")]

        import pypdf

        monkeypatch.setattr(pypdf, "PdfReader", _Reader)
        monkeypatch.setattr(ai_extract, "_MAX_EXTRACTED_CHARS", 12)
        assert len(ai_extract.extract_text_from_pdf(b"%PDF-fake")) <= 12


# ---------------------------------------------------------------------------
# P2 — education ``note`` no longer dropped by an operator-precedence bug
# ---------------------------------------------------------------------------
class TestEducationNote:
    def test_explicit_note_preserved(self):
        cv = importers.from_rendercv(
            "cv:\n  sections:\n    education:\n"
            "      - institution: MIT\n        note: 'Graduated with honors'\n"
        )
        assert cv["education"][0]["note"] == "Graduated with honors"

    def test_note_falls_back_to_first_highlight(self):
        cv = importers.from_rendercv(
            "cv:\n  sections:\n    education:\n"
            "      - institution: MIT\n        highlights: ['Dean list']\n"
        )
        assert cv["education"][0]["note"] == "Dean list"

    def test_no_note_no_highlights_gives_empty(self):
        cv = importers.from_rendercv(
            "cv:\n  sections:\n    education:\n      - institution: MIT\n"
        )
        assert cv["education"][0]["note"] == ""


# ---------------------------------------------------------------------------
# P3 — non-mapping ``sections`` raises a friendly ValueError, not TypeError
# ---------------------------------------------------------------------------
class TestSectionsTypeCheck:
    def test_list_sections_rejected(self):
        with pytest.raises(ValueError, match="'sections' must be a mapping"):
            importers.from_rendercv("cv:\n  sections: [experience]\n")

    def test_string_sections_rejected(self):
        with pytest.raises(ValueError, match="'sections' must be a mapping"):
            importers.from_rendercv("cv:\n  sections: experience\n")


# ---------------------------------------------------------------------------
# P3 — photo data-URI validated on the engine path
# ---------------------------------------------------------------------------
class TestPhotoValidation:
    def _png_data_uri(self) -> str:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (10, 10), (255, 0, 0)).save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    def test_svg_data_uri_rejected(self, tmp_path):
        svg = "data:image/svg+xml;base64," + base64.b64encode(
            b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        ).decode()
        with pytest.raises(ValueError, match="photo: SVG images are not allowed"):
            _load(tmp_path, VALID_CV + f'photo: "{svg}"\n')

    def test_valid_png_data_uri_accepted(self, tmp_path):
        cv = _load(tmp_path, VALID_CV + f'photo: "{self._png_data_uri()}"\n')
        assert cv["photo"].startswith("data:image/png;base64,")

    def test_relative_photo_path_passes_through(self, tmp_path):
        cv = _load(tmp_path, VALID_CV + "photo: design/photos/me/photo.png\n")
        assert cv["photo"] == "design/photos/me/photo.png"


# ---------------------------------------------------------------------------
# P3 — empty/whitespace required field values fail validation
# ---------------------------------------------------------------------------
class TestEmptyRequiredFields:
    def test_empty_string_title_rejected(self, tmp_path):
        with pytest.raises(ValueError, match=r"projects\[0\] missing required field 'title'"):
            _load(tmp_path, VALID_CV + "projects:\n  - title: ''\n")

    def test_whitespace_only_title_rejected(self, tmp_path):
        with pytest.raises(ValueError, match=r"projects\[0\] missing required field 'title'"):
            _load(tmp_path, VALID_CV + "projects:\n  - title: '   '\n")

    def test_none_required_field_rejected(self, tmp_path):
        src = (
            "name: QA Person\ncontact:\n  - label: mail\n"
            "experience:\n  - role:\n    company: C\n    start: '2020'\n    end: P\n"
        )
        with pytest.raises(ValueError, match="missing required field 'role'"):
            _load(tmp_path, src)

    def test_valid_cv_still_loads(self, tmp_path):
        cv = _load(tmp_path, VALID_CV)
        assert cv["name"] == "QA Person"


# ---------------------------------------------------------------------------
# P3 — validation errors no longer leak the full temp-file path
# ---------------------------------------------------------------------------
class TestErrorPathLeak:
    def test_error_contains_basename_not_directory(self, tmp_path):
        p = tmp_path / "cv.yaml"
        p.write_text("name: Only Name\n", encoding="utf-8")
        with pytest.raises(ValueError) as excinfo:
            content.load(p)
        msg = str(excinfo.value)
        assert str(tmp_path) not in msg
        assert "cv.yaml: missing required field 'contact'" in msg

    def test_missing_file_name_still_in_message(self, tmp_path):
        with pytest.raises(ValueError, match="cv.yaml: top-level YAML"):
            _load(tmp_path, "- just\n- a\n- list\n")


# ---------------------------------------------------------------------------
# P3 — case-duplicate sections render/outline once
# ---------------------------------------------------------------------------
class TestCaseDuplicateSections:
    def test_outline_skips_case_duplicate(self):
        import yaml

        cv = yaml.safe_load(VALID_CV + "Experience:\n  - title: dup\nawards:\n  - title: a\n")
        slugs = [o["slug"] for o in content.section_outline(cv)]
        assert "Experience" not in slugs
        assert "awards" in slugs

    def test_render_emits_single_heading(self, tmp_path):
        cv = _load(tmp_path, VALID_CV + "Experience:\n  - title: dup\n")
        html = templates.render_body(cv)
        assert html.count('class="section-title"') == 1

    def test_lone_capitalized_key_still_renders(self, tmp_path):
        cv = _load(tmp_path, VALID_CV + "Certifications:\n  - title: AWS SA\n")
        html = templates.render_body(cv)
        assert ">Certifications<" in html


# ---------------------------------------------------------------------------
# P2/P3 — build.py honours --output for the HTML sibling; friendly errors
# ---------------------------------------------------------------------------
class TestBuildCli:
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO / "engine" / "build.py"), *args],
            capture_output=True,
            text=True,
            cwd=REPO,
        )

    def test_custom_output_does_not_clobber_repo_html(self, tmp_path):
        cfg = tmp_path / "cv.yaml"
        cfg.write_text(VALID_CV, encoding="utf-8")
        repo_html = REPO / "output" / "cv.html"
        before = repo_html.read_bytes() if repo_html.exists() else None

        out_pdf = tmp_path / "out.pdf"
        result = self._run("--config", str(cfg), "--output", str(out_pdf), "--skip-pdf")

        assert result.returncode == 0, result.stderr
        assert (tmp_path / "out.html").exists()
        after = repo_html.read_bytes() if repo_html.exists() else None
        assert before == after

    def test_invalid_config_returns_1_without_traceback(self, tmp_path):
        cfg = tmp_path / "cv.yaml"
        cfg.write_text("name: Only Name\n", encoding="utf-8")
        result = self._run("--config", str(cfg), "--output", str(tmp_path / "x.pdf"))
        assert result.returncode == 1
        assert "missing required field 'contact'" in result.stderr
        assert "Traceback" not in result.stderr
        assert str(tmp_path) not in result.stderr
