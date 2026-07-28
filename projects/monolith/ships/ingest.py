"""Supervised AISStream ingest background task.

The only always-on networked component of the ships module. Holds a websocket
to AISStream.io, parses each message via ships.ais.parse_message, batches the
results, and flushes them through the stateless persister (ships.store) straight
to Postgres. NATS is gone: this writes directly to the database.

Ported from the standalone ingest pod (projects/ships/ingest/main.py,
subscribe_to_aisstream), folded in-process. It runs inside the monolith's
FastAPI lifespan and MUST NEVER crash the app: an AISStream hiccup or a parse
error is swallowed and retried, never propagated. Only asyncio.CancelledError
(clean shutdown) is allowed through.
"""

import asyncio
import json
import logging
import os
import ssl
import time

from ships.ais import parse_message

logger = logging.getLogger("ships")

# WebSocket reconnection settings (same names/defaults as the old ingest).
INITIAL_RECONNECT_DELAY = 1.0
MAX_RECONNECT_DELAY = 60.0
RECONNECT_BACKOFF_FACTOR = 2.0

# Batching: flush when the batch reaches this many rows, or this many seconds
# have elapsed since the last flush, whichever comes first.
FLUSH_SIZE = 200
FLUSH_SECONDS = 2.0

AISSTREAM_URL_DEFAULT = "wss://stream.aisstream.io/v0/stream"
# Pacific Northwest coast, copied verbatim from the old ingest BOUNDING_BOX
# default (projects/ships/ingest/main.py).
DEFAULT_BOUNDING_BOX = "[[[46.876152, -129.552155], [51.413769, -121.213531]]]"


def _now() -> float:
    """Monotonic clock for the flush timer (never wall-clock)."""
    return time.monotonic()


async def ais_stream_loop(stop: asyncio.Event) -> None:
    """Supervised AISStream listener. Reconnects forever; never raises out.

    The only exception allowed to propagate is asyncio.CancelledError, so the
    lifespan can cancel this task cleanly on shutdown. Every other error is
    logged and retried with exponential backoff. The app can never be crashed
    by an ingest failure.
    """
    import certifi
    import websockets

    api_key = os.environ.get("AISSTREAM_API_KEY", "")
    if not api_key:
        logger.warning("AISSTREAM_API_KEY unset; ships ingest disabled")
        return
    url = os.environ.get("AISSTREAM_URL", AISSTREAM_URL_DEFAULT)
    bbox = os.environ.get("BOUNDING_BOX", DEFAULT_BOUNDING_BOX)
    # certifi CA bundle: minimal container images ship no system CA store.
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    delay = INITIAL_RECONNECT_DELAY

    while not stop.is_set():
        try:
            logger.info("ships ingest: connecting to AISStream at %s", url)
            async with websockets.connect(url, ssl=ssl_ctx) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "APIKey": api_key,
                            "BoundingBoxes": json.loads(bbox),
                            "FilterMessageTypes": [
                                "PositionReport",
                                "ShipStaticData",
                            ],
                        }
                    )
                )
                # Reset backoff once the connection (and subscription) succeed.
                delay = INITIAL_RECONNECT_DELAY
                positions: list[dict] = []
                vessels: list[dict] = []
                last_flush = _now()

                async for raw in ws:
                    if stop.is_set():
                        break
                    kind, row = parse_message(raw)
                    if kind == "position":
                        positions.append(row)
                    elif kind == "vessel":
                        vessels.append(row)
                    if (
                        len(positions) + len(vessels) >= FLUSH_SIZE
                        or _now() - last_flush > FLUSH_SECONDS
                    ):
                        await asyncio.to_thread(_flush, positions, vessels)
                        positions, vessels, last_flush = [], [], _now()

                # Flush any remainder on a clean close.
                if positions or vessels:
                    await asyncio.to_thread(_flush, positions, vessels)
        except asyncio.CancelledError:
            # Let cancellation through for a clean shutdown.
            raise
        except Exception:
            logger.exception("ships ingest: stream error")

        if not stop.is_set():
            await asyncio.sleep(delay)
            delay = min(delay * RECONNECT_BACKOFF_FACTOR, MAX_RECONNECT_DELAY)


def _flush(positions: list[dict], vessels: list[dict]) -> None:
    """Synchronous DB write in a worker thread, with a fresh Session.

    Background tasks open their own session; the request-scoped get_session is
    not available here. A flush failure is logged and swallowed so the ingest
    loop keeps running.
    """
    from sqlmodel import Session

    from core.db import get_engine
    from ships.store import persist_batch

    if not positions and not vessels:
        return
    try:
        with Session(get_engine()) as session:
            persist_batch(session, list(positions), list(vessels))
    except Exception:
        logger.exception("ships ingest: flush failed")
