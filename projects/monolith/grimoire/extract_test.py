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
    _apply_rel_signature,
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
        session,
        "mm",
        "mm-001",
        "An owlbear stalks the Zhentarim camp. "
        "Armor Class 13, Hit Points 59, Challenge 3.",
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
        # Owlbear(creature) MEMBER_OF Zhentarim(faction) is a valid signature:
        # no swap, no downgrade.
        "rel_signature_swaps": {},
        "rel_signature_downgrades": {},
        # Stats are anchored in the chunk text, so nothing is dropped; no self loop.
        "detail_ungrounded_drops": {},
        "rel_self_loops_dropped": 0,
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
        "rel_signature_swaps": {},
        "rel_signature_downgrades": {},
        "detail_ungrounded_drops": {},
        "rel_self_loops_dropped": 0,
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
    chunk1 = _make_chunk(
        session,
        "mm",
        "mm-001",
        "An owlbear guards the lair. Armor Class 13, Hit Points 59.",
    )
    chunk2 = _make_chunk(
        session,
        "mm",
        "mm-002",
        "The owlbear returns to its den. Armor Class 99, Hit Points 1.",
    )
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
    chunk1 = _make_chunk(session, "mm", "mm-001", "Aboleth lore. Challenge 10.")
    chunk2 = _make_chunk(
        session,
        "mm",
        "mm-002",
        "Aboleth stat block. Armor Class 17, Hit Points 135, Challenge 99.",
    )
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
        "v3": "1d4f924fb55bc021dddffb320461e4a93c065bd2ef1946aeca95293e25959377",
        "v4": "2437034db1f660f0b2f0f130d55d1f94c4cdd039a170af6bd269dd7264ff71c9",
        "v5": "df94db206235900ce377e50162a2678c06b000f75960d0af4c2982972e4e7459",
        "v6": "ebd026a353b5d752ff882112842e5bbb233e8c41cdb4742e73f4561d410f0c59",
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


def test_v3_extends_v2():
    """v3 carries the same schema as v2 and contains v2's text verbatim (built by
    concatenation) plus the type-signature guidance."""
    assert PROMPT_VERSIONS["v3"].schema is PROMPT_VERSIONS["v2"].schema
    v3_text = PROMPT_VERSIONS["v3"].text
    assert v3_text.startswith(PROMPT_VERSIONS["v2"].text)
    assert "RELATIONSHIP TYPE SIGNATURES" in v3_text
    assert "A NAMED IDENTITY IS AN NPC, NOT AN ITEM" in v3_text


def test_v4_extends_v3():
    """v4 carries the same schema as v3 and contains v3's text verbatim (built by
    concatenation) plus the generic-typed-extraction taxonomy (gameplay +
    mechanics types, category/temporality, new rels)."""
    assert PROMPT_VERSIONS["v4"].schema is PROMPT_VERSIONS["v3"].schema
    v4_text = PROMPT_VERSIONS["v4"].text
    assert v4_text.startswith(PROMPT_VERSIONS["v3"].text)
    assert "GENERIC TYPED EXTRACTION" in v4_text
    assert "class_feature, NEVER spell" in v4_text
    assert "OCCURRED_AT" in v4_text and "SUBCLASS_OF" in v4_text


def test_v5_extends_v4():
    """v5 carries the same schema as v4 and contains v4's text verbatim (built by
    concatenation) plus the two mechanics clarifications (condition scope,
    class_feature vs monster abilities). Prompt text only: same schema object as
    v4. (v5 was the active version through v6's promotion.)"""
    assert PROMPT_VERSIONS["v5"].schema is PROMPT_VERSIONS["v4"].schema
    v5_text = PROMPT_VERSIONS["v5"].text
    assert v5_text.startswith(PROMPT_VERSIONS["v4"].text)
    assert "MECHANICS CLARIFICATIONS" in v5_text
    assert "CONDITION SCOPE" in v5_text
    assert "Dancing Lights" in v5_text and "Darkvision" in v5_text
    assert "CLASS_FEATURE VS MONSTER ABILITIES" in v5_text
    assert "do not extract monster abilities as" in v5_text


def test_v6_is_active_and_extends_v5():
    """v6 is the active version, carries the same schema OBJECT as v4/v5 (now
    widened with the `table` entity_type via EXTRACT_SCHEMA), and contains v5's
    text verbatim (built by concatenation) plus the table / class_feature-heading /
    anthology clarifications."""
    assert ACTIVE_PROMPT_VERSION == "v6"
    assert PROMPT_VERSIONS["v6"].schema is PROMPT_VERSIONS["v4"].schema
    v6_text = PROMPT_VERSIONS["v6"].text
    assert v6_text.startswith(PROMPT_VERSIONS["v5"].text)
    # The table summary must be descriptive (it is the only text embedded for
    # entity vector search besides the name).
    assert "found by meaning, not just by caption" in v6_text
    assert "TABLES ARE ONE ENTITY" in v6_text
    assert "LEVEL N: FEATURE NAME" in v6_text
    assert "adventure-anthology" in v6_text
    # A table's rows live in detail (never embedded), so its summary must carry the
    # searchable meaning; the addendum requires a genuinely descriptive summary.
    assert "summary MUST be genuinely descriptive" in v6_text


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


