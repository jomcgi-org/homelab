"""Hermetic tests for routine job updates."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text
from sqlmodel import Session, create_engine

from agent import routine_jobs


def _guarded_engine(tmp_path, name, *, locked_at, status=None, next_run_at=None):
    engine = create_engine(f"sqlite:///{tmp_path / (name.replace(':', '-') + '.db')}")
    with Session(engine) as session:
        session.execute(
            text(
                """
                CREATE TABLE routine_jobs (
                    name TEXT PRIMARY KEY, routine_kind TEXT, interval_secs INTEGER,
                    next_run_at TEXT, last_run_at TEXT, last_status TEXT,
                    last_summary TEXT, locked_by TEXT, locked_at TEXT,
                    ttl_secs INTEGER, payload TEXT, created_by TEXT, created_at TEXT
                )
                """
            )
        )
        session.execute(
            text(
                """
                INSERT INTO routine_jobs
                    (name, routine_kind, next_run_at, last_status, last_summary,
                     locked_by, locked_at, payload, created_by, created_at)
                VALUES
                    (:name, 'kg-drain', :next_run_at, :status, :summary,
                     :locked_by, :locked_at, :payload, 'factory', :created_at)
                """
            ),
            {
                "name": name,
                "next_run_at": next_run_at,
                "status": status,
                "summary": None,
                "locked_by": "luna-drainer" if locked_at is not None else None,
                "locked_at": locked_at,
                "payload": json.dumps({"prompt": "keep"}),
                "created_at": "2026-01-01 00:00:00",
            },
        )
        session.commit()
    return engine


def test_guarded_unknown_hold_matches_exact_claim(monkeypatch, tmp_path):
    engine = _guarded_engine(
        tmp_path, "kg:sha256", locked_at="2026-09-07 00:00:00", next_run_at=None
    )
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    expected = datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert routine_jobs.hold_job_for_unknown_outcome(
        "kg:sha256",
        2797,
        "unknown",
        expected_locked_by="luna-drainer",
        expected_locked_at=expected.isoformat(),
    )
    with Session(engine) as session:
        row = session.execute(text("SELECT * FROM routine_jobs")).one()
    assert row.payload == json.dumps({"prompt": "keep"})
    assert row.created_by == "factory"
    assert row.name == "kg:sha256"
    assert row.last_status == routine_jobs.UNKNOWN_INVOCATION
    assert row.next_run_at is None
    assert row.locked_by is None
    assert row.locked_at is None


def test_guarded_unknown_hold_idempotency_is_unlocked(monkeypatch, tmp_path):
    engine = _guarded_engine(
        tmp_path,
        "kg:idempotent",
        locked_at=None,
        status=routine_jobs.UNKNOWN_INVOCATION,
        next_run_at=None,
    )
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    with Session(engine) as session:
        session.execute(
            text("UPDATE routine_jobs SET last_summary = :summary WHERE name = :name"),
            {"name": "kg:idempotent", "summary": "session_id=2797: original"},
        )
        session.commit()
    assert routine_jobs.hold_job_for_unknown_outcome(
        "kg:idempotent",
        2797,
        "replacement",
        expected_locked_by="luna-drainer",
        expected_locked_at="2026-09-07T00:00:01+00:00",
    )
    with Session(engine) as session:
        row = session.execute(text("SELECT * FROM routine_jobs")).one()
    assert row.last_summary == "session_id=2797: original"
    assert row.payload == json.dumps({"prompt": "keep"})
    assert row.created_by == "factory"


def test_guarded_unknown_hold_does_not_touch_successor(monkeypatch, tmp_path):
    engine = _guarded_engine(
        tmp_path,
        "kg:successor",
        locked_at="2026-09-07 00:00:02",
        status="running",
        next_run_at="2026-09-07 00:01:00",
    )
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    assert not routine_jobs.hold_job_for_unknown_outcome(
        "kg:successor",
        2797,
        "stale",
        expected_locked_by="luna-drainer",
        expected_locked_at="2026-09-07T00:00:00+00:00",
    )
    with Session(engine) as session:
        row = session.execute(text("SELECT * FROM routine_jobs")).one()
    assert (row.locked_by, row.locked_at, row.last_status, row.next_run_at) == (
        "luna-drainer",
        "2026-09-07 00:00:02",
        "running",
        "2026-09-07 00:01:00",
    )


def test_update_job_payload_replaces_only_payload(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'routine-jobs.db'}")
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    with Session(engine) as session:
        session.execute(
            text(
                """
                CREATE TABLE routine_jobs (
                    name TEXT PRIMARY KEY,
                    routine_kind TEXT NOT NULL,
                    interval_secs INTEGER,
                    payload TEXT,
                    locked_by TEXT
                )
                """
            )
        )
        session.execute(
            text(
                """
                INSERT INTO routine_jobs
                    (name, routine_kind, interval_secs, payload, locked_by)
                VALUES
                    ('scout', 'kg-drain', 3600, :payload, 'holder')
                """
            ),
            {"payload": json.dumps({"mode": "repo-diff", "last_sha": None})},
        )
        session.commit()

    assert routine_jobs.update_job_payload(
        "scout", {"mode": "repo-diff", "last_sha": "a" * 40}
    )

    with Session(engine) as session:
        row = session.execute(text("SELECT * FROM routine_jobs")).one()
    assert json.loads(row.payload) == {
        "mode": "repo-diff",
        "last_sha": "a" * 40,
    }
    assert row.routine_kind == "kg-drain"
    assert row.interval_secs == 3600
    assert row.locked_by == "holder"


def test_unknown_job_hold_survives_reclaim_and_preserves_recurring_payload(
    monkeypatch, tmp_path
):
    engine = create_engine(f"sqlite:///{tmp_path / 'held-jobs.db'}")
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    payload = json.dumps({"raw_id": "raw-retain", "attempts": 1})
    with Session(engine) as session:
        session.execute(
            text("""
            CREATE TABLE routine_jobs (
                name TEXT PRIMARY KEY, routine_kind TEXT, interval_secs INTEGER,
                next_run_at TEXT, last_run_at TEXT, last_status TEXT, last_summary TEXT,
                locked_by TEXT, locked_at TEXT, ttl_secs INTEGER, payload TEXT,
                created_by TEXT, created_at TEXT
            )
        """)
        )
        for name, interval in (
            ("one-shot", None),
            ("recurring", 3600),
            ("ordinary", 3600),
        ):
            session.execute(
                text("""
                INSERT INTO routine_jobs (name, routine_kind, interval_secs,
                    next_run_at, locked_by, locked_at, ttl_secs, payload)
                VALUES (:name, 'kg-drain', :interval, CURRENT_TIMESTAMP,
                    'dead-drainer', '2000-01-01', 1, :payload)
            """),
                {"name": name, "interval": interval, "payload": payload},
            )
        session.commit()
    assert routine_jobs.complete_job("ordinary", "ok", "finished") is True
    assert routine_jobs.trigger_job("ordinary") is True
    assert routine_jobs.claim_job("worker", 60, name="ordinary")["name"] == "ordinary"
    assert routine_jobs.defer_job("ordinary", 1) is True
    assert routine_jobs.deregister_job("ordinary") is True
    for name in ("one-shot", "recurring"):
        assert routine_jobs.hold_job_for_unknown_outcome(name, 2448, "reconcile first")
    for name in ("one-shot", "recurring"):
        assert routine_jobs.complete_job(name, "ok", "late result") is False
        assert routine_jobs.trigger_job(name) is False
        assert routine_jobs.defer_job(name, 1) is False
        assert routine_jobs.deregister_job(name) is False
    # Simulate later processes opening fresh connections, including an ordinary
    # re-arm. Neither periodic nor named admission may infer reconciliation.
    with Session(engine) as session:
        rows = session.execute(text("SELECT * FROM routine_jobs ORDER BY name")).all()
        assert all(row.next_run_at is None for row in rows)
        assert all(row.last_status == "invocation_outcome_unknown" for row in rows)
        assert all(row.payload == payload for row in rows)
        assert all("session_id=2448" in row.last_summary for row in rows)
        assert rows[1].interval_secs == 3600
        session.execute(text("UPDATE routine_jobs SET next_run_at = CURRENT_TIMESTAMP"))
        session.commit()
    assert routine_jobs.claim_job("replacement", 60, kinds=["kg-drain"]) is None
    assert routine_jobs.claim_job("replacement", 60, name="recurring") is None
