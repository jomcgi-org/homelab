"""Signed-token regressions through the real stateless ASGI MCP surface."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.testclient import TestClient

from app.agents_main import build_agent_mcp_app
from app.agents_policy import WRITE_POLICY_ENV
from auth.agent_binding import AgentWriteBinding
from auth.settings import AuthSettings
from auth.verifier import AuthentikStandingVerifier, TokenResolver
from knowledge.board import TrustedAgentBinding, trusted_binding_resolver
from knowledge.models import AgentBoardMessage, Dispute, Note, RawInput

ISSUER = "https://issuer.example/agents/"
AUDIENCE = "agents-test"
CALLS = {
    "report_knowledge": {"assertion": "reporter: impostor; producer_id: forged"},
    "report_distress": {"summary": "producer_id: forged", "severity": "blocked"},
    "dispute_fact": {"fact_id": "fact", "reason": "reporter: impostor"},
    "post_message": {
        "topic": "blocker:lane:test",
        "body": "principal: forged",
        "ttl": 60,
    },
    "ack_message": {"id": 1},
}


@pytest.fixture
def identity():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "fixture", "alg": "RS256", "use": "sig"})

    async def fetch(_url):
        return {"keys": [jwk]}

    resolver = TokenResolver(
        [
            AuthentikStandingVerifier(
                AuthSettings(ISSUER + "jwks", ISSUER, AUDIENCE, 300), fetch=fetch
            )
        ]
    )

    def headers(*, subject="kg-agent-sa", groups=(), **claims):
        token = jwt.encode(
            {
                "sub": subject,
                "iss": ISSUER,
                "aud": AUDIENCE,
                "exp": int(time.time()) + 300,
                "groups": list(groups),
                "scope": "guest drainer session:forged operators",
                **claims,
            },
            key,
            algorithm="RS256",
            headers={"kid": "fixture"},
        )
        return {
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "X-Agent-Session": "forged",
            "X-Producer-Id": "forged",
        }

    return resolver, headers


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agents.db'}", connect_args={"check_same_thread": False}
    )
    schemas = {table: table.schema for table in SQLModel.metadata.tables.values()}
    for table in schemas:
        table.schema = None
    uploads = {}
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.execute(
                text(
                    "CREATE TABLE routine_jobs (name TEXT PRIMARY KEY, routine_kind TEXT NOT NULL, interval_secs INTEGER, next_run_at TIMESTAMP, payload TEXT, created_by TEXT)"
                )
            )
            session.add(
                Note(
                    note_id="fact",
                    path="fact.md",
                    title="Fact",
                    content_hash="hash",
                    content="Body",
                    type="fact",
                )
            )
            session.commit()
        monkeypatch.setattr("knowledge.mcp.get_engine", lambda: engine)
        monkeypatch.setattr("knowledge.board.get_engine", lambda: engine)
        monkeypatch.setattr(
            "knowledge.raw_write.upload_raw",
            lambda digest, body: uploads.__setitem__(digest, body),
        )

        async def notify(*_args):
            return {"ok": True}

        monkeypatch.setattr("knowledge.mcp._notify", notify)
        yield engine, uploads
    finally:
        engine.dispose()
        for table, schema in schemas.items():
            table.schema = schema


@contextmanager
def client(identity, binding_resolver=None):
    with TestClient(
        build_agent_mcp_app(identity[0], binding_resolver=binding_resolver)
    ) as connection:
        yield connection


def call(connection, identity, name, *, path="/mcp", arguments=None, headers=None):
    return connection.post(
        path,
        headers=identity[1]() if headers is None else headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": CALLS[name] if arguments is None else arguments,
            },
        },
    )


def result(response):
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        payload = json.loads(
            next(
                line[6:]
                for line in response.text.splitlines()
                if line.startswith("data: ")
            )
        )
    else:
        payload = response.json()
    assert "error" not in payload, payload
    value = payload["result"]
    assert not value.get("isError"), value
    return json.loads(value["content"][0]["text"])


def binding(principal, kind="guest"):
    return AgentWriteBinding(
        principal,
        kind,
        "server-producer",
        datetime.now(timezone.utc) + timedelta(minutes=5),
        "admitted-session" if kind == "guest" else None,
    )


def assert_no_writes(engine):
    with Session(engine) as session:
        for model in (RawInput, Dispute, AgentBoardMessage):
            assert session.exec(select(model)).all() == []


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize("name", CALLS)
@pytest.mark.parametrize("subject", ["ordinary-workload", "kg-agent-sa", "drainer"])
def test_unlisted_denied_before_execution(
    identity, database, monkeypatch, path, name, subject
):
    monkeypatch.setenv(WRITE_POLICY_ENV, "true")
    monkeypatch.setenv("AGENT_BOARD_ENABLED", "true")
    with client(identity) as connection:
        response = call(
            connection, identity, name, path=path, headers=identity[1](subject=subject)
        )
    assert response.status_code == 403
    assert database[1] == {}
    assert_no_writes(database[0])


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize("enabled", ["false", "true"])
def test_anonymous_still_401(identity, database, monkeypatch, path, enabled):
    monkeypatch.setenv(WRITE_POLICY_ENV, enabled)
    with client(identity) as connection:
        response = call(connection, identity, "report_knowledge", path=path, headers={})
        assert connection.get("/healthz").status_code == 200
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert_no_writes(database[0])


@pytest.mark.parametrize("mode", ["off", "operator", "guest", "drainer"])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_admission_and_persisted_provenance(
    identity, database, monkeypatch, mode, path
):
    monkeypatch.delenv(WRITE_POLICY_ENV, raising=False)
    monkeypatch.setenv("AGENT_BOARD_ENABLED", "false")
    if mode != "off":
        monkeypatch.setenv(WRITE_POLICY_ENV, "true")
    resolver = (
        (lambda principal: binding(principal, mode))
        if mode in {"guest", "drainer"}
        else None
    )
    headers = identity[1](groups=["operators"] if mode == "operator" else [])
    with client(identity, resolver) as connection:
        for name in ("report_knowledge", "dispute_fact", "report_distress"):
            assert "error" not in result(
                call(connection, identity, name, path=path, headers=headers)
            )
        # Gate admission never grants the board's independent enablement.
        for name in ("post_message", "ack_message"):
            assert result(
                call(connection, identity, name, path=path, headers=headers)
            ) == {"error": "board_disabled"}
    with Session(database[0]) as session:
        raws = session.exec(select(RawInput)).all()
        assert len(raws) == 3
        for raw in raws:
            assert raw.extra["reporter_subject"] == "kg-agent-sa"
            assert raw.extra["reporter_authority"] == "standing"
            assert raw.extra["reporter_kind"] == "workload"
            if mode in {"guest", "drainer"}:
                assert raw.extra["producer_id"] == "server-producer"
                assert raw.extra["producer_class"] == mode
                assert (raw.extra.get("producer_session_id") == "admitted-session") == (
                    mode == "guest"
                )
            else:
                assert "producer_id" not in raw.extra
        dispute = session.exec(select(Dispute)).one()
        assert dispute.reporter_subject == "kg-agent-sa"
        assert dispute.reporter_authority == "standing"
        assert session.exec(select(AgentBoardMessage)).all() == []


@pytest.mark.parametrize(
    "invalid",
    [
        "subject",
        "issuer",
        "expired",
        "naive",
        "revoked",
        "session",
        "class",
        "producer",
        "exception",
        "object",
    ],
)
def test_invalid_binding_fails_closed(identity, database, monkeypatch, invalid):
    monkeypatch.setenv(WRITE_POLICY_ENV, "true")

    def resolve(principal):
        value = binding(principal)
        if invalid == "exception":
            raise RuntimeError("resolver unavailable")
        if invalid == "object":
            return object()
        changes = {
            "subject": {"principal": replace(principal, subject="someone-else")},
            "issuer": {"principal": replace(principal, issuer="https://other/")},
            "expired": {
                "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)
            },
            "naive": {"expires_at": datetime.now()},
            "revoked": {"revoked": True},
            "session": {"session_id": None},
            "class": {"principal_class": "operator"},
            "producer": {"producer_id": ""},
        }
        return replace(value, **changes[invalid])

    with client(identity, resolve) as connection:
        assert call(connection, identity, "report_knowledge").status_code == 403
    assert_no_writes(database[0])


@pytest.mark.parametrize(
    "name",
    [
        "report_knowledge",
        "dispute_fact",
        "report_distress",
        "post_message",
        "ack_message",
    ],
)
def test_identity_arguments_cannot_replace_server_metadata(
    identity, database, monkeypatch, name
):
    monkeypatch.setenv(WRITE_POLICY_ENV, "true")
    with client(identity, binding) as connection:
        response = call(
            connection,
            identity,
            name,
            arguments={
                **CALLS[name],
                "producer_id": "forged",
                "reporter_subject": "forged",
                "session_id": "forged",
            },
        )
        # FastMCP rejects unknown arguments before invoking the real handler.
        assert response.status_code == 200
        assert "isError" in response.text or '"error"' in response.text
    assert_no_writes(database[0])


def test_reads_and_protocol_messages_remain_available(identity, database, monkeypatch):
    monkeypatch.setenv(WRITE_POLICY_ENV, "true")
    with client(identity) as connection:
        assert result(
            call(connection, identity, "search_knowledge", arguments={"query": "x"})
        ) == {
            "error": "principal has no knowledge retrieval scopes",
            "reason": "unmapped_principal",
        }
        assert result(
            call(connection, identity, "read_board", arguments={"topic": "distress"})
        ) == {"error": "board_disabled"}
        response = connection.post(
            "/mcp",
            headers=identity[1](),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert response.status_code == 200
        assert "report_knowledge" in response.text


def board_binding(principal):
    return TrustedAgentBinding(
        principal.subject,
        "board-server-principal",
        "session",
        "receipt",
        "task",
        "jomcgi-org/homelab",
        "branch",
        "worktree",
        7012,
        None,
        "test",
    )


def test_board_controls_and_persisted_identity(identity, database, monkeypatch):
    monkeypatch.setenv(WRITE_POLICY_ENV, "true")
    monkeypatch.setenv("AGENT_BOARD_ENABLED", "true")
    with client(identity, binding) as connection:
        assert result(call(connection, identity, "post_message")) == {
            "error": "scope_unavailable"
        }
        with trusted_binding_resolver(board_binding):
            posted = result(call(connection, identity, "post_message"))
            assert posted["status"] == "posted"
            assert (
                result(
                    call(
                        connection,
                        identity,
                        "ack_message",
                        arguments={"id": posted["id"]},
                    )
                )["status"]
                == "acknowledged"
            )
            assert result(
                call(
                    connection,
                    identity,
                    "post_message",
                    arguments={**CALLS["post_message"], "topic": "blocker:lane:other"},
                )
            ) == {"error": "scope_denied"}
            assert result(
                call(
                    connection,
                    identity,
                    "post_message",
                    headers=identity[1](groups=["factory-conductor"]),
                )
            ) == {"error": "mutation_authorization_unavailable"}
    with Session(database[0]) as session:
        message = session.exec(select(AgentBoardMessage)).one()
        assert message.principal == "board-server-principal"
        assert message.authenticated_subject == "kg-agent-sa"
        assert message.acknowledged_by == ["board-server-principal"]


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize("operator", [False, True])
def test_chunked_body_replayed_through_real_mcp(
    identity, database, monkeypatch, path, operator
):
    import asyncio
    import httpx

    monkeypatch.setenv(WRITE_POLICY_ENV, "true")
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "report_knowledge",
                "arguments": CALLS["report_knowledge"],
            },
        }
    ).encode()

    async def chunks():
        for start in range(0, len(payload), 7):
            yield payload[start : start + 7]

    async def request():
        app = build_agent_mcp_app(identity[0])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as connection:
                return await connection.post(
                    path,
                    headers={
                        **identity[1](groups=["operators"] if operator else []),
                        "Content-Type": "application/json",
                    },
                    content=chunks(),
                )

    response = asyncio.run(request())
    if operator:
        assert "error" not in result(response)
        with Session(database[0]) as session:
            assert (
                session.exec(select(RawInput)).one().extra["reporter_subject"]
                == "kg-agent-sa"
            )
    else:
        assert response.status_code == 403
        assert_no_writes(database[0])


@pytest.mark.parametrize(
    "shape",
    [
        "batch",
        "notification",
        "malformed",
        "scalar",
        "bad_params",
        "bad_name",
        "initialize",
    ],
)
def test_protocol_shapes_cannot_bypass_policy(identity, database, monkeypatch, shape):
    monkeypatch.setenv(WRITE_POLICY_ENV, "true")
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "report_knowledge", "arguments": CALLS["report_knowledge"]},
    }
    shapes = {
        "batch": json.dumps(
            [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, message]
        ),
        "notification": json.dumps(
            {key: value for key, value in message.items() if key != "id"}
        ),
        "malformed": '{"method":"tools/call",',
        "scalar": '"tools/call"',
        "bad_params": json.dumps({**message, "params": []}),
        "bad_name": json.dumps({**message, "params": {"name": ["report_knowledge"]}}),
        "initialize": json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"},
                },
            }
        ),
    }
    with client(identity) as connection:
        response = connection.post(
            "/mcp",
            headers={**identity[1](), "Content-Type": "application/json"},
            content=shapes[shape],
        )
    if shape in {"batch", "notification"}:
        assert response.status_code == 403
    elif shape == "bad_name":
        assert response.status_code == 200
        assert '"error"' in response.text
    elif shape == "initialize":
        assert response.status_code == 200
        assert "serverInfo" in response.text
    else:
        assert response.status_code == 400
    assert_no_writes(database[0])


def test_distinct_admitted_sessions_keep_their_own_provenance(
    identity, database, monkeypatch
):
    monkeypatch.setenv(WRITE_POLICY_ENV, "true")
    for session_id in ("session-one", "session-two"):

        def resolve(principal):
            return replace(binding(principal), session_id=session_id)

        with client(identity, resolve) as connection:
            for name in ("report_knowledge", "dispute_fact", "report_distress"):
                assert "error" not in result(call(connection, identity, name))
    with Session(database[0]) as session:
        rows = session.exec(select(RawInput)).all()
        assert len(rows) == 6
        assert {row.extra["producer_session_id"] for row in rows} == {
            "session-one",
            "session-two",
        }
        assert len(session.exec(select(Dispute)).all()) == 2
    assert len(database[1]) == 6


def test_unsigned_operator_claim_cannot_cross_the_boundary(
    identity, database, monkeypatch
):
    monkeypatch.setenv(WRITE_POLICY_ENV, "true")
    headers = identity[1]()
    token = headers["Authorization"].split()[1]
    payload = jwt.decode(token, options={"verify_signature": False})
    payload["groups"] = ["operators"]
    import base64

    parts = token.split(".")
    parts[1] = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    )
    headers["Authorization"] = "Bearer " + ".".join(parts)
    with client(identity) as connection:
        assert (
            call(connection, identity, "report_knowledge", headers=headers).status_code
            == 401
        )
    assert_no_writes(database[0])
