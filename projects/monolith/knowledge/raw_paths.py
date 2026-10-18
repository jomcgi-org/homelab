"""Content-addressed raw input IDs."""

from __future__ import annotations

import hashlib


def compute_raw_id(content: str) -> str:
    """Return sha256 hex digest of content encoded as UTF-8."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
