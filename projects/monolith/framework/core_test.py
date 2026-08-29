"""Unit tests for the FastMonolith composition core (framework/core.py).

These tests compose synthetic modules (no real domains) so they exercise the
framework's own behavior: validation, tier selection, health endpoints, MCP
gating, and the leader-singleton start/stop composition.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging

import core.leadership as leadership
import framework.core as framework_core
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

from framework import (
    PRIVATE_PROFILE,
    PUBLIC_PROFILE,
    Module,
    Tier,
    build_app,
    build_private_lifespan,
    domain_profile,
    start_leader_singletons,
    stop_leader_singletons,
)

# A private-tier test profile with the process-global concerns disabled so the
# tests stay hermetic (no OTel provider, no static mount, no MCP instance).
_PLAIN_PRIVATE = dataclasses.replace(
    PRIVATE_PROFILE,
    mcp_enabled=False,
    otel_enabled=False,
    static_frontend=False,
)

_OTEL_PRIVATE = dataclasses.replace(
    _PLAIN_PRIVATE,
    otel_enabled=True,
)

_WHOAMI_CORE_TEST_REGISTERED = False


def _register_whoami_core_test() -> None:
    global _WHOAMI_CORE_TEST_REGISTERED
    if _WHOAMI_CORE_TEST_REGISTERED:
        return

    from auth.api import current_principal
    from core.mcp_app import mcp

    @mcp.tool(name="whoami_core_test")
    async def whoami_core_test() -> str:
        return current_principal().subject

    _WHOAMI_CORE_TEST_REGISTERED = True


def _mcp_response_json(response: httpx.Response) -> dict:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = next(
            line.removeprefix("data:").strip()
            for line in response.text.splitlines()
            if line.startswith("data:")
        )
        return json.loads(data)
    return response.json()


def test_build_app_skips_otel_without_an_endpoint(monkeypatch):
    calls: list[tuple[FastAPI, str]] = []
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setattr(
        framework_core,
        "_setup_otel",
        lambda app, service_name: calls.append((app, service_name)),
    )

    build_app(_OTEL_PRIVATE, [])

    assert calls == []


def test_build_app_enables_otel_with_an_endpoint(monkeypatch):
    calls: list[tuple[FastAPI, str]] = []
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "http://collector.example:4318/v1/traces",
    )
    monkeypatch.setattr(
        framework_core,
        "_setup_otel",
        lambda app, service_name: calls.append((app, service_name)),
    )

    app = build_app(_OTEL_PRIVATE, [])

    assert calls == [(app, "monolith-backend")]


def _routed_module(name: str, prefix: str) -> Module:
    def register(app: FastAPI) -> None:
        @app.get(f"/api/{prefix}/ping")
        def ping():
            return {"pong": name}

    def register_public(app: FastAPI) -> None:
        @app.get(f"/api/{prefix}/public-ping")
        def public_ping():
            return {"pong": name}

    return Module(name=name, register=register, register_public=register_public)


def _paths(app: FastAPI) -> set[str]:
    return {r.path for r in app.routes if hasattr(r, "path")}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_public_profile_rejects_module_without_public_surface():
    private_only = Module(name="privy", register=lambda app: None)
    with pytest.raises(ValueError, match="no public surface"):
        build_app(PUBLIC_PROFILE, [private_only])


def test_restricted_private_profile_rejects_secret_requiring_module():
    secretive = Module(
        name="secretive",
        register=lambda app: None,
        requires_secrets=frozenset({"SOME_TOKEN"}),
    )
    restricted = dataclasses.replace(_PLAIN_PRIVATE, allowed_secrets=frozenset())
    with pytest.raises(ValueError, match="SOME_TOKEN"):
        build_app(restricted, [secretive])


def test_public_tier_ignores_private_surface_secrets():
    """A both-tier module's private-surface secrets do not block public
    composition: the public tier never mounts the secret-consuming hooks."""
    both_tier = Module(
        name="both",
        register=lambda app: None,
        register_public=lambda app: None,
        requires_secrets=frozenset({"SOME_TOKEN"}),
    )
    app = build_app(PUBLIC_PROFILE, [both_tier])
    assert "/healthz" in {r.path for r in app.routes if hasattr(r, "path")}


def test_duplicate_module_names_rejected():
    a = _routed_module("dup", "a")
    b = _routed_module("dup", "b")
    with pytest.raises(ValueError, match="duplicate"):
        build_app(_PLAIN_PRIVATE, [a, b])


def test_private_profile_rejects_contentless_module():
    empty = Module(name="empty")
    with pytest.raises(ValueError, match="contributes nothing"):
        build_app(_PLAIN_PRIVATE, [empty])


# ---------------------------------------------------------------------------
# Tier selection + health
# ---------------------------------------------------------------------------


def test_private_tier_mounts_register_not_register_public():
    app = build_app(_PLAIN_PRIVATE, [_routed_module("m", "m")])
    paths = _paths(app)
    assert "/api/m/ping" in paths
    assert "/api/m/public-ping" not in paths
    assert "/healthz" in paths
    # The private tier serves the deep route too. It used to opt out, which
    # made every private-tier register_health component dead code: the
    # component composes and the endpoint that would run it does not exist.
    assert "/api/health" in paths


def test_public_tier_mounts_register_public_only():
    app = build_app(PUBLIC_PROFILE, [_routed_module("m", "m")])
    paths = _paths(app)
    assert "/api/m/public-ping" in paths
    assert "/api/m/ping" not in paths
    assert "/healthz" in paths
    assert "/api/health" in paths  # deep health on the public tier


@pytest.fixture
def _sqlite_engine(monkeypatch):
    """Point core.db.get_engine at an in-memory sqlite DB so /api/health's
    SELECT 1 baseline succeeds without a real Postgres."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr("core.db.get_engine", lambda: engine)
    return engine