# --- relationship type-signature validator (spec #2) -----------------------


def _extract_one_edge(
    session: Session,
    entities: list[dict],
    relationships: list[dict],
) -> tuple[dict, list[Relationship]]:
    """Run one chunk carrying the given entities + relationships; return the
    summary and the persisted Relationship rows (validator runs before write)."""
    chunk = _make_chunk(session, "lmop", "lmop-001", "A chunk body.")
    or_client = FakeOpenRouterClient(
        {
            chunk.content: {
                "entities": entities,
                "mentions": [],
                "relationships": relationships,
            }
        }
    )
    summary = _run(extract_chunks(session, or_client, FakeEmbedClient(), limit=25))
    rels = session.execute(select(Relationship)).scalars().all()
    return summary, rels


def _endpoint_names(session: Session, rel: Relationship) -> tuple[str, str]:
    return (
        session.get(Entity, rel.from_entity_id).name,
        session.get(Entity, rel.to_entity_id).name,
    )


def test_apply_rel_signature_unit_covers_each_branch():
    """The pure validator: keep / spatial-keep / spatial-swap / non-spatial-swap /
    downgrade."""
    # Valid as-is -> keep.
    assert _apply_rel_signature("npc", "faction", "MEMBER_OF") == (
        "MEMBER_OF",
        False,
        None,
    )
    # Spatial magical exception (location inside an item) -> left alone.
    assert _apply_rel_signature("location", "item", "LOCATED_IN") == (
        "LOCATED_IN",
        False,
        None,
    )
    # Narrow spatial swap: place LOCATED_IN agent -> agent LOCATED_IN place.
    assert _apply_rel_signature("location", "npc", "LOCATED_IN") == (
        "LOCATED_IN",
        True,
        "swap",
    )
    # Non-spatial reversed + asymmetric -> swap.
    assert _apply_rel_signature("faction", "npc", "MEMBER_OF") == (
        "MEMBER_OF",
        True,
        "swap",
    )
    # Impossible in both directions (SERVES against a place) -> downgrade.
    assert _apply_rel_signature("npc", "location", "SERVES") == (
        "RELATED_TO",
        False,
        "downgrade",
    )
    # A rel_type without a signature is untouched.
    assert _apply_rel_signature("faction", "spell", "GRANTS") == (
        "GRANTS",
        False,
        None,
    )


def test_rel_signature_auto_swaps_place_located_in_agent(session: Session):
    """`Cragmaw Castle LOCATED_IN King Grol` is backwards: the validator swaps it
    to `King Grol LOCATED_IN Cragmaw Castle` and records the swap by rel_type."""
    summary, rels = _extract_one_edge(
        session,
        entities=[
            {"entity_type": "location", "name": "Cragmaw Castle", "summary": "A ruin."},
            {"entity_type": "npc", "name": "King Grol", "summary": "A bugbear."},
        ],
        relationships=[
            {
                "from_name": "Cragmaw Castle",
                "to_name": "King Grol",
                "rel_type": "LOCATED_IN",
            }
        ],
    )
    assert summary["relationships_created"] == 1
    assert summary["rel_signature_swaps"] == {"LOCATED_IN": 1}
    assert summary["rel_signature_downgrades"] == {}
    assert len(rels) == 1
    assert rels[0].rel_type == "LOCATED_IN"
    assert _endpoint_names(session, rels[0]) == ("King Grol", "Cragmaw Castle")


def test_rel_signature_downgrades_serves_a_location(session: Session):
    """`Sildar SERVES Neverwinter` (serving a place) is impossible in both
    directions: keep the edge but downgrade rel_type to RELATED_TO."""
    summary, rels = _extract_one_edge(
        session,
        entities=[
            {"entity_type": "npc", "name": "Sildar", "summary": "A soldier."},
            {"entity_type": "location", "name": "Neverwinter", "summary": "A city."},
        ],
        relationships=[
            {"from_name": "Sildar", "to_name": "Neverwinter", "rel_type": "SERVES"}
        ],
    )
    assert summary["relationships_created"] == 1
    assert summary["rel_signature_downgrades"] == {"SERVES": 1}
    assert summary["rel_signature_swaps"] == {}
    assert len(rels) == 1
    assert rels[0].rel_type == "RELATED_TO"
    # Direction is NOT swapped on a downgrade; the edge is preserved as emitted.
    assert _endpoint_names(session, rels[0]) == ("Sildar", "Neverwinter")


