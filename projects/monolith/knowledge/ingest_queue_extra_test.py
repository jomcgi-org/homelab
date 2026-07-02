"""Extra unit tests for ingest_queue: _extract_video_id, ingest_handler.

Covers edge cases and paths not exercised by ingest_queue_test.py:

_extract_video_id:
  - youtu.be short URL
  - embed URL
  - query param fallback (v= in arbitrary position)
  - invalid URL raises ValueError

ingest_handler:
  - empty queue returns None without touching session
  - successful youtube ingest calls fetcher + ingest_raw + commit
  - successful webpage ingest calls fetcher + ingest_raw + commit
  - failed fetch triggers rollback + failed-status update + commit
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knowledge.ingest_queue import (
    IngestQueueItem,
    _extract_video_id,
    ingest_handler,
)


# ---------------------------------------------------------------------------
# _extract_video_id
# ---------------------------------------------------------------------------


class TestExtractVideoId:
    def test_standard_watch_url(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert _extract_video_id(url) == "dQw4w9WgXcQ"

    def test_youtu_be_short_url(self):
        """youtu.be/<id> is matched by the regex pattern."""
        assert _extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_embed_url(self):
        """youtube.com/embed/<id> is matched by the regex pattern."""
        assert (
            _extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ")
            == "dQw4w9WgXcQ"
        )

    def test_watch_url_with_extra_query_params_regex(self):
        """v= in a URL where other params precede it — regex still matches."""
        url = "https://www.youtube.com/watch?v=abcdefghijk&t=5s"
        assert _extract_video_id(url) == "abcdefghijk"

    def test_query_param_fallback_when_v_not_in_path(self):
        """When the regex does not match, falls back to parse_qs v= lookup."""
        # A URL where v= is present but not preceded by the path patterns the
        # regex expects — this exercises the parse_qs fallback branch.
        url = "https://www.youtube.com/results?search_query=test&v=xxxxxxxxxxx"
        assert _extract_video_id(url) == "xxxxxxxxxxx"

    def test_invalid_url_raises_value_error(self):
        """A URL with no video ID raises ValueError."""
        with pytest.raises(ValueError, match="cannot extract video ID"):
            _extract_video_id("https://example.com/not-a-youtube-url")

    def test_bare_youtube_homepage_raises_value_error(self):
        """youtube.com root with no v= param raises ValueError."""
        with pytest.raises(ValueError, match="cannot extract video ID"):
            _extract_video_id("https://www.youtube.com/")

    def test_empty_string_raises_value_error(self):
        """Empty string raises ValueError."""
        with pytest.raises(ValueError, match="cannot extract video ID"):
            _extract_video_id("")


# ---------------------------------------------------------------------------
# ingest_handler
# ---------------------------------------------------------------------------


def _make_item(source_type="youtube", url="https://youtube.com/watch?v=abc", item_id=1):
    return IngestQueueItem(
        id=item_id,
        url=url,
        source_type=source_type,
        status="processing",
    )


class TestIngestHandler:
    """Tests for ingest_handler.

    The handler delegates DB writes to module-level helpers
    (_mark_queue_done / _mark_queue_failed) that open their own
    sessions in worker threads (see PR #2340 -- the canonical
    fresh-session-per-to_thread pattern). The mock session passed in
    here only serves to source ``engine = session.get_bind()``; the
    actual UPDATEs happen inside the patched helpers, so we assert
    against helper call_args rather than session.execute call_args.
    """

    @pytest.fixture(autouse=True)
    def _mock_queue_writers(self):
        with (
            patch("knowledge.ingest_queue._mark_queue_done") as mock_done,
            patch("knowledge.ingest_queue._mark_queue_failed") as mock_failed,
        ):
            self.mock_done = mock_done
            self.mock_failed = mock_failed
            yield

    @pytest.mark.asyncio
    async def test_empty_queue_returns_none(self):
        """When _claim_one returns None (empty queue), ingest_handler returns None."""
        session = MagicMock()
        with patch("knowledge.ingest_queue._claim_one", return_value=None):
            result = await ingest_handler(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_queue_does_not_call_queue_writers(self):
        """Empty queue leaves the queue writers untouched."""
        session = MagicMock()
        with patch("knowledge.ingest_queue._claim_one", return_value=None):
            await ingest_handler(session)
        self.mock_done.assert_not_called()
        self.mock_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_youtube_ingest_calls_fetcher(self):
        """youtube source_type dispatches to fetch_youtube_transcript."""
        item = _make_item(source_type="youtube", url="https://youtube.com/watch?v=abc")
        session = MagicMock()
        mock_fetch = AsyncMock(return_value=("YouTube: abc", "transcript here"))
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch("knowledge.ingest_queue.fetch_youtube_transcript", mock_fetch),
            patch("knowledge.ingest_queue.ingest_raw"),
        ):
            await ingest_handler(session)
        mock_fetch.assert_awaited_once_with(item.url)

    @pytest.mark.asyncio
    async def test_successful_youtube_ingest_marks_done(self):
        """Successful youtube ingest calls _mark_queue_done exactly once."""
        item = _make_item(source_type="youtube")
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_youtube_transcript",
                AsyncMock(return_value=("Title", "body")),
            ),
            patch("knowledge.ingest_queue.ingest_raw"),
        ):
            result = await ingest_handler(session)
        assert result is None
        self.mock_done.assert_called_once()
        self.mock_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_youtube_ingest_passes_item_id_to_mark_done(self):
        """Successful youtube ingest passes the item's id to _mark_queue_done."""
        item = _make_item(source_type="youtube", item_id=42)
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_youtube_transcript",
                AsyncMock(return_value=("Title", "body")),
            ),
            patch("knowledge.ingest_queue.ingest_raw"),
        ):
            await ingest_handler(session)
        # Positional args: (engine, item_id). Engine is the MagicMock-derived
        # session.get_bind() return value; we just assert item_id.
        args, _ = self.mock_done.call_args
        assert args[1] == 42

    @pytest.mark.asyncio
    async def test_successful_webpage_ingest_calls_fetcher(self):
        """webpage source_type dispatches to fetch_webpage."""
        item = _make_item(source_type="webpage", url="https://example.com/post")
        session = MagicMock()
        mock_fetch = AsyncMock(return_value=("Example Post", "article body"))
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch("knowledge.ingest_queue.fetch_webpage", mock_fetch),
            patch("knowledge.ingest_queue.ingest_raw"),
        ):
            await ingest_handler(session)
        mock_fetch.assert_awaited_once_with(item.url)

    @pytest.mark.asyncio
    async def test_successful_webpage_ingest_marks_done(self):
        """Successful webpage ingest calls _mark_queue_done exactly once."""
        item = _make_item(source_type="webpage", url="https://example.com")
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_webpage",
                AsyncMock(return_value=("Title", "body")),
            ),
            patch("knowledge.ingest_queue.ingest_raw"),
        ):
            result = await ingest_handler(session)
        assert result is None
        self.mock_done.assert_called_once()
        self.mock_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_fetch_marks_failed(self):
        """When the fetcher raises, _mark_queue_failed is called exactly once."""
        item = _make_item(source_type="youtube")
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_youtube_transcript",
                AsyncMock(side_effect=RuntimeError("transcript unavailable")),
            ),
        ):
            result = await ingest_handler(session)
        assert result is None
        self.mock_failed.assert_called_once()
        self.mock_done.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_fetch_passes_item_id_to_mark_failed(self):
        """Failed fetch passes the item's id to _mark_queue_failed."""
        item = _make_item(source_type="youtube", item_id=99)
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_youtube_transcript",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            await ingest_handler(session)
        # Positional args: (engine, item_id, error). We assert item_id.
        args, _ = self.mock_failed.call_args
        assert args[1] == 99

    @pytest.mark.asyncio
    async def test_failed_fetch_passes_error_message_to_mark_failed(self):
        """The (truncated) error message is forwarded to _mark_queue_failed."""
        item = _make_item(source_type="youtube")
        session = MagicMock()
        error_text = "transcript unavailable for this video"
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_youtube_transcript",
                AsyncMock(side_effect=RuntimeError(error_text)),
            ),
        ):
            await ingest_handler(session)
        # Positional args: (engine, item_id, error).
        args, _ = self.mock_failed.call_args
        assert error_text in args[2]

    @pytest.mark.asyncio
    async def test_ingest_handler_returns_none_on_success(self):
        """ingest_handler always returns None (not a datetime)."""
        item = _make_item(source_type="webpage", url="https://example.com")
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_webpage",
                AsyncMock(return_value=("Title", "body")),
            ),
            patch("knowledge.ingest_queue.ingest_raw"),
        ):
            result = await ingest_handler(session)
        assert result is None
