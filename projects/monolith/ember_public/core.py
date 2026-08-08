"""Demo-postgres core (embervm R4 stateful sleep/wake exhibit).

A dedicated stateful Postgres workload (NOT the scratch-postgres real tenants
use) tuned to bank ~a sweeper tick after its last connection closes. The
demo's three verbs map straight onto the embervm surfaces:
  status -> GET  {EMBERVM_URL}/v1/stateful/demo-postgres (management introspection;
            an HTTP read of the control plane, so POLLING NEVER WAKES THE VM)
  query  -> a short-lived psycopg connect to DEMO_POSTGRES_DSN; the TCP
            activator wakes the VM on a miss, so connect wall time IS the wake
  reset  -> DELETE {EMBERVM_URL}/v1/stateful/demo-postgres/instance (destroys
            the live VM AND evicts the banked bundle; the volume survives, so
            the next connect cold-boots against retained data). Reset itself
            stays private-only (see demos/firecracker_api.py); this module
            only owns the destroy call it wraps.

This module must stay importable in the public closure (see
ember_public/__init__.py's docstring): EMBERVM_URL is read directly from the
environment rather than imported from sandbox.client, which is forbidden
publicly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
from datetime import datetime, timezone
from time import monotonic, perf_counter

import httpx
import psycopg
from sqlmodel import Session

from core.db import get_engine
from ember_public.db import get_savings_engine
from ember_public.models import DemoPgSavings
from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

# EmberVM control-plane base URL. Shared with sandbox.client's EMBERVM_URL, but
# read directly here (not imported) so this module never pulls sandbox.client
# into the public import closure.
EMBERVM_URL = os.environ.get("EMBERVM_URL", "")

_DEMO_PG_WORKLOAD = "demo-postgres"
# The workload's wakeTimeoutSeconds is 60 (cold boots include WAL recovery and,
# on first boot, mkfs + initdb); the client timeout must outlast it so a cold
# wake surfaces as a slow-but-successful connect, not a client-side timeout.
_DEMO_PG_CONNECT_TIMEOUT_S = 75
_DEMO_PG_HISTORY_ROWS = 15


def demo_pg_dsn() -> str:
    """Read at call time (not import) so tests can patch the environment."""
    return os.environ.get("DEMO_POSTGRES_DSN", "")


_DEMO_PG_SESSION_COOKIE = "demo_pg_session"


def demo_pg_session_salt() -> str:
    """Derived from the DSN so the salt is stable across replicas and restarts
    without a new secret. Never returned to clients."""
    return hashlib.sha256(
        ("demo-pg-session-salt:" + demo_pg_dsn()).encode()
    ).hexdigest()


def demo_pg_session_tag(cookie_value: str) -> str | None:
    """Hash the session cookie into an opaque per-visitor tag for attribution.
    Returns None when there is no cookie to tag."""
    if not cookie_value:
        return None
    salt = demo_pg_session_salt()
    return hashlib.sha256((salt + ":" + cookie_value).encode()).hexdigest()[:16]


async def fetch_demo_pg_status() -> dict:
    """GET the control plane's stateful introspection for the demo workload."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{EMBERVM_URL}/v1/stateful/{_DEMO_PG_WORKLOAD}",
            headers=auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Status cache: a 500ms single-flight window in front of fetch_demo_pg_status.
#
# The frontend polls /status sub-second; without this every concurrent poller
# would issue its own control-plane read. One async lock means the first
# caller past the TTL does the real fetch and every concurrent/near-concurrent
# caller shares that one result instead of piling on the control plane.
#
# ---------------------------------------------------------------------------

_STATUS_CACHE_TTL_S = 0.5
_status_cache_lock = asyncio.Lock()
_status_cache: dict = {
    "at": None,
    "payload": None,
}


async def cached_demo_pg_status() -> dict:
    """Single-flight, TTL-cached read through fetch_demo_pg_status.

    Concurrent callers within the TTL window share one upstream fetch. Also
    used by the /status endpoint, so concurrent readers share the exact same
    snapshot rather than racing separate control-plane calls.
    """
    async with _status_cache_lock:
        now = monotonic()
        cached_at = _status_cache["at"]
        if cached_at is not None and (now - cached_at) < _STATUS_CACHE_TTL_S:
            return _status_cache["payload"]

        payload = await fetch_demo_pg_status()
        _status_cache["at"] = now
        _status_cache["payload"] = payload
        return payload


