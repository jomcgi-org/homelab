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
from grimoire.models import Book, Embedding, KnowledgeChunk


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
        self.texts_seen: list[str] = []

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts_seen.extend(texts)
        return [[0.1] * 1024 for _ in texts]


def _line(
    chunk_ref: str,
    content: str,
    section_path: str | None = None,
    image_ref: str | None = None,
) -> str:
    obj = {"chunk_ref": chunk_ref, "content": content}
    if section_path is not None:
        obj["section_path"] = section_path
    if image_ref is not None:
        obj["image_ref"] = image_ref
    return json.dumps(obj)


def _run(coro):
    return asyncio.run(coro)


# --- parse_manifest_lines -----------------------------------------------


def test_parse_manifest_lines_valid_and_optional_fields():
    lines = [
        _line("phb-c3-014", "Wizards cast spells.", "Chapter 3 > Classes > Wizard"),
        _line("phb-c3-015", "Fighters fight."),
        _line(
            "mm-goblin-img",
            "A small green goblin.",
            "GOBLIN",
            image_ref="s3://grimoire/books/mm/raw/img/abc.jpg",
        ),
    ]
    valid, errors = ingest.parse_manifest_lines("phb", lines)

    assert errors == 0
    assert valid == [
        {
            "chunk_ref": "phb-c3-014",
            "content": "Wizards cast spells.",
            "section_path": "Chapter 3 > Classes > Wizard",
            "image_ref": None,
        },
        {
            "chunk_ref": "phb-c3-015",
            "content": "Fighters fight.",
            "section_path": None,
            "image_ref": None,
        },
        {
            "chunk_ref": "mm-goblin-img",
            "content": "A small green goblin.",
            "section_path": "GOBLIN",
            "image_ref": "s3://grimoire/books/mm/raw/img/abc.jpg",
        },
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
    s3 = FakeS3Client({"books/phb/chunks/chunks.ndjson": manifest})
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


def test_load_chunks_truncates_oversized_embed_input(session: Session):
    """A chunk longer than EMBED_INPUT_MAX_CHARS embeds a truncated input (the
    llama.cpp server 500s past its token window) while the stored chunk keeps
    its full content for retrieval."""
    giant = "spell table row " * 3000  # 48k chars, like the PHB'24 spell lists
    manifest = _line("c-giant", giant)
    s3 = FakeS3Client({"books/phb/chunks/chunks.ndjson": manifest})
    embedder = FakeEmbedClient()

    summary = _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    assert summary["chunks_embedded"] == 1
    assert len(embedder.texts_seen) == 1
    assert len(embedder.texts_seen[0]) == ingest.EMBED_INPUT_MAX_CHARS
    chunk = session.execute(select(KnowledgeChunk)).scalars().one()
    assert len(chunk.content) == len(giant)


def test_load_chunks_persists_image_ref_and_derives_book_id_from_path(
    session: Session,
):
    manifest = "\n".join(
        [
            _line("txt", "A goblin lurks in the dark."),
            _line(
                "img",
                "A small green goblin with a spear.",
                "GOBLIN",
                image_ref="s3://grimoire/books/mm/raw/img/goblin.jpg",
            ),
        ]
    )
    s3 = FakeS3Client({"books/mm/chunks/chunks.ndjson": manifest})
    _run(ingest.load_chunks(session, s3, FakeEmbedClient(), bucket="grimoire"))

    by_ref = {
        c.chunk_ref: c for c in session.execute(select(KnowledgeChunk)).scalars().all()
    }
    # book_id comes from the path segment, not the filename.
    assert by_ref["img"].book_id == "mm"
    assert by_ref["img"].image_ref == "s3://grimoire/books/mm/raw/img/goblin.jpg"
    assert by_ref["txt"].image_ref is None


def test_load_chunks_idempotent_rerun_embeds_nothing(session: Session):
    manifest = "\n".join([_line("c1", "content one")])
    s3 = FakeS3Client({"books/phb/chunks/chunks.ndjson": manifest})
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
    s3 = FakeS3Client({"books/phb/chunks/chunks.ndjson": _line("c1", "content one")})
    embedder = FakeEmbedClient()
    _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    original_embedding_id = session.execute(select(Embedding)).scalars().one().id

    s3.manifests["books/phb/chunks/chunks.ndjson"] = _line("c1", "content one, revised")
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
    s3 = FakeS3Client({"books/phb/chunks/chunks.ndjson": manifest})
    embedder = FakeEmbedClient()

    summary = _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    assert summary["errors"] == 2
    assert summary["chunks_upserted"] == 1
    chunks = session.execute(select(KnowledgeChunk)).scalars().all()
    assert [c.chunk_ref for c in chunks] == ["c1"]


def test_load_chunks_two_books_in_one_run(session: Session):
    s3 = FakeS3Client(
        {
            "books/phb/chunks/chunks.ndjson": _line("c1", "phb content"),
            "books/dmg/chunks/chunks.ndjson": _line("c1", "dmg content"),
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


# --- seq (reading order) + book metadata ---------------------------------


def test_load_chunks_assigns_seq_from_line_order(session: Session):
    manifest = "\n".join([_line("c1", "one"), _line("c2", "two"), _line("c3", "three")])
    s3 = FakeS3Client({"books/phb/chunks/chunks.ndjson": manifest})
    _run(ingest.load_chunks(session, s3, FakeEmbedClient(), bucket="grimoire"))

    by_ref = {
        c.chunk_ref: c for c in session.execute(select(KnowledgeChunk)).scalars().all()
    }
    assert (by_ref["c1"].seq, by_ref["c2"].seq, by_ref["c3"].seq) == (0, 1, 2)


def test_load_chunks_seq_rewritten_on_reorder_without_reembedding(session: Session):
    # First load: c1, c2. Re-upload with the order swapped and a new line
    # inserted. seq must follow the new NDJSON order even for unchanged content,
    # and a pure reorder must not re-embed.
    s3 = FakeS3Client(
        {
            "books/phb/chunks/chunks.ndjson": "\n".join(
                [_line("c1", "one"), _line("c2", "two")]
            )
        }
    )
    embedder = FakeEmbedClient()
    _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))
    embeds_after_first = embedder.calls

    s3.manifests["books/phb/chunks/chunks.ndjson"] = "\n".join(
        [_line("c2", "two"), _line("new", "new content"), _line("c1", "one")]
    )
    summary = _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    by_ref = {
        c.chunk_ref: c for c in session.execute(select(KnowledgeChunk)).scalars().all()
    }
    assert by_ref["c2"].seq == 0
    assert by_ref["new"].seq == 1
    assert by_ref["c1"].seq == 2
    # Only the brand-new chunk is upserted/embedded; the two reordered ones are
    # a pure seq shift (not counted as upserts, not re-embedded).
    assert summary["chunks_upserted"] == 1
    # The one embed batch this run covers only the new chunk.
    assert embedder.calls == embeds_after_first + 1


def test_load_chunks_seq_global_across_multiple_manifests(session: Session):
    # A book split across two manifest files must get one contiguous seq
    # sequence over both (sorted key order), not a 0-based restart per file --
    # otherwise seq collides within the book and the reader's ordering breaks.
    s3 = FakeS3Client(
        {
            "books/mm/chunks/chunks-001.ndjson": "\n".join(
                [_line("a1", "one"), _line("a2", "two")]
            ),
            "books/mm/chunks/chunks-002.ndjson": "\n".join(
                [_line("b1", "three"), _line("b2", "four")]
            ),
        }
    )
    summary = _run(
        ingest.load_chunks(session, s3, FakeEmbedClient(), bucket="grimoire")
    )
    assert summary["books"] == 1

    by_ref = {
        c.chunk_ref: c for c in session.execute(select(KnowledgeChunk)).scalars().all()
    }
    assert by_ref["a1"].seq == 0
    assert by_ref["a2"].seq == 1
    assert by_ref["b1"].seq == 2
    assert by_ref["b2"].seq == 3
    # Every seq within the book is distinct.
    seqs = [c.seq for c in by_ref.values()]
    assert len(set(seqs)) == len(seqs)


def test_load_chunks_upserts_book_row_once(session: Session):
    s3 = FakeS3Client({"books/mm/chunks/chunks.ndjson": _line("c1", "one")})
    embedder = FakeEmbedClient()
    _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    book = session.get(Book, "mm")
    assert book is not None
    # display_name defaults to the title-cased id until renamed.
    assert book.display_name == "Mm"

    # A rename must survive a re-upload (the loader never clobbers display_name).
    book.display_name = "Monster Manual"
    session.add(book)
    session.commit()
    _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))
    assert session.get(Book, "mm").display_name == "Monster Manual"


