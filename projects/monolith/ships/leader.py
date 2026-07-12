"""Ships domain leader-elected singleton: the AISStream ingest loop.

Moved verbatim from app/main.py. Runs on exactly one replica at a time (the
elected leader); the framework invokes ``leader_start`` on acquire and
``leader_stop`` on resign/shutdown (see framework/core.py).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from framework import log_task_exception

logger = logging.getLogger("monolith.ships.leader")


async def leader_start(app: FastAPI) -> list[asyncio.Task]:
    """Start the supervised AISStream ingest (reconnects forever)."""
    from ships.ingest import ais_stream_loop

    app.state.ships_stop = asyncio.Event()
    ships_task = asyncio.create_task(ais_stream_loop(app.state.ships_stop))
    ships_task.add_done_callback(log_task_exception)
    logger.info("Ships AISStream ingest started")
    return [ships_task]


async def leader_stop(app: FastAPI) -> None:
    """Signal the ingest loop to stop. Idempotent; runs on resign or shutdown."""
    ships_stop = getattr(app.state, "ships_stop", None)
    if ships_stop is not None:
        ships_stop.set()
        app.state.ships_stop = None
