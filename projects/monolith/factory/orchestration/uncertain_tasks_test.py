from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from sqlmodel import Session, SQLModel, create_engine

from factory.orchestration import uncertain_tasks
from factory.orchestration.factory_models import FactoryStart
from factory.orchestration.models import SwarmTask

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'uncertain-tasks.db'}",
        execution_options={"schema_translate_map": {"swarm": None}},
    )
    # SwarmTask is the parent of the FactoryStart.task_id foreign key, so its
    # table must exist before the start ledger table is created.
    SQLModel.metadata.create_all(
        engine, tables=[SwarmTask.__table__, FactoryStart.__table__]
    )
    monkeypatch.setattr(uncertain_tasks, "get_engine", lambda: engine)
    with Session(engine) as session:
        yield session


def _starts(rows):
    return [
        FactoryStart(
            task_id=task_id,
            start_key=start_key,
            actor="test",
            model="opus",
            max_cost_usd=1.0,
            status=status,
            updated_at=updated_at,
        )
        for task_id, start_key, status, updated_at in rows
    ]


def _spans(monkeypatch):
    """Capture uncertain_tasks spans without installing a global provider."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(uncertain_tasks, "tracer", provider.get_tracer("test"))
    return exporter


def test_zero_uncertain_returns_zero(db):
    assert uncertain_tasks.uncertain_task_snapshot(db, now=NOW) == {
        "uncertain_tasks": 0,
        "oldest_uncertain_age_seconds": 0.0,
    }


def test_counts_distinct_tasks_and_oldest_age(db):
    db.add_all(
        _starts(
            [
                ("task-a", "key-a1", "uncertain", NOW - timedelta(hours=3)),
                ("task-b", "key-b1", "uncertain", NOW - timedelta(hours=1)),
                # A second uncertain start on the same task counts once.
                ("task-b", "key-b2", "uncertain", NOW - timedelta(minutes=30)),
                # Terminal rows never count, however old.
                ("task-c", "key-c1", "failed", NOW - timedelta(days=9)),
                ("task-d", "key-d1", "succeeded", NOW - timedelta(days=9)),
            ]
        )
    )
    db.commit()
    assert uncertain_tasks.uncertain_task_snapshot(db, now=NOW) == {
        "uncertain_tasks": 2,
        "oldest_uncertain_age_seconds": 3 * 3600.0,
    }


def test_future_updated_at_clamps_to_zero(db):
    db.add_all(_starts([("task-a", "key-a1", "uncertain", NOW + timedelta(minutes=5))]))
    db.commit()
    assert uncertain_tasks.uncertain_task_snapshot(db, now=NOW) == {
        "uncertain_tasks": 1,
        "oldest_uncertain_age_seconds": 0.0,
    }


def test_emit_sets_both_span_attributes(db, monkeypatch):
    db.add_all(_starts([("task-a", "key-a1", "uncertain", NOW - timedelta(hours=3))]))
    db.commit()
    exporter = _spans(monkeypatch)
    monkeypatch.setattr(uncertain_tasks, "_now", lambda: NOW)
    snapshot = uncertain_tasks.emit_uncertain_task_snapshot()
    assert snapshot == {
        "uncertain_tasks": 1,
        "oldest_uncertain_age_seconds": 3 * 3600.0,
    }
    spans = [s for s in exporter.get_finished_spans() if s.name == uncertain_tasks.SPAN]
    assert len(spans) == 1
    attributes = dict(spans[0].attributes)
    assert attributes["factory.tasks.uncertain"] == 1
    assert attributes["factory.tasks.oldest_uncertain_age_seconds"] == 3 * 3600.0


def test_emit_zero_case_still_emits_span(db, monkeypatch):
    exporter = _spans(monkeypatch)
    assert uncertain_tasks.emit_uncertain_task_snapshot() == {
        "uncertain_tasks": 0,
        "oldest_uncertain_age_seconds": 0.0,
    }
    spans = [s for s in exporter.get_finished_spans() if s.name == uncertain_tasks.SPAN]
    assert len(spans) == 1
    attributes = dict(spans[0].attributes)
    assert attributes["factory.tasks.uncertain"] == 0
    assert attributes["factory.tasks.oldest_uncertain_age_seconds"] == 0.0


def test_emit_query_failure_returns_zero_without_raising(monkeypatch):
    def broken_engine():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(uncertain_tasks, "get_engine", broken_engine)
    exporter = _spans(monkeypatch)
    assert uncertain_tasks.emit_uncertain_task_snapshot() == {
        "uncertain_tasks": 0,
        "oldest_uncertain_age_seconds": 0.0,
    }
    spans = [s for s in exporter.get_finished_spans() if s.name == uncertain_tasks.SPAN]
    assert len(spans) == 1
    assert dict(spans[0].attributes)["factory.tasks.uncertain"] == 0
    assert spans[0].events, "the query failure must be recorded on the span"
