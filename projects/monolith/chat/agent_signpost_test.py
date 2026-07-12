"""Behavioral tests for signpost injection in agent.py.

At request time the agent's ``prepare_tools`` callback rewrites each tool's
ToolDefinition description to append 'USE WHEN: <signpost>'. The enriched
descriptions are injected into the chat template's <tools> block by vLLM, so
no separate system prompt is needed.

The rewrite itself lives in the pure, module-level ``apply_signposts()``
function; ``create_agent()`` snapshots the ``{tool_name: signpost}`` map from
the registered tools (via ``_collect_signposts``) and the callback closes over
it. These tests exercise both: the real signpost map collected from a built
agent, and ``apply_signposts`` directly. (pydantic-ai 1.107 removed the
``Agent._prepare_tools`` internal the old tests called; the pure function is
the supported, private-API-free replacement.)
"""

from pydantic_ai import ToolDefinition

from chat.agent import _collect_signposts, apply_signposts, create_agent


def _real_signpost_map() -> dict[str, str]:
    """The signpost map a built concierge agent actually registers."""
    agent = create_agent(base_url="http://fake:8080")
    return _collect_signposts(agent)


# ---------------------------------------------------------------------------
# apply_signposts() with the real signpost map from a built agent
# ---------------------------------------------------------------------------


class TestInjectSignpostsBehavior:
    def test_web_search_description_gets_use_when_suffix(self):
        """apply_signposts() appends 'USE WHEN: ...' to web_search description."""
        td = ToolDefinition(
            name="web_search",
            description="Search the web for current information.",
            parameters_json_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        )

        updated = apply_signposts([td], _real_signpost_map())

        assert len(updated) == 1
        assert "USE WHEN:" in updated[0].description
        assert updated[0].name == "web_search"

    def test_search_history_description_gets_use_when_suffix(self):
        """apply_signposts() appends 'USE WHEN: ...' to search_history description."""
        td = ToolDefinition(
            name="search_history",
            description="Search older messages in this channel by topic.",
            parameters_json_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        )

        updated = apply_signposts([td], _real_signpost_map())

        assert len(updated) == 1
        assert "USE WHEN:" in updated[0].description
        assert updated[0].name == "search_history"

    def test_get_user_summary_description_gets_use_when_suffix(self):
        """apply_signposts() appends 'USE WHEN: ...' to get_user_summary description."""
        td = ToolDefinition(
            name="get_user_summary",
            description="Get user activity summaries.",
            parameters_json_schema={"type": "object", "properties": {}},
        )

        updated = apply_signposts([td], _real_signpost_map())

        assert len(updated) == 1
        assert "USE WHEN:" in updated[0].description
        assert updated[0].name == "get_user_summary"

    def test_all_three_tools_get_use_when_suffix(self):
        """apply_signposts() rewrites descriptions for all three registered tools."""
        tool_defs = [
            ToolDefinition(
                name="web_search",
                description="Search the web.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            ),
            ToolDefinition(
                name="search_history",
                description="Search channel history.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            ),
            ToolDefinition(
                name="get_user_summary",
                description="Get user summaries.",
                parameters_json_schema={"type": "object", "properties": {}},
            ),
        ]

        updated = apply_signposts(tool_defs, _real_signpost_map())

        assert len(updated) == 3
        for td in updated:
            assert "USE WHEN:" in td.description, (
                f"Tool '{td.name}' description missing 'USE WHEN:': {td.description!r}"
            )

    def test_original_description_is_preserved_before_use_when(self):
        """apply_signposts() keeps the original description before the 'USE WHEN:' suffix."""
        original_desc = "Search the web for current information."

        td = ToolDefinition(
            name="web_search",
            description=original_desc,
            parameters_json_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        )

        updated = apply_signposts([td], _real_signpost_map())

        assert updated[0].description.startswith(original_desc)

    def test_unknown_tool_name_passthrough_unchanged(self):
        """apply_signposts() passes through ToolDefinitions for unknown tool names unchanged."""
        original_desc = "Some hypothetical tool."

        td = ToolDefinition(
            name="nonexistent_tool",
            description=original_desc,
            parameters_json_schema={"type": "object", "properties": {}},
        )

        updated = apply_signposts([td], _real_signpost_map())

        assert len(updated) == 1
        assert updated[0].description == original_desc
        assert "USE WHEN:" not in updated[0].description

    def test_empty_tool_list_returns_empty_list(self):
        """apply_signposts() returns an empty list when given no tool definitions."""
        updated = apply_signposts([], _real_signpost_map())
        assert updated == []


# ---------------------------------------------------------------------------
# _collect_signposts() wiring: create_agent actually registers the signposts
# ---------------------------------------------------------------------------


class TestSignpostMapWiring:
    def test_built_agent_registers_expected_signposts(self):
        """create_agent() registers signposts for the core tools."""
        signpost_map = _real_signpost_map()

        for name in ("web_search", "search_history", "get_user_summary"):
            assert name in signpost_map, (
                f"expected a signpost for {name!r}, got keys {sorted(signpost_map)}"
            )
            assert signpost_map[name].strip(), f"empty signpost for {name!r}"

    def test_apply_signposts_is_pure_and_needs_no_agent(self):
        """apply_signposts() works with a hand-built map, no agent internals."""
        td = ToolDefinition(
            name="my_tool",
            description="Do a thing.",
            parameters_json_schema={"type": "object", "properties": {}},
        )

        updated = apply_signposts([td], {"my_tool": "when you need a thing"})

        assert updated[0].description == "Do a thing. USE WHEN: when you need a thing"
