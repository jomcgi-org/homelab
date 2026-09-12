"""The tool publisher: additive, union-preserving, and loud when it cannot tell."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

# Mounted from the chart and run with the image's python3, so it is not an
# importable package. Bazel passes its runfiles path; the fallback keeps the
# file runnable straight from the source tree.
SCRIPT = Path(
    os.environ.get(
        "RECONCILE_TOOLS_SCRIPT",
        Path(__file__).parent / "files" / "reconcile_tools.py",
    )
)
_SPEC = importlib.util.spec_from_file_location("reconcile_tools", SCRIPT)
reconcile_tools = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reconcile_tools)

ReconcileError = reconcile_tools.ReconcileError


class FakeApi:
    """Records every write so a test can assert on what was actually sent."""

    def __init__(self, *, gateways=None, tools=None, servers=None):
        self.gateways = (
            gateways if gateways is not None else [{"id": "g1", "name": "monolith"}]
        )
        self.tools = tools if tools is not None else []
        self.servers = servers if servers is not None else []
        self.writes: list[tuple[str, str, dict]] = []

    def __call__(self, method, path, body=None):
        if method == "GET" and path == "/gateways":
            return 200, self.gateways
        if method == "GET" and path == "/tools":
            return 200, self.tools
        if method == "GET" and path == "/servers":
            return 200, self.servers
        if method == "PUT":
            self.writes.append((method, path, body))
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")


def _tool(tool_id, name, visibility="team", gateway_id="g1"):
    return {
        "id": tool_id,
        "name": name,
        "visibility": visibility,
        "gateway_id": gateway_id,
    }


def _server(tools):
    return {"id": "s1", "name": "homelab-admin", "associated_tools": list(tools)}


def _run(api):
    return reconcile_tools.reconcile(
        api, gateway_name="monolith", server_name="homelab-admin"
    )


# --- publishing -----------------------------------------------------------


def test_a_new_tool_is_raised_to_public_and_associated():
    api = FakeApi(tools=[_tool("t1", "factory_status")], servers=[_server([])])
    result = _run(api)
    assert result["published"] == ["factory_status"]
    assert result["associated"] == ["t1"]
    assert ("PUT", "/tools/t1", {"visibility": "public"}) in api.writes
    assert ("PUT", "/servers/s1", {"associated_tools": ["t1"]}) in api.writes


def test_an_already_public_and_associated_tool_writes_nothing():
    api = FakeApi(
        tools=[_tool("t1", "search_knowledge", visibility="public")],
        servers=[_server(["t1"])],
    )
    result = _run(api)
    assert result == {"tools": 1, "published": [], "associated": []}
    assert api.writes == []


def test_visibility_is_never_lowered():
    api = FakeApi(
        tools=[_tool("t1", "already", visibility="public")], servers=[_server(["t1"])]
    )
    _run(api)
    assert not [w for w in api.writes if w[1].startswith("/tools/")]


# --- the association union, which is the dangerous one --------------------


def test_existing_associations_are_preserved_not_replaced():
    """A PUT replaces associated_tools, so a bare list would strip the rest."""
    api = FakeApi(
        tools=[_tool("t1", "new_tool", visibility="public")],
        servers=[_server(["old_a", "old_b"])],
    )
    _run(api)
    put = [w for w in api.writes if w[1] == "/servers/s1"]
    assert len(put) == 1
    assert put[0][2]["associated_tools"] == ["old_a", "old_b", "t1"]


def test_association_is_skipped_when_nothing_is_missing():
    api = FakeApi(
        tools=[_tool("t1", "known", visibility="public")],
        servers=[_server(["other", "t1"])],
    )
    _run(api)
    assert not [w for w in api.writes if w[1] == "/servers/s1"]


def test_camel_case_association_key_is_understood():
    api = FakeApi(
        tools=[_tool("t1", "new", visibility="public")],
        servers=[{"id": "s1", "name": "homelab-admin", "associatedTools": ["old"]}],
    )
    _run(api)
    put = [w for w in api.writes if w[1] == "/servers/s1"][0]
    assert put[2]["associated_tools"] == ["old", "t1"]


# --- scoping --------------------------------------------------------------


def test_only_the_named_gateway_is_touched():
    api = FakeApi(
        tools=[_tool("t1", "mine"), _tool("t2", "someone_elses", gateway_id="g2")],
        servers=[_server([])],
    )
    result = _run(api)
    assert result["published"] == ["mine"]
    assert result["associated"] == ["t1"]


# --- refusing rather than guessing ---------------------------------------


def test_an_empty_catalogue_refuses_rather_than_emptying_the_server():
    """A refresh that has not run looks exactly like a gateway with no tools.

    Writing the association list in that state would unpublish everything.
    """
    api = FakeApi(tools=[], servers=[_server(["a", "b"])])
    with pytest.raises(ReconcileError, match="no tools"):
        _run(api)
    assert api.writes == []


def test_an_unregistered_gateway_is_an_error():
    api = FakeApi(gateways=[{"id": "g9", "name": "something-else"}])
    with pytest.raises(ReconcileError, match="not registered"):
        _run(api)


def test_a_missing_virtual_server_is_an_error_not_a_creation():
    api = FakeApi(tools=[_tool("t1", "x")], servers=[])
    with pytest.raises(ReconcileError, match="does not exist"):
        _run(api)


def test_a_paginated_envelope_is_read_like_a_bare_list():
    api = FakeApi(tools={"data": [_tool("t1", "paged")]}, servers=[_server([])])
    assert _run(api)["published"] == ["paged"]


def test_an_unreadable_payload_is_an_error():
    api = FakeApi(tools="nonsense", servers=[_server([])])
    with pytest.raises(ReconcileError, match="unreadable"):
        _run(api)


def test_an_http_error_is_surfaced():
    api = FakeApi(tools=[_tool("t1", "x")], servers=[_server([])])

    def failing(method, path, body=None):
        if method == "PUT":
            return 403, {"detail": "forbidden"}
        return FakeApi.__call__(api, method, path, body)

    with pytest.raises(ReconcileError, match="403"):
        reconcile_tools.reconcile(
            failing, gateway_name="monolith", server_name="homelab-admin"
        )
