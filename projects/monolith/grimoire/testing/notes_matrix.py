"""Seeded notes agreement matrix shared by SQLite and real PostgreSQL."""

import random
from datetime import datetime, timezone
from itertools import product
from types import SimpleNamespace

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

from grimoire.audience import AUTHOR_MEMBER_ID_TYPE, can_see_note, note_predicate
from grimoire.testing.audience_matrix import MEMBERS, viewer_for


def notes_table(name):
    # No kind CHECK: exercise corrupt and future kinds failing closed.
    return Table(
        name,
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("campaign_id", String, nullable=False),
        Column("kind", String, nullable=False),
        Column("author_member_id", AUTHOR_MEMBER_ID_TYPE),
        Column("dm_readable", Boolean, nullable=False),
        Column("deleted_at", DateTime),
    )


def assert_note_agreement(connection, table):
    values = list(
        product(
            ("character", "party", "unknown"),
            (None, *(member.id for member in MEMBERS)),
            (False, True),
            (None, datetime(2026, 10, 2, tzinfo=timezone.utc)),
            ("ours", "other"),
        )
    )
    random.Random(6616).shuffle(values)
    connection.execute(
        table.insert(),
        [
            dict(
                id=i,
                kind=kind,
                author_member_id=author,
                dm_readable=readable,
                deleted_at=deleted,
                campaign_id=campaign,
            )
            for i, (kind, author, readable, deleted, campaign) in enumerate(values, 1)
        ],
    )
    rows = [
        SimpleNamespace(**row._mapping) for row in connection.execute(select(table))
    ]
    for member in MEMBERS:
        viewer = viewer_for(member)
        expected = {
            row.id
            for row in rows
            if row.campaign_id == "ours"
            and row.deleted_at is None
            and (
                row.kind == "party"
                and viewer is not None
                or row.kind == "character"
                and (
                    member.role == "dm"
                    and row.dm_readable
                    or member.role != "dm"
                    and row.author_member_id == member.id
                )
            )
        }
        python_ids = {
            row.id
            for row in rows
            if row.campaign_id == "ours" and can_see_note(viewer, member, row)
        }
        assert expected and len(expected) < len(rows)
        assert python_ids == expected
        for source in (table, table.alias("notes")):
            sql_ids = set(
                connection.scalars(
                    select(source.c.id).where(
                        source.c.campaign_id == "ours",
                        note_predicate(source, viewer, member),
                    )
                )
            )
            assert sql_ids == expected, (viewer, sql_ids ^ expected)
