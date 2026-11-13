"""Unit tests for knowledge.http_cache ETag and Cache-Control helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from knowledge.http_cache import _GRAPH_CACHE_CONTROL, _graph_etag


# ---------------------------------------------------------------------------
# _graph_etag
# ---------------------------------------------------------------------------


class TestGraphEtag:
    def test_none_indexed_at_produces_null_stamp(self):
        etag = _graph_etag(10, None)
        assert '"null-10"' == etag

    def test_non_none_indexed_at_uses_isoformat(self):
        dt = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        etag = _graph_etag(5, dt)
        assert etag.startswith('"')
        assert etag.endswith('"')
        assert dt.isoformat() in etag
        assert "-5" in etag

    def test_node_count_zero(self):
        etag = _graph_etag(0, None)
        assert etag == '"null-0"'

    def test_different_counts_produce_different_etags(self):
        dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        e1 = _graph_etag(3, dt)
        e2 = _graph_etag(4, dt)
        assert e1 != e2

    def test_different_timestamps_produce_different_etags(self):
        dt1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        dt2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
        e1 = _graph_etag(3, dt1)
        e2 = _graph_etag(3, dt2)
        assert e1 != e2

    def test_etag_is_quoted_string(self):
        """ETag values must be double-quoted per RFC 7232."""
        etag = _graph_etag(1, None)
        assert etag[0] == '"' and etag[-1] == '"'


# ---------------------------------------------------------------------------
# _GRAPH_CACHE_CONTROL constant
# ---------------------------------------------------------------------------


def test_graph_cache_control_is_public():
    assert "public" in _GRAPH_CACHE_CONTROL


def test_graph_cache_control_has_stale_while_revalidate():
    assert "stale-while-revalidate" in _GRAPH_CACHE_CONTROL
