"""Tests for engine/render/importers.py."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from render import importers  # noqa: E402


RENDERCV_SAMPLE = """
cv:
  name: Alice Example
  email: alice@example.com
  phone: +1-555-0100
  website: https://alice.dev
  social_networks:
    - network: LinkedIn
      username: aliceex
    - network: GitHub
      username: aliceex
  sections:
    experience:
      - company: Acme Corp
        position: Senior Engineer
        location: Berlin
        start_date: 2022-03
        end_date: present
        highlights:
          - Built a thing.
          - Led a team of three.
    education:
      - institution: MIT
        degree: B.Sc.
        area: Computer Science
        location: Cambridge, MA
        start_date: 2017
        end_date: 2021
"""


PLAIN_TEXT_SAMPLE = """\
Alice Example
alice@example.com · +1 555 0100 · alice.dev · linkedin.com/in/aliceex · github.com/aliceex

EXPERIENCE

Acme Corp — Senior Engineer    Berlin   2022 – Present
• Led a team of three.
• Shipped the v2 API.

WidgetCo — Engineer    Remote   2019 – 2022
• Refactored monolith.

EDUCATION

MIT — B.Sc. Computer Science    Cambridge, MA   2015 – 2019

SKILLS
Languages: Python, TypeScript
Cloud: AWS, GCP
"""


class TestFromRendercv:
    def test_top_level_fields(self):
        cv = importers.from_rendercv(RENDERCV_SAMPLE)
        assert cv["name"] == "Alice Example"
        assert cv["accent"] == "#111111"
        assert cv["font"] == "serif"

    def test_contact_includes_brand_networks(self):
        cv = importers.from_rendercv(RENDERCV_SAMPLE)
        networks = [c.get("network") for c in cv["contact"]]
        assert "mail" in networks
        assert "phone" in networks
        assert "web" in networks
        assert "linkedin" in networks
        assert "github" in networks

    def test_experience_mapped(self):
        cv = importers.from_rendercv(RENDERCV_SAMPLE)
        e = cv["experience"][0]
        assert e["role"] == "Senior Engineer"
        assert e["company"] == "Acme Corp"
        assert e["location"] == "Berlin"
        assert e["start"] == "2022-03"
        assert e["end"] == "present"
        assert "Built a thing." in e["bullets"]

    def test_education_combines_degree_and_area(self):
        cv = importers.from_rendercv(RENDERCV_SAMPLE)
        ed = cv["education"][0]
        assert ed["school"] == "MIT"
        assert "B.Sc." in ed["degree"]
        assert "Computer Science" in ed["degree"]


class TestFromPlainText:
    def test_extracts_name(self):
        cv = importers.from_plain_text(PLAIN_TEXT_SAMPLE)
        assert cv["name"] == "Alice Example"

    def test_contact_picks_email_phone_socials(self):
        cv = importers.from_plain_text(PLAIN_TEXT_SAMPLE)
        networks = [(c.get("network"), c.get("username")) for c in cv["contact"]]
        assert ("mail", "alice@example.com") in networks
        assert ("linkedin", "aliceex") in networks
        assert ("github", "aliceex") in networks

    def test_experience_company_left_role_right(self):
        cv = importers.from_plain_text(PLAIN_TEXT_SAMPLE)
        first = cv["experience"][0]
        assert first["company"] == "Acme Corp"
        assert first["role"] == "Senior Engineer"
        assert first["location"] == "Berlin"
        assert first["start"] == "2022"
        assert first["end"] == "Present"
        assert any("Led a team" in b for b in first["bullets"])

    def test_education_role_company_swapped(self):
        cv = importers.from_plain_text(PLAIN_TEXT_SAMPLE)
        ed = cv["education"][0]
        assert ed["school"] == "MIT"
        assert "B.Sc." in ed["degree"]

    def test_skills_label_value_split(self):
        cv = importers.from_plain_text(PLAIN_TEXT_SAMPLE)
        labels = {s["label"]: s["items"] for s in cv["skills"]}
        assert labels["Languages"] == "Python, TypeScript"
        assert labels["Cloud"] == "AWS, GCP"


class TestEdgeCases:
    def test_minimal_resume_does_not_crash(self):
        cv = importers.from_plain_text("Some Name\nemail@example.com\n")
        assert cv["name"] == "Some Name"
        assert any(c.get("network") == "mail" for c in cv["contact"])

    def test_empty_text_returns_skeleton(self):
        cv = importers.from_plain_text("")
        assert cv["name"] == "Your Name"
        assert cv["contact"]  # at least the placeholder

    def test_rendercv_without_cv_wrapper(self):
        """Some examples are flat without ``cv:`` — still works."""
        flat = """
name: Bob
email: bob@example.com
sections:
  experience:
    - company: X
      position: Y
      start_date: 2020
      end_date: 2021
"""
        cv = importers.from_rendercv(flat)
        assert cv["name"] == "Bob"
