"""Hermetic tests for the incremental note-export workflow.

No Temporal server, no DB, no network: psycopg's ``connect`` is patched to a fake
connection returning canned rows, the NATS client is a fake that captures
publishes, and coroutines run via ``asyncio.run`` (no pytest-asyncio plugin).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import temporalio.workflow

import projects.lakehouse.orchestrator.workflows.export_note_changes as enc
from projects.lakehouse.orchestrator.workflows.export_note_changes import (
    ACTIVITIES,
    PRODUCER,
    WORKFLOWS,
    ExportNoteChangesWorkflow,
    advance_export_watermark,
    publish_note_change_events,
    read_export_lower_bound,
    read_note_changes_batch,
)

EMBEDDING_1024 = [0.001 * i for i in range(1024)]
_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_T1 = datetime(2026, 6, 1, 12, 5, 0, tzinfo=timezone.utc)


# --- fakes ----------------------------------------------------------------


class FakeCursor:
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
    """Fake psycopg connection; accepts ``isolation_level`` assignment."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.isolation_level = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor


@contextmanager
def patched_psycopg(note_rows, chunk_rows):
    import psycopg

    fake = FakeConn(FakeCursor(note_rows, chunk_rows))
    with (
        patch.object(psycopg, "connect", return_value=fake),
        patch.dict(
            enc.os.environ,
            {"DATABASE_URL": "postgresql://app:app@localhost:5432/monolith"},
        ),
    ):
        yield fake


class FakeNatsClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def publish(self, subject, payload, *, msg_id=None, headers=None) -> None:
        self.calls.append({"subject": subject, "payload": payload, "msg_id": msg_id})

    async def close(self) -> None:
        self.closed = True


def _note_row(pk, note_id, indexed_at, deleted_at=None):
    # (id, note_id, path, title, content_hash, type, status, visibility,
    #  tags, aliases, indexed_at, deleted_at, change_at)
    change_at = max(indexed_at, deleted_at) if deleted_at else indexed_at
    return (
        pk,
        note_id,
        f"_processed/{note_id}.md",
        f"Title {note_id}",
        f"hash-{pk}",
        "atom",
        "active",
        "public",
        ["tag-a"],
        [f"alias-{note_id}"],
        indexed_at,
        deleted_at,
        change_at,
    )


def _chunk_row(note_fk, idx, embedding=None):
    return (note_fk, idx, f"§{idx}", f"chunk {idx}", embedding or EMBEDDING_1024)


# --- URL helpers ----------------------------------------------------------


def test_lakehouse_db_url_swaps_db_and_normalizes_dialect():
    with patch.dict(
        enc.os.environ,
        {"DATABASE_URL": "postgresql+psycopg://app:pw@host:5432/monolith"},
        clear=True,
    ):
        assert (
            enc._resolve_lakehouse_db_url() == "postgresql://app:pw@host:5432/lakehouse"
        )


def test_lakehouse_db_url_honors_catalog_db_override():
    with patch.dict(
        enc.os.environ,
        {
            "DATABASE_URL": "postgresql://app@host:5432/monolith",
            "ICEBERG_CATALOG_DB": "lh2",
        },
        clear=True,
    ):
        assert enc._resolve_lakehouse_db_url() == "postgresql://app@host:5432/lh2"


# --- read_note_changes_batch ---------------------------------------------


def test_read_batch_emits_updated_with_chunks_and_epoch_version():
    rows = [_note_row(1, "alpha", _T0)]
    chunks = [_chunk_row(1, 0), _chunk_row(1, 1)]
    with patched_psycopg(rows, chunks):
        batch = asyncio.run(read_note_changes_batch("1970-01-01", None, 0, 50))

    assert len(batch) == 1
    note = batch[0]
    assert note["event_type"] == "updated"
    # event_version = epoch-millis of indexed_at (the change marker).
    assert note["event_version"] == int(_T0.timestamp() * 1000)
    assert note["note_id"] == "alpha"
    assert len(note["chunks"]) == 2
    assert note["chunks"][0]["embedding"] == EMBEDDING_1024


def test_read_batch_emits_tombstone_for_soft_deleted_note():
    # deleted_at set + later than indexed_at -> tombstone versioned on deleted_at,
    # carrying NO chunks (RTBF: body/embeddings not forwarded).
    rows = [_note_row(2, "gone", _T0, deleted_at=_T1)]
    with patched_psycopg(rows, chunk_rows=[]):
        batch = asyncio.run(read_note_changes_batch("1970-01-01", None, 0, 50))

    assert len(batch) == 1
    note = batch[0]
    assert note["event_type"] == "tombstoned"
    assert note["event_version"] == int(_T1.timestamp() * 1000)
    assert "chunks" not in note


def test_read_batch_sets_repeatable_read_snapshot():
    import psycopg

    rows = [_note_row(1, "alpha", _T0)]
    with patched_psycopg(rows, [_chunk_row(1, 0)]) as conn:
        asyncio.run(read_note_changes_batch("1970-01-01", None, 0, 50))
    # The note + its chunks must be read in ONE snapshot so they can't be torn.
    assert conn.isolation_level == psycopg.IsolationLevel.REPEATABLE_READ


def test_read_batch_empty_returns_empty_list():
    with patched_psycopg(note_rows=[], chunk_rows=[]):
        assert asyncio.run(read_note_changes_batch("1970-01-01", None, 0, 50)) == []


# --- publish_note_change_events ------------------------------------------


