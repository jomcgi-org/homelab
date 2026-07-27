from framework import Module as _Module


def _register_mcp() -> None:
    """Attach agent_sessions MCP tools to the shared instance."""
    import agent_sessions.mcp  # noqa: F401, PLC0415


MODULE = _Module(
    name="agent_sessions",
    register_mcp=_register_mcp,
)
