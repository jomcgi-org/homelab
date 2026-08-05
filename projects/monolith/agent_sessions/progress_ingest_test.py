from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions import progress_ingest, store
from agent_sessions.models import AgentSession, PendingMessage


def _client(monkeypatch, tmp_path, token="token"):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'progress_ingest_test.db'}",
        connect_args={"check_same_thread": False},
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
    monkeypatch.setattr(progress_ingest, "_token_last_write", {})
    return TestClient(progress_ingest.app), engine, schemas


def _restore_schemas(schemas):
    for table in SQLModel.metadata.tables.values():
        if table.name in schemas:
            table.schema = schemas[table.name]


def test_progress_ingest_valid_request_returns_204(monkeypatch, tmp_path):
    client, engine, schemas = _client(monkeypatch, tmp_path)
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


def test_progress_ingest_accepts_valid_activities(monkeypatch, tmp_path):
    client, _, schemas = _client(monkeypatch, tmp_path)
    try:
        response = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "working", "activities": [{"type": "tool"}]},
        )
        assert response.status_code == 204
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_rejects_non_list_activities(monkeypatch, tmp_path):
    client, _, schemas = _client(monkeypatch, tmp_path)
    try:
        response = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "working", "activities": "not a list"},
        )
        assert response.status_code == 422
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_rejects_oversized_activities_list(monkeypatch, tmp_path):
    client, _, schemas = _client(monkeypatch, tmp_path)
    try:
        response = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "working", "activities": [{}] * 301},
        )
        assert response.status_code == 422
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_rejects_non_dict_activity_items(monkeypatch, tmp_path):
    client, _, schemas = _client(monkeypatch, tmp_path)
    try:
        response = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "working", "activities": ["tool"]},
        )
        assert response.status_code == 422
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_missing_or_invalid_auth_returns_401(monkeypatch, tmp_path):
    client, _, schemas = _client(monkeypatch, tmp_path)
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


def test_progress_ingest_rejects_oversized_text(monkeypatch, tmp_path):
    client, _, schemas = _client(monkeypatch, tmp_path)
    try:
        response = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "x" * 262145},
        )
        assert response.status_code == 413
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_malformed_json_returns_422(monkeypatch, tmp_path):
    client, _, schemas = _client(monkeypatch, tmp_path)
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


def test_progress_ingest_valid_token_without_pending_row_is_noop(monkeypatch, tmp_path):
    client, _, schemas = _client(monkeypatch, tmp_path)
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


def test_progress_ingest_multibyte_cap(monkeypatch, tmp_path):
    """UTF-8 multibyte characters count toward 262144 byte cap."""
    client, _, schemas = _client(monkeypatch, tmp_path)
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


def test_progress_ingest_boundary_case(monkeypatch, tmp_path):
    """String content near 262144 bytes (accounting for JSON overhead) is accepted."""
    client, _, schemas = _client(monkeypatch, tmp_path)
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


def test_progress_ingest_middleware_rejects_oversized_content_length(
    monkeypatch, tmp_path
):
    """Middleware rejects Content-Length > 262144 before handler runs."""
    client, _, schemas = _client(monkeypatch, tmp_path)
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


def test_progress_ingest_rate_limit_drops_second_push(monkeypatch, tmp_path):
    """Second push within 0.15s for same token returns 204 (dropped)."""
    client, engine, schemas = _client(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            agent_session = session.exec(select(AgentSession)).one()
            session.add(
                PendingMessage(
                    session_id=agent_session.id, seq=1, message_text="prompt"
                )
            )
            session.commit()
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
        with Session(engine) as session:
            pending = session.exec(select(PendingMessage)).one()
            assert pending.partial_text == "first"
    finally:
        _restore_schemas(schemas)


def test_progress_ingest_rate_limit_window_150ms(monkeypatch, tmp_path):
    """Pushes before 150ms are dropped, and pushes after it are accepted."""
    import time

    client, engine, schemas = _client(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            agent_session = session.exec(select(AgentSession)).one()
            session.add(
                PendingMessage(
                    session_id=agent_session.id, seq=1, message_text="prompt"
                )
            )
            session.commit()
        client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "first"},
        )
        time.sleep(0.1)
        response2 = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "second"},
        )
        assert response2.status_code == 204
        time.sleep(0.1)
        response3 = client.post(
            "/ingest/progress",
            headers={"Authorization": "Bearer token"},
            json={"partial_text": "third"},
        )
        assert response3.status_code == 204
        with Session(engine) as session:
            pending = session.exec(select(PendingMessage)).one()
            assert pending.partial_text == "third"
    finally:
        _restore_schemas(schemas)


def test_healthz_passes_middleware_without_content_length():
    # kubelet probes send no Content-Length; a 411 here would fail liveness.
    from fastapi.testclient import TestClient

    from agent_sessions.progress_ingest import app

    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
