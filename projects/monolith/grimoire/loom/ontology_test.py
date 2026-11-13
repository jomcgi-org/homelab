"""Projection parity uses production Postgres types, without SQLite coercion."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import BigInteger, Boolean, DateTime, Integer
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import TypeDecorator

from grimoire.loom.client import TableRef
from grimoire.loom.ontology import Partition, SOURCES, SUMMARY_FIELDS, properties

CAMPAIGN = "11111111-1111-4111-8111-111111111111"
PC = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


@pytest.mark.parametrize("kind", list(SOURCES))
def test_schema_parity(kind):
    props = {p.name: p for p in properties(kind)}
    if kind == "SessionSummary":
        assert set(props) == set(SUMMARY_FIELDS)
        assert all(p.ty == "string" and p.required for p in props.values())
        return
    columns = SOURCES[kind].__table__.columns
    assert set(props) == set(columns.keys())
    for column in columns:
        ty = column.type.dialect_impl(postgresql.dialect())
        while isinstance(ty, TypeDecorator):
            ty = ty.impl
        expected = (
            "boolean"
            if isinstance(ty, Boolean)
            else "long"
            if isinstance(ty, BigInteger)
            else "integer"
            if isinstance(ty, Integer)
            else "string"
        )
        assert props[column.name].ty == expected
        # SQLModel marks these PK columns nullable; Loom identity must be required.
        assert props[column.name].required == (
            column.primary_key or not column.nullable
        )
        if isinstance(ty, DateTime):
            assert ty.timezone
    assert columns.id.primary_key and props["id"].required
    assert not any("vector" in p.ty.lower() for p in props.values())


def test_partition_bindings_and_links_never_cross_campaign_or_pc():
    partitions = [
        Partition(CAMPAIGN),
        Partition(CAMPAIGN, PC),
        Partition(CAMPAIGN, OTHER),
        Partition(OTHER, PC),
        Partition(CAMPAIGN, dm=True),
    ]
    names, tables = set(), set()
    for partition in partitions:
        for kind in SOURCES:
            model = partition.model(kind)
            assert isinstance(model.table, TableRef)
            assert model.name not in names
            assert model.table not in tables
            names.add(model.name)
            tables.add(model.table)
        local = {partition.model(k).name for k in SOURCES}
        assert all(
            link.source in local and link.target in local for link in partition.links()
        )
    with pytest.raises(ValueError):
        Partition("bad")
    with pytest.raises(ValueError):
        Partition(CAMPAIGN, PC, dm=True)


def row(kind):
    result = {p.name: None for p in properties(kind)}
    for p in properties(kind):
        if p.required:
            result[p.name] = (
                False
                if p.ty == "boolean"
                else 1
                if p.ty in {"long", "integer"}
                else "value"
            )
    result.update(id=str(uuid4()), campaign_id=CAMPAIGN)
    return result


@pytest.mark.parametrize("kind", ["KnowledgeGrantSlice", "Note", "CharacterFact"])
def test_private_partition_rejects_foreign_rows(kind):
    p = Partition(CAMPAIGN, PC)
    data = row(kind)
    data.update(player_character_id=OTHER)
    if kind == "Note":
        data.update(kind="character")
    if kind == "CharacterFact":
        data.update(viewer_key=OTHER)
    with pytest.raises(ValueError):
        p.payload(kind, data)
    data["campaign_id"] = OTHER
    with pytest.raises(ValueError, match="foreign campaign"):
        p.payload(kind, data)


def test_grant_projection_and_lossless_wire_types():
    p = Partition(CAMPAIGN, PC)
    data = row("KnowledgeGrantSlice")
    data.update(
        entity_id=str(uuid4()),
        player_character_id=PC,
        grant_scope="partial",
        revealed_details={"known": "only this"},
        created_at=datetime(2026, 1, 1, 1, 2, 3, 456789, tzinfo=timezone.utc),
    )
    payload = p.payload("KnowledgeGrantSlice", data)
    assert payload["revealed_details"] == '{"known":"only this"}'
    assert payload["created_at"] == "2026-01-01T01:02:03.456789+00:00"
    with pytest.raises(ValueError):
        Partition(CAMPAIGN).payload("KnowledgeGrantSlice", data)
    data["grant_scope"] = "name_only"
    with pytest.raises(ValueError, match="recognition"):
        p.payload("KnowledgeGrantSlice", data)
    data["revealed_details"] = None
    assert p.payload("KnowledgeGrantSlice", data)["revealed_details"] is None
    data["created_at"] = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="timezone"):
        p.payload("KnowledgeGrantSlice", data)


@pytest.mark.parametrize("kind", ["SessionEvent", "SessionSummary"])
def test_session_bodies_cannot_be_written_to_player_or_global_partition(kind):
    for partition in (Partition(CAMPAIGN), Partition(CAMPAIGN, PC)):
        with pytest.raises(ValueError, match="DM-only"):
            partition.payload(kind, row(kind))


def test_fact_global_and_pc_isolation_and_long_wire_shape():
    data = row("CharacterFact")
    data.update(
        player_character_id=None,
        viewer_key="party",
        session_id=str(uuid4()),
        evidence_event_ids=[str(uuid4())],
        created_at=datetime.now(timezone.utc),
    )
    assert Partition(CAMPAIGN).payload("CharacterFact", data)["viewer_key"] == "party"
    with pytest.raises(ValueError, match="another partition"):
        Partition(CAMPAIGN, PC).payload("CharacterFact", data)
    event = row("SessionEvent")
    event.update(
        session_id=str(uuid4()),
        seq=2**54,
        body={"safe": False},
        audience_pc_ids=[],
        created_at=datetime.now(timezone.utc),
    )
    assert Partition(CAMPAIGN, dm=True).payload("SessionEvent", event)["seq"] == str(
        2**54
    )
    event["seq"] = True
    with pytest.raises(ValueError, match="integer"):
        Partition(CAMPAIGN, dm=True).payload("SessionEvent", event)


def test_partition_configuration_is_typed_and_uuid_comparisons_are_canonical():
    with pytest.raises(ValueError, match="boolean"):
        Partition(CAMPAIGN, dm="false")
    with pytest.raises(ValueError):
        Partition(CAMPAIGN, player_character_id="")
    campaign = str(uuid4())
    pc = uuid4()
    partition = Partition(campaign.upper(), str(pc).upper())
    assert partition.campaign_id == campaign
    assert partition.player_character_id == str(pc)
    grant = row("KnowledgeGrantSlice")
    grant.update(
        campaign_id=campaign.upper(),
        player_character_id=pc,
        entity_id=str(uuid4()),
        grant_scope="partial",
        revealed_details={},
    )
    assert partition.payload("KnowledgeGrantSlice", grant)[
        "player_character_id"
    ] == str(pc)
