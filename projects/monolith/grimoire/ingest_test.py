"""Tests for grimoire.ingest: manifest parsing + the S3 -> knowledge_chunk
+ embedding upsert loop. boto3 is never imported here; the fake S3 client
below matches only the two calls load_chunks actually makes
(list_objects_v2, get_object)."""

import asyncio
import json

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from grimoire import ingest
from grimoire.models import Embedding, KnowledgeChunk


@pytest.fixture(name="session")
def session_fixture():
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
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


class FakeS3Client:
    """Minimal fake matching only the calls load_chunks makes.

    ``manifests`` maps key -> ndjson text (already encoded as bytes on
    read, mirroring a real boto3 StreamingBody).
    """

    def __init__(self, manifests: dict[str, str]):
        self.manifests = manifests

    def list_objects_v2(self, Bucket: str, Prefix: str, **_kwargs):
        keys = [k for k in self.manifests if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_object(self, Bucket: str, Key: str):
        class _Body:
            def __init__(self, data: bytes):
                self._data = data

            def read(self):
                return self._data

        return {"Body": _Body(self.manifests[Key].encode("utf-8"))}


class FakeEmbedClient:
    """Returns a fixed 1024-dim vector per text, tagged with a call count so
    tests can assert re-embeds happened (or didn't)."""

    model = "voyage-4-nano"

    def __init__(self):
        self.calls = 0

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.1] * 1024 for _ in texts]


def _line(chunk_ref: str, content: str, section_path: str | None = None) -> str:
    obj = {"chunk_ref": chunk_ref, "content": content}
    if section_path is not None:
        obj["section_path"] = section_path
    return json.dumps(obj)


def _run(coro):
    return asyncio.run(coro)


# --- parse_manifest_lines -----------------------------------------------


def test_parse_manifest_lines_valid_and_optional_fields():
    lines = [
        _line("phb-c3-014", "Wizards cast spells.", "Chapter 3 > Classes > Wizard"),
        _line("phb-c3-015", "Fighters fight."),
    ]
    valid, errors = ingest.parse_manifest_lines("phb", lines)

    assert errors == 0
    assert valid == [
        {
            "chunk_ref": "phb-c3-014",
            "content": "Wizards cast spells.",
            "section_path": "Chapter 3 > Classes > Wizard",
        },
        {"chunk_ref": "phb-c3-015", "content": "Fighters fight.", "section_path": None},
    ]


def test_parse_manifest_lines_tolerates_bad_lines():
    lines = [
        "not json at all {",
        '{"chunk_ref": "ok-1", "content": "fine"}',
        '{"content": "missing chunk_ref"}',
        '{"chunk_ref": "no-content"}',
        '{"chunk_ref": "", "content": "empty ref"}',
        "",
        "   ",
    ]
    valid, errors = ingest.parse_manifest_lines("book", lines)

    assert [v["chunk_ref"] for v in valid] == ["ok-1"]
    assert errors == 4


# --- load_chunks ----------------------------------------------------------


def test_load_chunks_fresh_load_creates_chunks_and_embeddings(session: Session):
    manifest = "\n".join(
        [
            _line("c1", "content one"),
            _line("c2", "content two", "Ch1"),
        ]
    )
    s3 = FakeS3Client({"chunks/phb.ndjson": manifest})
    embedder = FakeEmbedClient()

    summary = _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    assert summary == {
        "books": 1,
        "chunks_upserted": 2,
        "chunks_embedded": 2,
        "errors": 0,
    }

    chunks = session.execute(select(KnowledgeChunk)).scalars().all()
    assert {c.chunk_ref for c in chunks} == {"c1", "c2"}
    embeddings = session.execute(select(Embedding)).scalars().all()
    assert len(embeddings) == 2
    assert {e.embeddable_kind for e in embeddings} == {"chunk"}
    assert {e.model for e in embeddings} == {"voyage-4-nano"}
    assert {e.dim for e in embeddings} == {1024}


def test_load_chunks_idempotent_rerun_embeds_nothing(session: Session):
    manifest = "\n".join([_line("c1", "content one")])
    s3 = FakeS3Client({"chunks/phb.ndjson": manifest})
    embedder = FakeEmbedClient()

    _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))
    chunk_count_before = len(session.execute(select(KnowledgeChunk)).scalars().all())
    embedding_count_before = len(session.execute(select(Embedding)).scalars().all())

    summary = _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    assert summary["chunks_embedded"] == 0
    assert summary["errors"] == 0
    chunk_count_after = len(session.execute(select(KnowledgeChunk)).scalars().all())
    embedding_count_after = len(session.execute(select(Embedding)).scalars().all())
    assert chunk_count_after == chunk_count_before
    assert embedding_count_after == embedding_count_before


def test_load_chunks_changed_content_updates_and_reembeds(session: Session):
    s3 = FakeS3Client({"chunks/phb.ndjson": _line("c1", "content one")})
    embedder = FakeEmbedClient()
    _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    original_embedding_id = session.execute(select(Embedding)).scalars().one().id

    s3.manifests["chunks/phb.ndjson"] = _line("c1", "content one, revised")
    summary = _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    assert summary["chunks_upserted"] == 1
    assert summary["chunks_embedded"] == 1

    chunk = session.execute(select(KnowledgeChunk)).scalars().one()
    assert chunk.content == "content one, revised"

    embeddings = session.execute(select(Embedding)).scalars().all()
    assert len(embeddings) == 1
    assert embeddings[0].id == original_embedding_id  # updated in place, not duplicated


def test_load_chunks_bad_lines_counted_valid_lines_still_loaded(session: Session):
    manifest = "\n".join(
        [
            "{not valid json",
            _line("c1", "good content"),
            '{"chunk_ref": "c2"}',  # missing content
        ]
    )
    s3 = FakeS3Client({"chunks/phb.ndjson": manifest})
    embedder = FakeEmbedClient()

    summary = _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    assert summary["errors"] == 2
    assert summary["chunks_upserted"] == 1
    chunks = session.execute(select(KnowledgeChunk)).scalars().all()
    assert [c.chunk_ref for c in chunks] == ["c1"]


def test_load_chunks_two_books_in_one_run(session: Session):
    s3 = FakeS3Client(
        {
            "chunks/phb.ndjson": _line("c1", "phb content"),
            "chunks/dmg.ndjson": _line("c1", "dmg content"),
        }
    )
    embedder = FakeEmbedClient()

    summary = _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    assert summary["books"] == 2
    assert summary["chunks_upserted"] == 2
    assert summary["chunks_embedded"] == 2

    chunks = session.execute(select(KnowledgeChunk)).scalars().all()
    # Same chunk_ref across two books is not a collision -- unique key is
    # (book_id, chunk_ref).
    assert {(c.book_id, c.chunk_ref) for c in chunks} == {
        ("phb", "c1"),
        ("dmg", "c1"),
    }
