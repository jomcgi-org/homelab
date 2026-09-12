"""The MCP group gate: who may list, who may call, and the gateway carve-out."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import AuthorizationError

from auth.dependencies import reset_current_principal, set_current_principal
from auth.principal import Authority, Principal, PrincipalKind
from core import mcp_policy


def _principal(
    *,
    authority: Authority = Authority.STANDING,
    kind: PrincipalKind = PrincipalKind.HUMAN,
    groups: tuple[str, ...] = ("operators",),
) -> Principal:
    return Principal(
        subject="joe",
        actor=(),
        scope=(),
        groups=groups,
        email=None,
        kind=kind,
        authority=authority,
    )


ANONYMOUS = _principal(authority=Authority.ANONYMOUS, groups=())
OPERATOR = _principal()
OUTSIDER = _principal(groups=("family",))


def _tool(name: str, *tags: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, tags=set(tags))


GATED = _tool("factory_status")
PUBLIC = _tool("monolith_voice_ui_show", mcp_policy.PUBLIC_TAG)


def _run(principal: Principal, coro_factory):
    token = set_current_principal(principal)
    try:
        return asyncio.run(coro_factory())
    finally:
        reset_current_principal(token)


def _list_context():
    return SimpleNamespace(message=SimpleNamespace(), fastmcp_context=None)


def _call_context(name: str, tool: SimpleNamespace | None, *, with_server=True):
    server = None
    if with_server:

        async def get_tool(_requested: str):
            return tool

        server = SimpleNamespace(fastmcp=SimpleNamespace(get_tool=get_tool))
    return SimpleNamespace(message=SimpleNamespace(name=name), fastmcp_context=server)


# --- permitted ------------------------------------------------------------


def test_public_tag_admits_anyone_including_anonymous():
    assert mcp_policy.permitted(PUBLIC, ANONYMOUS)


def test_untagged_tool_requires_the_operator_group():
    assert mcp_policy.permitted(GATED, OPERATOR)
    assert not mcp_policy.permitted(GATED, OUTSIDER)
    assert not mcp_policy.permitted(GATED, ANONYMOUS)


def test_unresolvable_tool_is_denied():
    # Fail closed: a name this server could not classify is not a name it runs.
    assert not mcp_policy.permitted(None, OPERATOR)


# --- listing --------------------------------------------------------------


def test_anonymous_listing_is_unfiltered_so_the_gateway_caches_everything():
    """The load-bearing carve-out.

    Context Forge refreshes its cached catalogue anonymously. Filtering here
    would teach it the monolith serves nothing and empty the catalogue every
    real caller reads.
    """
    middleware = mcp_policy.GroupPolicyMiddleware()

    async def call_next(_context):
        return [GATED, PUBLIC]

    listed = _run(
        ANONYMOUS, lambda: middleware.on_list_tools(_list_context(), call_next)
    )
    assert [tool.name for tool in listed] == [GATED.name, PUBLIC.name]


def test_identified_caller_sees_only_what_it_may_call():
    middleware = mcp_policy.GroupPolicyMiddleware()

    async def call_next(_context):
        return [GATED, PUBLIC]

    assert [
        tool.name
        for tool in _run(
            OPERATOR, lambda: middleware.on_list_tools(_list_context(), call_next)
        )
    ] == [GATED.name, PUBLIC.name]
    assert [
        tool.name
        for tool in _run(
            OUTSIDER, lambda: middleware.on_list_tools(_list_context(), call_next)
        )
    ] == [PUBLIC.name]


# --- calling --------------------------------------------------------------


def _call(principal: Principal, name: str, tool, **kwargs):
    middleware = mcp_policy.GroupPolicyMiddleware()

    async def call_next(_context):
        return "ran"

    return _run(
        principal,
        lambda: middleware.on_call_tool(_call_context(name, tool, **kwargs), call_next),
    )


def test_operator_may_call_a_gated_tool():
    assert _call(OPERATOR, GATED.name, GATED) == "ran"


def test_anonymous_may_call_a_public_tool():
    assert _call(ANONYMOUS, PUBLIC.name, PUBLIC) == "ran"


@pytest.mark.parametrize("principal", [ANONYMOUS, OUTSIDER])
def test_gated_tool_denies_callers_without_the_group(principal):
    with pytest.raises(AuthorizationError):
        _call(principal, GATED.name, GATED)


def test_unknown_tool_name_is_denied():
    with pytest.raises(AuthorizationError):
        _call(OPERATOR, "not_a_tool", None)


def test_missing_server_context_denies_rather_than_assuming():
    with pytest.raises(AuthorizationError):
        _call(OPERATOR, GATED.name, GATED, with_server=False)


# --- the escape hatch -----------------------------------------------------


def test_disabling_enforcement_restores_the_previous_behaviour(monkeypatch):
    monkeypatch.setenv("MCP_GROUP_POLICY_ENFORCED", "false")
    assert not mcp_policy.enforced()
    assert _call(ANONYMOUS, GATED.name, GATED) == "ran"

    middleware = mcp_policy.GroupPolicyMiddleware()

    async def call_next(_context):
        return [GATED, PUBLIC]

    listed = _run(
        OUTSIDER, lambda: middleware.on_list_tools(_list_context(), call_next)
    )
    assert [tool.name for tool in listed] == [GATED.name, PUBLIC.name]


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "", "  "])
def test_enforcement_is_on_unless_explicitly_disabled(monkeypatch, value):
    # An empty or blank value reads as "not set to anything meaningful", which
    # must land on the safe side rather than silently opening the surface.
    monkeypatch.setenv("MCP_GROUP_POLICY_ENFORCED", value)
    assert mcp_policy.enforced()


@pytest.mark.parametrize("value", ["false", "0", "no", "FALSE", " no "])
def test_enforcement_is_off_only_for_explicit_negatives(monkeypatch, value):
    monkeypatch.setenv("MCP_GROUP_POLICY_ENFORCED", value)
    assert not mcp_policy.enforced()


def test_enforcement_defaults_on_when_unset(monkeypatch):
    monkeypatch.delenv("MCP_GROUP_POLICY_ENFORCED", raising=False)
    assert mcp_policy.enforced()
