"""Tests for shared knowledge-pipeline constants and slug normalization."""

import pytest

from knowledge.gardener import GARDENER_VERSION, MAX_GARDENER_RETRIES, _slugify


class TestSlugify:
    def test_ascii_text(self):
        assert _slugify("Hello World") == "hello-world"

    def test_unicode_nfkd_strips_accents(self):
        assert _slugify("Héllo") == "hello"

    def test_empty_string_returns_note(self):
        assert _slugify("") == "note"

    def test_multiple_special_chars_collapse_to_single_hyphen(self):
        assert _slugify("Hello!! World") == "hello-world"


class TestSurvivingConstants:
    def test_gardener_version_stamp(self):
        assert GARDENER_VERSION == "claude-sonnet-4-6@v1"

    def test_max_retries_ceiling(self):
        assert MAX_GARDENER_RETRIES == 3


@pytest.mark.parametrize("distinct_raws", [False, True])
def test_clone_merge_dry_run_provenance_and_idempotence(tmp_path, distinct_raws):
    from sqlalchemy import text
    from sqlmodel import Session, SQLModel, create_engine, select
    from knowledge.gardener import merge_clones
    from knowledge.models import (
        AtomRawProvenance,
        Chunk,
        Dispute,
        Note,
        NoteLink,
        RawInput,
    )
    from knowledge.store import provenance_for_notes

    engine = create_engine(f"sqlite:///{tmp_path / 'clones.db'}").execution_options(
        schema_translate_map={"knowledge": None}
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            Note.__table__,
            Chunk.__table__,
            RawInput.__table__,
            AtomRawProvenance.__table__,
            Dispute.__table__,
            NoteLink.__table__,
        ],
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX atom_raw_provenance_real "
                "ON atom_raw_provenance (atom_fk, raw_fk) "
                "WHERE atom_fk IS NOT NULL AND raw_fk IS NOT NULL"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX atom_raw_provenance_atom_sentinel "
                "ON atom_raw_provenance (atom_fk) "
                "WHERE raw_fk IS NULL AND gardener_version = 'pre-migration'"
            )
        )
    with Session(engine) as session:
        raw = RawInput(raw_id="raw", path="raw", content_hash="raw", source="test")
        session.add(raw)
        session.flush()
        other_raw = RawInput(
            raw_id="other", path="other", content_hash="other", source="test"
        )
        session.add(other_raw)
        session.flush()
        for key, confidence, scope, state in [
            ("winner", 0.9, "repo:acme/repo", "verified"),
            ("loser", 0.8, "repo:acme/repo", "verified"),
            ("unverified", 1, "repo:acme/repo", "unverified"),
            ("other", 1, "repo:other/repo", "verified"),
            ("disputed", 1, "repo:acme/repo", "disputed"),
            ("invalid", 1, "repo:acme/repo", "invalidated"),
        ]:
            note = Note(
                note_id=key,
                path=key,
                title=key,
                content_hash=key,
                type="fact",
                scope=scope,
                confidence=confidence,
                verification_state=state,
            )
            session.add(note)
            session.flush()
            session.add(
                Chunk(
                    note_fk=note.id,
                    chunk_index=0,
                    chunk_text=key,
                    embedding=[1.0] + [0.0] * 1023,
                )
            )
            session.add(
                AtomRawProvenance(
                    atom_fk=note.id,
                    raw_fk=other_raw.id
                    if distinct_raws and key in {"loser", "unverified"}
                    else raw.id,
                    derived_note_id=key,
                    gardener_version="original",
                )
            )
        session.commit()
        plan = merge_clones(session)
        assert plan == [{"survivor": "winner", "invalidated": ["loser", "unverified"]}]
        loser = session.exec(select(Note).where(Note.note_id == "loser")).one()
        assert loser.verification_state == "verified"
        assert len(session.exec(select(AtomRawProvenance)).all()) == 6
        assert merge_clones(session, apply=True) == plan
        assert loser.verification_state == "invalidated"
        assert loser.extra["merged_into"] == "winner"
        assert len(session.exec(select(AtomRawProvenance)).all()) == (
            7 if distinct_raws else 6
        )
        assert len(provenance_for_notes(session, ["winner"])["winner"]) == (
            2 if distinct_raws else 1
        )
        assert merge_clones(session, apply=True) == []
        assert len(session.exec(select(AtomRawProvenance)).all()) == (
            7 if distinct_raws else 6
        )
        winner = session.exec(select(Note).where(Note.note_id == "winner")).one()
        for note in (winner, loser):
            session.add(
                AtomRawProvenance(
                    atom_fk=note.id,
                    raw_fk=None,
                    derived_note_id=note.note_id,
                    gardener_version="pre-migration",
                )
            )
        loser.verification_state = "verified"
        loser.valid_until = None
        session.add(loser)
        session.commit()
        assert merge_clones(session, apply=True) == [
            {"survivor": "winner", "invalidated": ["loser"]}
        ]
        assert len(session.exec(select(AtomRawProvenance)).all()) == (
            9 if distinct_raws else 8
        )
        assert merge_clones(session, apply=True) == []


