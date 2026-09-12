from framework import Module as _Module
from framework import register_leader_tasks

from agent_sessions.provider_quota import provider_quota_health


def register(app) -> None:
    """Register the agent_sessions HTTP router with the app."""
    from agent_sessions.public_router import router as public_router
    from agent_sessions.router import router

    app.include_router(router)
    app.include_router(public_router)


def _register_mcp() -> None:
    """Attach agent_sessions MCP tools to the shared instance."""
    import agent_sessions.mcp  # noqa: F401, PLC0415


async def _leader_start(app):
    """Start leader-owned agent session maintenance loops."""
    from agent_sessions.execution_api import start_receipt_cleanup_loop
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
        start_receipt_cleanup_loop,
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
    name="agent_sessions",
    register=register,
    register_mcp=_register_mcp,
    shutdown=_shutdown,
    leader_start=_leader_start,
    register_health_advisory={"provider_quota": provider_quota_health},
)