def test_rel_signature_preserves_magical_exception_location_in_item(session: Session):
    """A location inside an item is a magical exception the text can assert; the
    validator leaves `location LOCATED_IN item` untouched (no swap, no downgrade)."""
    summary, rels = _extract_one_edge(
        session,
        entities=[
            {
                "entity_type": "location",
                "name": "Demiplane of Dread",
                "summary": "A pocket realm.",
            },
            {
                "entity_type": "item",
                "name": "Amulet of Binding",
                "summary": "A cursed amulet.",
            },
        ],
        relationships=[
            {
                "from_name": "Demiplane of Dread",
                "to_name": "Amulet of Binding",
                "rel_type": "LOCATED_IN",
            }
        ],
    )
    assert summary["relationships_created"] == 1
    assert summary["rel_signature_swaps"] == {}
    assert summary["rel_signature_downgrades"] == {}
    assert rels[0].rel_type == "LOCATED_IN"
    assert _endpoint_names(session, rels[0]) == (
        "Demiplane of Dread",
        "Amulet of Binding",
    )


def test_rel_signature_swaps_reversed_non_spatial_edge(session: Session):
    """`Redbrands MEMBER_OF Glasstaff` (faction is a member of a person) is
    backwards: swap to `Glasstaff MEMBER_OF Redbrands`."""
    summary, rels = _extract_one_edge(
        session,
        entities=[
            {"entity_type": "faction", "name": "Redbrands", "summary": "A gang."},
            {"entity_type": "npc", "name": "Glasstaff", "summary": "A wizard."},
        ],
        relationships=[
            {"from_name": "Redbrands", "to_name": "Glasstaff", "rel_type": "MEMBER_OF"}
        ],
    )
    assert summary["relationships_created"] == 1
    assert summary["rel_signature_swaps"] == {"MEMBER_OF": 1}
    assert summary["rel_signature_downgrades"] == {}
    assert rels[0].rel_type == "MEMBER_OF"
    assert _endpoint_names(session, rels[0]) == ("Glasstaff", "Redbrands")


# --- v4 generic typed extraction (category / temporality / detail / new rels) --


def _extract_entities(
    session: Session, entities: list[dict]
) -> tuple[dict, list[Entity]]:
    """Run one chunk carrying the given entities; return the summary and the
    persisted Entity rows (re-selected so the DB-derived category is loaded)."""
    chunk = _make_chunk(session, "phb", "phb-001", "A rules chunk.")
    or_client = FakeOpenRouterClient(
        {chunk.content: {"entities": entities, "mentions": [], "relationships": []}}
    )
    summary = _run(extract_chunks(session, or_client, FakeEmbedClient(), limit=25))
    rows = session.execute(select(Entity)).scalars().all()
    return summary, rows


def test_category_derived_for_each_category_including_spell_lore(session: Session):
    """category is a stored generated column derived from entity_type: lore for the
    seven lore types (spell included), gameplay for event/quest, mechanics for the
    rules types. spell is lore at the column level (the mechanics surface unions it
    in at query time, not here)."""
    _summary, rows = _extract_entities(
        session,
        entities=[
            {"entity_type": "creature", "name": "Beholder", "summary": "An eye."},
            {"entity_type": "spell", "name": "Fireball", "summary": "A boom."},
            {"entity_type": "item", "name": "Sun Blade", "summary": "A sword."},
            {"entity_type": "event", "name": "Sundering", "summary": "A cataclysm."},
            {"entity_type": "quest", "name": "Find the Gem", "summary": "A task."},
            {"entity_type": "condition", "name": "Poisoned", "summary": "Ill."},
            {"entity_type": "class", "name": "Wizard", "summary": "A caster."},
            {"entity_type": "class_feature", "name": "Rage", "summary": "Anger."},
        ],
    )
    by_name = {e.name: e for e in rows}
    assert by_name["Beholder"].category == "lore"
    assert by_name["Fireball"].category == "lore"  # spell derives to lore
    assert by_name["Sun Blade"].category == "lore"
    assert by_name["Sundering"].category == "gameplay"
    assert by_name["Find the Gem"].category == "gameplay"
    assert by_name["Poisoned"].category == "mechanics"
    assert by_name["Wizard"].category == "mechanics"
    assert by_name["Rage"].category == "mechanics"


def test_temporality_set_only_for_event_and_quest(session: Session):
    """temporality is lifted onto the spine for event/quest only; a creature that
    (wrongly) carries a temporality field is ignored."""
    _summary, rows = _extract_entities(
        session,
        entities=[
            {
                "entity_type": "event",
                "name": "Dragon War",
                "summary": "A long war.",
                "temporality": "historical",
            },
            {
                "entity_type": "quest",
                "name": "Stop the Ritual",
                "summary": "Urgent.",
                "detail": {"temporality": "present", "objective": "Stop it"},
            },
            {
                "entity_type": "creature",
                "name": "Goblin",
                "summary": "Small.",
                "temporality": "future",
            },
        ],
    )
    by_name = {e.name: e for e in rows}
    assert by_name["Dragon War"].temporality == "historical"
    assert by_name["Stop the Ritual"].temporality == "present"  # read from detail
    assert by_name["Goblin"].temporality is None  # ignored for non-gameplay types


