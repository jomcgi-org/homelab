from pathlib import Path
import re

from app.agents_main import AGENT_TOOL_NAMES

BOARD_SCHEMAS = {
    "post_message": {"topic", "body", "ttl"},
    "read_board": {"topic", "since"},
    "ack_message": {"id"},
}


def _runfile(relative: str) -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        hit = candidate / relative
        if hit.exists():
            return hit
    raise AssertionError(f"{relative} not found in runfiles under {here}")


def _pi_tool_blocks(source: str) -> dict[str, str]:
    starts = list(re.finditer(r'mcpName: "([a-z_]+)"', source))
    return {
        match.group(1): source[
            match.start() : starts[index + 1].start()
            if index + 1 < len(starts)
            else source.index("];", match.start())
        ]
        for index, match in enumerate(starts)
    }


def test_pi_hard_coded_catalogue_exactly_matches_server_catalogue():
    source = _runfile("projects/embervm/runtimes/pi/agent-mcp.ts").read_text()
    assert tuple(_pi_tool_blocks(source)) == AGENT_TOOL_NAMES


def test_board_wire_schemas_are_pinned_in_pi_bridge():
    source = _runfile("projects/embervm/runtimes/pi/agent-mcp.ts").read_text()
    blocks = _pi_tool_blocks(source)
    for tool, fields in BOARD_SCHEMAS.items():
        assert fields == set(
            re.findall(r"^\s{6}([a-z_]+): ", blocks[tool], re.MULTILINE)
        )


def test_claude_and_codex_use_dynamic_agents_server_catalogue_and_prompt_names_tools():
    source = _runfile("projects/embervm/runtimes/claude/shim.py").read_text()
    assert '"mcpServers": {"agents":' in source
    assert "[mcp_servers.agents]" in source
    for tool in BOARD_SCHEMAS:
        assert tool in source
