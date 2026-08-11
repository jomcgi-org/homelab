"""FastMonolith core: ``Tier``, ``Profile``, ``Module``, and ``build_app``.

This module owns everything ``app/main.py`` used to hand-author once per
entrypoint: the FastAPI app, the lifespan, leader-elected singletons, the MCP
mount, OpenTelemetry setup, the static frontend mount, and the health
endpoints. Entrypoints reduce to one ``build_app`` call over a module list.

Import discipline: this file must stay importable inside the pruned PUBLIC
binary, whose file set excludes the private domains and ``app/mcp_app.py``.
Anything private-tier-only (MCP instance, OTel, leadership, DB engine) is
imported lazily inside the code paths that a public profile never takes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI

from core.log import configure_logging

if TYPE_CHECKING:
    pass

logger = logging.getLogger("monolith.framework")

# Sentinel: the private tier may hold any secret the deployment injects. The
# real control is runtime injection (ADR 010: the runtime capability set, not
# artifact contents, is the boundary); this set exists so a PUBLIC profile can
# fail fast when handed a module that declares a secret dependency.
ALL_SECRETS: frozenset[str] = frozenset({"*"})

RegisterHook = Callable[[FastAPI], None]
StartupHook = Callable[[FastAPI], Awaitable[None]]
LeaderStartHook = Callable[[FastAPI], Awaitable["list[asyncio.Task]"]]
LeaderStopHook = Callable[[FastAPI], Awaitable[None]]
HealthCheck = Callable[[], Awaitable[dict]]


class Tier(enum.Enum):
    """Which runtime security context a binary (or module surface) targets."""

    PUBLIC = "public"
    PRIVATE = "private"


@dataclasses.dataclass(frozen=True)
class Profile:
    """What a composed binary's runtime is allowed to do.

    The profile is a description of capabilities, not the enforcement: the
    deployment enforces the boundary by what it injects (secrets, DB role).
    ``build_app`` uses the profile to fail fast on impossible compositions and
    to decide which cross-cutting concerns (MCP, OTel, leader singletons,
    static frontend) to wire up.
    """

    tier: Tier
    title: str
    service_name: str
    allowed_secrets: frozenset[str] = ALL_SECRETS
    mcp_enabled: bool = False
    otel_enabled: bool = False
    static_frontend: bool = False
    deep_health: bool = False
    leader_singletons: bool = False
    # Scopes the Postgres leader election. Differently-composed binaries must
    # not share a lease, or whichever process wins silently benches the
    # other's singletons; the confined monolith keeps the historical key.
    leader_lease_key: str = "singletons"


PRIVATE_PROFILE = Profile(
    tier=Tier.PRIVATE,
    title="Monolith",
    service_name="monolith-backend",
    allowed_secrets=ALL_SECRETS,
    mcp_enabled=True,
    otel_enabled=True,
    static_frontend=True,
    # The confined monolith serves the deep /api/health route too. It used to
    # opt out, which silently made every private-tier register_health component
    # dead code: the component composes fine and the endpoint that would run it
    # does not exist (the cd component shipped in #4599 and never executed).
    # A private tier serving this route is already established and tested,
    # since domain_profile() below has always set it.
    deep_health=True,
    leader_singletons=True,
)

PUBLIC_PROFILE = Profile(
    tier=Tier.PUBLIC,
    title="Monolith Public",
    service_name="monolith-public",
    allowed_secrets=frozenset(),
    mcp_enabled=False,
    otel_enabled=False,
    static_frontend=False,
    deep_health=True,
    leader_singletons=False,
)


def domain_profile(name: str) -> Profile:
    """Private-tier profile for a single domain composed standalone.

    A standalone domain binary runs its own leader singletons and exposes an
    MCP server carrying just its tools (when it registers any), reports traces
    under its own service name, and serves no static frontend (the SvelteKit
    bundle belongs to the confined monolith's deployment).
    """
    return Profile(
        tier=Tier.PRIVATE,
        title=f"Monolith ({name})",
        service_name=f"monolith-{name}",
        allowed_secrets=ALL_SECRETS,
        mcp_enabled=True,
        otel_enabled=True,
        static_frontend=False,
        deep_health=True,
        leader_singletons=True,
        leader_lease_key=f"singletons.{name}",
    )


@dataclasses.dataclass(frozen=True)
class Module:
    """What a domain contributes to a composed binary.

    All callables are optional so thin domains stay thin: an MCP-only domain
    sets just ``register_mcp``; a public-only domain sets just
    ``register_public``. Hooks:

    - ``register``: mount the full private route surface.
    - ``register_public``: mount the public read-only route surface.
    - ``register_mcp``: register the domain's MCP tools (typically a
      side-effect import of the domain's ``@mcp.tool`` module).
    - ``startup``: awaited on every replica during private lifespan startup.
    - ``leader_start`` / ``leader_stop``: leader-elected singletons; start
      returns the background tasks it spawned so the framework can track and
      cancel them. The hook must attach ``log_task_exception`` as a
      done-callback on each task it creates (at ``create_task`` time, so a
      crash before the hook returns is still logged).
    - ``register_health``: optional ``{name: async check}`` components folded
      into the deep ``/api/health`` response (see ``_add_health``). A check
      returns ``{"ok": bool, "detail": str}``; a crashing check is caught by
      the framework and reported not-ok rather than 500ing the whole handler.
    - ``requires_secrets``: secret env names the module's PRIVATE surface
      uses (leader hooks, MCP, write routers). A restricted private profile
      refuses to compose such a module; the public tier ignores it because it
      composes none of those surfaces.
    """

    name: str
    register: RegisterHook | None = None
    register_public: RegisterHook | None = None
    register_mcp: Callable[[], None] | None = None
    startup: StartupHook | None = None
    leader_start: LeaderStartHook | None = None
    leader_stop: LeaderStopHook | None = None
    register_health: dict[str, HealthCheck] | None = None
    requires_secrets: frozenset[str] = frozenset()


def log_task_exception(task: "asyncio.Task[object]") -> None:
    """Log unhandled exceptions from background tasks instead of dropping them."""
    if not task.cancelled() and task.exception():
        logger.error(
            "Background task %s failed", task.get_name(), exc_info=task.exception()
        )


def _validate(profile: Profile, modules: Sequence[Module]) -> None:
    """Fail fast on impossible compositions (raises ValueError)."""
    names = [m.name for m in modules]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate module names in composition: {names}")
    for m in modules:
        if profile.tier is Tier.PUBLIC:
            if m.register_public is None:
                raise ValueError(
                    f"module {m.name!r} has no public surface; it cannot be "
                    "composed into a PUBLIC-tier binary"
                )
            # requires_secrets describes the module's PRIVATE surface (leader
            # hooks, MCP, write routers); the public tier composes none of
            # those, so a both-tier module's secret declaration does not block
            # public composition. The public tier's guarantee is structural.
        else:
            # A restricted PRIVATE profile must fail fast rather than boot
            # with a module whose declared secrets it forbids.
            if m.requires_secrets and profile.allowed_secrets != ALL_SECRETS:
                missing = m.requires_secrets - profile.allowed_secrets
                if missing:
                    raise ValueError(
                        f"module {m.name!r} requires secrets {sorted(missing)} "
                        f"not allowed by profile {profile.service_name!r}"
                    )
            if (
                m.register is None
                and m.register_mcp is None
                and m.startup is None
                and m.leader_start is None
            ):
                raise ValueError(
                    f"module {m.name!r} contributes nothing to a PRIVATE-tier "
                    "binary (no register/register_mcp/startup/leader_start)"
                )


async def start_leader_singletons(app: FastAPI, modules: Sequence[Module]) -> None:
    """Start every composed module's leader-elected singletons.

    Invoked only on the elected leader replica. Tasks returned by each
    module's ``leader_start`` are tracked on ``app.state.singleton_tasks`` so
    ``stop_leader_singletons`` can cancel them on resign or shutdown. Tracking
    is incremental (the state list is extended after each hook) so an
    exception partway through the module sequence still leaves the
    already-started modules' tasks cancellable. Hooks attach their own
    ``log_task_exception`` done-callbacks at ``create_task`` time, so a task
    that crashes before its hook returns is still logged.
    """
    tasks: list[asyncio.Task] = list(getattr(app.state, "singleton_tasks", []))
    app.state.singleton_tasks = tasks
    for m in modules:
        if m.leader_start is None:
            continue
        tasks.extend(await m.leader_start(app))


async def stop_leader_singletons(app: FastAPI, modules: Sequence[Module]) -> None:
    """Stop every composed module's singletons. Idempotent (resign + shutdown)."""
    for m in modules:
        if m.leader_stop is None:
            continue
        try:
            await m.leader_stop(app)
        except Exception:
            logger.exception("leader_stop for module %s failed", m.name)
    for task in getattr(app.state, "singleton_tasks", []):
        task.cancel()  # cancelling an already-done task is a safe no-op
    app.state.singleton_tasks = []


def _add_health(app: FastAPI, profile: Profile, modules: Sequence[Module]) -> None:
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    if not profile.deep_health:
        return

    from fastapi.responses import JSONResponse

    # Preserve the public tier's historical log identity: SigNoz alerts and
    # runbooks key on logger "monolith.public" + "public health check failed"
    # (the read-replica/public_reader outage signal). Other tiers log under
    # the framework logger.
    if profile.tier is Tier.PUBLIC:
        health_logger = logging.getLogger("monolith.public")
        health_message = "public health check failed"
        component_message = "public health components unhealthy"
    else:
        health_logger = logger
        health_message = "deep health check failed"
        component_message = "deep health components unhealthy"

    component_checks: dict[str, HealthCheck] = {}
    for m in modules:
        if m.register_health:
            component_checks.update(m.register_health)

    async def _run_component(name: str, check: HealthCheck) -> dict:
        try:  # nosemgrep: no-broad-except-swallow - logged via health_logger below
            return await check()
        except Exception as exc:  # noqa: BLE001 - a crashing check is itself a finding
            health_logger.exception("health component %s raised", name)
            return {"ok": False, "detail": str(exc)}

    @app.get("/api/health")
    async def api_health():
        """Deep health: SELECT 1 proves the DB endpoint + role actually work.

        On the public tier the backend is never internet-reachable, so the
        externally assertable signal is the frontend /health proxy reaching
        this route; a DB outage returns a clean 503 (logged once per probe)
        instead of a traceback. Module-contributed components (see
        ``Module.register_health``) run alongside SELECT 1 and are folded
        into a ``components`` map; any component not ok makes the whole
        response 503, same as the SELECT 1 baseline.
        """
        from sqlmodel import Session, text

        from core.db import get_engine

        db_ok = True
        try:  # nosemgrep: no-broad-except-swallow - logged via health_logger below
            with Session(get_engine()) as session:
                session.execute(text("SELECT 1"))
        except Exception:
            health_logger.exception(health_message)
            db_ok = False

        components: dict[str, dict] = {}
        if component_checks:
            names = list(component_checks)
            results = await asyncio.gather(
                *(_run_component(name, component_checks[name]) for name in names)
            )
            components = dict(zip(names, results))

        all_ok = db_ok and all(c.get("ok") for c in components.values())
        # Ember checks return {"ok": False, detail} in-band and never raise
        # (see ember_public/synthetic_probe.py), so _run_component's exception
        # log never fires for them and a component-caused 503 was silent. This
        # warning is the only server-side record of which component tripped.
        if not all_ok:
            failing = {n: c for n, c in components.items() if not c.get("ok")}
            if failing:
                health_logger.warning(
                    "%s: %s",
                    component_message,
                    "; ".join(
                        f"{n}={c.get('detail') or 'no detail'}"
                        for n, c in sorted(failing.items())
                    ),
                )
        body = {"status": "ok" if all_ok else "unhealthy"}
        if components:
            body["components"] = components
        if not all_ok:
            return JSONResponse(body, status_code=503)
        return body


def _mount_static_frontend(app: FastAPI) -> None:
    """Serve the SvelteKit static bundle (must mount after API routes)."""
    from fastapi.staticfiles import StaticFiles

    static_dir = os.environ.get(
        "STATIC_DIR",
        str(Path(__file__).resolve().parents[1] / "frontend" / "dist"),
    )
    if Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
        logger.info("Serving frontend from %s", static_dir)


# OTel globals are process-wide (tracer provider, httpx instrumentation), so
# guard them: the first build_app call in a process wins the service name.
# Per-app FastAPI instrumentation still runs for every composed app.
_OTEL_PROVIDER = None


def _setup_otel(app: FastAPI, service_name: str):
    """Instrument the app; return the tracer provider for lifespan shutdown."""
    global _OTEL_PROVIDER

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if _OTEL_PROVIDER is None:
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name}),
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""),
                )
            )
        )
        trace.set_tracer_provider(provider)
        # Instrument outbound httpx calls (embedding queries, LLM calls) so slow
        # RAG/chat paths show a child span for the HTTP leg instead of being an
        # opaque multi-second server span. Process-global: do it once.
        HTTPXClientInstrumentor().instrument()
        _OTEL_PROVIDER = provider

    FastAPIInstrumentor.instrument_app(app)
    logger.info("OpenTelemetry instrumentation enabled")
    return _OTEL_PROVIDER