@pytest.fixture
def _public_app_with_logs(caplog):
    """Build a PUBLIC tier app without losing caplog capture.

    build_app calls configure_logging, and logging.basicConfig(force=True)
    removes every handler already on the root logger, including the one
    caplog installed for this test. Records emitted after build_app are
    therefore invisible to caplog unless the handler is put back, which is
    what this fixture does.
    """
    root = logging.getLogger()

    def _build(modules):
        app = build_app(PUBLIC_PROFILE, modules)
        if caplog.handler not in root.handlers:
            root.addHandler(caplog.handler)
        return app

    try:
        yield _build
    finally:
        root.removeHandler(caplog.handler)


def test_public_tier_health_has_no_components_when_no_module_registers_health(
    _sqlite_engine,
):
    app = build_app(PUBLIC_PROFILE, [_routed_module("m", "m")])
    resp = TestClient(app).get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_public_tier_health_folds_in_module_components(_sqlite_engine):
    async def healthy_check():
        return {"ok": True, "detail": "all good"}

    module = Module(
        name="m",
        register_public=lambda app: None,
        register_health={"widget": healthy_check},
    )
    app = build_app(PUBLIC_PROFILE, [module])
    resp = TestClient(app).get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["components"] == {"widget": {"ok": True, "detail": "all good"}}


def test_advisory_failure_returns_200_degraded_and_logs_info(
    _sqlite_engine, caplog, _public_app_with_logs
):
    async def unhealthy_check():
        return {"ok": False, "detail": "widget is behind"}

    module = Module(
        name="m",
        register_public=lambda app: None,
        register_health_advisory={"widget": unhealthy_check},
    )
    app = _public_app_with_logs([module])
    with caplog.at_level(logging.INFO, logger="monolith.public"):
        resp = TestClient(app).get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["degraded"] == ["widget"]
    assert not any(
        "public health components unhealthy" in r.message for r in caplog.records
    )
    assert any(
        r.levelno == logging.INFO
        and "public health advisory components degraded" in r.message
        for r in caplog.records
    )


def test_fatal_failure_alongside_advisory_still_503s_and_warns(
    _sqlite_engine, caplog, _public_app_with_logs
):
    async def advisory_check():
        return {"ok": False}

    async def fatal_check():
        return {"ok": False, "detail": "fatal"}

    module = Module(
        name="m",
        register_public=lambda app: None,
        register_health={"database": fatal_check},
        register_health_advisory={"widget": advisory_check},
    )
    app = _public_app_with_logs([module])
    with caplog.at_level(logging.INFO, logger="monolith.public"):
        resp = TestClient(app).get("/api/health")
    assert resp.status_code == 503
    assert resp.json()["degraded"] == ["widget"]
    assert any(
        r.levelno == logging.WARNING
        and "public health components unhealthy" in r.message
        and "database" in r.message
        for r in caplog.records
    )


def test_raising_advisory_stays_200_and_is_degraded(
    _sqlite_engine, caplog, _public_app_with_logs
):
    async def crashing_check():
        raise RuntimeError("advisory boom")

    module = Module(
        name="m",
        register_public=lambda app: None,
        register_health_advisory={"widget": crashing_check},
    )
    app = _public_app_with_logs([module])
    with caplog.at_level(logging.INFO, logger="monolith.public"):
        resp = TestClient(app).get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["degraded"] == ["widget"]


