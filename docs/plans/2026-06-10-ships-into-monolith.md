# Ships into Monolith Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Migrate the standalone `marine`/ships AIS vessel-tracking app (3 pods + NATS + SQLite) into the monolith as a single stateless in-process module serving a public, SSR-only, CDN-cached live map at `jomcgi.dev/app/ships`.

**Architecture:** A supervised always-on asyncio task ingests AISStream.io over a websocket, dedupes and batch-writes to Postgres (the sole source of truth, no in-memory set). An SSR-only `/api/ships/snapshot` endpoint serializes a precomputed `latest_positions` table; Cloudflare CDN does the viewer fan-out (`s-maxage=120`), so origin load is flat in viewer count. A SvelteKit route mirrors the `/notes` pattern and renders a restyled MapLibre basemap with client-side dead reckoning between snapshots.

**Tech Stack:** Python 3 / FastAPI / SQLModel / psycopg3 / Postgres (partitioned, Atlas migrations) / `shared.scheduler` / SvelteKit (Svelte 5) / MapLibre GL / Bazel + apko.

**Non-negotiable house rules (apply to every task):**

- **No em-dashes** in any code, comment, commit, or doc. Use commas, colons, or parentheses.
- **No local test loop.** Write tests, but do NOT run `bazel test`/`pytest`/`vitest` from the workstation. Implement, commit, push the branch, watch CI via `gh pr checks <n> --watch`. Diagnose failures via `mcp__buildbuddy__*`.
- **One code review per PR** at the end, not per task. Implementers self-review before each commit.
- **Conventional Commits** for every commit. Commit frequently (one logical step per commit).
- **Reuse, do not reinvent.** Every pattern below already exists in the monolith; the referenced file is the source of truth for style.

**Worktree:** `/tmp/claude-worktrees/ships-monolith` on branch `feat/ships-into-monolith`. All work happens here.

---

## Background: what we are deleting, not porting

The old `projects/ships/` design is shaped by service boundaries the monolith dissolves. The following are **deleted, not migrated**:

- The 3 separate pods (`ingest`, `backend`, `frontend`) and the Bun proxy `server.ts`.
- **NATS JetStream** and the `ais.position.*`/`ais.static.*` subjects. In-process, ingest hands rows straight to the persister.
- **SQLite** and its Longhorn PVC / single-replica StatefulSet.
- The **stream-replay / catchup state machine** (durable consumer `ships-api`, `CATCHUP_PENDING_THRESHOLD`, replay batching). Postgres is durable, so there is nothing to rebuild on restart. On restart we just reconnect to AISStream and miss a few seconds, which is fine for live tracking.
- The **in-memory authoritative set** and the websocket fan-out (`WebSocketManager`, `/ws/live`). The CDN is the fan-out.

What we **keep and port**: the AIS message parsing, the dedup thresholds, moored detection, the haversine helper, ETA parsing, and the reconnect/backoff loop. Source: `projects/ships/backend/main.py` and `projects/ships/ingest/main.py`.

---

## Task 0: Scaffold the `ships` Python package and register it

**Files:**

- Create: `projects/monolith/ships/__init__.py`
- Create: `projects/monolith/ships/router.py` (stub)
- Modify: `projects/monolith/app/main.py:196-199` (router registration), lifespan startup-jobs block (~`:62-66`)
- Modify: `projects/monolith/BUILD` (hand-register, the dir is under `# gazelle:exclude knowledge` sibling conventions; ships python targets must be registered by hand the same way)

**Step 1:** Create `ships/__init__.py` mirroring `knowledge/__init__.py`:

```python
"""Ships: in-monolith AIS vessel tracking (migrated from the standalone marine app)."""

from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    """Register ships routers with the app."""
    from ships.router import router

    app.include_router(router)


def on_startup_jobs(session: Session) -> None:
    """Register ships scheduled jobs (retention / partition maintenance)."""
    from shared.scheduler import register_job
    from ships.retention import partition_maintenance_handler

    register_job(
        session,
        name="ships.partition_maintenance",
        interval_secs=86400,  # daily
        handler=partition_maintenance_handler,
        ttl_secs=3600,
    )
```

**Step 2:** Create `ships/router.py` stub:

```python
"""Ships HTTP API. SSR-only: never added to httproute-public.yaml."""

import logging

from fastapi import APIRouter

logger = logging.getLogger("ships")
router = APIRouter(prefix="/api/ships", tags=["ships"])
```

