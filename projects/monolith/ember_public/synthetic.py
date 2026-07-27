"""Public read-side access to the ember synthetic probe latch."""

from __future__ import annotations

import asyncio
import logging

from sqlmodel import Session

from app.db import get_engine
from ember_public.synthetic_models import EmberSyntheticProbe

logger = logging.getLogger(__name__)


def _read_probe_sync(demo: str) -> EmberSyntheticProbe | None:
    with Session(get_engine()) as session:
        return session.get(EmberSyntheticProbe, demo)


async def read_probe(demo: str) -> EmberSyntheticProbe | None:
    """Read one probe row by demo name using the default public_reader engine.

    The synchronous helper opens its own Session(get_engine()) in a worker
    thread. Missing table during the pre-migration rollout is deliberately
    treated as no row, rather than breaking the health endpoint.
    """
    # The broad except means a genuine DB outage does not surface through
    # these health components. That is intentional: framework/core.py's own
    # SELECT 1 baseline already 503s the whole endpoint on a DB outage, and
    # reporting it again here would only add noise.
    try:
        return await asyncio.to_thread(_read_probe_sync, demo)
    except Exception as exc:  # noqa: BLE001 - missing pre-migration table is expected
        logger.warning("synthetic probe read failed for %s: %s", demo, exc)
        return None
