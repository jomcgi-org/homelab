"""Focused regression tests for review-approved Grimoire alias merges."""

from __future__ import annotations

import asyncio

import pytest
from auth.api import Authority, Principal, PrincipalKind, get_principal
from core.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from knowledge.api import get_embedding_client
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire import aliases
from grimoire.models import (
    AliasCandidate,
    ChunkEntityMention,
    Embedding,
    Entity,
    EntityNpc,
    KnowledgeChunk,
    Relationship,
)
from grimoire.router import router


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'aliases.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

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
        engine.dispose()
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


class FakeEmbedClient:
    model = "test-embedding"

    def __init__(self):
        self.calls: list[list[str]] = []

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.75] * 1024 for _ in texts]


def _entity(
    session: Session,
    entity_type: str,
    name: str,
    book: str = "lmop",
    **values,
) -> Entity:
    row = Entity(
        entity_type=entity_type,
        name=name,
        source_book=book,
        **values,
    )
    session.add(row)
    session.flush()
    return row


def _chunk(session: Session, ref: str, content: str, book: str = "lmop"):
    row = KnowledgeChunk(book_id=book, chunk_ref=ref, content=content)
    session.add(row)
    session.flush()
    return row


def _mention(session: Session, chunk, entity, text: str | None = None):
    session.add(
        ChunkEntityMention(
            chunk_id=chunk.id,
            entity_id=entity.id,
            mention_text=text,
        )
    )


def _candidate(session: Session) -> AliasCandidate:
    report = aliases.generate_candidates(session)
    assert len(report["candidates"]) == 1
    return session.get(AliasCandidate, report["candidates"][0]["id"])


def _approve(session: Session, candidate: AliasCandidate):
    return aliases.approve_candidate(
        session,
        candidate.id,
        reviewer="reviewer@example.test",
        survivor_id=candidate.full_entity_id,
        expected_state_hash=candidate.state_hash,
    )


def test_candidate_signals_and_comention_report(session: Session):
    prefix_short = _entity(session, "npc", "Gundren")
    prefix_full = _entity(session, "npc", "Gundren Rockseeker")
    subset_short = _entity(session, "npc", "Rockseeker")
    unrelated = _entity(session, "npc", "Sildar Hallwinter")
    wrong_type = _entity(session, "faction", "Gundren's Company")
    wrong_book = _entity(session, "npc", "Gundren Stonefoot", book="other")
    no_comention = _entity(session, "npc", "Gundren Forgekeeper")
    location_short = _entity(session, "location", "Hall", site="redbrand-hideout")
    location_full = _entity(session, "location", "Grand Hall", site="cragmaw-castle")
    chunk = _chunk(
        session,
        "c1",
        "Gundren Rockseeker, often called Gundren or Rockseeker, met Sildar.",
    )
    for entity in (
        prefix_short,
        prefix_full,
        subset_short,
        unrelated,
        wrong_type,
        wrong_book,
        location_short,
        location_full,
    ):
        _mention(session, chunk, entity, f"summary for {entity.name}")
    other = _chunk(session, "c2", "Gundren Forgekeeper appears elsewhere.")
    _mention(session, other, no_comention, "separate evidence")
    session.commit()

    report = aliases.generate_candidates(session)

    pairs = {
        (row["short_name"], row["full_name"]): row
        for row in report["candidates"]
        if row["status"] == "pending"
    }
    assert set(pairs) == {
        ("Gundren", "Gundren Rockseeker"),
        ("Rockseeker", "Gundren Rockseeker"),
    }
    evidence = pairs[("Gundren", "Gundren Rockseeker")]["evidence"]
    assert evidence[0]["chunk_id"] == chunk.id
    assert "Gundren Rockseeker" in evidence[0]["snippet"]
    assert evidence[0]["short_mention"] == "summary for Gundren"
    assert pairs[("Gundren", "Gundren Rockseeker")]["evidence_count"] == 1


