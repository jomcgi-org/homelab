"""Original synthetic advice, actual ingest/retrieval/context, and route ACLs."""

import asyncio
import io
import json
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from core.db import get_session
from grimoire import guidance_context as guidance
from grimoire import ingest, public, search, topics
from grimoire.models import ChunkExtraction, Embedding, KnowledgeChunk
from grimoire.router_public import router as public_router
from grimoire.testing.leak_harness import MEMBERS, ROLES, fake_knn, sqlite_harness

# Original text. Source slugs model DMG and setting-guide metadata only.
ADVICE = "You can ask your players which conflicts interest their characters before preparing encounters."
SETTING_ADVICE = "As the Dungeon Master, consider linking the campaign's factions to goals your players choose."


class Embedder:
    model = "synthetic-test"

    def __init__(self):
        self.texts = []

    async def embed_batch(self, texts):
        self.texts.extend(texts)
        return [[1.0] + [0.0] * 1023 for _ in texts]

    async def embed(self, text):
        return (await self.embed_batch([text]))[0]


class Manifests:
    def __init__(self, books):
        self.objects = {
            f"books/{book}/chunks/001.ndjson": "\n".join(map(json.dumps, rows))
            for book, rows in books.items()
        }

    def list_objects_v2(self, **kwargs):
        return {"Contents": [{"Key": key} for key in self.objects]}

    def get_object(self, *, Key, **kwargs):
        return {"Body": io.BytesIO(self.objects[Key].encode())}


def manifest(content=ADVICE, **kwargs):
    return {
        "chunk_ref": "advice-001",
        "content": content,
        "section_path": "Getting Players Invested",
        **kwargs,
    }


@pytest.fixture
def harness(tmp_path):
    with sqlite_harness(tmp_path / "topics.db") as h:
        yield h


def load(h, books, embedder=None):
    embedder = embedder or Embedder()
    return asyncio.run(
        ingest.load_chunks(h.session, Manifests(books), embedder, "synthetic")
    )


def chunk(h, book):
    return h.session.exec(
        select(KnowledgeChunk).where(KnowledgeChunk.book_id == book)
    ).one()


def filtered_knn(session, vector, kinds, limit, model=None, where=None):
    # Stub only vector distance. Execute actual candidate SQL BEFORE limit.
    stmt = select(Embedding).where(Embedding.embeddable_kind.in_(kinds))
    if where is not None:
        stmt = stmt.where(where)
    return [
        (row, 0.1)
        for row in session.exec(stmt.order_by(Embedding.id).limit(limit)).all()
    ]


@pytest.mark.parametrize(
    "heading,content,expected",
    [
        ("Getting Players Invested", ADVICE, "dm_guidance"),
        ("Setting > The Glass Coast", SETTING_ADVICE, "dm_guidance"),
        (
            "Creating Encounters",
            "Consider varying encounters to give players several approaches.",
            "dm_guidance",
        ),
        ("Running the Game", "The adamantine door weighs six hundred pounds.", None),
        (
            "Dungeon Master's Guide > Treasure",
            "You can sell this gem for 50 gold pieces.",
            None,
        ),
        ("Pacing", "The pacing bird nests beside the campaign monument.", None),
        (
            "Running the Game",
            "You can move characters thirty feet during encounters.",
            None,
        ),
        ("Creating Encounters", "You can ask your players to roll initiative.", None),
        (None, "This weapon deals damage to nearby characters.", None),
        ("Encounters", "You can carry three torches during encounters.", None),
    ],
)
def test_classification_requires_chunk_advice(heading, content, expected):
    assert topics.classify_topic(content, heading, None) == expected


@pytest.mark.parametrize(
    "topic", ["advice", "", "DM_GUIDANCE", "dm_guidance,spell", [], 42]
)
def test_topic_validation(topic):
    with pytest.raises(ValueError):
        topics.validate_topic(topic)


