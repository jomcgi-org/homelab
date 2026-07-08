"""Helpers for computing raw IDs and target paths under _raw/."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from knowledge.gardener import _slugify

RAW_ROOT_NAME = "_raw"
GRANDFATHERED_SUBDIR = "grandfathered"
_HASH_PREFIX_LEN = 8


def compute_raw_id(content: str) -> str:
    """Return sha256 hex digest of content encoded as UTF-8."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def raw_target_path(
    *,
    vault_root: Path,
    raw_id: str,
    title: str,
    created_at: datetime | None = None,
    grandfathered: bool = False,
) -> Path:
    """Build the target path under vault_root/_raw/ for a raw input.

    Ongoing ingests go under _raw/YYYY/MM/DD/<hash-prefix>-<slug>.md;
    grandfathered files go under _raw/grandfathered/<hash-prefix>-<slug>.md.
    """
    prefix = raw_id[:_HASH_PREFIX_LEN]
    slug = _slugify(title)
    filename = f"{prefix}-{slug}.md"

    if grandfathered:
        return vault_root / RAW_ROOT_NAME / GRANDFATHERED_SUBDIR / filename

    if created_at is None:
        raise ValueError("created_at is required unless grandfathered=True")
    y = f"{created_at.year:04d}"
    m = f"{created_at.month:02d}"
    d = f"{created_at.day:02d}"
    return vault_root / RAW_ROOT_NAME / y / m / d / filename
