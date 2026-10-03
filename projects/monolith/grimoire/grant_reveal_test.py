"""Grant reveals exercise real HTTP authorization, projections and rollback."""

from __future__ import annotations

import copy
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlmodel import select

from grimoire import router, search
from grimoire.models import (
    CampaignMember,
    Embedding,
    Entity,
    GameSession,
    PlayerCharacter,
    SessionEvent,
)
from grimoire.testing.leak_harness import ROLES, fake_knn, sqlite_harness
from grimoire.visibility import visible_entities_query

IDENTITY_KEYS = {"entity_id", "name", "entity_type", "grant_scope"}
MISSING_UUID = "00000000-0000-4000-8000-000000000001"
INVALID_UUIDS = [
    None,
    "not-a-uuid",
    f"urn:uuid:{MISSING_UUID}",
    "{" + MISSING_UUID + "}",
    MISSING_UUID.replace("-", ""),
]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    monkeypatch.setattr(search, "knn_embeddings", fake_knn)
    with sqlite_harness(tmp_path / "reveals.db") as h:
        yield h


def prefix(h):
    return f"/api/grimoire/campaigns/{h.rows['campaign'].id}"


def call(client, h, method, path, viewer="dm", **kwargs):
    response = client.request(
        method, prefix(h) + path, headers=h.headers(viewer), **kwargs
    )
    h.assert_no_leak(response, viewer)
    return response


def poll_all(client, h):
    """Every event GET polls all roles and passes the wire canary scanner."""
    responses = {}
    for viewer in ROLES:
        response = call(
            client,
            h,
            "GET",
            f"/sessions/{h.rows['campaign_session'].id}/events",
            viewer,
        )
        assert response.status_code == (
            404 if viewer in ("outsider", "other_campaign") else 200
        )
        responses[viewer] = response
    return responses


def events(h):
    return h.session.exec(
        select(SessionEvent)
        .where(SessionEvent.kind == "reveal")
        .order_by(SessionEvent.seq)
    ).all()


def remove_grant(h, key):
    grant = h.rows[key]
    details = copy.deepcopy(grant.revealed_details)
    h.session.delete(grant)
    h.session.commit()
    return details


def item(h, entity, pc="character_a", scope="name_only", details=None):
    return {
        "entity_id": h.rows[entity].id,
        "player_character_id": h.rows[pc].id,
        "grant_scope": scope,
        "revealed_details": details,
    }


@pytest.mark.parametrize(
    "entity,scope",
    [("partial", "partial"), ("name_only", "name_only"), ("a_only", "full")],
)
def test_grantee_projection_and_wire_audience(harness, entity, scope):
    h = harness
    details = remove_grant(h, f"grant_{entity}")
    h.prepare("play")
    with TestClient(h.app()) as client:
        response = call(
            client,
            h,
            "POST",
            "/grants",
            json=item(h, entity, scope=scope, details=details),
        )
        assert response.status_code == 200, response.text
        assert response.json()["granted_in_session"] == h.rows["campaign_session"].id
        (row,) = events(h)
        assert row.campaign_id == h.rows["campaign"].id
        assert row.session_id == h.rows["campaign_session"].id
        assert row.audience == "pcs"
        assert row.audience_pc_ids == [h.rows["character_a"].id]
        dm = h.session.exec(
            select(CampaignMember).where(
                CampaignMember.campaign_id == h.rows["campaign"].id,
                CampaignMember.role == "dm",
                CampaignMember.app_user_id == h.rows["user_dm"].id,
            )
        ).one()
        assert row.author_member_id == dm.id
        assert set(row.body) == (
            IDENTITY_KEYS if scope == "name_only" else IDENTITY_KEYS | {"entity"}
        )
        assert row.body["entity_id"] == h.rows[entity].id
        assert row.body["grant_scope"] == scope
        if scope == "partial":
            assert set(row.body["entity"]) == {
                "id",
                "entity_type",
                "name",
                "revealed_details",
            }
            assert row.body["entity"]["revealed_details"] == details
        elif scope == "full":
            assert row.body["entity"]["race"] == h.rows["a_only_detail"].race
            assert (
                row.body["entity"]["description"] == h.rows["a_only_detail"].description
            )
            assert isinstance(row.body["entity"]["created_at"], str)
            datetime.fromisoformat(row.body["entity"]["created_at"])
        h.session.expire(row)
        stored_body = copy.deepcopy(row.body)
        responses = poll_all(client, h)
        for viewer in ("dm", "player_a"):
            (reveal,) = [
                event for event in responses[viewer].json() if event["kind"] == "reveal"
            ]
            assert reveal["body"] == stored_body
            assert reveal["retracted_at"] is None
        for viewer in ("player_b", "no_character"):
            assert not [
                event for event in responses[viewer].json() if event["kind"] == "reveal"
            ]
        if scope == "partial":
            wire = responses["player_a"].text.casefold()
            # This explicit typed-detail canary assertion guards the issue's leak.
            assert h.rows["partial_detail"].description.casefold() not in wire
            for value in (
                h.rows["partial"].source_book,
                h.rows["partial"].site,
                h.rows["partial"].detail["secret"],
                h.rows["partial_detail"].race,
                h.rows["partial_detail"].occupation,
                h.rows["partial_detail"].disposition,
            ):
                assert value.casefold() not in wire