def test_ingestion_refresh_identity_hierarchy_and_metadata_only(harness):
    h = harness
    embedder = Embedder()
    original = manifest(
        section_path="DM Toolbox", section_hierarchy="DMG > Getting Players Invested"
    )
    assert load(h, {"dmg-synthetic": [original]}, embedder)["chunks_embedded"] == 1
    row = chunk(h, "dmg-synthetic")
    identity, created = row.id, row.created_at
    marker = ChunkExtraction(
        chunk_id=row.id, model="test", prompt_version="v1", status="empty"
    )
    h.session.add(marker)
    h.session.commit()
    # Simulate stale/unclassified stored topic and an older manifest missing hierarchy.
    row.topic = None
    h.session.commit()
    assert (
        load(h, {"dmg-synthetic": [manifest(section_path="DM Toolbox")]}, embedder)[
            "chunks_embedded"
        ]
        == 0
    )
    assert row.topic == "dm_guidance"
    assert row.section_hierarchy == original["section_hierarchy"]
    assert len(embedder.texts) == 1
    assert (
        load(
            h,
            {
                "dmg-synthetic": [
                    manifest(section_path="DM Toolbox", section_hierarchy=" ")
                ]
            },
            embedder,
        )["chunks_embedded"]
        == 0
    )
    assert row.section_hierarchy == original["section_hierarchy"]
    result = load(h, {"dmg-synthetic": [manifest(section_path="DM Toolbox")]}, embedder)
    assert result["chunks_upserted"] == 0
    # Content replacement clears stale guidance and re-embeds exactly once.
    mundane = manifest("The gate is made of stone.")
    assert load(h, {"dmg-synthetic": [mundane]}, embedder)["chunks_embedded"] == 1
    assert row.topic is None and row.id == identity and row.created_at == created
    assert load(h, {"dmg-synthetic": [mundane]}, embedder)["chunks_embedded"] == 0
    assert h.session.get(ChunkExtraction, (identity, "test", "v1")) is not None


def test_paged_backfill_is_bounded_idempotent_and_does_not_embed(harness):
    h = harness
    load(
        h,
        {
            "setting-synthetic": [
                manifest(SETTING_ADVICE, chunk_ref=str(i)) for i in range(3)
            ],
            "other": [manifest()],
        },
    )
    rows = h.session.exec(
        select(KnowledgeChunk).where(KnowledgeChunk.book_id == "setting-synthetic")
    ).all()
    for row in rows:
        row.topic = None
    h.session.commit()
    before = [
        (r.model_dump(exclude={"vector"}), list(r.vector))
        for r in h.session.exec(select(Embedding)).all()
    ]
    first = topics.classify_stored_chunks(
        h.session, book_id="setting-synthetic", limit=2
    )
    assert first == {"scanned": 2, "changed": 2, "next_ref": "1"}
    assert (
        topics.classify_stored_chunks(h.session, book_id="setting-synthetic", limit=2)[
            "changed"
        ]
        == 0
    )
    assert topics.classify_stored_chunks(
        h.session, book_id="setting-synthetic", after_ref=first["next_ref"], limit=2
    ) == {"scanned": 1, "changed": 1, "next_ref": None}
    # Metadata-only function leaves transaction ownership to the caller.
    h.session.rollback()
    assert all(row.topic is None for row in rows)
    after = [
        (r.model_dump(exclude={"vector"}), list(r.vector))
        for r in h.session.exec(select(Embedding)).all()
    ]
    assert before == after
    assert chunk(h, "other").topic == "dm_guidance"
    with pytest.raises(ValueError):
        topics.classify_stored_chunks(h.session, book_id="setting-synthetic", limit=501)


