"""Literal policy cases plus exhaustive and seeded SQL/Python agreement."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql, sqlite

from grimoire.audience import (
    AUDIENCE_PC_IDS_TYPE,
    AUDIENCE_TYPE,
    AUTHOR_MEMBER_ID_TYPE,
    Audience,
    audience_predicate,
    can_see,
)
from grimoire.testing.audience_matrix import (
    MEMBERS,
    assert_agreement,
    audience_table,
    generated_rows,
    viewer_for,
)


@pytest.mark.parametrize(
    "member_index,kind,ids,author_index,expected",
    [
        (0, "table", [], None, True),
        (0, "dm", [], 2, True),
        (0, "pcs", ["b"], 2, True),
        (0, "unknown", [], None, True),
        (1, "table", [], None, True),
        (1, "pcs", ["a"], None, True),
        (1, "pcs", ["a", "b"], 2, True),
        (1, "dm", [], 1, True),
        (1, "pcs", ["b"], 1, True),
        (1, "dm", [], None, False),
        (1, "dm", [], 2, False),
        (1, "pcs", ["b"], 2, False),
        (1, "pcs", ["aa", "xay"], None, False),
        (1, "unknown", ["a"], 1, False),
        (2, "pcs", ["a"], 1, False),
        (3, "table", [], 3, True),
        (3, "table", [], None, True),
        (3, "dm", [], 3, False),
        (3, "pcs", ["a", "b"], 3, False),
        (3, "unknown", [], 3, False),
        (6, "pcs", ['a"quote'], None, True),
        (7, "pcs", ["a\\slash"], None, True),
    ],
)
def test_can_see_rules(member_index, kind, ids, author_index, expected):
    member = MEMBERS[member_index]
    row = SimpleNamespace(
        audience=kind,
        audience_pc_ids=ids,
        author_member_id=MEMBERS[author_index].id if author_index is not None else None,
    )
    assert can_see(viewer_for(member), member, row) is expected


@pytest.mark.parametrize(
    "viewer,member",
    [
        (None, None),
        ("dm", None),
        ("a", None),
        ("dm", MEMBERS[1]),
        ("b", MEMBERS[1]),
        (None, MEMBERS[1]),
        ("a", MEMBERS[0]),
        (None, MEMBERS[0]),
        ("a", MEMBERS[3]),
        ("dm", SimpleNamespace(id="member", role="player", player_character_id="dm")),
        (None, SimpleNamespace(id="member", role="outsider", player_character_id=None)),
        (None, SimpleNamespace(id=None, role="player", player_character_id=None)),
    ],
)
def test_invalid_viewers_raise_in_both_paths(viewer, member):
    table = audience_table("invalid_pair")
    row = SimpleNamespace(audience="table", audience_pc_ids=[], author_member_id=None)
    with pytest.raises(ValueError):
        audience_predicate(table, viewer, member)
    with pytest.raises(ValueError):
        can_see(viewer, member, row)


@pytest.mark.parametrize(
    "kind,ids",
    [
        ("pcs", []),
        ("table", ["a"]),
        ("dm", ["b"]),
        ("unknown", []),
        ("pcs", [""]),
        ("pcs", [42]),
    ],
)
def test_invalid_audiences_raise(kind, ids):
    with pytest.raises(ValueError):
        Audience(kind, ids)
    with pytest.raises(ValueError):
        Audience.from_columns(kind, ids)


def test_audience_immutable_and_round_trip():
    author = MEMBERS[1].id
    value = Audience.from_columns("pcs", ["b", "a", "b"], author)
    assert value.pc_ids == frozenset({"a", "b"})
    assert isinstance(value.pc_ids, frozenset)
    assert value.to_columns() == {
        "audience": "pcs",
        "audience_pc_ids": ["a", "b"],
        "author_member_id": author,
    }
    assert Audience.from_columns(**value.to_columns()) == value
    assert hash(value) == hash(Audience("pcs", frozenset({"b", "a"}), author))
    with pytest.raises(FrozenInstanceError):
        value.kind = "dm"
    # Neither the input list nor returned columns can mutate the value.
    ids = ["a"]
    copied = Audience("pcs", ids)
    ids.append("b")
    copied.to_columns()["audience_pc_ids"].append("b")
    assert copied.pc_ids == frozenset({"a"})
    for kind in ("table", "dm"):
        for provenance in (None, author):
            columns = {
                "audience": kind,
                "audience_pc_ids": [],
                "author_member_id": provenance,
            }
            assert Audience.from_columns(**columns).to_columns() == columns


def test_storage_types_pin_both_dialects():
    pg, sq = postgresql.dialect(), sqlite.dialect()
    assert AUDIENCE_TYPE.compile(dialect=pg) == "VARCHAR"
    assert AUDIENCE_TYPE.compile(dialect=sq) == "VARCHAR"
    assert AUDIENCE_PC_IDS_TYPE.compile(dialect=pg) == "JSONB"
    assert AUDIENCE_PC_IDS_TYPE.compile(dialect=sq) == "JSON"
    assert AUTHOR_MEMBER_ID_TYPE.compile(dialect=pg) == "UUID"
    assert AUTHOR_MEMBER_ID_TYPE.compile(dialect=sq) == "VARCHAR(36)"


def test_generated_matrix_is_pinned():
    rows = generated_rows()
    assert len(rows) == 2560
    assert rows == generated_rows()
    assert rows[2304] == {
        "id": 2305,
        "audience": "dm",
        "audience_pc_ids": [],
        "author_member_id": MEMBERS[4].id,
    }


def test_sqlite_audience_agreement(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audience.db'}")
    table = audience_table("audience_matrix")
    try:
        table.create(engine)
        with engine.begin() as connection:
            assert_agreement(connection, table)
    finally:
        table.drop(engine, checkfirst=True)
        engine.dispose()