def test_execution_requires_explicit_approval(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was known as Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    embedder = FakeEmbedClient()

    with pytest.raises(aliases.ApprovalRequired):
        asyncio.run(aliases.execute_approved_candidate(session, candidate.id, embedder))

    assert embedder.calls == []
    assert session.get(Entity, short.id) is not None
    assert session.get(Entity, full.id) is not None


def test_merge_rewrites_mentions_edges_and_typed_detail(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    target = _entity(session, "location", "Wave Echo Cave")
    session.add(EntityNpc(entity_id=short.id, race="dwarf", occupation="miner"))
    session.add(
        EntityNpc(entity_id=full.id, occupation="businessperson", description="full")
    )
    shared = _chunk(session, "c1", "Gundren Rockseeker, called Gundren, found a cave.")
    short_only = _chunk(session, "c2", "Gundren guarded the map.")
    _mention(session, shared, short, "short summary")
    _mention(session, shared, full, "full summary")
    _mention(session, short_only, short, "map summary")
    session.add_all(
        [
            Relationship(
                from_entity_id=short.id,
                to_entity_id=target.id,
                rel_type="FOUND",
                properties={"short": True, "conflict": "short"},
                chunk_id=shared.id,
            ),
            Relationship(
                from_entity_id=full.id,
                to_entity_id=target.id,
                rel_type="FOUND",
                properties={"full": True, "conflict": "full"},
                chunk_id=short_only.id,
            ),
            Relationship(
                from_entity_id=target.id,
                to_entity_id=short.id,
                rel_type="KNOWN_BY",
            ),
            Relationship(
                from_entity_id=short.id,
                to_entity_id=full.id,
                rel_type="RELATED_TO",
            ),
        ]
    )
    session.add_all(
        [
            Embedding(
                embeddable_kind="entity",
                embeddable_id=short.id,
                model="old",
                dim=1024,
                vector=[0.1] * 1024,
            ),
            Embedding(
                embeddable_kind="entity",
                embeddable_id=full.id,
                model="old",
                dim=1024,
                vector=[0.2] * 1024,
            ),
            Embedding(
                embeddable_kind="entity",
                embeddable_id=full.id,
                model="older",
                dim=1024,
                vector=[0.3] * 1024,
            ),
        ]
    )
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)
    embedder = FakeEmbedClient()

    result = asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, embedder)
    )

    assert result["embedding_refreshed"] is True
    assert session.get(Entity, short.id) is None
    assert session.get(Entity, full.id) is not None
    mentions = session.exec(select(ChunkEntityMention)).all()
    assert {(row.chunk_id, row.entity_id) for row in mentions} == {
        (shared.id, full.id),
        (short_only.id, full.id),
    }
    shared_mention = session.get(ChunkEntityMention, (shared.id, full.id))
    assert shared_mention.mention_text == "full summary\nshort summary"

    edges = session.exec(select(Relationship)).all()
    assert {(row.from_entity_id, row.to_entity_id, row.rel_type) for row in edges} == {
        (full.id, target.id, "FOUND"),
        (target.id, full.id, "KNOWN_BY"),
    }
    found = next(row for row in edges if row.rel_type == "FOUND")
    assert found.properties["full"] is True
    assert found.properties["short"] is True
    assert found.properties["conflict"] == "full"
    assert found.properties["_alias_merge_conflicts"]["conflict"] == [
        "full",
        "short",
    ]
    assert found.properties["_alias_merge_chunk_ids"] == sorted(
        [shared.id, short_only.id]
    )

    detail = session.get(EntityNpc, full.id)
    assert detail.race == "dwarf"
    assert detail.occupation == "businessperson"
    assert detail.description == "full"
    embeddings = session.exec(
        select(Embedding).where(Embedding.embeddable_kind == "entity")
    ).all()
    assert [(row.embeddable_id, row.model) for row in embeddings] == [
        (full.id, "test-embedding")
    ]
    assert embedder.calls == [
        ["Gundren Rockseeker: full summary | map summary | short summary"]
    ]


def test_generic_detail_merge_is_recursive_and_survivor_wins(session: Session):
    short = _entity(
        session,
        "item",
        "Orb",
        detail={"stats": {"charges": 3, "range": 30}, "owner": "short"},
    )
    full = _entity(
        session,
        "item",
        "Orb of Doom",
        detail={"stats": {"charges": 5}, "owner": "full"},
    )
    chunk = _chunk(session, "c1", "The Orb of Doom is also called the Orb.")
    _mention(session, chunk, short, "orb summary")
    _mention(session, chunk, full, "doom summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)

    asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, FakeEmbedClient())
    )

    survivor = session.get(Entity, full.id)
    assert survivor.detail == {
        "stats": {"charges": 5, "range": 30},
        "owner": "full",
    }


