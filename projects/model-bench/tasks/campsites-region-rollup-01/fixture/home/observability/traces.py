"""Fetch SigNoz trace spans from ClickHouse for the frontend waterfall renderer.

Backs the authenticated demos page (GET /api/demos/firecracker/trace/{trace_id}):
trace ingestion lags ~5-10s behind span emission, so callers poll this helper and
treat an empty list as "still ingesting" rather than an error.
"""

from __future__ import annotations

import logging
import os
import re

from home.observability.clickhouse import ClickHouseClient

logger = logging.getLogger(__name__)

# SigNoz trace IDs are 32 lowercase hex chars (traceID FixedString(32)).
# ClickHouseClient has no parameterized-query support (see clickhouse.py), so
# we validate strictly before interpolating rather than inventing bind params.
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_SPANS_QUERY = """\
SELECT spanID, parentSpanID, name, serviceName, durationNano, hasError,
       toUnixTimestamp64Nano(timestamp) AS start_nano
FROM signoz_traces.distributed_signoz_index_v3
WHERE traceID = '{trace_id}'
ORDER BY timestamp"""

# Goose (service `goose-coding`) does NOT honor an inbound TRACEPARENT (v1.39.0),
# so its spans live in their own trace, not nested under the demo run. The runner
# stamps the caller's trace id onto every goose span as the resource attribute
# `caller.trace_id` (via OTEL_RESOURCE_ATTRIBUTES), so we recover the agent's own
# internal spans by that correlation key and render them as their own sub-timeline
# under the demo waterfall. Resource attributes live in the `resources_string` Map.
_CORRELATED_QUERY = """\
SELECT spanID, parentSpanID, name, serviceName, durationNano, hasError,
       toUnixTimestamp64Nano(timestamp) AS start_nano
FROM signoz_traces.distributed_signoz_index_v3
WHERE serviceName = 'goose-coding'
  AND resources_string['caller.trace_id'] = '{trace_id}'
ORDER BY timestamp"""


async def fetch_trace_spans(trace_id: str) -> list[dict]:
    """Return normalized spans for trace_id, sorted by start time.

    Returns [] if the trace has no spans yet (not an error: ingestion lags
    span emission by ~5-10s, so callers poll until spans appear) or if
    trace_id is not a well-formed 32-char hex SigNoz trace ID.
    """
    if not _TRACE_ID_RE.match(trace_id):
        logger.warning("Rejecting malformed trace_id: %r", trace_id)
        return []

    client = ClickHouseClient(
        base_url=os.environ.get("CLICKHOUSE_URL", ""),
        user=os.environ.get("CLICKHOUSE_USER", ""),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )
    try:
        rows = await client.query_rows(_SPANS_QUERY.format(trace_id=trace_id))
    finally:
        await client.close()

    return _normalize_spans(rows)


async def fetch_correlated_spans(trace_id: str) -> list[dict]:
    """Return normalized `goose-coding` spans correlated to trace_id.

    Goose runs in its own trace (it does not honor an inbound parent context),
    but the runner stamps `caller.trace_id=<trace_id>` onto every goose span as
    a resource attribute, so we recover the agent's own internal spans by that
    key. Normalized identically to :func:`fetch_trace_spans`, with start_ms
    relative to this correlated set's OWN earliest span, so it renders as its
    own self-contained sub-timeline. Returns [] when none have landed yet (they
    stream in as the agent works) or if trace_id is malformed.
    """
    if not _TRACE_ID_RE.match(trace_id):
        logger.warning("Rejecting malformed trace_id: %r", trace_id)
        return []

    client = ClickHouseClient(
        base_url=os.environ.get("CLICKHOUSE_URL", ""),
        user=os.environ.get("CLICKHOUSE_USER", ""),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )
    try:
        rows = await client.query_rows(_CORRELATED_QUERY.format(trace_id=trace_id))
    finally:
        await client.close()

    return _normalize_spans(rows)


def _normalize_spans(rows: list[dict]) -> list[dict]:
    if not rows:
        return []

    start_nanos = [int(row["start_nano"]) for row in rows]
    min_start = min(start_nanos)

    spans = []
    for row, start_nano in zip(rows, start_nanos):
        spans.append(
            {
                "span_id": row["spanID"],
                "parent_span_id": row["parentSpanID"],
                "name": row["name"],
                "service": row["serviceName"],
                "start_ms": (start_nano - min_start) / 1e6,
                "duration_ms": int(row["durationNano"]) / 1e6,
                "error": bool(row["hasError"]),
            }
        )

    spans.sort(key=lambda span: span["start_ms"])
    return spans