def test_public_tier_health_503_logs_failing_component(
    _sqlite_engine, caplog, _public_app_with_logs
):
    async def unhealthy_check():
        return {"ok": False, "detail": "widget is broken"}

    module = Module(
        name="m",
        register_public=lambda app: None,
        register_health={"widget": unhealthy_check},
    )
    app = _public_app_with_logs([module])
    with caplog.at_level(logging.WARNING, logger="monolith.public"):
        resp = TestClient(app).get("/api/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["components"]["widget"]["ok"] is False
    records = [r for r in caplog.records if r.name == "monolith.public"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "public health components unhealthy" in records[0].message
    assert "widget" in records[0].message
    assert "widget is broken" in records[0].message


def test_public_tier_healthy_component_logs_no_component_warning(
    _sqlite_engine, caplog, _public_app_with_logs
):
    async def healthy_check():
        return {"ok": True, "detail": "all good"}

    module = Module(
        name="m",
        register_public=lambda app: None,
        register_health={"widget": healthy_check},
    )
    app = _public_app_with_logs([module])
    with caplog.at_level(logging.WARNING, logger="monolith.public"):
        resp = TestClient(app).get("/api/health")
    assert resp.status_code == 200
    assert not any(
        "public health components unhealthy" in r.message for r in caplog.records
    )


def test_public_tier_health_crashing_component_reports_not_ok_not_500(
    _sqlite_engine, caplog, _public_app_with_logs
):
    async def crashing_check():
        raise RuntimeError("kaboom")

    module = Module(
        name="m",
        register_public=lambda app: None,
        register_health={"widget": crashing_check},
    )
    app = _public_app_with_logs([module])
    with caplog.at_level(logging.WARNING, logger="monolith.public"):
        resp = TestClient(app).get("/api/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["components"]["widget"]["ok"] is False
    assert "kaboom" in body["components"]["widget"]["detail"]
    assert any(
        r.name == "monolith.public"
        and r.levelno == logging.WARNING
        and "public health components unhealthy" in r.message
        and "widget" in r.message
        and "kaboom" in r.message
        for r in caplog.records
    )


def test_public_tier_has_no_lifespan_side_effects_or_mcp():
    app = build_app(PUBLIC_PROFILE, [_routed_module("m", "m")])
    mounts = [r for r in app.routes if getattr(r, "path", "") == "/mcp"]
    assert not mounts


def test_domain_profile_shape():
    profile = domain_profile("hikes")
    assert profile.tier is Tier.PRIVATE
    assert profile.service_name == "monolith-hikes"
    assert profile.static_frontend is False
    assert profile.leader_singletons is True


# ---------------------------------------------------------------------------
# MCP gating
# ---------------------------------------------------------------------------


def test_mcp_only_module_is_valid_and_mounts_mcp():
    calls: list[str] = []

    mcp_only = Module(name="tools", register_mcp=lambda: calls.append("mcp"))
    profile = dataclasses.replace(_PLAIN_PRIVATE, mcp_enabled=True)
    app = build_app(profile, [mcp_only])

    assert calls == ["mcp"]
    assert any(getattr(r, "path", "") == "/mcp" for r in app.routes)


@pytest.mark.asyncio
async def test_mcp_mount_serves_streamable_http_not_sse():
    profile = dataclasses.replace(_PLAIN_PRIVATE, mcp_enabled=True)
    app = build_app(profile, [Module(name="tools", register_mcp=lambda: None)])

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            sse_response = await client.get("/mcp/sse")
            initialize_response = await client.post(
                "/mcp/",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
            )

    assert sse_response.status_code == 404
    assert initialize_response.status_code == 200


@pytest.mark.asyncio
async def test_mcp_tool_sees_per_message_principal(monkeypatch):
    import auth.api as auth_api
    from auth.api import Authority, Principal, PrincipalKind

    principals = {
        subject: Principal(
            subject=subject,
            actor=(),
            scope=(),
            groups=(),
            email=f"{subject}@example.com",
            kind=PrincipalKind.HUMAN,
            authority=Authority.STANDING,
        )
        for subject in ("alice", "bob")
    }

    class Resolver:
        async def resolve(self, token: str) -> Principal:
            return principals[token]

    monkeypatch.setattr(auth_api, "get_default_resolver", lambda: Resolver())
    profile = dataclasses.replace(_PLAIN_PRIVATE, mcp_enabled=True)
    app = build_app(
        profile,
        [Module(name="whoami", register_mcp=_register_whoami_core_test)],
    )

    def headers(subject: str, session_id: str | None = None) -> dict[str, str]:
        result = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {subject}",
        }
        if session_id is not None:
            result["Mcp-Session-Id"] = session_id
        return result

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            initialize_response = await client.post(
                "/mcp/",
                headers=headers("alice"),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
            )
            assert initialize_response.status_code == 200
            session_id = initialize_response.headers.get("mcp-session-id")

            initialized_response = await client.post(
                "/mcp/",
                headers=headers("alice", session_id),
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
            )
            assert initialized_response.status_code in (200, 202)

            alice_response = await client.post(
                "/mcp/",
                headers=headers("alice", session_id),
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "whoami_core_test", "arguments": {}},
                },
            )
            bob_response = await client.post(
                "/mcp/",
                headers=headers("bob", session_id),
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "whoami_core_test", "arguments": {}},
                },
            )

    assert alice_response.status_code == 200
    assert bob_response.status_code == 200
    alice_result = _mcp_response_json(alice_response)["result"]
    bob_result = _mcp_response_json(bob_response)["result"]
    assert alice_result["content"][0]["text"] == "alice"
    assert bob_result["content"][0]["text"] == "bob"


