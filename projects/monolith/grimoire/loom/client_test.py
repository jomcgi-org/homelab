"""Local ASGI contract double for pinned Loom e6ca13c.

Routes/envelopes: query-api/src/http.rs:105-135,250-320,727-805,1435-1490;
render.rs:15-38; admin.rs:516-600,802-938; params.rs:30-59,175-222.
The fake deliberately exposes NO admin GET, link backing or action assignments.
"""

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from grimoire.loom import jobs
from grimoire.loom.client import (
    Conflict,
    DefinitionMismatch,
    Forbidden,
    Gone,
    LoomClient,
    LoomError,
    NotFound,
    ServerError,
    Timeout,
    Unauthorized,
)
from grimoire.loom.ontology import Partition, SOURCES, apply, upsert

CAMPAIGN = "11111111-1111-4111-8111-111111111111"
PC = "22222222-2222-4222-8222-222222222222"
PARTITION = Partition(CAMPAIGN, PC)


class PinnedLoom:
    def __init__(self):
        self.models = {}
        self.links = {}
        self.actions = {}
        self.rows = {}
        self.calls = []
        self.failure = None
        self.pages = None
        self.race = None

    async def __call__(self, scope, receive, send):
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body"):
                break
        path, method = scope["path"], scope["method"]
        query = {k: v[-1] for k, v in parse_qs(scope["query_string"].decode()).items()}
        data = json.loads(body) if body else None
        self.calls.append((method, path, query, data))
        status, response = self.handle(method, path, query, data)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {"type": "http.response.body", "body": json.dumps(response).encode()}
        )

    def handle(self, method, path, query, data):
        if self.failure:
            return self.failure, {"error": "do not expose response bodies"}
        if method == "POST" and path.startswith("/admin/"):
            collection = path.rsplit("/", 1)[1]
            if collection == "models":
                assert isinstance(data["table"], dict)  # Exactly ONE TableRef.
                assert data["identity"] == "id"
                self.models[data["name"]] = data
            elif collection == "links":
                assert data["cardinality"] in {"One", "Many"}
                assert list(data["backing"]) == ["ForeignKey"]
                self.links[(data["from"], data["name"])] = data
            elif collection == "actions":
                assert data["kind"] in {"insert", "update", "delete"}
                target = self.models[data["target"]]
                props = {p["name"]: p for p in target["properties"]}
                assert all(
                    p["ty"] == props[p["name"]]["ty"] for p in data["parameters"]
                )
                assert any(
                    p["name"] == "id" and p["required"] for p in data["parameters"]
                )
                if data["kind"] == "delete":
                    assert len(data["parameters"]) == 1
                self.actions[data["name"]] = data
            else:
                return 404, None
            return 201, {"name": data["name"]}
        if method == "GET" and path.startswith("/ontology/types/"):
            name = path.rsplit("/", 1)[1]
            if name not in self.models:
                return 404, None
            return 200, {
                **self.models[name],
                "links": [
                    {
                        **{k: v for k, v in link.items() if k != "backing"},
                        # http.rs:190-198 calls Cardinality.as_str();
                        # core/src/ontology.rs:256-264 emits lowercase read tokens.
                        "cardinality": link["cardinality"].lower(),
                    }
                    for (source, _), link in self.links.items()
                    if source == name
                ],
                "links_to": [],
                "vector_indexes": [],
            }
        if method == "GET" and path == "/openapi.json":
            return 200, {
                "paths": {
                    f"/actions/{name}": {
                        "post": {
                            "summary": {
                                "insert": f"Insert a {action['target']}",
                                "update": f"Update a {action['target']} (identity-targeted PATCH)",
                                "delete": f"Delete a {action['target']} by identity",
                            }[action["kind"]],
                            "description": action["description"],
                            "tags": [action["target"]],
                            "responses": {
                                "201" if action["kind"] == "insert" else "200": {}
                            },
                            "requestBody": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "properties": {
                                                p["name"]: {
                                                    "type": (
                                                        "string"
                                                        if p["ty"] == "long"
                                                        else p["ty"]
                                                    )
                                                    if p["required"]
                                                    else [
                                                        "string"
                                                        if p["ty"] == "long"
                                                        else p["ty"],
                                                        "null",
                                                    ],
                                                    **(
                                                        {"format": "int32"}
                                                        if p["ty"] == "integer"
                                                        else {}
                                                    ),
                                                }
                                                for p in action["parameters"]
                                            },
                                            "required": [
                                                p["name"]
                                                for p in action["parameters"]
                                                if p["required"]
                                            ],
                                        }
                                    }
                                }
                            },
                        }
                    }
                    for name, action in self.actions.items()
                }
                | {f"/objects/{name}": {"get": {}} for name in self.models}
            }
        if method == "GET" and path.startswith("/datasets/"):
            return 200, {
                "snapshot_id": 987654321,
                "snapshot_time": "2026-01-01T00:00:00",
            }
        if method == "GET" and path.startswith("/objects/"):
            name = path.rsplit("/", 1)[1]
            if self.pages:
                return 200, self.pages[1 if "cursor" in query else 0]
            ids = query.get("_ids", "").split(",")
            return 200, {
                "objects": [
                    v for (ty, id_), v in self.rows.items() if ty == name and id_ in ids
                ],
                "next": None,
            }
        if method == "POST" and path.startswith("/actions/"):
            name = path.rsplit("/", 1)[1]
            if name not in self.actions:
                return 404, None
            action = self.actions[name]
            params = {p["name"]: p for p in action["parameters"]}
            if set(data) - set(params) or any(
                p["required"] and data.get(k) is None for k, p in params.items()
            ):
                return 422, None
            for key, value in data.items():
                if value is None:
                    continue
                ty = params[key]["ty"]
                if (
                    (ty == "string" and not isinstance(value, str))
                    or (ty == "boolean" and type(value) is not bool)
                    or (ty == "integer" and type(value) is not int)
                ):
                    return 422, None
                if ty == "long":
                    if not isinstance(value, str):
                        return 422, None
                    int(value)
            key = (action["target"], data["id"])
            if self.race:
                failure = self.race
                self.race = None
                if failure == 409:
                    self.rows[key] = data
                else:
                    self.rows.pop(key, None)
                return failure, None
            if action["kind"] in {"update", "delete"} and key not in self.rows:
                return 404, None
            if action["kind"] == "delete":
                return 200, self.rows.pop(key)
            if action["kind"] == "update":
                # action.rs:1271-1279: identity-targeted PATCH skips Null params.
                self.rows[key].update(
                    {k: v for k, v in data.items() if k != "id" and v is not None}
                )
                return 200, self.rows[key]
            self.rows[key] = data
            return (201 if action["kind"] == "insert" else 200), data
        return 404, None


