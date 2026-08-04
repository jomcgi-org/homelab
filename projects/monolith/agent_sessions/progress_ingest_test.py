from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from agent_sessions import progress_ingest, store
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
    monkeypatch.setattr(store, "get_engine", lambda: engine)
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


def test_progress_ingest_multibyte_cap(monkeypatch):
    """UTF-8 multibyte characters count toward 262144 byte cap."""
    client, _, schemas = _client(monkeypatch)
    try:
        oversized = "é" * 200000
        response = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": oversized},
        )
        assert response.status_code == 413
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_boundary_case(monkeypatch):
    """String content near 262144 bytes (accounting for JSON overhead) is accepted."""
    client, _, schemas = _client(monkeypatch)
    try:
        # Account for JSON overhead: {"partial_text": "..."} adds ~20 bytes
        content = "x" * 262120
        response = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": content},
        )
        assert response.status_code == 204
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_middleware_rejects_oversized_content_length(monkeypatch):
    """Middleware rejects Content-Length > 262144 before handler runs."""
    client, _, schemas = _client(monkeypatch)
    try:
        response = client.post(
            "/ingest/progress",
            headers={
                "Authorization": "Bearer token",
                "Content-Length": str(262145),
            },
            content=b"",
        )
        assert response.status_code == 413
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_rate_limit_drops_second_push(monkeypatch):
    """Second push within 0.3s for same token returns 204 (dropped)."""
    client, _, schemas = _client(monkeypatch)
    try:
        response1 = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "first"},
        )
        assert response1.status_code == 204

        response2 = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "second"},
        )
        assert response2.status_code == 204
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_rate_limit_allows_delayed_push(monkeypatch):
    """Push after 0.3s delay is accepted."""
    import time

    client, _, schemas = _client(monkeypatch)
    try:
        client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "first"},
        )
        time.sleep(0.31)
        response2 = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "second"},
        )
        assert response2.status_code == 204
    finally:
        _restore_schemas(schemas)


def test_healthz_passes_middleware_without_content_length():
    # kubelet probes send no Content-Length; a 411 here would fail liveness.
    from fastapi.testclient import TestClient

    from agent_sessions.progress_ingest import app

    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
