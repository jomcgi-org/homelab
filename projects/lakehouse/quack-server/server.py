"""Quack serving pod: hot-swap consumer + minimal stateless HTTP query API.

ADR platform/004 §Hot-swap mechanism / §Serving artifact lifecycle / §Security.

Lifecycle
---------
* **Startup** — :func:`build_connection` opens a DuckDB connection wired for the
  SeaweedFS lakehouse (``duckdb_query.connect`` loads httpfs/iceberg/vss and the
  S3 secret). If ``SERVING_ARTIFACT_URL`` is set, that artifact is ATTACHed as the
  serving schema so the pod can answer queries before the first swap event.
* **Hot-swap consumer** — :func:`run_swap_consumer` is a durable NATS pull
  subscriber on ``events.serving.artifact-ready``. On each event it parses the
  artifact path/version (ADR-017 envelope ``payload``) and issues
  ``ATTACH OR REPLACE`` via :func:`duckdb_query.attach_or_replace_sql`. On
  DuckDB 1.5.3 this is a ~2ms non-blocking swap: in-flight queries finish against
  the old snapshot, new queries see the new artifact (verified zero-downtime).
* **HTTP API** — ``GET /healthz`` (liveness + current artifact version) and
  ``POST /search`` (VSS nearest-neighbour query). Cloudflare CDN fronts this API.

Security (platform/004 §Security)
---------------------------------
The hot-swap is **not** a public query. In the ADR the swap SQL travels over
Quack's privileged query path with an auth token scoped to the builder worker
pool. Here the swap is driven by a *durable NATS consumer* the pod owns (only the
builder publishes ``artifact-ready``, NATS is internal-only), so the swap never
crosses the public HTTP boundary at all — the privileged-token requirement is
satisfied by topology rather than by an HTTP token. The public ``POST /search``
path is gated by ``QUACK_QUERY_TOKEN`` (a per-deployment bearer token; the
monolith issues per-client tokens for the web app's read path).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

import duckdb
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from projects.lakehouse import duckdb_query
from projects.lakehouse.nats_client import NatsClient

logger = logging.getLogger(__name__)

# NATS subject the builder workflow publishes a new serving artifact on
# (platform/004 §Write path: BuildServingArtifactWorkflow -> artifact-ready).
ARTIFACT_READY_SUBJECT = "events.serving.artifact-ready"

# Durable consumer name. A durable (consumer-group) name means a restarted pod
# resumes from where it left off rather than replaying the whole stream; every
# pod uses the same durable so each gets the swap event (JetStream fan-out).
SWAP_DURABLE = "quack-serving-swap"

# Schema alias the serving artifact is ATTACHed under. Queries reference
# ``<alias>.<table>`` (e.g. ``notes.chunks``); the swap rebinds the alias.
SERVING_ALIAS = "notes"

# Default table the /search endpoint runs VSS over inside the artifact.
DEFAULT_SEARCH_TABLE = f"{SERVING_ALIAS}.chunks"

# Clamp on /search result count (defence against pathological LIMITs).
_MAX_SEARCH_K = 200


# --------------------------------------------------------------------------- #
# Artifact-ready event parsing (pure — unit-tested)
# --------------------------------------------------------------------------- #


def parse_artifact_ready(raw: bytes) -> tuple[str, str | None]:
    """Parse an ``events.serving.artifact-ready`` message into ``(path, version)``.

    The message body is an ADR-017 :class:`EventEnvelope` JSON document. The
    serving artifact location lives in ``payload``; this reads it tolerantly
    (additive-schema rules) accepting either ``artifact_url`` or ``path`` for the
    S3 URI and ``version`` for the optional human version tag.

    Pure: no I/O, no DuckDB. Raises :class:`ValueError` if no artifact path is
    present so the consumer can ``term`` (not redeliver) a malformed message.
    """
    doc = json.loads(raw)
    payload = doc.get("payload", {}) if isinstance(doc, dict) else {}
    if not isinstance(payload, dict):
        raise ValueError("artifact-ready payload is not an object")

    path = payload.get("artifact_url") or payload.get("path")
    if not path or not isinstance(path, str):
        raise ValueError("artifact-ready event has no artifact_url/path")

    version = payload.get("version")
    if version is not None:
        version = str(version)
    return path, version


# --------------------------------------------------------------------------- #
# Serving connection state
# --------------------------------------------------------------------------- #


class ServingState:
    """Holds the live DuckDB connection and the current artifact version.

    A single connection is shared across requests and the swap consumer.
    ``ATTACH OR REPLACE`` is non-blocking on DuckDB 1.5.3, so the swap does not
    need to coordinate with in-flight queries (each finishes on its starting
    snapshot). ``version`` is advisory metadata surfaced by ``/healthz``.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection, *, version: str | None = None):
        self.con = con
        self.version = version

    def swap(self, path: str, version: str | None) -> None:
        """Apply ``ATTACH OR REPLACE`` for ``path`` and record ``version``."""
        sql = duckdb_query.attach_or_replace_sql(SERVING_ALIAS, path)
        self.con.execute(sql)
        self.version = version
        logger.info("hot-swapped serving artifact: path=%s version=%s", path, version)

    def search(self, query_vector: list[float], k: int) -> list[dict[str, Any]]:
        """Run a VSS nearest-neighbour query, returning ``k`` rows as dicts."""
        sql = duckdb_query.vector_search_sql(DEFAULT_SEARCH_TABLE, k)
        cur = self.con.execute(sql, {"query": query_vector})
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def build_connection(env: Mapping[str, str] | None = None) -> ServingState:
    """Open the serving connection and ATTACH the initial artifact if configured.

    Reads ``SERVING_ARTIFACT_URL`` (optional). Touches the network (extension
    install + S3) so it is NOT exercised by hermetic tests — those build a
    :class:`ServingState` over an in-memory ``duckdb.connect(':memory:')``.
    """
    env = os.environ if env is None else env
    artifact = env.get("SERVING_ARTIFACT_URL") or None
    con = duckdb_query.connect(read_only_artifact=artifact, env=env)
    return ServingState(con, version=artifact)