@pytest.mark.parametrize("status", ["active", "paused", "ended"])
@pytest.mark.parametrize("explicit", [False, True])
def test_current_session_and_explicit_session(harness, status, explicit):
    h = harness
    remove_grant(h, "grant_name_only")
    h.rows["campaign_session"].status = status
    # A different ended session is a valid explicit origin, never the event target.
    origin = GameSession(campaign_id=h.rows["campaign"].id, status="ended")
    h.session.add(origin)
    h.session.commit()
    body = item(h, "name_only")
    if explicit:
        body["granted_in_session"] = origin.id
    with TestClient(h.app()) as client:
        response = call(client, h, "POST", "/grants", json=body)
        assert response.status_code == 200
        expected = (
            origin.id
            if explicit
            else (None if status == "ended" else h.rows["campaign_session"].id)
        )
        assert response.json()["granted_in_session"] == expected
        assert len(events(h)) == (0 if status == "ended" else 1)
        if status != "ended":
            assert events(h)[0].session_id == h.rows["campaign_session"].id
        poll_all(client, h)


def test_flag_off_preserves_grants_and_unfiltered_reads(harness, monkeypatch):
    h = harness
    h.prepare("play")
    remove_grant(h, "grant_name_only")
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "false")
    with TestClient(h.app()) as client:
        response = call(client, h, "POST", "/grants", json=item(h, "name_only"))
        assert response.status_code == 200
        assert response.json()["granted_in_session"] is None
        grant_id = response.json()["id"]
        assert (
            call(
                client,
                h,
                "PATCH",
                f"/grants/{grant_id}",
                json={"grant_scope": "partial"},
            ).status_code
            == 200
        )
        assert call(client, h, "DELETE", f"/grants/{grant_id}").status_code == 204
        assert events(h) == []
        assert (
            call(
                client,
                h,
                "POST",
                "/grants/bulk",
                json={"grants": [item(h, "name_only")]},
            ).status_code
            == 404
        )
        for route in ("/entities", "/search"):
            params = {"q": "canary", "k": 50} if route == "/search" else {}
            baseline = call(client, h, "GET", route, params=params)
            assert baseline.status_code == 200
            for viewer in ROLES:
                response = call(
                    client,
                    h,
                    "GET",
                    route,
                    viewer,
                    params={**params, "not_granted_to": h.rows["character_a"].id},
                )
                assert response.status_code == 404
                assert response.json()["detail"] == (
                    "campaign not found"
                    if viewer in ("outsider", "other_campaign")
                    else "Not found"
                )
            monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
            assert (
                call(client, h, "GET", route, params=params).content == baseline.content
            )
            monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "false")
        monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
        poll_all(client, h)


@pytest.mark.parametrize(
    "old,new,count",
    [
        ("name_only", "partial", 1),
        ("partial", "full", 1),
        ("name_only", "full", 1),
        ("full", "partial", 0),
        ("partial", "name_only", 0),
        ("partial", "partial", 0),
        ("partial", None, 0),
    ],
)
def test_strict_upgrades_only_after_details(harness, old, new, count):
    h = harness
    h.prepare("play")
    grant = h.rows["grant_a_only"]
    grant.grant_scope = old
    h.session.commit()
    body = {"revealed_details": {"public": "newly revealed"}}
    if new is not None:
        body["grant_scope"] = new
    with TestClient(h.app()) as client:
        assert (
            call(client, h, "PATCH", f"/grants/{grant.id}", json=body).status_code
            == 200
        )
        assert len(events(h)) == count
        if count:
            assert events(h)[0].body["grant_scope"] == new
            if new == "partial":
                assert (
                    events(h)[0].body["entity"]["revealed_details"]
                    == body["revealed_details"]
                )
        poll_all(client, h)


