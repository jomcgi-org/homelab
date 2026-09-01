import json

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions import store
from agent_sessions.constants import (
    LEGACY_QWEN_SYNTHETIC_PROMPT,
    SYNTHETIC_SESSION_PREFIX,
)
from agent_sessions.models import AgentSession, PendingMessage


def test_lexical_search_excludes_synthetic_sessions():
    captured = {}

    class FakeSession:
        def exec(self, statement, params):
            captured["sql"] = str(statement)
            captured["params"] = params
            return []

    assert store.lexical_search(FakeSession(), "qwen") == []
    assert "s.local_session_id NOT LIKE :synthetic_prefix" in captured["sql"]
    assert "first_turn.session_id = s.id" in captured["sql"]
    assert captured["params"]["synthetic_prefix"] == (f"{SYNTHETIC_SESSION_PREFIX}%")
    assert captured["params"]["qwen_synthetic_prompt"] == LEGACY_QWEN_SYNTHETIC_PROMPT


def _database(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'store_test.db'}",
        connect_args={"check_same_thread": False},
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


def test_write_progress_sync_updates_claimed_row(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
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


def test_create_session_persists_optional_system_prompt(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            prompted = store.create_session(
                session,
                "prompted",
                "<guest>",
                "main",
                system_prompt="X",
            )
            unprompted = store.create_session(session, "unprompted", "<guest>", "main")

            assert prompted.system_prompt == "X"
            assert unprompted.system_prompt is None
    finally:
        _restore_schemas(schemas)


def test_create_session_persists_normalized_triggered_by(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            triggered = store.create_session(
                session,
                "triggered",
                "<guest>",
                "main",
                triggered_by="  EXAMPLE@EXAMPLE.COM  ",
            )
            untriggered = store.create_session(
                session, "untriggered", "<guest>", "main"
            )
            blank = store.create_session(
                session, "blank", "<guest>", "main", triggered_by="   "
            )

            assert triggered.triggered_by == "example@example.com"
            assert untriggered.triggered_by is None
            # Whitespace-only must land as NULL, not "". An empty string passes a
            # NULL check but matches no caller, so it would own rows nobody reads.
            assert blank.triggered_by is None
    finally:
        _restore_schemas(schemas)


def test_create_session_persists_and_queries_workflow_id(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            linked = store.create_session(
                session,
                "linked",
                "<guest>",
                "main",
                workflow_id="wf-123",
            )
            unlinked = store.create_session(session, "unlinked", "<guest>", "main")

            assert linked.workflow_id == "wf-123"
            assert unlinked.workflow_id is None
            queried = session.exec(
                select(AgentSession).where(AgentSession.workflow_id == "wf-123")
            ).one()
            assert queried.id == linked.id
            indexes = inspect(engine).get_indexes("agent_sessions")
            assert any("workflow_id" in index["column_names"] for index in indexes)
    finally:
        _restore_schemas(schemas)


def test_sessions_for_workflow_returns_only_matching_rows(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            matching = store.create_session(
                session, "matching", "<guest>", "main", workflow_id="wf-1"
            )
            store.create_session(
                session, "other", "<guest>", "main", workflow_id="wf-2"
            )
            assert store.sessions_for_workflow(session, "wf-1") == [matching]
    finally:
        _restore_schemas(schemas)


def test_write_progress_sync_stores_activities(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    activities = [{"type": "tool", "name": "shell"}]
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
            session.add(
                PendingMessage(
                    session_id=agent.id,
                    seq=1,
                    message_text="first",
                    claimed_by_replica="replica-a",
                )
            )
            session.commit()

        assert store.write_progress_sync("token", "working", activities) == "ok"
        with Session(engine) as session:
            row = session.exec(select(PendingMessage)).one()
            assert json.loads(row.partial_activities) == activities
    finally:
        _restore_schemas(schemas)


def test_write_progress_sync_unknown_token_returns_unknown_token(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        assert store.write_progress_sync("missing", "working") == "unknown_token"
    finally:
        _restore_schemas(schemas)


def test_write_progress_sync_without_pending_row_returns_no_row(monkeypatch, tmp_path):
    engine, schemas = _database(monkeypatch, tmp_path)
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


def test_write_progress_sync_falls_back_to_unclaimed_row(monkeypatch, tmp_path):
    """Fallback updates lowest seq unclaimed row when no claimed row exists."""
    engine, schemas = _database(monkeypatch, tmp_path)
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


def test_discord_thread_binds_at_most_one_session(monkeypatch, tmp_path):
    """A thread can never fan out to two sessions.

    The unique constraint is what makes session_id_for_thread a lookup rather
    than a choice: without it a second /agent in the same thread would create a
    rival session and turns would land in whichever one the query happened to
    return first.
    """
    engine, schemas = _database(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            store.create_session(
                session, "local-1", "<guest>", "main", "luna", discord_thread="t-1"
            )
            with pytest.raises(IntegrityError):
                store.create_session(
                    session, "local-2", "<guest>", "main", "luna", discord_thread="t-1"
                )
            session.rollback()

        # Unbound sessions are unaffected: many NULLs are allowed under the
        # constraint, which is what keeps the UI and MCP lanes working.
        with Session(engine) as session:
            store.create_session(session, "local-3", "<guest>", "main", "luna")
            store.create_session(session, "local-4", "<guest>", "main", "luna")
            rows = session.exec(
                select(AgentSession).where(AgentSession.discord_thread.is_(None))
            ).all()
            assert len(rows) == 2
    finally:
        _restore_schemas(schemas)