def client(fake, **kwargs):
    return LoomClient(
        "http://local-fake", "dummy", transport=httpx.ASGITransport(app=fake), **kwargs
    )


def campaign_row():
    return {
        "id": CAMPAIGN,
        "name": "Test",
        "transcript_retention_days": 30,
        "dm_name": "DM",
        "notes_dm_readable_default": False,
        "owner_app_user_id": uuid4(),
        "created_at": datetime(2026, 1, 1, 1, 2, 3, 456789, tzinfo=timezone.utc),
    }


async def test_apply_twice_and_changed_definitions():
    fake = PinnedLoom()
    async with client(fake) as api:
        assert await apply(api, (PARTITION,)) == 42  # 8 models + 10 links + 24 actions
        count = len([c for c in fake.calls if c[0] == "POST"])
        assert await apply(api, (PARTITION,)) == 0
        assert len([c for c in fake.calls if c[0] == "POST"]) == count
        model = PARTITION.model("Campaign")
        fake.models[model.name]["identity"] = "name"
        with pytest.raises(DefinitionMismatch):
            await api.define_model(model)
        link = PARTITION.links()[0]
        fake.links[(link.source, link.name)]["description"] = "foreign"
        with pytest.raises(DefinitionMismatch):
            await api.define_link(link)
        action = PARTITION.actions("Note")[0]
        fake.actions[action.name]["description"] = "foreign"
        with pytest.raises(DefinitionMismatch):
            await api.define_action(action)
        assert len([c for c in fake.calls if c[0] == "POST"]) == count


@pytest.mark.parametrize("paths", [{}, {"/objects/Unrelated": {"get": {}}}])
async def test_degraded_openapi_cannot_overwrite_actions(paths):
    fake = PinnedLoom()
    async with client(fake) as api:
        await apply(api, (PARTITION,))
        action = PARTITION.actions("Campaign")[0]
        fake.actions[action.name]["description"] = "foreign"
        original = fake.handle

        def degraded(method, path, query, data):
            if path == "/openapi.json":
                return 200, {"paths": paths}
            return original(method, path, query, data)

        fake.handle = degraded
        fake.calls.clear()
        with pytest.raises(DefinitionMismatch, match="target missing from OpenAPI"):
            await apply(api, (PARTITION,))
        assert all(c[0] == "GET" for c in fake.calls)
        assert fake.actions[action.name]["description"] == "foreign"


