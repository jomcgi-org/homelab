"""Hermetic persistence tests for deployment observations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge.deployment_observations import (
    DEPLOYMENT_OBSERVATION_FIELDS,
    build_deployment_observation,
    list_deployment_observations,
    persist_deployment_observation,
)
from knowledge.models import AtomRawProvenance, Note, RawInput


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'deployment-observations.db'}")
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _observation(poll_time: datetime, **overrides):
    values = {
        "app": "monolith",
        "status": "complete",
        "poll_time": poll_time,
        "probe_interval_s": 300,
        "requested_revision": "0.506.0",
        "deployed_revision": "0.505.1",
        "newest_freight_version": "0.507.0",
        "writeback_commit": "a" * 40,
    }
    values.update(overrides)
    return build_deployment_observation(**values)


async def _persist(session, observation):
    with patch("knowledge.raw_write.upload_raw"):
        return await persist_deployment_observation(
            session,
            observation,
            vectors=[[0.0] * 1024],
        )


@pytest.mark.asyncio
async def test_persists_one_raw_fact_and_server_provenance(session):
    poll_time = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    observation = _observation(poll_time)

    result = await _persist(session, observation)

    raws = session.exec(select(RawInput)).all()
    notes = session.exec(select(Note)).all()
    provenance = session.exec(select(AtomRawProvenance)).all()
    assert result.raw_created and result.fact_created and result.provenance_created
    assert len(raws) == len(notes) == len(provenance) == 1
    note = notes[0]
    assert note.scope == "environment:homelab"
    assert note.source == "deployment-observation"
    assert note.extra["requested_revision"] == "0.506.0"
    assert note.extra["deployed_revision"] == "0.505.1"
    assert note.extra["newest_freight_version"] == "0.507.0"
    assert note.extra["writeback_commit"] == "a" * 40
    assert note.observed_at.replace(tzinfo=timezone.utc) == poll_time
    assert note.valid_from.replace(tzinfo=timezone.utc) == poll_time
    assert note.valid_until.replace(tzinfo=timezone.utc) == poll_time + timedelta(
        seconds=750
    )
    assert provenance[0].atom_fk == note.id
    assert provenance[0].raw_fk == raws[0].id
    assert provenance[0].gardener_version == "deployment-observation/v1"


@pytest.mark.asyncio
async def test_same_event_replay_and_rebuild_create_no_duplicates(session):
    poll_time = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    first = await _persist(session, _observation(poll_time))
    replay = await _persist(session, _observation(poll_time))
    rebuilt = await _persist(
        session,
        _observation(poll_time, deployed_revision="0.505.2"),
    )

    assert first.raw_created and first.fact_created and first.provenance_created
    assert not any(
        (
            replay.raw_created,
            replay.fact_created,
            replay.provenance_created,
            rebuilt.raw_created,
            rebuilt.fact_created,
            rebuilt.provenance_created,
        )
    )
    assert len(session.exec(select(RawInput)).all()) == 1
    assert len(session.exec(select(Note)).all()) == 1
    assert len(session.exec(select(AtomRawProvenance)).all()) == 1


@pytest.mark.asyncio
async def test_later_unchanged_poll_is_a_distinct_observation(session):
    poll_time = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    await _persist(session, _observation(poll_time))
    await _persist(session, _observation(poll_time + timedelta(seconds=300)))

    assert len(session.exec(select(RawInput)).all()) == 2
    assert len(session.exec(select(Note)).all()) == 2
    assert len(session.exec(select(AtomRawProvenance)).all()) == 2


@pytest.mark.asyncio
async def test_active_as_of_excludes_expiry_but_history_keeps_overlaps(session):
    poll_time = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    await _persist(session, _observation(poll_time))
    await _persist(session, _observation(poll_time + timedelta(seconds=300)))

    history = list_deployment_observations(session)
    overlap = list_deployment_observations(
        session, active_as_of=poll_time + timedelta(seconds=500)
    )
    first_boundary = list_deployment_observations(
        session, active_as_of=poll_time + timedelta(seconds=750)
    )
    later = list_deployment_observations(
        session, active_as_of=poll_time + timedelta(seconds=800)
    )

    assert len(history) == 2
    assert len(overlap) == 2
    assert len(first_boundary) == 1
    assert len(later) == 1
    assert first_boundary[0]["observed_at"].replace(tzinfo=timezone.utc) == (
        poll_time + timedelta(seconds=300)
    )


def test_payload_allowlist_rejects_telemetry_fields():
    poll_time = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    observation = _observation(poll_time)
    forbidden = {
        "pod_readiness",
        "restart_count",
        "container",
        "termination_reason",
    }

    assert forbidden.isdisjoint(DEPLOYMENT_OBSERVATION_FIELDS)
    for field in forbidden:
        invalid = {**observation, field: "must not persist"}
        with pytest.raises(ValueError, match="forbidden fields"):
            from knowledge.deployment_observations import _validate_observation

            _validate_observation(invalid)
