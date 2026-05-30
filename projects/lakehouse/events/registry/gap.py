"""Gap-event payload schemas (entity_type ``gap``).

Mirrors what the gap-drain pipeline publishes onto ``events.knowledge.gap``.
The gap lifecycle (states, classes) follows the existing knowledge-graph gap
pipeline; these payloads carry just enough for consumers/projections without
inlining the full gap record.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# Gap classification (epistemic kind), per the gap pipeline. ``internal`` and
# ``hybrid`` gaps wait for human review; ``external`` gaps auto-research.
GapClass = str  # "external" | "internal" | "hybrid"

# Gap workflow state, per the gap pipeline scheduler.
GapState = str  # e.g. "new" | "researching" | "in_review" | "answered" | "dropped"


class GapCreatedPayload(BaseModel):
    """Payload for a ``gap`` ``created`` event."""

    model_config = ConfigDict(extra="allow")

    topic: str
    gap_class: GapClass
    state: GapState
    context: dict[str, Any] | None = None


class GapUpdatedPayload(BaseModel):
    """Payload for a ``gap`` ``updated`` event (state transition / re-class)."""

    model_config = ConfigDict(extra="allow")

    topic: str | None = None
    gap_class: GapClass | None = None
    state: GapState
    context: dict[str, Any] | None = None


class GapTombstonedPayload(BaseModel):
    """Payload for a ``gap`` ``tombstoned`` event.

    Per ADR 017, a tombstone references the entity and a redacted reason; it
    does not carry the data being forgotten.
    """

    model_config = ConfigDict(extra="allow")

    reason: str | None = None


def register(schemas: dict[str, dict]) -> None:
    """Register gap payload models into the shared SCHEMAS dict."""
    schemas.setdefault("gap", {}).update(
        {
            "created": GapCreatedPayload,
            "updated": GapUpdatedPayload,
            "tombstoned": GapTombstonedPayload,
        }
    )
