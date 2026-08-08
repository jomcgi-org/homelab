"""Tests for the dead letter API endpoints."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from knowledge.gardener import MAX_GARDENER_RETRIES
from knowledge.models import AtomRawProvenance, RawInput


@pytest.fixture
def session():
    from sqlmodel.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as s:
            yield s
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture
def client(session):
    import dataclasses
    import knowledge.module
    from framework import PRIVATE_PROFILE, build_app

    # Compose only the knowledge domain instead of the whole monolith: the
    # same framework wiring the production app gets, without depending on
    # the app composition root, which imports every other domain.
    app = build_app(
        dataclasses.replace(PRIVATE_PROFILE, otel_enabled=False),
        (knowledge.module.MODULE,),
    )
    from core.db import get_session

    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_raw(
    session: Session,
    *,
    raw_id: str = "raw-1",
    path: str = "raw/test.md",
    source: str = "test",
) -> RawInput:
    raw = RawInput(
        raw_id=raw_id,
        path=path,
        source=source,
        content="test content",
        content_hash="abc123",
    )
    session.add(raw)
    session.commit()
    session.refresh(raw)
    return raw


def _make_dead_letter(
    session: Session, raw: RawInput, *, error: str = "boom", retry_count: int = 3
) -> AtomRawProvenance:
    prov = AtomRawProvenance(
        raw_fk=raw.id,
        derived_note_id="failed",
        gardener_version="test-v1",
        error=error,
        retry_count=retry_count,
    )
    session.add(prov)
    session.commit()
    session.refresh(prov)
    return prov


class TestListDeadLetters:
    def test_returns_exhausted_raws(self, client, session):
        raw = _make_raw(session)
        _make_dead_letter(session, raw, retry_count=MAX_GARDENER_RETRIES)

        resp = client.get("/api/knowledge/dead-letter")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["id"] == raw.id
        assert item["path"] == raw.path
        assert item["source"] == raw.source
        assert item["error"] == "boom"
        assert item["retry_count"] == MAX_GARDENER_RETRIES

    def test_excludes_retriable(self, client, session):
        raw = _make_raw(session)
        _make_dead_letter(session, raw, retry_count=1)

        resp = client.get("/api/knowledge/dead-letter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []

    def test_empty_when_no_failures(self, client, session):
        resp = client.get("/api/knowledge/dead-letter")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []


class TestReplayDeadLetter:
    def test_replay_deletes_provenance(self, client, session):
        raw = _make_raw(session)
        prov = _make_dead_letter(session, raw)

        resp = client.post(f"/api/knowledge/dead-letter/{raw.id}/replay")
        assert resp.status_code == 200
        assert resp.json() == {"replayed": True}

        # Provenance row should be deleted
        remaining = session.get(AtomRawProvenance, prov.id)
        assert remaining is None

    def test_404_for_unknown_raw_id(self, client, session):
        resp = client.post("/api/knowledge/dead-letter/9999/replay")
        assert resp.status_code == 404

    def test_404_for_non_dead_lettered_raw(self, client, session):
        raw = _make_raw(session)
        # Raw exists but has no dead-letter provenance
        resp = client.post(f"/api/knowledge/dead-letter/{raw.id}/replay")
        assert resp.status_code == 404


class TestReplayDeadLetterIntegration:
    """After replaying a dead-lettered raw via the API, the raw must become
    eligible for decomposition again (appear in raws_needing_decomposition)."""

    def test_replayed_raw_appears_in_raws_needing_decomposition(
        self, client, session, tmp_path
    ):
        """Replaying a dead-lettered raw removes the 'failed' provenance row,
        so the raw has no handled provenance and becomes eligible for
        decomposition in the next gardener cycle."""
        from knowledge.store import KnowledgeStore

        raw = _make_raw(session)
        # Make it a dead letter (exhausted retries)
        _make_dead_letter(session, raw, retry_count=MAX_GARDENER_RETRIES)

        # Verify it is NOT eligible before replay (retry_count >= _MAX_RETRIES).
        store = KnowledgeStore(session)
        before = [r.id for r in store.raws_needing_decomposition()]
        assert raw.id not in before, (
            "exhausted raw must not appear in decomposition queue before replay"
        )

        # Replay via the API.
        resp = client.post(f"/api/knowledge/dead-letter/{raw.id}/replay")
        assert resp.status_code == 200
        assert resp.json() == {"replayed": True}

        # Now the raw has no provenance at all — it should reappear as fresh.
        after = [r.id for r in store.raws_needing_decomposition()]
        assert raw.id in after, (
            "replayed raw must appear in decomposition queue after replay"
        )

    def test_replay_endpoint_returns_404_then_raw_stays_absent(
        self, client, session, tmp_path
    ):
        """Replaying a non-dead-lettered raw leaves nothing changed — the raw
        (which has no provenance at all) is still eligible for decomposition."""
        from knowledge.store import KnowledgeStore

        raw = _make_raw(session)

        # No dead-letter provenance → replay returns 404.
        resp = client.post(f"/api/knowledge/dead-letter/{raw.id}/replay")
        assert resp.status_code == 404

        # The raw has no provenance so it's still a fresh decomposition candidate.
        store = KnowledgeStore(session)
        result = [r.id for r in store.raws_needing_decomposition()]
        assert raw.id in result