def test_multichunk_facts_require_full_coverage(tmp_path):
    from sqlmodel import Session, SQLModel, create_engine
    from knowledge.gardener import merge_clones
    from knowledge.models import Chunk, Dispute, Note

    engine = create_engine(f"sqlite:///{tmp_path / 'multichunk.db'}").execution_options(
        schema_translate_map={"knowledge": None}
    )
    SQLModel.metadata.create_all(
        engine, tables=[Note.__table__, Chunk.__table__, Dispute.__table__]
    )
    with Session(engine) as session:
        for key, vectors, confidence in [
            ("complete", [[1.0, 0.0], [0.0, 1.0]], 0.9),
            ("clone", [[0.0, 1.0], [1.0, 0.0]], 0.8),
            ("partial", [[1.0, 0.0]], 1.0),
        ]:
            note = Note(
                note_id=key,
                path=key,
                title=key,
                content_hash=key,
                type="fact",
                scope="repo:acme/repo",
                confidence=confidence,
                verification_state="verified",
            )
            session.add(note)
            session.flush()
            for i, vector in enumerate(vectors):
                session.add(
                    Chunk(
                        note_fk=note.id,
                        chunk_index=i,
                        chunk_text=key,
                        embedding=vector + [0.0] * 1022,
                    )
                )
        session.commit()
        assert merge_clones(session) == [
            {"survivor": "complete", "invalidated": ["clone"]}
        ]


def test_clone_merge_never_touches_deployment_observations(tmp_path):
    """Observations are identical by construction; only the ordinary pair merges."""
    from datetime import datetime, timedelta, timezone

    from sqlmodel import Session, SQLModel, create_engine, select
    from knowledge.gardener import merge_clones
    from knowledge.models import (
        AtomRawProvenance,
        Chunk,
        Dispute,
        Note,
        NoteLink,
        RawInput,
    )

    engine = create_engine(
        f"sqlite:///{tmp_path / 'observation-clones.db'}"
    ).execution_options(schema_translate_map={"knowledge": None})
    SQLModel.metadata.create_all(
        engine,
        tables=[
            Note.__table__,
            Chunk.__table__,
            RawInput.__table__,
            AtomRawProvenance.__table__,
            Dispute.__table__,
            NoteLink.__table__,
        ],
    )
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    with Session(engine) as session:
        for key, source, scope, valid_until in [
            ("obs-a", "deployment-observation", "environment:homelab", future),
            ("obs-b", "deployment-observation", "environment:homelab", future),
            ("plain-a", None, "repo:acme/repo", None),
            ("plain-b", None, "repo:acme/repo", None),
        ]:
            note = Note(
                note_id=key,
                path=key,
                title=key,
                content_hash=key,
                type="fact",
                scope=scope,
                source=source,
                valid_until=valid_until,
                confidence=0.9 if key.endswith("-a") else 0.8,
                verification_state="verified",
            )
            session.add(note)
            session.flush()
            session.add(
                Chunk(
                    note_fk=note.id,
                    chunk_index=0,
                    chunk_text=key,
                    embedding=[1.0] + [0.0] * 1023,
                )
            )
        session.commit()

        plans = merge_clones(session)
        assert plans == [{"survivor": "plain-a", "invalidated": ["plain-b"]}]

        assert merge_clones(session, apply=True) == plans
        session.expire_all()
        states = {
            note.note_id: note.verification_state
            for note in session.exec(select(Note)).all()
        }
        assert states["obs-a"] == "verified"
        assert states["obs-b"] == "verified"
        assert states["plain-b"] == "invalidated"


