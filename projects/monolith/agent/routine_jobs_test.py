"""Hermetic tests for routine job updates."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlmodel import Session, create_engine

from agent import routine_jobs


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