def _updated_dict(note_id, version):
    return {
        "note_id": note_id,
        "path": f"_processed/{note_id}.md",
        "title": "T",
        "content_hash": "h",
        "type": "atom",
        "status": "active",
        "visibility": "public",
        "tags": [],
        "aliases": [],
        "chunks": [
            {
                "chunk_index": 0,
                "section_header": None,
                "chunk_text": "c",
                "embedding": EMBEDDING_1024,
            }
        ],
        "event_type": "updated",
        "event_version": version,
    }


def _tombstone_dict(note_id, version):
    return {"note_id": note_id, "event_type": "tombstoned", "event_version": version}


def test_publish_updated_carries_body_and_source_derived_msg_id():
    fake = FakeNatsClient()
    with patch.object(enc, "NatsClient", return_value=fake):
        count = asyncio.run(
            publish_note_change_events([_updated_dict("alpha", 1717243200000)])
        )

    assert count == 1
    call = fake.calls[0]
    assert call["msg_id"] == "alpha-v1717243200000"
    body = json.loads(call["payload"])
    assert body["event_type"] == "updated"
    assert body["producer"] == PRODUCER
    assert body["payload"]["chunks"][0]["embedding"] == EMBEDDING_1024
    assert fake.closed is True


def test_publish_tombstone_has_no_body_or_embeddings():
    fake = FakeNatsClient()
    with patch.object(enc, "NatsClient", return_value=fake):
        asyncio.run(
            publish_note_change_events([_tombstone_dict("gone", 1717243500000)])
        )

    body = json.loads(fake.calls[0]["payload"])
    assert body["event_type"] == "tombstoned"
    assert fake.calls[0]["msg_id"] == "gone-v1717243500000"
    # Tombstone payload carries only the note id (+ optional reason) — no chunks.
    assert "chunks" not in body["payload"]
    assert body["payload"]["note_id"] == "gone"


def test_publish_empty_batch_is_noop_no_connect():
    fake = FakeNatsClient()
    with patch.object(enc, "NatsClient", return_value=fake):
        assert asyncio.run(publish_note_change_events([])) == 0
    assert fake.connected is False


# --- corpus membership (the soft-delete tombstone fix) -------------------


def test_corpus_member_includes_pre_delete_path():
    # The HIGH-severity fix: soft-delete rewrites path out of _processed/ into
    # _trash/ while stashing the original in pre_delete_path. Membership MUST
    # gate deleted rows on pre_delete_path, or no tombstone is ever emitted.
    member = enc._CORPUS_MEMBER
    assert "deleted_at IS NULL AND path LIKE '_processed/%%'" in member
    assert "deleted_at IS NOT NULL AND pre_delete_path LIKE '_processed/%%'" in member


# --- watermark activities (read_export_lower_bound / advance) -------------


class _WmCursor:
    """Routes the watermark/seed queries; records executed SQL."""

    def __init__(self, watermark_row, max_change_at):
        self._watermark_row = watermark_row  # None or (datetime,)
        self._max_change_at = max_change_at  # datetime or None
        self.executed: list[str] = []
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if "SELECT watermark FROM export_state" in sql:
            self._last = self._watermark_row
        elif "SELECT MAX(" in sql:
            self._last = (self._max_change_at,)
        else:  # CREATE TABLE / INSERT — no result row
            self._last = None

    def fetchone(self):
        return self._last


class _WmConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


@contextmanager
def _patched_wm(watermark_row, max_change_at):
    import psycopg

    conn = _WmConn(_WmCursor(watermark_row, max_change_at))
    with (
        patch.object(psycopg, "connect", return_value=conn),
        patch.dict(
            enc.os.environ,
            {"DATABASE_URL": "postgresql://app:app@localhost:5432/monolith"},
        ),
    ):
        yield conn


def test_lower_bound_first_run_seeds_from_max_change_at():
    # No watermark row yet -> seed from the corpus MAX(change_at) and return
    # MAX - margin (assumes the bootstrap already covered history).
    with _patched_wm(watermark_row=None, max_change_at=_T1) as conn:
        lb = asyncio.run(read_export_lower_bound("note-export", 300))

    assert lb == (_T1 - timedelta(seconds=300)).isoformat()
    assert any("INSERT INTO export_state" in s for s in conn._cursor.executed)
    assert conn.committed is True


def test_lower_bound_uses_existing_watermark_minus_margin():
    with _patched_wm(watermark_row=(_T1,), max_change_at=None) as conn:
        lb = asyncio.run(read_export_lower_bound("note-export", 60))

    assert lb == (_T1 - timedelta(seconds=60)).isoformat()
    # Existing watermark -> no seed INSERT, no MAX query needed.
    assert not any("INSERT INTO export_state" in s for s in conn._cursor.executed)
    assert not any("SELECT MAX(" in s for s in conn._cursor.executed)


def test_advance_watermark_upserts_monotonically_with_greatest():
    with _patched_wm(watermark_row=None, max_change_at=None) as conn:
        asyncio.run(advance_export_watermark("note-export", _T1.isoformat()))

    joined = " ".join(conn._cursor.executed)
    assert "INSERT INTO export_state" in joined
    # GREATEST so a stale/replayed advance never moves the watermark backwards.
    assert "GREATEST(export_state.watermark, EXCLUDED.watermark)" in joined
    assert conn.committed is True


# --- exports --------------------------------------------------------------


def test_workflow_is_defn_and_exported():
    assert ExportNoteChangesWorkflow in WORKFLOWS
    assert temporalio.workflow._Definition.from_class(ExportNoteChangesWorkflow)


def test_activities_exported():
    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert {
        "read_export_lower_bound",
        "read_note_changes_batch",
        "publish_note_change_events",
        "advance_export_watermark",
    } <= names
