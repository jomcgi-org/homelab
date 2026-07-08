from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.log import configure_logging
import artifact
import campsites
import chat
import demos
import dr_jobs
import grimoire
import hikes
import home
import knowledge
import scheduler
import ships
import stars
import trips
import worldcup
from home.observability.rollup import prime_snapshots

configure_logging()
logger = logging.getLogger("monolith.main")


async def _wait_for_sidecar() -> None:
    """Block until the frontend sidecar is healthy, or return immediately if unconfigured."""
    url = os.environ.get("FRONTEND_HEALTH_URL", "")
    if not url:
        return
    import httpx

    logger.info("Waiting for frontend sidecar at %s", url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        while True:
            try:
                resp = await client.get(url, timeout=2)
                if resp.status_code < 500:
                    logger.info("Frontend sidecar is ready")
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)


def _log_task_exception(task: "asyncio.Task[object]") -> None:
    """Log unhandled exceptions from background tasks instead of silently dropping them."""
    if not task.cancelled() and task.exception():
        logger.error(
            "Background task %s failed", task.get_name(), exc_info=task.exception()
        )


async def _start_singletons(app: FastAPI) -> None:
    """Start the background singletons. Invoked only on the elected leader replica.

    These (Discord bot, Discord outbox drain, AIS ingest, bot-coupled lock
    sweep) must run on exactly one replica, so they live behind leader election
    rather than starting unconditionally in the lifespan.
    """
    from app.db import get_engine
    from sqlmodel import Session

    tasks: list[asyncio.Task] = []

    # Discord bot + chat jobs if configured.
    bot = None
    discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if discord_token:
        from chat.acl import bootstrap_defaults
        from chat.bot import create_bot
        from chat.summarizer import build_llm_caller
        from chat.summarizer import on_startup as chat_startup

        # Idempotently seed the default Discord feature grants (ADR 029) before
        # the bot accepts commands, so the home server + owner work out of the
        # box. Non-fatal: a failed seed (e.g. DB briefly unreachable) leaves the
        # ACL fail-closed and re-seeds on the next restart, rather than taking
        # down the bot.
        try:
            bootstrap_defaults()
        except Exception:
            logger.exception("acl: failed to seed default feature grants; continuing")

        bot = create_bot()
        app.state.bot = bot
        with Session(get_engine()) as session:
            chat_startup(session, bot=bot, llm_call=build_llm_caller())

        async def _start_bot_when_ready():
            await _wait_for_sidecar()
            await bot.start(discord_token)

        bot_task = asyncio.create_task(_start_bot_when_ready())
        bot_task.add_done_callback(_log_task_exception)
        tasks.append(bot_task)
        logger.info("Discord bot starting")

        # Leader-only Discord outbox drain: posts rows enqueued by any replica
        # or Argo job (notify, changelog) through this pod's bot connection.
        from chat.outbox import run_outbox_drain

        drain_task = asyncio.create_task(run_outbox_drain(bot, get_engine()))
        drain_task.add_done_callback(_log_task_exception)
        tasks.append(drain_task)
        logger.info("Discord outbox drain starting")

        # Reclaim goosecracker agent turns orphaned by the prior owner's death
        # (this replica just became leader, so any turn still marked running was
        # owned by a process that is gone). Re-dispatches them so queued replies
        # do not wedge forever on ⏳. One-shot; non-fatal so a sweep failure never
        # blocks startup. Runs off the loop (sync DB work) via to_thread.
        try:
            from chat.api import reclaim_orphaned_agent_sessions

            reclaimed = await asyncio.to_thread(reclaim_orphaned_agent_sessions)
            if reclaimed:
                logger.info("Reclaimed %d orphaned goosecracker turn(s)", reclaimed)
        except Exception:
            logger.exception("goosecracker: orphaned-turn reclaim sweep failed")

    # Supervised AISStream ingest (reconnects forever; CancelledError on stop).
    from ships.ingest import ais_stream_loop

    app.state.ships_stop = asyncio.Event()
    ships_task = asyncio.create_task(ais_stream_loop(app.state.ships_stop))
    ships_task.add_done_callback(_log_task_exception)
    tasks.append(ships_task)
    logger.info("Ships AISStream ingest started")

    # Bot-coupled lock sweep (reclaims expired message locks via SKIP LOCKED).
    if discord_token and bot:

        async def _lock_sweep_loop():
            from shared.embedding import EmbeddingClient
            from chat.store import MessageStore

            embed_client = EmbeddingClient()
            while not bot.is_ready():
                await asyncio.sleep(2)
            while True:
                await asyncio.sleep(30)
                try:
                    with Session(get_engine()) as session:
                        store = MessageStore(session=session, embed_client=embed_client)
                        expired = store.reclaim_expired(ttl_seconds=30, limit=5)
                        for lock in expired:
                            logger.info(
                                "Reclaiming expired lock for message %s",
                                lock.discord_message_id,
                            )
                            await bot.reprocess_message(
                                lock.discord_message_id, lock.channel_id
                            )
                        cleaned = store.cleanup_completed(max_age_seconds=3600)
                        if cleaned:
                            logger.debug("Cleaned up %d completed locks", cleaned)
                except Exception:
                    logger.exception("Lock sweep failed")

        sweep_task = asyncio.create_task(_lock_sweep_loop())
        sweep_task.add_done_callback(_log_task_exception)
        tasks.append(sweep_task)
        logger.info("Message lock sweep started (30s interval)")

    app.state.singleton_tasks = tasks


