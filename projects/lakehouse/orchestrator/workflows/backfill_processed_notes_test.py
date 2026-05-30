"""Hermetic tests for the processed-notes backfill workflow.

No Temporal test server, no DB, no network: psycopg's ``connect`` is patched to
a fake connection that returns canned ``_processed`` rows (including a 1024-float
embedding), and the NATS client is a fake that captures publishes. Coroutines
are driven with ``asyncio.run`` so the suite needs no pytest-asyncio plugin.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
import temporalio.workflow

import projects.lakehouse.orchestrator.workflows.backfill_processed_notes as bp
from projects.lakehouse.orchestrator.workflows.backfill_processed_notes import (
    ACTIVITIES,
    BACKFILL_EVENT_VERSION,
    PRODUCER,
    WORKFLOWS,
    BackfillFromProcessedNotesWorkflow,
    publish_note_events,
    read_processed_notes_batch,
)

# A full-length voyage-4-nano vector so the test exercises the real dimension.
EMBEDDING_1024 = [0.001 * i for i in range(1024)]


# --- fakes ----------------------------------------------------------------


class FakeCursor:
    """Returns canned result sets keyed by which query is executed.

    The activity issues two queries: a notes page, then a chunks fetch. We route
    on a substring of the SQL so the fake stays insensitive to whitespace.
    """

    def __init__(self, note_rows, chunk_rows):
        self._note_rows = note_rows
        self._chunk_rows = chunk_rows
        self._pending: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "FROM knowledge.notes" in sql:
            self._pending = list(self._note_rows)
        elif "FROM knowledge.chunks" in sql:
            self._pending = list(self._chunk_rows)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected query: {sql}")

    def fetchall(self):
        return self._pending


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor


@contextmanager
def patched_psycopg(note_rows, chunk_rows):
    """Patch ``psycopg.connect`` (imported inside the activity) + DATABASE_URL."""
    import psycopg

    fake = FakeConn(FakeCursor(note_rows, chunk_rows))
    with (
        patch.object(psycopg, "connect", return_value=fake) as connect,
        patch.dict(
            bp.os.environ,
            {"DATABASE_URL": "postgresql://app:app@localhost:5432/monolith"},
        ),
    ):
        yield connect


class FakeNatsClient:
    """Captures publish calls; satisfies the NatsClient surface the activity uses."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def publish(self, subject, payload, *, msg_id=None, headers=None) -> None:
        self.calls.append(
            {
                "subject": subject,
                "payload": payload,
                "msg_id": msg_id,
                "headers": headers,
            }
        )

    async def close(self) -> None:
        self.closed = True


# --- read_processed_notes_batch ------------------------------------------


def _note_row(pk, note_id):
    # (id, note_id, path, title, content_hash, type, status, visibility, tags, aliases)
    return (
        pk,
        note_id,
        f"_processed/{note_id}.md",
        f"Title {note_id}",
        f"hash-{pk}",
        "atom",
        "active",
        "public",
        ["tag-a", "tag-b"],
        [f"alias-{note_id}"],
    )


def test_read_batch_builds_note_dicts_with_chunks_and_embeddings():
    note_rows = [_note_row(1, "wong-zakai-theorem"), _note_row(2, "standby-crutch")]
    chunk_rows = [
        # (note_fk, chunk_index, section_header, chunk_text, embedding)
        (1, 0, "Intro", "first chunk", EMBEDDING_1024),
        (1, 1, None, "second chunk", EMBEDDING_1024),
        (2, 0, "Body", "note two chunk", EMBEDDING_1024),
    ]
    with patched_psycopg(note_rows, chunk_rows):
        batch = asyncio.run(read_processed_notes_batch(0, 50))

    assert [n["note_id"] for n in batch] == ["wong-zakai-theorem", "standby-crutch"]
    first = batch[0]
    assert first["path"] == "_processed/wong-zakai-theorem.md"
    assert first["title"] == "Title wong-zakai-theorem"
    assert first["content_hash"] == "hash-1"
    assert first["type"] == "atom"
    assert first["status"] == "active"
    assert first["visibility"] == "public"
    assert first["tags"] == ["tag-a", "tag-b"]
    assert first["aliases"] == ["alias-wong-zakai-theorem"]
    # Two chunks for note 1, ordered, with full 1024-dim embeddings preserved.
    assert len(first["chunks"]) == 2
    assert first["chunks"][0]["chunk_index"] == 0
    assert first["chunks"][0]["section_header"] == "Intro"
    assert first["chunks"][1]["section_header"] is None
    assert len(first["chunks"][0]["embedding"]) == 1024
    assert first["chunks"][0]["embedding"][1] == pytest.approx(0.001)
    # Note 2 got its single chunk; chunks aren't cross-contaminated.
    assert len(batch[1]["chunks"]) == 1
    assert batch[1]["chunks"][0]["chunk_text"] == "note two chunk"


def test_read_batch_handles_note_with_no_chunks_and_null_visibility():
    # NULL visibility normalizes to "" (NoteCreatedPayload wants a str).
    row = list(_note_row(7, "no-chunks"))
    row[7] = None  # visibility
    note_rows = [tuple(row)]
    with patched_psycopg(note_rows, chunk_rows=[]):
        batch = asyncio.run(read_processed_notes_batch(0, 50))
    assert batch[0]["visibility"] == ""
    assert batch[0]["chunks"] == []


