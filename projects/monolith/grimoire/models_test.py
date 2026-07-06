"""Unit tests for grimoire.models: happy-path inserts and CHECK/UNIQUE
enforcement (mirrored on the model so SQLite create_all fixtures catch the
same violations Postgres would)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from grimoire.models import (
    Campaign,
    Entity,
    EntityCreature,
    KnowledgeGrant,
    PlayerCharacter,
)


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite has no schemas; strip the Postgres schema= override so
    # create_all() lands every table in the default schema, matching the
    # pattern in knowledge/models_test.py.
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


def _make_campaign_and_pc(session: Session) -> tuple[Campaign, PlayerCharacter]:
    campaign = Campaign(name="The Mighty Nein", dm_name="Matt")
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    pc = PlayerCharacter(
        campaign_id=campaign.id,
        player_name="Joe",
        character_name="Caleb",
        class_name="Wizard",
        level=5,
    )
    session.add(pc)
    session.commit()
    session.refresh(pc)
    return campaign, pc


def test_entity_creature_and_grant_happy_path(session: Session):
    entity = Entity(entity_type="creature", name="Umbrasyl", source_book="phb")
    session.add(entity)
    session.commit()
    session.refresh(entity)

    detail = EntityCreature(
        entity_id=entity.id,
        size="Gargantuan",
        creature_type="dragon",
        ac=19,
        hp_avg=367,
        cr=23,
        speed={"fly": 80, "walk": 40},
        ability_scores={"str": 27, "dex": 10},
        actions=[{"name": "Bite", "damage": "2d10+8"}],
        traits=[{"name": "Legendary Resistance"}],
    )
    session.add(detail)
    session.commit()

    campaign, pc = _make_campaign_and_pc(session)
    grant = KnowledgeGrant(
        campaign_id=campaign.id,
        entity_id=entity.id,
        player_character_id=pc.id,
        grant_scope="partial",
        revealed_details={"name": "a black dragon"},
    )
    session.add(grant)
    session.commit()
    session.refresh(grant)

    stored_entity = session.exec(select(Entity).where(Entity.id == entity.id)).one()
    stored_detail = session.exec(
        select(EntityCreature).where(EntityCreature.entity_id == entity.id)
    ).one()
    stored_grant = session.exec(
        select(KnowledgeGrant).where(KnowledgeGrant.id == grant.id)
    ).one()

    assert stored_entity.name == "Umbrasyl"
    assert stored_entity.is_global is True
    assert stored_detail.ac == 19
    assert stored_grant.grant_scope == "partial"
    assert stored_grant.revealed_details == {"name": "a black dragon"}


def test_knowledge_grant_rejects_bad_grant_scope(session: Session):
    """grant_scope is Literal-typed only; the SQL CHECK is what enforces at
    write time. Insert a bogus value via raw SQL and assert the constraint
    fires, same as knowledge/models_test.py's visibility CHECK test: the
    CheckConstraint is declared on the model so SQLModel.metadata.create_all
    emits it for SQLite too, keeping this test honest without a real Postgres."""
    entity = Entity(entity_type="npc", name="Yasha")
    campaign, pc = _make_campaign_and_pc(session)
    session.add(entity)
    session.commit()
    session.refresh(entity)

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO knowledge_grant "
                "(id, campaign_id, entity_id, player_character_id, grant_scope) "
                "VALUES (:id, :c, :e, :p, :s)"
            ),
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "c": campaign.id,
                "e": entity.id,
                "p": pc.id,
                "s": "secret",
            },
        )
        session.flush()
    session.rollback()


def test_chunk_extraction_marker_roundtrip(session: Session):
    from grimoire.models import ChunkExtraction

    row = ChunkExtraction(
        chunk_id="11111111-1111-1111-1111-111111111111",
        model="qwen3.6-27b",
        prompt_version="v2",
        status="empty",
    )
    session.add(row)
    session.commit()
    got = session.get(
        ChunkExtraction,
        ("11111111-1111-1111-1111-111111111111", "qwen3.6-27b", "v2"),
    )
    assert got is not None
    assert got.status == "empty"
    assert got.extracted_at is not None


def test_chunk_extraction_status_check(session: Session):
    """status is Literal-typed only; the SQL CHECK is what enforces at write
    time. Insert a bogus value via raw SQL (SQLModel table models skip
    validation, and raw SQL guarantees we reach the DB CHECK regardless),
    same as test_knowledge_grant_rejects_bad_grant_scope above. extracted_at
    is supplied so the only constraint that can fire is the status CHECK."""
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO chunk_extraction "
                "(chunk_id, model, prompt_version, status, extracted_at) "
                "VALUES (:cid, :m, :h, :s, :t)"
            ),
            {
                "cid": "22222222-2222-2222-2222-222222222222",
                "m": "m",
                "h": "h",
                "s": "bogus",
                "t": "2026-07-03T00:00:00+00:00",
            },
        )
        session.flush()
    session.rollback()


def test_book_and_chunk_seq_roundtrip(session: Session):
    from grimoire.models import Book, KnowledgeChunk

    session.add(Book(id="mm", display_name="Monster Manual"))
    session.add(
        KnowledgeChunk(
            book_id="mm",
            chunk_ref="r0",
            content="Aboleths are aberrations.",
            section_path="Monsters/Aboleth",
            seq=0,
        )
    )
    session.commit()

    book = session.get(Book, "mm")
    assert book is not None
    assert book.display_name == "Monster Manual"
    assert book.created_at is not None

    chunk = session.exec(
        select(KnowledgeChunk).where(KnowledgeChunk.book_id == "mm")
    ).one()
    assert chunk.seq == 0


def test_knowledge_grant_unique_entity_player_character(session: Session):
    entity = Entity(entity_type="npc", name="Nott")
    campaign, pc = _make_campaign_and_pc(session)
    session.add(entity)
    session.commit()
    session.refresh(entity)

    session.add(
        KnowledgeGrant(
            campaign_id=campaign.id,
            entity_id=entity.id,
            player_character_id=pc.id,
            grant_scope="full",
        )
    )
    session.commit()

    session.add(
        KnowledgeGrant(
            campaign_id=campaign.id,
            entity_id=entity.id,
            player_character_id=pc.id,
            grant_scope="name_only",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_adventure_roundtrip(session: Session):
    from grimoire.models import Adventure, Book, KnowledgeChunk

    session.add(Book(id="cm", display_name="Candlekeep Mysteries"))
    session.add(
        KnowledgeChunk(
            book_id="cm",
            chunk_ref="r0",
            content="The Joy of Extradimensional Spaces begins here.",
            section_path="The Joy of Extradimensional Spaces",
            seq=71,
        )
    )
    session.commit()

    adventure = Adventure(
        book_id="cm",
        name="The Joy of Extradimensional Spaces",
        seq=1,
        summary="A wizard's tower folds in on itself.",
        level_range="1",
        start_seq=71,
        end_seq=157,
    )
    session.add(adventure)
    session.commit()
    session.refresh(adventure)

    stored = session.exec(select(Adventure).where(Adventure.book_id == "cm")).one()
    assert stored.name == "The Joy of Extradimensional Spaces"
    assert stored.start_seq == 71
    assert stored.end_seq == 157
    assert stored.created_at is not None


def test_adventure_unique_book_id_name(session: Session):
    from grimoire.models import Adventure, Book

    session.add(Book(id="cm", display_name="Candlekeep Mysteries"))
    session.commit()

    session.add(
        Adventure(
            book_id="cm",
            name="The Joy of Extradimensional Spaces",
            seq=1,
            start_seq=71,
            end_seq=157,
        )
    )
    session.commit()

    session.add(
        Adventure(
            book_id="cm",
            name="The Joy of Extradimensional Spaces",
            seq=2,
            start_seq=158,
            end_seq=200,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