# ---------------------------------------------------------------------------
# Global query semaphore: caps concurrent roundtrips against demo-postgres so
# a burst of visitors cannot pile connections onto a VM that just woke (or
# stampede the activator while it is relighting). Non-blocking acquire: an
# exhausted semaphore returns an in-band busy response rather than queuing,
# since a queued visitor has no way to know they are waiting.
# ---------------------------------------------------------------------------

_QUERY_SEMAPHORE_SIZE = int(os.environ.get("EMBER_DEMO_MAX_CONCURRENT", "4"))
_query_semaphore = asyncio.Semaphore(_QUERY_SEMAPHORE_SIZE)


def try_acquire_query_slot() -> bool:
    """Non-blocking acquire. Caller MUST release_query_slot() iff this is True.

    asyncio.Semaphore has no native nowait variant, so this checks locked()
    first (true once the internal counter hits zero) before acquiring; the
    acquire itself is synchronous and cannot actually block once locked() is
    False, so this never yields to the event loop.
    """
    if _query_semaphore.locked():
        return False
    _query_semaphore._value -= 1
    return True


def release_query_slot() -> None:
    _query_semaphore.release()


# ---------------------------------------------------------------------------
# Per-session insert bucket: one insert per session_tag per second. Keyed
# on the opaque session tag (not IP), bounded by pruning stale entries on
# every access so long-running processes never grow this dict unbounded.
# The window sits just below the frontend queue's 1000ms cooldown so the
# honest one-per-second queue never trips it; this stays a backstop against
# scripted clients that bypass the UI or tabs racing a shared session cookie.
# ---------------------------------------------------------------------------

_INSERT_BUCKET_WINDOW_S = 0.9
_INSERT_BUCKET_PRUNE_AGE_S = 3600.0
_insert_bucket: dict[str, float] = {}


def _prune_insert_bucket(now: float) -> None:
    stale = [
        tag
        for tag, last in _insert_bucket.items()
        if (now - last) > _INSERT_BUCKET_PRUNE_AGE_S
    ]
    for tag in stale:
        del _insert_bucket[tag]


def check_and_record_insert(session_tag: str) -> bool:
    """True if this insert is allowed; records the attempt either way.

    Returns False (rejected) when the same session_tag inserted within the
    last _INSERT_BUCKET_WINDOW_S seconds, without updating its timestamp (a
    rejected attempt must not reset the visitor's own window).
    """
    now = monotonic()
    _prune_insert_bucket(now)
    last = _insert_bucket.get(session_tag)
    if last is not None and (now - last) < _INSERT_BUCKET_WINDOW_S:
        return False
    _insert_bucket[session_tag] = now
    return True


# ---------------------------------------------------------------------------
# Presence: a rough live count of visitors watching the exhibit right now, so
# the shared-VM warmth reads as "N people are here poking it" instead of a
# ghost wake nobody in the room triggered. Each client mints an ephemeral id
# on page load and carries it on every status poll (the poll the page already
# fires sub-second); we stamp last-seen per id and count ids seen within the
# TTL window. Keyed on the opaque client id, never an IP, and NOT the insert
# session_tag: the session cookie is minted only on first insert and is not
# forwarded on the status poll, so tagging presence to it would count only
# people who have written, not people watching.
#
# Bounded two ways: a TTL prune on every access (a client that closes the tab
# ages out within _PRESENCE_TTL_S), plus a hard id cap so a scripted client
# churning a fresh id every poll cannot grow the map without limit.
# ---------------------------------------------------------------------------

_PRESENCE_TTL_S = 6.0
_PRESENCE_MAX_IDS = 5000
_PRESENCE_ID_MAXLEN = 64
_presence: dict[str, float] = {}


def _prune_presence(now: float) -> None:
    cutoff = now - _PRESENCE_TTL_S
    stale = [cid for cid, seen in _presence.items() if seen < cutoff]
    for cid in stale:
        del _presence[cid]


def record_presence(client_id: str) -> None:
    """Stamp this client id as seen now. Ignores an empty or oversized id, and
    refuses a brand-new id once the map is at its cap (so an id-churning client
    cannot grow it past _PRESENCE_MAX_IDS); an id already present is always
    refreshed so genuine visitors never age out under the cap."""
    if not client_id or len(client_id) > _PRESENCE_ID_MAXLEN:
        return
    now = monotonic()
    _prune_presence(now)
    if client_id not in _presence and len(_presence) >= _PRESENCE_MAX_IDS:
        return
    _presence[client_id] = now


