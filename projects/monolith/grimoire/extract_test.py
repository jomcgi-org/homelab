"""Tests for grimoire.extract: OpenRouter entity extraction from chunks.

FakeOpenRouterClient below stubs the network call entirely (canned dicts
keyed by chunk content); the OpenRouterClient HTTP/retry tests at the
bottom mock httpx.AsyncClient directly, mirroring
shared/embedding_test.py + shared/embedding_retry_test.py.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from grimoire import extract
from grimoire.extract import OpenRouterClient, OpenRouterError, extract_chunks
from grimoire.models import (
    ChunkEntityMention,
    Embedding,
    Entity,
    EntityCreature,
    KnowledgeChunk,
    Relationship,
)


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


class FakeOpenRouterClient:
    """Canned extraction responses keyed by chunk content; no HTTP."""

    model = "test-extract-model"

    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[str] = []

    async def extract(self, chunk_text: str) -> dict:
        self.calls.append(chunk_text)
        result = self.responses.get(chunk_text)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return {"entities": [], "mentions": [], "relationships": []}
        return result


class FakeEmbedClient:
    """Returns a fixed 1024-dim vector per text, tracking call count."""

    model = "voyage-4-nano"

    def __init__(self):
        self.calls = 0

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.2] * 1024 for _ in texts]


def _make_chunk(
    session: Session, book_id: str, chunk_ref: str, content: str
) -> KnowledgeChunk:
    chunk = KnowledgeChunk(book_id=book_id, chunk_ref=chunk_ref, content=content)
    session.add(chunk)
    session.commit()
    session.refresh(chunk)
    return chunk


def _run(coro):
    return asyncio.run(coro)


# --- extract_chunks --------------------------------------------------------


def test_extract_chunks_happy_path(session: Session):
    chunk = _make_chunk(
        session, "mm", "mm-001", "An owlbear stalks the Zhentarim camp."
    )

    existing_npc = Entity(entity_type="npc", name="Existing NPC", source_book="mm")
    session.add(existing_npc)
    session.commit()
    session.refresh(existing_npc)

    responses = {
        chunk.content: {
            "entities": [
                {
                    "entity_type": "creature",
                    "name": "Owlbear",
                    "summary": "A fearsome bear-owl hybrid.",
                    "detail": {
                        "size": "Large",
                        "creature_type": "monstrosity",
                        "ac": 13,
                        "hp_avg": 59,
                        "cr": 3.0,
                    },
                },
                {
                    "entity_type": "faction",
                    "name": "The Zhentarim",
                    "summary": "A mercenary trading company.",
                },
            ],
            "mentions": [
                {"entity_name": "Existing NPC", "mention_text": "seen nearby"},
            ],
            "relationships": [
                {
                    "from_name": "Owlbear",
                    "to_name": "The Zhentarim",
                    "rel_type": "MEMBER_OF",
                },
            ],
        }
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()

    summary = _run(extract_chunks(session, or_client, embed_client, limit=25))

    assert summary == {
        "chunks_processed": 1,
        "chunks_failed": 0,
        "entities_created": 2,
        "entities_reused": 0,
        "mentions_created": 3,
        "relationships_created": 1,
        "entities_embedded": 2,
    }

    entities = session.execute(select(Entity)).scalars().all()
    assert {e.name for e in entities} == {"Owlbear", "The Zhentarim", "Existing NPC"}

    owlbear = (
        session.execute(select(Entity).where(Entity.name == "Owlbear")).scalars().one()
    )
    assert owlbear.entity_type == "creature"
    assert owlbear.source_book == "mm"
    detail = (
        session.execute(
            select(EntityCreature).where(EntityCreature.entity_id == owlbear.id)
        )
        .scalars()
        .one()
    )
    assert detail.ac == 13
    assert detail.hp_avg == 59
    assert detail.cr == pytest.approx(3.0)

    mentions = session.execute(select(ChunkEntityMention)).scalars().all()
    assert len(mentions) == 3

    relationships = session.execute(select(Relationship)).scalars().all()
    assert len(relationships) == 1
    assert relationships[0].rel_type == "MEMBER_OF"

    embeddings = session.execute(select(Embedding)).scalars().all()
    assert len(embeddings) == 2
    assert {e.embeddable_kind for e in embeddings} == {"entity"}
    assert {e.model for e in embeddings} == {"voyage-4-nano"}


def test_extract_chunks_rerun_skips_already_mentioned_chunks(session: Session):
    chunk = _make_chunk(session, "mm", "mm-001", "A goblin ambushes the party.")
    responses = {
        chunk.content: {
            "entities": [
                {"entity_type": "creature", "name": "Goblin", "summary": "Small."}
            ],
            "mentions": [],
            "relationships": [],
        }
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()
    _run(extract_chunks(session, or_client, embed_client, limit=25))

    calls_before = len(or_client.calls)
    summary = _run(extract_chunks(session, or_client, embed_client, limit=25))

    assert summary == {
        "chunks_processed": 0,
        "chunks_failed": 0,
        "entities_created": 0,
        "entities_reused": 0,
        "mentions_created": 0,
        "relationships_created": 0,
        "entities_embedded": 0,
    }
    assert len(or_client.calls) == calls_before


def test_extract_chunks_name_dedup_reuses_and_does_not_overwrite_detail(
    session: Session,
):
    chunk1 = _make_chunk(session, "mm", "mm-001", "An owlbear guards the lair.")
    chunk2 = _make_chunk(session, "mm", "mm-002", "The owlbear returns to its den.")
    responses = {
        chunk1.content: {
            "entities": [
                {
                    "entity_type": "creature",
                    "name": "Owlbear",
                    "summary": "First sighting.",
                    "detail": {"ac": 13, "hp_avg": 59},
                }
            ],
            "mentions": [],
            "relationships": [],
        },
        chunk2.content: {
            "entities": [
                {
                    "entity_type": "creature",
                    "name": "OWLBEAR",  # case-insensitive match against chunk1's entity
                    "summary": "Second sighting.",
                    "detail": {"ac": 99, "hp_avg": 1},
                }
            ],
            "mentions": [],
            "relationships": [],
        },
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()

    _run(extract_chunks(session, or_client, embed_client, limit=1))
    summary2 = _run(extract_chunks(session, or_client, embed_client, limit=1))

    assert summary2["entities_created"] == 0
    assert summary2["entities_reused"] == 1
    assert summary2["entities_embedded"] == 0

    entities = session.execute(select(Entity)).scalars().all()
    assert len(entities) == 1

    detail = session.execute(select(EntityCreature)).scalars().one()
    assert detail.ac == 13  # first extraction wins, not overwritten by 99
    assert detail.hp_avg == 59


def test_extract_chunks_malformed_extraction_counted_failed_and_retryable(
    session: Session,
):
    bad_chunk = _make_chunk(session, "mm", "mm-001", "Garbled text.")
    good_chunk = _make_chunk(
        session, "mm", "mm-002", "A dragon terrorizes the village."
    )
    responses = {
        bad_chunk.content: ValueError("malformed JSON"),
        good_chunk.content: {
            "entities": [
                {"entity_type": "creature", "name": "Dragon", "summary": "Big."}
            ],
            "mentions": [],
            "relationships": [],
        },
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()

    summary = _run(extract_chunks(session, or_client, embed_client, limit=25))

    assert summary["chunks_failed"] == 1
    assert summary["chunks_processed"] == 1
    assert summary["entities_created"] == 1

    mentions = session.execute(select(ChunkEntityMention)).scalars().all()
    assert {m.chunk_id for m in mentions} == {good_chunk.id}

    # bad_chunk still has no mentions, so it is selectable again next run.
    remaining = (
        session.execute(
            select(KnowledgeChunk).where(
                ~select(ChunkEntityMention.chunk_id)
                .where(ChunkEntityMention.chunk_id == KnowledgeChunk.id)
                .exists()
            )
        )
        .scalars()
        .all()
    )
    assert [c.id for c in remaining] == [bad_chunk.id]


def test_extract_chunks_unresolvable_relationship_skipped_without_error(
    session: Session,
):
    chunk = _make_chunk(session, "mm", "mm-001", "A tale of two strangers.")
    responses = {
        chunk.content: {
            "entities": [],
            "mentions": [],
            "relationships": [
                {"from_name": "Nobody", "to_name": "Nobody Else", "rel_type": "KNOWS"}
            ],
        }
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()

    summary = _run(extract_chunks(session, or_client, embed_client, limit=25))

    assert summary["chunks_processed"] == 1
    assert summary["relationships_created"] == 0
    assert session.execute(select(Relationship)).scalars().all() == []


def test_extract_chunks_limit_respected(session: Session):
    for i in range(3):
        _make_chunk(session, "mm", f"mm-{i}", f"Chunk body {i}")
    or_client = FakeOpenRouterClient({})  # every chunk falls back to empty extraction
    embed_client = FakeEmbedClient()

    summary = _run(extract_chunks(session, or_client, embed_client, limit=2))

    assert summary["chunks_processed"] == 2
    remaining = (
        session.execute(
            select(KnowledgeChunk).where(
                ~select(ChunkEntityMention.chunk_id)
                .where(ChunkEntityMention.chunk_id == KnowledgeChunk.id)
                .exists()
            )
        )
        .scalars()
        .all()
    )
    # extraction of an empty payload writes no mention rows, so the two
    # "processed" chunks remain selectable too -- only the limit is asserted.
    assert len(remaining) == 3


# --- OpenRouterClient -------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_client_extract_success():
    client = OpenRouterClient(api_key="test-key", base_url="http://fake/chat")
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"entities": [], "mentions": [], "relationships": []}
                    )
                }
            }
        ]
    }

    with patch("grimoire.extract.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = fake_response
        mock_client_cls.return_value = mock_client

        result = await client.extract("some chunk text")

    assert result == {"entities": [], "mentions": [], "relationships": []}
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "http://fake/chat"
    payload = call_args[1]["json"]
    assert payload["messages"][1]["content"] == "some chunk text"
    assert payload["response_format"] == {"type": "json_object"}
    assert call_args[1]["headers"]["Authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_openrouter_client_extract_retries_then_raises_openrouter_error():
    client = OpenRouterClient(api_key="test-key", base_url="http://fake/chat")

    with (
        patch("grimoire.extract.httpx.AsyncClient") as mock_client_cls,
        patch("grimoire.extract.asyncio.sleep", new=AsyncMock()) as mock_sleep,
    ):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
        mock_client_cls.return_value = mock_client

        with pytest.raises(OpenRouterError):
            await client.extract("chunk text")

    assert mock_client.post.call_count == extract.EXTRACT_MAX_RETRIES
    assert mock_sleep.call_count == extract.EXTRACT_MAX_RETRIES - 1


@pytest.mark.asyncio
async def test_openrouter_client_extract_malformed_content_raises_value_error():
    client = OpenRouterClient(api_key="test-key", base_url="http://fake/chat")
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "not json"}}]
    }

    with patch("grimoire.extract.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = fake_response
        mock_client_cls.return_value = mock_client

        with pytest.raises(ValueError):
            await client.extract("chunk text")

    # not retried: malformed JSON in the content will not fix itself.
    mock_client.post.assert_called_once()
