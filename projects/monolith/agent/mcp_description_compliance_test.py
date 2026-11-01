"""Asserts every registered MCP tool description survives Context Forge sanitization.

Context Forge (the in-cluster MCP gateway at
``context-forge-gateway-mcp-stack-mcpgateway`` in namespace ``mcp``)
runs a security validator on every tool description it discovers.
Tools whose description trips any of the rules below are silently
dropped from the proxied surface, with only an ERROR log line in the
gateway pod. The result is partial-discovery (e.g. 14 of 16 tools
visible to Claude.ai) that is invisible from monolith's side.

This test asserts at CI time that no future MCP tool addition can
sneak in a forbidden pattern and ship to production with silent
discovery loss. The forbidden-substring list is not the only rule:
descriptions must also survive the gateway's display-text validators
(HTML tags, script-scheme/JS patterns, polyglot sequences, and nested
percent-encoding), mirrored below from the pinned gateway sources.

The deployed list lives in this repo, not just in the gateway image:
``projects/mcp/context-forge-gateway/chart/charts/mcp-stack-1.0.7.tgz``
-> ``mcp-stack/values.yaml`` ``TOOL_DESCRIPTION_FORBIDDEN_PATTERNS``, with
``VALIDATION_STRICT: "true"`` alongside it, which is what makes the gateway
raise rather than warn. Neither of this chart's own values files overrides
either key. If Context Forge is upgraded and the rules change, update the
constants below against that path.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

import pytest

# A deliberate SUPERSET of the deployed list, which is
# ["&&", ";", "||", "$(", "> ", "< "]. The bare "|" is ours: it is not
# refused today, but a description carrying one is a pipe away from the
# "> " and "< " that are, and nothing in the monolith needs it. Do not
# "correct" this down to six. Backticks are explicitly allowed by the
# gateway (commonly used for Markdown inline code).
FORBIDDEN_PATTERNS = ["&&", ";", "||", "$(", "|", "> ", "< "]

# From mcpgateway/common/validators.py — gateway default is 8192 (8KB).
# Real cap could differ if the gateway is configured with a smaller
# value, but we assert against the default; a smaller cap would
# similarly catch over-long descriptions in CI.
MAX_DESCRIPTION_LENGTH = 8192

# Mirror of the gateway's display-text validators, verbatim from the pinned
# image (ghcr.io/ibm/mcp-context-forge:v1.0.7). The two patterns below are
# the defaults of validation_dangerous_html_pattern and
# validation_dangerous_js_pattern in mcpgateway/config.py, and
# POLYGLOT_PATTERNS is the _POLYGLOT_PATTERNS list in
# mcpgateway/common/validators.py. ToolCreate.validate_description (in
# mcpgateway/schemas.py) returns SecurityValidator.sanitize_display_text,
# so anything that helper raises on never reaches the proxied catalogue.
# Do not "simplify" these; if the gateway tag moves, re-derive them.
DANGEROUS_HTML_PATTERN = r"<(script|iframe|object|embed|link|meta|base|form|img|svg|video|audio|source|track|area|map|canvas|applet|frame|frameset|html|head|body|style)\b|</*(script|iframe|object|embed|link|meta|base|form|img|svg|video|audio|source|track|area|map|canvas|applet|frame|frameset|html|head|body|style)>"
DANGEROUS_JS_PATTERN = r"(?i)(?:^|\s|[\"'`<>=])(javascript:|vbscript:|data:\s*[^,]*[;\s]*(javascript|vbscript)|\bon[a-z]+\s*=|<\s*script\b)"
POLYGLOT_PATTERNS = [
    re.compile(r"['\"];.*alert\s*\(", re.IGNORECASE),
    re.compile(r"-->\s*<[^>]+>", re.IGNORECASE),
    re.compile(r"['\"].*//['\"]", re.IGNORECASE),
    re.compile(r"<<[A-Z]+>", re.IGNORECASE),
    re.compile(r"String\.fromCharCode", re.IGNORECASE),
    re.compile(r"javascript:.*\(", re.IGNORECASE),
]


def _display_violations(desc: str) -> list[str]:
    """Reasons desc would fail the gateway display-text validator.

    Mirrors SecurityValidator.sanitize_display_text as implemented at
    v1.0.7 (mcpgateway/common/validators.py): decode once when '%' is
    present and flag nested percent-encoding (its _decode_strict raises
    when a second unquote still changes the text, blocking the
    %253Cscript%253E bypass class), then run the HTML, script-scheme/JS
    and polyglot searches against the decoded text. Returns one reason
    per hit instead of raising on the first, so a failure names every
    matched validator; empty means the gateway keeps the description.
    """
    reasons: list[str] = []
    if "%" in desc:
        # Single decode pass, like the gateway's _unquote_if_needed.
        text = unquote(desc)
        if text != desc and unquote(text) != text:
            reasons.append("double percent-encoded (gateway rejects nested encoding)")
    else:
        text = desc
    html = re.search(DANGEROUS_HTML_PATTERN, text, re.IGNORECASE)
    if html:
        reasons.append(f"matches gateway HTML pattern {html.group(0)!r}")
    js = re.search(DANGEROUS_JS_PATTERN, text, re.IGNORECASE)
    if js:
        reasons.append(f"matches gateway script-scheme/JS pattern {js.group(0)!r}")
    for pattern in POLYGLOT_PATTERNS:
        poly = pattern.search(text)
        if poly:
            reasons.append(
                f"matches gateway polyglot pattern {pattern.pattern!r} "
                f"({poly.group(0)!r})"
            )
    return reasons


def _register_all_tools() -> int:
    """Register every composed module's MCP tools; return how many modules did.

    Derived from the FastMonolith module registry (the single source of truth
    for tool registration since app/main.py became a build_app call) instead
    of hand-listed here, so a newly added tool module is covered
    automatically. A hand-maintained list previously omitted ``cluster.mcp``
    and shipped the k8s-* tools unvalidated (the gateway then silently dropped
    four of them); reading the real registration source closes that gap.
    """
    from app.modules_private import ALL_MODULES

    registered = 0
    for module in ALL_MODULES:
        if module.register_mcp is not None:
            module.register_mcp()
            registered += 1
    return registered


# A non-zero module count proves only that some module declared a
# register_mcp callable, not that any tool reached the shared instance.
# register_mcp bodies are side-effect imports (factory/module.py registers
# factory.execution.mcp AND factory.orchestration.mcp), so dropping one line
# would take a whole domain out of this test's field of view while leaving
# the count non-zero and the run green. These sentinels span four separate
# register_mcp bodies, including the orchestration module that carried the
# semicolons this test was wired up for.
SENTINEL_TOOLS = {
    "factory_control",
    "factory_configure_refill",
    "factory_amend_policy",
    "k8s_list_resources",
    "monolith_agent_notify",
    "search_knowledge",
}


@pytest.mark.asyncio
async def test_all_mcp_tool_descriptions_pass_context_forge_validation():
    """Every registered tool's description must be Context Forge-safe."""
    registered = _register_all_tools()
    assert registered, (
        "no module in app.modules_private.ALL_MODULES declares register_mcp; "
        "the test cannot see the tool set"
    )
    from core.mcp_app import mcp

    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    missing = sorted(SENTINEL_TOOLS - names)
    assert not missing, (
        f"registered {registered} module(s) but {missing} never reached "
        "core.mcp_app.mcp, so this test is scanning a partial catalogue and "
        "would pass while whole domains go unchecked"
    )

    violations: list[str] = []
    for tool in tools:
        desc = tool.description or ""
        for pat in FORBIDDEN_PATTERNS:
            if pat in desc:
                violations.append(f"  {tool.name}: contains forbidden pattern {pat!r}")
        if len(desc) > MAX_DESCRIPTION_LENGTH:
            violations.append(
                f"  {tool.name}: description too long "
                f"({len(desc)} > {MAX_DESCRIPTION_LENGTH})"
            )
        for reason in _display_violations(desc):
            violations.append(f"  {tool.name}: {reason}")
    assert not violations, (
        "These MCP tool descriptions will be silently dropped by Context Forge:\n"
        + "\n".join(violations)
        + "\n\nFix: rephrase the docstring to avoid the listed pattern. "
        "See the tool catalogue refresh section of projects/mcp/ARCHITECTURE.md."
    )


