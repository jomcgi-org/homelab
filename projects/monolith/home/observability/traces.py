"""Trace waterfall compatibility helpers while the span store is replaced.

The previous span store was SigNoz's ClickHouse, which is being removed in
#5362. These helpers return no spans until #5363 connects the replacement span
store. Callers already treat an empty list as "still ingesting" and poll, so the
UI spins instead of erroring.
"""

from __future__ import annotations

import re

# Trace IDs are 32 lowercase hexadecimal characters.
#
# Retained deliberately for #5363 and currently referenced by nothing: both
# fetch helpers return before validating anything. Do not read this as live
# input validation, and do not delete it either, since the replacement store
# will need the same guard before interpolating a trace id into a query.
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


async def fetch_trace_spans(trace_id: str) -> list[dict]:
    """Return no spans until #5363 connects the replacement span store."""
    return []


async def fetch_correlated_spans(trace_id: str) -> list[dict]:
    """Return no correlated spans until #5363 connects the replacement store.

    Whatever replaces the span store has to preserve resource attributes, or
    this function cannot be restored. Goose (service ``goose-coding``, v1.39.0)
    does not honor an inbound TRACEPARENT, so its spans live in their own trace
    rather than nested under the demo run. The runner stamps the caller's trace
    id onto every goose span as the resource attribute ``caller.trace_id`` (via
    OTEL_RESOURCE_ATTRIBUTES), and that correlation key is the only way back to
    the agent's internal spans. They render as their own sub-timeline, with
    start_ms relative to the correlated set's OWN earliest span.
    """
    return []


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
