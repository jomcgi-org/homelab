"""Campaign working-set templates; no data export or serving integration.

Every concrete model has ONE table. Campaign-global and facts_<pc> schemas
are separate ACL targets; dm is a separate, never player-readable partition.
Projection serialization is lossless: pinned Loom JSON actions only support
scalars, and their timestamp parser drops offsets and fractional precision.
JSON, arrays and aware timestamps therefore use canonical string columns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.types import TypeDecorator
from sqlalchemy.dialects import postgresql
from sqlmodel import SQLModel

from grimoire import models
from grimoire.loom.client import (
    Action,
    Conflict,
    Link,
    LoomClient,
    Model,
    NotFound,
    Property,
    TableRef,
)

SOURCES: dict[str, type[SQLModel]] = {
    "Campaign": models.Campaign,
    "PlayerCharacter": models.PlayerCharacter,
    "GameSession": models.GameSession,
    "SessionEvent": models.SessionEvent,
    "KnowledgeGrantSlice": models.KnowledgeGrant,
    "Note": models.Note,
    "CharacterFact": models.CharacterFact,
    "SessionSummary": models.GameSession,
}
# Slice is a grant projection, never a join to the unrestricted entity body.
# Summary is an explicit future materialization, not an existing SQLModel table.
SUMMARY_FIELDS = ("id", "campaign_id", "session_id", "summary", "created_at")


def column_type(column: Any) -> str:
    ty = column.type.dialect_impl(postgresql.dialect())
    while isinstance(ty, TypeDecorator):
        ty = ty.impl
    if isinstance(ty, Boolean):
        return "boolean"
    if isinstance(ty, BigInteger):
        return "long"
    if isinstance(ty, Integer):
        return "integer"
    if isinstance(
        ty, (String, DateTime, postgresql.UUID, postgresql.JSONB, postgresql.ARRAY)
    ):
        return "string"
    raise ValueError(f"unsupported projected SQL type: {column.name}: {ty}")


def properties(kind: str) -> tuple[Property, ...]:
    if kind == "SessionSummary":
        return tuple(Property(name, "string", True) for name in SUMMARY_FIELDS)
    return tuple(
        Property(c.name, column_type(c), c.primary_key or not c.nullable)
        for c in SOURCES[kind].__table__.columns
    )


def uuid_key(value: str) -> str:
    return UUID(str(value)).hex


@dataclass(frozen=True)
class Partition:
    campaign_id: str
    player_character_id: str | None = None
    dm: bool = False

    def __post_init__(self) -> None:
        if type(self.dm) is not bool:
            raise ValueError("dm must be a boolean")
        object.__setattr__(self, "campaign_id", str(UUID(str(self.campaign_id))))
        if self.player_character_id is not None:
            object.__setattr__(
                self, "player_character_id", str(UUID(str(self.player_character_id)))
            )
        if self.dm and self.player_character_id:
            raise ValueError("DM and PC partitions are mutually exclusive")

    @property
    def schema(self) -> str:
        scope = (
            "dm"
            if self.dm
            else (
                f"facts_{uuid_key(self.player_character_id)}"
                if self.player_character_id
                else "facts_global"
            )
        )
        return f"grimoire_{uuid_key(self.campaign_id)}_{scope}"

    def model(self, kind: str) -> Model:
        return Model(
            f"{kind}_{self.schema}",
            TableRef(
                self.schema,
                SOURCES[kind].__tablename__
                if kind != "SessionSummary"
                else "session_summary",
            ),
            properties(kind),
        )

    def links(self) -> tuple[Link, ...]:
        # All endpoints stay in the same campaign AND audience partition.
        endpoints = [
            (k, "campaign", "Campaign", "campaign_id")
            for k in SOURCES
            if k != "Campaign"
        ]
        endpoints += [
            (k, "session", "GameSession", "session_id")
            for k in ("SessionEvent", "CharacterFact", "SessionSummary")
        ]
        return tuple(
            Link(name, self.model(source).name, self.model(target).name, column)
            for source, name, target, column in endpoints
        )

    def actions(self, kind: str) -> tuple[Action, Action, Action]:
        model = self.model(kind)
        return (
            Action(f"insert_{model.name}", model.name, "insert", model.properties),
            Action(f"update_{model.name}", model.name, "update", model.properties),
            Action(
                f"retract_{model.name}",
                model.name,
                "delete",
                (Property("id", "string", True),),
            ),
        )

    def payload(self, kind: str, row: dict[str, Any]) -> dict[str, Any]:
        """Validate partition ownership before serialization; no implicit fan-out.

        SessionEvent/SessionSummary are DM-only until an audience-safe export is
        separately implemented. No exporter is installed by this support package.
        """
        row = dict(row)
        for name in ("id", "campaign_id", "player_character_id"):
            if name in row and row[name] is not None:
                row[name] = str(UUID(str(row[name])))
        if "viewer_key" in row and row["viewer_key"] != "party":
            row["viewer_key"] = str(UUID(str(row["viewer_key"])))
        allowed = {p.name for p in properties(kind)}
        if set(row) != allowed:
            raise ValueError("payload must contain exactly the declared columns")
        campaign = row["id"] if kind == "Campaign" else row["campaign_id"]
        if uuid_key(str(campaign)) != uuid_key(self.campaign_id):
            raise ValueError("foreign campaign")
        if (
            self.dm
            and kind == "Note"
            and row["kind"] == "character"
            and row["dm_readable"] is not True
        ):
            raise ValueError("private character note is not DM-readable")
        if kind == "Note" and row["deleted_at"] is not None:
            raise ValueError("deleted note requires retraction")
        if kind == "SessionEvent" and row["retracted_at"] is not None:
            raise ValueError("retracted event cannot be exported")
        if kind == "CharacterFact" and row["status"] == "retracted":
            raise ValueError("retracted fact cannot be exported")
        if not self.dm:
            pc = self.player_character_id
            if kind in {"SessionEvent", "SessionSummary"}:
                raise ValueError("unprojected session content is DM-only")
            if kind == "KnowledgeGrantSlice":
                if not pc or row["player_character_id"] != pc:
                    raise ValueError("grant requires its exact PC partition")
                if (
                    row["grant_scope"] == "name_only"
                    and row["revealed_details"] is not None
                ):
                    raise ValueError(
                        "recognition stub cannot contain a retrievable body"
                    )
                if row["grant_scope"] not in {"full", "partial", "name_only"}:
                    raise ValueError("unknown grant scope")
            if kind == "Note":
                if (
                    pc
                    and (row["kind"] != "character" or row["player_character_id"] != pc)
                ) or (
                    not pc
                    and (
                        row["kind"] != "party" or row["player_character_id"] is not None
                    )
                ):
                    raise ValueError("note belongs to another partition")
            if kind == "CharacterFact":
                if row["player_character_id"] != pc or row["viewer_key"] != (
                    pc or "party"
                ):
                    raise ValueError("fact belongs to another partition")
            if kind == "PlayerCharacter" and pc and row["id"] != pc:
                raise ValueError("character belongs to another partition")
        result = {}
        for prop in properties(kind):
            value = row[prop.name]
            if value is not None and kind != "SessionSummary":
                ty = (
                    SOURCES[kind]
                    .__table__.columns[prop.name]
                    .type.dialect_impl(postgresql.dialect())
                )
                while isinstance(ty, TypeDecorator):
                    ty = ty.impl
                if isinstance(ty, postgresql.UUID):
                    value = str(UUID(str(value)))
                if isinstance(ty, DateTime):
                    if isinstance(value, str):
                        value = datetime.fromisoformat(value)
                    if not isinstance(value, datetime):
                        raise ValueError("invalid timestamp")
                if isinstance(ty, postgresql.JSONB) and not isinstance(
                    value, (dict, list)
                ):
                    raise ValueError("JSON projection requires structured data")
                if isinstance(ty, postgresql.ARRAY):
                    if not isinstance(value, list):
                        raise ValueError("array projection requires a list")
                    value = [str(UUID(str(v))) for v in value]
            if value is None:
                if prop.required:
                    raise ValueError(f"required column: {prop.name}")
            elif prop.ty == "string":
                if isinstance(value, datetime):
                    if value.tzinfo is None or value.utcoffset() is None:
                        raise ValueError("timestamp requires an explicit timezone")
                    value = value.isoformat()
                elif isinstance(value, UUID):
                    value = str(value)
                elif isinstance(value, (dict, list)):
                    value = json.dumps(value, sort_keys=True, separators=(",", ":"))
                elif not isinstance(value, str):
                    raise ValueError(f"invalid string: {prop.name}")
            elif prop.ty == "boolean" and type(value) is not bool:
                raise ValueError(f"invalid boolean: {prop.name}")
            elif prop.ty in {"integer", "long"}:
                if type(value) is not int or not -(2**63) <= value < 2**63:
                    raise ValueError(f"invalid integer: {prop.name}")
                if prop.ty == "integer" and not -(2**31) <= value < 2**31:
                    raise ValueError(f"integer exceeds int32: {prop.name}")
                if prop.ty == "long":
                    value = str(value)
            result[prop.name] = value
        UUID(result["id"])
        return result


async def apply(client: LoomClient, partitions: tuple[Partition, ...]) -> int:
    writes = 0
    for partition in partitions:
        for kind in SOURCES:
            writes += await client.define_model(partition.model(kind))
        for link in partition.links():
            writes += await client.define_link(link)
        for kind in SOURCES:
            for action in partition.actions(kind):
                writes += await client.define_action(action)
    return writes


async def upsert(
    client: LoomClient, partition: Partition, kind: str, row: dict[str, Any]
) -> Any:
    """Composite upsert_* contract; Loom has no native upsert action kind.

    Caller must serialize writers for this partition AND establish read-after-write
    visibility before another call. An acknowledged action alone is not a proven
    mirror-visibility barrier. Enablement must establish that contract or supply
    an explicit barrier in the future writer; no exporter calls this today.
    One latest-state read and
    one insert/update, with at most one deterministic reconciliation on a 409 or
    update-target 404. Never retry timeouts, 5xx or uncertain writes. Loom does
    not promise unique insert identities, so this is NOT concurrent-safe without
    the caller's single-writer lease. Definitions alone do not acquire a lease.
    """
    body = partition.payload(kind, row)
    model = partition.model(kind)
    insert, update, _ = partition.actions(kind)
    exists = await client.contains(model, body["id"])
    try:
        return await client.invoke(update.name if exists else insert.name, body)
    except (Conflict, NotFound) as error:
        # A missing insert action is configuration failure, not an absent row.
        if isinstance(error, NotFound) and not exists:
            raise
        now_exists = await client.contains(model, body["id"])
        if now_exists == exists:
            raise
        return await client.invoke(update.name if now_exists else insert.name, body)
