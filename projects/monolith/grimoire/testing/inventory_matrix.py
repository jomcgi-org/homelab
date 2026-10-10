"""Independent visibility oracle shared by SQLite and PostgreSQL."""

from datetime import UTC, datetime
from itertools import product
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    select,
)

from grimoire.audience import AUTHOR_MEMBER_ID_TYPE, can_see_item, inventory_predicate

OWNER = str(UUID(int=6633))
OTHER = str(UUID(int=6634))


def inventory_table(name):
    # Deliberately omit owner constraints to test corrupt/future kinds.
    return Table(
        name,
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, nullable=False),
        Column("owner_kind", String, nullable=False),
        Column("player_character_id", AUTHOR_MEMBER_ID_TYPE),
        Column("hidden_from_party", Boolean, nullable=False),
        Column("deleted_at", DateTime),
    )


def assert_inventory_agreement(connection, table):
    values = product(
        ("party", "character", "unknown"),
        (None, OWNER, OTHER),
        (False, True),
        (None, datetime(2026, 10, 10, tzinfo=UTC)),
        ("ours", "other"),
    )
    connection.execute(
        table.insert(),
        [
            {
                "id": i,
                "campaign_id": campaign,
                "owner_kind": kind,
                "player_character_id": pc,
                "hidden_from_party": hidden,
                "deleted_at": deleted,
            }
            for i, (kind, pc, hidden, deleted, campaign) in enumerate(values, 1)
        ],
    )
    rows = [
        SimpleNamespace(**row._mapping) for row in connection.execute(select(table))
    ]
    for viewer in ("dm", OWNER, OTHER, None):
        expected = {
            row.id
            for row in rows
            if row.campaign_id == "ours"
            and row.deleted_at is None
            and viewer is not None
            and (
                row.owner_kind == "party"
                and row.player_character_id is None
                and (viewer == "dm" or not row.hidden_from_party)
                or row.owner_kind == "character"
                and row.player_character_id is not None
                and (viewer == "dm" or viewer == row.player_character_id)
            )
        }
        assert {
            row.id
            for row in rows
            if row.campaign_id == "ours" and can_see_item(viewer, row)
        } == expected
        for source in (table, table.alias("inventory")):
            actual = set(
                connection.scalars(
                    select(source.c.id).where(
                        source.c.campaign_id == "ours",
                        inventory_predicate(source, viewer),
                    )
                )
            )
            assert actual == expected, (viewer, actual ^ expected)