**Step 3:** Register in `app/main.py`. Add `import ships` near the other domain imports, add `ships.register(app)` after `scheduler.register(app)` (`:199`), and inside the lifespan startup `with Session(get_engine()) as session:` block (next to `home.on_startup_jobs(session)`), add `ships.on_startup_jobs(session)`.

**Step 4:** Register the package in `projects/monolith/BUILD` following the existing hand-registered `knowledge`/`chat` `py_library` entries (the tree is `gazelle:exclude`d, so new libraries and tests must be added manually). Add a `py_library` for `ships` with deps it will need: `@pip//sqlmodel`, `@pip//fastapi`, `@pip//websockets`, `@pip//certifi`, and the app/shared libs. (Confirm exact dep label style by copying the `knowledge` `py_library` block.)

**Step 5:** Commit.

```bash
git add projects/monolith/ships projects/monolith/app/main.py projects/monolith/BUILD
git commit -m "feat(monolith): scaffold ships package and register router"
```

> Note: registration referencing `ships.retention` and `ships.router` endpoints that do not exist yet is fine; the imports are lazy (inside functions). CI will only exercise them once those modules land in later tasks. If CI import-checks the registration eagerly, land Task 1, 5, and 6 modules before pushing.

---

## Task 1: Postgres schema (migration + SQLModel models)

**Files:**

- Create: `projects/monolith/chart/migrations/20260610000000_ships_schema.sql`
- Create: `projects/monolith/ships/models.py`
- Test: `projects/monolith/ships/models_test.py`

**Design notes:**

- Schema `ships`. All timestamps are `timestamptz`, never text.
- `ships.positions` is **range-partitioned by day** on `recorded_at`. Retention = drop old partitions (Task 6), never a `DELETE` loop.
- `ships.latest_positions` is the serving table (one row per vessel), maintained by upsert. It is the snapshot source and the dedup read-back source.
- `mmsi` stays `TEXT` (avoids leading-zero / identifier edge cases). Join `positions`/`latest_positions` to `vessels` on `mmsi`.

**Step 1: Write the migration SQL.**

```sql
-- 20260610000000_ships_schema.sql
CREATE SCHEMA IF NOT EXISTS ships;

-- Vessel metadata (AIS Type 5 / ShipStaticData).
CREATE TABLE ships.vessels (
    mmsi         TEXT PRIMARY KEY,
    imo          TEXT,
    call_sign    TEXT,
    name         TEXT,
    ship_type    INTEGER,
    dimension_a  INTEGER,
    dimension_b  INTEGER,
    dimension_c  INTEGER,
    dimension_d  INTEGER,
    destination  TEXT,
    eta          TIMESTAMPTZ,
    draught      DOUBLE PRECISION,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Position history (AIS Type 1/2/3). Partitioned by day for drop-partition retention.
CREATE TABLE ships.positions (
    id           BIGGENERATED... -- see note
    mmsi         TEXT NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    speed        DOUBLE PRECISION,
    course       DOUBLE PRECISION,
    heading      INTEGER,
    nav_status   INTEGER,
    ship_name    TEXT,
    recorded_at  TIMESTAMPTZ NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
) PARTITION BY RANGE (recorded_at);

-- Partitioned tables cannot have a plain bigserial PK that is also the partition
-- key, so use a composite PK (recorded_at, id) with id from a sequence.
-- Replace the BIGGENERATED line above with:
--   id BIGINT NOT NULL,
-- and after the table:
CREATE SEQUENCE ships.positions_id_seq OWNED BY ships.positions.id;
ALTER TABLE ships.positions ALTER COLUMN id SET DEFAULT nextval('ships.positions_id_seq');
ALTER TABLE ships.positions ADD PRIMARY KEY (recorded_at, id);

-- BRIN for the time-window scans (retention, /track since=), tiny + append-friendly.
CREATE INDEX positions_recorded_brin ON ships.positions USING brin (recorded_at);
-- Btree for /track (filter by mmsi, order by time).
CREATE INDEX positions_mmsi_recorded ON ships.positions (mmsi, recorded_at DESC);

-- Seed an initial set of daily partitions so inserts have somewhere to land
-- before the maintenance job first runs. Create yesterday..+2 days.
-- (The maintenance job, Task 6, keeps a rolling window ahead.)
CREATE TABLE ships.positions_default PARTITION OF ships.positions DEFAULT;

-- Current position per vessel (serving + dedup read-back).
CREATE TABLE ships.latest_positions (
    mmsi                    TEXT PRIMARY KEY,
    lat                     DOUBLE PRECISION NOT NULL,
    lon                     DOUBLE PRECISION NOT NULL,
    speed                   DOUBLE PRECISION,
    course                  DOUBLE PRECISION,
    heading                 INTEGER,
    nav_status              INTEGER,
    ship_name               TEXT,
    recorded_at             TIMESTAMPTZ NOT NULL,
    first_seen_at_location  TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

> Implementer: clean up the inline `BIGGENERATED...` placeholder per the comment block (it documents the partition-PK constraint). A `DEFAULT` partition guarantees inserts never fail for a missing day; Task 6 converts the rolling window to explicit daily partitions and drops old ones. Verify the final SQL renders by reading a sibling migration (`20260408000000_knowledge_schema.sql`) for the Atlas file conventions (no transaction wrapper, idempotent-friendly).

**Step 2: Write SQLModel models** mirroring the migration, with `schema="ships"` and `extend_existing`. Mirror any future CHECK constraints into `__table_args__` (knowledge/models.py:67-79 pattern) so SQLite unit tests enforce them. Use `datetime` (tz-aware) fields, not str.

```python
from datetime import datetime, timezone
from sqlmodel import Field, SQLModel


