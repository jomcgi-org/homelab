"""BDD tests for the goosecracker sessions.db blob store (ADR 026 Phase 2)."""

from __future__ import annotations

from goosecracker import sessions, threads


def _make_row(session_id: str) -> None:
    threads.upsert_run(session_id, recipe="agent", tier="", task="t", discord_thread="")


def test_save_then_load_round_trips_bytes(ledger_db):
    _make_row("sess-db-1")
    blob = b"\x00sqlite-bytes\xff\x01"

    sessions.save("sess-db-1", blob)

    assert sessions.load("sess-db-1") == blob


def test_load_returns_none_when_never_saved(ledger_db):
    _make_row("sess-db-2")
    assert sessions.load("sess-db-2") is None


def test_load_returns_none_for_unknown_session(ledger_db):
    assert sessions.load("no-such-session") is None


def test_save_overwrites_previous_blob(ledger_db):
    _make_row("sess-db-3")
    sessions.save("sess-db-3", b"first")
    sessions.save("sess-db-3", b"second")

    assert sessions.load("sess-db-3") == b"second"
