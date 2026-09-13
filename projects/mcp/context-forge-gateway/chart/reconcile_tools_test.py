"""The tool publisher: additive, union-preserving, and loud when it cannot tell.

The fake here models the deployed gateway's wire shape rather than a tidy
one. Every read schema extends BaseModelWithConfigDict, so fields arrive in
camelCase; ServerRead carries tool NAMES in associatedTools and tool IDS in
associatedToolIds; and a PUT to /servers/{id} replaces the association list,
resolving strictly by id and silently dropping anything else. An earlier
version of this fake returned ids under the names key and honoured the PUT
verbatim, which is exactly how the real defects went unnoticed.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

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


def _tool(tool_id, name, visibility="team", gateway_id="g1"):
    # camelCase, as the gateway serialises it.
    return {
        "id": tool_id,
        "name": name,
        "visibility": visibility,
        "gatewayId": gateway_id,
    }


class FakeApi:
    """A gateway that behaves like the real one on the paths this job uses."""

    def __init__(self, *, gateways=None, tools=None, server_tool_ids=None, server=True):
        self.gateways = (
            gateways if gateways is not None else [{"id": "g1", "name": "monolith"}]
        )
        self.tools = tools if tools is not None else []
        self.server_tool_ids = list(server_tool_ids or [])
        self.has_server = server
        self.writes: list[tuple[str, str, dict]] = []
        # Simulate an upstream that fails to retain some ids on write.
        self.drop_on_write: set[str] = set()
        # The real gateway pages every list at 50 unless asked for limit=0.
        self.page_size = 50
        self.queries: list[tuple[str, str]] = []

    def _server_read(self):
        by_id = {
            t["id"]: t for t in (self.tools if isinstance(self.tools, list) else [])
        }
        return {
            "id": "s1",
            "name": "homelab-admin",
            # Names, enabled only, exactly like ServerRead.
            "associatedTools": [
                by_id[i]["name"] for i in self.server_tool_ids if i in by_id
            ],
            "associatedToolIds": list(self.server_tool_ids),
        }

    def _page(self, rows, query):
        if query == "limit=0" or not isinstance(rows, list):
            return rows
        return rows[: self.page_size]

    def __call__(self, method, path, body=None):
        path, _, query = path.partition("?")
        if method == "GET":
            self.queries.append((path, query))
        if method == "GET" and path == "/gateways":
            return 200, self._page(self.gateways, query)
        if method == "GET" and path == "/tools":
            return 200, self._page(self.tools, query)
        if method == "GET" and path == "/servers":
            return 200, self._page(
                [self._server_read()] if self.has_server else [], query
            )
        if method == "GET" and path == "/servers/s1":
            return 200, self._server_read()
        if method == "PUT" and path.startswith("/tools/"):
            self.writes.append((method, path, body))
            return 200, {}
        if method == "PUT" and path == "/servers/s1":
            self.writes.append((method, path, body))
            # _update_server_associations: clear, then keep only ids that
            # resolve. Names and unknown ids vanish without an error.
            known = (
                {t["id"] for t in self.tools} if isinstance(self.tools, list) else set()
            )
            sent = body.get("associated_tools", [])
            self.server_tool_ids = [
                i for i in sent if i in known and i not in self.drop_on_write
            ]
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")


def _run(api):
    return reconcile_tools.reconcile(
        api, gateway_name="monolith", server_name="homelab-admin"
    )


# --- publishing -----------------------------------------------------------


def test_a_new_tool_is_raised_to_public_and_associated():
    api = FakeApi(tools=[_tool("t1", "factory_status")])
    result = _run(api)
    assert result["published"] == ["factory_status"]
    assert result["associated"] == ["t1"]
    assert ("PUT", "/tools/t1", {"visibility": "public"}) in api.writes
    assert ("PUT", "/servers/s1", {"associated_tools": ["t1"]}) in api.writes
    assert api.server_tool_ids == ["t1"]


def test_an_already_public_and_associated_tool_writes_nothing():
    api = FakeApi(
        tools=[_tool("t1", "search_knowledge", visibility="public")],
        server_tool_ids=["t1"],
    )
    assert _run(api) == {"tools": 1, "published": [], "associated": []}
    assert api.writes == []


def test_visibility_is_never_lowered():
    api = FakeApi(
        tools=[_tool("t1", "already", visibility="public")], server_tool_ids=["t1"]
    )
    _run(api)
    assert not [w for w in api.writes if w[1].startswith("/tools/")]


# --- the wire shape, which is where the real defects lived -----------------


def test_tools_are_matched_on_the_camel_case_gateway_key():
    """The gateway serialises gateway_id as gatewayId. A filter on the
    snake_case key finds nothing and the job refuses every run."""
    api = FakeApi(tools=[_tool("t1", "x")])
    assert _run(api)["tools"] == 1


def test_snake_case_keys_are_accepted_too():
    api = FakeApi(
        tools=[{"id": "t1", "name": "x", "visibility": "team", "gateway_id": "g1"}]
    )
    assert _run(api)["tools"] == 1


def test_existing_associations_come_from_ids_never_from_names():
    """ServerRead.associatedTools is NAMES. Sending names to the update path
    matches no id, and the server is left with only the new tool."""
    api = FakeApi(
        tools=[
            _tool("old_a", "alpha", visibility="public"),
            _tool("old_b", "beta", visibility="public"),
            _tool("t1", "new", visibility="public"),
        ],
        server_tool_ids=["old_a", "old_b"],
    )
    _run(api)
    put = [w for w in api.writes if w[1] == "/servers/s1"]
    assert len(put) == 1
    assert put[0][2]["associated_tools"] == ["old_a", "old_b", "t1"]
    # Nothing was stripped: the fake resolved every id it was sent.
    assert api.server_tool_ids == ["old_a", "old_b", "t1"]


def test_a_server_without_an_ids_field_is_refused_not_guessed():
    api = FakeApi(tools=[_tool("t1", "x")])
    original = api._server_read

    def without_ids():
        row = original()
        del row["associatedToolIds"]
        return row

    api._server_read = without_ids
    with pytest.raises(ReconcileError, match="associated_tool_ids"):
        _run(api)
    assert not [w for w in api.writes if w[1] == "/servers/s1"]


def test_association_is_skipped_when_nothing_is_missing():
    api = FakeApi(
        tools=[
            _tool("t1", "known", visibility="public"),
            _tool("other", "o", visibility="public"),
        ],
        server_tool_ids=["other", "t1"],
    )
    _run(api)
    assert not [w for w in api.writes if w[1] == "/servers/s1"]


# --- verify after write ---------------------------------------------------


def test_an_association_the_gateway_did_not_retain_is_an_error():
    """The update path drops unresolved ids silently. Reading back is the
    only point at which a stripped association can be noticed."""
    api = FakeApi(
        tools=[
            _tool("old", "o", visibility="public"),
            _tool("t1", "new", visibility="public"),
        ],
        server_tool_ids=["old"],
    )
    api.drop_on_write = {"old"}
    with pytest.raises(ReconcileError, match="did not retain"):
        _run(api)


# --- scoping --------------------------------------------------------------


def test_only_the_named_gateway_is_touched():
    api = FakeApi(
        tools=[_tool("t1", "mine"), _tool("t2", "someone_elses", gateway_id="g2")]
    )
    result = _run(api)
    assert result["published"] == ["mine"]
    assert result["associated"] == ["t1"]


# --- refusing rather than guessing ---------------------------------------


def test_an_empty_catalogue_refuses_rather_than_emptying_the_server():
    api = FakeApi(tools=[], server_tool_ids=["a", "b"])
    with pytest.raises(ReconcileError, match="no tools"):
        _run(api)
    assert api.writes == []


def test_an_unregistered_gateway_is_an_error():
    api = FakeApi(gateways=[{"id": "g9", "name": "something-else"}])
    with pytest.raises(ReconcileError, match="not registered"):
        _run(api)


def test_a_missing_virtual_server_is_an_error_not_a_creation():
    api = FakeApi(tools=[_tool("t1", "x")], server=False)
    with pytest.raises(ReconcileError, match="does not exist"):
        _run(api)


def test_a_paginated_envelope_is_read_like_a_bare_list():
    api = FakeApi(tools=[_tool("t1", "paged")])
    api.tools_list = api.tools
    envelope = {"data": api.tools}
    original = api.__call__

    def call(method, path, body=None):
        if method == "GET" and path.startswith("/tools?"):
            return 200, envelope
        return original(method, path, body)

    assert reconcile_tools.reconcile(
        call, gateway_name="monolith", server_name="homelab-admin"
    )["published"] == ["paged"]


def test_an_unreadable_payload_is_an_error():
    api = FakeApi(tools=[_tool("t1", "x")])
    original = api.__call__

    def call(method, path, body=None):
        if method == "GET" and path.startswith("/tools?"):
            return 200, "nonsense"
        return original(method, path, body)

    with pytest.raises(ReconcileError, match="unreadable"):
        reconcile_tools.reconcile(
            call, gateway_name="monolith", server_name="homelab-admin"
        )


def test_an_http_error_is_surfaced():
    api = FakeApi(tools=[_tool("t1", "x")])
    original = api.__call__

    def call(method, path, body=None):
        if method == "PUT":
            return 403, {"detail": "forbidden"}
        return original(method, path, body)

    with pytest.raises(ReconcileError, match="403"):
        reconcile_tools.reconcile(
            call, gateway_name="monolith", server_name="homelab-admin"
        )


# --- pagination -----------------------------------------------------------


def test_every_list_is_read_unpaged():
    """The gateway pages at 50 by default. The first live run read 50 of 60
    tools and reconciled the truncated catalogue; the rest stayed hidden."""
    tools = [_tool(f"t{i}", f"tool_{i}", visibility="public") for i in range(60)]
    tools[59] = _tool("t59", "session_start")
    api = FakeApi(tools=tools, server_tool_ids=[f"t{i}" for i in range(59)])
    result = _run(api)
    assert result["tools"] == 60
    assert result["published"] == ["session_start"]
    assert result["associated"] == ["t59"]
    assert api.server_tool_ids == [f"t{i}" for i in range(60)]
    lists = {p: q for p, q in api.queries if p in ("/gateways", "/tools", "/servers")}
    assert lists == {"/gateways": "limit=0", "/tools": "limit=0", "/servers": "limit=0"}


def test_a_paginated_tool_response_is_refused():
    api = FakeApi(tools=[_tool("t1", "x")])
    original = api.__call__

    def call(method, path, body=None):
        if method == "GET" and path.startswith("/tools?"):
            return 200, {"data": api.tools, "nextCursor": "abc"}
        return original(method, path, body)

    with pytest.raises(ReconcileError, match="paginated"):
        reconcile_tools.reconcile(
            call, gateway_name="monolith", server_name="homelab-admin"
        )
    assert not api.writes
