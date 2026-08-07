from framework import Module as _Module


def register(app) -> None:
    """Register the agent_sessions HTTP router with the app."""
    from agent_sessions.router import router

    app.include_router(router)


def _register_mcp() -> None:
    """Attach agent_sessions MCP tools to the shared instance."""
    import agent_sessions.mcp  # noqa: F401, PLC0415


async def _leader_start(app):
    """Start the leader-owned pending-message sweep and title refresh."""
    from agent_sessions.mcp import start_pending_message_sweep
    from agent_sessions.titles import start_title_refresh_loop

    return start_pending_message_sweep() + start_title_refresh_loop()


MODULE = _Module(
    name="agent_sessions",
    register=register,
    register_mcp=_register_mcp,
    leader_start=_leader_start,
)
