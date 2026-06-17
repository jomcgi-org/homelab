"""Shared gardener constants and helpers.

The gardener decomposition itself no longer runs in-pod: it is a remote
claude.ai routine over MCP (ADR 006 Phase 4c). This module survives only
to host the constants and the slug helper that other knowledge modules
still import: ``GARDENER_VERSION`` (provenance version stamp),
``Gardener._MAX_RETRIES`` (dead-letter retry ceiling), and ``_slugify``.
"""

from __future__ import annotations

import re
import unicodedata

# Version stamp recorded on every provenance row the gardener produces.
# Bump this when the prompt or model changes to trigger a manual reprocess.
GARDENER_VERSION = "claude-sonnet-4-6@v1"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text_in: str) -> str:
    normalized = unicodedata.normalize("NFKD", text_in)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_only.lower()).strip("-")
    return slug or "note"


class Gardener:
    """Retired: gardener now runs as remote claude.ai routines over MCP
    (ADR 006 Phase 4c). Only the retry ceiling constant survives, used by
    store.raws_needing_decomposition and router's dead-letter endpoints."""

    _MAX_RETRIES = 3
