"""The staged plugin (copied to its own repository for the listings, #7255)
must stay one plugin: the Claude Code and OpenAI manifests agree on name and
version, both MCP configs point at the live connector, and the directory
listing URLs are the jomcgi.dev pages that exist."""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_SITE = "https://jomcgi.dev/scotland"
_MCP_URL = f"{_SITE}/mcp"


def _json(rel: str) -> dict:
    return json.loads((_DIR / rel).read_text())


def test_manifests_agree():
    claude = _json(".claude-plugin/plugin.json")
    openai = _json("plugin.json")
    assert claude["name"] == openai["name"] == "scotland"
    assert claude["version"] == openai["version"]
    assert claude["description"] == openai["description"]
    assert claude["license"] == openai["license"] == "MPL-2.0"
    assert (_DIR / "LICENSE").read_text().startswith("Mozilla Public License")


def test_both_mcp_configs_point_at_the_connector():
    claude = _json(".mcp.json")["mcpServers"]["scotland"]
    openai = _json("mcp.json")["mcpServers"]["scotland"]
    assert claude == {"type": "http", "url": _MCP_URL}
    # The Agent Plugins schema closes a streamable-http entry to type, url
    # and headers; OpenAI reads a server with no auth block as no auth.
    assert openai == {"type": "streamable-http", "url": _MCP_URL}


def test_marketplace_lists_the_plugin_at_the_root():
    market = _json(".claude-plugin/marketplace.json")
    assert [p["name"] for p in market["plugins"]] == ["scotland"]
    assert market["plugins"][0]["source"] == "./"


def test_listing_urls_are_the_connector_pages():
    claude = _json(".claude-plugin/plugin.json")
    openai = _json("plugin.json")["extensions"]["com.openai"]["interface"]
    assert claude["documentationUrl"] == openai["websiteURL"] == _SITE
    for claude_key, openai_key, page in [
        ("supportUrl", "supportURL", "support"),
        ("privacyPolicyUrl", "privacyPolicyURL", "privacy"),
        ("termsOfServiceUrl", "termsOfServiceURL", "terms"),
    ]:
        assert claude[claude_key] == openai[openai_key] == f"{_SITE}/{page}"


def test_openai_listing_fields_fit_their_limits():
    openai = _json("plugin.json")["extensions"]["com.openai"]["interface"]
    assert 0 < len(openai["displayName"]) <= 30
    assert 0 < len(openai["shortDescription"]) <= 30
    assert 0 < len(openai["longDescription"]) <= 4000
    assert 0 < len(openai["developerName"]) <= 80
    assert openai["category"]
    assert len(openai["defaultPrompt"]) <= 3
    assert all(len(p) <= 128 for p in openai["defaultPrompt"])


def test_skill_frontmatter_names_its_folder():
    skill = (_DIR / "skills/scotland-trips/SKILL.md").read_text()
    front = re.match(r"---\n(.*?)\n---\n", skill, re.DOTALL)
    assert front, "SKILL.md needs YAML frontmatter"
    assert "name: scotland-trips" in front.group(1).splitlines()
    assert any(line.startswith("description: ") for line in front.group(1).splitlines())


def _png_size(rel: str) -> tuple[int, int]:
    data = (_DIR / rel).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{rel} is not a PNG"
    return struct.unpack(">II", data[16:24])


def test_icons_are_square_pngs_both_directories_accept():
    claude = _json(".claude-plugin/plugin.json")
    openai = _json("plugin.json")["extensions"]["com.openai"]["interface"]
    assert claude["icon"] == openai["logo"] == openai["composerIcon"]
    assert openai["logoDark"] == openai["composerIconDark"]
    for rel in {claude["icon"], openai["logoDark"]}:
        w, h = _png_size(rel)
        # Claude: square, 512 to 2048 px, under 2 MB.
        assert w == h and 512 <= w <= 2048, (rel, w, h)
        assert (_DIR / rel).stat().st_size < 2 * 1024 * 1024