def build_private_lifespan(profile: Profile, modules: Sequence[Module]):
    """The private-tier lifespan: startup hooks + leader-elected singletons.

    Public (rather than an inline closure) so the app-level lifecycle tests
    can drive the lifespan directly against a composed module list.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.bot = None
        app.state.backfill_task = None
        app.state.singleton_tasks = []

        # Per-module startup hooks run on every replica (best-effort priming;
        # scheduled Argo CronWorkflows own the refresh cadence thereafter).
        for m in modules:
            if m.startup is not None:
                await m.startup(app)

        # Background singletons (Discord bot, AIS ingest, lock sweep) run on
        # ONLY ONE replica at a time, elected via a Postgres heartbeat lease,
        # so the web tier can scale horizontally without N duplicate
        # bots/streams. Followers just serve HTTP. See app/leadership.py.
        app.state.elector = None
        elector_task = None
        # A second deployment fed by a copy of prod's database (the standing dev
        # environment) must start NONE of these. Two of them act on queue state
        # the copy brings with it: the chat outbox drain would post prod's
        # queued messages to Discord a second time, and DBOS recovers pending
        # workflows on launch, so dev would resume prod's in-flight agent runs.
        #
        # The gate stops the elector being CONSTRUCTED rather than stopping its
        # callbacks. The lease is scoped by profile.leader_lease_key, not by
        # database, so a dev instance that takes the lease benches prod's
        # leader: the mute would cause the outage it exists to prevent.
        #
        # Unset and unrecognised both mean enabled, so a typo cannot silently
        # stop prod's bot while every probe still reports healthy.
        leader_singletons_config = os.environ.get("MONOLITH_LEADER_SINGLETONS")
        leader_singletons_enabled = leader_singletons_config is None or (
            leader_singletons_config.strip().lower() not in {"false", "0", "no"}
        )
        leader_singletons_available = profile.leader_singletons and any(
            m.leader_start for m in modules
        )
        if leader_singletons_available and not leader_singletons_enabled:
            logger.info(
                "Monolith started (leader singletons disabled by configuration)"
            )
        elif leader_singletons_available:
            from core.leadership import LeaderElector

            elector = LeaderElector(lease_key=profile.leader_lease_key)
            app.state.elector = elector
            elector_task = asyncio.create_task(
                elector.run(
                    on_acquire=lambda: start_leader_singletons(app, modules),
                    on_resign=lambda: stop_leader_singletons(app, modules),
                )
            )
            elector_task.add_done_callback(log_task_exception)
            logger.info("Monolith started (leader election active)")
        else:
            logger.info("Monolith started")
        yield

        if elector_task is not None:
            elector_task.cancel()
        await stop_leader_singletons(app, modules)
        backfill_task = getattr(app.state, "backfill_task", None)
        if backfill_task and not backfill_task.done():
            backfill_task.cancel()

        if _OTEL_PROVIDER is not None:
            _OTEL_PROVIDER.shutdown()
        logger.info("Monolith shutting down")

    return lifespan


def build_app(profile: Profile, modules: Sequence[Module]) -> FastAPI:
    """Compose ``modules`` into a FastAPI app for ``profile``. The one root.

    PUBLIC tier keeps its historical shape exactly: no lifespan side effects,
    no MCP, no OTel, no static mount; importing a public entrypoint stays
    cheap and side-effect-free (the route-table guard test relies on that).
    """
    _validate(profile, modules)
    configure_logging()

    if profile.tier is Tier.PUBLIC:
        app = FastAPI(title=profile.title)
        for m in modules:
            m.register_public(app)
        _add_health(app, profile, modules)
        return app

    # ---- PRIVATE tier ----
    lifespan = build_private_lifespan(profile, modules)

    mcp_http_app = None
    if profile.mcp_enabled:
        # The shared FastMCP instance lives in app/mcp_app.py (excluded from
        # the public file set); domain tool modules attach to it on import.
        # Mounted whenever the profile enables MCP, even if no composed module
        # registers tools, preserving the invariant that the private tier
        # always serves /mcp (an empty tool list is a visible symptom; a
        # missing mount looks like an unrelated 404 at the gateway).
        from core.mcp_app import mcp as monolith_mcp

        for m in modules:
            if m.register_mcp is not None:
                m.register_mcp()
        mcp_http_app = monolith_mcp.http_app(transport="sse", path="/")

    if mcp_http_app is not None:
        app_lifespan = lifespan

        @asynccontextmanager
        async def combined_lifespan(app: FastAPI):
            async with app_lifespan(app):
                async with mcp_http_app.lifespan(app):
                    yield

        lifespan = combined_lifespan

    app = FastAPI(title=profile.title, lifespan=lifespan)

    for m in modules:
        if m.register is not None:
            m.register(app)

    if mcp_http_app is not None:
        app.mount("/mcp", mcp_http_app)

    _add_health(app, profile, modules)

    if profile.static_frontend:
        _mount_static_frontend(app)

    if profile.otel_enabled:
        _setup_otel(app, profile.service_name)

    return app