def test_failure_rolls_back_and_leaves_approval_retryable(
    session: Session, monkeypatch
):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)

    def fail_relationship_rewrite(*_args):
        raise RuntimeError("forced database-stage failure")

    monkeypatch.setattr(aliases, "_rewrite_relationships", fail_relationship_rewrite)
    with pytest.raises(RuntimeError, match="forced database-stage failure"):
        asyncio.run(
            aliases.execute_approved_candidate(session, candidate.id, FakeEmbedClient())
        )

    assert session.get(Entity, short.id) is not None
    assert session.get(Entity, full.id) is not None
    assert len(session.exec(select(ChunkEntityMention)).all()) == 2
    assert session.get(AliasCandidate, candidate.id).status == "approved"


def test_embedding_failure_happens_before_database_mutation(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)

    class FailingEmbedClient:
        model = "test-embedding"

        async def embed_batch(self, _texts):
            raise RuntimeError("embedding unavailable")

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        asyncio.run(
            aliases.execute_approved_candidate(
                session, candidate.id, FailingEmbedClient()
            )
        )

    assert session.get(Entity, short.id) is not None
    assert session.get(Entity, full.id) is not None
    assert session.get(AliasCandidate, candidate.id).status == "approved"


def test_changed_entity_makes_approval_stale_without_embedding(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)
    short.source_book = "changed-book"
    session.commit()
    embedder = FakeEmbedClient()

    with pytest.raises(aliases.StaleApproval, match="same type and book"):
        asyncio.run(aliases.execute_approved_candidate(session, candidate.id, embedder))

    assert embedder.calls == []
    assert session.get(AliasCandidate, candidate.id).status == "stale"
    assert session.get(Entity, short.id) is not None


def test_changed_evidence_text_makes_approval_stale(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)
    chunk.content = "The reviewed source text changed after approval."
    session.commit()
    embedder = FakeEmbedClient()

    with pytest.raises(aliases.StaleApproval, match="approval is stale"):
        asyncio.run(aliases.execute_approved_candidate(session, candidate.id, embedder))

    assert embedder.calls == []
    assert session.get(AliasCandidate, candidate.id).status == "stale"
    assert session.get(Entity, short.id) is not None


def test_successful_execution_is_replay_safe(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)
    embedder = FakeEmbedClient()

    first = asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, embedder)
    )
    second = asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, embedder)
    )

    assert first["replay"] is False
    assert second == {"status": "merged", "candidate_id": candidate.id, "replay": True}
    assert len(embedder.calls) == 1
    assert session.get(AliasCandidate, candidate.id).status == "merged"


def test_same_model_embedding_is_updated_in_place_and_retryable(
    session: Session, monkeypatch
):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    current = Embedding(
        embeddable_kind="entity",
        embeddable_id=full.id,
        model="test-embedding",
        dim=1024,
        vector=[0.25] * 1024,
    )
    session.add(current)
    session.commit()
    current_id = current.id
    candidate = _candidate(session)
    _approve(session, candidate)

    original_commit = session.commit
    failed = False

    def fail_once():
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("forced commit failure")
        original_commit()

    monkeypatch.setattr(session, "commit", fail_once)
    with pytest.raises(RuntimeError, match="forced commit failure"):
        asyncio.run(
            aliases.execute_approved_candidate(session, candidate.id, FakeEmbedClient())
        )

    stored = session.get(Embedding, current_id)
    assert stored is not None
    assert stored.vector[0] == pytest.approx(0.25)
    assert session.get(AliasCandidate, candidate.id).status == "approved"
    assert session.get(Entity, short.id) is not None

    monkeypatch.setattr(session, "commit", original_commit)
    result = asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, FakeEmbedClient())
    )
    assert result["embedding_refreshed"] is True
    stored = session.get(Embedding, current_id)
    assert stored is not None
    assert stored.vector[0] == pytest.approx(0.75)


