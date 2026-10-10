"""Inventory authorization, privacy, audit accounting and atomic table events."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from grimoire.models import InventoryChange, InventoryItem, SessionEvent
from grimoire.testing.leak_harness import sqlite_harness


@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "false")
    with sqlite_harness(tmp_path / "inventory.db") as h, TestClient(h.app()) as client:
        yield h, client


def request(http, method="GET", viewer="dm", item=None, suffix="", **kwargs):
    h, client = http
    path = f"/api/grimoire/campaigns/{h.rows['campaign'].id}/inventory"
    if item is not None:
        path += f"/{item}"
    response = client.request(
        method, path + suffix, headers=h.headers(viewer), **kwargs
    )
    h.assert_no_leak(response, viewer)
    return response


def create(http, **kwargs):
    response = request(
        http, "POST", json={"owner": "party", "name": "Rope", "quantity": 5, **kwargs}
    )
    assert response.status_code == 200, response.text
    return response.json()


def audits(h, item):
    h.session.expire_all()
    return h.session.exec(
        select(InventoryChange)
        .where(InventoryChange.item_id == item)
        .order_by(InventoryChange.created_at, InventoryChange.id)
    ).all()


@pytest.mark.parametrize("key", ("item_a", "item_a_hidden"))
def test_other_pc_denied_every_item_path_without_writes(http, key):
    h, _ = http
    item = h.rows[key].id
    before = h.snapshot()
    for method, suffix, kwargs in (
        ("PATCH", "", {"json": {"quantity": 2}}),
        ("POST", "/move", {"json": {"owner": "party"}}),
        ("GET", "/changes", {"params": {"item_id": item}}),
    ):
        response = request(
            http,
            method,
            "player_b",
            None if method == "GET" else item,
            suffix,
            **kwargs,
        )
        assert response.status_code in (403, 404)
        assert h.snapshot() == before


@pytest.mark.parametrize("viewer", ("player_a", "player_b", "no_character"))
def test_hidden_visibility_and_current_item_audit_scope(http, viewer):
    h, _ = http
    listed = request(http, viewer=viewer).json()
    changes = request(http, viewer=viewer, suffix="/changes").json()
    ids = {row["id"] for row in listed}
    change_ids = {row["item_id"] for row in changes}
    assert ids == change_ids
    assert h.rows["item_party_hidden"].id not in ids
    assert h.rows["item_deleted"].id not in ids
    assert (h.rows["item_a_hidden"].id in ids) == (viewer == "player_a")
    assert (h.rows["item_b"].id in ids) == (viewer == "player_b")
    if viewer == "no_character":
        assert listed == changes == []
    entity_item = next(
        (row for row in listed if row["id"] == h.rows["item_party_entity"].id), None
    )
    if entity_item:
        assert entity_item["entity"] is None
    for row in changes:
        assert "who_member_id" not in row
        assert set(row["changes"].get("owner", {}).values()) <= {
            "party",
            "you",
            "character",
        }


@pytest.mark.parametrize(
    "method,key,suffix,body",
    (
        ("POST", None, "", {"owner": "party", "name": "Rope"}),
        ("DELETE", "item_a", "", None),
        ("PATCH", "item_a", "", {"name": "Renamed"}),
        ("PATCH", "item_a", "", {"notes": "Edited"}),
        ("PATCH", "item_a", "", {"hidden_from_party": True}),
        ("PATCH", "item_a", "", {"entity_id": "$a_only"}),
        ("PATCH", "item_party", "", {"quantity": 1}),
        ("POST", "item_a", "/move", {"owner": "$character"}),
        ("POST", "item_a_hidden", "/move", {"owner": "party"}),
        ("POST", "item_party", "/move", {"owner": "$character"}),
        ("POST", "item_a", "/move", {"owner": "party", "quantity": 4}),
    ),
)
def test_player_cannot_cross_mutation_guards(http, method, key, suffix, body):
    h, _ = http
    if body:
        body = {
            key: h.rows[value[1:]].id
            if isinstance(value, str) and value.startswith("$")
            else value
            for key, value in body.items()
        }
    before = h.snapshot()
    response = request(
        http, method, "player_a", h.rows[key].id if key else None, suffix, json=body
    )
    assert response.status_code in (403, 404, 422)
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "field", ("name", "notes", "entity_id", "hidden_from_party", "quantity")
)
def test_patch_rejects_null(http, field):
    h, _ = http
    before = h.snapshot()
    assert (
        request(
            http, "PATCH", item=h.rows["item_party"].id, json={field: None}
        ).status_code
        == 422
    )
    assert h.snapshot() == before


def test_every_operation_records_exact_audit_and_quantity_conservation(http):
    h, _ = http
    pc = h.rows["character_a"].id
    item = create(http, owner=pc, reason="Given")
    item_id = item["id"]
    assert item["owner"] == pc
    assert [
        (c.action, c.delta, c.quantity_after, c.reason) for c in audits(h, item_id)
    ] == [("create", 5, 5, "Given")]
    assert (
        request(
            http,
            "PATCH",
            "player_a",
            item_id,
            json={"quantity": 7, "reason": "Found two"},
        ).status_code
        == 200
    )
    assert (
        request(
            http,
            "PATCH",
            item=item_id,
            json={"name": "Silk rope", "notes": "Keep dry", "reason": "Inspected"},
        ).status_code
        == 200
    )
    moved = request(
        http,
        "POST",
        "player_a",
        item_id,
        "/move",
        json={"owner": "party", "quantity": 2, "reason": "Shared"},
    )
    assert moved.status_code == 200
    dest_id = moved.json()["id"]
    assert dest_id != item_id and moved.json()["quantity"] == 2
    assert h.session.get(InventoryItem, item_id).quantity == 5
    assert moved.json()["name"] == "Silk rope" and moved.json()["notes"] == "Keep dry"
    assert (
        request(
            http,
            "POST",
            "player_a",
            item_id,
            "/move",
            json={"owner": "party", "reason": "Shared rest"},
        ).json()["id"]
        == item_id
    )
    assert request(http, "DELETE", item=item_id).status_code == 204
    rows = audits(h, item_id)
    assert [(c.action, c.delta, c.quantity_after, c.reason) for c in rows] == [
        ("create", 5, 5, "Given"),
        ("update", 2, 7, "Found two"),
        ("update", 0, 7, "Inspected"),
        ("move", -2, 5, "Shared"),
        ("move", 0, 5, "Shared rest"),
        ("delete", 0, 5, ""),
    ]
    assert [c.who_member_id for c in rows] == [
        h.rows[key].id
        for key in (
            "member_dm",
            "member_player_a",
            "member_dm",
            "member_player_a",
            "member_player_a",
            "member_dm",
        )
    ]
    assert all(c.session_id is None and c.event_id is None for c in rows)
    source = rows[3]
    target = audits(h, dest_id)
    assert len(target) == 1
    assert (
        target[0].action,
        target[0].delta,
        target[0].quantity_after,
        target[0].reason,
    ) == ("move", 2, 2, "Shared")
    assert target[0].who_member_id == h.rows["member_player_a"].id
    assert target[0].changes == source.changes == {"owner": {"from": pc, "to": "party"}}
    assert target[0].session_id is None and target[0].event_id is None
    assert rows[2].changes == {
        "name": {"from": "Rope", "to": "Silk rope"},
        "notes": {"from": "", "to": "Keep dry"},
    }
    assert item_id not in {row["id"] for row in request(http).json()}
    assert (
        len(request(http, suffix="/changes", params={"item_id": item_id}).json()) == 6
    )
    assert (
        request(
            http, viewer="player_a", suffix="/changes", params={"item_id": item_id}
        ).status_code
        == 404
    )


@pytest.mark.parametrize("status", ("active", "paused"))
@pytest.mark.parametrize(
    "operation",
    ("create", "quantity", "fields", "move", "partial", "delete", "hide", "unhide"),
)
def test_play_events_atomic_neutral_and_hidden_suppression(
    http, monkeypatch, status, operation
):
    h, _ = http
    item = create(
        http,
        owner=h.rows["character_a"].id,
        entity_id=h.rows["private"].id,
        hidden_from_party=operation == "unhide",
    )
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    current = h.rows["campaign_session"]
    current.status = status
    h.session.commit()
    before_events = set(h.session.exec(select(SessionEvent.id)).all())
    before_changes = set(h.session.exec(select(InventoryChange.id)).all())
    if operation == "create":
        create(http, entity_id=h.rows["private"].id)
    elif operation in ("quantity", "fields", "hide", "unhide"):
        body = (
            {"quantity": 8}
            if operation == "quantity"
            else {"name": "New rope"}
            if operation == "fields"
            else {"hidden_from_party": operation == "hide"}
        )
        assert request(http, "PATCH", item=item["id"], json=body).status_code == 200
    elif operation in ("move", "partial"):
        body = {"owner": "party", **({"quantity": 2} if operation == "partial" else {})}
        assert (
            request(
                http, "POST", item=item["id"], suffix="/move", json=body
            ).status_code
            == 200
        )
    else:
        assert request(http, "DELETE", item=item["id"]).status_code == 204
    h.session.expire_all()
    changes = h.session.exec(
        select(InventoryChange).where(InventoryChange.id.not_in(before_changes))
    ).all()
    events = h.session.exec(
        select(SessionEvent).where(SessionEvent.id.not_in(before_events))
    ).all()
    assert len(changes) == (2 if operation == "partial" else 1)
    assert all(row.session_id == current.id for row in changes)
    if operation == "hide":
        assert events == [] and changes[0].event_id is None
    else:
        assert len(events) == 1
        event = events[0]
        assert event.kind == "system" and event.audience == "table"
        assert (
            event.audience_pc_ids == []
            and event.author_member_id == h.rows["member_dm"].id
        )
        assert {row.event_id for row in changes} == {event.id}
        assert set(event.body["inventory_change_ids"]) == {row.id for row in changes}
        assert event.body["action"] == changes[0].action
        for key in (
            "character_a",
            "character",
            "member_dm",
            "member_player_a",
            "private",
        ):
            assert h.rows[key].id not in str(event.body)
        assert h.rows["character_a"].character_name not in str(event.body)


def test_hidden_changes_record_session_without_event(http, monkeypatch):
    h, _ = http
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    h.prepare("play")
    before = set(h.session.exec(select(SessionEvent.id)).all())
    item = create(http, hidden_from_party=True)
    assert (
        request(http, "PATCH", item=item["id"], json={"quantity": 2}).status_code == 200
    )
    assert (
        request(
            http,
            "POST",
            item=item["id"],
            suffix="/move",
            json={"owner": h.rows["character"].id},
        ).status_code
        == 200
    )
    assert request(http, "DELETE", item=item["id"]).status_code == 204
    rows = audits(h, item["id"])
    assert len(rows) == 4
    assert all(
        row.session_id == h.rows["campaign_session"].id and row.event_id is None
        for row in rows
    )
    assert set(h.session.exec(select(SessionEvent.id)).all()) == before


def test_no_current_session_even_with_play_enabled(http, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    item = create(http)
    h, _ = http
    assert audits(h, item["id"])[0].session_id is None
    assert audits(h, item["id"])[0].event_id is None


def test_dm_every_owner_and_player_pool_round_trip(http):
    h, _ = http
    for owner in ("party", h.rows["character_a"].id, h.rows["character"].id):
        item = create(http, owner=owner, hidden_from_party=True)
        assert (
            request(
                http,
                "PATCH",
                item=item["id"],
                json={
                    "quantity": 0,
                    "name": "Empty",
                    "notes": "Stowed",
                    "entity_id": h.rows["private"].id,
                },
            ).status_code
            == 200
        )
        assert (
            request(http, "PATCH", item=item["id"], json={"quantity": 5}).status_code
            == 200
        )
        assert (
            request(
                http,
                "POST",
                item=item["id"],
                suffix="/move",
                json={"owner": h.rows["character"].id, "quantity": 2},
            ).status_code
            == 200
        )
        assert request(http, "DELETE", item=item["id"]).status_code == 204
    item = create(http)
    mine = request(
        http,
        "POST",
        "player_a",
        item["id"],
        "/move",
        json={"owner": h.rows["character_a"].id},
    )
    assert mine.status_code == 200 and mine.json()["is_mine"] is True
    assert (
        request(
            http, "POST", "player_a", item["id"], "/move", json={"owner": "party"}
        ).status_code
        == 200
    )
    for viewer in ("player_a", "player_b"):
        changes = request(
            http, viewer=viewer, suffix="/changes", params={"item_id": item["id"]}
        ).json()
        expected = "you" if viewer == "player_a" else "character"
        assert changes[0]["changes"]["owner"] == {"from": expected, "to": "party"}
        assert changes[1]["changes"]["owner"] == {"from": "party", "to": expected}


def test_bad_owner_links_provenance_and_bounds_are_write_free(http):
    h, _ = http
    before = h.snapshot()
    for body in (
        {"owner": str(uuid4())},
        {"owner": "not a UUID"},
        {"owner": h.rows["other_character"].id},
        {"entity_id": h.rows["foreign"].id},
        {"source_event_id": str(uuid4())},
        {"quantity": 0},
        {"quantity": 1000001},
        {"reason": "x" * 501},
        {"name": ""},
        {"who_member_id": h.rows["member_dm"].id},
    ):
        response = request(
            http, "POST", json={"owner": "party", "name": "Rope", **body}
        )
        assert response.status_code in (404, 422), response.text
        assert h.snapshot() == before
    for quantity in (0, 4, None):
        assert (
            request(
                http,
                "POST",
                item=h.rows["item_party"].id,
                suffix="/move",
                json={"owner": "party", "quantity": quantity},
            ).status_code
            == 422
        )
        assert h.snapshot() == before
    for limit in (0, 501):
        assert (
            request(http, suffix="/changes", params={"limit": limit}).status_code == 422
        )


def test_audit_entity_changes_and_source_event_are_viewer_projected(http):
    h, _ = http
    event = next(
        row
        for key, row in h.rows.items()
        if isinstance(row, SessionEvent) and row.audience == "dm"
    )
    item = create(http, source_event_id=event.id, entity_id=h.rows["private"].id)
    assert audits(h, item["id"])[0].changes["source_event_id"] == event.id
    assert (
        request(
            http, "PATCH", item=item["id"], json={"entity_id": h.rows["a_only"].id}
        ).status_code
        == 200
    )
    for viewer in ("player_a", "player_b"):
        rows = request(
            http, viewer=viewer, suffix="/changes", params={"item_id": item["id"]}
        ).json()
        assert "source_event_id" not in rows[1]["changes"]
        assert rows[0]["changes"]["entity_id"] == {
            "from": None,
            "to": h.rows["a_only"].id if viewer == "player_a" else None,
        }


def test_ended_session_rolls_back_item_event_and_audit(http, monkeypatch):
    from grimoire import router
    from grimoire.session_events import SessionEndedError

    h, _ = http
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    h.prepare("play")
    before = h.snapshot()

    def ended(*args, **kwargs):
        raise SessionEndedError("Cannot append to an ended session")

    monkeypatch.setattr(router, "append_event", ended)
    response = request(
        http, "PATCH", item=h.rows["item_party"].id, json={"quantity": 9}
    )
    assert response.status_code == 409
    assert h.snapshot() == before
