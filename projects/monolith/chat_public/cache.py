"""Durable, cross-pod response cache for public chat (ADR 005 follow-up).

Repeated identical questions, especially the page's starter prompts, should come
back immediately without spending a GPU slot when nothing relevant has changed.
The cache is a Postgres table (``chat_public.response_cache``), so it is durable
across pod restarts and shared by every replica: when the public web backend
scales past one pod (its HPA ceiling is raised after the Phase 6 load test), all
pods read and write the same cache rather than each keeping its own.

The cache is a simple key/value keyed by ``cache_key``, a hash of
``(normalized_message, prompt_version, notes_watermark)``:

- ``normalized_message``: the user message with surrounding/collapsed whitespace
  removed and lowercased, so trivial whitespace/case differences still hit.
- ``prompt_version``: a stable hash of the active system prompt + model name, so
  a prompt edit or a model swap invalidates every entry.
- ``notes_watermark``: ``max(indexed_at)`` over the public notes view, so any
  change to the published notes invalidates the cache. The watermark query is
  itself memoized for a short TTL so it does not run on every turn.

Reads + writes of the cache table use the ``public_writer`` chat engine (the
``get_chat_session`` dependency), which can DML the chat_public schema. The
watermark query reads the ``public_api.knowledge_notes`` view via the
``public_reader`` engine (``read_db``); it is a schema-qualified raw SELECT so
importing this module never registers the pgvector public models in
SQLModel.metadata. Anywhere the view is absent (SQLite unit fixtures, an
unconfigured dev DB) the watermark query raises and we fail closed: the watermark
is None and caching is simply disabled for that turn, so behaviour is identical
to the pre-cache path.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from chat_public.models import ChatResponseCache

logger = logging.getLogger(__name__)

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


@dataclass
class CacheKey:
    """A resolved cache key: the hashed ``cache_key`` plus its component parts
    (stored alongside the row for debuggability)."""

    cache_key: str
    normalized_message: str
    prompt_version: str
    notes_watermark: str


# Short-TTL memo for the notes watermark (value, monotonic-deadline).
_watermark_lock = threading.Lock()
_watermark_value: str | None = None
_watermark_deadline: float = 0.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def _hash_key(
    normalized_message: str, prompt_version_: str, notes_watermark: str
) -> str:
    """Hash the three key components into the single ``cache_key`` primary key."""
    digest = hashlib.sha256()
    digest.update(normalized_message.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(prompt_version_.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(notes_watermark.encode("utf-8"))
    return digest.hexdigest()


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
    db: Session, read_db: Session, message: str, system_prompt: str, model: str
) -> tuple[CacheKey | None, CachedResponse | None]:
    """Resolve the cache key and return ``(key, cached_or_None)``.

    A None key means caching is disabled for this turn (no watermark): the caller
    should generate normally and NOT store the result. A non-None key with a None
    value is a cache miss the caller can store under after generating. On a hit
    the row's hit_count is bumped (atomically) for observability.
    """
    watermark = current_watermark(read_db)
    if watermark is None:
        return None, None
    normalized = normalize_message(message)
    pv = prompt_version(system_prompt, model)
    key = CacheKey(
        cache_key=_hash_key(normalized, pv, watermark),
        normalized_message=normalized,
        prompt_version=pv,
        notes_watermark=watermark,
    )

    try:
        row = db.get(ChatResponseCache, key.cache_key)
    except Exception:  # noqa: BLE001 - a cache read must never fail a turn
        logger.warning(
            "chat_public.cache.lookup_failed; treating as miss", exc_info=True
        )
        db.rollback()
        return key, None
    if row is None:
        return key, None

    cached = CachedResponse(text=row.response_text, touched=list(row.touched or []))
    try:
        db.execute(
            update(ChatResponseCache)
            .where(ChatResponseCache.cache_key == key.cache_key)
            .values(hit_count=ChatResponseCache.hit_count + 1)
        )
        db.commit()
    except Exception:  # noqa: BLE001 - the hit still serves even if the bump fails
        logger.warning("chat_public.cache.hit_count_bump_failed", exc_info=True)
        db.rollback()
    return key, cached


def store(db: Session, key: CacheKey, response_text: str, touched: list[dict]) -> None:
    """Store a generated turn under a previously-resolved (non-None) key.

    INSERT ... ON CONFLICT (cache_key) DO UPDATE so a concurrent miss on another
    pod (or a regenerated answer) refreshes the row atomically and bumps
    hit_count. A store failure must never fail the turn (the answer already
    streamed), so it is logged and swallowed.
    """
    table = ChatResponseCache.__table__
    insert_fn = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    stmt = insert_fn(table).values(
        cache_key=key.cache_key,
        normalized_message=key.normalized_message,
        prompt_version=key.prompt_version,
        notes_watermark=key.notes_watermark,
        response_text=response_text,
        touched=list(touched),
        created_at=_utcnow(),
        hit_count=0,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["cache_key"],
        set_={
            "response_text": stmt.excluded.response_text,
            "touched": stmt.excluded.touched,
            "notes_watermark": stmt.excluded.notes_watermark,
            "created_at": stmt.excluded.created_at,
            "hit_count": table.c.hit_count + 1,
        },
    )
    try:
        db.execute(stmt)
        db.commit()
    except Exception:  # noqa: BLE001 - a cache write must never fail a turn
        logger.warning("chat_public.cache.store_failed", exc_info=True)
        db.rollback()


def reset_watermark_memo() -> None:
    """Clear the short-TTL watermark memo (tests / ops)."""
    global _watermark_value, _watermark_deadline
    with _watermark_lock:
        _watermark_value = None
        _watermark_deadline = 0.0
