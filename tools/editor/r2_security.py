"""Shared validation for Cloudflare R2 endpoint configuration."""
from __future__ import annotations

import re


_ACCOUNT_ID_RE = re.compile(r"[0-9a-fA-F]{32}\Z")


def validate_r2_account_id(value: str) -> str:
    """Return a valid Cloudflare account id or raise without echoing it."""
    account_id = str(value).strip()
    if not _ACCOUNT_ID_RE.fullmatch(account_id):
        raise ValueError("R2_ACCOUNT_ID must be exactly 32 hexadecimal characters")
    return account_id
