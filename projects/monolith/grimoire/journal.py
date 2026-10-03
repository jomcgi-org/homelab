"""Deterministic journal projection over authorized, unretracted event snapshots."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from grimoire.audience import Member, Viewer, can_see
from grimoire.models import SessionEvent


def _identity(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        return value


@dataclass
class Journal:
    learned: list[dict[str, Any]] = field(default_factory=list)
    received: list[dict[str, Any]] = field(default_factory=list)
    people_and_places: list[dict[str, Any]] = field(default_factory=list)
    rolls: list[dict[str, Any]] = field(default_factory=list)
    open_threads: list[dict[str, Any]] = field(default_factory=list)
    # True when the session's visible event stream exceeded the router's
    # per-session read budget, so this journal folds only the earliest
    # budgeted events. Never silently truncated: readers must treat a True
    # value as partial.
    truncated: bool = False


def narration_entity_ids(events: Iterable[SessionEvent]) -> set[str]:
    """Read UUID references only; callers supply already authorized live rows."""
    ids = set()
    for row in events:
        values = row.body.get("entity_ids") if row.kind == "narration" else None
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            try:
                canonical = str(UUID(value))
            except ValueError:
                continue
            if value.casefold() == canonical:
                ids.add(canonical)
    return ids


def visible_rows(viewer, member, events, view):
    if view not in ("mine", "party"):
        raise ValueError("unknown journal view")
    return sorted(
        (
            row
            for row in events
            if can_see(viewer, member, row)
            and row.retracted_at is None
            and (view != "party" or row.audience == "table")
        ),
        key=lambda row: (row.seq, row.id),
    )


def _event(row: SessionEvent, viewer: Viewer, member: Member) -> dict[str, Any]:
    return {
        "id": row.id,
        "seq": row.seq,
        "kind": row.kind,
        "audience": row.audience,
        "audience_pc_ids": [
            pc for pc in row.audience_pc_ids if viewer == "dm" or pc == viewer
        ],
        "author_member_id": (
            row.author_member_id
            if viewer == "dm" or row.author_member_id == member.id
            else None
        ),
        "body": row.body,
    }


def journal(
    viewer: Viewer,
    member: Member,
    events: Iterable[SessionEvent],
    *,
    current_grants: set[tuple[str, str]],
    visible_entities: Mapping[str, dict[str, Any]],
    view: Literal["mine", "party"] = "mine",
) -> Journal:
    """Historical Learned snapshots survive downgrades, but not silent revokes.

    Narration body.entity_ids is a list of dashed UUIDs. body.reply_to is an
    exact event id; only a later visible live reply resolves a private action.
    Administrative author and audience ids follow the feed projection rules.
    """
    rows = visible_rows(viewer, member, events, view)
    current_grants = {
        (_identity(pc), _identity(entity)) for pc, entity in current_grants
    }
    result = Journal()
    learned = {}
    people = {}
    replies = {}
    for row in rows:
        reply_to = row.body.get("reply_to")
        if isinstance(reply_to, str):
            replies[reply_to] = row.seq
        if row.kind == "reveal" and view == "mine" and row.audience == "pcs":
            bodies = row.body.get("reveals", [row.body])
            if not isinstance(bodies, list):
                continue
            for body in bodies:
                if not isinstance(body, dict) or body.get("silent"):
                    continue
                entity_id = body.get("entity_id")
                if not isinstance(entity_id, str):
                    continue
                for pc in row.audience_pc_ids:
                    if viewer != "dm" and pc != viewer:
                        continue
                    entry = {
                        "event_id": row.id,
                        "seq": row.seq,
                        "entity_id": entity_id,
                        "name": body.get("name"),
                        "entity_type": body.get("entity_type"),
                        "grant_scope": body.get("grant_scope"),
                        "retracted": bool(body.get("retracted")),
                    }
                    if viewer == "dm":
                        entry["player_character_id"] = pc
                    if not entry["retracted"] and "entity" in body:
                        entry["entity"] = body["entity"]
                    learned[pc, _identity(entity_id)] = entry
        elif row.kind == "handout":
            result.received.append(_event(row, viewer, member))
        elif row.kind == "roll" and (
            view == "party" or row.author_member_id == member.id
        ):
            result.rolls.append(_event(row, viewer, member))

    for key, entry in sorted(
        learned.items(), key=lambda item: (item[1]["seq"], item[0])
    ):
        if not entry["retracted"] and tuple(map(_identity, key)) not in current_grants:
            continue
        result.learned.append(entry)
        if not entry["retracted"]:
            people[_identity(entry["entity_id"])] = {
                "id": entry["entity_id"],
                "name": entry["name"],
                "entity_type": entry["entity_type"],
            }
    for entity_id in sorted(narration_entity_ids(rows)):
        if entity_id in visible_entities:
            identity = visible_entities[entity_id]
            people.setdefault(
                entity_id,
                {key: identity[key] for key in ("id", "name", "entity_type")},
            )
    result.people_and_places = [people[key] for key in sorted(people)]
    if view == "mine":
        result.open_threads = [
            _event(row, viewer, member)
            for row in rows
            if row.kind == "action"
            and row.author_member_id == member.id
            and row.audience != "table"
            and replies.get(row.id, 0) <= row.seq
        ]
    return result