def present_count() -> int:
    """Distinct client ids seen within the TTL window (prunes as it reads)."""
    now = monotonic()
    _prune_presence(now)
    return len(_presence)


def shape_pg_status(status: dict) -> dict:
    """Reduce the control-plane payload to what the sleep indicator renders."""
    instance = status.get("instance") or {}
    return {
        "state": status.get("state"),
        "generation": status.get("generation"),
        "bundle_generation": status.get("bundle_generation"),
        "pair_valid": status.get("pair_valid"),
        "volume_bytes": status.get("volume_bytes"),
        "healthy": instance.get("healthy"),
        "last_active_at": instance.get("last_active_at"),
        "last_park_ms": instance.get("last_park_ms"),
        "last_park_at": instance.get("last_park_at"),
        "park_seq": instance.get("park_seq"),
        "created_at": instance.get("created_at"),
    }


# All-time "memory saved while asleep" counter (every visitor, not just this
# session). Accrual is lazy on the status poll: the demo VM can only wake when
# something connects, so two consecutive samples that are both state ==
# "banked" with the SAME generation prove the VM slept the entire gap between
# the samples, and that whole gap is credited at the guest's full footprint.
# Any other transition (a generation change, or either sample not banked)
# credits nothing, so the counter is a conservative undercount, never an
# overcount.
#
# COUPLING: this MUST equal the demo workload's memMib, which is the size of
# the memory the guest is actually not holding while it sleeps. It is declared
# in projects/embervm/chart/templates/workload-demo-postgres.yaml (resources.
# memMib). Changing one without the other silently mis-scales every future
# credit; the historical total was accrued at whatever the rate was then, so
# only new accrual moves.
_DEMO_PG_SAVINGS_MIB_PER_S = 150.0
# Below this gap, skip the write entirely: the status endpoint is polled
# sub-second, and a banked VM's state/generation do not change between polls,
# so without a throttle every poll would issue an UPDATE for zero new credit.
_DEMO_PG_SAVINGS_WRITE_THROTTLE_S = 5.0


def _as_utc(dt: datetime) -> datetime:
    """SQLite test fixtures round-trip TIMESTAMPTZ as naive; Postgres is tz-aware."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def record_demo_pg_savings_core(
    session: Session, *, state: str | None, generation: int | None, now: datetime
) -> float:
    """Credit elapsed sleep time into the single all-time row. Sync; to_thread.

    Loads (or creates) the id=1 row, credits it per the banked-to-banked
    same-generation rule above, and persists the new sample unless the sample
    is an unchanged-state, unchanged-generation, sub-throttle-window repeat (in
    which case nothing is written and the current total is simply returned).
    """
    row = session.get(DemoPgSavings, 1)
    if row is None:
        row = DemoPgSavings(id=1, total_mib_seconds=0.0)
        session.add(row)

    unchanged = state == row.last_state and generation == row.last_generation
    if unchanged and row.last_sample_at is not None:
        gap_s = (now - _as_utc(row.last_sample_at)).total_seconds()
        if 0 <= gap_s < _DEMO_PG_SAVINGS_WRITE_THROTTLE_S:
            return row.total_mib_seconds

    if (
        row.last_sample_at is not None
        and row.last_state == "banked"
        and state == "banked"
        and generation == row.last_generation
    ):
        elapsed_s = (now - _as_utc(row.last_sample_at)).total_seconds()
        row.total_mib_seconds += max(0.0, elapsed_s) * _DEMO_PG_SAVINGS_MIB_PER_S

    row.last_sample_at = now
    row.last_state = state
    row.last_generation = generation
    session.commit()
    return row.total_mib_seconds


def _record_demo_pg_savings_sync(state: str | None, generation: int | None) -> float:
    """Opens its own Session against the writer engine (public_writer on the
    public tier, the default app engine on the private tier); never receives
    a session from the caller's thread."""
    with Session(get_savings_engine()) as session:
        return record_demo_pg_savings_core(
            session, state=state, generation=generation, now=datetime.now(timezone.utc)
        )


