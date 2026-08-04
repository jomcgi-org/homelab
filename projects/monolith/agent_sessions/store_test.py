from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from agent_sessions import store
from agent_sessions.models import AgentSession, PendingMessage


def _database(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(store, "get_engine", lambda: engine)
    return engine, schemas


def _restore_schemas(schemas):
    for table in SQLModel.metadata.tables.values():
        if table.name in schemas:
            table.schema = schemas[table.name]


def test_write_progress_sync_updates_claimed_row(monkeypatch):
    engine, schemas = _database(monkeypatch)
    try:
        with Session(engine) as session:
            agent = AgentSession(
                local_session_id="local",
                workspace="workspace",
                branch="main",
                progress_token="token",
            )
            session.add(agent)
            session.commit()
            session.refresh(agent)
            session.add_all(
                [
                    PendingMessage(
                        session_id=agent.id,
                        seq=1,
                        message_text="first",
                        claimed_by_replica=None,
                    ),
                    PendingMessage(
                        session_id=agent.id,
                        seq=2,
                        message_text="second",
                        claimed_by_replica="replica-a",
                    ),
                ]
            )
            session.commit()

        assert store.write_progress_sync("token", "working") == "ok"
        with Session(engine) as session:
            rows = session.exec(
                select(PendingMessage).order_by(PendingMessage.seq)
            ).all()
            assert rows[0].partial_text is None
            assert rows[1].partial_text == "working"
    finally:
        _restore_schemas(schemas)


def test_write_progress_sync_unknown_token_returns_unknown_token(monkeypatch):
    engine, schemas = _database(monkeypatch)
    try:
        assert store.write_progress_sync("missing", "working") == "unknown_token"
    finally:
        _restore_schemas(schemas)


def test_write_progress_sync_without_pending_row_returns_no_row(monkeypatch):
    engine, schemas = _database(monkeypatch)
    try:
        with Session(engine) as session:
            session.add(
                AgentSession(
                    local_session_id="local",
                    workspace="workspace",
                    branch="main",
                    progress_token="token",
                )
            )
            session.commit()
        assert store.write_progress_sync("token", "working") == "no_row"
    finally:
        _restore_schemas(schemas)


def test_write_progress_sync_falls_back_to_unclaimed_row(monkeypatch):
    """Fallback updates lowest seq unclaimed row when no claimed row exists."""
    engine, schemas = _database(monkeypatch)
    try:
        with Session(engine) as session:
            agent = AgentSession(
                local_session_id="local",
                workspace="workspace",
                branch="main",
                progress_token="token",
            )
            session.add(agent)
            session.commit()
            session.refresh(agent)
            session.add_all(
                [
                    PendingMessage(
                        session_id=agent.id,
                        seq=1,
                        message_text="first",
                        claimed_by_replica=None,
                    ),
                    PendingMessage(
                        session_id=agent.id,
                        seq=2,
                        message_text="second",
                        claimed_by_replica=None,
                    ),
                ]
            )
            session.commit()

        result = store.write_progress_sync("token", "working")
        assert result == "ok"
        with Session(engine) as session:
            rows = session.exec(
                select(PendingMessage).order_by(PendingMessage.seq)
            ).all()
            assert rows[0].partial_text == "working"
            assert rows[1].partial_text is None
    finally:
        _restore_schemas(schemas)