async def test_pinned_update_preserves_null_columns():
    fake = PinnedLoom()
    async with client(fake) as api:
        await apply(api, (PARTITION,))
        row = campaign_row()
        await upsert(api, PARTITION, "Campaign", row)
        row.update(dm_name=None, name="Renamed")
        update = PARTITION.actions("Campaign")[1]
        await api.invoke(update.name, PARTITION.payload("Campaign", row))
        stored = fake.rows[(update.target, CAMPAIGN)]
        assert stored["name"] == "Renamed"
        assert stored["dm_name"] == "DM"


@pytest.mark.parametrize(
    "kind,fields,cleared",
    [
        ("Campaign", {"name": "Test", "dm_name": "DM"}, {"dm_name": None}),
        (
            "PlayerCharacter",
            {"character_name": "Test", "class_name": "Rogue", "level": 4},
            {"class_name": None, "level": None},
        ),
        (
            "KnowledgeGrantSlice",
            {
                "entity_id": CAMPAIGN,
                "player_character_id": PC,
                "grant_scope": "partial",
                "revealed_details": {"secret": "Previously visible body"},
            },
            {"grant_scope": "name_only", "revealed_details": None},
        ),
        (
            "Note",
            {
                "kind": "character",
                "player_character_id": PC,
                "dm_readable": True,
                "title": "Test",
            },
            {"player_character_id": None},
        ),
        (
            "CharacterFact",
            {
                "session_id": CAMPAIGN,
                "player_character_id": PC,
                "viewer_key": PC,
                "statement": "Test",
                "entity_id": CAMPAIGN,
                "evidence_event_ids": [CAMPAIGN],
                "extraction_version": "v1",
            },
            {"entity_id": None},
        ),
    ],
)
async def test_upsert_clears_optional_columns(kind, fields, cleared):
    partition = Partition(CAMPAIGN, dm=True) if kind == "Note" else PARTITION
    row = SOURCES[kind](
        id=CAMPAIGN if kind == "Campaign" else PC, campaign_id=CAMPAIGN, **fields
    ).model_dump()
    fake = PinnedLoom()
    async with client(fake) as api:
        await apply(api, (partition,))
        await upsert(api, partition, kind, row)
        fake.calls.clear()
        row.update(cleared)
        await upsert(api, partition, kind, row)
        model = partition.model(kind)
        assert fake.rows == {(model.name, row["id"]): partition.payload(kind, row)}
        assert [c[1] for c in fake.calls if c[0] == "POST"] == [
            f"/actions/retract_{model.name}",
            f"/actions/insert_{model.name}",
        ]


@pytest.mark.parametrize("step", ["retract", "insert"])
@pytest.mark.parametrize(
    "status,error",
    [(404, NotFound), (409, Conflict), (503, ServerError), ("timeout", Timeout)],
)
async def test_replacement_failure_is_not_retried_or_reconciled(step, status, error):
    fake = PinnedLoom()
    async with client(fake) as api:
        await apply(api, (PARTITION,))
        row = campaign_row()
        await upsert(api, PARTITION, "Campaign", row)
        row["dm_name"] = None
        original = fake.handle

        def failed(method, path, query, data):
            if path.startswith(f"/actions/{step}_"):
                if status == "timeout":
                    raise httpx.ReadTimeout("uncertain write")
                return status, None
            return original(method, path, query, data)

        fake.handle = failed
        fake.calls.clear()
        with pytest.raises(error):
            await upsert(api, PARTITION, "Campaign", row)
        model = PARTITION.model("Campaign")
        expected = [
            f"/ontology/types/{model.name}",
            f"/objects/{model.name}",
            f"/actions/retract_{model.name}",
        ]
        if step == "insert":
            expected.append(f"/actions/insert_{model.name}")
            assert fake.rows == {}
        else:
            assert fake.rows[(model.name, CAMPAIGN)]["dm_name"] == "DM"
        assert [c[1] for c in fake.calls] == expected