def test_mcp_mount_is_wrapped_in_principal_middleware():
    """A route existing at /mcp is not the same as that route authenticating.

    build_app keeps the unwrapped app in scope as `raw_mcp_http_app`, because
    the lifespan has to come off it (PrincipalMiddleware is a plain callable
    with no .lifespan). So the unauthenticated app is reachable by name at the
    mount, and mounting it instead is a one-word edit.

    Nothing else catches that. The middleware's own tests construct it
    directly, which proves it works rather than that it is installed, and the
    mount test above passes for a route that authenticates nothing. Mounting
    `raw_mcp_http_app` here leaves the ENTIRE suite green (verified: 721 pass)
    while serving the whole tool catalogue to any unauthenticated caller that
    reaches the ClusterIP, which Context Forge does without traversing the
    ingress gate.
    """
    from auth.api import PrincipalMiddleware

    profile = dataclasses.replace(_PLAIN_PRIVATE, mcp_enabled=True)
    app = build_app(profile, [Module(name="tools", register_mcp=lambda: None)])

    mcp_route = next(r for r in app.routes if getattr(r, "path", "") == "/mcp")
    assert isinstance(mcp_route.app, PrincipalMiddleware)


def test_private_app_registers_the_auth_error_handler():
    """The handler being importable is not the same as it being installed.

    Without this registration FastAPI's built-in HTTPException handler serves
    AuthError (which subclasses it) and emits only {"detail": ...}, so HTTP
    routes silently drop the `reason` that the /mcp mount emits. That is a
    divergence no test of the handler in isolation can catch, because such a
    test registers the handler itself.
    """
    from auth.api import AuthError

    app = build_app(_PLAIN_PRIVATE, [_routed_module("m", "m")])

    assert AuthError in app.exception_handlers


def test_mcp_disabled_profile_skips_registration_and_mount():
    calls: list[str] = []

    module = Module(
        name="tools",
        register=lambda app: None,
        register_mcp=lambda: calls.append("mcp"),
    )
    app = build_app(_PLAIN_PRIVATE, [module])

    assert calls == []
    assert not any(getattr(r, "path", "") == "/mcp" for r in app.routes)


# ---------------------------------------------------------------------------
# Leader singleton composition
# ---------------------------------------------------------------------------


def _leader_module(name: str, log: list[str]) -> Module:
    async def leader_start(app: FastAPI) -> list[asyncio.Task]:
        log.append(f"{name}:start")

        async def _forever():
            await asyncio.Event().wait()

        return [asyncio.create_task(_forever(), name=f"{name}-task")]

    async def leader_stop(app: FastAPI) -> None:
        log.append(f"{name}:stop")

    return Module(name=name, leader_start=leader_start, leader_stop=leader_stop)


class _FakeLeaderElector:
    constructed = 0

    def __init__(self, lease_key: str) -> None:
        type(self).constructed += 1
        self.lease_key = lease_key

    async def run(self, on_acquire, on_resign) -> None:
        await on_acquire()
        await asyncio.Event().wait()


def _patch_fake_elector(monkeypatch):
    _FakeLeaderElector.constructed = 0
    monkeypatch.setattr(leadership, "LeaderElector", _FakeLeaderElector)


async def _run_private_lifespan(monkeypatch, modules, env_name, env_value=None):
    _patch_fake_elector(monkeypatch)
    if env_value is None:
        monkeypatch.delenv(env_name, raising=False)
    else:
        monkeypatch.setenv(env_name, env_value)
    app = FastAPI()
    async with build_private_lifespan(_PLAIN_PRIVATE, modules)(app):
        await asyncio.sleep(0)
    return app


