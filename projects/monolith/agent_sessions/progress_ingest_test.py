from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from agent_sessions import progress_ingest
from agent_sessions.models import AgentSession


def _client(monkeypatch, token="token"):
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
    with Session(engine) as session:
        session.add(
            AgentSession(
                local_session_id="local",
                workspace="workspace",
                branch="main",
                progress_token=token,
            )
        )
        session.commit()
    monkeypatch.setattr(progress_ingest, "get_engine", lambda: engine)
    monkeypatch.setattr(progress_ingest.store, "get_engine", lambda: engine)
    return TestClient(progress_ingest.app), engine, schemas


def _restore_schemas(schemas):
    for table in SQLModel.metadata.tables.values():
        if table.name in schemas:
            table.schema = schemas[table.name]


def test_progress_ingest_valid_request_returns_204(monkeypatch):
    client, engine, schemas = _client(monkeypatch)
    try:
        response = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "working"},
        )
        assert response.status_code == 204
        with Session(engine) as session:
            assert session.exec(select(AgentSession)).one().progress_token == "token"
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_missing_or_invalid_auth_returns_401(monkeypatch):
    client, _, schemas = _client(monkeypatch)
    try:
        assert (
            client.post("/ingest/progress", json={"partial_text": "x"}).status_code
            == 401
        )
        assert (
            client.post(
                "/ingest/progress",
                headers={"Authorization": "Bearer invalid"},
                json={"partial_text": "x"},
            ).status_code
            == 401
        )
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_rejects_oversized_text(monkeypatch):
    client, _, schemas = _client(monkeypatch)
    try:
        response = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "x" * 262145},
        )
        assert response.status_code == 413
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_malformed_json_returns_422(monkeypatch):
    client, _, schemas = _client(monkeypatch)
    try:
        response = client.post(
            "/ingest/progress",
            headers={
                "Authorization": "Bearer token",
                "Content-Type": "application/json",
            },
            content=b"not-json",
        )
        assert response.status_code == 422
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_valid_token_without_pending_row_is_noop(monkeypatch):
    client, _, schemas = _client(monkeypatch)
    try:
        assert (
            client.post(
                "/ingest/progress",
                headers={"Authorization": "Bearer token"},
                json={"partial_text": "x"},
            ).status_code
            == 204
        )
    finally:
        _restore_schemas(schemas)