async def record_demo_pg_savings(
    state: str | None, generation: int | None
) -> float | None:
    """Best-effort: a missing table (pre-migration) must not break status polls."""
    try:
        return await asyncio.to_thread(_record_demo_pg_savings_sync, state, generation)
    except Exception as exc:  # noqa: BLE001 - accrual is best-effort, never fatal
        logger.warning("demo-postgres savings accrual failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# GET /savings: a 30s in-process cache over a plain SELECT of the singleton
# demo_pg_savings row. Reads always use the DEFAULT reader engine
# (core.db.get_engine, public_reader on the replica): SELECT works fine on the
# replica, and reserving the writer engine for accrual keeps the read path
# off the primary. Missing table (pre-migration) or any error degrades to
# total_saved_mib_s: None, never a 5xx (mirrors the status endpoint's
# in-band error posture).
# ---------------------------------------------------------------------------

_SAVINGS_CACHE_TTL_S = 30.0
_savings_cache_lock = asyncio.Lock()
_savings_cache: dict = {"at": None, "total_saved_mib_s": None, "as_of": None}


def _read_demo_pg_savings_sync() -> float | None:
    with Session(get_engine()) as session:
        row = session.get(DemoPgSavings, 1)
        return row.total_mib_seconds if row is not None else None


async def cached_demo_pg_savings() -> dict:
    """Single-flight, 30s-TTL-cached read of the all-time savings counter.

    Returns {"total_saved_mib_s": float | None, "as_of": iso8601}; as_of is
    when the value was actually read from the DB (the cached_at timestamp),
    not the current time, so a stale-but-still-fresh cached response is
    honest about its age.
    """
    async with _savings_cache_lock:
        now = monotonic()
        cached_at = _savings_cache["at"]
        if cached_at is not None and (now - cached_at) < _SAVINGS_CACHE_TTL_S:
            return {
                "total_saved_mib_s": _savings_cache["total_saved_mib_s"],
                "as_of": _savings_cache["as_of"],
            }

        try:
            total = await asyncio.to_thread(_read_demo_pg_savings_sync)
        except Exception as exc:  # noqa: BLE001 - a read failure is data, not a fault
            logger.warning("demo-postgres savings read failed: %s", exc)
            total = None

        as_of = datetime.now(timezone.utc).isoformat()
        _savings_cache["at"] = now
        _savings_cache["total_saved_mib_s"] = total
        _savings_cache["as_of"] = as_of
        return {"total_saved_mib_s": total, "as_of": as_of}


def classify_wake(before: dict | None) -> str:
    """Predict this query's boot path from the PRE-query lifecycle state.

    The StartStateful response's was_relight never reaches the management API,
    so the demo classifies from what the workload looked like the instant
    before the connect: a serving instance answers warm, a banked bundle whose
    stamped generation still pairs with the volume relights, a broken pair or
    no instance at all cold-boots. A mid-transition snapshot (banking,
    relighting, ...) is reported as such rather than guessed at.
    """
    if before is None:
        return "unknown"
    state = before.get("state")
    if state == "serving":
        return "warm"
    if state == "banked":
        return "relight" if before.get("pair_valid") else "cold"
    if not state:
        return "cold"
    return "transitional"


# The fixed menu keeps the demo zero-typing: an insert picks a random line item
# server-side. Prices are illustrative; the aggregate query is the point.
_DEMO_PG_MENU = [
    ("flat white", 3.50),
    ("mechanical keyboard", 89.00),
    ("rubber duck", 1.20),
    ("gpu", 1999.00),
    ("ergonomic chair", 349.00),
    ("sticker pack", 4.75),
]

_DEMO_PG_DDL = (
    "CREATE TABLE IF NOT EXISTS demo_orders ("
    "  id bigserial PRIMARY KEY,"
    "  item text NOT NULL,"
    "  qty int NOT NULL,"
    "  unit_price numeric(8,2) NOT NULL,"
    "  written_at timestamptz NOT NULL DEFAULT now(),"
    "  postmaster_start timestamptz NOT NULL,"
    "  session_tag text)"
)

# The prod table predates session_tag; this idempotent ALTER backfills it on
# every roundtrip so existing deployments pick up the column without a
# separate migration step.
_DEMO_PG_ADD_SESSION_TAG = (
    "ALTER TABLE demo_orders ADD COLUMN IF NOT EXISTS session_tag text"
)

# The ledger is a rolling one-hour window: this sweep runs lazily on the
# first query past the hour (the VM may be asleep at the stroke of the hour,
# and nothing can observe the data until a query wakes it anyway).
_DEMO_PG_SWEEP = "DELETE FROM demo_orders WHERE written_at < date_trunc('hour', now())"

_DEMO_PG_INSERT = (
    "INSERT INTO demo_orders (item, qty, unit_price, postmaster_start, session_tag) "
    "VALUES (%s, %s, %s, pg_postmaster_start_time(), %s) RETURNING id"
)

_DEMO_PG_RECENT = (
    "SELECT id, item, qty, unit_price, written_at, postmaster_start, session_tag "
    "FROM demo_orders ORDER BY id DESC LIMIT %s"
)

_DEMO_PG_AGGREGATE = (
    "SELECT item, sum(qty) AS units, sum(qty * unit_price) AS revenue "
    "FROM demo_orders GROUP BY item ORDER BY revenue DESC"
)

_DEMO_PG_TOTALS = (
    "SELECT count(*), coalesce(sum(qty * unit_price), 0), "
    "pg_postmaster_start_time() FROM demo_orders"
)


def demo_pg_orders_roundtrip(dsn: str, mode: str, session_tag: str | None) -> dict:
    """Connect, run the mode's statements, and time each one. Sync; to_thread.

    connect_ms is the wake (the activator parks the TCP connect while the VM
    relights or cold-boots); each executed statement is returned verbatim with
    its own wall time so the UI can show the SQL that just ran. The connection
    is short-lived by design: an open connection pins the VM awake.

    insert    - DDL-if-missing, sweep the ledger to its rolling one-hour
                window, append a random menu line item, then read the
                recent rows, the aggregate breakdown, and the totals.
    aggregate - read-only: the same reads without writing anything, proving a
                wake needs no write.
    """
    started = perf_counter()
    conn = psycopg.connect(dsn, connect_timeout=_DEMO_PG_CONNECT_TIMEOUT_S)
    connect_ms = (perf_counter() - started) * 1000

    statements: list[dict] = []

    def run(cur, sql: str, params=None):
        stmt_started = perf_counter()
        cur.execute(sql, params)
        statements.append({"sql": sql, "ms": (perf_counter() - stmt_started) * 1000})

    inserted = None
    query_started = perf_counter()
    # psycopg3: the connection context commits on clean exit AND closes.
    with conn, conn.cursor() as cur:
        run(cur, _DEMO_PG_DDL)
        run(cur, _DEMO_PG_ADD_SESSION_TAG)
        run(cur, _DEMO_PG_SWEEP)
        if mode == "insert":
            item, unit_price = random.choice(_DEMO_PG_MENU)
            qty = random.randint(1, 5)
            run(cur, _DEMO_PG_INSERT, (item, qty, unit_price, session_tag))
            inserted = {
                "id": cur.fetchone()[0],
                "item": item,
                "qty": qty,
                "unit_price": unit_price,
            }
        run(cur, _DEMO_PG_RECENT, (_DEMO_PG_HISTORY_ROWS,))
        rows = [
            {
                "id": r[0],
                "item": r[1],
                "qty": r[2],
                "unit_price": float(r[3]),
                "written_at": r[4].isoformat(),
                "postmaster_start": r[5].isoformat(),
                "yours": bool(session_tag and r[6] == session_tag),
            }
            for r in cur.fetchall()
        ]
        run(cur, _DEMO_PG_AGGREGATE)
        breakdown = [
            {"item": r[0], "units": int(r[1]), "revenue": float(r[2])}
            for r in cur.fetchall()
        ]
        run(cur, _DEMO_PG_TOTALS)
        total_orders, total_revenue, postmaster_start = cur.fetchone()
    query_ms = (perf_counter() - query_started) * 1000

    return {
        "connect_ms": connect_ms,
        "query_ms": query_ms,
        "mode": mode,
        "statements": statements,
        "inserted": inserted,
        "rows": rows,
        "breakdown": breakdown,
        "total_orders": total_orders,
        "total_revenue": float(total_revenue),
        "postmaster_start": postmaster_start.isoformat(),
    }


async def destroy_demo_pg_instance() -> dict:
    """DELETE .../instance: destroys the live VM AND evicts the banked bundle.

    The volume is untouched, so the next connect cold-boots Postgres against
    the retained data. Wrapped here so the private-only reset endpoint
    (demos/firecracker_api.py) does not duplicate the EMBERVM_URL call shape;
    the endpoint itself (auth posture, 503-if-unconfigured) stays private.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"{EMBERVM_URL}/v1/stateful/{_DEMO_PG_WORKLOAD}/instance",
            headers=auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()
