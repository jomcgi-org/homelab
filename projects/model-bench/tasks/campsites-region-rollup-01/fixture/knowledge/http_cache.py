"""Shared HTTP caching helpers for knowledge graph endpoints.

These helpers back the Cache-Control / ETag / Last-Modified behaviour for both
the private graph route (``knowledge.router``) and the public graph route
(``knowledge.public_router``). They live in this neutral module so the public
router never has to import the private-route module to reuse them.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Mirrors NOTES_PAGE_CACHE_CONTROL in projects/monolith/frontend/src/lib/cache-headers.js — keep in sync.
_GRAPH_CACHE_CONTROL = (
    "public, s-maxage=3600, stale-while-revalidate=86400, stale-if-error=31536000"
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to tz-aware UTC.

    Postgres returns tz-aware values; SQLite (used in tests) can return
    naive ones even though we always write tz-aware UTC. Treat naive
    datetimes as UTC so downstream formatters and ETag stamps are stable
    across both backends.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _graph_etag(node_count: int, indexed_at: datetime | None) -> str:
    """Stable ETag for a graph payload.

    Combines max(indexed_at) with node count so deletions invalidate even
    when the surviving notes' timestamps don't move.
    """
    stamp = indexed_at.isoformat() if indexed_at is not None else "null"
    return f'"{stamp}-{node_count}"'
