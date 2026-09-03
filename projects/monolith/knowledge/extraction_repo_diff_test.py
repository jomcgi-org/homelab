"""Tests for the repository diff scout stage."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge.extraction import (
    ExtractionOutputInvalid,
    apply_repo_diff,
    build_repo_diff_prompt,
    ensure_repo_diff_job,
)
from knowledge.models import RawInput


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'repo-diff.db'}")
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.execute(
                text(
                    """
                    CREATE TABLE routine_jobs (
                        name TEXT PRIMARY KEY,
                        routine_kind TEXT NOT NULL,
                        interval_secs INTEGER,
                        next_run_at TIMESTAMP,
                        last_run_at TIMESTAMP,
                        payload TEXT,
                        created_by TEXT
                    )
                    """
                )
            )
            session.commit()
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _scout_job(session: Session, name: str = "kg-repo-diff") -> None:
    session.execute(
        text(
            """
            INSERT INTO routine_jobs
                (name, routine_kind, interval_secs, next_run_at, payload, created_by)
            VALUES (:name, 'kg-drain', 3600, CURRENT_TIMESTAMP, :payload, 'test')
            """
        ),
        {"name": name, "payload": json.dumps({"mode": "repo-diff", "last_sha": None})},
    )
    session.commit()


def _output(*, base_sha: str | None, diff_stat: str = "", diff: str = "") -> str:
    return (
        "```json\n"
        + json.dumps(
            {
                "head_sha": "b" * 40,
                "base_sha": base_sha,
                "diff_stat": diff_stat,
                "diff": diff,
            }
        )
        + "\n```"
    )


def test_scout_prompt_renders_null_cursor_branch():
    prompt = build_repo_diff_prompt(None)

    assert "prior cursor is null" in prompt
    assert "This first run only establishes" in prompt
    assert '"base_sha": "full SHA or null"' in prompt
    assert prompt.rstrip().endswith("```")


def test_scout_prompt_renders_set_cursor_branch():
    sha = "a" * 40
    prompt = build_repo_diff_prompt(sha)

    assert sha in prompt
    assert "git diff --stat <last_sha>..HEAD" in prompt
    assert "pnpm-lock.yaml" in prompt
    assert "requirements*.txt" in prompt
    assert "everything under `bazel-*`" in prompt
    assert "60000 characters" in prompt
    assert "[... elided ...]" in prompt


def test_apply_no_change_advances_cursor_without_raw(session):
    _scout_job(session)

    applied = apply_repo_diff(session, "kg-repo-diff", _output(base_sha=None))

    assert applied["summary"] == "no changes"
    assert session.exec(select(RawInput)).all() == []
    payload = session.execute(
        text("SELECT payload FROM routine_jobs WHERE name = 'kg-repo-diff'")
    ).scalar_one()
    assert json.loads(payload) == {"mode": "repo-diff", "last_sha": "b" * 40}


def test_apply_change_writes_raw_queues_extraction_and_advances_cursor(
    session, monkeypatch
):
    _scout_job(session)
    uploaded = {}
    monkeypatch.setattr(
        "knowledge.ingest_queue.upload_raw",
        lambda raw_id, content: uploaded.update(raw_id=raw_id, content=content),
    )

    applied = apply_repo_diff(
        session,
        "kg-repo-diff",
        _output(
            base_sha="a" * 40,
            diff_stat=" projects/monolith/example.py | 2 ++\n 1 file changed",
            diff="diff --git a/example.py b/example.py\n+setting = true",
        ),
    )

    raw = session.exec(select(RawInput)).one()
    assert applied["raw_id"] == raw.raw_id
    assert applied["created"] is True
    assert raw.source == "repo-diff"
    assert raw.original_path == f"repo-diff:{'a' * 40}..{'b' * 40}"
    assert raw.extra["changed_files"] == 1
    assert "title: main diff aaaaaaa..bbbbbbb" in uploaded["content"]
    jobs = session.execute(
        text("SELECT name, payload FROM routine_jobs ORDER BY name")
    ).all()
    assert [row.name for row in jobs] == ["kg-repo-diff", f"kg:{raw.raw_id}"]
    scout_payload = json.loads(jobs[0].payload)
    assert scout_payload["last_sha"] == "b" * 40


def test_duplicate_content_still_advances_cursor(session, monkeypatch):
    _scout_job(session)
    monkeypatch.setattr("knowledge.ingest_queue.upload_raw", lambda *_args: None)
    result = _output(
        base_sha="a" * 40,
        diff_stat=" file.py | 1 +",
        diff="diff --git a/file.py b/file.py\n+x = 1",
    )
    assert apply_repo_diff(session, "kg-repo-diff", result)["created"] is True
    session.execute(
        text("UPDATE routine_jobs SET payload = :payload WHERE name = 'kg-repo-diff'"),
        {"payload": json.dumps({"mode": "repo-diff", "last_sha": "a" * 40})},
    )
    session.commit()

    replay = apply_repo_diff(session, "kg-repo-diff", result)

    assert replay["created"] is False
    assert len(session.exec(select(RawInput)).all()) == 1
    payload = session.execute(
        text("SELECT payload FROM routine_jobs WHERE name = 'kg-repo-diff'")
    ).scalar_one()
    assert json.loads(payload)["last_sha"] == "b" * 40


def test_malformed_scout_json_raises(session):
    _scout_job(session)

    with pytest.raises(ExtractionOutputInvalid):
        apply_repo_diff(session, "kg-repo-diff", "```json\n{bad}\n```")


def test_repo_diff_job_registration_follows_flag(session, monkeypatch):
    monkeypatch.setenv("KG_REPO_DIFF_ENABLED", "true")
    assert ensure_repo_diff_job(session) is True
    session.commit()
    job = session.execute(text("SELECT interval_secs, payload FROM routine_jobs")).one()
    assert job.interval_secs == 3600
    assert json.loads(job.payload) == {"mode": "repo-diff", "last_sha": None}

    monkeypatch.setenv("KG_REPO_DIFF_ENABLED", "false")
    assert ensure_repo_diff_job(session) is True
    session.commit()
    assert session.execute(text("SELECT name FROM routine_jobs")).all() == []
