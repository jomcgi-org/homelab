"""Tests for the URL ingest queue."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.ingest_queue import (
    IngestQueueItem,
    fetch_youtube_transcript,
    fetch_webpage,
    ingest_handler,
    ingest_raw,
    ingest_raw_with_status,
)
from knowledge.models import RawInput
from knowledge.raw_paths import compute_raw_id


def test_ingest_queue_item_defaults():
    item = IngestQueueItem(url="https://youtube.com/watch?v=abc", source_type="youtube")
    assert item.status == "pending"
    assert item.error is None
    assert item.started_at is None
    assert item.processed_at is None


def test_ingest_queue_item_source_type_validation():
    """source_type must be youtube or webpage."""
    IngestQueueItem(url="https://example.com", source_type="youtube")
    IngestQueueItem(url="https://example.com", source_type="webpage")


@pytest.mark.asyncio
async def test_fetch_youtube_transcript_extracts_text():
    mock_api = MagicMock()
    mock_api.fetch.return_value.to_raw_data.return_value = [
        {"text": "Hello world", "start": 0.0, "duration": 1.0},
        {"text": "This is a test", "start": 1.0, "duration": 1.0},
    ]
    with patch("knowledge.ingest_queue.YouTubeTranscriptApi", return_value=mock_api):
        title, body = await fetch_youtube_transcript(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )
    assert "Hello world" in body
    assert "This is a test" in body


@pytest.mark.asyncio
async def test_fetch_youtube_transcript_extracts_video_id():
    """Should extract video ID from various YouTube URL formats."""
    mock_api = MagicMock()
    mock_api.fetch.return_value.to_raw_data.return_value = [
        {"text": "test", "start": 0.0, "duration": 1.0},
    ]
    with patch("knowledge.ingest_queue.YouTubeTranscriptApi", return_value=mock_api):
        await fetch_youtube_transcript("https://youtu.be/dQw4w9WgXcQ")
        mock_api.fetch.assert_called_with("dQw4w9WgXcQ")


@pytest.mark.asyncio
async def test_fetch_webpage_returns_markdown():
    with patch("knowledge.ingest_queue.trafilatura") as mock_traf:
        mock_traf.fetch_url.return_value = (
            "<html><body><h1>Title</h1><p>Content</p></body></html>"
        )
        mock_traf.extract.return_value = "# Title\n\nContent"
        mock_traf.extract_metadata.return_value = MagicMock(title="Title")
        title, body = await fetch_webpage("https://example.com/article")
    assert title == "Title"
    assert "Content" in body
    mock_traf.extract.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_webpage_handles_no_content():
    with patch("knowledge.ingest_queue.trafilatura") as mock_traf:
        mock_traf.fetch_url.return_value = "<html></html>"
        mock_traf.extract.return_value = None
        with pytest.raises(RuntimeError, match="no content extracted"):
            await fetch_webpage("https://example.com/empty")


@pytest.fixture()
def db_session():
    """Real SQLite session with schema= overrides stripped for create_all().

    Mirrors notes_crud_test.real_session: the knowledge tables declare a
    Postgres schema, which SQLite can't honour, so strip and restore them.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as s:
            yield s
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