def test_filtered_retrieval_reaches_beyond_unfiltered_top_k(harness, monkeypatch):
    h = harness
    load(
        h,
        {
            "dmg-synthetic": [manifest()],
            "setting-synthetic": [manifest(SETTING_ADVICE)],
        },
    )
    # Candidate ordering models 50 nearer irrelevant chunks ahead of guidance.
    irrelevant = [
        KnowledgeChunk(
            id=str(uuid4()),
            book_id="mundane",
            chunk_ref=str(i),
            content="A brass door.",
        )
        for i in range(50)
    ]
    h.session.add_all(irrelevant)
    h.session.flush()
    vectors = [
        Embedding(
            id=f"00000000-0000-0000-0000-{i:012d}",
            embeddable_kind="chunk",
            embeddable_id=row.id,
            model="test",
            dim=1024,
            vector=[1.0] * 1024,
        )
        for i, row in enumerate(irrelevant)
    ]
    h.session.add_all(vectors)
    h.session.commit()
    monkeypatch.setattr(search, "knn_embeddings", filtered_knn)
    member = h.rows["member_dm"]
    hits = asyncio.run(
        search.search_knowledge(
            h.session,
            Embedder(),
            member.campaign_id,
            "dm",
            member,
            "players",
            2,
            topic="dm_guidance",
        )
    )
    assert {hit["book_id"] for hit in hits} == {"dmg-synthetic", "setting-synthetic"}
    for hit in hits:
        assert hit["topic"] == "dm_guidance"
        assert hit["source"]["chunk_id"] == hit["id"]
        assert hit["source"]["section_path"] == "Getting Players Invested"
        assert hit["source"]["chunk_ref"] == "advice-001"
    unfiltered = asyncio.run(
        search.search_knowledge(
            h.session, Embedder(), member.campaign_id, "dm", member, "players", 2
        )
    )
    assert all(hit["book_id"] == "mundane" for hit in unfiltered)


def test_stale_and_deleted_sources_cannot_return_or_enter_context(harness, monkeypatch):
    h = harness
    load(
        h,
        {
            "dmg-synthetic": [manifest()],
            "setting-synthetic": [manifest(SETTING_ADVICE)],
        },
    )
    old = chunk(h, "dmg-synthetic")
    deleted = chunk(h, "setting-synthetic")
    ids = {old.id, deleted.id}
    candidates = [
        (r, 0.0)
        for r in h.session.exec(
            select(Embedding).where(Embedding.embeddable_id.in_(ids))
        ).all()
    ]
    # Warm the identity map, then mutate storage outside this Session.
    with h.session.get_bind().begin() as connection:
        connection.execute(
            KnowledgeChunk.__table__.update()
            .where(KnowledgeChunk.id == old.id)
            .values(topic=None)
        )
        connection.execute(
            KnowledgeChunk.__table__.delete().where(KnowledgeChunk.id == deleted.id)
        )
    monkeypatch.setattr(search, "knn_embeddings", lambda *a, **kw: candidates)
    member = h.rows["member_dm"]
    context = guidance.DMGuidanceContext(member.campaign_id, member.id)
    assert guidance.assemble_guidance_context(h.session, context, [1.0] * 1024) == []