async def test_insert_conflict_reconciles_with_replacement_for_null_values():
    fake = PinnedLoom()
    async with client(fake) as api:
        await apply(api, (PARTITION,))
        row = campaign_row()
        row["dm_name"] = None
        fake.race = 409
        fake.calls.clear()
        await upsert(api, PARTITION, "Campaign", row)
        model = PARTITION.model("Campaign")
        assert fake.rows == {(model.name, CAMPAIGN): PARTITION.payload("Campaign", row)}
        assert [c[1] for c in fake.calls if c[0] == "POST"] == [
            f"/actions/insert_{model.name}",
            f"/actions/retract_{model.name}",
            f"/actions/insert_{model.name}",
        ]


async def test_upsert_insert_update_retract_and_payloads():
    fake = PinnedLoom()
    async with client(fake) as api:
        await apply(api, (PARTITION,))
        row = campaign_row()
        first = await upsert(api, PARTITION, "Campaign", row)
        assert first["created_at"].endswith(".456789+00:00")
        row["name"] = "Changed"
        assert (await upsert(api, PARTITION, "Campaign", row))["name"] == "Changed"
        writes = [c[1] for c in fake.calls if c[1].startswith("/actions/")]
        assert writes == [
            f"/actions/insert_{PARTITION.model('Campaign').name}",
            f"/actions/update_{PARTITION.model('Campaign').name}",
        ]
        retract = PARTITION.actions("Campaign")[2]
        await api.invoke(retract.name, {"id": CAMPAIGN})
        assert fake.rows == {}
        for kind in SOURCES:
            for action in PARTITION.actions(kind):
                data = {
                    p.name: (
                        "1"
                        if p.ty == "long"
                        else 1
                        if p.ty == "integer"
                        else False
                        if p.ty == "boolean"
                        else str(uuid4())
                    )
                    for p in action.parameters
                }
                data["id"] = CAMPAIGN
                fake.rows[(action.target, CAMPAIGN)] = data
                await api.invoke(action.name, data)
                with pytest.raises(LoomError):
                    await api.invoke(action.name, {**data, "undeclared": "bad"})
                with pytest.raises(LoomError):
                    await api.invoke(action.name, {**data, "id": None})


@pytest.mark.parametrize("exists,status", [(False, 409), (True, 404)])
async def test_bounded_deterministic_upsert_race(exists, status):
    fake = PinnedLoom()
    async with client(fake) as api:
        await apply(api, (PARTITION,))
        if exists:
            fake.rows[(PARTITION.model("Campaign").name, CAMPAIGN)] = PARTITION.payload(
                "Campaign", campaign_row()
            )
        fake.race = status
        await upsert(api, PARTITION, "Campaign", campaign_row())
        assert len([c for c in fake.calls if c[1].startswith("/actions/")]) == 2


@pytest.mark.parametrize(
    "status,error,attempts",
    [
        (401, Unauthorized, 1),
        (403, Forbidden, 1),
        (404, NotFound, 1),
        (409, Conflict, 1),
        (410, Gone, 1),
        (503, ServerError, 3),
    ],
)
async def test_typed_errors_and_safe_retry(status, error, attempts):
    fake = PinnedLoom()
    fake.failure = status
    async with client(fake) as api:
        with pytest.raises(error):
            await api.snapshot(PARTITION.model("Campaign").table)
        assert len(fake.calls) == attempts
        fake.calls.clear()
        with pytest.raises(error):
            await api.invoke("uncertain", {"id": CAMPAIGN})
        assert len(fake.calls) == 1


async def test_timeout_retry_and_unsafe_write():
    calls = []

    async def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive", request=request)

    async with LoomClient(
        "http://local-fake", "dummy", transport=httpx.MockTransport(timeout)
    ) as api:
        with pytest.raises(Timeout, match="^Loom request timed out$"):
            await api.snapshot(PARTITION.model("Campaign").table)
        assert len(calls) == 3
        calls.clear()
        with pytest.raises(Timeout):
            await api.invoke("uncertain", {})
        assert len(calls) == 1
        calls.clear()

        # A create timeout after the one definition GET must not repeat POST.
        async def create_timeout(request):
            calls.append(request)
            if request.method == "GET":
                return httpx.Response(404)
            raise httpx.ReadTimeout("sensitive", request=request)

        api._http._transport = httpx.MockTransport(create_timeout)
        with pytest.raises(Timeout):
            await api.define_model(PARTITION.model("Campaign"))
        assert [c.method for c in calls] == ["GET", "POST"]