@pytest.fixture
def capped_clone_session(tmp_path):
    from sqlmodel import Session, SQLModel, create_engine
    from knowledge.models import (
        AtomRawProvenance,
        Chunk,
        Dispute,
        Note,
        NoteLink,
        RawInput,
    )

    engine = create_engine(
        f"sqlite:///{tmp_path / 'capped-clones.db'}"
    ).execution_options(schema_translate_map={"knowledge": None})
    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (Note, Chunk, RawInput, AtomRawProvenance, Dispute, NoteLink)
        ],
    )
    with Session(engine) as session:
        # Reverse lexical order makes Note.id cluster ordering observable.
        for axis, name in enumerate(["z", "m", "a"]):
            for role in ["winner", "loser"]:
                key = f"{name}-{role}"
                note = Note(
                    note_id=key,
                    path=key,
                    title=key,
                    content_hash=key,
                    type="fact",
                    scope="repo:acme/repo",
                    verification_state="verified",
                    confidence=0.9 if role == "winner" else 0.8,
                    extra={"original": key},
                )
                raw = RawInput(raw_id=key, path=key, content_hash=key, source="test")
                session.add_all([note, raw])
                session.flush()
                vector = [0.0] * 1024
                vector[axis] = 1.0
                session.add_all(
                    [
                        Chunk(
                            note_fk=note.id,
                            chunk_index=0,
                            chunk_text=key,
                            embedding=vector,
                        ),
                        AtomRawProvenance(
                            atom_fk=note.id,
                            raw_fk=raw.id,
                            derived_note_id=key,
                            gardener_version="original",
                        ),
                    ]
                )
        session.commit()
        yield session
    engine.dispose()


def test_clone_merge_cap_defers_untouched_clusters_and_makes_progress(
    capped_clone_session,
):
    from datetime import datetime
    from sqlmodel import select
    from knowledge.gardener import merge_clones
    from knowledge.models import AtomRawProvenance, Chunk, Note, NoteLink

    session = capped_clone_session

    def snapshot(model):
        return [
            row.model_dump()
            for row in session.exec(select(model).order_by(model.id)).all()
        ]

    before = {
        model: snapshot(model) for model in (Note, Chunk, AtomRawProvenance, NoteLink)
    }
    counts = {}
    plans = merge_clones(session, max_merges=2, counts=counts)
    assert plans == [
        {"survivor": "z-winner", "invalidated": ["z-loser"]},
        {"survivor": "m-winner", "invalidated": ["m-loser"]},
    ]
    assert counts == {
        "merged": 2,
        "skipped": 1,
        "clusters_found": 3,
        "invalidated": 2,
        "contested_skipped": 0,
    }
    assert {model: snapshot(model) for model in before} == before
    assert merge_clones(session, apply=True, max_merges=2, counts=counts) == plans
    assert counts["merged"] == 2 and counts["skipped"] == 1
    notes = session.exec(select(Note).order_by(Note.id)).all()
    assert len(notes) == 6
    for loser in (notes[1], notes[3]):
        assert loser.verification_state == "invalidated"
        assert isinstance(loser.valid_until, datetime)
        assert loser.deleted_at is None
    assert [row.model_dump() for row in notes[4:]] == before[Note][4:]
    assert snapshot(Chunk) == before[Chunk]
    links = session.exec(select(NoteLink)).all()
    assert {(link.src_note_fk, link.target_id, link.edge_type) for link in links} == {
        (notes[0].id, "z-loser", "supersedes"),
        (notes[2].id, "m-loser", "supersedes"),
    }
    provenance = session.exec(
        select(AtomRawProvenance).order_by(AtomRawProvenance.id)
    ).all()
    assert len(provenance) == 8
    assert [row.model_dump() for row in provenance[:6]] == before[AtomRawProvenance]
    assert {row.atom_fk for row in provenance[6:]} == {notes[0].id, notes[2].id}
    assert merge_clones(session, apply=True, max_merges=2, counts=counts) == [
        {"survivor": "a-winner", "invalidated": ["a-loser"]},
    ]
    assert counts["merged"] == 1 and counts["skipped"] == 0
    assert merge_clones(session, apply=True, max_merges=2, counts=counts) == []
    assert counts["merged"] == 0 and counts["skipped"] == 0
    assert len(session.exec(select(Note)).all()) == 6


