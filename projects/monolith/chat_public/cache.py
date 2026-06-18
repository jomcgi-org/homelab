"""In-process response cache for public chat (ADR 005 follow-up).

Repeated identical questions, especially the page's starter prompts, should come
back immediately without spending a GPU slot when nothing relevant has changed.
This module is a small bounded LRU keyed by
``(normalized_message, prompt_version, notes_watermark)``:

- ``normalized_message``: the user message with surrounding/collapsed whitespace
  removed and lowercased, so trivial whitespace/case differences still hit.
- ``prompt_version``: a stable hash of the active system prompt + model name, so
  a prompt edit or a model swap invalidates every entry.
- ``notes_watermark``: ``max(indexed_at)`` over the public notes view, so any
  change to the published notes invalidates the cache. The watermark query is
  itself cached for a short TTL so it does not run on every turn.

IMPORTANT: this cache is GLOBAL only because the public web backend runs at
``maxReplicas=1`` (see the monolith-public chart). If the public backend ever
scales out, this becomes a per-pod cache; a shared cache (or precompute) is the
Phase 6 follow-up.

The watermark query is a deliberately schema-qualified raw SELECT against the
``public_api.knowledge_notes`` view (the public_reader role's only window onto
notes). It is intentionally NOT routed through the ORM model so importing this
module never registers the pgvector-backed public models in SQLModel.metadata.
Anywhere the view is absent (SQLite unit fixtures, an unconfigured dev DB) the
query raises and we fail closed: the watermark is None and caching is simply
disabled for that turn, so behaviour is identical to the pre-cache path.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlmodel import Session

logger = logging.getLogger(__name__)

# Max number of distinct (message, prompt_version, watermark) answers retained.
# The public set and the starter prompts are small, so a few hundred entries is
# ample; old entries fall off in least-recently-used order.
CACHE_MAX_ENTRIES = int(os.environ.get("CHAT_PUBLIC_CACHE_MAX_ENTRIES", "256"))

# How long a computed notes watermark is reused before it is recomputed, so the
# watermark SELECT does not run on every single turn. A change to the public
# notes therefore takes effect within this window.
WATERMARK_TTL_SECONDS = float(
    os.environ.get("CHAT_PUBLIC_CACHE_WATERMARK_TTL_SECONDS", "60")
)


@dataclass
class CachedResponse:
    """A stored assistant turn: the full reply text plus the touched-note list.

    ``touched`` mirrors the ``node_touched`` SSE payloads (``{"id", "title"}``) so
    a cache hit can repaint the same grounded nodes before replaying the text.
    """

    text: str
    touched: list[dict] = field(default_factory=list)


class _LruCache:
    """A tiny thread-safe bounded LRU over an OrderedDict."""

    def __init__(self, max_entries: int) -> None:
        self._max = max(1, max_entries)
        self._store: "OrderedDict[tuple, CachedResponse]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> CachedResponse | None:
        with self._lock:
            value = self._store.get(key)
            if value is not None:
                self._store.move_to_end(key)
            return value

    def put(self, key: tuple, value: CachedResponse) -> None:
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_cache = _LruCache(CACHE_MAX_ENTRIES)

# Short-TTL memo for the notes watermark (value, monotonic-deadline).
_watermark_lock = threading.Lock()
_watermark_value: str | None = None
_watermark_deadline: float = 0.0


def normalize_message(message: str) -> str:
    """Collapse surrounding/internal whitespace and lowercase a user message."""
    return " ".join((message or "").split()).lower()


def prompt_version(system_prompt: str, model: str) -> str:
    """Stable short hash of the active system prompt + model name.

    A change to either the server-fixed prompt or the model alias produces a new
    version string, which changes every cache key and thus invalidates the cache.
    """
    digest = hashlib.sha256()
    digest.update((system_prompt or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update((model or "").encode("utf-8"))
    return digest.hexdigest()[:16]


def _query_watermark(read_db: Session) -> str:
    """``max(indexed_at)`` over the public notes view, as a stable string key.

    Raises if the view is unreachable (e.g. SQLite test fixtures); callers treat
    any failure as "caching disabled for this turn".
    """
    row = read_db.execute(
        text("SELECT max(indexed_at) FROM public_api.knowledge_notes")
    ).scalar()
    return row.isoformat() if row is not None else "empty"


def current_watermark(read_db: Session) -> str | None:
    """The notes watermark, recomputed at most once per ``WATERMARK_TTL_SECONDS``.

    Returns None if the watermark cannot be computed (view absent / DB error), in
    which case the caller disables caching for the turn and generates normally.
    """
    global _watermark_value, _watermark_deadline
    now = time.monotonic()
    with _watermark_lock:
        if now < _watermark_deadline:
            return _watermark_value
    try:
        value = _query_watermark(read_db)
    except Exception:  # noqa: BLE001 - any failure just disables caching
        logger.debug("chat_public.cache.watermark_unavailable; caching disabled")
        return None
    with _watermark_lock:
        _watermark_value = value
        _watermark_deadline = now + WATERMARK_TTL_SECONDS
    return value


def lookup(
    read_db: Session, message: str, system_prompt: str, model: str
) -> tuple[tuple | None, CachedResponse | None]:
    """Resolve the cache key and return ``(key, cached_or_None)``.

    A None key means caching is disabled for this turn (no watermark): the caller
    should generate normally and NOT store the result. A non-None key with a None
    value is a cache miss the caller can store under after generating.
    """
    watermark = current_watermark(read_db)
    if watermark is None:
        return None, None
    key = (normalize_message(message), prompt_version(system_prompt, model), watermark)
    return key, _cache.get(key)


def store(key: tuple, text_value: str, touched: list[dict]) -> None:
    """Store a generated turn under a previously-resolved (non-None) key."""
    _cache.put(key, CachedResponse(text=text_value, touched=list(touched)))


def reset() -> None:
    """Clear the response cache and the watermark memo (tests / ops)."""
    global _watermark_value, _watermark_deadline
    _cache.clear()
    with _watermark_lock:
        _watermark_value = None
        _watermark_deadline = 0.0
