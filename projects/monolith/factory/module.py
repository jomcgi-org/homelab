"""Factory composition and lifecycle, hosted inside the monolith.

Legacy swarm and agent_sessions packages are implementation details during
consolidation. Keep durable workflow functions and storage identities stable.
"""

from framework import Module as _Module
from framework import register_leader_tasks
from agent_sessions.provider_quota import provider_quota_health

from knowledge.api import kg_health
from swarm.health import drainer_health


def register(app) -> None:
    from swarm.factory_router import router as factory_router
    from swarm.drain_console_router import router as drain_console_router
    from swarm.drainer_router import router as drainer_router
    from swarm.router import router

    from agent_sessions.router import router as sessions_router
    from agent_sessions.public_router import router as public_router

    app.include_router(sessions_router)
    app.include_router(public_router)
    app.include_router(router)
    app.include_router(drainer_router)
    app.include_router(drain_console_router)
    app.include_router(factory_router)


def _register_mcp() -> None:
    """Attach the factory interaction and status MCP tools to the shared instance (side-effect import)."""
    import agent_sessions.mcp  # noqa: F401, PLC0415
    import swarm.mcp  # noqa: F401, PLC0415


async def _leader_start(app):
    from swarm import runtime
    from swarm.factory_conductor import start_loop
    from swarm import node_workflows  # noqa: F401 - register before DBOS launch

    runtime.launch()
    app.state.leader_singletons_dbos_launched = runtime.is_launched()
    tasks = start_loop() if runtime.is_launched() else []
    register_leader_tasks(app, tasks)
    # Track each task before starting the next component: partial startup must
    # leave no unowned task when the framework releases and retries the lease.
    tasks.extend(await _start_session_maintenance(app))
    return tasks


async def _leader_stop(app):
    from swarm import runtime
    from swarm.factory_conductor import disarm_watchdog

    disarm_watchdog()
    try:
        runtime.shutdown()
    finally:
        app.state.leader_singletons_dbos_launched = False


def _factory_liveness() -> dict:
    from swarm.factory_conductor import watchdog_health

    return watchdog_health()


async def _start_session_maintenance(app):
    """Start leader-owned agent session maintenance loops."""
    from agent_sessions.kg_feed import start_kg_feed_loop
    from agent_sessions.mcp import start_pending_message_sweep
    from agent_sessions.permit_supervision import start_permit_supervision_loop
    from agent_sessions.result_receipts import start_receipt_retention_loop
    from agent_sessions.titles import start_title_refresh_loop

    tasks = []
    for start in (
        start_pending_message_sweep,
        start_title_refresh_loop,
        start_kg_feed_loop,
        start_permit_supervision_loop,
        start_receipt_retention_loop,
    ):
        started = start()
        register_leader_tasks(app, started)
        tasks.extend(started)
    return tasks


async def _shutdown(_app) -> None:
    """Let in-flight turn executors record their outcome before teardown.

    Runs on every replica, not only the leader: a turn executes wherever its
    message was claimed. Bounded inside the pod's termination grace, and an
    executor that does not finish in time is covered by the claim lease exactly
    as it was before (#5938).
    """
    from agent_sessions.mcp import drain_inflight_executors

    await drain_inflight_executors()


MODULE = _Module(
    name="factory",
    leader_priority=0,
    register=register,
    register_mcp=_register_mcp,
    leader_start=_leader_start,
    leader_stop=_leader_stop,
    shutdown=_shutdown,
    register_health_advisory={
        "drainer": drainer_health,
        "kg": kg_health,
        "provider_quota": provider_quota_health,
    },
    register_liveness={"factory": _factory_liveness},
)
