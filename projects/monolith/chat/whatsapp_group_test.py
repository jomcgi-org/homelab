"""Tests for the chat.whatsapp_group registry model (ADR 039, spec section 6).

DB-backed tests run against in-memory SQLite with the chat schema stripped,
mirroring chat.directives_test. The digest_config JSONB column falls back to
JSON on SQLite (models._JSONB), so a dict round-trips there too.
"""

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat.models import WhatsappGroup


def _engine():
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
    SQLModel.metadata.create_all(engine)
    return engine, original_schemas


def _restore(original_schemas):
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


def test_defaults_and_roundtrip():
    engine, restore = _engine()
    try:
        with Session(engine) as session:
            session.add(WhatsappGroup(group_jid="123-456@g.us"))
            session.commit()
        with Session(engine) as session:
            row = session.get(WhatsappGroup, "123-456@g.us")
        assert row is not None
        # Registry defaults per spec section 6.
        assert row.tier == "household"
        assert row.ambient is True
        assert row.enabled is True
        assert row.display_name is None
        assert row.directive_seed is None
        assert row.digest_config is None
    finally:
        _restore(restore)


def test_digest_config_dict_roundtrips():
    engine, restore = _engine()
    try:
        cfg = {"digest_hour": 8, "quiet_hours": [22, 7]}
        with Session(engine) as session:
            session.add(
                WhatsappGroup(
                    group_jid="g1@g.us",
                    display_name="Household",
                    directive_seed="log what we did",
                    digest_config=cfg,
                    ambient=False,
                    enabled=False,
                )
            )
            session.commit()
        with Session(engine) as session:
            row = session.get(WhatsappGroup, "g1@g.us")
        assert row.display_name == "Household"
        assert row.directive_seed == "log what we did"
        assert row.digest_config == cfg
        assert row.ambient is False
        assert row.enabled is False
    finally:
        _restore(restore)


def test_query_by_enabled():
    engine, restore = _engine()
    try:
        with Session(engine) as session:
            session.add(WhatsappGroup(group_jid="on@g.us", enabled=True))
            session.add(WhatsappGroup(group_jid="off@g.us", enabled=False))
            session.commit()
        with Session(engine) as session:
            enabled = session.exec(
                select(WhatsappGroup).where(WhatsappGroup.enabled == True)  # noqa: E712
            ).all()
        assert {r.group_jid for r in enabled} == {"on@g.us"}
    finally:
        _restore(restore)