class Vessel(SQLModel, table=True):
    __tablename__ = "vessels"
    __table_args__ = {"schema": "ships", "extend_existing": True}

    mmsi: str = Field(primary_key=True)
    imo: str | None = None
    call_sign: str | None = None
    name: str | None = None
    ship_type: int | None = None
    dimension_a: int | None = None
    dimension_b: int | None = None
    dimension_c: int | None = None
    dimension_d: int | None = None
    destination: str | None = None
    eta: datetime | None = None
    draught: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LatestPosition(SQLModel, table=True):
    __tablename__ = "latest_positions"
    __table_args__ = {"schema": "ships", "extend_existing": True}

    mmsi: str = Field(primary_key=True)
    lat: float
    lon: float
    speed: float | None = None
    course: float | None = None
    heading: int | None = None
    nav_status: int | None = None
    ship_name: str | None = None
    recorded_at: datetime
    first_seen_at_location: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Position(SQLModel, table=True):
    __tablename__ = "positions"
    __table_args__ = {"schema": "ships", "extend_existing": True}

    # Composite PK (recorded_at, id) in PG; for SQLite tests a single PK is fine.
    id: int | None = Field(default=None, primary_key=True)
    mmsi: str
    lat: float
    lon: float
    speed: float | None = None
    course: float | None = None
    heading: int | None = None
    nav_status: int | None = None
    ship_name: str | None = None
    recorded_at: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

**Step 3: Write a model test** (`models_test.py`) using the SQLite + `SQLModel.metadata.create_all()` fixture convention (see knowledge tests; mirror their conftest). Assert a vessel + latest_position round-trips. Keep it minimal; this just proves the models import and create.

**Step 4:** Register the test in `projects/monolith/BUILD` (hand-registered, gazelle-excluded tree).

**Step 5:** Commit.

```bash
git add projects/monolith/chart/migrations/20260610000000_ships_schema.sql projects/monolith/ships/models.py projects/monolith/ships/models_test.py projects/monolith/BUILD
git commit -m "feat(monolith): add ships postgres schema and models"
```

---

## Task 2: Pure AIS logic (parsing, dedup, moored, haversine, ETA)

These are pure functions with no DB or network, so they are the most testable. Port verbatim from the old code, swapping the in-memory cache argument for an explicit "previous position" argument so the function stays pure (the stateless design feeds it the read-back row).

**Files:**

- Create: `projects/monolith/ships/ais.py`
- Test: `projects/monolith/ships/ais_test.py` (port cases from `projects/ships/ingest/ais_parsing_test.py` and `projects/ships/backend/tests/unit_test.py`)

**Step 1:** Write `haversine_distance` (verbatim from `backend/main.py:143-158`), the dedup thresholds as module constants (`DEDUP_SPEED_THRESHOLD=0.5`, `DEDUP_DISTANCE_METERS=100`, `DEDUP_TIME_THRESHOLD=300`, `MOORED_RADIUS_METERS=500`, read from env with these defaults), and a pure `should_insert_position(data, last)` ported from `backend/main.py:273-328` where `last` is a `LatestPosition | None` instead of the cache lookup:

```python
def should_insert_position(
    data: dict, last: "PriorPosition | None"
) -> tuple[bool, datetime | None]:
    """Dedup decision. Returns (should_insert, first_seen_at_location).

    Pure: `last` is the prior latest_positions row (or None), passed in by the
    caller after a batched read-back. Ported from projects/ships/backend/main.py.
    """
    # ... port the speed > threshold / distance > threshold / time > threshold
    # ladder exactly, but compare datetimes directly (timestamptz) instead of
    # parsing ISO strings.
```

