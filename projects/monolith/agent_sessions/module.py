from framework import Module as _Module


def _register_mcp() -> None:
    """Attach agent_sessions MCP tools to the shared instance."""
    import agent_sessions.mcp  # noqa: F401, PLC0415


async def _leader_start(app):
    """Start the leader-owned pending-message recovery sweep."""
    from agent_sessions.mcp import start_pending_message_sweep

    return start_pending_message_sweep()


MODULE = _Module(
    name="agent_sessions",
    register_mcp=_register_mcp,
    leader_start=_leader_start,
)
