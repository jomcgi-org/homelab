from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from observability.merged_prs import (
    MERGE_TYPES,
    MergedPR,
    is_agent_authored,
    parse_title,
)
from observability.merged_prs_writer import upsert_and_prune

_NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("feat: add dashboard", ("feat", None)),
        ("fix(monolith): repair snapshot", ("fix", "monolith")),
        ("docs!: rewrite guide", ("docs", None)),
        ("chore(deps)!: update lock", ("chore", "deps")),
        ("test: cover parser", ("test", None)),
        ("refactor(api): simplify query", ("refactor", "api")),
        ("ci: update workflow", ("ci", None)),
        ("build(image): publish container", ("build", "image")),
        ("perf: reduce query count", ("perf", None)),
        ("style: format files", ("style", None)),
        ("revert: restore behavior", ("revert", None)),
        ("wip(factory): checkpoint", ("wip", "factory")),
        ("[qwen] feat(factory): drain queue", ("feat", "factory")),
        ("[qwen] update documentation", ("other", None)),
        ("Fix: wrong case", ("other", None)),
        ("release version 1", ("other", None)),
    ],
)
def test_parse_title(title, expected):
    assert parse_title(title) == expected


def test_merge_types_lists_every_supported_title_type():
    assert MERGE_TYPES == (
        "feat",
        "fix",
        "docs",
        "chore",
        "test",
        "refactor",
        "ci",
        "build",
        "perf",
        "style",
        "revert",
        "wip",
    )


@pytest.mark.parametrize(
    "body",
    [
        "Generated with [Claude Code]",
        "generated WITH [claude code] in a footer",
        "https://claude.ai/code/session/123",
        "Generated with OpenAI Codex",
    ],
)
def test_is_agent_authored_detects_markers_case_insensitively(body):
    assert is_agent_authored(body)


def test_is_agent_authored_rejects_unmarked_body():
    assert not is_agent_authored("Written and reviewed by a person")


def test_is_agent_authored_rejects_false_positives():
    body = "The shared Claude/Codex/Pi shim sends progress to the control plane."

    assert not is_agent_authored(body)


@pytest.fixture(name="engine")
def engine_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'merged-prs.db'}")
    table = MergedPR.__table__
    original_schema = table.schema
    table.schema = None
    try:
        SQLModel.metadata.create_all(engine, tables=[table])
        yield engine
    finally:
        table.schema = original_schema
        engine.dispose()


def _pull(number: int, merged_at: datetime, title: str = "feat(core): add thing"):
    return {
        "number": number,
        "title": title,
        "merged_at": merged_at,
        "additions": 10,
        "deletions": 3,
        "changed_files": 2,
        "body": "Generated with [Claude Code]",
    }


def test_upsert_is_idempotent(engine):
    cutoff = _NOW - timedelta(days=90)
    with Session(engine) as session:
        upsert_and_prune(session, [_pull(1, _NOW)], cutoff, snapshotted_at=_NOW)
        updated = _pull(1, _NOW, "fix: corrected")
        updated["additions"] = 20
        upsert_and_prune(session, [updated], cutoff, snapshotted_at=_NOW)

        rows = list(session.exec(select(MergedPR)).all())
        assert len(rows) == 1
        assert rows[0].title == "fix: corrected"
        assert rows[0].type == "fix"
        assert rows[0].additions == 20


def test_delete_old_rows(engine):
    cutoff = _NOW - timedelta(days=90)
    with Session(engine) as session:
        snapshotted, deleted = upsert_and_prune(
            session,
            [
                _pull(1, cutoff - timedelta(seconds=1)),
                _pull(2, cutoff + timedelta(seconds=1)),
            ],
            cutoff,
            snapshotted_at=_NOW,
        )

        assert snapshotted == 2
        assert deleted == 1
        assert [row.number for row in session.exec(select(MergedPR)).all()] == [2]


def test_ninety_day_boundary_is_inclusive(engine):
    cutoff = _NOW - timedelta(days=90)
    with Session(engine) as session:
        upsert_and_prune(
            session,
            [_pull(1, cutoff), _pull(2, cutoff - timedelta(microseconds=1))],
            cutoff,
            snapshotted_at=_NOW,
        )

        assert [row.number for row in session.exec(select(MergedPR)).all()] == [1]


def test_upsert_processes_every_two_hundred_row_chunk(engine):
    cutoff = _NOW - timedelta(days=90)
    pulls = [_pull(number, _NOW) for number in range(1, 402)]

    with Session(engine) as session:
        snapshotted, deleted = upsert_and_prune(
            session, pulls, cutoff, snapshotted_at=_NOW
        )
        numbers = list(session.exec(select(MergedPR.number)).all())

        assert snapshotted == 401
        assert deleted == 0
        assert len(numbers) == 401