def test_new_type_persists_with_generic_detail_jsonb(session: Session):
    """A gameplay/mechanics type has no typed detail table: its detail is stored on
    the entity's generic JSONB column, and category is derived."""
    _summary, rows = _extract_entities(
        session,
        entities=[
            {
                "entity_type": "quest",
                "name": "Retrieve the Orb",
                "summary": "Fetch it.",
                "detail": {
                    "objective": "Bring the Orb to Sildar",
                    "giver": "Sildar",
                    "reward": "500 gp",
                    "quest_type": "side",
                    "temporality": "present",
                },
            }
        ],
    )
    quest = rows[0]
    assert quest.entity_type == "quest"
    assert quest.category == "gameplay"
    assert quest.temporality == "present"
    assert quest.detail["objective"] == "Bring the Orb to Sildar"
    assert quest.detail["quest_type"] == "side"


def test_class_feature_accepted_as_own_type_not_spell(session: Session):
    """class_feature is a first-class entity_type (mechanics), never coerced to
    spell, and gets no entity_spell detail row."""
    from grimoire.models import EntitySpell

    _summary, rows = _extract_entities(
        session,
        entities=[
            {
                "entity_type": "class_feature",
                "name": "Sneak Attack",
                "summary": "Extra damage.",
                "detail": {"class": "Rogue", "level": 1},
            }
        ],
    )
    feature = rows[0]
    assert feature.entity_type == "class_feature"
    assert feature.category == "mechanics"
    assert feature.detail["class"] == "Rogue"
    # No spell detail table row was created for it.
    assert session.execute(select(EntitySpell)).scalars().all() == []


def test_apply_rel_signature_new_v4_rels():
    """The v4 asymmetric rels swap a reversed edge and downgrade an off-type one."""
    # Valid as-is.
    assert _apply_rel_signature("event", "location", "OCCURRED_AT") == (
        "OCCURRED_AT",
        False,
        None,
    )
    assert _apply_rel_signature("subclass", "class", "SUBCLASS_OF") == (
        "SUBCLASS_OF",
        False,
        None,
    )
    # Reversed asymmetric -> swap.
    assert _apply_rel_signature("location", "event", "OCCURRED_AT") == (
        "OCCURRED_AT",
        True,
        "swap",
    )
    # Off-type in both directions -> downgrade (quest GIVEN_BY a location).
    assert _apply_rel_signature("quest", "location", "GIVEN_BY") == (
        "RELATED_TO",
        False,
        "downgrade",
    )


def test_rel_signature_swaps_reversed_occurred_at(session: Session):
    """`Neverwinter OCCURRED_AT Siege` is backwards: swap to `Siege OCCURRED_AT
    Neverwinter` (event -> location)."""
    summary, rels = _extract_one_edge(
        session,
        entities=[
            {"entity_type": "location", "name": "Neverwinter", "summary": "A city."},
            {"entity_type": "event", "name": "Siege", "summary": "A battle."},
        ],
        relationships=[
            {
                "from_name": "Neverwinter",
                "to_name": "Siege",
                "rel_type": "OCCURRED_AT",
            }
        ],
    )
    assert summary["relationships_created"] == 1
    assert summary["rel_signature_swaps"] == {"OCCURRED_AT": 1}
    assert summary["rel_signature_downgrades"] == {}
    assert rels[0].rel_type == "OCCURRED_AT"
    assert _endpoint_names(session, rels[0]) == ("Siege", "Neverwinter")


def test_rel_signature_downgrades_given_by_wrong_endpoint(session: Session):
    """`Find the Gem GIVEN_BY Neverwinter` (a quest given by a place) is impossible
    in both directions: keep the edge but downgrade rel_type to RELATED_TO."""
    summary, rels = _extract_one_edge(
        session,
        entities=[
            {"entity_type": "quest", "name": "Find the Gem", "summary": "A task."},
            {"entity_type": "location", "name": "Neverwinter", "summary": "A city."},
        ],
        relationships=[
            {
                "from_name": "Find the Gem",
                "to_name": "Neverwinter",
                "rel_type": "GIVEN_BY",
            }
        ],
    )
    assert summary["relationships_created"] == 1
    assert summary["rel_signature_downgrades"] == {"GIVEN_BY": 1}
    assert summary["rel_signature_swaps"] == {}
    assert rels[0].rel_type == "RELATED_TO"
    assert _endpoint_names(session, rels[0]) == ("Find the Gem", "Neverwinter")


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
    """extract_chunks threads the hierarchy (preferred over leaf for a non-bestiary
    book), image_ref, and book title + genre into the extract call. Bestiaries take
    the leaf-only quarantine path, covered separately below."""
    session.add(Book(id="curse-of-strahd", display_name="Curse of Strahd"))
    chunk = KnowledgeChunk(
        book_id="curse-of-strahd",
        chunk_ref="cos-001",
        content="A caption of a castle.",
        section_path="A > Castle Ravenloft",
        section_hierarchy="Chapter 4: Barovia > Svalich Woods > Castle Ravenloft",
        image_ref="s3://grimoire/img/castle.png",
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
        "Chapter 4: Barovia > Svalich Woods > Castle Ravenloft"
    )
    assert captured["image_ref"] == "s3://grimoire/img/castle.png"
    assert captured["book_title"] == "Curse of Strahd"
    assert captured["book_kind"] == "adventure"


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
    # Mixed lore + player options + bestiary compendia map to bestiary.
    assert book_kind("fizbans-treasury-of-dragons") == "bestiary"
    assert book_kind("bigby-presents-glory-of-giants") == "bestiary"
    # Majority-lore book around the Deck of Many Things.
    assert book_kind("the-book-of-many-things") == "setting-guide"
    # Unmapped slug: no guessed genre.
    assert book_kind("some-random-homebrew") is None


