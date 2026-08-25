from __future__ import annotations

import os

from swarm import config

_dbos = None
_launched = False


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
        instance.launch()
        _launched = True


def shutdown() -> None:
    global _dbos, _launched
    if _dbos is not None and _enabled() and os.environ.get("DATABASE_URL"):
        _dbos.destroy()
        _dbos = None
        _launched = False