class TestIngestRaw:
    """ingest_raw: fileless raw_inputs + S3 insert (ADR 006 Phase 4c-4)."""

    def test_inserts_raw_input_and_uploads(self, db_session):
        content = "---\ntitle: Hello\n---\n\nBody\n"
        with patch("knowledge.ingest_queue.upload_raw") as mock_upload:
            raw = ingest_raw(
                db_session,
                content=content,
                source="capture",
                original_url="https://example.com",
            )

        expected_id = compute_raw_id(content)
        assert raw.raw_id == expected_id
        assert raw.content_hash == expected_id
        assert raw.path == f"raws/{expected_id}.md"
        assert raw.source == "capture"
        assert raw.original_path == "https://example.com"
        mock_upload.assert_called_once_with(expected_id, content)

        rows = db_session.exec(select(RawInput)).all()
        assert len(rows) == 1
        assert rows[0].raw_id == expected_id

    def test_dedup_returns_existing_no_duplicate(self, db_session):
        content = "same content"
        with patch("knowledge.ingest_queue.upload_raw") as mock_upload:
            first = ingest_raw(db_session, content=content, source="capture")
            second = ingest_raw(db_session, content=content, source="capture")

        assert first.id == second.id
        # Upload only happens on the first (novel) insert.
        assert mock_upload.call_count == 1
        rows = db_session.exec(select(RawInput)).all()
        assert len(rows) == 1

    def test_concurrent_duplicate_insert_returns_existing(self):
        existing = RawInput(
            id=7,
            raw_id=compute_raw_id("same content"),
            path="raws/existing.md",
            source="capture",
            content_hash=compute_raw_id("same content"),
        )
        missing_result = MagicMock()
        missing_result.first.return_value = None
        existing_result = MagicMock()
        existing_result.first.return_value = existing
        session = MagicMock()
        session.exec.side_effect = [missing_result, existing_result]
        savepoint = session.begin_nested.return_value
        session.flush.side_effect = IntegrityError(
            "INSERT INTO knowledge.raw_inputs", {}, Exception("duplicate")
        )

        with patch("knowledge.ingest_queue.upload_raw"):
            raw, created = ingest_raw_with_status(
                session, content="same content", source="capture"
            )

        assert raw is existing
        assert created is False
        savepoint.rollback.assert_called_once_with()
        session.commit.assert_not_called()

    def test_failing_extraction_enqueue_still_commits_raw(self, db_session, caplog):
        content = "agent report that must survive"
        with (
            patch("knowledge.ingest_queue.upload_raw"),
            patch(
                "knowledge.extraction.enqueue_extraction",
                side_effect=RuntimeError("queue unavailable"),
            ),
        ):
            raw, created = ingest_raw_with_status(
                db_session, content=content, source="agent-report"
            )

        assert created is True
        db_session.expire_all()
        stored = db_session.exec(
            select(RawInput).where(RawInput.raw_id == raw.raw_id)
        ).one()
        assert stored.raw_id == compute_raw_id(content)
        assert "failed to enqueue raw" in caplog.text

    def test_lane_owned_source_redacts_nested_extra_strings(self, db_session):
        secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        with patch("knowledge.ingest_queue.upload_raw") as mock_upload:
            raw, created = ingest_raw_with_status(
                db_session,
                content=f"body {secret}",
                source="distress",
                extra={
                    "plain": secret,
                    "nested": {"items": [secret, 7, {"value": secret}]},
                },
            )

        assert created is True
        uploaded = mock_upload.call_args.args[1]
        assert secret not in uploaded
        assert secret not in str(raw.extra)
        assert raw.extra["nested"]["items"][1] == 7
        assert raw.extra["server_redactions"] == {"github_token": 4}


@pytest.mark.asyncio
async def test_ingest_handler_calls_ingest_raw_with_built_content():
    """ingest_handler fetches, assembles frontmatter+body, and calls ingest_raw."""
    item = IngestQueueItem(url="https://example.com/article", source_type="webpage")
    item.id = 7
    session = MagicMock()

    with (
        patch("knowledge.ingest_queue._claim_one", return_value=item),
        patch(
            "knowledge.ingest_queue.fetch_webpage",
            new=AsyncMock(return_value=("Page Title", "page body")),
        ),
        patch("knowledge.ingest_queue.ingest_raw") as mock_ingest,
        patch(
            "knowledge.ingest_queue._mark_queue_done", return_value=None
        ) as mock_done,
    ):
        await ingest_handler(session)

    mock_ingest.assert_called_once()
    kwargs = mock_ingest.call_args.kwargs
    assert kwargs["source"] == "webpage"
    assert kwargs["original_url"] == "https://example.com/article"
    content = kwargs["content"]
    assert 'title: "Page Title"' in content
    assert "source: webpage" in content
    assert "page body" in content
    mock_done.assert_called_once()