def test_book_kind_adventure_anthology_split():
    from grimoire.extract import ADVENTURE_BOOK_KINDS

    assert book_kind("candlekeep-mysteries") == "adventure-anthology"
    assert book_kind("tales-from-the-yawning-portal") == "adventure-anthology"
    assert book_kind("keys-from-the-golden-vault") == "adventure-anthology"
    assert book_kind("ghosts-of-saltmarsh") == "adventure-anthology"
    assert book_kind("tomb-of-annihilation") == "adventure"
    assert book_kind("descent-into-avernus") == "adventure"
    assert book_kind("the-wild-beyond-the-witchlight") == "adventure"
    assert book_kind("waterdeep-dungeon-of-the-mad-mage") == "adventure"
    # Unmapped slug still returns None, not a guessed adventure kind.
    assert book_kind("some-random-homebrew") is None
    assert ADVENTURE_BOOK_KINDS == {"adventure", "adventure-anthology"}


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


def test_in_cluster_uses_both_structured_output_dialects():
    """In-cluster engines receive the same schema in both vendor extensions."""
    client = OpenRouterClient(
        api_key="", base_url="http://inference/v1/chat/completions", prompt_version="v2"
    )
    fmt = client._format_kwargs()
    assert fmt["guided_json"] == PROMPT_VERSIONS["v2"].schema
    assert fmt["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "grimoire_extraction",
            "strict": True,
            "schema": PROMPT_VERSIONS["v2"].schema,
        },
    }


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


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


@pytest.mark.asyncio
async def test_deepseek_disables_reasoning_via_thinking(monkeypatch):
    """Direct DeepSeek gets thinking:{type:disabled}, never OpenRouter's
    reasoning field, and never the provider-routing block (even if env set)."""
    monkeypatch.setenv("GRIMOIRE_EXTRACT_PROVIDER", "deepseek")
    client = OpenRouterClient(api_key="k", base_url=DEEPSEEK_URL)
    payload = await _capture_payload(client)
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning" not in payload
    assert "provider" not in payload


def test_deepseek_uses_json_object_not_schema_or_guided():
    """Direct DeepSeek takes plain json_object (no strict json_schema block, no
    vLLM guided_json); the post-parse safety net covers the enum."""
    client = OpenRouterClient(api_key="k", base_url=DEEPSEEK_URL, prompt_version="v2")
    fmt = client._format_kwargs()
    assert fmt == {"response_format": {"type": "json_object"}}


def test_client_reads_grimoire_extract_api_key(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_EXTRACT_API_KEY", "ds-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert OpenRouterClient().api_key == "ds-key"


def test_client_falls_back_to_openrouter_api_key(monkeypatch):
    monkeypatch.delenv("GRIMOIRE_EXTRACT_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    assert OpenRouterClient().api_key == "or-key"


def test_client_explicit_api_key_wins_over_env(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_EXTRACT_API_KEY", "ds-key")
    assert OpenRouterClient(api_key="explicit").api_key == "explicit"


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
    assert payload["guided_json"] is PROMPT_VERSIONS["v2"].schema
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "grimoire_extraction",
            "strict": True,
            "schema": PROMPT_VERSIONS["v2"].schema,
        },
    }
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


# --- extraction hardening: site-scoped locations, numeric gate, provenance ----


def _make_chunk_h(
    session: Session,
    book_id: str,
    chunk_ref: str,
    content: str,
    hierarchy: str,
) -> KnowledgeChunk:
    """A chunk carrying a section_hierarchy breadcrumb (for the site/quarantine
    paths), committed and refreshed like _make_chunk."""
    chunk = KnowledgeChunk(
        book_id=book_id,
        chunk_ref=chunk_ref,
        content=content,
        section_hierarchy=hierarchy,
    )
    session.add(chunk)
    session.commit()
    session.refresh(chunk)
    return chunk


def _location_response(name: str, summary: str) -> dict:
    return {
        "entities": [{"entity_type": "location", "name": name, "summary": summary}],
        "mentions": [],
        "relationships": [],
    }


def test_location_same_name_different_sites_stay_separate(session: Session):
    """Two rooms both named 'Kitchen' under different sites in ONE book are two
    distinct location entities, not one merged node."""
    c1 = _make_chunk_h(
        session, "cos", "cos-001", "A castle kitchen.", "Castle Ravenloft > Kitchen"
    )
    c2 = _make_chunk_h(
        session, "cos", "cos-002", "A village kitchen.", "Village of Barovia > Kitchen"
    )
    responses = {
        c1.content: _location_response("Kitchen", "In the castle."),
        c2.content: _location_response("Kitchen", "In the village."),
    }
    _run(extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient()))

    locations = (
        session.execute(select(Entity).where(Entity.entity_type == "location"))
        .scalars()
        .all()
    )
    assert len(locations) == 2
    assert {e.site for e in locations} == {"castle ravenloft", "village of barovia"}


