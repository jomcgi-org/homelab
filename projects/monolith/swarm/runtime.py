from __future__ import annotations

import os
import threading

from swarm import config

_dbos = None
_launched = False
_read_client = None
# read_client() is the one accessor built on the REQUEST path rather than at
# startup, and get_run/list_runs are sync endpoints, so FastAPI runs them on the
# threadpool. A burst of console polls against a cold follower would otherwise
# race check-then-act on _read_client, construct two clients, and leak the
# loser's connection pool with no destroy().
_read_client_lock = threading.Lock()


def _enabled() -> bool:
    from agent.config import drainer_enabled

    return config.enabled() or drainer_enabled()


def init_dbos():
    global _dbos
    if _dbos is not None or not _enabled():
        return _dbos
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None
    from dbos import DBOS, DBOSConfig

    _dbos = DBOS(
        config=DBOSConfig(
            name="monolith",
            system_database_url=database_url,
            dbos_system_schema="dbos",
        )
    )
    return _dbos


def read_client():
    """A read-only handle on the DBOS system database, for ANY replica.

    DBOS launches on the leader only, and its system database is constructed
    inside launch() (dbos/_dbos.py builds _sys_db_field there, and the property
    raises "System database accessed before DBOS was launched" until it does).
    So a follower cannot answer even a pure read through the DBOS instance, and
    the run surfaces are pure reads. Gating them on leadership behind a
    round-robin Service made roughly half of every console poll 503, which the
    browser renders as "engine: unreachable".

    DBOSClient is the supported way to read a DBOS application's state from
    outside it: system database only, no migrations, no workflow registration,
    and no queue consumption, so a follower holding one still cannot execute
    anything. Returns None when swarm is disabled or DATABASE_URL is unset,
    matching init_dbos.
    """
    global _read_client
    if _read_client is not None or not _enabled():
        return _read_client
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None
    from dbos import DBOSClient

    with _read_client_lock:
        # Re-check inside the lock: a caller that blocked here while another
        # thread built the client must return that one, not build a second.
        if _read_client is not None:
            return _read_client
        _read_client = DBOSClient(
            system_database_url=database_url,
            dbos_system_schema="dbos",
            # Deliberately small. This pool exists on every replica that
            # never launches DBOS and serves console polling, not work. The
            # leader never builds one at all: it already has the launched
            # instance's pool.
            system_database_pool_size=2,
        )
    return _read_client


def is_launched() -> bool:
    """True only on a replica that actually launched DBOS.

    DBOS launches on the LEADER only, but every replica serves the router, so a
    request can land on a follower. Constructing a DBOS instance there and
    calling start_workflow on it would submit against an unlaunched runtime, so
    callers gate on this instead of on init_dbos() returning non-None.
    """
    return _launched


def launch() -> None:
    global _launched
    instance = init_dbos()
    if instance is not None and not _launched:
        # Construct the Queue objects BEFORE launching. A Queue registers
        # itself with DBOS when it is constructed, and swarm/queues.py builds
        # them lazily, so without this the process launches with no queues and
        # logs "Listening to 0 queues". The queue thread does re-read the
        # registry, so a later construction is eventually picked up, but that
        # left the first registration depending on whichever request path
        # happened to call for a queue first. A pod that rolled holding a
        # backlog then sat idle until something triggered that call.
        from swarm.queues import get_queues

        get_queues()
        instance.launch()
        _launched = True


def shutdown() -> None:
    global _dbos, _launched, _read_client
    if _read_client is not None:
        _read_client.destroy()
        _read_client = None
    if _dbos is not None and _enabled() and os.environ.get("DATABASE_URL"):
        _dbos.destroy()
        _dbos = None
        _launched = False