Also port AIS message parsing (PositionReport -> position dict, ShipStaticData -> vessel dict) and ETA parsing (`ingest/main.py` ETA -> ISO; here produce a tz-aware `datetime`). Keep field names aligned with the models.

**Step 2:** Port the test cases. Cover: first position always inserts; moving vessel inserts; stationary within 100 m and 300 s skips; stationary but > 300 s inserts; moved > 100 m resets `first_seen`; haversine known-distance sanity; ETA parse incl. the AIS "not available" sentinel.

**Step 3:** Register test in BUILD.

**Step 4:** Commit.

```bash
git commit -am "feat(monolith): port AIS parsing, dedup, and haversine logic"
```

---

## Task 3: Stateless persistence layer (read-back + batched write)

**Files:**

- Create: `projects/monolith/ships/store.py`
- Test: `projects/monolith/ships/store_test.py`

**Design:** one function per ingest batch, fully stateless. It opens nothing long-lived; the caller passes a `Session`. Steps inside:

1. Collect parsed positions + vessels from the batch.
2. `SELECT ... FROM ships.latest_positions WHERE mmsi = ANY(:mmsis)` once. Build `{mmsi: PriorPosition}`.
3. For each position, call `should_insert_position(data, prior.get(mmsi))`; keep survivors with their computed `first_seen_at_location`.
4. Batch `INSERT` survivors into `ships.positions` (history).
5. Batch upsert survivors into `ships.latest_positions` (`INSERT ... ON CONFLICT (mmsi) DO UPDATE`, `COALESCE(excluded.ship_name, latest_positions.ship_name)` to preserve names, mirror `backend/main.py:408-430`).
6. Batch upsert vessels into `ships.vessels` (`ON CONFLICT (mmsi) DO UPDATE`, mirror `backend/main.py:455-478`).
7. One commit.

Use `sqlalchemy.text()` with bound params. Heed `feedback_psycopg3_nullable_param_cast`: nullable string params used in `IS NULL OR ...` shapes need explicit `::text` casts. Use `mmsi = ANY(:mmsis)` with a list param (psycopg3 adapts a Python list to a Postgres array).

**Step 1:** Write `store_test.py` first (SQLite fixture): seed a `latest_positions` row, feed a batch with one stationary-skip + one moving-insert, assert `positions` got 1 row and `latest_positions` updated only the mover. (Note: ARRAY/`ANY` and `ON CONFLICT` differ on SQLite; for the unit test, either parametrize the read-back to an `IN` clause or gate the Postgres-specific SQL behind a dialect check. Simplest: write the read-back as `WHERE mmsi IN :mmsis` via `expanding=True` bindparam, which works on both, and use SQLModel/SQLAlchemy `merge`-style upsert helpers that the existing code uses cross-dialect. Confirm how knowledge/chat do cross-dialect upserts before choosing.)

**Step 2:** Implement `persist_batch(session, positions, vessels)`.

**Step 3:** Register test in BUILD.

**Step 4:** Commit.

```bash
git commit -am "feat(monolith): stateless ships persistence (read-back + batched upsert)"
```

---

## Task 4: AISStream ingest background task (supervised, in lifespan)

**Files:**

- Create: `projects/monolith/ships/ingest.py`
- Modify: `projects/monolith/app/main.py` (lifespan: spawn + cancel the task)
- Test: `projects/monolith/ships/ingest_test.py` (reconnect/backoff + batch-flush, mock the websocket; port from `ingest/reconnect_cap_test.py`, `nats_and_reconnect_test.py` minus NATS)

**Design:** mirror `ingest/main.py:241-282` connect/backoff loop, but publish to the persister instead of NATS, and batch. The loop:

```python
async def ais_stream_loop(stop: asyncio.Event) -> None:
    """Supervised AISStream listener. Reconnects forever; never raises out."""
    import ssl, json, certifi, websockets
    from app.db import get_engine
    from sqlmodel import Session
    from ships.ais import parse_message
    from ships.store import persist_batch

    api_key = os.environ.get("AISSTREAM_API_KEY", "")
    url = os.environ.get("AISSTREAM_URL", "wss://stream.aisstream.io/v0/stream")
    bbox = os.environ.get("BOUNDING_BOX", DEFAULT_BBOX)  # Pacific NW default
    if not api_key:
        logger.warning("AISSTREAM_API_KEY unset; ships ingest disabled")
        return

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    delay = INITIAL_RECONNECT_DELAY
    while not stop.is_set():
        try:
            async with websockets.connect(url, ssl=ssl_ctx) as ws:
                await ws.send(json.dumps({
                    "APIKey": api_key,
                    "BoundingBoxes": json.loads(bbox),
                    "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
                }))
                delay = INITIAL_RECONNECT_DELAY
                batch_pos, batch_ves, last_flush = [], [], loop_time()
                async for raw in ws:
                    if stop.is_set():
                        break
                    kind, row = parse_message(raw)
                    if kind == "position":
                        batch_pos.append(row)
                    elif kind == "vessel":
                        batch_ves.append(row)
                    # Flush on size or time (e.g. >=200 rows or >2s).
                    if len(batch_pos) + len(batch_ves) >= FLUSH_SIZE or \
                       loop_time() - last_flush > FLUSH_SECONDS:
                        await asyncio.to_thread(_flush, batch_pos, batch_ves)
                        batch_pos, batch_ves, last_flush = [], [], loop_time()
        except Exception:
            logger.exception("ships ingest: stream error")
        if not stop.is_set():
            await asyncio.sleep(delay)
            delay = min(delay * RECONNECT_BACKOFF_FACTOR, MAX_RECONNECT_DELAY)


def _flush(positions, vessels) -> None:
    from app.db import get_engine
    from sqlmodel import Session
    from ships.store import persist_batch
    if not positions and not vessels:
        return
    with Session(get_engine()) as session:
        persist_batch(session, positions, vessels)
```

DB writes go through `asyncio.to_thread` with a fresh `Session(get_engine())` (the request-scoped `get_session` is unavailable in a background task; this matches how scheduler handlers open their own session).

**Step 1:** Write `ingest_test.py` first: mock `websockets.connect` to yield a couple of messages then raise `ConnectionClosed`, assert the loop calls `_flush` with parsed rows and that backoff grows then resets. Assert an exception inside the loop body does not propagate (the `while` keeps going).

**Step 2:** Implement `ingest.py`.

**Step 3:** Wire into `app/main.py` lifespan: create an `asyncio.Event()` stored on `app.state.ships_stop`, `task = asyncio.create_task(ais_stream_loop(stop))`, `task.add_done_callback(_log_task_exception)`. In shutdown, `stop.set()` then `task.cancel()` + awaited with `CancelledError` swallow (mirror the lock-sweep teardown).

**Step 4:** Register test in BUILD. Add `@pip//websockets`, `@pip//certifi` deps to the ships `py_library` if not already.

**Step 5:** Commit.

```bash
git commit -am "feat(monolith): supervised AISStream ingest background task"
```

---

## Task 5: Snapshot + track endpoints (SSR-only, CDN-cached)

**Files:**

- Modify: `projects/monolith/ships/router.py`
- Test: `projects/monolith/ships/router_test.py`

**Design:** mirror `knowledge/router.py:112-167` (cache-control constant, ETag, conditional GET 304).

```python
# Mirrors SHIPS_SNAPSHOT_CACHE_CONTROL in frontend/src/lib/cache-headers.js. Keep in sync.
_SNAPSHOT_CACHE_CONTROL = (
    "public, s-maxage=120, stale-while-revalidate=600, stale-if-error=86400"
)


def _snapshot_etag(vessel_count: int, max_updated: datetime | None) -> str:
    stamp = max_updated.isoformat() if max_updated is not None else "null"
    return f'"{stamp}-{vessel_count}"'


@router.get("/snapshot")
def get_snapshot(request: Request, response: Response, session: Session = Depends(get_session)):
    """All current vessel positions for the /app/ships map. SSR-only, CDN-cached."""
    rows = session.exec(text(
        "SELECT lp.*, v.name AS vessel_name, v.ship_type, v.destination, v.eta "
        "FROM ships.latest_positions lp "
        "LEFT JOIN ships.vessels v USING (mmsi)"
    )).mappings().all()
    max_updated = max((r["updated_at"] for r in rows), default=None)
    etag = _snapshot_etag(len(rows), _as_utc(max_updated))
    headers = {"Cache-Control": _SNAPSHOT_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    for k, v in headers.items():
        response.headers[k] = v
    return {"count": len(rows), "vessels": [dict(r) for r in rows]}


@router.get("/track/{mmsi}")
def get_track(mmsi: str, since: str | None = None, limit: int = 1000,
              request: Request = None, response: Response = None,
              session: Session = Depends(get_session)):
    """Position history for one vessel (shown on marker click). SSR-only."""
    # SELECT ... FROM ships.positions WHERE mmsi = :mmsi [AND recorded_at > now() - :since]
    # ORDER BY recorded_at DESC LIMIT :limit. Short-TTL cache header.
```