# Rejections the helper must catch: each trips at least one gateway
# display-text validator. The backticked accept case below is the live
# shape (the run_code docstring wraps ``javascript`` in backticks for
# exactly this reason), so these two cases pin both sides of that line.
@pytest.mark.parametrize(
    "desc",
    [
        "- javascript: x",
        "pass <source> here",
        "<form>",
        "String.fromCharCode(65)",
        "x onclick=y",
        "%253Cscript%253E",
    ],
)
def test_display_text_helper_rejects_gateway_dangerous_text(desc: str):
    """_display_violations flags text the gateway display validator drops."""
    assert _display_violations(desc), (
        f"{desc!r} produced no display-text violation; the helper no longer "
        "mirrors the gateway and would pass a description Context Forge drops"
    )


# Benign shapes the gateway keeps: backticked ``javascript`` (no colon right
# after the scheme word), a non-listed tag like <b> (the HTML pattern names
# its tags), and a bare percent sign (unquote leaves "50% off" unchanged, so
# no double encoding).
@pytest.mark.parametrize(
    "desc",
    [
        "- `javascript`: main.js",
        "Hello <b>World</b>",
        "50% off",
    ],
)
def test_display_text_helper_accepts_safe_text(desc: str):
    """_display_violations stays quiet on text the gateway keeps."""
    assert _display_violations(desc) == [], (
        f"{desc!r} produced {_display_violations(desc)!r}; the helper is "
        "stricter than the gateway and would block a safe description"
    )
