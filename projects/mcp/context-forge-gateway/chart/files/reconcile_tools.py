"""Publish new monolith MCP tools in Context Forge without a hand-written row.

A catalogue refresh puts rows in `tools` and stops there. Two more things have
to happen before any caller sees a tool, and refresh writes neither: the row's
`visibility` has to be `public` (refresh leaves a new tool at `team`), and a
`server_tool_association` row has to link it to the virtual server the admin
route serves. Both were hand edits in CF's Postgres, made from a laptop, and a
tool sat invisible until someone remembered. This script makes each tick do
them, the same way reconcile_team_mapping.py made Git own the team wiring.

Authorization is NOT what this moves. Every tool it publishes is already gated
at the monolith on the caller's authentik groups (core/mcp_policy.py), and CF's
team model enforces nothing today: the tools a caller sees are visible because
they are `public`, which skips the team check entirely. So `visibility` here is
a publishing switch, not an access control, and treating it as one would be a
mistake in either direction.

Additive by construction, and deliberately so. A PUT to /servers/{id} REPLACES
associated_tools rather than appending to it, so a run that sent only the tools
it just computed would silently strip every association it did not know about,
which is the whole catalogue. Every write here sends the union of what is there
and what is missing, never removes an association, and never lowers a
visibility it did not raise. The worst a bug can do is publish something that
was going to be published by hand anyway.

Stdlib only, matching reconcile_team_mapping.py, so the unit test runs without
the gateway image.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

# (method, path, body) -> (status, decoded json or None)
Api = Callable[[str, str, Any], tuple[int, Any]]

PUBLIC = "public"


class ReconcileError(RuntimeError):
    """A condition the job must surface as a failed run, never paper over."""


def http_api(base_url: str, token: str, timeout: float = 30.0) -> Api:
    base = base_url.rstrip("/")

    def call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (in-cluster service URL from env)
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode(errors="replace")

    return call


def _expect(status: int, payload: Any, what: str) -> None:
    if status >= 400:
        raise ReconcileError(f"{what} failed with {status}: {payload!r}")


def _items(payload: Any, what: str) -> list[dict]:
    """CF returns either a bare list or a paginated envelope, depending on
    whether the caller asked for a page. Accept both rather than pinning the
    job to one shape upstream is free to change under us."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [row for row in inner if isinstance(row, dict)]
    raise ReconcileError(f"{what} returned an unreadable payload: {payload!r}")


def gateway_id(api: Api, name: str) -> str:
    status, payload = api("GET", "/gateways", None)
    _expect(status, payload, "GET /gateways")
    for row in _items(payload, "GET /gateways"):
        if row.get("name") == name or row.get("slug") == name:
            found = row.get("id")
            if not found:
                raise ReconcileError(f"gateway {name!r} has no id")
            return str(found)
    raise ReconcileError(f"gateway {name!r} is not registered")


def gateway_tools(api: Api, gid: str) -> list[dict]:
    status, payload = api("GET", "/tools", None)
    _expect(status, payload, "GET /tools")
    return [
        row
        for row in _items(payload, "GET /tools")
        if str(row.get("gateway_id")) == gid
    ]


def publish(api: Api, tools: list[dict]) -> list[str]:
    """Raise every tool that is not yet public. Never lowers one."""
    raised: list[str] = []
    for tool in tools:
        if tool.get("visibility") == PUBLIC:
            continue
        tool_id = tool.get("id")
        if not tool_id:
            continue
        status, payload = api("PUT", f"/tools/{tool_id}", {"visibility": PUBLIC})
        _expect(status, payload, f"PUT /tools/{tool_id}")
        raised.append(str(tool.get("name") or tool_id))
    return raised


def server_by_name(api: Api, name: str) -> dict:
    status, payload = api("GET", "/servers", None)
    _expect(status, payload, "GET /servers")
    for row in _items(payload, "GET /servers"):
        if row.get("name") == name:
            return row
    raise ReconcileError(f"virtual server {name!r} does not exist")


def associate(api: Api, tools: list[dict], server: dict) -> list[str]:
    """Add missing tools to the server, preserving every existing association."""
    server_id = server.get("id")
    if not server_id:
        raise ReconcileError(f"virtual server {server.get('name')!r} has no id")
    existing = [
        str(t)
        for t in (server.get("associated_tools") or server.get("associatedTools") or [])
    ]
    wanted = [str(tool["id"]) for tool in tools if tool.get("id")]
    missing = [tool_id for tool_id in wanted if tool_id not in existing]
    if not missing:
        return []
    # Union, never a replacement: a bare list here would drop every
    # association this run did not compute.
    status, payload = api(
        "PUT", f"/servers/{server_id}", {"associated_tools": existing + missing}
    )
    _expect(status, payload, f"PUT /servers/{server_id}")
    return missing


def reconcile(api: Api, *, gateway_name: str, server_name: str) -> dict:
    gid = gateway_id(api, gateway_name)
    tools = gateway_tools(api, gid)
    if not tools:
        # An empty catalogue is a refresh that has not run or a gateway that is
        # down, never a reason to rewrite the server's associations to nothing.
        raise ReconcileError(f"gateway {gateway_name!r} reports no tools; refusing")
    raised = publish(api, tools)
    server = server_by_name(api, server_name)
    added = associate(api, tools, server)
    return {"tools": len(tools), "published": raised, "associated": added}


def main() -> int:
    base = os.environ["CF_GATEWAY_URL"]
    token = os.environ["CF_ADMIN_TOKEN"]
    gateway_name = os.environ.get("CF_GATEWAY_NAME", "monolith")
    server_name = os.environ.get("CF_SERVER_NAME", "homelab-admin")
    try:
        result = reconcile(
            http_api(base, token), gateway_name=gateway_name, server_name=server_name
        )
    except ReconcileError as exc:
        print(f"reconcile-tools: {exc}", file=sys.stderr)
        return 1
    print(
        "reconcile-tools: {tools} tool(s); published={published} associated={associated}".format(
            **result
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
