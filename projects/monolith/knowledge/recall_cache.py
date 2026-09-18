"""Durable, content-addressed recall vectors, populated outside session creation."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
import re
import threading

from sqlmodel import Session

from knowledge.models import RecallEmbedding
from shared.embedding import EmbeddingClient

RECALL_QUERY_CAP = 2000
RECALL_EMBED_TIMEOUT_SECONDS = 30.0
RECALL_BACKFILL_CAPACITY = 64
EMBEDDING_MODEL = "voyage-4-nano"
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kg-recall")
_pending: set[str] = set()
_lock = threading.Lock()
logger = logging.getLogger(__name__)


def query_text(text: str | None) -> str:
    """Strip the receipt URL and known launch envelopes before embedding."""
    value = (text or "").strip()
    if re.match(r"GitHub issue https?://\S+\n\n", value):
        value = value.partition("\n\n")[2].strip()
    # Session envelopes sometimes precede the actual user request.
    for tag in ("AGENTS.md instructions", "environment_context", "system-reminder"):
        value = re.sub(rf"<{tag}>.*?</{tag}>", "", value, flags=re.S).strip()
    if value.startswith("Factory task ") or value.startswith("You are a coding agent"):
        return ""
    return value[:RECALL_QUERY_CAP]


def cache_key(text: str) -> str:
    return hashlib.sha256(f"{EMBEDDING_MODEL}\n{text}".encode()).hexdigest()


def cached_vector(session: Session, text: str) -> list[float] | None:
    row = session.get(RecallEmbedding, cache_key(text))
    return list(row.embedding) if row is not None else None


def _backfill(text: str) -> None:
    from core.db import get_engine
    from knowledge.recall_metrics import increment
    from sqlalchemy.exc import IntegrityError

    try:
        with Session(get_engine()) as session:
            if cached_vector(session, text) is not None:
                return

        # No connection is held while the embedding service runs.
        async def embed():
            return await asyncio.wait_for(
                EmbeddingClient(model=EMBEDDING_MODEL).embed(text),
                timeout=RECALL_EMBED_TIMEOUT_SECONDS,
            )

        vector = asyncio.run(embed())
        with Session(get_engine()) as session:
            session.add(RecallEmbedding(key=cache_key(text), embedding=vector))
            try:
                session.commit()
            except IntegrityError:
                # Another replica completed the same content-addressed job.
                session.rollback()
    except TimeoutError:
        increment("timeouts")
        logger.warning("knowledge recall embedding backfill timed out")
    except Exception as exc:  # noqa: BLE001 - advisory background work
        logger.warning("knowledge recall backfill failed: %s", type(exc).__name__)
    finally:
        with _lock:
            _pending.discard(cache_key(text))


def prepare_recall(text: str | None) -> None:
    """Schedule bounded, single-flight backfill, never wait for an embedding."""
    from knowledge.recall import RECALL_MIN_PROMPT_CHARS, recall_enabled

    text = query_text(text)
    if not recall_enabled() or len(text) < RECALL_MIN_PROMPT_CHARS:
        return
    key = cache_key(text)
    with _lock:
        if key in _pending or len(_pending) >= RECALL_BACKFILL_CAPACITY:
            return
        _pending.add(key)
    try:
        _executor.submit(_backfill, text)
    except RuntimeError:
        with _lock:
            _pending.discard(key)