@pytest.mark.parametrize("apply", [False, True])
def test_clone_merge_span_counts(capped_clone_session, monkeypatch, apply):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    import knowledge.gardener as gardener

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(gardener, "tracer", provider.get_tracer("clone-test"))
    try:
        gardener.merge_clones(capped_clone_session, apply=apply, max_merges=2)
        (span,) = exporter.get_finished_spans()
        assert span.name == "knowledge.merge_clones"
        assert dict(span.attributes) == {
            "apply": apply,
            "max_merges": 2,
            "merged": 2,
            "skipped": 1,
            "clusters_found": 3,
            "invalidated": 2,
            "contested_skipped": 0,
        }
    finally:
        provider.shutdown()


def test_clone_merge_span_ends_with_counts_and_error_on_commit_failure(
    capped_clone_session, monkeypatch
):
    from opentelemetry.trace import StatusCode
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    import knowledge.gardener as gardener

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(gardener, "tracer", provider.get_tracer("clone-test"))

    def fail_commit():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(capped_clone_session, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            gardener.merge_clones(capped_clone_session, apply=True, max_merges=2)
        (span,) = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes["merged"] == 2
        assert span.attributes["skipped"] == 1
        assert span.events[0].name == "exception"
        assert span.events[0].attributes["exception.message"] == "commit failed"
    finally:
        capped_clone_session.rollback()
        provider.shutdown()


@pytest.mark.parametrize("seed", [6719, 10, 50])
def test_gardener_vectorised_clusters_match_scalar_rule(seed):
    import numpy as np
    from knowledge.clones import clone_clusters
    from knowledge.gardener import _gardener_clone_clusters

    rng = np.random.default_rng(seed)
    vectors = rng.normal(size=(12, 1024))
    items = []
    for i in range(80):
        chunks = [(vectors[i % 12] + rng.normal(scale=0.03, size=1024)).tolist()]
        if i % 3 == 0:
            chunks.append(vectors[(i + 1) % 12].tolist())
        items.append(
            {
                "note_id": f"note-{80 - i:03d}",
                "scope": (f"repo:scope-{i % 2}", "private"),
                "embeddings": chunks,
                "confidence": float(rng.random()),
                "verification_state": "verified" if i % 4 else "unverified",
                "disputed": i % 7 == 0,
            }
        )
    # Partial coverage must not merge a multi-chunk note with a one-chunk note.
    items.extend(
        [
            {
                "note_id": "whole",
                "scope": "coverage",
                "embeddings": vectors[:2].tolist(),
            },
            {
                "note_id": "partial",
                "scope": "coverage",
                "embeddings": vectors[:1].tolist(),
            },
            {
                "note_id": "whole-copy",
                "scope": "coverage",
                "embeddings": vectors[:2].tolist(),
            },
        ]
    )
    assert _gardener_clone_clusters(items) == clone_clusters(items)


def test_gardener_vectorised_threshold_and_invalid_vectors_match_scalar_rule():
    import math
    from knowledge.clones import CLONE_COSINE_THRESHOLD, clone_clusters
    from knowledge.gardener import _gardener_clone_clusters

    items = []
    for i, similarity in enumerate(
        [
            CLONE_COSINE_THRESHOLD - 1e-13,
            CLONE_COSINE_THRESHOLD,
            CLONE_COSINE_THRESHOLD + 1e-13,
        ]
    ):
        items.extend(
            [
                {"note_id": f"base-{i}", "scope": i, "embeddings": [[1.0, 0.0]]},
                {
                    "note_id": f"boundary-{i}",
                    "scope": i,
                    "embeddings": [[similarity, math.sqrt(1 - similarity**2)]],
                },
            ]
        )
    for i, vector in enumerate(
        [
            None,
            [],
            [0.0, 0.0],
            [float("nan"), 1.0],
            [float("inf"), 1.0],
            [1e90, 0.0],
            [1e-90, 0.0],
            [1.0],
        ]
    ):
        items.extend(
            [
                {
                    "note_id": f"invalid-{i}",
                    "scope": f"invalid-{i}",
                    "embeddings": [vector],
                },
                {
                    "note_id": f"peer-{i}",
                    "scope": f"invalid-{i}",
                    "embeddings": [[1.0, 0.0]],
                },
            ]
        )
    assert _gardener_clone_clusters(items) == clone_clusters(items)
