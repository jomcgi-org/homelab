"""PostgreSQL concurrency regressions for review-approved alias merges."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select

from grimoire import aliases
from grimoire.models import (
    AliasCandidate,
    ChunkEntityMention,
    Embedding,
    Entity,
    KnowledgeChunk,
    Relationship,
)

WAIT_SECONDS = 5


class FakeEmbedClient:
    model = "test-embedding"

    async def embed_batch(self, texts):
        return [[0.75] * 1024 for _ in texts]


@pytest.fixture
def lane(pg):
    identity = uuid4().hex
    book = f"alias-race-{identity}"
    engines = []

    def engine(actor):
        value = create_engine(
            pg.url,
            poolclass=NullPool,
            connect_args={
                "application_name": f"alias-{identity}-{actor}",
                "connect_timeout": WAIT_SECONDS,
                "options": "-c lock_timeout=15000 -c statement_timeout=20000",
            },
        )
        engines.append(value)
        return value

    observer = engine("observer")
    try:
        yield SimpleNamespace(book=book, engine=engine, observer=observer)
    finally:
        with observer.begin() as connection:
            entity_ids = (
                connection.execute(select(Entity.id).where(Entity.source_book == book))
                .scalars()
                .all()
            )
            connection.execute(
                delete(AliasCandidate).where(AliasCandidate.source_book == book)
            )
            if entity_ids:
                connection.execute(
                    delete(Embedding).where(
                        Embedding.embeddable_kind == "entity",
                        Embedding.embeddable_id.in_(entity_ids),
                    )
                )
                connection.execute(
                    delete(Relationship).where(
                        Relationship.from_entity_id.in_(entity_ids)
                        | Relationship.to_entity_id.in_(entity_ids)
                    )
                )
                connection.execute(
                    delete(ChunkEntityMention).where(
                        ChunkEntityMention.entity_id.in_(entity_ids)
                    )
                )
                connection.execute(delete(Entity).where(Entity.id.in_(entity_ids)))
            connection.execute(
                delete(KnowledgeChunk).where(KnowledgeChunk.book_id == book)
            )
        for value in engines:
            value.dispose()


@contextmanager
def _session(engine):
    with Session(engine) as session:
        yield session


def _entity(session, lane, name):
    row = Entity(entity_type="npc", name=name, source_book=lane.book)
    session.add(row)
    session.flush()
    return row


def _chunk(session, lane):
    row = KnowledgeChunk(
        book_id=lane.book,
        chunk_ref=uuid4().hex,
        content="Gundren Rockseeker, Gundren, and Rockseeker appear together.",
    )
    session.add(row)
    session.flush()
    return row


def _mention(session, chunk, entity, value):
    session.add(
        ChunkEntityMention(chunk_id=chunk.id, entity_id=entity.id, mention_text=value)
    )


def _seed_pair(lane):
    with _session(lane.observer) as session:
        short = _entity(session, lane, "Gundren")
        full = _entity(session, lane, "Gundren Rockseeker")
        chunk = _chunk(session, lane)
        _mention(session, chunk, short, "short")
        _mention(session, chunk, full, "full")
        session.commit()
        report = aliases.generate_candidates(session)
        candidate = next(
            row
            for row in report["candidates"]
            if row["short_entity_id"] == short.id and row["full_entity_id"] == full.id
        )
        aliases.approve_candidate(
            session,
            candidate["id"],
            "human:reviewer",
            full.id,
            candidate["state_hash"],
        )
        return SimpleNamespace(
            short_id=short.id,
            full_id=full.id,
            chunk_id=chunk.id,
            candidate_id=candidate["id"],
        )


def test_child_edit_commits_before_execute_validation_and_stales_approval(lane):
    pair = _seed_pair(lane)
    writer_ready = Event()
    release_writer = Event()

    def edit_child():
        with _session(lane.engine("child-edit")) as session:
            mention = session.get(
                ChunkEntityMention, (pair.chunk_id, pair.short_id), with_for_update=True
            )
            mention.mention_text = "edited after review"
            session.flush()
            writer_ready.set()
            assert release_writer.wait(WAIT_SECONDS)
            session.commit()

    def execute():
        with _session(lane.engine("execute")) as session:
            return asyncio.run(
                aliases.execute_approved_candidate(
                    session, pair.candidate_id, FakeEmbedClient()
                )
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(edit_child)
        assert writer_ready.wait(WAIT_SECONDS)
        execution = pool.submit(execute)
        with pytest.raises(FutureTimeout):
            execution.result(timeout=0.2)
        release_writer.set()
        writer.result(timeout=WAIT_SECONDS)
        with pytest.raises(aliases.StaleApproval):
            execution.result(timeout=WAIT_SECONDS)

    with _session(lane.observer) as session:
        assert session.get(AliasCandidate, pair.candidate_id).status == "stale"
        assert session.get(Entity, pair.short_id) is not None


def test_scan_waits_for_execute_and_cannot_overwrite_merged_status(lane, monkeypatch):
    pair = _seed_pair(lane)
    merge_locked = Event()
    release_merge = Event()
    original_lock = aliases._lock_candidate

    def lock_and_pause(session, candidate_id):
        row = original_lock(session, candidate_id)
        if candidate_id == pair.candidate_id and not merge_locked.is_set():
            merge_locked.set()
            assert release_merge.wait(WAIT_SECONDS)
        return row

    monkeypatch.setattr(aliases, "_lock_candidate", lock_and_pause)

    def execute():
        with _session(lane.engine("execute")) as session:
            return asyncio.run(
                aliases.execute_approved_candidate(
                    session, pair.candidate_id, FakeEmbedClient()
                )
            )

    def scan():
        with _session(lane.engine("scan")) as session:
            return aliases.generate_candidates(session)

    with ThreadPoolExecutor(max_workers=2) as pool:
        execution = pool.submit(execute)
        assert merge_locked.wait(WAIT_SECONDS)
        scanning = pool.submit(scan)
        with pytest.raises(FutureTimeout):
            scanning.result(timeout=0.2)
        release_merge.set()
        assert execution.result(timeout=WAIT_SECONDS)["status"] == "merged"
        scanning.result(timeout=WAIT_SECONDS)

    with _session(lane.observer) as session:
        assert session.get(AliasCandidate, pair.candidate_id).status == "merged"


def test_prepare_refresh_preserves_concurrently_completed_merge(lane, monkeypatch):
    pair = _seed_pair(lane)
    first_read = Event()
    release_first = Event()
    original_lock = aliases._lock_candidate

    def pause_first_before_lock(session, candidate_id):
        if candidate_id == pair.candidate_id and not first_read.is_set():
            first_read.set()
            assert release_first.wait(WAIT_SECONDS)
        return original_lock(session, candidate_id)

    monkeypatch.setattr(aliases, "_lock_candidate", pause_first_before_lock)

    def execute(actor):
        with _session(lane.engine(actor)) as session:
            return asyncio.run(
                aliases.execute_approved_candidate(
                    session, pair.candidate_id, FakeEmbedClient()
                )
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, "prepare-first")
        assert first_read.wait(WAIT_SECONDS)
        second = pool.submit(execute, "merge-second")
        try:
            assert second.result(timeout=WAIT_SECONDS)["replay"] is False
        finally:
            release_first.set()
        assert first.result(timeout=WAIT_SECONDS) == {
            "status": "merged",
            "candidate_id": pair.candidate_id,
            "replay": True,
        }

    with _session(lane.observer) as session:
        assert session.get(AliasCandidate, pair.candidate_id).status == "merged"
        assert session.get(Entity, pair.short_id) is None


def test_concurrent_scans_publish_one_candidate_without_lost_updates(lane):
    with _session(lane.observer) as session:
        short = _entity(session, lane, "Gundren")
        full = _entity(session, lane, "Gundren Rockseeker")
        chunk = _chunk(session, lane)
        _mention(session, chunk, short, "short")
        _mention(session, chunk, full, "full")
        session.commit()

    def scan(actor):
        with _session(lane.engine(actor)) as session:
            return aliases.generate_candidates(session)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(scan, ("scan-a", "scan-b")))
    assert sorted(result["created"] for result in results) == [0, 1]
    with _session(lane.observer) as session:
        rows = session.exec(
            select(AliasCandidate).where(AliasCandidate.source_book == lane.book)
        ).all()
        assert len(rows) == 1
        assert rows[0].status == "pending"


def test_overlapping_merges_serialize_and_stale_the_losing_snapshot(lane):
    with _session(lane.observer) as session:
        short_a = _entity(session, lane, "Gundren")
        short_b = _entity(session, lane, "Rockseeker")
        full = _entity(session, lane, "Gundren Rockseeker")
        chunk = _chunk(session, lane)
        for entity in (short_a, short_b, full):
            _mention(session, chunk, entity, entity.name)
        session.commit()
        report = aliases.generate_candidates(session)
        candidate_ids = []
        for candidate in (
            row for row in report["candidates"] if row["source_book"] == lane.book
        ):
            aliases.approve_candidate(
                session,
                candidate["id"],
                "human:reviewer",
                full.id,
                candidate["state_hash"],
            )
            candidate_ids.append(candidate["id"])
        full_id = full.id

    def execute(actor, candidate_id):
        with _session(lane.engine(actor)) as session:
            try:
                return asyncio.run(
                    aliases.execute_approved_candidate(
                        session, candidate_id, FakeEmbedClient()
                    )
                )["status"]
            except aliases.StaleApproval:
                return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda item: execute(*item),
                (("merge-a", candidate_ids[0]), ("merge-b", candidate_ids[1])),
            )
        )
    assert sorted(results) == ["merged", "stale"]
    with _session(lane.observer) as session:
        statuses = sorted(
            session.get(AliasCandidate, candidate_id).status
            for candidate_id in candidate_ids
        )
        assert statuses == ["merged", "stale"]
        assert session.get(Entity, full_id) is not None
