"""Tracing helpers for swarm workflows and steps.

Spans inside ``@DBOS.step()`` bodies emit once per real execution and never on
replay because DBOS returns a cached checkpoint. Workflow-level spans
deliberately re-emit on recovery because recovery is a distinct execution.
"""

from __future__ import annotations

from opentelemetry import trace

tracer = trace.get_tracer("swarm")


def set_attributes(span, attributes: dict) -> None:
    """Set present attributes without passing optional None values to OTel.

    OTel drops None values, so filtering here avoids log noise from optional
    fields such as a session id before the session exists or a terminal reason
    on a turn with none.
    """
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)
