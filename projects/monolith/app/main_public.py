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

import dr_jobs
import hikes
import home
import knowledge
import ships
import stars
from app.log import configure_logging
from fastapi import FastAPI

configure_logging()

app = FastAPI(title="Monolith Public")

ships.register_public(app)
stars.register_public(app)
hikes.register_public(app)
dr_jobs.register_public(app)
knowledge.register_public(app)
home.register_public(app)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
