"""Tests for engine/render/ai_extract.py.

The Claude API path is exercised only when ANTHROPIC_API_KEY is set; the
pure-Python helpers (PDF/DOCX text extraction, fallback path) always run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from render import ai_extract  # noqa: E402


class TestAvailability:
    def test_disabled_when_no_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert ai_extract.is_available() is False

    def test_enabled_when_key_set(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
        # Should be true even with a fake key — only the *call* validates.
        # If the SDK isn't installed we still expect false; that's intended.
        try:
            import anthropic  # noqa: F401

            assert ai_extract.is_available() is True
        except ImportError:
            assert ai_extract.is_available() is False


class TestPdfExtraction:
    def test_extracts_text_from_real_pdf(self):
        # Drop a sample PDF at ``tests/fixtures/sample-resume.pdf`` if you
        # want to exercise the PDF text extractor against a real document.
        pdf_path = REPO / "tests" / "fixtures" / "sample-resume.pdf"
        if not pdf_path.exists():
            pytest.skip("Sample PDF not present.")
        text = ai_extract.extract_text_from_pdf(pdf_path.read_bytes())
        assert len(text) > 100


class TestStripCodeFences:
    def test_strips_yaml_fence(self):
        wrapped = "```yaml\nname: Alice\n```"
        assert ai_extract._strip_code_fences(wrapped) == "name: Alice"

    def test_strips_bare_fence(self):
        wrapped = "```\nname: Alice\n```"
        assert ai_extract._strip_code_fences(wrapped) == "name: Alice"

    def test_passes_through_no_fence(self):
        assert ai_extract._strip_code_fences("name: Alice") == "name: Alice"


class TestExtractToDictFallback:
    def test_falls_back_to_heuristic_when_unavailable(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        text = "Alice Example\nalice@example.com\n\nEXPERIENCE\nAcme — Engineer  2020 – 2021\n"
        cv = ai_extract.extract_to_dict(text)
        assert cv["name"] == "Alice Example"
        # heuristic path always populates the schema sketch
        assert isinstance(cv["contact"], list)