@pytest.mark.asyncio
async def test_start_and_stop_leader_singletons_compose_across_modules():
    log: list[str] = []
    modules = [_leader_module("a", log), _leader_module("b", log)]
    app = FastAPI()

    await start_leader_singletons(app, modules)
    assert log == ["a:start", "b:start"]
    assert len(app.state.singleton_tasks) == 2

    tasks = list(app.state.singleton_tasks)
    await stop_leader_singletons(app, modules)
    assert log == ["a:start", "b:start", "a:stop", "b:stop"]
    assert app.state.singleton_tasks == []
    for task in tasks:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()


@pytest.mark.asyncio
async def test_failing_leader_start_propagates_after_tracking_prior_modules():
    log: list[str] = []

    async def bad_start(app: FastAPI) -> list[asyncio.Task]:
        log.append("bad:start")
        raise RuntimeError("boom")

    modules = [_leader_module("good", log), Module(name="bad", leader_start=bad_start)]
    app = FastAPI()

    with pytest.raises(RuntimeError, match="boom"):
        await start_leader_singletons(app, modules)

    assert log == ["good:start", "bad:start"]
    assert len(app.state.singleton_tasks) == 1
    await stop_leader_singletons(app, modules)


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_survives_failing_hook():
    log: list[str] = []

    async def bad_stop(app: FastAPI) -> None:
        raise RuntimeError("boom")

    modules = [
        Module(
            name="bad",
            leader_start=_leader_module("bad", log).leader_start,
            leader_stop=bad_stop,
        ),
        _leader_module("good", log),
    ]
    app = FastAPI()
    await start_leader_singletons(app, modules)
    # A failing leader_stop must not prevent the other modules stopping.
    await stop_leader_singletons(app, modules)
    assert "good:stop" in log
    # Second stop is a no-op.
    await stop_leader_singletons(app, modules)


@pytest.mark.asyncio
async def test_private_lifespan_runs_startup_hooks_and_skips_elector_without_leader_hooks():
    started: list[str] = []

    async def startup(app: FastAPI) -> None:
        started.append("primed")

    module = Module(name="m", register=lambda app: None, startup=startup)
    lifespan = build_private_lifespan(_PLAIN_PRIVATE, [module])

    app = FastAPI()
    async with lifespan(app):
        assert started == ["primed"]
        assert app.state.bot is None
        assert app.state.singleton_tasks == []
        # No module declares leader hooks, so no elector was created; the
        # attribute still exists (None) so readers never AttributeError.
        assert app.state.elector is None


@pytest.mark.asyncio
async def test_private_lifespan_leader_singletons_default_to_enabled(monkeypatch):
    log: list[str] = []
    app = await _run_private_lifespan(
        monkeypatch, [_leader_module("a", log)], "MONOLITH_LEADER_SINGLETONS"
    )

    assert _FakeLeaderElector.constructed == 1
    assert log == ["a:start", "a:stop"]
    assert app.state.elector is not None


@pytest.mark.asyncio
async def test_private_lifespan_false_disables_leader_singletons(monkeypatch):
    log: list[str] = []
    app = await _run_private_lifespan(
        monkeypatch, [_leader_module("a", log)], "MONOLITH_LEADER_SINGLETONS", "false"
    )

    assert _FakeLeaderElector.constructed == 0
    assert app.state.elector is None
    assert "a:start" not in log


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["FALSE", " false "])
async def test_private_lifespan_false_is_case_and_whitespace_insensitive(
    monkeypatch, value
):
    log: list[str] = []
    app = await _run_private_lifespan(
        monkeypatch, [_leader_module("a", log)], "MONOLITH_LEADER_SINGLETONS", value
    )

    assert _FakeLeaderElector.constructed == 0
    assert app.state.elector is None
    assert "a:start" not in log


@pytest.mark.asyncio
async def test_private_lifespan_unrecognized_value_fails_safe(monkeypatch):
    log: list[str] = []
    app = await _run_private_lifespan(
        monkeypatch, [_leader_module("a", log)], "MONOLITH_LEADER_SINGLETONS", "maybe"
    )

    assert _FakeLeaderElector.constructed == 1
    assert app.state.elector is not None
    assert log == ["a:start", "a:stop"]


@pytest.mark.asyncio
async def test_private_lifespan_true_enables_leader_singletons(monkeypatch):
    log: list[str] = []
    app = await _run_private_lifespan(
        monkeypatch, [_leader_module("a", log)], "MONOLITH_LEADER_SINGLETONS", "true"
    )

    assert _FakeLeaderElector.constructed == 1
    assert app.state.elector is not None
    assert log == ["a:start", "a:stop"]