def test_approval_is_bound_to_report_hash_across_rescan_and_retry(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "H1 short")
    _mention(session, chunk, full, "H1 full")
    session.commit()
    candidate = _candidate(session)
    h1 = candidate.state_hash

    session.get(ChunkEntityMention, (chunk.id, short.id)).mention_text = "H2 short"
    session.commit()
    aliases.generate_candidates(session)
    candidate = session.get(AliasCandidate, candidate.id)
    h2 = candidate.state_hash
    assert h2 != h1

    for _ in range(2):
        with pytest.raises(aliases.StaleApproval, match="report changed"):
            aliases.approve_candidate(
                session,
                candidate.id,
                reviewer="human:reviewer",
                survivor_id=full.id,
                expected_state_hash=h1,
            )
        assert session.get(AliasCandidate, candidate.id).status == "pending"

    approved = aliases.approve_candidate(
        session,
        candidate.id,
        reviewer="human:reviewer",
        survivor_id=full.id,
        expected_state_hash=h2,
    )
    assert approved["approved_state_hash"] == h2
    assert approved["approved_by"] == "human:reviewer"


def test_rejection_persists_until_version_bound_reopen(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    h1 = candidate.state_hash

    rejected = aliases.reject_candidate(
        session, candidate.id, "human:reviewer", expected_state_hash=h1
    )
    assert rejected["status"] == "rejected"
    assert rejected["rejected_by"] == "human:reviewer"
    aliases.generate_candidates(session)
    assert session.get(AliasCandidate, candidate.id).status == "rejected"
    with pytest.raises(aliases.ApprovalRequired):
        asyncio.run(
            aliases.execute_approved_candidate(session, candidate.id, FakeEmbedClient())
        )
    with pytest.raises(aliases.AliasError, match="explicitly reopened"):
        aliases.approve_candidate(
            session,
            candidate.id,
            "human:reviewer",
            full.id,
            expected_state_hash=h1,
        )

    reopened = aliases.reopen_candidate(
        session, candidate.id, "human:operator", expected_state_hash=h1
    )
    assert reopened["status"] == "pending"
    assert reopened["reopened_by"] == "human:operator"
    assert reopened["rejected_by"] == "human:reviewer"


def test_stale_rejection_retry_does_not_replace_decision(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "H1 short")
    _mention(session, chunk, full, "H1 full")
    session.commit()
    candidate = _candidate(session)
    h1 = candidate.state_hash
    aliases.reject_candidate(session, candidate.id, "human:first", h1)
    session.get(ChunkEntityMention, (chunk.id, short.id)).mention_text = "H2 short"
    session.commit()
    aliases.generate_candidates(session)
    candidate = session.get(AliasCandidate, candidate.id)
    assert candidate.state_hash != h1

    with pytest.raises(aliases.StaleApproval, match="report changed"):
        aliases.reject_candidate(session, candidate.id, "human:second", h1)
    candidate = session.get(AliasCandidate, candidate.id)
    assert candidate.status == "rejected"
    assert candidate.rejected_by == "human:first"
    assert candidate.rejected_state_hash == h1


@pytest.mark.parametrize(
    "entity_type,field,value",
    [("location", "site", "cragmaw-castle"), ("event", "temporality", "historical")],
)
def test_merge_preserves_missing_survivor_spine_metadata(
    session: Session, entity_type: str, field: str, value: str
):
    short = _entity(session, entity_type, "Fall", **{field: value})
    full = _entity(session, entity_type, "The Great Fall")
    chunk = _chunk(session, "c1", "The Great Fall is also known as Fall.")
    _mention(session, chunk, short, "same")
    _mention(session, chunk, full, "same")
    session.commit()
    candidate = _candidate(session)
    assert candidate_view_value(candidate, field, short=True) == value
    _approve(session, candidate)
    asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, FakeEmbedClient())
    )
    assert getattr(session.get(Entity, full.id), field) == value


def candidate_view_value(candidate: AliasCandidate, field: str, short: bool):
    return aliases.candidate_view(candidate)[f"{'short' if short else 'full'}_{field}"]


def test_temporality_change_invalidates_old_report_approval(session: Session):
    short = _entity(session, "event", "Fall", temporality="historical")
    full = _entity(session, "event", "The Great Fall")
    chunk = _chunk(session, "c1", "The Great Fall is called Fall.")
    _mention(session, chunk, short, "short")
    _mention(session, chunk, full, "full")
    session.commit()
    candidate = _candidate(session)
    h1 = candidate.state_hash
    short.temporality = "present"
    session.commit()
    aliases.generate_candidates(session)
    with pytest.raises(aliases.StaleApproval, match="report changed"):
        aliases.approve_candidate(session, candidate.id, "human:reviewer", full.id, h1)