def test_location_same_room_entry_reused(session: Session):
    """A second keyed entry for the same room (same name, site, and book) reuses
    the existing node instead of forking a duplicate."""
    c1 = _make_chunk_h(
        session, "cos", "cos-001", "Kitchen, first pass.", "Castle Ravenloft > Kitchen"
    )
    c2 = _make_chunk_h(
        session, "cos", "cos-002", "Kitchen, second pass.", "Castle Ravenloft > Kitchen"
    )
    responses = {
        c1.content: _location_response("Kitchen", "A."),
        c2.content: _location_response("Kitchen", "B."),
    }
    summary = _run(
        extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient())
    )

    locations = (
        session.execute(select(Entity).where(Entity.entity_type == "location"))
        .scalars()
        .all()
    )
    assert len(locations) == 1
    assert locations[0].site == "castle ravenloft"
    assert summary["entities_created"] == 1
    assert summary["entities_reused"] == 1


def test_location_same_name_different_books_stay_separate(session: Session):
    """The same room name in two different books never merges (dedup is
    book-scoped for locations)."""
    c1 = _make_chunk_h(
        session, "cos", "cos-001", "Kitchen in cos.", "Castle Ravenloft > Kitchen"
    )
    c2 = _make_chunk_h(
        session, "lmop", "lmop-001", "Kitchen in lmop.", "Cragmaw Castle > Kitchen"
    )
    responses = {
        c1.content: _location_response("Kitchen", "A."),
        c2.content: _location_response("Kitchen", "B."),
    }
    _run(extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient()))

    locations = (
        session.execute(select(Entity).where(Entity.entity_type == "location"))
        .scalars()
        .all()
    )
    assert len(locations) == 2
    assert {e.source_book for e in locations} == {"cos", "lmop"}


def test_location_prose_reference_reuses_unique_same_book_candidate(session: Session):
    """A site-less (prose) reference to a location resolves to the book's single
    same-named candidate, the keyed room entry, rather than forking a new node."""
    c1 = _make_chunk_h(
        session, "swn", "swn-001", "The town of Nightstone.", "Sword Coast > Nightstone"
    )
    # No breadcrumb: a bare prose reference, so _room_site returns None.
    c2 = _make_chunk(session, "swn", "swn-002", "Nightstone is mentioned again.")
    responses = {
        c1.content: _location_response("Nightstone", "A town."),
        c2.content: _location_response("Nightstone", "Seen again."),
    }
    summary = _run(
        extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient())
    )

    locations = (
        session.execute(select(Entity).where(Entity.entity_type == "location"))
        .scalars()
        .all()
    )
    assert len(locations) == 1
    # The keyed entry's site is retained; the prose reference reused it.
    assert locations[0].site == "sword coast"
    assert summary["entities_reused"] == 1


def test_creature_dedup_remains_global_across_books(session: Session):
    """Only locations are book-scoped; a creature of the same name still dedups
    globally across books."""
    c1 = _make_chunk(session, "mm", "mm-001", "A goblin lurks.")
    c2 = _make_chunk(session, "tob", "tob-001", "A goblin reappears.")
    responses = {
        c1.content: {
            "entities": [
                {"entity_type": "creature", "name": "Goblin", "summary": "A."}
            ],
            "mentions": [],
            "relationships": [],
        },
        c2.content: {
            "entities": [
                {"entity_type": "creature", "name": "Goblin", "summary": "B."}
            ],
            "mentions": [],
            "relationships": [],
        },
    }
    _run(extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient()))

    creatures = (
        session.execute(select(Entity).where(Entity.entity_type == "creature"))
        .scalars()
        .all()
    )
    assert len(creatures) == 1


def test_numeric_gate_keeps_grounded_ac_drops_absent_hp(session: Session):
    """An AC anchored in the chunk text is kept; an HP the model invented (absent
    from the text) is dropped and tallied in detail_ungrounded_drops."""
    chunk = _make_chunk(session, "mm", "mm-001", "The ogre is tough. Armor Class 15.")
    responses = {
        chunk.content: {
            "entities": [
                {
                    "entity_type": "creature",
                    "name": "Ogre",
                    "summary": "Big.",
                    "detail": {"ac": 15, "hp_avg": 200},
                }
            ],
            "mentions": [],
            "relationships": [],
        }
    }
    summary = _run(
        extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient())
    )

    detail = session.execute(select(EntityCreature)).scalars().one()
    assert detail.ac == 15
    assert detail.hp_avg is None
    assert summary["detail_ungrounded_drops"] == {"hp_avg": 1}


