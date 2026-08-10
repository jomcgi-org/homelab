from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from agent_sessions import api, store
from agent_sessions import mcp
import agent_sessions.router as agent_router
from agent_sessions.codex_login import codex_login_gate
from agent_sessions.models import AgentSession, AgentTurn, PendingMessage
from agent_sessions.router import router
from core.db import get_session
from faas.embervm_client import EmberVMTransportError


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'router_test.db'}",
        connect_args={"check_same_thread": False},
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


@pytest.fixture(autouse=True)
def reset_vm_state_cache():
    """Clear the shared VM cache around every test.

    The cache is deliberately module level, since it is shared by all SSE
    subscribers in a replica, which makes it leak between tests in the same
    process: a test that populated 500 sessions made a later "control plane
    is down" assertion fail against the earlier test's data, and the failure
    depended on test ORDER rather than on either test being wrong. Autouse so
    a future test touching VM state inherits the isolation without knowing to
    ask for it.
    """
    cache = agent_router._vm_state_cache
    # task is dropped rather than cancelled, which is safe only because every
    # test that starts a refresher does so inside asyncio.run, whose loop
    # teardown cancels outstanding tasks. A session-scoped loop, or driving
    # the stream through TestClient (its portal loop outlives the call),
    # would strand the task instead. Cancel here if either becomes true.
    cache.cache_map = {}
    cache.last_refreshed_at = 0.0
    cache.subscriber_count = 0
    cache.task = None
    cache.change_event = None
    cache.generation = 0
    cache.initialized = False
    cache.last_error = None
    yield
    cache.cache_map = {}
    cache.task = None
    cache.subscriber_count = 0


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _session(session: Session, name: str, status: str = "running", **kwargs):
    row = AgentSession(
        local_session_id=name,
        workspace=kwargs.pop("workspace", "<guest>"),
        branch=kwargs.pop("branch", "main"),
        status=status,
        **kwargs,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_list_sessions_empty(client):
    response = client.get("/api/agents/sessions")
    assert response.status_code == 200
    assert response.json() == []


def test_list_sessions_with_status_filter(client, session):
    _session(session, "running", "running")
    _session(session, "done", "completed")

    body = client.get("/api/agents/sessions?status=completed").json()
    assert [item["local_session_id"] for item in body] == ["done"]


def test_list_sessions_ordering(client, session):
    # A NULL last_turn_at is unrepresentable: the column is NOT NULL DEFAULT
    # NOW() in Postgres, and the model's default_factory backfills an explicit
    # None at INSERT time, so only the DESC ordering is observable.
    now = datetime.now(timezone.utc)
    _session(session, "oldest", last_turn_at=now - timedelta(minutes=5))
    _session(session, "old", last_turn_at=now - timedelta(minutes=1))
    _session(session, "new", last_turn_at=now)

    body = client.get("/api/agents/sessions").json()
    assert [item["local_session_id"] for item in body] == ["new", "old", "oldest"]


def test_list_sessions_aggregates(client, session):
    row = _session(session, "aggregate")
    session.add_all(
        [
            AgentTurn(
                session_id=row.id,
                seq=1,
                prompt="one",
                result_text="done",
                cost_usd=0.06,
            ),
            AgentTurn(
                session_id=row.id,
                seq=2,
                prompt="two",
                result_text="done",
                cost_usd=0.04,
            ),
            PendingMessage(session_id=row.id, seq=3, message_text="three"),
        ]
    )
    session.commit()

    item = client.get("/api/agents/sessions").json()[0]
    assert item["turn_count"] == 2
    assert item["pending_count"] == 1
    assert item["total_cost_usd"] == pytest.approx(0.1)
    assert item["title"] == "one"


def test_list_sessions_title_falls_back_to_pending_prompt(client, session):
    pending_only = _session(session, "pending-only")
    session.add(
        PendingMessage(
            session_id=pending_only.id,
            seq=1,
            message_text="First line\nsecond line",
        )
    )
    _session(session, "empty")
    session.commit()

    _session(session, "named", title="Qwen picked this name")
    body = client.get("/api/agents/sessions").json()
    titles = {item["local_session_id"]: item["title"] for item in body}
    # No completed turn yet: the queued prompt names the session, first
    # line only. A session with no turns and no queue has no title, and a
    # Qwen-generated name always wins over the prompt fallback.
    assert titles["pending-only"] == "First line"
    assert titles["empty"] == ""
    assert titles["named"] == "Qwen picked this name"


def test_list_sessions_exposes_ember_binding(client, session):
    _session(
        session,
        "bound",
        ember_session_id="s-abc123",
        workflow_id="wf-123",
    )
    _session(session, "unbound")

    body = client.get("/api/agents/sessions").json()
    bindings = {item["local_session_id"]: item["ember_session_id"] for item in body}
    assert bindings["bound"] == "s-abc123"
    assert bindings["unbound"] is None
    workflow_ids = {item["local_session_id"]: item["workflow_id"] for item in body}
    assert workflow_ids["bound"] == "wf-123"
    assert workflow_ids["unbound"] is None


def test_list_session_vms_maps_control_plane_states(client, monkeypatch):
    async def fake_list_sessions(limit=50, offset=0):
        return {
            "items": [
                {"session_id": "s-run", "state": "running", "expires_at": 99},
                {"session_id": "s-park", "state": "parked", "last_invoke_at": 5},
                {"session_id": "s-bank", "state": "banked"},
                {"session_id": "s-gone", "state": "destroying"},
            ]
        }

    monkeypatch.setattr(mcp._transport, "list_sessions", fake_list_sessions)
    # Deliberately NOT pre-refreshed. The autouse fixture leaves the cache
    # uninitialized, so this exercises the on-demand refresh inside the
    # endpoint, which is the whole reason the snapshot route survives a
    # broken stream. Pre-populating here would let that branch be deleted
    # with every test still green.
    body = client.get("/api/agents/vms").json()
    assert body["vms"]["s-run"]["state"] == "awake"
    assert body["vms"]["s-park"]["state"] == "asleep"
    assert body["vms"]["s-bank"]["state"] == "asleep"
    assert body["vms"]["s-gone"]["state"] == "off"
    assert body["vms"]["s-run"]["expires_at"] == 99
    assert body["vms"]["s-park"]["cp_state"] == "parked"


def test_list_session_vms_follows_pagination(client, monkeypatch):
    calls = []

    async def fake_list_sessions(limit=50, offset=0):
        calls.append(offset)
        if offset == 0:
            return {
                "total": 501,
                "items": [
                    {"session_id": f"s-{i}", "state": "parked"} for i in range(500)
                ],
            }
        return {"total": 501, "items": [{"session_id": "s-tail", "state": "running"}]}

    monkeypatch.setattr(mcp._transport, "list_sessions", fake_list_sessions)
    asyncio.run(agent_router._refresh_cp_state())
    body = client.get("/api/agents/vms").json()
    # The CP retains terminal rows for days; the tail page must be
    # fetched or an old parked session silently renders as "off".
    assert calls == [0, 500]
    assert len(body["vms"]) == 501
    assert body["vms"]["s-tail"]["state"] == "awake"


def test_list_session_vms_degrades_when_embervm_is_down(client, monkeypatch):
    async def broken_list_sessions(limit=50, offset=0):
        raise EmberVMTransportError("control plane unreachable")

    monkeypatch.setattr(mcp._transport, "list_sessions", broken_list_sessions)
    asyncio.run(agent_router._refresh_cp_state())
    body = client.get("/api/agents/vms").json()
    assert body["vms"] == {}
    assert "unreachable" in body["error"]


def test_vm_cache_refreshes_at_interval_and_stops_when_unused(monkeypatch):
    calls = []

    async def fake_list_sessions(limit=50, offset=0):
        calls.append((limit, offset))
        return {"items": [{"session_id": "s-1", "state": "running"}]}

    async def exercise():
        monkeypatch.setattr(mcp._transport, "list_sessions", fake_list_sessions)
        cache = agent_router._vm_state_cache
        monkeypatch.setattr(cache, "refresh_interval", 0.01)
        await agent_router._start_refresher()
        await asyncio.sleep(0.025)
        assert len(calls) >= 2
        assert cache.subscriber_count == 1
        await agent_router._stop_refresher()
        assert cache.subscriber_count == 0
        assert cache.task is None

    asyncio.run(exercise())


def test_vm_stream_emits_initial_and_changed_snapshots(monkeypatch):
    state = [{"session_id": "s-1", "state": "running"}]

    async def fake_list_sessions(limit=50, offset=0):
        return {"items": state}

    async def exercise():
        monkeypatch.setattr(mcp._transport, "list_sessions", fake_list_sessions)
        cache = agent_router._vm_state_cache
        monkeypatch.setattr(cache, "heartbeat_interval", 0.01)
        response = await agent_router.stream_session_vms()
        stream = response.body_iterator
        initial = await stream.__anext__()
        assert '"s-1"' in initial
        generation = cache.generation
        await agent_router._refresh_cp_state()
        assert cache.generation == generation
        assert not cache.change_event.is_set()
        state[0] = {"session_id": "s-1", "state": "parked"}
        await agent_router._refresh_cp_state()
        changed = await stream.__anext__()
        assert '"asleep"' in changed
        await stream.aclose()
        assert cache.subscriber_count == 0

    asyncio.run(exercise())


def test_refresher_survives_an_iteration_that_raises(monkeypatch):
    """A refresher that dies is invisible, so prove it does not.

    Before this loop caught broad exceptions, one malformed control-plane
    page killed it for good while every surface still looked healthy:
    subscribers kept getting heartbeats, no message ever arrived so the
    client fallback never engaged, and the snapshot endpoint saw a non-None
    task and stopped refreshing too.
    """
    calls = []

    async def flaky_list_sessions(limit=50, offset=0):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("malformed page")
        return {"items": [{"session_id": "s-1", "state": "running"}]}

    async def exercise():
        monkeypatch.setattr(mcp._transport, "list_sessions", flaky_list_sessions)
        cache = agent_router._vm_state_cache
        monkeypatch.setattr(cache, "refresh_interval", 0.01)
        task = asyncio.create_task(agent_router._run_vm_refresher())
        cache.task = task
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # It kept polling past the failure, and the failure was published
        # rather than swallowed into a frozen map.
        assert len(calls) >= 2
        assert cache.cache_map.get("s-1", {}).get("state") == "awake"

    asyncio.run(exercise())


def test_snapshot_reclaims_a_dead_refresher(monkeypatch):
    """A finished task is not None, so `task is None` alone froze this too."""
    calls = []

    async def fake_list_sessions(limit=50, offset=0):
        calls.append(1)
        return {"items": [{"session_id": "s-1", "state": "running"}]}

    async def dead():
        return None

    async def exercise():
        monkeypatch.setattr(mcp._transport, "list_sessions", fake_list_sessions)
        cache = agent_router._vm_state_cache
        finished = asyncio.create_task(dead())
        await finished
        cache.task = finished
        body = await agent_router.list_session_vms()
        assert calls, "a done() task must not count as owning the cache"
        assert body["vms"]["s-1"]["state"] == "awake"

    asyncio.run(exercise())


def test_vm_stream_heartbeats_when_idle(monkeypatch):
    async def fake_list_sessions(limit=50, offset=0):
        return {"items": []}

    async def exercise():
        monkeypatch.setattr(mcp._transport, "list_sessions", fake_list_sessions)
        cache = agent_router._vm_state_cache
        monkeypatch.setattr(cache, "heartbeat_interval", 0.01)
        response = await agent_router.stream_session_vms()
        stream = response.body_iterator
        await stream.__anext__()
        assert await stream.__anext__() == ": ping\n\n"
        await stream.aclose()

    asyncio.run(exercise())


def test_get_session_detail(client, session):
    row = _session(session, "detail")
    other_row = _session(session, "other")
    session.add_all(
        [
            AgentTurn(
                session_id=row.id,
                seq=2,
                prompt="two",
                result_text="result",
                usage_json='{"activities": ["shell"]}',
            ),
            AgentTurn(session_id=row.id, seq=1, prompt="one", result_text="result"),
            PendingMessage(
                session_id=row.id,
                seq=3,
                message_text="next",
                partial_text="in progress",
                partial_activities='[{"type": "tool", "name": "shell"}]',
            ),
            AgentTurn(
                session_id=other_row.id,
                seq=1,
                prompt="unrelated",
                result_text="unrelated",
                cost_usd=9.99,
            ),
            PendingMessage(session_id=other_row.id, seq=2, message_text="unrelated"),
        ]
    )
    session.commit()

    body = client.get(f"/api/agents/sessions/{row.id}").json()
    assert body["session"]["id"] == row.id
    assert body["session"]["turn_count"] == 2
    assert body["session"]["pending_count"] == 1
    assert body["session"]["total_cost_usd"] == 0
    assert body["session"]["title"] == "one"
    assert [turn["seq"] for turn in body["turns"]] == [1, 2]
    assert body["turns"][1]["usage"] == {"activities": ["shell"]}
    assert body["pending_queue"][0]["prompt"] == "next"
    assert body["pending_queue"][0]["partial_text"] == "in progress"
    assert body["pending_queue"][0]["partial_activities"] == [
        {"type": "tool", "name": "shell"}
    ]

    newer = client.get(f"/api/agents/sessions/{row.id}?after_seq=1").json()
    assert [turn["seq"] for turn in newer["turns"]] == [2]
    assert newer["session"]["id"] == row.id
    assert newer["pending_queue"][0]["prompt"] == "next"


def test_get_session_detail_rejects_negative_after_seq(client, session):
    row = _session(session, "after-seq")
    assert client.get(f"/api/agents/sessions/{row.id}?after_seq=-1").status_code == 422


def test_get_session_not_found(client):
    assert client.get("/api/agents/sessions/999").status_code == 404


def test_start_session_happy_path(client, session, monkeypatch):
    monkeypatch.setattr(
        "agent_sessions.router._persist_session",
        lambda local, workspace, branch, model, repo, **kwargs: store.create_session(
            session, local, workspace, branch, model, repo, **kwargs
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr("agent_sessions.router._schedule_next_message", lambda _: None)

    body = client.post("/api/agents/sessions", json={"prompt": "Hello"}).json()
    assert body["accepted"] is True
    assert body["turn"] == 1


def test_start_session_persists_repo(client, session, monkeypatch):
    monkeypatch.setattr(
        "agent_sessions.router._persist_session",
        lambda local, workspace, branch, model, repo, **kwargs: store.create_session(
            session, local, workspace, branch, model, repo, **kwargs
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr("agent_sessions.router._schedule_next_message", lambda _: None)

    body = client.post(
        "/api/agents/sessions",
        json={"prompt": "Hello", "repo": "jomcgi/homelab"},
    ).json()
    row = session.get(AgentSession, body["session_id"])
    assert body["accepted"] is True
    assert row.repo == "jomcgi/homelab"
    assert client.get("/api/agents/sessions").json()[0]["repo"] == "jomcgi/homelab"


def test_start_session_persists_triggered_by_header(client, session, monkeypatch):
    monkeypatch.setattr(
        "agent_sessions.router._persist_session",
        lambda local, workspace, branch, model, repo, **kwargs: store.create_session(
            session, local, workspace, branch, model, repo, **kwargs
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr("agent_sessions.router._schedule_next_message", lambda _: None)

    response = client.post(
        "/api/agents/sessions",
        json={"prompt": "Hello"},
        headers={"X-Auth-Email": "  EXAMPLE@EXAMPLE.COM  "},
    )

    assert response.status_code == 200
    session_id = response.json()["session_id"]
    assert session.get(AgentSession, session_id).triggered_by == "example@example.com"
    assert client.get("/api/agents/sessions").json()[0]["triggered_by"] == (
        "example@example.com"
    )


def test_start_session_without_triggered_by_header_succeeds(
    client, session, monkeypatch
):
    monkeypatch.setattr(
        "agent_sessions.router._persist_session",
        lambda local, workspace, branch, model, repo, **kwargs: store.create_session(
            session, local, workspace, branch, model, repo, **kwargs
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr("agent_sessions.router._schedule_next_message", lambda _: None)

    response = client.post("/api/agents/sessions", json={"prompt": "Hello"})

    assert response.status_code == 200
    session_id = response.json()["session_id"]
    assert session.get(AgentSession, session_id).triggered_by is None


def test_start_session_rejects_unknown_repo(client):
    body = client.post(
        "/api/agents/sessions", json={"prompt": "Hello", "repo": "bad/repo"}
    ).json()
    assert body["accepted"] is False
    assert body["error"].startswith("unknown repo bad/repo; catalog:")


def test_list_repos_degrades_when_github_is_down(client, monkeypatch):
    async def github_down(url):
        raise RuntimeError("offline")

    monkeypatch.setattr("agent_sessions.router._github_get", github_down)
    monkeypatch.setattr("agent_sessions.router._DEFAULT_BRANCH_CACHE", {})

    response = client.get("/api/agents/repos")
    assert response.status_code == 200
    assert [repo["id"] for repo in response.json()["repos"]] == [
        "jomcgi/homelab",
        "weave-hand/loom",
        "colincee/homelab",
        "scotscottmca/parkedlikea",
    ]
    assert all(repo["default_branch"] is None for repo in response.json()["repos"])


def test_list_repos_success_cache_hit_and_expiry(client, monkeypatch):
    repo_ids = [
        "jomcgi/homelab",
        "weave-hand/loom",
        "colincee/homelab",
        "scotscottmca/parkedlikea",
    ]
    calls = []
    now = 1000.0

    async def github_get(url):
        calls.append(url)
        repo_id = url.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"default_branch": f"branch-{repo_id}"})

    monkeypatch.setattr("agent_sessions.router._DEFAULT_BRANCH_CACHE", {})
    monkeypatch.setattr("agent_sessions.router.time.monotonic", lambda: now)
    monkeypatch.setattr("agent_sessions.router._github_get", github_get)

    first = client.get("/api/agents/repos")
    assert first.status_code == 200
    assert len(calls) == len(repo_ids)
    assert [repo["id"] for repo in first.json()["repos"]] == repo_ids

    async def github_failed(url):
        raise AssertionError(f"unexpected cache miss: {url}")

    monkeypatch.setattr("agent_sessions.router._github_get", github_failed)
    second = client.get("/api/agents/repos")
    assert second.json() == first.json()
    assert len(calls) == len(repo_ids)

    monkeypatch.setattr("agent_sessions.router.time.monotonic", lambda: now + 301)
    monkeypatch.setattr("agent_sessions.router._github_get", github_get)
    third = client.get("/api/agents/repos")
    assert third.json() == first.json()
    assert len(calls) == len(repo_ids) * 2


def test_list_repo_branches_success_with_pagination(client, monkeypatch):
    monkeypatch.setenv("GITHUB_API_TOKEN", "test-token")
    page_one = [{"name": f"branch-{index:03d}"} for index in range(100)]
    page_two = [{"name": "main"}] + [
        {"name": f"branch-{index:03d}"} for index in range(100, 120)
    ]
    calls = []

    async def github_get(url):
        calls.append(url)
        if url.endswith("/jomcgi/homelab"):
            return httpx.Response(200, json={"default_branch": "main"})
        if "page=2" in url:
            return httpx.Response(200, json=page_two)
        return httpx.Response(
            200,
            json=page_one,
            headers={
                "Link": '<https://api.github.com/repos/jomcgi/homelab/branches?per_page=100&page=2>; rel="next"'
            },
        )

    monkeypatch.setattr("agent_sessions.router._github_get", github_get)

    response = client.get("/api/agents/repos/jomcgi/homelab/branches")

    assert response.status_code == 200
    body = response.json()
    assert len(body["branches"]) == 121
    assert body["branches"][0] == {"name": "main"}
    assert body["branches"][1:] == sorted(
        body["branches"][1:], key=lambda item: item["name"]
    )
    assert len(calls) == 3
    assert "page=2" in calls[-1]


def test_list_repo_branches_requires_catalog_and_token(client, monkeypatch):
    assert (
        client.get("/api/agents/repos/not-cataloged/repo/branches").status_code == 404
    )
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)
    response = client.get("/api/agents/repos/jomcgi/homelab/branches")
    assert response.status_code == 503
    assert "GITHUB_API_TOKEN" in response.json()["detail"]


def test_start_session_model_validation(client):
    body = client.post(
        "/api/agents/sessions", json={"prompt": "Hello", "model": "unknown"}
    ).json()
    assert body["accepted"] is False
    assert "Unknown model" in body["error"]


def test_send_message_happy_path(client, session, monkeypatch):
    row = _session(session, "send", status="completed")
    monkeypatch.setattr("agent_sessions.router._load_session_row", lambda _: row)
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._set_session_status",
        lambda session_id, status: store.update_session_status(
            session, session_id, status
        ),
    )
    monkeypatch.setattr("agent_sessions.router._schedule_next_message", lambda _: None)

    body = client.post(
        f"/api/agents/sessions/{row.id}/messages", json={"prompt": "follow up"}
    ).json()
    assert body == {"accepted": True, "session_id": row.id, "turn": 1}
    assert session.get(AgentSession, row.id).status == "running"


def test_send_message_model_family_mismatch(client, session, monkeypatch):
    row = _session(session, "pinned", model="opus")
    monkeypatch.setattr("agent_sessions.router._load_session_row", lambda _: row)
    body = client.post(
        f"/api/agents/sessions/{row.id}/messages",
        json={"prompt": "follow up", "model": "luna"},
    ).json()
    assert body["accepted"] is False
    assert "Model family mismatch" in body["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["opus", "qwen"])
async def test_codex_login_gate_skips_broker_for_non_codex_models(monkeypatch, model):
    calls = []

    async def broker_request(*args):
        calls.append(args)
        raise AssertionError("non-Codex models must not call the broker")

    monkeypatch.setattr(mcp, "_broker_request", broker_request)

    assert await codex_login_gate(model) is None
    assert calls == []


@pytest.mark.asyncio
async def test_codex_login_gate_granted_does_not_start_login(monkeypatch):
    calls = []

    async def broker_request(method, path):
        calls.append((method, path))
        return {"state": "granted"}

    monkeypatch.setattr(mcp, "_broker_request", broker_request)

    assert await codex_login_gate("luna") is None
    assert calls == [("GET", "/grants/codex-cluster/login/status")]


@pytest.mark.asyncio
async def test_codex_login_gate_none_returns_device_login(monkeypatch):
    calls = []

    async def broker_request(method, path):
        calls.append((method, path))
        if method == "GET":
            return {"state": "none"}
        return {"verification_url": "https://example.test", "user_code": "CODE"}

    monkeypatch.setattr(mcp, "_broker_request", broker_request)

    result = await codex_login_gate("luna")
    assert result["login_required"] is True
    assert result["verification_url"] == "https://example.test"
    assert result["user_code"] == "CODE"
    assert calls == [
        ("GET", "/grants/codex-cluster/login/status"),
        ("POST", "/grants/codex-cluster/login/start"),
    ]


@pytest.mark.asyncio
async def test_codex_login_gate_login_pending_is_not_an_error(monkeypatch):
    async def broker_request(method, path):
        if method == "GET":
            return {"state": "none"}
        response = httpx.Response(409, request=httpx.Request("POST", "https://broker"))
        raise httpx.HTTPStatusError(
            "login pending", request=response.request, response=response
        )

    monkeypatch.setattr(mcp, "_broker_request", broker_request)

    result = await codex_login_gate("luna")
    assert result["pending"] is True
    assert result["login_required"] is True


@pytest.mark.asyncio
async def test_codex_login_gate_broker_failure_returns_login_error(monkeypatch):
    async def broker_request(*args):
        raise Exception("broker offline")

    monkeypatch.setattr(mcp, "_broker_request", broker_request)

    assert await codex_login_gate("luna") is None


@pytest.mark.asyncio
async def test_codex_login_gate_unset_broker_url_proceeds(monkeypatch):
    monkeypatch.delenv("EMBER_TOKENBROKER_URL", raising=False)

    assert await codex_login_gate("luna") is None


@pytest.mark.asyncio
async def test_codex_login_gate_does_not_swallow_unknown_model(monkeypatch):
    async def broker_request(*args):
        raise AssertionError("model validation must happen before broker I/O")

    monkeypatch.setattr(mcp, "_broker_request", broker_request)

    with pytest.raises(ValueError, match="Unknown model"):
        await codex_login_gate("unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["INVALID", "-invalid"])
async def test_codex_login_gate_does_not_swallow_invalid_grant(monkeypatch, grant):
    async def broker_request(*args):
        raise AssertionError("grant validation must happen before broker I/O")

    monkeypatch.setattr(mcp, "_broker_request", broker_request)

    with pytest.raises(ValueError, match="invalid grant name"):
        await codex_login_gate("luna", grant)


def test_send_message_gates_on_effective_model(client, session, monkeypatch):
    row = _session(session, "pinned-codex", model="luna", status="completed")
    seen = []

    async def fake_gate(model):
        seen.append(model)
        return None

    monkeypatch.setattr("agent_sessions.router._load_session_row", lambda _: row)
    monkeypatch.setattr("agent_sessions.router.codex_login_gate", fake_gate)
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._set_session_status",
        lambda session_id, status: store.update_session_status(
            session, session_id, status
        ),
    )
    monkeypatch.setattr("agent_sessions.router._schedule_next_message", lambda _: None)

    body = client.post(
        f"/api/agents/sessions/{row.id}/messages",
        json={"prompt": "follow up", "model": "terra"},
    ).json()
    assert body["accepted"] is True
    assert seen == ["terra"]


@pytest.mark.parametrize("model", ["opus", "qwen"])
def test_router_start_non_codex_models_enqueue_without_broker(
    client, session, monkeypatch, model
):
    calls = []

    async def broker_request(*args):
        calls.append(args)
        raise AssertionError("non-Codex models must not call the broker")

    monkeypatch.setattr(mcp, "_broker_request", broker_request)
    monkeypatch.setattr(
        "agent_sessions.router._persist_session",
        lambda local, workspace, branch, selected_model, repo, **kwargs: (
            store.create_session(
                session, local, workspace, branch, selected_model, repo, **kwargs
            )
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, selected_model: (
            store.create_pending_message(
                session, session_id, prompt, selected_model
            ).seq
        ),
    )
    monkeypatch.setattr("agent_sessions.router._schedule_next_message", lambda _: None)

    body = client.post(
        "/api/agents/sessions",
        json={"prompt": "Hello", "model": model},
    ).json()

    assert body["accepted"] is True
    assert body["turn"] == 1
    assert calls == []


def test_router_start_codex_login_required_preserves_session_and_watches(
    client, session, monkeypatch
):
    calls = []
    watched = []

    async def broker_request(method, path):
        calls.append((method, path))
        if method == "GET":
            return {"state": "none"}
        return {"verification_url": "https://example.test", "user_code": "CODE"}

    monkeypatch.setattr(mcp, "_broker_request", broker_request)
    monkeypatch.setattr(
        "agent_sessions.router._persist_session",
        lambda local, workspace, branch, model, repo, **kwargs: store.create_session(
            session, local, workspace, branch, model, repo, **kwargs
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router.watch_for_login",
        lambda grant, callback: watched.append((grant, callback)),
    )

    body = client.post(
        "/api/agents/sessions", json={"prompt": "Hello", "model": "luna"}
    ).json()

    assert body["accepted"] is False
    assert body["login_required"] is True
    assert body["verification_url"] == "https://example.test"
    assert body["user_code"] == "CODE"
    assert body["session_id"]
    assert body["turn"] == 1
    row = store.get_session(session, body["session_id"])
    assert row.discord_thread is None
    assert watched and watched[0][0] == "codex-cluster"
    assert calls == [
        ("GET", "/grants/codex-cluster/login/status"),
        ("POST", "/grants/codex-cluster/login/start"),
    ]


def test_send_message_session_not_found(client, monkeypatch):
    monkeypatch.setattr("agent_sessions.router._load_session_row", lambda _: None)
    body = client.post(
        "/api/agents/sessions/999/messages", json={"prompt": "hello"}
    ).json()
    assert body == {"accepted": False, "error": "Unknown agent session 999"}


def test_delete_session(client, session, monkeypatch):
    row = _session(session, "delete", ember_session_id="ember-1")
    destroyed = []

    async def fake_destroy(ember_session_id):
        destroyed.append(ember_session_id)
        return {"session_id": ember_session_id, "state": "destroyed"}

    monkeypatch.setattr("agent_sessions.router._load_session_row", lambda _: row)
    monkeypatch.setattr(
        "agent_sessions.router._transport.destroy_session", fake_destroy
    )
    monkeypatch.setattr(
        "agent_sessions.router._clear_ember_bindings_for",
        lambda ember_id: store.clear_ember_bindings_by_ember_id(session, ember_id),
    )
    body = client.delete(f"/api/agents/sessions/{row.id}").json()
    assert destroyed == ["ember-1"]
    assert body["cleared_bindings"] == [row.id]
    assert session.get(AgentSession, row.id).ember_session_id is None


def test_delete_session_not_found(client, monkeypatch):
    monkeypatch.setattr("agent_sessions.router._load_session_row", lambda _: None)
    assert client.delete("/api/agents/sessions/999").status_code == 404


def test_search_empty_query(client):
    assert client.get("/api/agents/search?q=").json() == {"results": []}


def test_mcp_tools_still_work(session, monkeypatch):
    monkeypatch.setattr(
        mcp,
        "_persist_session",
        lambda local, workspace, branch, model, repo=None, **kwargs: (
            store.create_session(
                session, local, workspace, branch, model, repo, **kwargs
            )
        ),
    )
    monkeypatch.setattr(
        mcp,
        "_persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _: None)

    body = asyncio.run(mcp.monolith_agent_session_start("hello"))
    assert body["accepted"] is True
    assert body["turn"] == 1


def test_start_session_marks_message_ui_originated(client, session, monkeypatch):
    """A session started from the UI must not echo its turn to Discord."""
    mcp._ui_originated.clear()
    monkeypatch.setattr(
        "agent_sessions.router._persist_session",
        lambda local, workspace, branch, model, repo, **kwargs: store.create_session(
            session, local, workspace, branch, model, repo, **kwargs
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr("agent_sessions.router._schedule_next_message", lambda _: None)

    body = client.post("/api/agents/sessions", json={"prompt": "Hello"}).json()
    assert store.get_session(session, body["session_id"]).system_prompt is None
    assert mcp._consume_ui_originated(body["session_id"], body["turn"]) is True
    mcp._ui_originated.clear()


def test_start_session_for_discord_thread_has_no_system_prompt(session, monkeypatch):
    async def broker_request(*args):
        return {"state": "granted"}

    monkeypatch.setattr(mcp, "_broker_request", broker_request)
    monkeypatch.setattr(api, "_persist_pending_message", lambda *_args: 1)
    monkeypatch.setattr(api, "_schedule_next_message", lambda _session_id: None)
    monkeypatch.setattr(
        api,
        "_persist_session",
        lambda local, workspace, branch, model, repo=None, **kwargs: (
            store.create_session(
                session, local, workspace, branch, model, repo, **kwargs
            )
        ),
    )

    session_id = asyncio.run(
        api.start_session_for_thread("thread-1", "Hello", repo=None)
    )

    row = store.get_session(session, session_id)
    assert row.discord_thread == "thread-1"
    assert row.system_prompt is None


@pytest.mark.parametrize("model", ["opus", "qwen"])
def test_send_to_thread_session_queues_without_broker_for_non_codex(
    session, monkeypatch, model
):
    row = _session(session, "thread-send", model=model)
    calls = []

    async def broker_request(*args):
        calls.append(args)
        raise AssertionError("Non-Codex sessions must not call the broker")

    monkeypatch.setattr(mcp, "_broker_request", broker_request)
    monkeypatch.setattr(api, "session_id_for_thread", lambda _: row.id)
    monkeypatch.setattr(api, "_load_session_row", lambda _: row)
    monkeypatch.setattr(
        api,
        "_persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr(
        api,
        "_set_session_status",
        lambda session_id, status: store.update_session_status(
            session, session_id, status
        ),
    )
    monkeypatch.setattr(api, "_schedule_next_message", lambda _: None)

    result = asyncio.run(api.send_to_thread_session("thread-1", "follow up"))

    assert result["action"] == "queued"
    assert result["turn"] == 1
    assert calls == []


def test_send_message_marks_message_ui_originated(client, session, monkeypatch):
    """A follow-up sent from the UI must not echo its turn to Discord."""
    mcp._ui_originated.clear()
    row = _session(session, "send-ui", status="completed")
    monkeypatch.setattr("agent_sessions.router._load_session_row", lambda _: row)
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._set_session_status",
        lambda session_id, status: store.update_session_status(
            session, session_id, status
        ),
    )
    monkeypatch.setattr("agent_sessions.router._schedule_next_message", lambda _: None)

    body = client.post(
        f"/api/agents/sessions/{row.id}/messages", json={"prompt": "follow up"}
    ).json()
    assert mcp._consume_ui_originated(row.id, body["turn"]) is True
    mcp._ui_originated.clear()
