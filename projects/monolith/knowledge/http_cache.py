"""Shared HTTP caching helpers for knowledge graph endpoints.

These helpers back the Cache-Control / ETag / Last-Modified behaviour for both
the private graph route (``knowledge.router``) and the public graph route
(``knowledge.public_router``). They live in this neutral module so the public
router never has to import the private-route module to reuse them.
"""

from __future__ import annotations

import math
from datetime import datetime

from core.clock import as_utc

# Mirrors NOTES_PAGE_CACHE_CONTROL in projects/monolith/frontend/src/lib/cache-headers.js — keep in sync.
_GRAPH_CACHE_CONTROL = (
    "public, s-maxage=3600, stale-while-revalidate=86400, stale-if-error=31536000"
)


def _graph_etag(node_count: int, indexed_at: datetime | None) -> str:
    """Stable ETag for a graph payload.

    Combines max(indexed_at) with node count so deletions invalidate even
    when the surviving notes' timestamps don't move.
    """
    stamp = indexed_at.isoformat() if indexed_at is not None else "null"
    return f'"{stamp}-{node_count}"'


def public_cache_control(notes, *, now: datetime, default: str) -> str:
    """Bound every cache to the served notes' earliest review deadline.

    Public current-only responses never permit stale serving. Empty sets and
    subsecond leases are not stored; browsers get an explicit freshness bound.
    The private graph policy is unchanged.
    """
    deadlines = [as_utc(note.review_after) for note in notes]
    if not deadlines or any(deadline is None for deadline in deadlines):
        return "no-store"
    remaining = math.floor((min(deadlines) - as_utc(now)).total_seconds())
    if remaining <= 0:
        return "no-store"
    directives = dict(
        part.strip().split("=", 1) for part in default.split(",") if "=" in part
    )
    shared = min(remaining, int(directives.get("s-maxage", "0")))
    browser = min(remaining, int(directives.get("max-age", shared)))
    return f"public, max-age={browser}, s-maxage={shared}, must-revalidate"