async def _stop_singletons(app: FastAPI) -> None:
    """Stop the background singletons. Idempotent; runs on resign or shutdown."""
    bot = getattr(app.state, "bot", None)
    if bot is not None:
        try:
            await bot.close()
        except Exception:
            logger.exception("Discord bot close failed")
        app.state.bot = None

    ships_stop = getattr(app.state, "ships_stop", None)
    if ships_stop is not None:
        ships_stop.set()
        app.state.ships_stop = None

    for task in getattr(app.state, "singleton_tasks", []):
        task.cancel()  # cancelling an already-done task is a safe no-op
    app.state.singleton_tasks = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.bot = None
    app.state.backfill_task = None

    # All formerly in-process scheduled jobs now run as Argo CronWorkflows in the
    # monolith-workflows namespace (one-flag cutover via jobs.cronWorkflows). The
    # in-process scheduler dispatch loop and its register_job startup hooks were
    # removed, so the monolith is purely orchestration + APIs - no batch work on
    # the request-serving pods.

    # Prime the observability snapshots (topology + stats) once at startup so the
    # first request has data; the scheduled rollup jobs refresh them thereafter.
    # Runs on every replica (best-effort; the scheduler rollup refreshes later).
    await prime_snapshots()

    # Background singletons (Discord bot, scheduler loop, AIS ingest, lock sweep)
    # run on ONLY ONE replica at a time, elected via a Postgres heartbeat lease,
    # so the web tier can scale horizontally without N duplicate bots/streams.
    # Followers just serve HTTP. See app/leadership.py.
    from app.leadership import LeaderElector

    elector = LeaderElector()
    app.state.elector = elector
    app.state.singleton_tasks = []
    elector_task = asyncio.create_task(
        elector.run(
            on_acquire=lambda: _start_singletons(app),
            on_resign=lambda: _stop_singletons(app),
        )
    )
    elector_task.add_done_callback(_log_task_exception)

    logger.info("Monolith started (leader election active)")
    yield

    elector_task.cancel()
    await _stop_singletons(app)
    backfill_task = getattr(app.state, "backfill_task", None)
    if backfill_task and not backfill_task.done():
        backfill_task.cancel()

    if _tracer_provider is not None:
        _tracer_provider.shutdown()
    logger.info("Monolith shutting down")


import knowledge.mcp  # noqa: F401 — registers tools on shared MCP instance
import agent.mcp  # noqa: F401 — registers monolith-agent-* tools on shared MCP instance
import cluster.mcp  # noqa: F401 — registers k8s-* cluster debug tools
import semgrep.mcp  # noqa: F401  registers the semgrep_scan tool on shared MCP instance
import sandbox.mcp  # noqa: F401  registers the run_python tool on shared MCP instance

from app.mcp_app import mcp as monolith_mcp

_mcp_app = monolith_mcp.http_app(transport="sse", path="/")


@asynccontextmanager
async def _combined_lifespan(app: FastAPI):
    async with lifespan(app):
        async with _mcp_app.lifespan(app):
            yield


app = FastAPI(title="Monolith", lifespan=_combined_lifespan)

home.register(app)
chat.register(app)
knowledge.register(app)
scheduler.register(app)
ships.register(app)
grimoire.register(app)
hikes.register(app)
stars.register(app)
trips.register(app)
dr_jobs.register(app)
campsites.register(app)
worldcup.register(app)
artifact.register(app)
demos.register(app)
app.mount("/mcp", _mcp_app)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# Serve SvelteKit static frontend (must be after API routes)
_static_dir = os.environ.get(
    "STATIC_DIR",
    str(Path(__file__).resolve().parent.parent / "frontend" / "dist"),
)
if Path(_static_dir).is_dir():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="frontend")
    logger.info("Serving frontend from %s", _static_dir)

# OTEL instrumentation (manual setup -- operator auto-inject breaks Bazel runfiles)
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_tracer_provider = TracerProvider(
    resource=Resource.create({"service.name": "monolith-backend"}),
)
_tracer_provider.add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(
            endpoint=os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""),
        )
    )
)
trace.set_tracer_provider(_tracer_provider)
FastAPIInstrumentor.instrument_app(app)
# Instrument outbound httpx calls (embedding queries to inference-embeddings,
# LLM calls to inference) so slow RAG/chat paths show a child span for the HTTP
# leg instead of being an opaque multi-second server span.
HTTPXClientInstrumentor().instrument()
logger.info("OpenTelemetry instrumentation enabled")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
