"""Unit tests for the goosecracker sessions.db store seam (ADR 026 Phase 2).

The store is a thin passthrough to ``artifact.s3`` (S3-backed), so the tests
assert the wiring: load/save delegate to get_session/put_session keyed by the
session id. artifact.s3 is stubbed with an in-memory dict so no S3 is needed.
"""

from __future__ import annotations

from goosecracker import sessions


def test_save_then_load_round_trips_via_s3(monkeypatch):
    store: dict[str, bytes] = {}
    monkeypatch.setattr(
        "artifact.s3.put_session", lambda sid, db: store.__setitem__(sid, db)
    )
    monkeypatch.setattr("artifact.s3.get_session", lambda sid: store.get(sid))

    blob = b"\x00sqlite-bytes\xff\x01"
    sessions.save("sess-1", blob)

    assert sessions.load("sess-1") == blob


def test_load_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr("artifact.s3.get_session", lambda sid: None)
    assert sessions.load("no-such-session") is None


def test_save_overwrites_previous_blob(monkeypatch):
    store: dict[str, bytes] = {}
    monkeypatch.setattr(
        "artifact.s3.put_session", lambda sid, db: store.__setitem__(sid, db)
    )
    monkeypatch.setattr("artifact.s3.get_session", lambda sid: store.get(sid))

    sessions.save("sess-3", b"first")
    sessions.save("sess-3", b"second")

    assert sessions.load("sess-3") == b"second"


def test_load_delegates_with_session_id(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr("artifact.s3.get_session", lambda sid: seen.append(sid) or b"x")
    sessions.load("the-session-id")
    assert seen == ["the-session-id"]
