"""Async contract for weave-hand/loom e6ca13cc43264dd8b2b1a067b675979c4ac323b0.

See runtime/src/admin.rs, query-api/src/{http,render,openapi_gen,params}.rs.
Definitions are create-only. Description receipts retain fields the pinned read
API omits (link backing and action assignments); foreign definitions fail closed.
Concurrent definition administrators must serialize externally: Loom has no CAS
on definition POST. A read/create sequence is never automatically retried.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

JsonObject = dict[str, Any]


class LoomError(Exception):
    """HTTP failure without response bodies or credentials in the message."""


class Unauthorized(LoomError):
    pass


class Forbidden(LoomError):
    pass


class NotFound(LoomError):
    """Also covers unreadable datasets; never infer existence from a 404."""


class Conflict(LoomError):
    pass


class Gone(LoomError):
    pass


class ServerError(LoomError):
    pass


class Timeout(LoomError):
    pass


class TransportError(LoomError):
    pass


class DefinitionMismatch(Conflict):
    pass


@dataclass(frozen=True)
class TableRef:
    schema: str
    name: str


@dataclass(frozen=True)
class Property:
    name: str
    ty: str
    required: bool


@dataclass(frozen=True)
class Model:
    name: str
    table: TableRef
    properties: tuple[Property, ...]
    identity: str = "id"

    def body(self) -> JsonObject:
        return {
            **asdict(self),
            "properties": [asdict(p) for p in self.properties],
            "derived": [],
        }


@dataclass(frozen=True)
class Link:
    name: str
    source: str
    target: str
    from_column: str
    to_column: str = "id"

    def body(self) -> JsonObject:
        return {
            "name": self.name,
            "from": self.source,
            "to": self.target,
            "cardinality": "One",
            "backing": {
                "ForeignKey": {
                    "from_column": self.from_column,
                    "to_column": self.to_column,
                }
            },
        }


@dataclass(frozen=True)
class Action:
    name: str
    target: str
    kind: Literal["insert", "update", "delete"]
    parameters: tuple[Property, ...]

    def body(self) -> JsonObject:
        return {
            "name": self.name,
            "target": self.target,
            "kind": self.kind,
            "parameters": [asdict(p) for p in self.parameters],
            "assignments": [],
            "downstream": [],
        }


def receipt(body: JsonObject) -> str:
    """Immutable ownership record, compared before any existing name is accepted."""
    return "grimoire-loom-v1:" + json.dumps(body, sort_keys=True, separators=(",", ":"))


def parameter_schema_matches(parameter: Property, schema: JsonObject) -> bool:
    # Pinned openapi_gen.rs:25-114: schema_type_of, base_type_to_schema,
    # and property_schema produce OpenAPI 3.1 nullable type arrays.
    types = {
        "string": ("string", None),
        "long": ("string", None),
        "integer": ("integer", "int32"),
        "boolean": ("boolean", None),
        "double": ("number", "double"),
        "date": ("string", "date"),
        "timestamp": ("string", "date-time"),
    }
    if parameter.ty not in types:
        raise ValueError("unsupported action parameter logical type")
    wire_type, format_ = types[parameter.ty]
    expected = wire_type if parameter.required else [wire_type, "null"]
    return schema.get("type") == expected and schema.get("format") == format_


def segment(value: str) -> str:
    if not value or value in {".", ".."}:
        raise ValueError("empty or relative Loom identifier")
    return quote(value, safe="")


class LoomClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        read_attempts: int = 3,
        timeout: float = 10.0,
    ) -> None:
        if (
            not token
            or not base_url
            or not 1 <= read_attempts <= 5
            or not 0 < timeout <= 60
        ):
            raise ValueError(
                "Loom requires URL, explicit token and bounded timeout/attempts"
            )
        self._attempts = read_attempts
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout),
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> LoomClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        # Only GET is retried. POST may have committed even on timeout or 5xx.
        attempts = self._attempts if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = await self._http.request(method, path, **kwargs)
            except httpx.TimeoutException:
                if attempt + 1 == attempts:
                    raise Timeout("Loom request timed out") from None
            except httpx.TransportError:
                if attempt + 1 == attempts:
                    raise TransportError("Loom transport failure") from None
            else:
                status = response.status_code
                if 200 <= status < 300:
                    return response
                error = {
                    401: Unauthorized,
                    403: Forbidden,
                    404: NotFound,
                    409: Conflict,
                    410: Gone,
                }.get(status)
                if error:
                    raise error(f"Loom HTTP {status}")
                if status < 500 or attempt + 1 == attempts:
                    raise (ServerError if status >= 500 else LoomError)(
                        f"Loom HTTP {status}"
                    )
            await asyncio.sleep(random.uniform(0.01, 0.05) * 2**attempt)
        raise AssertionError("unreachable")

    async def type_detail(self, name: str) -> JsonObject:
        return (await self._request("GET", f"/ontology/types/{segment(name)}")).json()

    async def define_model(self, model: Model) -> bool:
        body = model.body()
        try:
            current = await self.type_detail(model.name)
        except NotFound:
            await self._request("POST", "/admin/models", json=body)
            return True
        expected = {k: current.get(k) for k in body}
        if expected != body:
            raise DefinitionMismatch(f"model differs: {model.name}")
        return False

    async def define_link(self, link: Link) -> bool:
        body = link.body()
        current = await self.type_detail(link.source)
        matches = [v for v in current["links"] if v["name"] == link.name]
        description = receipt(body)
        if matches:
            visible = {k: v for k, v in body.items() if k != "backing"}
            # core/src/ontology.rs:256-264: read token is lower-case, while
            # admin.rs:814-819 accepts PascalCase serde enum variants.
            visible["cardinality"] = body["cardinality"].lower()
            if (
                len(matches) != 1
                or any(matches[0].get(k) != v for k, v in visible.items())
                or matches[0].get("description") != description
            ):
                raise DefinitionMismatch(
                    f"link differs or lacks ownership receipt: {link.name}"
                )
            return False
        await self._request(
            "POST", "/admin/links", json={**body, "description": description}
        )
        return True

    async def define_action(self, action: Action) -> bool:
        # Pinned API has no GET /admin/actions. Its live OpenAPI enumerates actions.
        # A failed type listing returns HTTP 200 with static paths only. A failed
        # action listing is indistinguishable from no actions; see architecture.md.
        spec = (await self._request("GET", "/openapi.json")).json()
        current = spec["paths"].get(f"/actions/{action.name}", {}).get("post")
        body = action.body()
        if current is not None:
            status = "201" if action.kind == "insert" else "200"
            schema = current["requestBody"]["content"]["application/json"]["schema"]
            summary = {
                "insert": f"Insert a {action.target}",
                "update": f"Update a {action.target} (identity-targeted PATCH)",
                "delete": f"Delete a {action.target} by identity",
            }[action.kind]
            if (
                current.get("summary") != summary
                or current.get("description") != receipt(body)
                or current.get("tags") != [action.target]
                or status not in current["responses"]
                or set(schema.get("properties", {}))
                != {p.name for p in action.parameters}
                or any(
                    not parameter_schema_matches(
                        p, schema["properties"].get(p.name, {})
                    )
                    for p in action.parameters
                )
                or set(schema.get("required", []))
                != {p.name for p in action.parameters if p.required}
            ):
                raise DefinitionMismatch(
                    f"action differs or lacks ownership receipt: {action.name}"
                )
            return False
        if f"/objects/{action.target}" not in spec["paths"]:
            raise DefinitionMismatch(
                f"action target missing from OpenAPI: {action.target}"
            )
        await self._request(
            "POST", "/admin/actions", json={**body, "description": receipt(body)}
        )
        return True

    async def invoke(self, name: str, parameters: Mapping[str, Any]) -> Any:
        return (
            await self._request(
                "POST", f"/actions/{segment(name)}", json=dict(parameters)
            )
        ).json()

    async def snapshot(self, table: TableRef) -> int:
        body = (
            await self._request(
                "GET", f"/datasets/{segment(table.schema)}/{segment(table.name)}"
            )
        ).json()
        value = body["snapshot_id"]
        if type(value) is not int:
            raise LoomError("invalid snapshot id")
        return value

    async def objects(
        self,
        model: Model,
        *,
        snapshot: int,
        limit: int = 200,
    ) -> AsyncIterator[JsonObject]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        if type(snapshot) is not int:
            raise ValueError("snapshot must be an exact integer selector")
        # Resolve once and reject an unexpected table before reading any objects.
        current = await self.type_detail(model.name)
        if (
            current["table"] != asdict(model.table)
            or current["identity"] != model.identity
        ):
            raise DefinitionMismatch("object binding changed")
        params: dict[str, str | int] = {"limit": limit, "as_of_snapshot": snapshot}
        seen: set[str] = set()
        while True:
            if "cursor" in params:
                current = await self.type_detail(model.name)
                if (
                    current["table"] != asdict(model.table)
                    or current["identity"] != model.identity
                ):
                    raise DefinitionMismatch("object binding changed during pagination")
            body = (
                await self._request(
                    "GET", f"/objects/{segment(model.name)}", params=params
                )
            ).json()
            for row in body["objects"]:
                yield row
            cursor = body["next"]
            if cursor is None:
                return
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise LoomError("invalid or repeated opaque cursor")
            seen.add(cursor)
            params["cursor"] = cursor

    async def contains(self, model: Model, identity: str) -> bool:
        # _ids selects identity rows without a cursor; use latest state for upsert.
        current = await self.type_detail(model.name)
        if (
            current["table"] != asdict(model.table)
            or current["identity"] != model.identity
        ):
            raise DefinitionMismatch("upsert binding changed")
        result = (
            await self._request(
                "GET", f"/objects/{segment(model.name)}", params={"_ids": identity}
            )
        ).json()
        return bool(result["objects"])
