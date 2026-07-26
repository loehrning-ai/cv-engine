#!/usr/bin/env python3
"""Validate a reviewed external template manifest.

This command is deliberately offline: it reads a local JSON file, validates
provenance and asset metadata, and exits non-zero on any guardrail violation.
It does not fetch GitHub content and does not execute template code.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))

from render import template_manifest, template_registry  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: tools/validate_template_manifest.py path/to/template_manifest.json", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        parsed = template_manifest.validate_manifest(
            data,
            known_ids=set(template_registry.available_template_ids()),
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print a concise validation error
        print(f"invalid template manifest: {exc}", file=sys.stderr)
        return 1
    print(f"valid template manifest: {parsed.id} ({parsed.commit})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
