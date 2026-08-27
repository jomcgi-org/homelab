"""Tests for the trace waterfall compatibility helpers."""

from __future__ import annotations

import pytest

from home.observability import traces


@pytest.mark.asyncio
@pytest.mark.parametrize("trace_id", ["a" * 32, "not-a-trace-id; DROP TABLE x"])
async def test_fetch_trace_spans_returns_empty(trace_id):
    assert await traces.fetch_trace_spans(trace_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("trace_id", ["a" * 32, "not-a-trace-id; DROP TABLE x"])
async def test_fetch_correlated_spans_returns_empty(trace_id):
    assert await traces.fetch_correlated_spans(trace_id) == []


def test_normalize_spans_normalizes_and_sorts():
    rows = [
        {
            "spanID": "span-2",
            "parentSpanID": "span-1",
            "name": "invoke",
            "serviceName": "fc-invoke",
            "durationNano": 2_000_000,
            "hasError": False,
            "start_nano": 1_005_000_000,
        },
        {
            "spanID": "span-1",
            "parentSpanID": "",
            "name": "root",
            "serviceName": "monolith",
            "durationNano": 10_000_000,
            "hasError": True,
            "start_nano": 1_000_000_000,
        },
    ]

    assert traces._normalize_spans(rows) == [
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