@pytest.mark.parametrize("silent", [False, True])
def test_retract_identity_and_original_unchanged(harness, silent):
    h = harness
    h.prepare("play")
    remove_grant(h, "grant_partial")
    with TestClient(h.app()) as client:
        response = call(
            client,
            h,
            "POST",
            "/grants",
            json=item(h, "partial", scope="partial", details={"public": "hello"}),
        )
        assert response.status_code == 200
        original = copy.deepcopy(events(h)[0].body)
        response = call(
            client,
            h,
            "DELETE",
            f"/grants/{response.json()['id']}",
            params={"silent": silent},
        )
        assert response.status_code == 204
        first, second = events(h)
        assert first.body == original
        assert first.retracted_at is None
        expected = {"retracted": True, "silent": silent}
        if not silent:
            expected.update(
                {
                    "entity_id": h.rows["partial"].id,
                    "name": h.rows["partial"].name,
                    "entity_type": "npc",
                    "grant_scope": "partial",
                }
            )
        assert second.body == expected
        responses = poll_all(client, h)
        reveals = [
            event for event in responses["player_a"].json() if event["kind"] == "reveal"
        ]
        assert [event["body"] for event in reveals] == [original, expected]
        for value in (h.rows["partial"].id, h.rows["partial"].name):
            assert value.casefold() not in responses["player_b"].text.casefold()


@pytest.mark.parametrize("shape", ["one_pc", "one_entity"])
def test_bulk_shapes_one_event_per_pc_in_order(harness, shape):
    h = harness
    h.prepare("play")
    remove_grant(h, "grant_name_only")
    if shape == "one_pc":
        remove_grant(h, "grant_a_only")
        body = {"grants": [item(h, "name_only"), item(h, "a_only")]}
        expected_pcs = [h.rows["character_a"].id]
    else:
        # Restrict the second PC to a public/global identity with no canary changes.
        body = {"grants": [item(h, "ancestry"), item(h, "ancestry", "character")]}
        expected_pcs = [h.rows["character_a"].id, h.rows["character"].id]
    with TestClient(h.app()) as client:
        response = call(client, h, "POST", "/grants/bulk", json=body)
        assert response.status_code == 200, response.text
        assert len(response.json()) == 2
        assert all(
            grant["granted_in_session"] == h.rows["campaign_session"].id
            for grant in response.json()
        )
        assert [event.audience_pc_ids for event in events(h)] == [
            [pc] for pc in expected_pcs
        ]
        for event in events(h):
            expected_entities = [
                grant["entity_id"]
                for grant in body["grants"]
                if grant["player_character_id"] in event.audience_pc_ids
            ]
            assert set(event.body) == {"reveals"}
            assert [
                reveal["entity_id"] for reveal in event.body["reveals"]
            ] == expected_entities
            assert all(set(reveal) == IDENTITY_KEYS for reveal in event.body["reveals"])
        responses = poll_all(client, h)
        for viewer, pc in (("player_a", "character_a"), ("player_b", "character")):
            admitted = [
                event for event in responses[viewer].json() if event["kind"] == "reveal"
            ]
            assert len(admitted) == (1 if h.rows[pc].id in expected_pcs else 0)


def test_bulk_scope_projections(harness):
    h = harness
    h.prepare("play")
    partial_details = remove_grant(h, "grant_partial")
    remove_grant(h, "grant_a_only")
    remove_grant(h, "grant_name_only")
    grants = [
        item(h, "a_only", scope="full"),
        item(h, "partial", scope="partial", details=partial_details),
        item(h, "name_only"),
    ]
    with TestClient(h.app()) as client:
        response = call(client, h, "POST", "/grants/bulk", json={"grants": grants})
        assert response.status_code == 200
        (event,) = events(h)
        full, partial, name_only = event.body["reveals"]
        assert full["entity"]["description"] == h.rows["a_only_detail"].description
        assert isinstance(full["entity"]["created_at"], str)
        assert set(partial["entity"]) == {
            "id",
            "name",
            "entity_type",
            "revealed_details",
        }
        assert partial["entity"]["revealed_details"] == partial_details
        assert set(name_only) == IDENTITY_KEYS
        responses = poll_all(client, h)
        (wire,) = [
            row for row in responses["player_a"].json() if row["kind"] == "reveal"
        ]
        assert wire["body"] == event.body


