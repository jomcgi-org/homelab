"""Application entrypoint.

Each feature domain lives in its own top-level package and exposes a
``register(app)`` function that mounts its router. create_app() wires the domains
together. To add a domain, import it and call its register() below, following the
`health` example.
"""

from __future__ import annotations

from fastapi import FastAPI

import health


def create_app() -> FastAPI:
    app = FastAPI(title="monolith")
    health.register(app)
    # Register additional domains here.
    return app


app = create_app()
