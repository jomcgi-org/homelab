"""Initiative order logic for a game session (pure, no FastAPI).

The DM owns the order. Players only ever see a projection: hidden NPC entries
are masked (label and initiative replaced) or omitted entirely, depending on
the order's `hidden_display`. Turn events persist only the player projection,
so a hidden NPC's real label and initiative never reach the event log.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

MASKED_LABEL = "???"
MAX_ENTRIES = 50

HiddenDisplay = Literal["mask", "omit"]
Direction = Literal["next", "previous"]


class EmptyOrderError(ValueError):
    """The order has no entries, so there is no turn to advance."""


class InitiativeEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ]
    player_character_id: UUID | None = None
    initiative: int
    hidden: bool = False

    @model_validator(mode="after")
    def hidden_entries_are_npcs(self) -> "InitiativeEntry":
        if self.hidden and self.player_character_id is not None:
            raise ValueError("only NPC entries can be hidden")
        return self


class InitiativeSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[InitiativeEntry] = Field(max_length=MAX_ENTRIES)
    hidden_display: HiddenDisplay = "mask"
    active_index: int = Field(default=0, ge=0)
    round: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def active_index_in_range(self) -> "InitiativeSetRequest":
        limit = len(self.entries) or 1
        if self.active_index >= limit:
            raise ValueError("active_index is out of range")
        return self


class InitiativeAdvanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: Direction


def stored_entries(entries: list[InitiativeEntry]) -> list[dict[str, Any]]:
    """JSON-safe form persisted in the entries column."""
    return [
        {
            "label": entry.label,
            "player_character_id": (
                str(entry.player_character_id)
                if entry.player_character_id is not None
                else None
            ),
            "initiative": entry.initiative,
            "hidden": entry.hidden,
        }
        for entry in entries
    ]


def empty_view() -> dict[str, Any]:
    return {"round": 1, "active_index": None, "entries": []}


def _canonical_id(value: Any) -> str | None:
    """Lowercase UUID text, so ids compare equal whatever casing a driver returns."""
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except ValueError:
        return str(value)


def player_view(row: Any, viewer_character_id: str | None = None) -> dict[str, Any]:
    """Projection safe for players and for persisted turn events.

    Character ids are per-player: an entry keeps its id only when it is the
    viewer's own character. Turn events pass no viewer, so they carry none.
    """
    viewer = _canonical_id(viewer_character_id)
    entries: list[dict[str, Any]] = []
    active: int | None = None
    for index, entry in enumerate(row.entries):
        if entry.get("hidden"):
            if row.hidden_display == "omit":
                continue
            projected = {
                "label": MASKED_LABEL,
                "player_character_id": None,
                "initiative": None,
                "hidden": True,
            }
        else:
            projected = {
                "label": entry["label"],
                "player_character_id": (
                    entry.get("player_character_id")
                    if viewer is not None
                    and _canonical_id(entry.get("player_character_id")) == viewer
                    else None
                ),
                "initiative": entry["initiative"],
                "hidden": False,
            }
        if index == row.active_index:
            active = len(entries)
        entries.append(projected)
    return {"round": row.round, "active_index": active, "entries": entries}


def dm_view(row: Any) -> dict[str, Any]:
    return {
        "round": row.round,
        "active_index": row.active_index if row.entries else None,
        "hidden_display": row.hidden_display,
        "entries": [dict(entry) for entry in row.entries],
    }


def advance(row: Any, direction: Direction) -> None:
    """Move the active turn in place, wrapping rounds at either end."""
    count = len(row.entries)
    if count == 0:
        raise EmptyOrderError("Initiative order is empty")
    if direction == "next":
        if row.active_index + 1 >= count:
            row.active_index = 0
            row.round += 1
        else:
            row.active_index += 1
    elif row.active_index > 0:
        row.active_index -= 1
    elif row.round > 1:
        row.active_index = count - 1
        row.round -= 1
