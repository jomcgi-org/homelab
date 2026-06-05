"""Hermetic tests for ``iceberg_batch`` (no Temporal test server, no network).

Strategy (per the WF-STORAGE spec): exercise the ``drain_and_commit`` activity
**function** directly with mocked NatsClient / Iceberg catalog, asserting the
grouping-by-table + ack-after-commit logic; and assert the workflow class is a
``@workflow.defn`` exported in ``WORKFLOWS``. ``temporalio.testing`` is avoided
because it downloads a server binary.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import temporalio.workflow

from projects.lakehouse.events.envelope import build_envelope
from projects.lakehouse.orchestrator.workflows import iceberg_batch as mod


def _msg(envelope) -> MagicMock:
    """A fake NATS Msg whose ``.data`` is the envelope JSON and ``.ack`` is async."""
    m = MagicMock()
    m.data = envelope.model_dump_json().encode("utf-8")
    m.ack = AsyncMock()
    return m


def _note_envelope(entity_id: str = "n1", version: int = 1):
    return build_envelope(
        entity_type="note",
        entity_id=entity_id,
        event_type="created",
        event_version=version,
        producer="test",
        payload={"note_id": entity_id, "title": "t", "embedding": [0.1, 0.2]},
    )


def _gap_envelope(entity_id: str = "g1", version: int = 1):
    return build_envelope(
        entity_type="gap",
        entity_id=entity_id,
        event_type="created",
        event_version=version,
        producer="test",
        payload={"topic": "x", "gap_class": "external", "state": "open"},
    )


# --------------------------------------------------------------------------- #
# Workflow definition / registration
# --------------------------------------------------------------------------- #


def test_workflow_is_defn_and_exported() -> None:
    assert mod.IcebergBatchCommitWorkflow in mod.WORKFLOWS
    # @workflow.defn registers a definition retrievable from the class.
    defn = temporalio.workflow._Definition.from_class(mod.IcebergBatchCommitWorkflow)
    assert defn is not None


def test_activity_exported() -> None:
    assert mod.drain_and_commit in mod.ACTIVITIES
    # @activity.defn sets the activity-definition dunder.
    assert hasattr(mod.drain_and_commit, "__temporal_activity_definition")


# --------------------------------------------------------------------------- #
# _envelope_to_row + pure mapping
# --------------------------------------------------------------------------- #


def test_table_by_entity_matches_publish_subjects() -> None:
    # Every entity_type with an Iceberg table must be a known publish subject.
    from projects.lakehouse.events.publish import SUBJECT_BY_ENTITY

    for entity_type in mod.TABLE_BY_ENTITY:
        assert entity_type in SUBJECT_BY_ENTITY


def test_envelope_to_rows_spreads_payload_and_envelope() -> None:
    env = _note_envelope()
    rows = mod._envelope_to_rows(json.loads(env.model_dump_json()))
    # A chunkless payload yields exactly one row.
    assert len(rows) == 1
    row = rows[0]
    # Envelope columns present.
    assert row["entity_type"] == "note"
    assert row["entity_id"] == "n1"
    assert row["event_version"] == 1
    # Payload columns spread in.
    assert row["note_id"] == "n1"
    assert row["title"] == "t"
    assert row["embedding"] == [0.1, 0.2]
    # occurred_at is parsed from the ISO string to a datetime so pyarrow's
    # timestamptz column accepts it (from_pylist won't parse ISO strings).
    assert isinstance(row["occurred_at"], datetime)


def test_envelope_to_rows_explodes_chunks() -> None:
    """A NoteCreated payload carries chunks as a nested list; the flat note_events
    schema is per-chunk, so each chunk becomes its own row with the note-level
    columns repeated and the chunk's fields lifted to the flat columns."""
    env = build_envelope(
        entity_type="note",
        entity_id="n2",
        event_type="created",
        event_version=3,
        producer="test",
        payload={
            "note_id": "n2",
            "path": "_processed/x.md",
            "title": "X",
            "chunks": [
                {
                    "chunk_index": 0,
                    "section_header": "Intro",
                    "chunk_text": "hello",
                    "embedding": [0.1, 0.2],
                },
                {
                    "chunk_index": 1,
                    "section_header": "Body",
                    "chunk_text": "world",
                    "embedding": [0.3, 0.4],
                },
            ],
        },
    )
    rows = mod._envelope_to_rows(json.loads(env.model_dump_json()))
    assert len(rows) == 2
    # Note-level columns repeated; the nested chunks list is NOT a column.
    assert all(r["note_id"] == "n2" and r["path"] == "_processed/x.md" for r in rows)
    assert all("chunks" not in r for r in rows)
    # Each chunk's fields are lifted to the flat per-chunk columns.
    assert [r["chunk_index"] for r in rows] == [0, 1]
    assert [r["embedding"] for r in rows] == [[0.1, 0.2], [0.3, 0.4]]
    assert [r["chunk_text"] for r in rows] == ["hello", "world"]


# --------------------------------------------------------------------------- #
# drain_and_commit activity — grouping + ack-after-commit
# --------------------------------------------------------------------------- #


