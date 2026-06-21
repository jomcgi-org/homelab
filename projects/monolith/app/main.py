from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.log import configure_logging
import chat
import dr_jobs
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.db import get_engine
    from scheduler.api import purge_stale_jobs, run_scheduler_loop
    from sqlmodel import Session

    app.state.bot = None
    app.state.backfill_task = None

    # Register all scheduled jobs
    with Session(get_engine()) as session:
        from knowledge.service import on_startup as knowledge_startup

        knowledge_startup(session)
        home.on_startup_jobs(session)
        ships.on_startup_jobs(session)
        hikes.on_startup_jobs(session)
        stars.on_startup_jobs(session)
        dr_jobs.on_startup_jobs(session)
        worldcup.on_startup_jobs(session)
        knowledge.on_startup_jobs(session)

        from home.observability import rollup as observability_rollup

        observability_rollup.register(session)

    # Start Discord bot + chat jobs if configured
    bot = None
    bot_task = None
    discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if discord_token:
        from chat.bot import create_bot
        from chat.summarizer import build_llm_caller
        from chat.summarizer import on_startup as chat_startup

        bot = create_bot()
        app.state.bot = bot

        with Session(get_engine()) as session:
            chat_startup(session, bot=bot, llm_call=build_llm_caller())

        async def _start_bot_when_ready():
            await _wait_for_sidecar()
            await bot.start(discord_token)

        bot_task = asyncio.create_task(_start_bot_when_ready())
        bot_task.add_done_callback(_log_task_exception)
        logger.info("Discord bot starting")

    # Purge stale jobs from DB (e.g. removed changelog channels)
    with Session(get_engine()) as session:
        purge_stale_jobs(session)

    # Start the shared scheduler loop (replaces 4 separate asyncio tasks)
    scheduler_task = asyncio.create_task(run_scheduler_loop())
    scheduler_task.add_done_callback(_log_task_exception)

    # Start the supervised AISStream ingest listener. It reconnects forever and
    # never raises out (only CancelledError on shutdown), so it can never crash
    # the app. Disabled automatically when AISSTREAM_API_KEY is unset.
    from ships.ingest import ais_stream_loop

    app.state.ships_stop = asyncio.Event()
    app.state.ships_task = asyncio.create_task(ais_stream_loop(app.state.ships_stop))
    app.state.ships_task.add_done_callback(_log_task_exception)
    logger.info("Ships AISStream ingest started")

    # Lock sweep stays in-memory (30s, bot-coupled, already multi-pod safe via SKIP LOCKED)
    sweep_task = None
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
        logger.info("Message lock sweep started (30s interval)")

    # Prime the observability snapshots (topology + stats) once at startup so the
    # first request has data; the scheduled rollup jobs refresh them thereafter.
    # Best-effort: failures are logged and the scheduler retries (ADR 004 Layer 4).
    await prime_snapshots()

    logger.info("Monolith started")
    yield

    backfill_task = getattr(app.state, "backfill_task", None)
    if backfill_task and not backfill_task.done():
        backfill_task.cancel()
    if sweep_task:
        sweep_task.cancel()
    if bot:
        await bot.close()
    if bot_task:
        bot_task.cancel()
    scheduler_task.cancel()

    # Stop the ships ingest loop: signal it and cancel (cancel-only, matching the
    # bot/scheduler/sweep teardown above). The supervised loop re-raises
    # CancelledError on shutdown; its done callback (_log_task_exception) handles
    # it. We do not await here so the path stays robust when tests patch
    # asyncio.create_task into a non-awaitable mock.
    app.state.ships_stop.set()
    app.state.ships_task.cancel()

    if _tracer_provider is not None:
        _tracer_provider.shutdown()
    logger.info("Monolith shutting down")


import knowledge.mcp  # noqa: F401 — registers tools on shared MCP instance
import agent.mcp  # noqa: F401 — registers monolith-agent-* tools on shared MCP instance
import cluster.mcp  # noqa: F401 — registers k8s-* cluster debug tools

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
hikes.register(app)
stars.register(app)
trips.register(app)
dr_jobs.register(app)
worldcup.register(app)
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