@pytest.mark.parametrize("survivor_has_current", [False, True])
def test_equal_mentions_refresh_only_when_current_embedding_missing(
    session: Session, survivor_has_current: bool
):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "same summary")
    _mention(session, chunk, full, "same summary")
    owner = full if survivor_has_current else short
    embedding = Embedding(
        embeddable_kind="entity",
        embeddable_id=owner.id,
        model="test-embedding",
        dim=1024,
        vector=[0.2] * 1024,
    )
    session.add(embedding)
    session.commit()
    existing_id = embedding.id
    candidate = _candidate(session)
    _approve(session, candidate)
    embedder = FakeEmbedClient()

    result = asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, embedder)
    )
    assert result["embedding_refreshed"] is (not survivor_has_current)
    assert len(embedder.calls) == (0 if survivor_has_current else 1)
    survivor_embedding = session.exec(
        select(Embedding).where(Embedding.embeddable_id == full.id)
    ).one()
    if survivor_has_current:
        assert survivor_embedding.id == existing_id
        assert survivor_embedding.vector[0] == pytest.approx(0.2)
    else:
        assert survivor_embedding.vector[0] == pytest.approx(0.75)


def test_missing_current_embedding_provider_failure_is_retryable(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "same summary")
    _mention(session, chunk, full, "same summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)

    class FailingEmbedClient:
        model = "test-embedding"

        async def embed_batch(self, _texts):
            raise RuntimeError("embedding unavailable")

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        asyncio.run(
            aliases.execute_approved_candidate(
                session, candidate.id, FailingEmbedClient()
            )
        )
    assert session.get(AliasCandidate, candidate.id).status == "approved"
    result = asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, FakeEmbedClient())
    )
    assert result["embedding_refreshed"] is True


def test_database_serialization_conflict_retries_without_reembedding(
    session: Session, monkeypatch
):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short")
    _mention(session, chunk, full, "full")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)
    embedder = FakeEmbedClient()
    original_apply = aliases._apply_merge
    attempts = 0

    class SerializationFailure(Exception):
        sqlstate = "40001"

    def apply_with_one_conflict(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DBAPIError("UPDATE", {}, SerializationFailure(), False)
        return original_apply(*args)

    monkeypatch.setattr(aliases, "_apply_merge", apply_with_one_conflict)
    result = asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, embedder)
    )
    assert result["status"] == "merged"
    assert attempts == 2
    assert len(embedder.calls) == 1


def test_sequential_merges_union_edge_conflicts_and_source_chunks(session: Session):
    short_a = _entity(session, "npc", "Gundren")
    short_b = _entity(session, "npc", "Rockseeker")
    full = _entity(session, "npc", "Gundren Rockseeker")
    target = _entity(session, "location", "Wave Echo Cave")
    chunks = [
        _chunk(session, f"c{index}", "Gundren Rockseeker, Gundren, Rockseeker.")
        for index in range(1, 4)
    ]
    for entity in (short_a, short_b, full):
        _mention(session, chunks[0], entity, entity.name)
    session.add_all(
        [
            Relationship(
                from_entity_id=full.id,
                to_entity_id=target.id,
                rel_type="FOUND",
                properties={"x": "full"},
                chunk_id=chunks[0].id,
            ),
            Relationship(
                from_entity_id=short_a.id,
                to_entity_id=target.id,
                rel_type="FOUND",
                properties={"x": "A"},
                chunk_id=chunks[1].id,
            ),
            Relationship(
                from_entity_id=short_b.id,
                to_entity_id=target.id,
                rel_type="FOUND",
                properties={"x": "B"},
                chunk_id=chunks[2].id,
            ),
        ]
    )
    session.commit()
    report = aliases.generate_candidates(session)
    candidates = {
        row["short_entity_id"]: session.get(AliasCandidate, row["id"])
        for row in report["candidates"]
    }
    first = candidates[short_a.id]
    _approve(session, first)
    asyncio.run(
        aliases.execute_approved_candidate(session, first.id, FakeEmbedClient())
    )

    aliases.generate_candidates(session)
    second = session.get(AliasCandidate, candidates[short_b.id].id)
    _approve(session, second)
    asyncio.run(
        aliases.execute_approved_candidate(session, second.id, FakeEmbedClient())
    )

    edge = session.exec(select(Relationship)).one()
    assert edge.properties["x"] == "full"
    assert edge.properties["_alias_merge_conflicts"]["x"] == ["full", "A", "B"]
    assert edge.properties["_alias_merge_chunk_ids"] == sorted(
        chunk.id for chunk in chunks
    )


