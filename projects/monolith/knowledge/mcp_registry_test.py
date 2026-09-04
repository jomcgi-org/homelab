"""The knowledge MCP catalogue registers at the definition site.

Tools used to register via @mcp.tool at import time, so defining one was
enough. Splitting the catalogue across tiers meant registration had to become
explicit, and an explicit LIST is a thing to forget: a tool absent from it does
not error, it silently vanishes from that tier. These tests pin the marker so
the list cannot drift from the definitions.
"""

from __future__ import annotations

import inspect

from knowledge import mcp as knowledge_mcp


def test_every_marked_tool_is_registered():
    class _Recorder:
        def __init__(self) -> None:
            self.tools: list = []

        def add_tool(self, tool) -> None:
            self.tools.append(tool)

    recorder = _Recorder()
    knowledge_mcp.register_mcp_tools(recorder)

    assert recorder.tools == knowledge_mcp._KNOWLEDGE_TOOLS
    assert len(recorder.tools) == len(set(recorder.tools)), "a tool registered twice"


def test_the_catalogue_is_not_empty_and_names_are_unique():
    names = [tool.__name__ for tool in knowledge_mcp._KNOWLEDGE_TOOLS]
    assert names, "no knowledge tools registered at all"
    assert len(names) == len(set(names)), f"duplicate tool names: {names}"


def test_public_async_functions_are_all_marked_tools():
    """A public async function here is a tool, so it must carry the marker.

    Module-internal helpers are underscore-prefixed (see _notify), which keeps
    this rule unambiguous: anything public and async that is not registered
    would ship on no MCP server at all, silently.
    """

    registered = {tool.__name__ for tool in knowledge_mcp._KNOWLEDGE_TOOLS}
    for name, obj in vars(knowledge_mcp).items():
        if name.startswith("_") or name == "register_mcp_tools":
            continue
        if not inspect.iscoroutinefunction(obj):
            continue
        if getattr(obj, "__module__", None) != knowledge_mcp.__name__:
            continue
        assert name in registered, (
            f"{name} is a public async tool in knowledge.mcp but carries no "
            "@_knowledge_tool marker, so it registers on no MCP server"
        )


def test_knowledge_mcp_does_not_import_agent_api():
    source = inspect.getsource(knowledge_mcp)
    assert "agent.api" not in source