# --------------------------------------------------------------------------- #
# Hot-swap consumer
# --------------------------------------------------------------------------- #


async def run_swap_consumer(
    state: ServingState,
    client: NatsClient,
    *,
    stop: asyncio.Event | None = None,
    poll_timeout: float = 5.0,
) -> None:
    """Durable NATS pull loop applying ``artifact-ready`` events as hot-swaps.

    Subscribes to :data:`ARTIFACT_READY_SUBJECT` with durable
    :data:`SWAP_DURABLE`, then fetches in a loop. Each message is parsed
    (:func:`parse_artifact_ready`) and applied (:meth:`ServingState.swap`).
    Messages that swap successfully are ``ack``-ed; malformed messages are
    ``term``-ed (won't redeliver); a failed swap is left for redelivery.

    ``stop`` lets the FastAPI shutdown hook (or a test) break the loop. Fetch
    timeouts are normal (idle stream) and simply re-poll.
    """
    sub = await client.pull_subscribe(ARTIFACT_READY_SUBJECT, SWAP_DURABLE)
    while stop is None or not stop.is_set():
        try:
            msgs = await sub.fetch(timeout=poll_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            continue  # idle stream — re-poll
        for msg in msgs:
            await _apply_swap_message(state, msg)


async def _apply_swap_message(state: ServingState, msg: Any) -> None:
    """Apply one ``artifact-ready`` message: parse, swap, then ack.

    Disposition:
      * malformed payload -> ``term`` (won't redeliver — replaying it never helps);
      * swap failure (e.g. S3 hiccup) -> neither ack nor term, so JetStream
        redelivers later;
      * success -> ``ack``.
    """
    parsed = _parse_or_term(msg)
    if parsed is None:
        await msg.term()
        return
    path, version = parsed
    if _try_swap(state, path, version):
        await msg.ack()


def _parse_or_term(msg: Any) -> tuple[str, str | None] | None:
    """Parse the artifact-ready body; return ``None`` (and log) if malformed."""
    parsed: tuple[str, str | None] | None = None
    try:
        parsed = parse_artifact_ready(msg.data)
    except ValueError:
        logger.exception("malformed artifact-ready message; will terminate")
    return parsed


def _try_swap(state: ServingState, path: str, version: str | None) -> bool:
    """Apply the hot-swap; return ``False`` (and log) on failure for redelivery."""
    ok = True
    try:
        state.swap(path, version)
    except Exception:  # noqa: BLE001 — one bad swap must not kill the consumer loop
        ok = False
        logger.exception("hot-swap failed; leaving message for redelivery")
    return ok


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


class SearchRequest(BaseModel):
    """``POST /search`` body: an embedding plus the number of neighbours."""

    query: list[float]
    k: int = 10


def _require_query_token(authorization: str | None, expected: str | None) -> None:
    """Enforce the ``QUACK_QUERY_TOKEN`` bearer token when one is configured.

    When ``expected`` is unset the endpoint is open (dev / in-cluster only). When
    set, the request must carry ``Authorization: Bearer <token>``.
    """
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid or missing query token")


def create_app(state: ServingState, *, query_token: str | None = None) -> FastAPI:
    """Build the FastAPI app bound to ``state``.

    Separated from :func:`main` so tests can construct the app over an in-memory
    connection without any NATS / network setup.
    """
    app = FastAPI(title="quack-server", docs_url=None, redoc_url=None)

    def auth(authorization: str | None = Header(default=None)) -> None:
        _require_query_token(authorization, query_token)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """Liveness probe: ok + the currently-served artifact version."""
        return {"status": "ok", "artifact_version": state.version}

    @app.post("/search")
    def search(req: SearchRequest, _: None = Depends(auth)) -> dict[str, Any]:
        """VSS nearest-neighbour search over the current serving artifact."""
        if req.k <= 0 or req.k > _MAX_SEARCH_K:
            raise HTTPException(status_code=400, detail=f"k must be 1..{_MAX_SEARCH_K}")
        results = state.search(req.query, req.k)
        return {"results": results, "artifact_version": state.version}

    return app


def main() -> None:  # pragma: no cover — process entrypoint (network + uvicorn)
    """Process entrypoint: connect, start the swap consumer, serve HTTP.

    Wires the swap consumer into the FastAPI lifespan so the durable NATS loop
    runs alongside the HTTP server and is cancelled cleanly on shutdown.
    """
    import contextlib

    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    state = build_connection()
    client = NatsClient()
    query_token = os.environ.get("QUACK_QUERY_TOKEN") or None
    app = create_app(state, query_token=query_token)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        await client.connect()
        stop = asyncio.Event()
        task = asyncio.create_task(run_swap_consumer(state, client, stop=stop))
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await client.close()

    app.router.lifespan_context = lifespan
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 — in-cluster Service only
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
