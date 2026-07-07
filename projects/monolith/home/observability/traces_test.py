"""Tests for the trace-spans-by-trace_id helper backing the demos waterfall."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from home.observability import traces

VALID_TRACE_ID = "a" * 32


def _mock_ch_client(rows):
    mock = MagicMock()
    mock.query_rows = AsyncMock(return_value=rows)
    mock.close = AsyncMock()
    return mock


@pytest.mark.asyncio
async def test_fetch_trace_spans_normalizes_shape_and_relative_start():
    rows = [
        {
            "spanID": "span-2",
            "parentSpanID": "span-1",
            "name": "invoke",
            "serviceName": "fc-invoke",
            "durationNano": 2_000_000,  # 2ms
            "hasError": False,
            "start_nano": 1_005_000_000,  # 5ms after span-1
        },
        {
            "spanID": "span-1",
            "parentSpanID": "",
            "name": "root",
            "serviceName": "monolith",
            "durationNano": 10_000_000,  # 10ms
            "hasError": True,
            "start_nano": 1_000_000_000,  # earliest
        },
    ]
    mock_ch = _mock_ch_client(rows)

    with patch("home.observability.traces.ClickHouseClient", return_value=mock_ch):
        result = await traces.fetch_trace_spans(VALID_TRACE_ID)

    assert result == [
        {
            "span_id": "span-1",
            "parent_span_id": "",
            "name": "root",
            "service": "monolith",
            "start_ms": 0.0,
            "duration_ms": 10.0,
            "error": True,
        },
        {
            "span_id": "span-2",
            "parent_span_id": "span-1",
            "name": "invoke",
            "service": "fc-invoke",
            "start_ms": 5.0,
            "duration_ms": 2.0,
            "error": False,
        },
    ]


@pytest.mark.asyncio
async def test_fetch_trace_spans_sorted_ascending_regardless_of_row_order():
    rows = [
        {
            "spanID": "late",
            "parentSpanID": "",
            "name": "late-span",
            "serviceName": "svc",
            "durationNano": 1_000_000,
            "hasError": False,
            "start_nano": 3_000_000_000,
        },
        {
            "spanID": "early",
            "parentSpanID": "",
            "name": "early-span",
            "serviceName": "svc",
            "durationNano": 1_000_000,
            "hasError": False,
            "start_nano": 1_000_000_000,
        },
        {
            "spanID": "mid",
            "parentSpanID": "",
            "name": "mid-span",
            "serviceName": "svc",
            "durationNano": 1_000_000,
            "hasError": False,
            "start_nano": 2_000_000_000,
        },
    ]
    mock_ch = _mock_ch_client(rows)

    with patch("home.observability.traces.ClickHouseClient", return_value=mock_ch):
        result = await traces.fetch_trace_spans(VALID_TRACE_ID)

    assert [span["span_id"] for span in result] == ["early", "mid", "late"]
    assert [span["start_ms"] for span in result] == [0.0, 1000.0, 2000.0]


@pytest.mark.asyncio
async def test_fetch_trace_spans_returns_empty_list_when_no_rows():
    mock_ch = _mock_ch_client([])

    with patch("home.observability.traces.ClickHouseClient", return_value=mock_ch):
        result = await traces.fetch_trace_spans(VALID_TRACE_ID)

    assert result == []
    mock_ch.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_trace_spans_rejects_malformed_trace_id_without_querying():
    mock_ch = _mock_ch_client([])

    with patch(
        "home.observability.traces.ClickHouseClient", return_value=mock_ch
    ) as mock_client_cls:
        result = await traces.fetch_trace_spans("not-a-valid-trace-id; DROP TABLE x")

    assert result == []
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_trace_spans_root_with_orphan_parent_is_kept():
    """A span whose parent_span_id isn't in the returned set is still kept as-is."""
    rows = [
        {
            "spanID": "child",
            "parentSpanID": "missing-parent",
            "name": "child-span",
            "serviceName": "svc",
            "durationNano": 500_000,
            "hasError": False,
            "start_nano": 1_000_000_000,
        },
    ]
    mock_ch = _mock_ch_client(rows)

    with patch("home.observability.traces.ClickHouseClient", return_value=mock_ch):
        result = await traces.fetch_trace_spans(VALID_TRACE_ID)

    assert len(result) == 1
    assert result[0]["parent_span_id"] == "missing-parent"