def test_context_is_bounded_cited_and_requires_live_dm(harness, monkeypatch):
    h = harness
    load(
        h,
        {
            "dmg-synthetic": [manifest()],
            "setting-synthetic": [manifest(SETTING_ADVICE)],
        },
    )
    monkeypatch.setattr(search, "knn_embeddings", filtered_knn)
    member = h.rows["member_dm"]
    context = guidance.DMGuidanceContext(member.campaign_id, member.id)
    excerpts = asyncio.run(
        guidance.guidance_context(
            h.session.get_bind(),
            Embedder(),
            context,
            "players",
            max_results=2,
            per_chunk_chars=30,
            max_chars=45,
        )
    )
    assert [len(row["content"]) for row in excerpts] == [30, 15]
    assert {row["source"]["book_id"] for row in excerpts} == {
        "dmg-synthetic",
        "setting-synthetic",
    }
    assert all(
        row["source"]["chunk_id"] and row["source"]["section_path"] for row in excerpts
    )
    assert excerpts == asyncio.run(
        guidance.guidance_context(
            h.session.get_bind(),
            Embedder(),
            context,
            "players",
            max_results=2,
            per_chunk_chars=30,
            max_chars=45,
        )
    )
    for role in ("player_a", "no_character", "other_campaign"):
        member = h.rows[f"member_{role}"]
        with pytest.raises(PermissionError):
            guidance.assemble_guidance_context(
                h.session,
                guidance.DMGuidanceContext(context.campaign_id, member.id),
                [1.0] * 1024,
            )
    with pytest.raises(PermissionError):
        guidance.assemble_guidance_context(
            h.session,
            guidance.DMGuidanceContext(context.campaign_id, str(uuid4())),
            [1.0] * 1024,
        )
    with pytest.raises(ValueError):
        guidance.assemble_guidance_context(h.session, context, [], max_results=11)

    # Membership revoked during query embedding fails the assembly recheck.
    def revoke():
        with Session(h.session.get_bind()) as session:
            dm = session.get(type(h.rows["member_dm"]), context.authorized_member_id)
            dm.role = "player"
            session.commit()

    class RevokingEmbedder(Embedder):
        async def embed(self, text):
            revoke()
            return [1.0] * 1024

    with pytest.raises(PermissionError):
        asyncio.run(
            guidance.guidance_context(
                h.session.get_bind(), RevokingEmbedder(), context, "players"
            )
        )


@pytest.mark.parametrize("viewer", ROLES)
def test_campaign_topic_routes_preserve_membership_and_privacy(
    harness, monkeypatch, viewer
):
    h = harness
    load(h, {"dmg-synthetic": [manifest()]})
    monkeypatch.setattr(search, "knn_embeddings", fake_knn)
    with TestClient(h.app()) as client:
        response = client.get(
            f"/api/grimoire/campaigns/{h.rows['campaign'].id}/knowledge/search",
            headers=h.headers(viewer),
            params={"q": "players", "topic": "dm_guidance", "k": 50},
        )
        h.assert_no_leak(response, viewer)
        assert response.status_code == (200 if viewer in MEMBERS else 404)
        if viewer in MEMBERS:
            assert [hit["book_id"] for hit in response.json()] == ["dmg-synthetic"]
        invalid = client.get(
            f"/api/grimoire/campaigns/{h.rows['campaign'].id}/knowledge/search",
            headers=h.headers(viewer),
            params={"q": "players", "topic": "unknown"},
        )
        assert invalid.status_code == 422


def test_public_filter_is_corpus_only_before_limit_and_validated(harness):
    h = harness
    load(
        h,
        {
            "z-dmg": [manifest()],
            "z-setting": [manifest(SETTING_ADVICE)],
            "a-mundane": [
                manifest("Players camp here.", chunk_ref=str(i)) for i in range(25)
            ],
        },
    )
    assert len(public.search_public(h.session, "players")["lore"]) == 20
    hits = public.search_public(h.session, "players", topic="dm_guidance")
    assert hits["entities"] == []
    assert {hit["book_id"] for hit in hits["lore"]} == {"z-dmg", "z-setting"}
    assert all(
        hit["topic"] == "dm_guidance" and hit["source"]["chunk_id"]
        for hit in hits["lore"]
    )
    app = FastAPI()
    app.include_router(public_router)
    app.dependency_overrides[get_session] = lambda: h.session
    with TestClient(app) as client:
        response = client.get(
            "/api/grimoire/search", params={"q": "players", "topic": "dm_guidance"}
        )
        assert response.status_code == 200 and response.json() == hits
        assert (
            client.get(
                "/api/grimoire/search", params={"q": "players", "topic": "invalid"}
            ).status_code
            == 422
        )
        for token in h.canaries:
            assert token.casefold() not in response.text.casefold()