@pytest.mark.parametrize("current", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_bulk_session_origin_and_no_current_session(harness, current, explicit):
    h = harness
    if current:
        h.prepare("play")
    origin = GameSession(campaign_id=h.rows["campaign"].id, status="ended")
    h.session.add(origin)
    h.session.commit()
    body = {"grants": [item(h, "ancestry")]}
    if explicit:
        body["granted_in_session"] = origin.id
    with TestClient(h.app()) as client:
        response = call(client, h, "POST", "/grants/bulk", json=body)
        assert response.status_code == 200
        expected = (
            origin.id
            if explicit
            else (h.rows["campaign_session"].id if current else None)
        )
        assert response.json()[0]["granted_in_session"] == expected
        assert len(events(h)) == int(current)
        if current:
            assert events(h)[0].session_id == h.rows["campaign_session"].id
        poll_all(client, h)


@pytest.mark.parametrize(
    "variant,status",
    [
        ("mixed", 422),
        ("duplicate", 422),
        ("conflict", 409),
        ("foreign_pc", 404),
        ("foreign_entity", 404),
        ("missing_entity", 404),
        ("missing_pc", 404),
        ("foreign_session", 404),
        ("extra", 422),
        ("item_extra", 422),
        ("empty", 422),
        ("too_many", 422),
    ],
)
def test_bulk_validates_every_item_before_writes(harness, variant, status):
    h = harness
    h.prepare("play")
    first = item(h, "ancestry")
    second = item(h, "ancestry", "character")
    body = {"grants": [first, second]}
    if variant == "mixed":
        second["entity_id"] = h.rows["class"].id
    elif variant == "duplicate":
        second["player_character_id"] = first["player_character_id"]
    elif variant == "conflict":
        body["grants"] = [first, item(h, "partial")]
    elif variant == "foreign_pc":
        second["player_character_id"] = h.rows["other_character"].id
    elif variant == "foreign_entity":
        body["grants"] = [first, item(h, "foreign")]
    elif variant == "missing_entity":
        body["grants"] = [first, {**first, "entity_id": MISSING_UUID}]
    elif variant == "missing_pc":
        second["player_character_id"] = MISSING_UUID
    elif variant == "foreign_session":
        body["granted_in_session"] = h.rows["other_session"].id
    elif variant == "extra":
        body["extra"] = True
    elif variant == "item_extra":
        second["extra"] = True
    elif variant == "empty":
        body["grants"] = []
    elif variant == "too_many":
        body["grants"] = [first] * 51
    before = h.snapshot()
    with TestClient(h.app()) as client:
        response = call(client, h, "POST", "/grants/bulk", json=body)
        assert response.status_code == status, response.text
        assert h.snapshot() == before
        poll_all(client, h)


@pytest.mark.parametrize(
    "viewer", ["player_a", "player_b", "no_character", "outsider", "other_campaign"]
)
def test_bulk_rejects_non_dm(harness, viewer):
    h = harness
    h.prepare("play")
    before = h.snapshot()
    with TestClient(h.app()) as client:
        response = call(
            client,
            h,
            "POST",
            "/grants/bulk",
            viewer,
            json={"grants": [item(h, "ancestry")]},
        )
        assert response.status_code == (
            404 if viewer in ("outsider", "other_campaign") else 403
        )
        assert h.snapshot() == before


@pytest.mark.parametrize("operation", ["create", "upgrade", "delete", "bulk"])
def test_append_failure_rolls_back_every_write(harness, monkeypatch, operation):
    h = harness
    h.prepare("play")
    before = h.snapshot()
    original = router.append_event
    calls = []

    def fail_append(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == (2 if operation == "bulk" else 1):
            raise RuntimeError("injected append failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(router, "append_event", fail_append)
    with TestClient(h.app(), raise_server_exceptions=False) as client:
        if operation == "create":
            response = call(client, h, "POST", "/grants", json=item(h, "ancestry"))
        elif operation == "upgrade":
            response = call(
                client,
                h,
                "PATCH",
                f"/grants/{h.rows['grant_name_only'].id}",
                json={"grant_scope": "partial"},
            )
        elif operation == "delete":
            response = call(
                client, h, "DELETE", f"/grants/{h.rows['grant_name_only'].id}"
            )
        else:
            response = call(
                client,
                h,
                "POST",
                "/grants/bulk",
                json={
                    "grants": [item(h, "ancestry"), item(h, "ancestry", "character")]
                },
            )
        assert response.status_code == 500
        assert len(calls) == (2 if operation == "bulk" else 1)
        assert h.snapshot() == before
        assert events(h) == []
        poll_all(client, h)


@pytest.mark.parametrize("route", ["/entities", "/search"])
def test_dm_not_granted_to_excludes_every_known_scope_and_globals(harness, route):
    h = harness
    # Include global entities and corpus chunks among vector candidates as well.
    h.session.add_all(
        [
            Embedding(
                embeddable_kind="entity",
                embeddable_id=h.rows["ancestry"].id,
                model="test",
                dim=1024,
                vector=[0.0] * 1024,
            ),
            Embedding(
                embeddable_kind="chunk",
                embeddable_id=h.rows["chunk_private"].id,
                model="test",
                dim=1024,
                vector=[0.0] * 1024,
            ),
        ]
    )
    h.session.commit()
    params = {"q": "canary", "k": 50} if route == "/search" else {}
    with TestClient(h.app()) as client:
        baseline = call(client, h, "GET", route, params=params)
        assert baseline.status_code == 200
        response = call(
            client,
            h,
            "GET",
            route,
            params={**params, "not_granted_to": h.rows["character_a"].id},
        )
        assert response.status_code == 200
        rows = response.json() if route == "/search" else response.json()["items"]
        ids = {row["id"] for row in rows if row.get("kind", "entity") == "entity"}
        assert ids == {h.rows["private"].id, h.rows["b_only"].id}
        if route == "/search":
            assert [row for row in rows if row["kind"] == "chunk"] == [
                row for row in baseline.json() if row["kind"] == "chunk"
            ]
        for viewer in ROLES[1:]:
            rejected = call(
                client,
                h,
                "GET",
                route,
                viewer,
                params={**params, "not_granted_to": h.rows["character_a"].id},
            )
            assert rejected.status_code == (
                404 if viewer in ("outsider", "other_campaign") else 403
            )
        assert (
            call(
                client,
                h,
                "GET",
                route,
                params={**params, "not_granted_to": h.rows["other_character"].id},
            ).status_code
            == 404
        )


def test_reveal_search_uses_correlated_alias():
    statement = visible_entities_query("campaign", "dm", not_granted_to="pc")
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "NOT (EXISTS" in sql
    assert "knowledge_grant AS knowledge_grant_1" in sql
    assert "knowledge_grant_1.entity_id = grimoire.entity.id" in sql
    assert "knowledge_grant_1.campaign_id = 'campaign'" in sql
    assert "knowledge_grant_1.player_character_id = 'pc'" in sql


@pytest.mark.parametrize(
    "field", ["entity_id", "player_character_id", "granted_in_session"]
)
@pytest.mark.parametrize("invalid", INVALID_UUIDS)
def test_bulk_missing_and_malformed_identifiers(harness, monkeypatch, field, invalid):
    h = harness
    before = h.snapshot()
    value = MISSING_UUID if invalid is None else invalid
    body = {"grants": [item(h, "ancestry")]}
    if field == "granted_in_session":
        body[field] = value
    else:
        body["grants"][0][field] = value
    original_get = h.session.get

    def guarded_get(model, identifier, *args, **kwargs):
        if invalid is not None and model in (Entity, PlayerCharacter, GameSession):
            assert identifier != value, "malformed UUID reached a database lookup"
        return original_get(model, identifier, *args, **kwargs)

    monkeypatch.setattr(h.session, "get", guarded_get)
    with TestClient(h.app()) as client:
        response = call(client, h, "POST", "/grants/bulk", json=body)
        assert response.status_code == 404
        assert h.snapshot() == before


@pytest.mark.parametrize("route", ["/entities", "/search"])
@pytest.mark.parametrize("invalid", INVALID_UUIDS)
def test_filter_invalid_pc_preserves_auth_order(harness, monkeypatch, route, invalid):
    h = harness
    value = MISSING_UUID if invalid is None else invalid
    params = {"not_granted_to": value}
    if route == "/search":
        params["q"] = "canary"
    original_get = h.session.get

    def guarded_get(model, identifier, *args, **kwargs):
        if invalid is not None and model is PlayerCharacter:
            assert identifier != value, "malformed UUID reached a database lookup"
        return original_get(model, identifier, *args, **kwargs)

    monkeypatch.setattr(h.session, "get", guarded_get)
    with TestClient(h.app()) as client:
        for flag in ("false", "true"):
            monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", flag)
            for viewer in ROLES:
                response = call(client, h, "GET", route, viewer, params=params)
                if viewer in ("outsider", "other_campaign"):
                    assert response.status_code == 404
                    assert response.json()["detail"] == "campaign not found"
                elif flag == "false":
                    assert response.status_code == 404
                    assert response.json()["detail"] == "Not found"
                elif viewer != "dm":
                    assert response.status_code == 403
                else:
                    assert response.status_code == 404
                    assert (
                        response.json()["detail"]
                        == "player character not found in this campaign"
                    )


@pytest.mark.parametrize("field", ["entity_id", "player_character_id", "both"])
def test_bulk_duplicate_uuid_case_is_422(harness, field):
    h = harness
    first = item(h, "ancestry")
    second = dict(first)
    for key in ("entity_id", "player_character_id"):
        if field in (key, "both"):
            first[key] = first[key].lower()
            second[key] = second[key].upper()
    before = h.snapshot()
    with TestClient(h.app()) as client:
        response = call(
            client, h, "POST", "/grants/bulk", json={"grants": [first, second]}
        )
        assert response.status_code == 422
        assert h.snapshot() == before


def test_bulk_uuid_case_grouping_uses_resolved_character(harness, monkeypatch):
    h = harness
    h.prepare("play")
    original_get = h.session.get

    def uuid_get(model, identifier, *args, **kwargs):
        # Simulate only PostgreSQL's case-insensitive UUID identity comparison.
        if (
            model is PlayerCharacter
            and identifier.casefold() == h.rows["character_a"].id.casefold()
        ):
            return h.rows["character_a"]
        return original_get(model, identifier, *args, **kwargs)

    monkeypatch.setattr(h.session, "get", uuid_get)
    first = item(h, "ancestry")
    second = item(h, "class")
    second["player_character_id"] = second["player_character_id"].lower()
    with TestClient(h.app()) as client:
        response = call(
            client, h, "POST", "/grants/bulk", json={"grants": [first, second]}
        )
        assert response.status_code == 200, response.text
        (event,) = events(h)
        assert event.audience_pc_ids == [h.rows["character_a"].id]
        assert [body["entity_id"] for body in event.body["reveals"]] == [
            first["entity_id"],
            second["entity_id"],
        ]
        assert all(
            grant["player_character_id"] == h.rows["character_a"].id
            for grant in response.json()
        )
        poll_all(client, h)


@pytest.mark.parametrize("enabled", [False, True])
def test_create_reveal_uses_resolved_character_id(harness, monkeypatch, enabled):
    h = harness
    h.prepare("play")
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true" if enabled else "false")
    original_get = h.session.get

    def uuid_get(model, identifier, *args, **kwargs):
        # Production PostgreSQL returns its canonical stored UUID spelling.
        if (
            model is PlayerCharacter
            and identifier.casefold() == h.rows["character_a"].id.casefold()
        ):
            return h.rows["character_a"]
        return original_get(model, identifier, *args, **kwargs)

    monkeypatch.setattr(h.session, "get", uuid_get)
    body = item(h, "ancestry")
    body["player_character_id"] = body["player_character_id"].lower()
    with TestClient(h.app()) as client:
        response = call(client, h, "POST", "/grants", json=body)
        assert response.status_code == 200
        assert response.json()["player_character_id"] == (
            h.rows["character_a"].id if enabled else body["player_character_id"]
        )
        assert len(events(h)) == int(enabled)
        if enabled:
            assert events(h)[0].audience_pc_ids == [h.rows["character_a"].id]
        monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
        poll_all(client, h)