def test_read_batch_empty_page_returns_empty_list():
    with patched_psycopg(note_rows=[], chunk_rows=[]):
        batch = asyncio.run(read_processed_notes_batch(1000, 50))
    assert batch == []


def test_read_batch_requires_database_url():
    import psycopg

    with (
        patch.object(psycopg, "connect"),
        patch.dict(bp.os.environ, {}, clear=True),
        pytest.raises(RuntimeError, match="DATABASE_URL"),
    ):
        asyncio.run(read_processed_notes_batch(0, 50))


def test_resolve_database_url_normalizes_sqlalchemy_dialect():
    with patch.dict(
        bp.os.environ,
        {"DATABASE_URL": "postgresql+psycopg://app:app@host:5432/monolith"},
    ):
        assert bp._resolve_database_url() == "postgresql://app:app@host:5432/monolith"


# --- publish_note_events --------------------------------------------------


def _note_dict(note_id, *, chunks=None):
    return {
        "note_id": note_id,
        "path": f"_processed/{note_id}.md",
        "title": f"Title {note_id}",
        "content_hash": f"hash-{note_id}",
        "type": "atom",
        "status": "active",
        "visibility": "public",
        "tags": ["t"],
        "aliases": [],
        "chunks": chunks
        if chunks is not None
        else [
            {
                "chunk_index": 0,
                "section_header": "S",
                "chunk_text": "body",
                "embedding": EMBEDDING_1024,
            }
        ],
    }


def _run_publish(batch):
    fake = FakeNatsClient()
    with patch.object(bp, "NatsClient", return_value=fake):
        count = asyncio.run(publish_note_events(batch))
    return fake, count


def test_publish_builds_envelope_and_msg_id():
    fake, count = _run_publish([_note_dict("wong-zakai-theorem")])

    assert count == 1
    assert fake.connected and fake.closed
    call = fake.calls[0]
    assert call["subject"] == "events.knowledge.note"
    # Idempotent dedup key: {note_id}-v1.
    assert call["msg_id"] == "wong-zakai-theorem-v1"
    assert call["headers"] == {"Nats-Msg-Id": "wong-zakai-theorem-v1"}

    decoded = json.loads(call["payload"].decode("utf-8"))
    assert decoded["entity_type"] == "note"
    assert decoded["entity_id"] == "wong-zakai-theorem"
    assert decoded["event_type"] == "created"
    assert decoded["event_version"] == BACKFILL_EVENT_VERSION == 1
    assert decoded["producer"] == PRODUCER == "lakehouse.backfill"
    # Embeddings travel in the payload (so serving rebuilds never re-embed).
    payload = decoded["payload"]
    assert payload["note_id"] == "wong-zakai-theorem"
    assert len(payload["chunks"]) == 1
    assert len(payload["chunks"][0]["embedding"]) == 1024


def test_publish_is_idempotent_msg_id_stable_across_reruns():
    # Same note published twice must yield the SAME Nats-Msg-Id -> JetStream
    # dedups the re-run (ADR 017 idempotency layer 1).
    note = _note_dict("standby-crutch")
    fake1, _ = _run_publish([note])
    fake2, _ = _run_publish([note])
    assert fake1.calls[0]["msg_id"] == fake2.calls[0]["msg_id"] == "standby-crutch-v1"


def test_publish_multiple_notes_each_get_their_own_msg_id():
    fake, count = _run_publish([_note_dict("a"), _note_dict("b"), _note_dict("c")])
    assert count == 3
    assert [c["msg_id"] for c in fake.calls] == ["a-v1", "b-v1", "c-v1"]


def test_publish_empty_batch_is_noop_no_connect():
    fake = FakeNatsClient()
    with patch.object(bp, "NatsClient", return_value=fake):
        count = asyncio.run(publish_note_events([]))
    assert count == 0
    assert not fake.connected
    assert fake.calls == []


def test_publish_closes_client_on_error():
    fake = FakeNatsClient()

    async def boom(*a, **k):
        raise RuntimeError("publish failed")

    fake.publish = boom  # type: ignore[method-assign]
    with patch.object(bp, "NatsClient", return_value=fake):
        with pytest.raises(RuntimeError, match="publish failed"):
            asyncio.run(publish_note_events([_note_dict("x")]))
    # finally: close() runs even on failure.
    assert fake.closed


# --- workflow registration ------------------------------------------------


def test_workflow_is_defn_and_exported():
    # @workflow.defn marks the class; the loader collects it from WORKFLOWS.
    assert (
        temporalio.workflow._Definition.from_class(BackfillFromProcessedNotesWorkflow)
        is not None
    )
    assert WORKFLOWS == [BackfillFromProcessedNotesWorkflow]


def test_activities_exported():
    assert read_processed_notes_batch in ACTIVITIES
    assert publish_note_events in ACTIVITIES
    # Activities are decorated with @activity.defn.
    import temporalio.activity

    assert temporalio.activity._Definition.from_callable(read_processed_notes_batch)
    assert temporalio.activity._Definition.from_callable(publish_note_events)