def _principal(
    authority: Authority, kind: PrincipalKind, groups: tuple[str, ...]
) -> Principal:
    return Principal(
        subject="verified:reviewer",
        actor=(),
        scope=(),
        groups=groups,
        email="reviewer@example.test" if kind is PrincipalKind.HUMAN else None,
        kind=kind,
        authority=authority,
    )


def _alias_client(
    session: Session, principal: Principal, embedder: FakeEmbedClient
) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_principal] = lambda: principal
    app.dependency_overrides[get_embedding_client] = lambda: embedder
    return TestClient(app)


@pytest.mark.parametrize(
    "principal",
    [
        _principal(Authority.ANONYMOUS, PrincipalKind.HUMAN, ("operators",)),
        _principal(Authority.STANDING, PrincipalKind.WORKLOAD, ("operators",)),
        _principal(Authority.STANDING, PrincipalKind.HUMAN, ()),
    ],
)
def test_alias_http_mutations_deny_unverified_machine_and_unauthorized_callers(
    session: Session, principal: Principal
):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short")
    _mention(session, chunk, full, "full")
    session.commit()
    candidate = _candidate(session)
    embedder = FakeEmbedClient()
    client = _alias_client(session, principal, embedder)
    decision = {"expected_state_hash": candidate.state_hash}

    responses = [
        client.post("/api/grimoire/alias-candidates/scan"),
        client.post(
            f"/api/grimoire/alias-candidates/{candidate.id}/approve",
            json={**decision, "survivor_entity_id": full.id},
            headers={"Cf-Access-Authenticated-User-Email": "spoof@example.test"},
        ),
        client.post(
            f"/api/grimoire/alias-candidates/{candidate.id}/reject", json=decision
        ),
        client.post(
            f"/api/grimoire/alias-candidates/{candidate.id}/reopen", json=decision
        ),
        client.post(f"/api/grimoire/alias-candidates/{candidate.id}/execute"),
    ]
    assert [response.status_code for response in responses] == [403] * 5
    assert session.get(AliasCandidate, candidate.id).status == "pending"
    assert session.get(Entity, short.id) is not None
    assert embedder.calls == []


def test_authorized_human_http_decision_uses_verified_subject_and_executes(
    session: Session,
):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short")
    _mention(session, chunk, full, "full")
    session.commit()
    candidate = _candidate(session)
    embedder = FakeEmbedClient()
    client = _alias_client(
        session,
        _principal(Authority.STANDING, PrincipalKind.HUMAN, ("operators",)),
        embedder,
    )

    rejected = client.post(
        f"/api/grimoire/alias-candidates/{candidate.id}/reject",
        json={"expected_state_hash": candidate.state_hash},
    )
    assert rejected.status_code == 200
    assert rejected.json()["rejected_by"] == "verified:reviewer"
    assert rejected.json()["rejected_at"] is not None
    reopened = client.post(
        f"/api/grimoire/alias-candidates/{candidate.id}/reopen",
        json={"expected_state_hash": candidate.state_hash},
    )
    assert reopened.status_code == 200
    assert reopened.json()["reopened_by"] == "verified:reviewer"

    approved = client.post(
        f"/api/grimoire/alias-candidates/{candidate.id}/approve",
        json={
            "survivor_entity_id": full.id,
            "expected_state_hash": candidate.state_hash,
        },
    )
    assert approved.status_code == 200
    assert approved.json()["approved_by"] == "verified:reviewer"
    executed = client.post(f"/api/grimoire/alias-candidates/{candidate.id}/execute")
    assert executed.status_code == 200
    assert executed.json()["status"] == "merged"
    assert len(embedder.calls) == 1
