"""Convert a quoted Compose dotenv file into Docker ``--env-file`` syntax.

Compose strips surrounding double quotes. Docker preserves them, which would
silently corrupt every URL and secret if the same file were passed to both.
The deploy workflow writes JSON-compatible quoted strings; this helper decodes
them and emits unquoted Docker entries. NULs and line breaks are rejected
because Docker's env-file format cannot represent them faithfully.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def read_values(source: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(source.read_text().splitlines(), 1):
        if not line:
            continue
        key, separator, encoded = line.partition("=")
        if not separator or not key:
            raise ValueError(f"invalid Compose env line {number}")
        value = json.loads(encoded)
        if not isinstance(value, str):
            raise ValueError(f"non-string Compose env value on line {number}")
        if any(character in value for character in ("\0", "\r", "\n")):
            raise ValueError(f"Docker-incompatible env value on line {number}")
        if key in values:
            raise ValueError(f"duplicate Compose env key on line {number}")
        values[key] = value
    return values


def convert(source: Path, target: Path) -> None:
    values = read_values(source)
    lines = [f"{key}={value}" for key, value in values.items()]
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write("\n".join(lines) + "\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) == 3 and args[0] == "--get":
            values = read_values(Path(args[1]))
            key = args[2]
            if key not in values:
                raise ValueError("requested Compose env key is missing")
            print(values[key])
            return 0
        if len(args) == 2:
            convert(Path(args[0]), Path(args[1]))
            return 0
        print(
            "usage: compose_env_to_docker.py SOURCE TARGET | --get SOURCE KEY",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"env conversion failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
