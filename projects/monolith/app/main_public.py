"""Public, read-only FastAPI entrypoint.

A second, lean entrypoint that mounts ONLY the public, read-only route surface
via each domain's ``register_public`` hook. It deliberately omits everything in
``app.main`` that the public tier must never run: the Discord bot, the scheduler
loop, AIS ingest, vault clone, the ``/mcp`` mount, OTEL background setup, and the
SvelteKit static mount. There is no lifespan, so importing this module is cheap
and side-effect-free (the route-table guard test relies on that).

Phase 5a only stands up the code and a CI guard test; the public service is not
deployed here (image/chart land in Phase 5c). The knowledge public routes still
query private tables today and will 500 as ``public_reader`` until Phase 5a'.
"""

from __future__ import annotations

import logging

import artifact
import chat_public
import dr_jobs
import hikes
import home
import knowledge
import ships
import stars
import trips
import worldcup
from app.db import get_engine
from app.log import configure_logging
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlmodel import Session, text

configure_logging()

logger = logging.getLogger("monolith.public")

app = FastAPI(title="Monolith Public")

ships.register_public(app)
stars.register_public(app)
trips.register_public(app)
hikes.register_public(app)
dr_jobs.register_public(app)
worldcup.register_public(app)
knowledge.register_public(app)
home.register_public(app)
chat_public.register_public(app)
artifact.register_public(app)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/health")
def api_health():
    """Deep health for the public tier, reached via the frontend /health proxy.

    The backend is never internet-reachable, so the externally assertable signal
    is jomcgi.dev/health, which proxies here in-cluster. Unlike /healthz (process
    up only), this runs SELECT 1 to confirm the read replica is reachable and the
    public_reader role can execute a query, the class of failure that left the
    process green while /app/dr-jobs 503'd. Returns a clean 503 rather than
    raising, so a DB outage logs once per probe instead of a traceback, and the
    frontend gets a machine-readable status either way.
    """
    try:
        with Session(get_engine()) as session:
            session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("public health check failed")
        return JSONResponse({"status": "unhealthy"}, status_code=503)
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