async def test_fixed_snapshot_opaque_cursor_binding_and_limits():
    fake = PinnedLoom()
    model = PARTITION.model("Campaign")
    async with client(fake) as api:
        await api.define_model(model)
        snap = await api.snapshot(model.table)
        fake.pages = [
            {"objects": [{"id": "a"}], "next": "opaque+/=?%"},
            {"objects": [{"id": "b"}], "next": None},
        ]
        assert [v async for v in api.objects(model, snapshot=snap, limit=1)] == [
            {"id": "a"},
            {"id": "b"},
        ]
        reads = [c for c in fake.calls if c[1].startswith("/objects/")]
        assert [c[2] for c in reads] == [
            {"limit": "1", "as_of_snapshot": str(snap)},
            {"limit": "1", "as_of_snapshot": str(snap), "cursor": "opaque+/=?%"},
        ]
        for limit in (0, 201, True):
            with pytest.raises(ValueError):
                [v async for v in api.objects(model, snapshot=snap, limit=limit)]
        fake.models[model.name]["table"] = {"schema": "foreign", "name": "private"}
        with pytest.raises(DefinitionMismatch):
            [v async for v in api.objects(model, snapshot=snap)]


async def test_disabled_and_missing_admin_fail_before_client(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("constructed client")

    monkeypatch.setattr(jobs, "LoomClient", forbidden)
    monkeypatch.delenv("GRIMOIRE_LOOM_ENABLED", raising=False)
    await jobs.grimoire_loom_ontology()
    monkeypatch.setenv("GRIMOIRE_LOOM_ENABLED", "true")
    monkeypatch.delenv("GRIMOIRE_LOOM_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("GRIMOIRE_LOOM_TOKEN", "runtime-must-never-be-used")
    with pytest.raises(ValueError, match="job-admin"):
        await jobs.grimoire_loom_ontology()


async def test_read_denial_and_uncertain_write_never_start_or_repeat_upsert():
    fake = PinnedLoom()
    async with client(fake) as api:
        await apply(api, (PARTITION,))
        fake.calls.clear()
        fake.failure = 404
        with pytest.raises(NotFound):
            await upsert(api, PARTITION, "Campaign", campaign_row())
        assert all(c[0] == "GET" for c in fake.calls)
        fake.failure = None
        original = fake.handle

        def uncertain(method, path, query, data):
            if path.startswith("/actions/"):
                return 503, None
            return original(method, path, query, data)

        fake.handle = uncertain
        fake.calls.clear()
        with pytest.raises(ServerError):
            await upsert(api, PARTITION, "Campaign", campaign_row())
        assert len([c for c in fake.calls if c[0] == "POST"]) == 1


async def test_repeated_race_is_bounded():
    fake = PinnedLoom()
    async with client(fake) as api:
        await apply(api, (PARTITION,))
        fake.calls.clear()
        original = fake.handle

        def repeated(method, path, query, data):
            if path.startswith("/actions/"):
                fake.race = 409 if "/insert_" in path else 404
            return original(method, path, query, data)

        fake.handle = repeated
        with pytest.raises(NotFound):
            await upsert(api, PARTITION, "Campaign", campaign_row())
        assert len([c for c in fake.calls if c[0] == "POST"]) == 2


async def test_job_validates_partitions_and_applies_idempotently(monkeypatch):
    fake = PinnedLoom()
    monkeypatch.setenv("GRIMOIRE_LOOM_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_LOOM_ADMIN_TOKEN", "dummy-local-only")
    monkeypatch.setenv("GRIMOIRE_LOOM_BASE_URL", "http://local-fake")
    monkeypatch.setattr(jobs, "LoomClient", lambda *args: client(fake))
    monkeypatch.setenv("GRIMOIRE_LOOM_PARTITIONS", "[]")
    with pytest.raises(ValueError, match="partition"):
        await jobs.grimoire_loom_ontology()
    assert fake.calls == []
    config = [{"campaign_id": CAMPAIGN, "player_character_id": PC}]
    monkeypatch.setenv("GRIMOIRE_LOOM_PARTITIONS", json.dumps(config))
    await jobs.grimoire_loom_ontology()
    fake.calls.clear()
    await jobs.grimoire_loom_ontology()
    assert all(c[0] == "GET" for c in fake.calls)
    monkeypatch.setenv("GRIMOIRE_LOOM_PARTITIONS", json.dumps(config * 2))
    with pytest.raises(ValueError, match="duplicate"):
        await jobs.grimoire_loom_ontology()
