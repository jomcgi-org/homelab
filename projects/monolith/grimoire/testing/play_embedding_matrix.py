"""Run every audience and note matrix case against copied embedding columns."""

from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, select

from grimoire.audience import (
    AUDIENCE_PC_IDS_TYPE,
    AUTHOR_MEMBER_ID_TYPE,
    audience_predicate,
    note_predicate,
)
from grimoire.testing.audience_matrix import (
    MEMBERS,
    audience_table,
    generated_rows,
    viewer_for,
)
from grimoire.testing.notes_matrix import assert_note_agreement, notes_table


def matrix_tables(prefix):
    # No model constraints: include unknown kinds and corrupt corpus copies.
    embeddings = Table(
        prefix + "_embeddings",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("source_id", Integer),
        Column("embeddable_kind", String),
        Column("campaign_id", String),
        Column("audience", String),
        Column("audience_pc_ids", AUDIENCE_PC_IDS_TYPE),
        Column("author_member_id", AUTHOR_MEMBER_ID_TYPE),
        Column("dm_readable", Boolean),
    )
    return (
        embeddings,
        audience_table(prefix + "_events"),
        notes_table(prefix + "_notes"),
    )


def assert_play_agreement(connection, embeddings, events, notes, predicate):
    rows = generated_rows()
    connection.execute(events.insert(), rows)
    # This oracle seeds all kind/author/readable/deletion/campaign combinations.
    assert_note_agreement(connection, notes)
    note_rows = connection.execute(select(notes)).mappings().all()
    copies = []
    for kind in ("event", "transcript"):
        for campaign in ("ours", "other"):
            copies.extend(
                {
                    "source_id": row["id"],
                    "embeddable_kind": kind,
                    "campaign_id": campaign,
                    "audience": row["audience"],
                    "audience_pc_ids": row["audience_pc_ids"],
                    "author_member_id": row["author_member_id"],
                    "dm_readable": None,
                }
                for row in rows
            )
    # Deletion removes vectors transactionally. Stale deleted vectors are tested
    # separately by the read-time leak harness, since no deleted_at is copied.
    copies.extend(
        {
            "source_id": row["id"],
            "embeddable_kind": "note",
            "campaign_id": row["campaign_id"],
            "audience": row["kind"],
            "audience_pc_ids": [],
            "author_member_id": row["author_member_id"],
            "dm_readable": row["dm_readable"],
        }
        for row in note_rows
        if row["deleted_at"] is None
    )
    copies.extend(
        {
            "source_id": row["id"],
            "embeddable_kind": "fact",
            "campaign_id": campaign,
            "audience": row["audience"],
            "audience_pc_ids": row["audience_pc_ids"],
            "author_member_id": row["author_member_id"],
            "dm_readable": None,
        }
        for row in rows
        for campaign in ("ours", "other")
    )
    for kind in ("entity", "chunk", "unknown"):
        for campaign in (None, "ours", "other"):
            copies.append(
                {
                    "source_id": -1,
                    "embeddable_kind": kind,
                    "campaign_id": campaign,
                    "audience": None,
                    "audience_pc_ids": [],
                    "author_member_id": None,
                    "dm_readable": None,
                }
            )
    connection.execute(
        embeddings.insert(), [dict(id=i, **row) for i, row in enumerate(copies, 1)]
    )
    for member in MEMBERS:
        viewer = viewer_for(member)
        expected_events = set(
            connection.scalars(
                select(events.c.id).where(audience_predicate(events, viewer, member))
            )
        )
        expected_notes = set(
            connection.scalars(
                select(notes.c.id).where(
                    notes.c.campaign_id == "ours", note_predicate(notes, viewer, member)
                )
            )
        )
        selected = (
            connection.execute(
                select(embeddings).where(predicate("ours", viewer, member))
            )
            .mappings()
            .all()
        )
        for kind in ("event", "transcript"):
            assert {
                row["source_id"] for row in selected if row["embeddable_kind"] == kind
            } == expected_events
        expected_facts = {
            row["id"]
            for row in rows
            if viewer is not None
            and row["audience"] in ("table", "pcs")
            and row["author_member_id"] is None
            and row["id"] in expected_events
        }
        assert {
            row["source_id"] for row in selected if row["embeddable_kind"] == "fact"
        } == expected_facts
        assert {
            row["source_id"] for row in selected if row["embeddable_kind"] == "note"
        } == expected_notes
        assert all(
            row["campaign_id"] == "ours"
            for row in selected
            if row["embeddable_kind"] not in ("entity", "chunk")
        )
        assert {
            (row["embeddable_kind"], row["campaign_id"])
            for row in selected
            if row["source_id"] == -1
        } == {("entity", None), ("chunk", None)}