Copy `_as_utc` from `knowledge/router.py:118-130` (or import if shared). Track endpoint gets a shorter `s-maxage` (e.g. 60s).

**Critical:** do **not** add `/api/ships/*` to `chart/templates/httproute-public.yaml`. The private HTTPRoute already passes unmatched `/api/*` to the backend, and SSR reaches it at `http://localhost:8000`. This keeps it off the public internet.

**Step 1:** Write `router_test.py`: seed latest_positions + vessels (SQLite fixture, FastAPI `TestClient`), assert `/api/ships/snapshot` returns the vessels and a `Cache-Control`/`ETag`; assert a matching `If-None-Match` yields 304; assert `/track/{mmsi}` returns ordered history. Port shapes from `backend/tests/ships_api_test.py`.

**Step 2:** Implement the endpoints.

**Step 3:** Register test in BUILD.

**Step 4:** Commit.

```bash
git commit -am "feat(monolith): ships snapshot and track endpoints (SSR-only, CDN-cached)"
```

---

## Task 6: Partition maintenance + retention scheduled job

**Files:**

- Create: `projects/monolith/ships/retention.py`
- Test: `projects/monolith/ships/retention_test.py`

**Design:** a `shared.scheduler` handler (registered in Task 0) that runs daily and:

1. Ensures daily partitions exist for today..+2 days (`CREATE TABLE IF NOT EXISTS ships.positions_YYYYMMDD PARTITION OF ships.positions FOR VALUES FROM (...) TO (...)`).
2. Drops partitions older than the retention window (default 7 days): find child partitions of `ships.positions` whose upper bound is before `now() - retention`, `DROP TABLE`. This is instant and produces zero vacuum churn (the whole point of partitioning on shared Postgres).

Handler signature matches `shared/scheduler.py` (`async def partition_maintenance_handler(session) -> datetime | None`). Do partition DDL via `asyncio.to_thread` with the engine if needed, or run synchronously on the passed session (DDL is quick).

**Step 1:** Write `retention_test.py`: assert the SQL builder produces correct partition names/bounds for a given date, and that the "drop older than N days" selection excludes in-window partitions. (Partition DDL itself is Postgres-only; unit-test the name/bound/selection logic as pure functions, defer the live DDL to CI/prod.)

**Step 2:** Implement `retention.py` (pure helpers + the handler).

**Step 3:** Register test in BUILD.

**Step 4:** Commit.

```bash
git commit -am "feat(monolith): ships partition maintenance and retention job"
```

---

## Task 7: Secrets and chart wiring

**Files:**

- Modify: `projects/monolith/chart/templates/onepassworditem.yaml` (or add a ships-specific item) to surface `AISSTREAM_API_KEY`
- Modify: `projects/monolith/chart/templates/deployment.yaml` (env: `AISSTREAM_API_KEY` via `secretKeyRef`; plain `AISSTREAM_URL`, `BOUNDING_BOX`)
- Modify: `projects/monolith/chart/values.yaml` (bounding box default, onepassword item path)
- Modify: `projects/monolith/chart/Chart.yaml` (version bump)
- Modify: `projects/monolith/deploy/application.yaml` (`targetRevision` to match the new chart version)

**Design:** the AISStream key currently lives in the `marine` 1Password item. Either (a) add the field to the monolith's existing OnePasswordItem in 1Password and reference it, or (b) add a second `OnePasswordItem` template gated by a value. Prefer (a) for fewer moving parts if the monolith item can hold the extra field; confirm the 1Password item layout before choosing. Inject via `secretKeyRef` exactly like `ICAL_FEED_URL` (deployment.yaml:43-47). NATS env vars are NOT added (NATS is gone).

**House rule:** bump `Chart.yaml` `version` AND `deploy/application.yaml` `targetRevision` together (the chart-version-bot normally does this, but manual bumps must keep both in sync).

**Step 1:** Render the chart locally to verify templating (allowed; this is not a test run):

```bash
helm template monolith projects/monolith/chart/ -f projects/monolith/deploy/values.yaml | grep -A3 AISSTREAM
```

Expected: the env var resolves to a `secretKeyRef`.

**Step 2:** Commit.

```bash
git commit -am "feat(monolith): wire AISStream secret and bump chart for ships"
```

---

## Task 8: Frontend route + MapLibre map + dead reckoning

