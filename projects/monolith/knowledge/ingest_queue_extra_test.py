"""Extra unit tests for ingest_queue: _extract_video_id, _write_raw_md, ingest_handler.

Covers edge cases and paths not exercised by ingest_queue_test.py:

_extract_video_id:
  - youtu.be short URL
  - embed URL
  - query param fallback (v= in arbitrary position)
  - invalid URL raises ValueError

_write_raw_md:
  - writes correct frontmatter (title, source, original_url) and body
  - file is created under _raw/ and parent dirs are created

ingest_handler:
  - empty queue returns None without touching session
  - successful youtube ingest calls fetcher + write + commit
  - successful webpage ingest calls fetcher + write + commit
  - failed fetch triggers rollback + failed-status update + commit
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knowledge.ingest_queue import (
    IngestQueueItem,
    _extract_video_id,
    _write_raw_md,
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
# _write_raw_md
# ---------------------------------------------------------------------------


class TestWriteRawMd:
    _NOW = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)

    def _call(self, vault_root, **kw):
        kwargs = dict(
            vault_root=vault_root,
            title="Test Title",
            body="Body content here.",
            source_type="youtube",
            original_url="https://youtube.com/watch?v=abc123",
            now=self._NOW,
        )
        kwargs.update(kw)
        return _write_raw_md(**kwargs)

    def test_returns_path_object(self, tmp_path):
        result = self._call(tmp_path)
        assert isinstance(result, Path)

    def test_file_exists_after_write(self, tmp_path):
        result = self._call(tmp_path)
        assert result.exists()

    def test_file_is_under_raw_directory(self, tmp_path):
        result = self._call(tmp_path)
        assert str(result).startswith(str(tmp_path / "_raw"))

    def test_creates_parent_directories(self, tmp_path):
        """Parent dirs under _raw/ are created even if they don't exist."""
        result = self._call(tmp_path)
        assert result.parent.is_dir()
        assert (tmp_path / "_raw").is_dir()

    def test_frontmatter_contains_title(self, tmp_path):
        result = self._call(tmp_path, title="My Video Title")
        content = result.read_text(encoding="utf-8")
        assert 'title: "My Video Title"' in content

    def test_frontmatter_contains_source_type(self, tmp_path):
        result = self._call(tmp_path, source_type="webpage")
        content = result.read_text(encoding="utf-8")
        assert "source: webpage" in content

    def test_frontmatter_contains_original_url(self, tmp_path):
        url = "https://example.com/some/article"
        result = self._call(tmp_path, original_url=url)
        content = result.read_text(encoding="utf-8")
        assert f"original_url: {url}" in content

    def test_body_written_after_frontmatter(self, tmp_path):
        result = self._call(tmp_path, body="This is the transcript body.")
        content = result.read_text(encoding="utf-8")
        assert "This is the transcript body." in content

    def test_frontmatter_delimited_by_triple_dash(self, tmp_path):
        result = self._call(tmp_path)
        content = result.read_text(encoding="utf-8")
        # Standard YAML frontmatter block
        assert content.startswith("---\n")
        # Second --- closes the block
        assert "\n---\n" in content

    def test_same_content_same_path(self, tmp_path):
        """Deterministic: same inputs produce the same output path."""
        path1 = self._call(tmp_path, title="Stable", body="Content")
        path2 = self._call(
            tmp_path,
            title="Stable",
            body="Content",
            source_type="youtube",
            original_url="https://youtube.com/watch?v=abc123",
        )
        assert path1 == path2


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
    async def test_successful_youtube_ingest_calls_fetcher(self, tmp_path):
        """youtube source_type dispatches to fetch_youtube_transcript."""
        item = _make_item(source_type="youtube", url="https://youtube.com/watch?v=abc")
        session = MagicMock()
        mock_fetch = AsyncMock(return_value=("YouTube: abc", "transcript here"))
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch("knowledge.ingest_queue.fetch_youtube_transcript", mock_fetch),
            patch(
                "knowledge.ingest_queue._write_raw_md", return_value=tmp_path / "out.md"
            ),
            patch.dict("os.environ", {"VAULT_ROOT": str(tmp_path)}),
        ):
            await ingest_handler(session)
        mock_fetch.assert_awaited_once_with(item.url)

    @pytest.mark.asyncio
    async def test_successful_youtube_ingest_marks_done(self, tmp_path):
        """Successful youtube ingest calls _mark_queue_done exactly once."""
        item = _make_item(source_type="youtube")
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_youtube_transcript",
                AsyncMock(return_value=("Title", "body")),
            ),
            patch(
                "knowledge.ingest_queue._write_raw_md", return_value=tmp_path / "out.md"
            ),
            patch.dict("os.environ", {"VAULT_ROOT": str(tmp_path)}),
        ):
            result = await ingest_handler(session)
        assert result is None
        self.mock_done.assert_called_once()
        self.mock_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_youtube_ingest_passes_item_id_to_mark_done(
        self, tmp_path
    ):
        """Successful youtube ingest passes the item's id to _mark_queue_done."""
        item = _make_item(source_type="youtube", item_id=42)
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_youtube_transcript",
                AsyncMock(return_value=("Title", "body")),
            ),
            patch(
                "knowledge.ingest_queue._write_raw_md", return_value=tmp_path / "out.md"
            ),
            patch.dict("os.environ", {"VAULT_ROOT": str(tmp_path)}),
        ):
            await ingest_handler(session)
        # Positional args: (engine, item_id). Engine is the MagicMock-derived
        # session.get_bind() return value; we just assert item_id.
        args, _ = self.mock_done.call_args
        assert args[1] == 42

    @pytest.mark.asyncio
    async def test_successful_webpage_ingest_calls_fetcher(self, tmp_path):
        """webpage source_type dispatches to fetch_webpage."""
        item = _make_item(source_type="webpage", url="https://example.com/post")
        session = MagicMock()
        mock_fetch = AsyncMock(return_value=("Example Post", "article body"))
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch("knowledge.ingest_queue.fetch_webpage", mock_fetch),
            patch(
                "knowledge.ingest_queue._write_raw_md", return_value=tmp_path / "out.md"
            ),
            patch.dict("os.environ", {"VAULT_ROOT": str(tmp_path)}),
        ):
            await ingest_handler(session)
        mock_fetch.assert_awaited_once_with(item.url)

    @pytest.mark.asyncio
    async def test_successful_webpage_ingest_marks_done(self, tmp_path):
        """Successful webpage ingest calls _mark_queue_done exactly once."""
        item = _make_item(source_type="webpage", url="https://example.com")
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_webpage",
                AsyncMock(return_value=("Title", "body")),
            ),
            patch(
                "knowledge.ingest_queue._write_raw_md", return_value=tmp_path / "out.md"
            ),
            patch.dict("os.environ", {"VAULT_ROOT": str(tmp_path)}),
        ):
            result = await ingest_handler(session)
        assert result is None
        self.mock_done.assert_called_once()
        self.mock_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_fetch_marks_failed(self, tmp_path):
        """When the fetcher raises, _mark_queue_failed is called exactly once."""
        item = _make_item(source_type="youtube")
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_youtube_transcript",
                AsyncMock(side_effect=RuntimeError("transcript unavailable")),
            ),
            patch.dict("os.environ", {"VAULT_ROOT": str(tmp_path)}),
        ):
            result = await ingest_handler(session)
        assert result is None
        self.mock_failed.assert_called_once()
        self.mock_done.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_fetch_passes_item_id_to_mark_failed(self, tmp_path):
        """Failed fetch passes the item's id to _mark_queue_failed."""
        item = _make_item(source_type="youtube", item_id=99)
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_youtube_transcript",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.dict("os.environ", {"VAULT_ROOT": str(tmp_path)}),
        ):
            await ingest_handler(session)
        # Positional args: (engine, item_id, error). We assert item_id.
        args, _ = self.mock_failed.call_args
        assert args[1] == 99

    @pytest.mark.asyncio
    async def test_failed_fetch_passes_error_message_to_mark_failed(self, tmp_path):
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
            patch.dict("os.environ", {"VAULT_ROOT": str(tmp_path)}),
        ):
            await ingest_handler(session)
        # Positional args: (engine, item_id, error).
        args, _ = self.mock_failed.call_args
        assert error_text in args[2]

    @pytest.mark.asyncio
    async def test_ingest_handler_returns_none_on_success(self, tmp_path):
        """ingest_handler always returns None (not a datetime)."""
        item = _make_item(source_type="webpage", url="https://example.com")
        session = MagicMock()
        with (
            patch("knowledge.ingest_queue._claim_one", return_value=item),
            patch(
                "knowledge.ingest_queue.fetch_webpage",
                AsyncMock(return_value=("Title", "body")),
            ),
            patch(
                "knowledge.ingest_queue._write_raw_md", return_value=tmp_path / "out.md"
            ),
            patch.dict("os.environ", {"VAULT_ROOT": str(tmp_path)}),
        ):
            result = await ingest_handler(session)
        assert result is None
