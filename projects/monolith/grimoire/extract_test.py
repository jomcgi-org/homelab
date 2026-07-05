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
from grimoire.extract import (
    ACTIVE_PROMPT_VERSION,
    PROMPT_VERSIONS,
    REL_TYPE_SET,
    OpenRouterClient,
    OpenRouterError,
    _canonicalize_name,
    book_kind,
    extract_chunks,
    prompt_version_hash,
)
from grimoire.models import (
    Book,
    ChunkEntityMention,
    ChunkExtraction,
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
    prompt_version = ACTIVE_PROMPT_VERSION

    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[str] = []

    async def extract(
        self,
        chunk_text: str,
        section_path: str | None = None,
        image_ref: str | None = None,
        book_title: str | None = None,
        book_kind: str | None = None,
    ) -> dict:
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


def test_extract_chunks_multiple_groups_all_marked(session: Session):
    """concurrency < chunk count runs several groups; every chunk is marked."""
    contents = [f"Chunk {i}: a goblin lurks." for i in range(5)]
    chunks = [
        _make_chunk(session, "mm", f"mm-{i:03d}", content)
        for i, content in enumerate(contents)
    ]
    responses = {
        content: {
            "entities": [
                {"entity_type": "creature", "name": f"Goblin {i}", "summary": "Small."}
            ],
            "mentions": [],
            "relationships": [],
        }
        for i, content in enumerate(contents)
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()

    # 5 chunks at concurrency=2 -> groups [2, 2, 1].
    summary = _run(
        extract_chunks(session, or_client, embed_client, limit=25, concurrency=2)
    )

    assert summary["chunks_processed"] == 5
    assert summary["chunks_failed"] == 0
    assert summary["entities_created"] == 5
    assert summary["entities_embedded"] == 5
    # Every chunk got a marker (so a rerun would skip all of them).
    markers = session.execute(select(ChunkExtraction)).scalars().all()
    assert {m.chunk_id for m in markers} == {c.id for c in chunks}
    assert len(or_client.calls) == 5


def test_extract_chunks_earlier_groups_commit_before_a_later_failure(
    session: Session,
):
    """A hard failure in a later group leaves earlier groups' work committed.

    This is the incremental-commit contract: a run killed/aborted partway
    through never rolls back the groups it already finished, so reruns resume
    instead of redoing everything.
    """
    good = [_make_chunk(session, "mm", f"mm-{i:03d}", f"Good {i}") for i in range(2)]
    boom = _make_chunk(session, "mm", "mm-100", "Boom")
    responses = {
        "Good 0": {"entities": [], "mentions": [], "relationships": []},
        "Good 1": {"entities": [], "mentions": [], "relationships": []},
        # A non-extract-client error (not OpenRouterError/ValueError) must
        # propagate rather than be swallowed as a failed chunk.
        "Boom": RuntimeError("unexpected"),
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()

    # concurrency=1 -> groups [Good 0], [Good 1], [Boom]; the third raises.
    with pytest.raises(RuntimeError, match="unexpected"):
        _run(extract_chunks(session, or_client, embed_client, limit=25, concurrency=1))

    # The two good chunks committed before the failing group ran.
    session.rollback()  # drop the aborted (uncommitted) third group
    marked = {
        m.chunk_id for m in session.execute(select(ChunkExtraction)).scalars().all()
    }
    assert marked == {good[0].id, good[1].id}
    assert boom.id not in marked


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
    # "The Zhentarim" is canonicalized to "Zhentarim" (leading article dropped),
    # and the MEMBER_OF edge's to_name resolves through the same canonical key.
    assert {e.name for e in entities} == {"Owlbear", "Zhentarim", "Existing NPC"}

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


def test_extract_chunks_rerun_skips_already_marked_chunks(session: Session):
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

    markers = session.execute(select(ChunkExtraction)).scalars().all()
    assert len(markers) == 1
    assert markers[0].chunk_id == chunk.id
    assert markers[0].model == FakeOpenRouterClient.model
    assert markers[0].prompt_version == ACTIVE_PROMPT_VERSION
    assert markers[0].status == "ok"


def test_processed_marker_reextracts_on_model_change(session: Session):
    chunk = _make_chunk(session, "mm", "mm-001", "A goblin ambushes the party.")
    session.add(
        ChunkExtraction(
            chunk_id=chunk.id,
            model="some-other-model",
            prompt_version=ACTIVE_PROMPT_VERSION,
            status="ok",
        )
    )
    session.commit()

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

    summary = _run(extract_chunks(session, or_client, embed_client, limit=25))

    assert summary["chunks_processed"] == 1
    markers = session.execute(select(ChunkExtraction)).scalars().all()
    assert {m.model for m in markers} == {
        "some-other-model",
        FakeOpenRouterClient.model,
    }


def test_processed_marker_reextracts_on_prompt_version_change(session: Session):
    chunk = _make_chunk(session, "mm", "mm-001", "A goblin ambushes the party.")
    # A marker under an OLD prompt version (v1) must not satisfy the active
    # version (v2), so the chunk is re-extracted and gets a second marker.
    session.add(
        ChunkExtraction(
            chunk_id=chunk.id,
            model=FakeOpenRouterClient.model,
            prompt_version="v1",
            status="ok",
        )
    )
    session.commit()

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

    summary = _run(extract_chunks(session, or_client, embed_client, limit=25))

    assert summary["chunks_processed"] == 1
    markers = session.execute(select(ChunkExtraction)).scalars().all()
    assert {m.prompt_version for m in markers} == {"v1", ACTIVE_PROMPT_VERSION}


def test_empty_yield_records_empty_marker(session: Session):
    chunk = _make_chunk(session, "mm", "mm-001", "Nothing extractable here.")
    or_client = FakeOpenRouterClient({})  # falls back to empty extraction
    embed_client = FakeEmbedClient()

    summary = _run(extract_chunks(session, or_client, embed_client, limit=25))

    assert summary["chunks_processed"] == 1
    marker = session.execute(select(ChunkExtraction)).scalars().one()
    assert marker.chunk_id == chunk.id
    assert marker.status == "empty"


def test_book_filter_scopes_selection(session: Session, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_EXTRACT_BOOK", "book-eval")
    eval_chunk = _make_chunk(session, "book-eval", "eval-001", "Eval book chunk.")
    _make_chunk(session, "book-other", "other-001", "Other book chunk.")

    or_client = FakeOpenRouterClient({})
    embed_client = FakeEmbedClient()

    summary = _run(extract_chunks(session, or_client, embed_client, limit=25))

    assert summary["chunks_processed"] == 1
    marker = session.execute(select(ChunkExtraction)).scalars().one()
    assert marker.chunk_id == eval_chunk.id


def test_failed_extraction_leaves_no_marker(session: Session):
    bad_chunk = _make_chunk(session, "mm", "mm-001", "Garbled text.")
    responses = {bad_chunk.content: ValueError("malformed JSON")}
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()

    summary = _run(extract_chunks(session, or_client, embed_client, limit=25))

    assert summary["chunks_failed"] == 1
    assert session.execute(select(ChunkExtraction)).scalars().all() == []


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
    # Enrich never clobbers an already-set value: chunk2's ac=99/hp=1 lose to
    # chunk1's on conflict (fill-only-when-NULL). See the disjoint-fields test
    # below for the case where chunk2 *adds* fields chunk1 left empty.
    assert detail.ac == 13
    assert detail.hp_avg == 59


def test_extract_chunks_enriches_disjoint_detail_across_chunks(
    session: Session,
):
    # A monster whose lore and stat block land in different chunks: chunk1 sets
    # only cr + a partial actions blob, chunk2 adds ac/hp + more actions. The
    # entity must end up whole (ADR 012 rev. enrich, not first-wins).
    chunk1 = _make_chunk(session, "mm", "mm-001", "Aboleth lore, cr only.")
    chunk2 = _make_chunk(session, "mm", "mm-002", "Aboleth stat block.")
    responses = {
        chunk1.content: {
            "entities": [
                {
                    "entity_type": "creature",
                    "name": "Aboleth",
                    "summary": "Lore chunk.",
                    "detail": {"cr": 10, "actions": {"Tentacle": "reach 10 ft."}},
                }
            ],
            "mentions": [],
            "relationships": [],
        },
        chunk2.content: {
            "entities": [
                {
                    "entity_type": "creature",
                    "name": "aboleth",  # case-insensitive reuse
                    "summary": "Stat chunk.",
                    "detail": {
                        "ac": 17,
                        "hp_avg": 135,
                        "cr": 99,  # conflicts: chunk1's cr=10 must win
                        "actions": {"Enslave": "DC 14 save"},
                    },
                }
            ],
            "mentions": [],
            "relationships": [],
        },
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()

    _run(extract_chunks(session, or_client, embed_client, limit=1))
    _run(extract_chunks(session, or_client, embed_client, limit=1))

    assert len(session.execute(select(Entity)).scalars().all()) == 1
    detail = session.execute(select(EntityCreature)).scalars().one()
    assert detail.ac == 17  # filled from chunk2 (was NULL)
    assert detail.hp_avg == 135  # filled from chunk2 (was NULL)
    assert detail.cr == pytest.approx(10.0)  # chunk1 wins the conflict
    # JSONB key-merge: both actions present, existing key wins on conflict.
    assert detail.actions == {"Tentacle": "reach 10 ft.", "Enslave": "DC 14 save"}


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

    # bad_chunk got no marker (extraction raised), so it is selectable again.
    markers = session.execute(select(ChunkExtraction)).scalars().all()
    assert [m.chunk_id for m in markers] == [good_chunk.id]


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
    # exactly the 2 processed chunks got a marker (status="empty"); the
    # third, never attempted, stays pending -- confirming the limit cutoff.
    markers = session.execute(select(ChunkExtraction)).scalars().all()
    assert len(markers) == 2
    assert all(m.status == "empty" for m in markers)


# --- prompt versioning + safety net + canonicalization ---------------------


def test_released_prompt_versions_are_frozen_by_hash():
    """Freeze each released version's text by sha256. Editing a shipped prompt
    (v1 or v2) changes its hash and fails here, forcing the change to be a NEW
    version (v3) with a new label, since the label is the marker cache key."""
    frozen = {
        "v1": "64cb5df96d35c282f0ad004233ef01ef52c0eb86eed94342f9bfec4479ac949f",
        "v2": "aeb536de96f21e54bcd54ed073efc9945b92b125e3dae6f748808e4f0325ab52",
    }
    # Every released version must be pinned (a new version needs a new pin here).
    assert set(frozen) == set(PROMPT_VERSIONS)
    for label, expected in frozen.items():
        assert prompt_version_hash(label) == expected, (
            f"prompt {label} text changed: bump to a new version label instead of "
            f"editing a released prompt"
        )


def test_v2_has_schema_v1_does_not():
    assert PROMPT_VERSIONS["v1"].schema is None
    assert PROMPT_VERSIONS["v2"].schema is not None


def test_non_enum_rel_type_mapped_to_related_to(session: Session):
    """A rel_type outside the closed set is stored as RELATED_TO, not free text."""
    chunk = _make_chunk(session, "mm", "mm-001", "Two heroes and a bond.")
    responses = {
        chunk.content: {
            "entities": [
                {"entity_type": "npc", "name": "Alice", "summary": "A hero."},
                {"entity_type": "npc", "name": "Bob", "summary": "Another hero."},
            ],
            "mentions": [],
            "relationships": [
                # Not in REL_TYPE_SET -> must be normalized to RELATED_TO.
                {"from_name": "Alice", "to_name": "Bob", "rel_type": "BEFRIENDS"},
            ],
        }
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()

    summary = _run(extract_chunks(session, or_client, embed_client, limit=25))

    assert summary["relationships_created"] == 1
    rels = session.execute(select(Relationship)).scalars().all()
    assert len(rels) == 1
    assert rels[0].rel_type == "RELATED_TO"
    assert rels[0].rel_type in REL_TYPE_SET


def test_enum_rel_type_stored_verbatim(session: Session):
    """A rel_type already inside the closed set is stored unchanged."""
    chunk = _make_chunk(session, "mm", "mm-001", "A ranger serves a lord.")
    responses = {
        chunk.content: {
            "entities": [
                {"entity_type": "npc", "name": "Ranger", "summary": "A scout."},
                {"entity_type": "faction", "name": "Harpers", "summary": "A network."},
            ],
            "mentions": [],
            "relationships": [
                {"from_name": "Ranger", "to_name": "Harpers", "rel_type": "MEMBER_OF"},
            ],
        }
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()

    _run(extract_chunks(session, or_client, embed_client, limit=25))
    rels = session.execute(select(Relationship)).scalars().all()
    assert [r.rel_type for r in rels] == ["MEMBER_OF"]


def test_canonicalize_name_strips_map_key_prefix():
    assert _canonicalize_name("L17. SURGERY") == "SURGERY"
    assert _canonicalize_name("P2. West Shore") == "West Shore"
    assert _canonicalize_name("17. Old Mill") == "Old Mill"
    # A real name that merely contains a period is left alone (no digit prefix).
    assert _canonicalize_name("St. Cuthbert") == "St. Cuthbert"


def test_canonicalize_name_drops_leading_article():
    assert _canonicalize_name("The Zhentarim") == "Zhentarim"
    assert _canonicalize_name("the Nine Hells") == "Nine Hells"
    assert _canonicalize_name("An Ancient Evil") == "Ancient Evil"
    # "Theramin" must not lose "The"; only a whole leading article word is dropped.
    assert _canonicalize_name("Theramin") == "Theramin"


def test_canonicalize_name_normalizes_curly_apostrophe_and_whitespace():
    assert _canonicalize_name("Uk’otoa") == "Uk'otoa"
    assert _canonicalize_name("  Wind   Dukes  ") == "Wind Dukes"
    # Proper-noun plurals are intentionally NOT de-pluralized in code (prompt-side).
    assert _canonicalize_name("Harpers") == "Harpers"


def test_canonical_names_dedupe_article_and_map_key_variants(session: Session):
    """Article/map-key variants collapse to one spine node via canonicalization."""
    c1 = _make_chunk(session, "mm", "mm-001", "The enclave stronghold.")
    c2 = _make_chunk(session, "mm", "mm-002", "Enclave, seen again.")
    responses = {
        c1.content: {
            "entities": [
                {
                    "entity_type": "faction",
                    "name": "The Emerald Enclave",
                    "summary": "A.",
                }
            ],
            "mentions": [],
            "relationships": [],
        },
        c2.content: {
            "entities": [
                {"entity_type": "faction", "name": "Emerald Enclave", "summary": "B."}
            ],
            "mentions": [],
            "relationships": [],
        },
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()
    _run(extract_chunks(session, or_client, embed_client, limit=1))
    _run(extract_chunks(session, or_client, embed_client, limit=1))

    entities = session.execute(select(Entity)).scalars().all()
    assert [e.name for e in entities] == ["Emerald Enclave"]


def test_extract_passes_context_to_client(session: Session):
    """extract_chunks threads the hierarchy (preferred over leaf), image_ref, and
    book title + genre into the extract call."""
    session.add(Book(id="monster-manual", display_name="Monster Manual"))
    chunk = KnowledgeChunk(
        book_id="monster-manual",
        chunk_ref="mm-001",
        content="A caption of a dragon.",
        section_path="A > BRASS DRAGON",
        section_hierarchy="Chapter 1: Dragons > Metallic Dragons > BRASS DRAGON",
        image_ref="s3://grimoire/img/brass.png",
    )
    session.add(chunk)
    session.commit()

    captured: dict = {}

    class CapturingClient(FakeOpenRouterClient):
        async def extract(
            self,
            chunk_text,
            section_path=None,
            image_ref=None,
            book_title=None,
            book_kind=None,
        ):
            captured.update(
                section_path=section_path,
                image_ref=image_ref,
                book_title=book_title,
                book_kind=book_kind,
            )
            return {"entities": [], "mentions": [], "relationships": []}

    or_client = CapturingClient({})
    embed_client = FakeEmbedClient()
    _run(extract_chunks(session, or_client, embed_client, limit=25))

    # The full hierarchy breadcrumb is preferred over the 2-level section_path.
    assert captured["section_path"] == (
        "Chapter 1: Dragons > Metallic Dragons > BRASS DRAGON"
    )
    assert captured["image_ref"] == "s3://grimoire/img/brass.png"
    assert captured["book_title"] == "Monster Manual"
    assert captured["book_kind"] == "bestiary"


def test_extract_falls_back_to_leaf_section_and_book_id(session: Session):
    """When section_hierarchy is null and the book has no title row, extract falls
    back to the leaf section_path and the raw book_id, with no genre."""
    chunk = KnowledgeChunk(
        book_id="homebrew-thing",
        chunk_ref="hb-001",
        content="A tavern.",
        section_path="The Rusty Tankard",
        section_hierarchy=None,
    )
    session.add(chunk)
    session.commit()

    captured: dict = {}

    class CapturingClient(FakeOpenRouterClient):
        async def extract(
            self,
            chunk_text,
            section_path=None,
            image_ref=None,
            book_title=None,
            book_kind=None,
        ):
            captured.update(
                section_path=section_path, book_title=book_title, book_kind=book_kind
            )
            return {"entities": [], "mentions": [], "relationships": []}

    _run(extract_chunks(session, CapturingClient({}), FakeEmbedClient(), limit=25))
    assert captured["section_path"] == "The Rusty Tankard"
    assert captured["book_title"] == "homebrew-thing"
    assert captured["book_kind"] is None


def test_book_kind_mapping():
    assert book_kind("monster-manual") == "bestiary"
    assert book_kind("players-handbook-2024") == "rulebook"
    assert book_kind("deep-magic-5e") == "spellbook"
    assert book_kind("curse-of-strahd") == "adventure"
    # Prefix family match after an exact miss.
    assert book_kind("tome-of-beasts-2") == "bestiary"
    assert book_kind("sword-coast-adventurers-guide") == "setting-guide"
    # Unmapped slug: no guessed genre.
    assert book_kind("some-random-homebrew") is None


def test_client_user_message_layers_book_section_and_image():
    """The user turn carries Book, Section (single newline apart), and an image
    signal ahead of the body; each line is optional."""
    client = OpenRouterClient(api_key="", base_url="http://fake/chat")
    assert client._user_message("Body text.", None, None) == "Body text."

    full = client._user_message(
        "Body text.",
        "Chapter 4 > Village > L17. Surgery",
        "s3://x/y.png",
        book_title="Curse of Strahd",
        book_kind="adventure",
    )
    lines = full.split("\n\n", 1)[0].split("\n")
    assert lines[0] == "Book: Curse of Strahd (adventure)"
    assert lines[1] == "Section: Chapter 4 > Village > L17. Surgery"
    assert "illustration" in lines[2]
    assert full.endswith("Body text.")

    # Book title with no mapped genre omits the parenthetical.
    no_kind = client._user_message(
        "B.", None, None, book_title="Homebrew", book_kind=None
    )
    assert no_kind.startswith("Book: Homebrew\n\n")


def test_openrouter_hosted_uses_json_schema_response_format():
    """Against OpenRouter, a v2 client sends a strict json_schema response_format
    (the enum hard-constraint) and disables reasoning."""
    client = OpenRouterClient(
        api_key="k", base_url=extract.OPENROUTER_URL, prompt_version="v2"
    )
    fmt = client._format_kwargs()
    assert fmt["response_format"]["type"] == "json_schema"
    assert fmt["response_format"]["json_schema"]["strict"] is True
    schema = fmt["response_format"]["json_schema"]["schema"]
    rel_enum = schema["properties"]["relationships"]["items"]["properties"]["rel_type"][
        "enum"
    ]
    assert "RELATED_TO" in rel_enum
    assert set(rel_enum) == set(REL_TYPE_SET)


def test_vllm_uses_guided_json():
    """Against a vLLM endpoint, a v2 client sends guided_json (not json_schema)."""
    client = OpenRouterClient(
        api_key="", base_url="http://inference/v1/chat/completions", prompt_version="v2"
    )
    fmt = client._format_kwargs()
    assert fmt["response_format"] == {"type": "json_object"}
    assert fmt["guided_json"] == PROMPT_VERSIONS["v2"].schema


def test_v1_client_keeps_json_object():
    client = OpenRouterClient(api_key="k", prompt_version="v1")
    assert client._format_kwargs() == {"response_format": {"type": "json_object"}}


async def _capture_payload(client: OpenRouterClient) -> dict:
    """Run one extract call against a mocked httpx and return the sent payload."""
    ok = MagicMock()
    ok.status_code = 200
    ok.json.return_value = {
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
    with patch("grimoire.extract.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = ok
        mock_cls.return_value = mock_client
        await client.extract("chunk")
    return mock_client.post.call_args[1]["json"]


@pytest.mark.asyncio
async def test_provider_routing_sent_to_openrouter_when_env_set(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_EXTRACT_PROVIDER", "deepseek")
    client = OpenRouterClient(api_key="k", base_url=extract.OPENROUTER_URL)
    payload = await _capture_payload(client)
    assert payload["provider"] == {"order": ["deepseek"], "allow_fallbacks": False}


@pytest.mark.asyncio
async def test_provider_routing_comma_list_becomes_ordered_array(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_EXTRACT_PROVIDER", "deepseek, fireworks")
    client = OpenRouterClient(api_key="k", base_url=extract.OPENROUTER_URL)
    payload = await _capture_payload(client)
    assert payload["provider"]["order"] == ["deepseek", "fireworks"]
    assert payload["provider"]["allow_fallbacks"] is False


@pytest.mark.asyncio
async def test_provider_routing_absent_when_env_unset(monkeypatch):
    monkeypatch.delenv("GRIMOIRE_EXTRACT_PROVIDER", raising=False)
    client = OpenRouterClient(api_key="k", base_url=extract.OPENROUTER_URL)
    payload = await _capture_payload(client)
    assert "provider" not in payload


@pytest.mark.asyncio
async def test_provider_routing_not_sent_to_vllm(monkeypatch):
    """Even with the env set, a vLLM endpoint never gets the OpenRouter-only
    provider field (nor the reasoning field)."""
    monkeypatch.setenv("GRIMOIRE_EXTRACT_PROVIDER", "deepseek")
    client = OpenRouterClient(
        api_key="", base_url="http://inference/v1/chat/completions"
    )
    payload = await _capture_payload(client)
    assert "provider" not in payload
    assert "reasoning" not in payload


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
async def test_openrouter_client_extract_strips_markdown_fence():
    """A ```json ... ``` fenced body parses without a second (correction) POST."""
    client = OpenRouterClient(api_key="test-key", base_url="http://fake/chat")
    clean = json.dumps({"entities": [], "mentions": [], "relationships": []})
    fenced = f"```json\n{clean}\n```"
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"choices": [{"message": {"content": fenced}}]}

    with patch("grimoire.extract.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = fake_response
        mock_client_cls.return_value = mock_client

        result = await client.extract("chunk text")

    assert result == {"entities": [], "mentions": [], "relationships": []}
    mock_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_openrouter_client_extract_self_corrects_once_on_bad_json():
    """First response is unparseable; the correction turn's valid JSON is returned."""
    client = OpenRouterClient(api_key="test-key", base_url="http://fake/chat")
    bad_response = MagicMock()
    bad_response.status_code = 200
    bad_response.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
    good_response = MagicMock()
    good_response.status_code = 200
    good_response.json.return_value = {
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
        mock_client.post = AsyncMock(side_effect=[bad_response, good_response])
        mock_client_cls.return_value = mock_client

        result = await client.extract("chunk text")

    assert result == {"entities": [], "mentions": [], "relationships": []}
    assert mock_client.post.call_count == 2

    second_call_messages = mock_client.post.call_args_list[1][1]["json"]["messages"]
    assert second_call_messages[0]["content"] == extract.EXTRACTION_PROMPT
    assert second_call_messages[1]["content"] == "chunk text"
    assert second_call_messages[2]["role"] == "assistant"
    assert second_call_messages[2]["content"] == "not json"
    assert second_call_messages[3]["role"] == "user"
    assert "did not parse as JSON" in second_call_messages[3]["content"]


def test_client_omits_auth_header_when_no_key():
    client = OpenRouterClient(api_key="")
    assert client._headers() == {}


def test_client_includes_auth_header_when_key_set():
    client = OpenRouterClient(api_key="test-key")
    assert client._headers() == {"Authorization": "Bearer test-key"}


def test_base_url_defaults_to_openrouter():
    client = OpenRouterClient(api_key="test-key")
    assert client.base_url == extract.OPENROUTER_URL


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_EXTRACT_BASE_URL", "http://local/v1/chat/completions")
    client = OpenRouterClient(api_key="")
    assert client.base_url == "http://local/v1/chat/completions"


def test_base_url_explicit_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_EXTRACT_BASE_URL", "http://local/v1/chat/completions")
    client = OpenRouterClient(api_key="", base_url="http://explicit/chat")
    assert client.base_url == "http://explicit/chat"


@pytest.mark.asyncio
async def test_openrouter_client_extract_omits_auth_header_when_keyless():
    """A keyless client (local Qwen) posts with no Authorization header."""
    client = OpenRouterClient(api_key="", base_url="http://fake/chat")
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

        await client.extract("some chunk text")

    call_args = mock_client.post.call_args
    assert "Authorization" not in call_args[1]["headers"]


@pytest.mark.asyncio
async def test_openrouter_client_extract_raises_after_failed_correction():
    """Both the original and the correction response are unparseable -> raises."""
    client = OpenRouterClient(api_key="test-key", base_url="http://fake/chat")
    bad_response = MagicMock()
    bad_response.status_code = 200
    bad_response.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
    still_bad_response = MagicMock()
    still_bad_response.status_code = 200
    still_bad_response.json.return_value = {
        "choices": [{"message": {"content": "still not json"}}]
    }

    with patch("grimoire.extract.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=[bad_response, still_bad_response])
        mock_client_cls.return_value = mock_client

        with pytest.raises(ValueError):
            await client.extract("chunk text")

    assert mock_client.post.call_count == 2