**Files:**

- Modify: `projects/monolith/frontend/package.json` (add `maplibre-gl`), regenerate the lockfile with `pnpm install` (vendored pnpm)
- Modify: `projects/monolith/frontend/src/lib/cache-headers.js` (add `SHIPS_SNAPSHOT_CACHE_CONTROL`, mirror the python constant, keep-in-sync comment)
- Create: `projects/monolith/frontend/src/routes/public/app/ships/+page.server.js`
- Create: `projects/monolith/frontend/src/routes/public/app/ships/+page.js` (`export const ssr = false`)
- Create: `projects/monolith/frontend/src/routes/public/app/ships/+page.svelte`
- Create: `projects/monolith/frontend/src/lib/public/components/ships/ShipsMap.svelte` (MapLibre wrapper)
- Create: `projects/monolith/frontend/src/lib/public/ships/deadReckoning.js` (pure extrapolation)
- Test: `projects/monolith/frontend/src/routes/public/app/ships/page.server.test.js`, `.../ships/deadReckoning.test.js`
- Modify: `projects/monolith/frontend/BUILD` (it is `gazelle:ignore`; the `:src` glob already picks up `src/**/*`, but confirm new test files match the `:test_lib` glob and add the maplibre dep to the js deps)
- Modify: `projects/monolith/frontend/src/routes/+layout.svelte` Nav `activeRoute` (add `/app/ships` active state) if a nav entry is wanted

**Step 1: Cache header constant** (mirror python):

```javascript
// /app/ships snapshot: 120s fresh · 10m SWR · 1d SIE. Mirrors _SNAPSHOT_CACHE_CONTROL
// in projects/monolith/ships/router.py — keep in sync.
export const SHIPS_SNAPSHOT_CACHE_CONTROL = `public, s-maxage=120, stale-while-revalidate=600, stale-if-error=86400`;
```

**Step 2: Server load** (clone of notes `+page.server.js`):

```javascript
import { error } from "@sveltejs/kit";
import { SHIPS_SNAPSHOT_CACHE_CONTROL } from "../../../../lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

export async function load({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/ships/snapshot`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw error(503, "ships snapshot unavailable");

  const headers = { "cache-control": SHIPS_SNAPSHOT_CACHE_CONTROL };
  const etag = res.headers?.get?.("etag");
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return { snapshot: await res.json() };
}
```

(Watch the relative `../` depth: the file is one level deeper than `public/notes/`, so it is `../../../../lib/cache-headers.js`. Verify against the actual tree.)

**Step 3: `+page.js`** sets `export const ssr = false;` (MapLibre needs `window`/WebGL; the server load still runs server-side, so data is SSR-sourced while the canvas renders client-side, identical to notes).

**Step 4: Dead reckoning** (pure, unit-tested):

```javascript
// Extrapolate a vessel's position from its last fix using speed (knots) + course (deg).
// Returns {lat, lon} for `elapsedSeconds` after the fix. Great-circle-ish small-step.
export function deadReckon({ lat, lon, speed, course }, elapsedSeconds) {
  if (!speed || speed <= 0) return { lat, lon };
  const metersPerSec = speed * 0.514444; // knots -> m/s
  const dist = metersPerSec * elapsedSeconds;
  const R = 6371000;
  const brng = (course ?? 0) * (Math.PI / 180);
  const lat1 = lat * (Math.PI / 180);
  const lon1 = lon * (Math.PI / 180);
  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(dist / R) +
      Math.cos(lat1) * Math.sin(dist / R) * Math.cos(brng),
  );
  const lon2 =
    lon1 +
    Math.atan2(
      Math.sin(brng) * Math.sin(dist / R) * Math.cos(lat1),
      Math.cos(dist / R) - Math.sin(lat1) * Math.sin(lat2),
    );
  return { lat: lat2 * (180 / Math.PI), lon: lon2 * (180 / Math.PI) };
}
```

Test: a vessel heading 090 (due east) at 10 kn for 60 s moves ~309 m east, lat ~unchanged; speed 0 returns the same point.

**Step 5: ShipsMap.svelte** (MapLibre wrapper):

- Init `maplibre-gl` map with a **no-API-key basemap**. Use OpenFreeMap (`https://tiles.openfreemap.org/styles/liberty`) or a CARTO raster style. Pick OpenFreeMap (vector, free, no key); note in a comment that self-hosting tiles is a follow-up if external dependency is unwanted.
- Restyle to the palette: cream water/land via a style override or a simple raster + CSS filter, `--ink` strokes, hide busy labels. Keep it muted so brutalist chrome reads on top.
- Render vessels as markers/symbols colored by `ship_type`; orientation from `heading`/`course`. Update positions every animation frame using `deadReckon(vessel, (now - fixTime)/1000)`; correct to the true fix when a new snapshot arrives.
- Brutalist chrome: a header bar (thick 2px `--ink` border, hard shadow, JetBrains Mono "SHIPS · live ●"), a vessel side-panel card (`.card-hard`, `--shadow-hard`) populated on marker click by fetching `/api/ships/track/{mmsi}` via the SvelteKit data layer (NOT a direct public call; route it through a `+page.server.js`-style endpoint or a `+server.js` proxy if needed to keep it SSR-only). Use design-system.css tokens; do not hardcode colors.
- Import `maplibre-gl/dist/maplibre-gl.css` and scope overrides.