@pytest.fixture
def patched_deps():
    """Patch NatsClient + iceberg catalog/writer for the activity under test."""
    nats_client = AsyncMock()
    sub = AsyncMock()
    nats_client.pull_subscribe.return_value = sub

    catalog = MagicMock()
    note_table = MagicMock()
    gap_table = MagicMock()

    def load_table(identifier):
        # identifier is (namespace, table_name)
        return note_table if identifier[1] == "note_events" else gap_table

    catalog.load_table.side_effect = load_table

    append_calls: list = []

    def append_events(table, rows):
        append_calls.append((table, list(rows)))

    with (
        patch(
            "projects.lakehouse.nats_client.client.NatsClient",
            return_value=nats_client,
        ),
        patch(
            "projects.lakehouse.iceberg.catalog.load_warehouse_catalog",
            return_value=catalog,
        ),
        patch(
            "projects.lakehouse.iceberg.writer.append_events",
            side_effect=append_events,
        ),
    ):
        yield {
            "nats_client": nats_client,
            "sub": sub,
            "catalog": catalog,
            "note_table": note_table,
            "gap_table": gap_table,
            "append_calls": append_calls,
        }


def test_drain_groups_by_table_and_acks_after_commit(patched_deps) -> None:
    note_msg = _msg(_note_envelope("n1", 1))
    gap_msg = _msg(_gap_envelope("g1", 1))
    note_msg2 = _msg(_note_envelope("n2", 1))
    patched_deps["sub"].fetch.return_value = [note_msg, gap_msg, note_msg2]

    result = asyncio.run(mod.drain_and_commit())

    # Two note rows -> note_events; one gap row -> gap_events.
    assert result.committed_by_table == {"note_events": 2, "gap_events": 1}
    assert result.acked == 3
    assert result.skipped_unmapped == 0
    assert result.empty is False

    # Every message acked (after its table committed).
    note_msg.ack.assert_awaited_once()
    note_msg2.ack.assert_awaited_once()
    gap_msg.ack.assert_awaited_once()

    # Connection opened + closed.
    patched_deps["nats_client"].connect.assert_awaited_once()
    patched_deps["nats_client"].close.assert_awaited_once()


def test_drain_skips_unmapped_entity_and_does_not_ack_it(patched_deps) -> None:
    unknown = build_envelope(
        entity_type="edge",  # has a publish subject but NO Iceberg table here
        entity_id="e1",
        event_type="created",
        event_version=1,
        producer="test",
        payload={},
    )
    edge_msg = _msg(unknown)
    note_msg = _msg(_note_envelope("n1", 1))
    patched_deps["sub"].fetch.return_value = [edge_msg, note_msg]

    result = asyncio.run(mod.drain_and_commit())

    assert result.skipped_unmapped == 1
    assert result.committed_by_table == {"note_events": 1}
    # The unmapped edge message is NOT acked (so it isn't silently lost).
    edge_msg.ack.assert_not_awaited()
    note_msg.ack.assert_awaited_once()


def test_drain_empty_fetch_returns_empty(patched_deps) -> None:
    patched_deps["sub"].fetch.return_value = []
    result = asyncio.run(mod.drain_and_commit())
    assert result.empty is True


# --------------------------------------------------------------------------- #
# _load_or_create_table — ensure-create on first use
# --------------------------------------------------------------------------- #


def test_load_or_create_table_uses_existing_when_present() -> None:
    """When the table exists, load it directly — no create calls."""
    catalog = MagicMock()
    existing = MagicMock()
    catalog.load_table.return_value = existing

    result = mod._load_or_create_table(catalog, "knowledge", "note_events")

    assert result is existing
    catalog.create_namespace_if_not_exists.assert_not_called()
    catalog.create_table.assert_not_called()


def test_load_or_create_table_creates_when_missing() -> None:
    """On NoSuchTableError, ensure the namespace then create the table from its
    registered schema (TABLES) — the writer's bootstrap-on-first-drain path."""
    from pyiceberg.exceptions import NoSuchTableError

    from projects.lakehouse.iceberg.tables import TABLES

    catalog = MagicMock()
    catalog.load_table.side_effect = NoSuchTableError("nope")
    created = MagicMock()
    catalog.create_table.return_value = created

    result = mod._load_or_create_table(catalog, "knowledge", "note_events")

    assert result is created
    catalog.create_namespace_if_not_exists.assert_called_once_with(("knowledge",))
    catalog.create_table.assert_called_once()
    _, kwargs = catalog.create_table.call_args
    # Created with the registered note_events schema, not an ad-hoc one.
    assert kwargs["schema"] is TABLES["note_events"]


def test_drain_timeout_returns_empty(patched_deps) -> None:
    import nats.errors

    patched_deps["sub"].fetch.side_effect = nats.errors.TimeoutError()
    result = asyncio.run(mod.drain_and_commit())
    assert result.empty is True
    # Still closes the connection on the idle path.
    patched_deps["nats_client"].close.assert_awaited_once()


def test_drain_does_not_ack_when_commit_fails(patched_deps) -> None:
    # If the Iceberg append raises, the messages for that table must NOT be acked.
    note_msg = _msg(_note_envelope("n1", 1))
    patched_deps["sub"].fetch.return_value = [note_msg]

    with patch(
        "projects.lakehouse.iceberg.writer.append_events",
        side_effect=RuntimeError("commit boom"),
    ):
        with pytest.raises(RuntimeError, match="commit boom"):
            asyncio.run(mod.drain_and_commit())

    note_msg.ack.assert_not_awaited()
    # Connection still closed (finally block) so the consumer isn't leaked.
    patched_deps["nats_client"].close.assert_awaited_once()
