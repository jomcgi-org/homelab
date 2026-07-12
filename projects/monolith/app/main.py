"""Private monolith entrypoint: the confined monolith (all domains).

Composed by the FastMonolith framework (ADR services/010): one ``build_app``
call over the full module registry. Everything this file used to hand-author
(lifespan, leader election, MCP mount, OTel, static frontend, health) lives in
``framework/core.py``; the leader-elected singletons live with their owning
domains (``chat/leader.py``, ``ships/leader.py``).

The aliases at the bottom keep the app-level lifecycle tests
(``app/main_*_test.py``) exercising the composed surface under the names this
module always exposed; they are thin delegations, not separate logic.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.modules_private import ALL_MODULES
from framework import (
    PRIVATE_PROFILE,
    build_app,
    build_private_lifespan,
    log_task_exception,
    start_leader_singletons,
    stop_leader_singletons,
)

app = build_app(PRIVATE_PROFILE, ALL_MODULES)

# ---------------------------------------------------------------------------
# Test-facing compatibility surface (delegations onto the framework/domains).
# ---------------------------------------------------------------------------

from chat.leader import wait_for_sidecar as _wait_for_sidecar  # noqa: E402,F401
from framework import core as _framework_core  # noqa: E402

# The process-global tracer provider created by build_app (OTel enabled on the
# private profile); main_otel_test asserts it is set after module load.
_tracer_provider = _framework_core._OTEL_PROVIDER

_log_task_exception = log_task_exception

# The app-only lifespan (without the MCP session manager), as this module
# historically exposed for the lifecycle tests to drive manually.
lifespan = build_private_lifespan(PRIVATE_PROFILE, ALL_MODULES)


async def _start_singletons(app: FastAPI) -> None:
    """Start the composed leader singletons (test-facing delegation)."""
    await start_leader_singletons(app, ALL_MODULES)


async def _stop_singletons(app: FastAPI) -> None:
    """Stop the composed leader singletons (test-facing delegation)."""
    await stop_leader_singletons(app, ALL_MODULES)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
