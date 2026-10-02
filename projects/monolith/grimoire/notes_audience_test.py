"""Notes opt-in DM visibility is separate from generic play-row audiences."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from grimoire.audience import can_see_note, note_predicate
from grimoire.testing.audience_matrix import MEMBERS
from grimoire.testing.notes_matrix import assert_note_agreement, notes_table


def test_sqlite_notes_agreement(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'matrix.db'}")
    table = notes_table("notes_matrix")
    try:
        table.create(engine)
        with engine.begin() as connection:
            assert_note_agreement(connection, table)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "viewer,member",
    [
        ("dm", None),
        (None, None),
        ("dm", MEMBERS[1]),
        ("wrong-pc", MEMBERS[1]),
        (None, SimpleNamespace(id=None, role="player", player_character_id=None)),
        (None, SimpleNamespace(id="unknown", role="unknown", player_character_id=None)),
    ],
)
def test_invalid_notes_viewer_raises(viewer, member):
    table = notes_table("invalid")
    with pytest.raises(ValueError):
        note_predicate(table, viewer, member)
    with pytest.raises(ValueError):
        can_see_note(viewer, member, SimpleNamespace())
