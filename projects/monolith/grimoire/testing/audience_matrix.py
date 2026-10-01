"""Shared generated-row agreement check for SQLite and real PostgreSQL."""

import random
from itertools import combinations, product
from types import SimpleNamespace

from sqlalchemy import Column, Integer, MetaData, Table, func, select

from grimoire.audience import (
    AUDIENCE_PC_IDS_TYPE,
    AUDIENCE_TYPE,
    AUTHOR_MEMBER_ID_TYPE,
    audience_predicate,
    can_see,
)

PC_IDS = ("a", "aa", "xay", 'a"quote', "a\\slash", "b")
MEMBERS = (
    SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001", role="dm", player_character_id=None
    ),
    SimpleNamespace(
        id="00000000-0000-0000-0000-000000000002",
        role="player",
        player_character_id="a",
    ),
    SimpleNamespace(
        id="00000000-0000-0000-0000-000000000003",
        role="player",
        player_character_id="b",
    ),
    SimpleNamespace(
        id="00000000-0000-0000-0000-000000000004",
        role="player",
        player_character_id=None,
    ),
    # Also query as the escaped and overlapping ids, not just store them.
    *(
        SimpleNamespace(
            id=f"00000000-0000-0000-0000-{index:012d}",
            role="player",
            player_character_id=pc_id,
        )
        for index, pc_id in enumerate(PC_IDS[1:5], 5)
    ),
)


def viewer_for(member):
    return "dm" if member.role == "dm" else member.player_character_id


def audience_table(name):
    """Own MetaData: never register a play model in SQLModel.metadata."""
    return Table(
        name,
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("audience", AUDIENCE_TYPE, nullable=False),
        Column("audience_pc_ids", AUDIENCE_PC_IDS_TYPE, nullable=False),
        Column("author_member_id", AUTHOR_MEMBER_ID_TYPE, nullable=True),
    )


def generated_rows():
    kinds = ("table", "dm", "pcs", "unknown")
    subsets = [list(ids) for size in range(7) for ids in combinations(PC_IDS, size)]
    authors = [None, *(member.id for member in MEMBERS)]
    values = list(product(kinds, subsets, authors))
    rng = random.Random(6607)
    values.extend(
        (rng.choice(kinds), rng.sample(PC_IDS, rng.randrange(7)), rng.choice(authors))
        for _ in range(256)
    )
    return [
        {
            "id": index,
            "audience": kind,
            "audience_pc_ids": ids,
            "author_member_id": author,
        }
        for index, (kind, ids, author) in enumerate(values, 1)
    ]


def assert_agreement(connection, table):
    rows = generated_rows()
    assert len(rows) == 2560
    connection.execute(table.insert(), rows)
    assert connection.scalar(select(func.count()).select_from(table)) == 2560
    stored = [
        SimpleNamespace(**row._mapping) for row in connection.execute(select(table))
    ]
    assert len(stored) == 2560
    for member in MEMBERS:
        viewer = viewer_for(member)
        python_ids = {row.id for row in stored if can_see(viewer, member, row)}
        sql_ids = set(
            connection.scalars(
                select(table.c.id).where(audience_predicate(table, viewer, member))
            )
        )
        assert sql_ids == python_ids, (viewer, sql_ids ^ python_ids)
        # A second oracle pins the policy even if both implementations regress.
        expected = {
            row.id
            for row in stored
            if member.role == "dm"
            or (
                row.audience in ("table", "dm", "pcs")
                and (
                    row.audience == "table"
                    or (
                        viewer is not None
                        and (
                            row.audience == "pcs"
                            and viewer in row.audience_pc_ids
                            or row.author_member_id == member.id
                        )
                    )
                )
            )
        }
        assert python_ids == expected
        assert python_ids
        if viewer == "dm":
            assert len(python_ids) == 2560  # A DM has no invisible rows.
        else:
            assert len(python_ids) < 2560
            # Pin witnesses for each rule, including authored unknown rows.
            for kind, ids, author, visible in (
                ("table", [], None, True),
                ("dm", [], None, False),
                ("dm", [], member.id, viewer is not None),
                ("pcs", [], member.id, viewer is not None),
                ("pcs", [viewer] if viewer else ["a"], None, viewer is not None),
                ("unknown", [], member.id, False),
            ):
                witnesses = [
                    row
                    for row in stored
                    if (row.audience, row.audience_pc_ids, row.author_member_id)
                    == (kind, ids, author)
                ]
                assert witnesses, (viewer, kind, ids, author)
                assert all((row.id in python_ids) == visible for row in witnesses)
        # Check an alias, a model-like column namespace, composition, and binds
        # reused through SQLAlchemy's statement cache for different viewers.
        alias = table.alias("play_row")
        alias_ids = set(
            connection.scalars(
                select(alias.c.id).where(audience_predicate(alias, viewer, member))
            )
        )
        assert alias_ids == expected
        column_model = SimpleNamespace(**dict(table.c.items()))
        bounded = set(
            connection.scalars(
                select(table.c.id).where(
                    table.c.id <= 10, audience_predicate(column_model, viewer, member)
                )
            )
        )
        assert bounded == expected.intersection(range(1, 11))