def test_numeric_gate_cr_grounded_by_fraction_form(session: Session):
    """A fractional CR grounds against the fraction spelling in the text
    ('Challenge 1/2' grounds cr=0.5)."""
    chunk = _make_chunk(session, "mm", "mm-001", "A weak kobold. Challenge 1/2.")
    responses = {
        chunk.content: {
            "entities": [
                {
                    "entity_type": "creature",
                    "name": "Kobold",
                    "summary": "Small.",
                    "detail": {"cr": 0.5},
                }
            ],
            "mentions": [],
            "relationships": [],
        }
    }
    summary = _run(
        extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient())
    )

    detail = session.execute(select(EntityCreature)).scalars().one()
    assert detail.cr == pytest.approx(0.5)
    assert summary["detail_ungrounded_drops"] == {}


def test_numeric_gate_spell_level_zero_grounded_by_cantrip(session: Session):
    """Spell level 0 grounds on the word 'cantrip'."""
    from grimoire.models import EntitySpell

    chunk = _make_chunk(
        session, "phb", "phb-001", "Prestidigitation is a cantrip of minor magic."
    )
    responses = {
        chunk.content: {
            "entities": [
                {
                    "entity_type": "spell",
                    "name": "Prestidigitation",
                    "summary": "Minor magic.",
                    "detail": {"level": 0, "school": "transmutation"},
                }
            ],
            "mentions": [],
            "relationships": [],
        }
    }
    summary = _run(
        extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient())
    )

    detail = session.execute(select(EntitySpell)).scalars().one()
    assert detail.level == 0
    assert detail.school == "transmutation"
    assert summary["detail_ungrounded_drops"] == {}


def test_self_loop_edge_dropped(session: Session):
    """An edge whose endpoints resolve to the same entity (X -> X) is dropped and
    counted in rel_self_loops_dropped."""
    chunk = _make_chunk(session, "cos", "cos-001", "Strahd broods alone.")
    responses = {
        chunk.content: {
            "entities": [
                {"entity_type": "npc", "name": "Strahd", "summary": "A vampire."}
            ],
            "mentions": [],
            "relationships": [
                {"from_name": "Strahd", "to_name": "Strahd", "rel_type": "ENEMY_OF"}
            ],
        }
    }
    summary = _run(
        extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient())
    )

    assert summary["rel_self_loops_dropped"] == 1
    assert summary["relationships_created"] == 0
    assert session.execute(select(Relationship)).scalars().all() == []


def test_relationship_rows_carry_chunk_id(session: Session):
    """A newly created relationship row records the chunk it was extracted from."""
    chunk = _make_chunk(session, "lmop", "lmop-001", "Klarg leads the Cragmaw goblins.")
    responses = {
        chunk.content: {
            "entities": [
                {"entity_type": "npc", "name": "Klarg", "summary": "A bugbear."},
                {"entity_type": "faction", "name": "Cragmaw", "summary": "A tribe."},
            ],
            "mentions": [],
            "relationships": [
                {"from_name": "Klarg", "to_name": "Cragmaw", "rel_type": "LEADER_OF"}
            ],
        }
    }
    _run(extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient()))

    rel = session.execute(select(Relationship)).scalars().one()
    assert rel.chunk_id == chunk.id


