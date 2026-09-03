"""Shared knowledge-pipeline constants and slug normalization."""

from __future__ import annotations

import re
import unicodedata

from knowledge.extraction import GARDENER_VERSION

MAX_GARDENER_RETRIES = 3

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text_in: str) -> str:
    normalized = unicodedata.normalize("NFKD", text_in)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_only.lower()).strip("-")
    return slug or "note"
