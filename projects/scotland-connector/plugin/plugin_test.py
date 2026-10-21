"""The staged plugin (copied to its own repository for the listings, #7255)
must stay one plugin: the Claude Code and OpenAI manifests agree on name and
version, both MCP configs point at the live connector, and the directory
listing URLs are the jomcgi.dev pages that exist."""

from __future__ import annotations

import json
import re
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_MCP_URL = "https://jomcgi.dev/scotland/mcp"


def _json(rel: str) -> dict:
    return json.loads((_DIR / rel).read_text())


def test_manifests_agree():
    claude = _json(".claude-plugin/plugin.json")
    openai = _json("plugin.json")
    assert claude["name"] == openai["name"] == "scotland"
    assert claude["version"] == openai["version"]
    assert claude["description"] == openai["description"]


def test_both_mcp_configs_point_at_the_connector():
    claude = _json(".mcp.json")["mcpServers"]["scotland"]
    openai = _json("mcp.json")["mcpServers"]["scotland"]
    assert claude == {"type": "http", "url": _MCP_URL}
    assert openai["type"] == "streamable-http"
    assert openai["url"] == _MCP_URL
    assert openai["extensions"]["com.openai"]["auth"] == {"type": "none"}


def test_marketplace_lists_the_plugin_at_the_root():
    market = _json(".claude-plugin/marketplace.json")
    assert [p["name"] for p in market["plugins"]] == ["scotland"]
    assert market["plugins"][0]["source"] == "./"


def test_listing_urls_are_the_connector_pages():
    claude = _json(".claude-plugin/plugin.json")
    assert claude["documentationUrl"] == "https://jomcgi.dev/scotland"
    for key, page in [
        ("supportUrl", "support"),
        ("privacyPolicyUrl", "privacy"),
        ("termsOfServiceUrl", "terms"),
    ]:
        assert claude[key] == f"https://jomcgi.dev/scotland/{page}"


def test_skill_frontmatter_names_its_folder():
    skill = (_DIR / "skills/scotland-trips/SKILL.md").read_text()
    front = re.match(r"---\n(.*?)\n---\n", skill, re.DOTALL)
    assert front, "SKILL.md needs YAML frontmatter"
    assert "name: scotland-trips" in front.group(1).splitlines()
    assert any(line.startswith("description: ") for line in front.group(1).splitlines())
