"""Unit tests for knowledge.http_cache -- _as_utc and _graph_etag helpers.

Both functions are pure (no I/O, no DB), so no fixtures are needed.
These helpers back the Cache-Control / ETag / Last-Modified behaviour for both
the private and public knowledge graph endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone

from knowledge.http_cache import _as_utc, _GRAPH_CACHE_CONTROL, _graph_etag


# ---------------------------------------------------------------------------
# _as_utc
# ---------------------------------------------------------------------------


class TestAsUtc:
    def test_none_returns_none(self):
        assert _as_utc(None) is None

    def test_naive_datetime_treated_as_utc(self):
        naive = datetime(2024, 6, 1, 12, 0, 0)
        result = _as_utc(naive)
        assert result is not None
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc
        # Year/month/day/hour preserved
        assert result.year == 2024
        assert result.month == 6
        assert result.hour == 12

    def test_aware_datetime_converted_to_utc(self):
        from datetime import timedelta

        plus2 = timezone(timedelta(hours=2))
        aware = datetime(2024, 6, 1, 14, 0, 0, tzinfo=plus2)
        result = _as_utc(aware)
        assert result is not None
        assert result.tzinfo == timezone.utc
        # 14:00 +02:00 == 12:00 UTC
        assert result.hour == 12

    def test_already_utc_passthrough(self):
        utc_dt = datetime(2024, 3, 15, 9, 30, 0, tzinfo=timezone.utc)
        result = _as_utc(utc_dt)
        assert result == utc_dt
        assert result.tzinfo == timezone.utc

    def test_returns_datetime_instance(self):
        naive = datetime(2025, 1, 1, 0, 0, 0)
        result = _as_utc(naive)
        assert isinstance(result, datetime)


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