**Step 6: +page.svelte**: `let { data } = $props();` feed `data.snapshot.vessels` into `ShipsMap`. Set up a `setInterval(() => invalidateAll(), 120_000)` (or `invalidate` of the load) in `onMount`, cleared on destroy, to refresh the snapshot through the CDN.

**Step 7:** Write `page.server.test.js` (mirror notes test: mock fetch + setHeaders, assert endpoint URL `/api/ships/snapshot`, cache-control set, etag forwarded, 503 on `!ok`) and `deadReckoning.test.js`.

**Step 8:** `pnpm install` to update the lockfile; confirm `frontend/BUILD` deps include maplibre (rules_js resolves from the lockfile). Do NOT run vitest locally.

**Step 9:** Commit (may be split into dep-add / load+dr / map-component commits).

```bash
git commit -am "feat(frontend): add /app/ships live MapLibre map with dead reckoning"
```

> macOS gotcha (from memory): `pnpm build` can clobber the Bazel `BUILD` file via the case-insensitive `build/` dir. Do not run `pnpm build`; let CI build. If the lockfile step touches `BUILD`, restore it.

---

## Task 9: Push, watch CI, end-of-PR review, verify live

**Step 1:** Push the branch and open the PR.

```bash
git push -u origin feat/ships-into-monolith
gh pr create --fill
gh pr checks <n> --watch
```

**Step 2:** Diagnose any CI failures via `mcp__buildbuddy__get_invocation` (commitSha selector) -> `get_target` -> `get_log`. Quote the actual error before hypothesizing (per CLAUDE.md). Push fixes.

**Step 3:** One comprehensive code review of the full diff (`/code-review` or the code-review skill). Address findings.

**Step 4:** Merge with rebase (`gh pr merge --rebase`; squash is disabled).

**Step 5:** Verify live after ArgoCD syncs (~5-10s) and the Atlas migration applies:

- `kubectl -n <monolith-ns> logs` for "ships ingest" connecting to AISStream and flushing batches.
- `curl -s https://jomcgi.dev/app/ships` returns the SSR page; confirm vessels render.
- Confirm `curl https://jomcgi.dev/api/ships/snapshot` from the public internet does **NOT** work (SSR-only); only the page does.
- Confirm Cache-Control on the page response (`curl -I`).
- Watch `ships.partition_maintenance` register and the daily partition appear.

---

## Task 10 (follow-up PR, AFTER live verification): decommission the standalone marine app

Do this only once the in-monolith ships is verified serving real vessels. Until then the old `marine` app keeps running untouched.

**Files (separate PR):**

- Delete: `projects/ships/` (chart, deploy, backend, ingest, frontend)
- Remove the `marine` ArgoCD Application from the root kustomization (run `format` to regenerate `projects/home-cluster/kustomization.yaml`)
- Remove the `marine` namespace `OnePasswordItem` (after confirming the key was copied into the monolith's 1Password item)
- Redirect `ships.jomcgi.dev` -> `jomcgi.dev/app/ships` (or remove the DNS/route) so the old URL does not 404
- Remove the `marine` SigNoz HTTP check alert (`projects/ships/deploy/marine-httpcheck-alert.yaml`); optionally add an equivalent check for `/app/ships`

Commit: `chore(marine): decommission standalone ships app, now served in monolith`.

---

## Open follow-ups (not blocking)

- Self-host the MapLibre basemap tiles behind Cloudflare instead of OpenFreeMap, if the external tile dependency is unwanted.
- Add a SigNoz HTTP check + alert for `/app/ships` (mirror the old `marine-httpcheck-alert.yaml` via the `add-httpcheck-alert` skill).
- Consider a compact snapshot encoding (arrays over objects) if the JSON payload grows large.
