"""URL ingest queue: model, fetchers, and scheduler handler."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import trafilatura
from sqlalchemy import Column, String, text
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel
from youtube_transcript_api import YouTubeTranscriptApi

from knowledge.raw_paths import compute_raw_id, raw_target_path

logger = logging.getLogger("monolith.knowledge.ingest_queue")

_STALE_INTERVAL = "5 minutes"

_YT_PATTERNS = re.compile(
    r"(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/embed/)"
    r"([a-zA-Z0-9_-]{11})"
)


class IngestQueueItem(  # nosemgrep: sqlmodel-datetime-without-factory
    SQLModel, table=True
):
    __tablename__ = "ingest_queue"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    url: str
    source_type: str = Field(sa_column=Column(String, nullable=False))
    status: str = Field(default="pending", sa_column=Column(String, nullable=False))
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    processed_at: datetime | None = None


def _extract_video_id(url: str) -> str:
    m = _YT_PATTERNS.search(url)
    if m:
        return m.group(1)
    parsed = urlparse(url)
    v = parse_qs(parsed.query).get("v")
    if v:
        return v[0]
    raise ValueError(f"cannot extract video ID from {url}")


async def fetch_youtube_transcript(url: str) -> tuple[str, str]:
    """Fetch a YouTube transcript. Returns (title, markdown_body)."""
    video_id = _extract_video_id(url)
    api = YouTubeTranscriptApi()
    transcript = api.fetch(video_id)
    segments = transcript.to_raw_data()
    body = "\n\n".join(seg["text"] for seg in segments)
    title = f"YouTube: {video_id}"
    return title, body


async def fetch_webpage(url: str) -> tuple[str, str]:
    """Fetch a webpage and extract as markdown. Returns (title, markdown_body)."""
    html = trafilatura.fetch_url(url)
    if not html:
        raise RuntimeError(f"failed to fetch {url}")
    body = trafilatura.extract(html, output_format="markdown", include_links=True)
    if not body:
        raise RuntimeError(f"no content extracted from {url}")
    meta = trafilatura.extract_metadata(html)
    title = meta.title if meta and meta.title else urlparse(url).netloc
    return title, body


def _mark_queue_done(engine: Engine, item_id: int) -> None:
    """Mark a queue item processed; opens its own session for ``to_thread``.

    Fresh session per call (canonical pattern from PR #2297) — Sessions
    are not thread-safe so the caller's loop-thread session must not be
    passed in.
    """
    with Session(engine) as session:
        session.execute(
            text("""
                UPDATE knowledge.ingest_queue
                SET status = 'done', processed_at = NOW()
                WHERE id = :id
            """),
            {"id": item_id},
        )
        session.commit()


def _mark_queue_failed(engine: Engine, item_id: int, error: str) -> None:
    """Mark a queue item failed; opens its own session for ``to_thread``."""
    with Session(engine) as session:
        session.execute(
            text("""
                UPDATE knowledge.ingest_queue
                SET status = 'failed', error = :error, processed_at = NOW()
                WHERE id = :id
            """),
            {"id": item_id, "error": error},
        )
        session.commit()


def _claim_one(session: Session) -> IngestQueueItem | None:
    """Claim one pending (or stale) queue item. Returns None if empty."""
    result = session.execute(
        text(f"""
            UPDATE knowledge.ingest_queue
            SET status = 'processing', started_at = NOW()
            WHERE id = (
                SELECT id FROM knowledge.ingest_queue
                WHERE status = 'pending'
                   OR (status = 'processing'
                       AND started_at < NOW() - INTERVAL '{_STALE_INTERVAL}')
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, url, source_type, status, error,
                      created_at, started_at, processed_at
        """)
    ).fetchone()
    if result is None:
        return None
    return IngestQueueItem.model_validate(dict(result._mapping))


def _write_raw_md(
    *,
    vault_root: Path,
    title: str,
    body: str,
    source_type: str,
    original_url: str,
    now: datetime,
) -> Path:
    """Write fetched content as a raw markdown file under _raw/."""
    content = (
        f"---\n"
        f'title: "{title}"\n'
        f"source: {source_type}\n"
        f"original_url: {original_url}\n"
        f"---\n\n"
        f"{body}\n"
    )
    raw_id = compute_raw_id(content)
    target = raw_target_path(
        vault_root=vault_root,
        raw_id=raw_id,
        title=title,
        created_at=now,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    logger.info("ingest_queue: wrote %s", target)
    return target


async def ingest_handler(session: Session) -> datetime | None:
    """Scheduler handler: claim and process one URL from the queue."""
    item = _claim_one(session)
    if item is None:
        return None

    engine = session.get_bind()
    vault_root = Path(os.environ.get("VAULT_ROOT", "/vault"))
    now = datetime.now(timezone.utc)

    try:
        if item.source_type == "youtube":
            title, body = await fetch_youtube_transcript(item.url)
        elif item.source_type == "webpage":
            title, body = await fetch_webpage(item.url)
        else:
            raise ValueError(f"unknown source_type: {item.source_type}")

        _write_raw_md(
            vault_root=vault_root,
            title=title,
            body=body,
            source_type=item.source_type,
            original_url=item.url,
            now=now,
        )

        await asyncio.to_thread(_mark_queue_done, engine, item.id)
        logger.info("ingest_queue: done %s", item.url)

    except Exception as exc:
        logger.exception("ingest_queue: failed %s", item.url)
        await asyncio.to_thread(_mark_queue_failed, engine, item.id, str(exc)[:500])

    return None