def test_load_chunks_default_display_name_title_cases_hyphenated_id(
    session: Session,
):
    # A hyphenated id title-cases to a human-friendly default (not clobbered
    # here since this is a fresh book row, unlike the rename check above).
    s3 = FakeS3Client({"books/monster-manual/chunks/chunks.ndjson": _line("c1", "one")})
    embedder = FakeEmbedClient()
    _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))

    book = session.get(Book, "monster-manual")
    assert book is not None
    assert book.display_name == "Monster Manual"


# --- embed batching + self-heal ------------------------------------------


def test_embed_batches_bounds_by_size_and_count():
    from types import SimpleNamespace as NS

    rows = [NS(content="a" * 10) for _ in range(5)]
    # char_budget 25 -> at most 2 rows/batch (a 3rd would be 30 > 25).
    assert [len(b) for b in ingest._embed_batches(rows, 25, 64)] == [2, 2, 1]
    # max_count caps a batch even when the size budget is huge.
    assert [len(b) for b in ingest._embed_batches(rows, 10_000, 2)] == [2, 2, 1]
    # a single over-budget row is yielded alone, never dropped.
    big = [NS(content="x" * 100), NS(content="y" * 5)]
    assert [len(b) for b in ingest._embed_batches(big, 25, 64)] == [1, 1]


def test_load_chunks_reembeds_chunks_missing_embedding(session: Session):
    # A prior run loaded the chunk but its embed step failed, leaving a chunk
    # with no vector. A plain re-run over unchanged data must self-heal it.
    s3 = FakeS3Client({"books/phb/chunks/chunks.ndjson": _line("c1", "content one")})
    embedder = FakeEmbedClient()
    _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))
    assert len(session.execute(select(Embedding)).scalars().all()) == 1

    for e in session.execute(select(Embedding)).scalars().all():
        session.delete(e)
    session.commit()

    # Content is unchanged, so the upsert queues nothing; self-heal must re-embed.
    summary = _run(ingest.load_chunks(session, s3, embedder, bucket="grimoire"))
    assert summary["chunks_embedded"] == 1
    assert len(session.execute(select(Embedding)).scalars().all()) == 1
