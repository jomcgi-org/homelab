from __future__ import annotations

import logging
import os
import threading
import time

from swarm import config

logger = logging.getLogger(__name__)

_dbos = None
_launched = False
_read_client = None
_read_client_error: Exception | None = None
_read_client_retry_at = 0.0
_READ_CLIENT_RETRY_SECONDS = 5.0
# read_client() is the one accessor built on the REQUEST path rather than at
# startup, and get_run/list_runs are sync endpoints, so FastAPI runs them on the
# threadpool. The lock protects publication and failure backoff state only.
# DBOSClient construction performs a real database query and must stay outside
# the lock so one cold connection cannot serialize every follower read.
_read_client_lock = threading.Lock()
_monotonic = time.monotonic


def _enabled() -> bool:
    from agent.config import drainer_enabled

    return config.enabled() or drainer_enabled()


def _node_workflow_members() -> tuple:
    """The functions that define a factory node workflow's durable shape.

    DBOS records each step by its position in the workflow, so recovery is only
    safe when the body and the steps around those positions are unchanged.
    That means execute_node itself, the workflow-level helpers that decide
    which steps run and how many times, and every step they call. Pure helpers
    that only shape a step's arguments are deliberately absent: a replay
    returns the recorded output, so they cannot move a checkpoint.
    """
    from swarm import node_workflows, steps

    return (
        node_workflows.execute_node,
        node_workflows._await_node_turn,
        node_workflows._await_dispatched_node_turn,
        node_workflows._reconciled_identity,
        node_workflows._start_node_session,
        node_workflows._reconcile_session,
        node_workflows._read_turn_artifact,
        node_workflows._read_node_dispatch,
        node_workflows._cleanup_node,
        steps.observe_clock,
        steps.poll_turn,
        steps.read_branch_head,
    )


def node_workflow_version() -> str | None:
    """An application version derived from the factory node workflow alone.

    DBOS computes its default version from the source of EVERY registered
    workflow (compute_app_version in dbos/_dbos.py), so editing any workflow in
    this process changes the version, and DBOS then neither recovers nor
    dequeues anything the previous version started. That stranded in-flight
    factory nodes on deploys that never touched them.

    The node workflow is the one whose in-flight runs have to survive a deploy,
    so the version is computed from its own durable shape instead. The
    construction follows DBOS: MD5 over the member sources in sorted order,
    with the DBOS package version mixed in last so a library upgrade still
    changes the version.

    The trade is deliberate. Every other workflow in the process now keeps its
    version across a deploy that changed it, so a changed body can be recovered
    against recorded steps. DBOS detects that as a step mismatch and raises
    DBOSUnexpectedStepError rather than replaying silently, so the failure is
    loud.

    Returns None when the source cannot be read, which leaves DBOS to compute
    its own version exactly as it did before.
    """
    import hashlib
    import inspect

    from dbos._utils import GlobalParams

    try:
        sources = sorted(
            inspect.getsource(member) for member in _node_workflow_members()
        )
    except Exception:  # noqa: BLE001 - an unreadable source is not a version
        logger.warning(
            "could not read the node workflow source, leaving DBOS to compute "
            "its own application version",
            exc_info=True,
        )
        return None
    sources.append(GlobalParams.dbos_version)
    hasher = hashlib.md5()
    for source in sources:
        hasher.update(source.encode("utf-8"))
    return hasher.hexdigest()


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
            # DBOS ignores a None here and computes its own version.
            application_version=node_workflow_version(),
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
    global _read_client, _read_client_error, _read_client_retry_at
    if _read_client is not None or not _enabled():
        return _read_client
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None
    from dbos import DBOSClient

    now = _monotonic()
    with _read_client_lock:
        if _read_client is not None:
            return _read_client
        if _read_client_error is not None and now < _read_client_retry_at:
            raise _read_client_error

    try:
        candidate = DBOSClient(
            system_database_url=database_url,
            dbos_system_schema="dbos",
            # Deliberately small. This pool exists on every replica that
            # never launches DBOS and serves console polling, not work. The
            # leader never builds one at all: it already has the launched
            # instance's pool.
            system_database_pool_size=2,
        )
    except Exception as error:
        with _read_client_lock:
            # A concurrent constructor may have succeeded while this attempt
            # failed. Prefer its usable client over publishing stale failure
            # state.
            if _read_client is not None:
                return _read_client
            _read_client_error = error
            _read_client_retry_at = _monotonic() + _READ_CLIENT_RETRY_SECONDS
        raise

    with _read_client_lock:
        if _read_client is None:
            _read_client = candidate
            _read_client_error = None
            _read_client_retry_at = 0.0
            return candidate
        winner = _read_client

    # DBOSClient.destroy() is safe here: each default client owns its engine,
    # and use_listen_notify defaults to False, so this only disposes the losing
    # candidate's connection pool.
    candidate.destroy()
    return winner


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
    global _read_client_error, _read_client_retry_at
    if _read_client is not None:
        _read_client.destroy()
        _read_client = None
    _read_client_error = None
    _read_client_retry_at = 0.0
    if _dbos is not None and _enabled() and os.environ.get("DATABASE_URL"):
        _dbos.destroy()
        _dbos = None
        _launched = False
