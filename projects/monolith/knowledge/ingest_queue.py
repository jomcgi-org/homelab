"""URL ingest queue: model, fetchers, and scheduler handler."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import trafilatura
from sqlalchemy import Column, String, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, select
from youtube_transcript_api import YouTubeTranscriptApi

from knowledge.models import RawInput
from knowledge.raw_paths import compute_raw_id
from knowledge.raw_store import upload_raw

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


def ingest_raw(
    session: Session,
    *,
    content: str,
    source: str,
    original_url: str | None = None,
    extra: dict | None = None,
) -> RawInput:
    """Persist raw content directly to knowledge.raw_inputs + S3 (no files).

    Content-addressed and idempotent: the ``raw_id`` is the sha256 of the
    content, so re-ingesting identical content returns the existing row without
    a duplicate insert or re-upload. The markdown body is uploaded to
    ``s3://knowledge/raws/<raw_id>.md``; the row's ``path`` is a synthetic
    stable value (no real file) since the model requires it non-null + unique.
    """
    raw, _ = ingest_raw_with_status(
        session,
        content=content,
        source=source,
        original_url=original_url,
        extra=extra,
    )
    return raw


def ingest_raw_with_status(
    session: Session,
    *,
    content: str,
    source: str,
    original_url: str | None = None,
    extra: dict | None = None,
) -> tuple[RawInput, bool]:
    """Persist raw content and report whether a new row was created."""
    raw_id = compute_raw_id(content)
    existing = session.exec(select(RawInput).where(RawInput.raw_id == raw_id)).first()
    if existing is not None:
        return existing, False

    upload_raw(raw_id, content)
    raw = RawInput(
        raw_id=raw_id,
        path=f"raws/{raw_id}.md",
        source=source,
        content_hash=raw_id,
        original_path=original_url,
        extra=extra or {},
    )
    savepoint = session.begin_nested()
    try:
        session.add(raw)
        session.flush()
    except IntegrityError:
        savepoint.rollback()
        existing = session.exec(
            select(RawInput).where(RawInput.raw_id == raw_id)
        ).first()
        if existing is None:
            raise
        return existing, False
    else:
        savepoint.commit()
    from knowledge.extraction import EXTRACTABLE_SOURCES, enqueue_extraction

    if source in EXTRACTABLE_SOURCES:
        enqueue_savepoint = session.begin_nested()
        try:
            enqueue_extraction(session, raw_id, commit=False)
        except Exception:  # noqa: BLE001 - the later sweep repairs missed jobs
            enqueue_savepoint.rollback()
            logger.exception("ingest_queue: failed to enqueue raw %s", raw_id)
        else:
            enqueue_savepoint.commit()
    session.commit()
    logger.info("ingest_queue: ingested raw %s (source=%s)", raw_id, source)
    return raw, True


async def ingest_handler(session: Session) -> datetime | None:
    """Scheduler handler: claim and process one URL from the queue."""
    item = _claim_one(session)
    if item is None:
        return None

    engine = session.get_bind()

    try:
        if item.source_type == "youtube":
            title, body = await fetch_youtube_transcript(item.url)
        elif item.source_type == "webpage":
            title, body = await fetch_webpage(item.url)
        else:
            raise ValueError(f"unknown source_type: {item.source_type}")

        content = (
            f"---\n"
            f'title: "{title}"\n'
            f"source: {item.source_type}\n"
            f"original_url: {item.url}\n"
            f"---\n\n"
            f"{body}\n"
        )
        ingest_raw(
            session,
            content=content,
            source=item.source_type,
            original_url=item.url,
        )

        await asyncio.to_thread(_mark_queue_done, engine, item.id)
        logger.info("ingest_queue: done %s", item.url)

    except Exception as exc:
        logger.exception("ingest_queue: failed %s", item.url)
        await asyncio.to_thread(_mark_queue_failed, engine, item.id, str(exc)[:500])

    return None