def test_bestiary_passes_leaf_only_section_context(session: Session):
    """A bestiary chunk gets leaf-only section context (the backfilled hierarchy
    mis-nests, so it is quarantined in favor of the 2-level section_path)."""
    session.add(Book(id="monster-manual", display_name="Monster Manual"))
    chunk = KnowledgeChunk(
        book_id="monster-manual",
        chunk_ref="mm-001",
        content="Aarakocra patrol the Howling Gyre.",
        section_path="AARAKOCRA",
        section_hierarchy="Appendix A > Misnested Sibling > AARAKOCRA",
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
            captured.update(section_path=section_path, book_kind=book_kind)
            return {"entities": [], "mentions": [], "relationships": []}

    _run(extract_chunks(session, CapturingClient({}), FakeEmbedClient(), limit=25))

    assert captured["book_kind"] == "bestiary"
    # Leaf-only: the mis-nesting hierarchy is dropped, the 2-level section_path wins.
    assert captured["section_path"] == "AARAKOCRA"


def test_adventure_passes_full_hierarchy_section_context(session: Session):
    """A non-bestiary (adventure) chunk keeps the full ancestry breadcrumb."""
    session.add(Book(id="curse-of-strahd", display_name="Curse of Strahd"))
    chunk = KnowledgeChunk(
        book_id="curse-of-strahd",
        chunk_ref="cos-001",
        content="A blood-stained surgery.",
        section_path="Chapter 13 > Surgery",
        section_hierarchy="Chapter 13: The Abbey > L17. Surgery",
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
            captured.update(section_path=section_path, book_kind=book_kind)
            return {"entities": [], "mentions": [], "relationships": []}

    _run(extract_chunks(session, CapturingClient({}), FakeEmbedClient(), limit=25))

    assert captured["book_kind"] == "adventure"
    assert captured["section_path"] == "Chapter 13: The Abbey > L17. Surgery"


# --- v6 table type (one entity per table, book-scoped dedup, row merge) --------


def _table_response(name: str, detail: dict) -> dict:
    return {
        "entities": [
            {
                "entity_type": "table",
                "name": name,
                "summary": "A rollable table.",
                "detail": detail,
            }
        ],
        "mentions": [],
        "relationships": [],
    }


def test_table_entity_persists_with_mechanics_category_and_detail(session: Session):
    """A table chunk yields ONE 'table' entity (not N row entities): it derives
    category 'mechanics' and stores its structure in the generic detail JSONB."""
    _summary, rows = _extract_entities(
        session,
        entities=[
            {
                "entity_type": "table",
                "name": "Random Encounters",
                "summary": "Roll for a wandering threat.",
                "detail": {
                    "dice": "d100",
                    "columns": ["Roll", "Encounter"],
                    "rows": {"01-10": "2 goblins", "11-20": "A brown bear"},
                },
            }
        ],
    )
    assert len(rows) == 1  # one table entity, no row entities
    table = rows[0]
    assert table.entity_type == "table"
    assert table.category == "mechanics"  # ELSE branch of the derived category
    assert table.detail["dice"] == "d100"
    assert table.detail["columns"] == ["Roll", "Encounter"]
    assert table.detail["rows"]["01-10"] == "2 goblins"


def test_table_same_name_different_books_stay_separate(session: Session):
    """The same table caption in two books stays two entities (table dedup is
    book-scoped, like locations)."""
    c1 = _make_chunk(session, "dmg", "dmg-001", "Treasure table, DMG.")
    c2 = _make_chunk(session, "xge", "xge-001", "Treasure table, XGE.")
    responses = {
        c1.content: _table_response("Random Encounters", {"rows": {"01": "A"}}),
        c2.content: _table_response("Random Encounters", {"rows": {"01": "B"}}),
    }
    _run(extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient()))

    tables = (
        session.execute(select(Entity).where(Entity.entity_type == "table"))
        .scalars()
        .all()
    )
    assert len(tables) == 2
    assert {e.source_book for e in tables} == {"dmg", "xge"}
    # Tables carry no site (unlike locations); the spine site column stays NULL.
    assert {e.site for e in tables} == {None}


def test_table_same_book_reused_and_rows_key_merged(session: Session):
    """The same table name within one book reuses the existing entity and key-merges
    its rows: an existing row key wins on conflict, a new row key is added."""
    c1 = _make_chunk(session, "dmg", "dmg-001", "Magic Item Table A, first half.")
    c2 = _make_chunk(session, "dmg", "dmg-002", "Magic Item Table A, second half.")
    responses = {
        c1.content: _table_response(
            "Magic Item Table A",
            {"dice": "d100", "rows": {"01-50": "Potion of Healing"}},
        ),
        c2.content: _table_response(
            # 01-50 conflicts (chunk1 must win); 51-60 is a new row key.
            "Magic Item Table A",
            {"rows": {"01-50": "WRONG", "51-60": "+1 Armor"}},
        ),
    }
    summary = _run(
        extract_chunks(session, FakeOpenRouterClient(responses), FakeEmbedClient())
    )

    tables = (
        session.execute(select(Entity).where(Entity.entity_type == "table"))
        .scalars()
        .all()
    )
    assert len(tables) == 1
    assert summary["entities_created"] == 1
    assert summary["entities_reused"] == 1
    detail = tables[0].detail
    assert detail["dice"] == "d100"  # retained from chunk1
    assert detail["rows"] == {"01-50": "Potion of Healing", "51-60": "+1 Armor"}


def test_table_rows_merge_across_separate_runs(session: Session):
    """A table split across chunks processed in SEPARATE runs accumulates rows: the
    second run reuses the committed table entity and adds its new row keys without
    clobbering the existing ones (the prompt's 'table continues from a previous
    chunk' contract)."""
    c1 = _make_chunk(session, "dmg", "dmg-001", "Wild Magic Surge, part 1.")
    c2 = _make_chunk(session, "dmg", "dmg-002", "Wild Magic Surge, part 2.")
    responses = {
        c1.content: _table_response(
            "Wild Magic Surge", {"dice": "d100", "rows": {"01-02": "Reroll"}}
        ),
        c2.content: _table_response(
            "Wild Magic Surge", {"rows": {"03-04": "Fireball", "01-02": "CLOBBER"}}
        ),
    }
    or_client = FakeOpenRouterClient(responses)
    embed_client = FakeEmbedClient()
    _run(extract_chunks(session, or_client, embed_client, limit=1))  # commits c1
    _run(extract_chunks(session, or_client, embed_client, limit=1))  # reuses, merges

    tables = (
        session.execute(select(Entity).where(Entity.entity_type == "table"))
        .scalars()
        .all()
    )
    assert len(tables) == 1
    detail = tables[0].detail
    assert detail["dice"] == "d100"
    # New row key added, existing key kept (chunk1 wins the 01-02 conflict).
    assert detail["rows"] == {"01-02": "Reroll", "03-04": "Fireball"}
