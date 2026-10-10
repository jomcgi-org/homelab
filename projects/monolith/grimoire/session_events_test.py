"""Storage and HTTP boundary guards, pinned independently of route policy."""

import random
from grimoire.dice import get_dice_rng
from datetime import datetime, timedelta, timezone
from typing import get_args
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, event, null
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire.audience import Audience
from dataclasses import asdict
from grimoire.journal import journal
from grimoire.models import (
    Campaign,
    EventKind,
    GameSession,
    PlayerCharacter,
    SessionEvent,
)
from grimoire.session_events import (
    InvalidEventAudienceError,
    InvalidEventKindError,
    SessionEndedError,
    append_event,
    play_enabled,
    require_play_enabled,
)
from grimoire.testing.leak_harness import ROLES, sqlite_harness


def test_character_assignment_rejects_occupied_and_foreign_characters(http_harness):
    h, client = http_harness
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}"
    target = base + f"/members/{h.rows['member'].id}/character"
    before = h.snapshot()
    for character, status in (("character_a", 409), ("other_character", 404)):
        response = client.put(
            target,
            headers=h.headers("dm"),
            json={"player_character_id": h.rows[character].id},
        )
        assert response.status_code == status, response.text
        assert h.snapshot() == before
    for body in (
        {},
        {"new": None},
        {"new": {"name": "  "}},
        {"new": {"name": "Nyx"}, "player_character_id": h.rows["character_a"].id},
    ):
        assert client.put(target, headers=h.headers("dm"), json=body).status_code == 422
        assert h.snapshot() == before


def test_player_creates_one_character_and_dm_can_create_or_clear(http_harness):
    h, client = http_harness
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}"
    response = client.post(
        base + "/characters/self",
        headers=h.headers("no_character"),
        json={"name": "  Nyx  "},
    )
    assert response.status_code == 200, response.text
    assert response.json()["character_name"] == "Nyx"
    before = h.snapshot()
    assert (
        client.post(
            base + "/characters/self",
            headers=h.headers("no_character"),
            json={"name": "Again"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            base + "/characters/self", headers=h.headers("dm"), json={"name": "Again"}
        ).status_code
        == 403
    )
    assert h.snapshot() == before
    target = base + f"/members/{h.rows['member_no_character'].id}/character"
    cleared = client.put(
        target, headers=h.headers("dm"), json={"player_character_id": None}
    )
    assert cleared.status_code == 200
    assert cleared.json()["character_name"] is None
    created = client.put(
        target, headers=h.headers("dm"), json={"new": {"name": "Wren"}}
    )
    assert created.status_code == 200
    assert created.json()["character_name"] == "Wren"


def test_reassignment_moves_sheet_and_knowledge_access_immediately(http_harness):
    h, client = http_harness
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}"
    character_id = h.rows["character_a"].id
    entity = base + f"/entities/{h.rows['a_only'].id}"
    sheets = base + f"/characters/{character_id}/sheets"
    assert client.get(entity, headers=h.headers("player_a")).status_code == 200
    assert client.get(entity, headers=h.headers("player_b")).status_code == 404
    for member_key, character in (("member_player_a", None), ("member", character_id)):
        response = client.put(
            base + f"/members/{h.rows[member_key].id}/character",
            headers=h.headers("dm"),
            json={"player_character_id": character},
        )
        assert response.status_code == 200, response.text
    assert client.get(entity, headers=h.headers("player_a")).status_code == 404
    assert client.get(sheets, headers=h.headers("player_a")).status_code == 404
    assert client.get(entity, headers=h.headers("player_b")).status_code == 200
    assert client.get(sheets, headers=h.headers("player_b")).status_code == 200


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'events.db'}",
        execution_options={
            "schema_translate_map": {
                table.schema: None
                for table in SQLModel.metadata.tables.values()
                if table.schema
            }
        },
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def game_session(session):
    campaign = Campaign(name="Event test")
    session.add(campaign)
    session.flush()
    row = GameSession(campaign_id=campaign.id)
    session.add(row)
    session.commit()
    return row


def _append(session, game_session, **changes):
    args = {
        "game_session": game_session,
        "kind": "action",
        "audience": Audience("table"),
        "author_member_id": None,
        "body": {},
    }
    args.update(changes)
    return append_event(session, **args)


@pytest.fixture
def http_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    with sqlite_harness(tmp_path / "http-events.db") as h:
        h.prepare("play")
        with TestClient(h.app()) as client:
            yield h, client


def _url(h, suffix="", *, campaign=None, game_session=None):
    return (
        f"/api/grimoire/campaigns/{campaign or h.rows['campaign'].id}/sessions"
        + (f"/{game_session or h.rows['campaign_session'].id}" if suffix else "")
        + suffix
    )


def _post(h, client, viewer="dm", **changes):
    body = {"kind": "action", "audience": "table", "body": {"text": "Move"}}
    body.update(changes)
    return client.post(_url(h, "/events"), headers=h.headers(viewer), json=body)


def test_message_retry_returns_one_event_and_rejects_changed_content(http_harness):
    h, client = http_harness
    token = str(uuid4())
    first = _post(h, client, "player_a", request_id=token)
    assert first.status_code == 200
    before = h.snapshot()
    again = _post(h, client, "player_a", request_id=token)
    assert again.json()["id"] == first.json()["id"]
    assert h.snapshot() == before
    changed = _post(h, client, "player_a", request_id=token, body={"text": "Different"})
    assert changed.status_code == 409
    assert h.snapshot() == before
    other = _post(h, client, "player_b", request_id=token)
    assert other.status_code == 200
    assert other.json()["id"] != first.json()["id"]


def test_message_retry_after_end_or_retraction_never_restores_body(http_harness):
    h, client = http_harness
    token = str(uuid4())
    first = _post(h, client, "player_a", request_id=token)
    event_id = first.json()["id"]
    retracted = client.post(
        _url(h, f"/events/{event_id}/retract"), headers=h.headers("dm")
    )
    assert retracted.status_code == 200
    ended = client.patch(
        _url(h, "/").rstrip("/"), headers=h.headers("dm"), json={"status": "ended"}
    )
    assert ended.status_code == 200
    before = h.snapshot()
    retry = _post(h, client, "player_a", request_id=token)
    assert retry.status_code == 200
    assert retry.json()["id"] == event_id
    assert retry.json()["body"] is None
    assert retry.json()["retracted_at"]
    assert h.snapshot() == before
    assert _post(h, client, "player_a", request_id=str(uuid4())).status_code == 409


@pytest.mark.parametrize("scope", ("partial", "name_only", "full"))
def test_grant_reveal_contains_only_grantee_projection(http_harness, scope):
    h, client = http_harness
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}/grants"
    before = h.snapshot()
    preview = client.post(
        base + "/preview",
        headers=h.headers("dm"),
        json={
            "grants": [
                {
                    "entity_id": h.rows["private"].id,
                    "player_character_id": h.rows["character_a"].id,
                    "grant_scope": scope,
                    "revealed_details": {"clue": "a friendly innkeeper"},
                }
            ]
        },
    )
    assert preview.status_code == 200
    assert h.snapshot() == before
    response = client.post(
        base,
        headers=h.headers("dm"),
        json={
            "entity_id": h.rows["private"].id,
            "player_character_id": h.rows["character_a"].id,
            "grant_scope": scope,
            "revealed_details": {"clue": "a friendly innkeeper"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["granted_in_session"] == h.rows["campaign_session"].id
    a_events = client.get(_url(h, "/events"), headers=h.headers("player_a"))
    reveal = next(row for row in a_events.json() if row["kind"] == "reveal")
    assert reveal["audience_pc_ids"] == [h.rows["character_a"].id]
    projection = reveal["body"].get("entity", preview.json()[0]["projection"])
    if scope != "name_only":
        assert projection == preview.json()[0]["projection"]
    if scope != "full":
        assert "description" not in projection
        assert "detail" not in projection
        assert h.rows["private_detail"].description not in a_events.text
    if scope == "partial":
        assert projection["revealed_details"] == {"clue": "a friendly innkeeper"}
    elif scope == "name_only":
        assert set(projection) == {"id", "name", "entity_type", "recognition_only"}
        assert "a friendly innkeeper" not in a_events.text
    else:
        assert projection["description"] == h.rows["private_detail"].description
        assert isinstance(projection["created_at"], str)
    b_events = client.get(_url(h, "/events"), headers=h.headers("player_b"))
    assert all(row["kind"] != "reveal" for row in b_events.json())


def test_grant_scope_changes_emit_once_and_flag_off_stays_legacy(
    http_harness, monkeypatch
):
    h, client = http_harness
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}/grants"
    grant = h.rows["grant_name_only"]
    for scope in ("partial", "partial", "name_only"):
        response = client.patch(
            f"{base}/{grant.id}", headers=h.headers("dm"), json={"grant_scope": scope}
        )
        assert response.status_code == 200
    events = client.get(_url(h, "/events"), headers=h.headers("player_a")).json()
    assert len([row for row in events if row["kind"] == "reveal"]) == 2
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "false")
    response = client.patch(
        f"{base}/{grant.id}", headers=h.headers("dm"), json={"grant_scope": "full"}
    )
    assert response.status_code == 200
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    events = client.get(_url(h, "/events"), headers=h.headers("player_a")).json()
    assert len([row for row in events if row["kind"] == "reveal"]) == 2


def test_partial_grant_edit_and_downgrade_replace_visible_history(http_harness):
    h, client = http_harness
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}/grants"
    grant = client.post(
        base,
        headers=h.headers("dm"),
        json={
            "entity_id": h.rows["private"].id,
            "player_character_id": h.rows["character_a"].id,
            "grant_scope": "full",
        },
    ).json()
    assert (
        client.patch(
            base + f"/{grant['id']}",
            headers=h.headers("dm"),
            json={
                "grant_scope": "partial",
                "revealed_details": {"clue": "OLD_SHARED_CLUE"},
            },
        ).status_code
        == 200
    )
    assert (
        client.patch(
            base + f"/{grant['id']}",
            headers=h.headers("dm"),
            json={"revealed_details": {"clue": "NEW_SHARED_CLUE"}},
        ).status_code
        == 200
    )
    events = client.get(_url(h, "/events"), headers=h.headers("player_a"))
    assert "OLD_SHARED_CLUE" not in events.text
    assert h.rows["private_detail"].description not in events.text
    current = client.get(_url(h, "/journal"), headers=h.headers("player_a")).json()
    assert len(current["learned"]) == 1
    assert current["learned"][0]["entity"]["revealed_details"] == {
        "clue": "NEW_SHARED_CLUE"
    }


def test_bulk_grants_roll_back_every_grant_and_reveal(http_harness):
    h, client = http_harness
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}"
    first = {
        "entity_id": h.rows["private"].id,
        "player_character_id": h.rows["character_a"].id,
        "grant_scope": "name_only",
    }
    response = client.post(
        base + "/grants/bulk",
        headers=h.headers("dm"),
        json={"grants": [first, {**first, "entity_id": str(uuid4())}]},
    )
    assert response.status_code == 404
    grants = client.get(base + "/grants", headers=h.headers("dm")).json()
    assert not any(row["entity_id"] == first["entity_id"] for row in grants)
    events = client.get(_url(h, "/events"), headers=h.headers("player_a")).json()
    assert not any(row["kind"] == "reveal" for row in events)
    response = client.post(
        base + "/grants/bulk",
        headers=h.headers("dm"),
        json={
            "grants": [first, {**first, "player_character_id": h.rows["character"].id}]
        },
    )
    assert response.status_code == 200
    for role in ("player_a", "player_b"):
        events = client.get(_url(h, "/events"), headers=h.headers(role)).json()
        assert len([row for row in events if row["kind"] == "reveal"]) == 1


@pytest.mark.parametrize("change", ("retract", "downgrade", "edit"))
@pytest.mark.parametrize("ended", (False, True))
def test_grouped_reveal_preserves_other_items_and_scrubs_changed_projection(
    http_harness, change, ended
):
    h, client = http_harness
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}"
    a_id, b_id = h.rows["character_a"].id, h.rows["character"].id
    private_id, second_id = h.rows["private"].id, h.rows["b_only"].id
    response = client.post(
        base + "/grants/bulk",
        headers=h.headers("dm"),
        json={
            "grants": [
                {
                    "entity_id": private_id,
                    "player_character_id": a_id,
                    "grant_scope": "partial",
                    "revealed_details": {"clue": "OLD_BATCH_CLUE"},
                },
                {
                    "entity_id": second_id,
                    "player_character_id": a_id,
                    "grant_scope": "name_only",
                },
            ]
        },
    )
    assert response.status_code == 200, response.text
    grants = response.json()
    response = client.post(
        base + "/grants/bulk",
        headers=h.headers("dm"),
        json={
            "grants": [
                {
                    "entity_id": private_id,
                    "player_character_id": b_id,
                    "grant_scope": "full",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text

    def events(role):
        return client.get(_url(h, "/events"), headers=h.headers(role)).json()

    a_reveals = [row for row in events("player_a") if row["kind"] == "reveal"]
    b_before = events("player_b")
    b_reveals = [row for row in b_before if row["kind"] == "reveal"]
    assert len(a_reveals) == len(b_reveals) == 1
    grouped_id = a_reveals[0]["id"]
    assert [item["entity_id"] for item in a_reveals[0]["body"]["reveals"]] == [
        private_id,
        second_id,
    ]
    assert "OLD_BATCH_CLUE" not in str(b_reveals)
    assert h.rows["private"].detail["secret"] not in str(a_reveals)
    current = client.get(_url(h, "/journal"), headers=h.headers("player_a")).json()
    assert {entry["entity_id"] for entry in current["learned"]} == {
        private_id,
        second_id,
    }
    pinned = client.post(
        base + "/notes",
        headers=h.headers("player_a"),
        json={"kind": "character", "from_event_id": grouped_id},
    )
    assert pinned.status_code == 200, pinned.text
    assert "OLD_BATCH_CLUE" in pinned.json()["markdown"]
    assert set([item["id"] for item in pinned.json()["links"]["entities"]]) == {
        private_id,
        second_id,
    }
    if ended:
        assert (
            client.patch(
                _url(h) + f"/{h.rows['campaign_session'].id}",
                headers=h.headers("dm"),
                json={"status": "ended"},
            ).status_code
            == 200
        )
    target = base + f"/grants/{grants[0]['id']}"
    if change == "retract":
        changed = client.delete(target, headers=h.headers("dm"))
    else:
        changed = client.patch(
            target,
            headers=h.headers("dm"),
            json={
                "grant_scope": "name_only" if change == "downgrade" else "partial",
                "revealed_details": {"clue": "NEW_BATCH_CLUE"},
            },
        )
    assert changed.status_code in (200, 204), changed.text
    a_after = events("player_a")
    original = next(row for row in a_after if row["id"] == grouped_id)
    assert original["retracted_at"] is None
    assert original["body"] == {"reveals": [a_reveals[0]["body"]["reveals"][1]]}
    assert "OLD_BATCH_CLUE" not in str(a_after)
    assert events("player_b") == b_before
    current = client.get(_url(h, "/journal"), headers=h.headers("player_a")).json()
    assert "OLD_BATCH_CLUE" not in str(current)
    campaign_journal = client.get(base + "/journal", headers=h.headers("player_a"))
    assert campaign_journal.status_code == 200
    assert "OLD_BATCH_CLUE" not in campaign_journal.text
    assert any(entry["entity_id"] == second_id for entry in current["learned"])
    again = client.post(
        base + "/notes",
        headers=h.headers("player_a"),
        json={"kind": "character", "from_event_id": grouped_id},
    )
    assert again.status_code == 200
    assert [item["id"] for item in again.json()["links"]["entities"]] == [second_id]
    assert "OLD_BATCH_CLUE" not in again.json()["markdown"]
    raw = next(row for row in events("dm") if row["id"] == grouped_id)
    assert "OLD_BATCH_CLUE" in str(raw["body"])


@pytest.mark.parametrize("silent", (True, False))
def test_grant_retraction_scrubs_history_and_emits_recipient_event(
    http_harness, silent
):
    h, client = http_harness
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}/grants"
    grant = client.post(
        base,
        headers=h.headers("dm"),
        json={
            "entity_id": h.rows["private"].id,
            "player_character_id": h.rows["character_a"].id,
            "grant_scope": "partial",
            "revealed_details": {"clue": "RETRACTED_CLUE"},
        },
    ).json()
    response = client.delete(
        f"{base}/{grant['id']}?silent={str(silent).lower()}", headers=h.headers("dm")
    )
    assert response.status_code == 204
    events_response = client.get(_url(h, "/events"), headers=h.headers("player_a"))
    assert "RETRACTED_CLUE" not in events_response.text
    reveals = [row for row in events_response.json() if row["kind"] == "reveal"]
    assert len(reveals) == 2
    assert reveals[0]["body"] is None
    assert (
        reveals[1]["body"] == {"retracted": True, "silent": True}
        if silent
        else reveals[1]["body"]["retracted"]
    )
    b_events = client.get(_url(h, "/events"), headers=h.headers("player_b")).json()
    assert not any(row["kind"] == "reveal" for row in b_events)


def test_ungranted_filter_is_dm_only_and_campaign_scoped(http_harness):
    h, client = http_harness
    url = f"/api/grimoire/campaigns/{h.rows['campaign'].id}/entities?not_granted_to={h.rows['character_a'].id}"
    response = client.get(url, headers=h.headers("dm"))
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["items"]}
    assert h.rows["private"].id in ids
    assert h.rows["a_only"].id not in ids
    assert client.get(url, headers=h.headers("player_a")).status_code == 403


def test_private_notes_are_hidden_from_dm_and_other_player(http_harness):
    h, client = http_harness
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}/notes"
    created = client.post(
        base,
        headers=h.headers("player_a"),
        json={
            "kind": "character",
            "title": "PRIVATE_NOTE_CANARY",
            "markdown": "A_PRIVATE_THOUGHT",
        },
    )
    assert created.status_code == 200
    note = created.json()
    assert note["dm_readable"] is False
    for role in ("dm", "player_b", "no_character", "outsider", "other_campaign"):
        assert (
            "PRIVATE_NOTE_CANARY" not in client.get(base, headers=h.headers(role)).text
        )
        assert (
            "PRIVATE_NOTE_CANARY"
            not in client.get(
                base + "?q=PRIVATE_NOTE_CANARY", headers=h.headers(role)
            ).text
        )
        assert (
            client.get(f"{base}/{note['id']}", headers=h.headers(role)).status_code
            == 404
        )
        assert (
            client.patch(
                f"{base}/{note['id']}",
                headers=h.headers(role),
                json={"title": "Stolen"},
            ).status_code
            == 404
        )
        assert (
            client.delete(f"{base}/{note['id']}", headers=h.headers(role)).status_code
            == 404
        )
    shared = client.patch(
        f"{base}/{note['id']}",
        headers=h.headers("player_a"),
        json={"dm_readable": True},
    )
    assert shared.status_code == 200
    assert (
        client.get(f"{base}/{note['id']}", headers=h.headers("dm")).status_code == 200
    )
    assert (
        client.get(f"{base}/{note['id']}", headers=h.headers("player_b")).status_code
        == 404
    )
    assert (
        client.patch(
            f"{base}/{note['id']}", headers=h.headers("dm"), json={"title": "DM edit"}
        ).status_code
        == 403
    )
    assert (
        client.delete(f"{base}/{note['id']}", headers=h.headers("player_a")).status_code
        == 204
    )
    for role in ("dm", "player_a", "player_b"):
        assert (
            client.get(f"{base}/{note['id']}", headers=h.headers(role)).status_code
            == 404
        )
        assert (
            "PRIVATE_NOTE_CANARY" not in client.get(base, headers=h.headers(role)).text
        )


def test_party_notes_and_visible_event_pin(http_harness):
    h, client = http_harness
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}"
    grant = client.post(
        base + "/grants",
        headers=h.headers("dm"),
        json={
            "entity_id": h.rows["private"].id,
            "player_character_id": h.rows["character_a"].id,
            "grant_scope": "partial",
            "revealed_details": {"clue": "ONLY_VISIBLE_CLUE"},
        },
    )
    assert grant.status_code == 200
    reveal = next(
        row
        for row in client.get(_url(h, "/events"), headers=h.headers("player_a")).json()
        if row["kind"] == "reveal"
    )
    created = client.post(
        base + "/notes",
        headers=h.headers("player_a"),
        json={"kind": "character", "from_event_id": reveal["id"]},
    )
    assert created.status_code == 200
    assert "ONLY_VISIBLE_CLUE" in created.json()["markdown"]
    assert h.rows["private_detail"].description not in created.text
    assert (
        client.post(
            base + "/notes",
            headers=h.headers("player_b"),
            json={"kind": "character", "from_event_id": reveal["id"]},
        ).status_code
        == 404
    )
    party = client.post(
        base + "/notes",
        headers=h.headers("player_a"),
        json={
            "kind": "party",
            "title": "PARTY_NOTE_CANARY",
            "markdown": "The bridge is closed.",
        },
    )
    assert party.status_code == 200
    for role in ("dm", "player_a", "player_b"):
        assert (
            "PARTY_NOTE_CANARY"
            in client.get(base + "/notes?kind=party", headers=h.headers(role)).text
        )
    assert (
        "PARTY_NOTE_CANARY"
        not in client.get(base + "/notes", headers=h.headers("no_character")).text
    )


PLAY_ROUTES = (
    ("GET", "journal"),
    ("GET", "campaign_journal"),
    ("GET", "list"),
    ("GET", "current"),
    ("POST", "append"),
    ("GET", "poll"),
    ("POST", "retract"),
    ("POST", "roll"),
)


@pytest.mark.parametrize("method,route", PLAY_ROUTES)
@pytest.mark.parametrize("viewer", (*ROLES, "anonymous"))
@pytest.mark.parametrize("flag", (None, "false"))
def test_all_play_routes_flag_off_before_auth(
    http_harness, monkeypatch, method, route, viewer, flag
):
    h, client = http_harness
    if flag is None:
        monkeypatch.delenv("GRIMOIRE_PLAY_ENABLED")
    else:
        monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", flag)
    paths = {
        "journal": _url(h, "/journal"),
        "campaign_journal": f"/api/grimoire/campaigns/{h.rows['campaign'].id}/journal",
        "list": _url(h),
        "current": _url(h) + "/current",
        "append": _url(h, "/events"),
        "poll": _url(h, "/events"),
        "retract": _url(h, f"/events/{h.rows['event_table'].id}/retract"),
        "roll": _url(h, "/rolls"),
    }
    before = h.snapshot()
    response = client.request(
        method,
        paths[route],
        headers={} if viewer == "anonymous" else h.headers(viewer),
        json={"kind": "action", "audience": "table", "body": {}},
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Not found"}
    assert h.snapshot() == before


def test_journal_projection_random_audiences_never_include_hidden_canaries(
    http_harness,
):
    import json
    from random import Random

    h, _ = http_harness
    rng = Random(6617)
    seeded = [row for key, row in h.rows.items() if key.startswith("event_")]
    for role in ("dm", "player_a", "player_b", "no_character"):
        member = h.rows["member" if role == "player_b" else f"member_{role}"]
        viewer = "dm" if role == "dm" else member.player_character_id
        for _ in range(30):
            events = []
            for index in range(50):
                seed = rng.choice(seeded)
                kind = rng.choice(("reveal", "handout", "narration", "roll", "action"))
                events.append(
                    {
                        "id": seed.id,
                        "session_id": seed.session_id,
                        "audience": seed.audience,
                        "audience_pc_ids": seed.audience_pc_ids,
                        "author_member_id": seed.author_member_id,
                        "kind": kind,
                        "retracted_at": seed.retracted_at,
                        "body": {
                            "entity_id": seed.id,
                            "name": seed.body["secret"],
                            "text": seed.body["secret"],
                        },
                    }
                )
            result = json.dumps(
                asdict(
                    journal(
                        viewer,
                        member,
                        [
                            SessionEvent(**event, seq=index)
                            for index, event in enumerate(events)
                        ],
                        current_grants=set(),
                        visible_entities={},
                    )
                )
            )
            for token, (allowed, _) in h.canaries.items():
                if role not in allowed:
                    assert token not in result


def test_journal_latest_reveal_retraction_own_rolls_and_open_threads(http_harness):
    h, client = http_harness
    member = h.rows["member_player_a"]
    own_action = _post(
        h,
        client,
        "player_a",
        kind="action",
        audience="dm",
        body={"text": "OPEN_THREAD"},
    ).json()
    own_roll = client.post(
        _url(h, "/rolls"),
        headers=h.headers("player_a"),
        json={"formula": "1d20", "visibility": "dm", "label": "MY_PRIVATE_ROLL"},
    )
    assert own_roll.status_code == 200
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}"
    grant = client.post(
        base + "/grants",
        headers=h.headers("dm"),
        json={
            "entity_id": h.rows["private"].id,
            "player_character_id": member.player_character_id,
            "grant_scope": "name_only",
        },
    ).json()
    assert (
        client.patch(
            base + f"/grants/{grant['id']}",
            headers=h.headers("dm"),
            json={
                "grant_scope": "partial",
                "revealed_details": {"clue": "JOURNAL_CLUE"},
            },
        ).status_code
        == 200
    )
    current = client.get(_url(h, "/journal"), headers=h.headers("player_a")).json()
    assert len(current["learned"]) == 1
    assert current["learned"][0]["grant_scope"] == "partial"
    assert current["rolls"][0]["body"]["label"] == "MY_PRIVATE_ROLL"
    assert current["open_threads"][0]["body"]["text"] == "OPEN_THREAD"
    party = client.get(
        _url(h, "/journal?view=party"), headers=h.headers("player_a")
    ).text
    assert (
        "JOURNAL_CLUE" not in party
        and "OPEN_THREAD" not in party
        and "MY_PRIVATE_ROLL" not in party
    )
    assert (
        _post(
            h,
            client,
            kind="narration",
            audience="pcs",
            audience_pc_ids=[member.player_character_id],
            body={
                "text": "Still investigating",
                "reply_to": own_action["id"],
                "resolved": False,
            },
        ).status_code
        == 200
    )
    unresolved = client.get(_url(h, "/journal"), headers=h.headers("player_a")).json()
    assert any(entry["id"] == own_action["id"] for entry in unresolved["open_threads"])
    assert (
        _post(
            h,
            client,
            kind="narration",
            audience="pcs",
            audience_pc_ids=[member.player_character_id],
            body={"text": "Reply", "reply_to": own_action["id"], "resolved": True},
        ).status_code
        == 200
    )
    assert (
        client.delete(
            base + f"/grants/{grant['id']}", headers=h.headers("dm")
        ).status_code
        == 204
    )
    current = client.get(_url(h, "/journal"), headers=h.headers("player_a")).json()
    assert current["open_threads"] == []
    assert current["learned"][0]["retracted"] is True
    assert "JOURNAL_CLUE" not in str(current)
    assert current["people_and_places"] == []
    campaign = client.get(base + "/journal?limit=1", headers=h.headers("player_a"))
    assert campaign.status_code == 200
    assert campaign.json()["sessions"][0]["journal"] == current
    older = GameSession(
        campaign_id=h.rows["campaign"].id,
        status="ended",
        started_at=h.rows["campaign_session"].started_at - timedelta(days=1),
        ended_at=datetime.now(timezone.utc),
    )
    h.session.add(older)
    h.session.commit()
    first = client.get(base + "/journal?limit=1", headers=h.headers("player_a")).json()
    assert first["next_cursor"] is not None
    second = client.get(
        base + "/journal?limit=1&cursor=" + first["next_cursor"],
        headers=h.headers("player_a"),
    ).json()
    assert second["sessions"][0]["session_id"] == older.id
    assert second["next_cursor"] is None


def test_existing_session_writes_work_with_flag_off(http_harness, monkeypatch):
    h, client = http_harness
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "false")
    response = client.patch(
        _url(h, "") + f"/{h.rows['campaign_session'].id}",
        headers=h.headers("dm"),
        json={"status": "ended"},
    )
    assert response.status_code == 200
    response = client.post(_url(h), headers=h.headers("dm"))
    assert response.status_code == 200
    assert response.json()["status"] == "active"


@pytest.mark.parametrize("viewer", ("dm", "player_a", "player_b", "no_character"))
def test_session_list_newest_and_current_active_or_paused(http_harness, viewer):
    h, client = http_harness
    current = h.rows["campaign_session"]
    older = GameSession(
        campaign_id=current.campaign_id,
        status="ended",
        started_at=current.started_at - timedelta(days=1),
    )
    h.session.add(older)
    h.session.commit()
    response = client.get(_url(h), headers=h.headers(viewer))
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [current.id, older.id]
    for status in ("active", "paused"):
        current.status = status
        h.session.commit()
        response = client.get(_url(h) + "/current", headers=h.headers(viewer))
        assert response.status_code == 200
        assert response.json()["id"] == current.id
        assert response.json()["status"] == status
    current.status = "ended"
    h.session.commit()
    assert (
        client.get(_url(h) + "/current", headers=h.headers(viewer)).status_code == 404
    )


@pytest.mark.parametrize("viewer", ("player_a", "player_b", "no_character"))
@pytest.mark.parametrize(
    "kind", ("narration", "reveal", "roll", "handout", "turn", "system", "utterance")
)
def test_player_forbidden_kinds(http_harness, viewer, kind):
    h, client = http_harness
    before = h.snapshot()
    response = _post(h, client, viewer, kind=kind)
    assert response.status_code == 403
    assert h.snapshot() == before


@pytest.mark.parametrize("viewer", ("player_a", "player_b", "no_character"))
def test_player_cannot_post_pcs_audience(http_harness, viewer):
    h, client = http_harness
    before = h.snapshot()
    assert (
        _post(
            h,
            client,
            viewer,
            audience="pcs",
            audience_pc_ids=[h.rows["character_a"].id],
        ).status_code
        == 403
    )
    assert h.snapshot() == before


def test_dm_utterance_ingest_only(http_harness):
    h, client = http_harness
    before = h.snapshot()
    assert _post(h, client, kind="utterance").status_code == 403
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "viewer,member_key",
    (
        ("dm", "member_dm"),
        ("player_a", "member_player_a"),
        ("player_b", "member"),
        ("no_character", "member_no_character"),
    ),
)
@pytest.mark.parametrize("audience", ("dm", "table"))
def test_allowed_actions_have_authenticated_author(
    http_harness, viewer, member_key, audience
):
    h, client = http_harness
    response = _post(h, client, viewer, audience=audience)
    assert response.status_code == 200, response.text
    assert response.json()["seq"] == 9
    assert response.json()["audience_pc_ids"] == []
    row = h.session.get(SessionEvent, response.json()["id"])
    assert row.author_member_id == h.rows[member_key].id
    assert row.campaign_id == h.rows["campaign"].id
    assert row.session_id == h.rows["campaign_session"].id
    assert row.kind == "action"
    assert row.body == {"text": "Move"}


@pytest.mark.parametrize(
    "kind", ("narration", "action", "reveal", "handout", "turn", "system")
)
@pytest.mark.parametrize("audience", ("dm", "table", "pcs"))
def test_dm_can_post_all_non_ingest_kinds_and_audiences(http_harness, kind, audience):
    h, client = http_harness
    ids = [h.rows["character_a"].id] if audience == "pcs" else []
    response = _post(h, client, kind=kind, audience=audience, audience_pc_ids=ids)
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == kind
    assert response.json()["audience"] == audience


@pytest.mark.parametrize("viewer", ("dm", "player_a"))
def test_ended_session_refuses_append(http_harness, viewer):
    h, client = http_harness
    h.rows["campaign_session"].status = "ended"
    h.session.commit()
    before = h.snapshot()
    assert _post(h, client, viewer).status_code == 409
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "changes",
    (
        {"audience": "unknown"},
        {"audience": "pcs", "audience_pc_ids": []},
        {"audience": "pcs", "audience_pc_ids": ["not-a-uuid"]},
        {"audience": "table", "audience_pc_ids": [str(uuid4())]},
        {"audience": "dm", "audience_pc_ids": [str(uuid4())]},
        {"audience": "pcs", "audience_pc_ids": [str(uuid4())]},
        {"kind": "unknown"},
        {"author_member_id": str(uuid4())},
        {"body": None},
    ),
)
def test_invalid_event_request_is_422_without_writes(http_harness, changes):
    h, client = http_harness
    before = h.snapshot()
    assert _post(h, client, **changes).status_code == 422
    assert h.snapshot() == before


def test_foreign_and_mixed_pc_audiences_are_422(http_harness):
    h, client = http_harness
    before = h.snapshot()
    for ids in (
        [h.rows["other_character"].id],
        [h.rows["character_a"].id, h.rows["other_character"].id],
    ):
        assert _post(h, client, audience="pcs", audience_pc_ids=ids).status_code == 422
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "viewer,keys",
    (
        (
            "player_a",
            (
                "event_character_a",
                "event_character_a_retracted",
                "event_table",
                "event_table_retracted",
            ),
        ),
        (
            "player_b",
            (
                "event_character",
                "event_character_retracted",
                "event_table",
                "event_table_retracted",
            ),
        ),
        ("no_character", ("event_table", "event_table_retracted")),
    ),
)
@pytest.mark.parametrize("limit", (1, 2))
def test_advancing_polls_never_leak_restricted_or_retracted_events(
    http_harness, viewer, keys, limit
):
    h, client = http_harness
    seen = []
    after = 0
    for _ in range(6):
        response = client.get(
            _url(h, "/events"),
            headers=h.headers(viewer),
            params={"after": after, "limit": limit},
        )
        assert response.status_code == 200
        h.assert_no_leak(response, viewer)
        rows = response.json()
        if not rows:
            break
        assert all(row["seq"] > after for row in rows)
        assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
        seen.extend(row["id"] for row in rows)
        after = rows[-1]["seq"]
    assert seen == [h.rows[key].id for key in keys]


def test_dm_narration_for_pc_a_only_canary(http_harness):
    h, client = http_harness
    token = h.token("http.narration.body", ("dm", "player_a"))
    response = _post(
        h,
        client,
        kind="narration",
        audience="pcs",
        audience_pc_ids=[h.rows["character_a"].id],
        body={"secret": token},
    )
    assert response.status_code == 200
    event_id = response.json()["id"]
    for viewer in ROLES:
        response = client.get(_url(h, "/events"), headers=h.headers(viewer))
        h.assert_no_leak(response, viewer)
        if viewer in ("outsider", "other_campaign"):
            assert response.status_code == 404
        else:
            assert response.status_code == 200
            assert (event_id in [row["id"] for row in response.json()]) == (
                viewer in ("dm", "player_a")
            )
            assert (token in response.text) == (viewer in ("dm", "player_a"))


def test_event_projection_keeps_other_member_and_pc_ids_private(http_harness):
    h, client = http_harness
    response = _post(
        h,
        client,
        kind="narration",
        audience="pcs",
        audience_pc_ids=[h.rows["character_a"].id, h.rows["character"].id],
    )
    assert response.status_code == 200
    event_id = response.json()["id"]
    for viewer, pc_key in (
        ("dm", None),
        ("player_a", "character_a"),
        ("player_b", "character"),
    ):
        response = client.get(_url(h, "/events"), headers=h.headers(viewer))
        h.assert_no_leak(response, viewer)
        row = next(row for row in response.json() if row["id"] == event_id)
        assert row["audience_pc_ids"] == (
            sorted([h.rows["character_a"].id, h.rows["character"].id])
            if viewer == "dm"
            else [h.rows[pc_key].id]
        )
        assert row["author_member_id"] == (
            h.rows["member_dm"].id if viewer == "dm" else None
        )


def test_poll_scopes_campaign_and_session(http_harness):
    h, client = http_harness
    second = GameSession(campaign_id=h.rows["campaign"].id, status="ended")
    h.session.add(second)
    h.session.flush()
    other_event = SessionEvent(
        campaign_id=h.rows["campaign"].id,
        session_id=second.id,
        seq=1,
        kind="system",
        audience="table",
        body={"foreign": "session"},
    )
    # The query also fails closed if an out-of-band writer mismatches campaign.
    mismatched = SessionEvent(
        campaign_id=h.rows["other"].id,
        session_id=h.rows["campaign_session"].id,
        seq=9,
        kind="system",
        audience="table",
        body={"foreign": "campaign"},
    )
    h.session.add_all([other_event, mismatched])
    h.session.commit()
    response = client.get(_url(h, "/events"), headers=h.headers("dm"))
    assert response.status_code == 200
    assert [row["seq"] for row in response.json()] == list(range(1, 9))
    response = client.get(
        _url(h, "/events", game_session=second.id), headers=h.headers("dm")
    )
    assert [row["id"] for row in response.json()] == [other_event.id]
    response = client.post(
        _url(h, f"/events/{mismatched.id}/retract"), headers=h.headers("dm")
    )
    assert response.status_code == 404


def test_poll_filters_in_one_sql_query_before_limit(http_harness):
    h, client = http_harness
    statements = []
    connection = h.session.connection()

    def capture(conn, cursor, statement, parameters, context, many):
        if "FROM session_event" in statement:
            statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        response = client.get(
            _url(h, "/events"), headers=h.headers("player_a"), params={"limit": 1}
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [h.rows["event_character_a"].id]
    assert len(statements) == 1
    assert "json_each" in statements[0]
    assert "session_event.seq >" in statements[0]
    assert "ORDER BY session_event.seq" in statements[0]
    assert "LIMIT" in statements[0]


def test_poll_literal_default_maximum_and_strict_after(http_harness):
    h, client = http_harness
    h.session.add_all(
        [
            SessionEvent(
                campaign_id=h.rows["campaign"].id,
                session_id=h.rows["campaign_session"].id,
                seq=seq,
                kind="system",
                audience="table",
                body={},
            )
            for seq in range(9, 510)
        ]
    )
    h.session.commit()

    def poll(**params):
        return client.get(_url(h, "/events"), headers=h.headers("dm"), params=params)

    rows = poll().json()
    assert len(rows) == 100
    assert [row["seq"] for row in rows] == list(range(1, 101))
    assert len(poll(limit=500).json()) == 500
    assert poll(limit=501).status_code == 422
    assert poll(limit=0).status_code == 422
    assert poll(limit=-1).status_code == 422
    assert poll(after=-1).status_code == 422
    assert poll(after="bad").status_code == 422
    assert [row["seq"] for row in poll(after=100, limit=1).json()] == [101]
    assert poll(after=509).json() == []


def test_retraction_permissions_body_projection_and_repeat(http_harness):
    h, client = http_harness
    response = _post(h, client, "player_a")
    own_id = response.json()["id"]
    own_url = _url(h, f"/events/{own_id}/retract")
    before = h.snapshot()
    assert client.post(own_url, headers=h.headers("player_b")).status_code == 403
    for key in ("event_table", "event_dm"):
        assert (
            client.post(
                _url(h, f"/events/{h.rows[key].id}/retract"),
                headers=h.headers("player_a"),
            ).status_code
            == 403
        )
    assert h.snapshot() == before
    response = client.post(own_url, headers=h.headers("player_a"))
    assert response.status_code == 200
    assert response.json()["body"] is None
    timestamp = response.json()["retracted_at"]
    assert timestamp.endswith("Z")
    before = h.snapshot()
    repeated = client.post(own_url, headers=h.headers("player_a"))
    assert repeated.status_code == 200
    assert repeated.json()["retracted_at"] == timestamp
    assert h.snapshot() == before
    for viewer in ("dm", "player_a", "player_b", "no_character"):
        response = client.get(_url(h, "/events"), headers=h.headers(viewer))
        row = next(row for row in response.json() if row["id"] == own_id)
        assert row["retracted_at"] == timestamp
        assert row["body"] == ({"text": "Move"} if viewer == "dm" else None)
    response = client.post(own_url, headers=h.headers("dm"))
    assert response.status_code == 200
    assert response.json()["body"] == {"text": "Move"}
    assert response.json()["retracted_at"] == timestamp


@pytest.mark.parametrize("viewer", ("dm", "player_a"))
def test_retraction_allowed_after_session_ends(http_harness, viewer):
    h, client = http_harness
    event_id = _post(h, client, "player_a").json()["id"]
    h.rows["campaign_session"].status = "ended"
    h.session.commit()
    response = client.post(
        _url(h, f"/events/{event_id}/retract"), headers=h.headers(viewer)
    )
    assert response.status_code == 200
    assert response.json()["retracted_at"] is not None


def test_player_cannot_retract_own_server_side_roll(http_harness):
    h, client = http_harness
    row = append_event(
        h.session,
        game_session=h.rows["campaign_session"],
        kind="roll",
        audience=Audience("table"),
        author_member_id=h.rows["member_player_a"].id,
        body={"dice": "d20"},
    )
    h.session.commit()
    before = h.snapshot()
    response = client.post(
        _url(h, f"/events/{row.id}/retract"), headers=h.headers("player_a")
    )
    assert response.status_code == 403
    assert h.snapshot() == before
    response = client.post(
        _url(h, f"/events/{row.id}/retract"), headers=h.headers("dm")
    )
    assert response.status_code == 200


def test_retraction_locks_and_refreshes_event(http_harness, monkeypatch):
    h, client = http_harness
    original_exec = h.session.exec
    statements = []

    def capture(statement, *args, **kwargs):
        if any(
            description.get("entity") is SessionEvent
            for description in statement.column_descriptions
        ):
            statements.append(statement)
        return original_exec(statement, *args, **kwargs)

    monkeypatch.setattr(h.session, "exec", capture)
    response = client.post(
        _url(h, f"/events/{h.rows['event_table'].id}/retract"), headers=h.headers("dm")
    )
    assert response.status_code == 200
    assert len(statements) == 1
    assert str(statements[0].compile(dialect=postgresql.dialect())).endswith(
        "FOR UPDATE"
    )
    assert statements[0].get_execution_options()["populate_existing"] is True


@pytest.mark.parametrize(
    "method,suffix",
    (("GET", "/events"), ("POST", "/events"), ("POST", "/events/replaced/retract")),
)
def test_session_and_event_scope_are_404(http_harness, method, suffix):
    h, client = http_harness
    suffix = suffix.replace("replaced", h.rows["event_table"].id)
    before = h.snapshot()
    for session_id in (h.rows["other_session"].id, str(uuid4())):
        response = client.request(
            method,
            _url(h, suffix, game_session=session_id),
            headers=h.headers("dm"),
            json={"kind": "action", "audience": "table", "body": {}},
        )
        assert response.status_code == 404
    # An event from this campaign but another session must also be hidden.
    second = GameSession(campaign_id=h.rows["campaign"].id, status="ended")
    h.session.add(second)
    h.session.commit()
    if suffix.endswith("/retract"):
        assert (
            client.post(
                _url(h, suffix, game_session=second.id), headers=h.headers("dm")
            ).status_code
            == 404
        )
        assert (
            client.post(
                _url(h, f"/events/{uuid4()}/retract"), headers=h.headers("dm")
            ).status_code
            == 404
        )
    h.session.delete(second)
    h.session.commit()
    assert h.snapshot() == before


def test_literal_kinds_and_all_kinds_append(session, game_session):
    kinds = (
        "narration",
        "action",
        "roll",
        "reveal",
        "handout",
        "turn",
        "system",
        "utterance",
    )
    assert get_args(EventKind) == kinds
    for seq, kind in enumerate(kinds, start=1):
        row = _append(session, game_session, kind=kind, body={"kind": kind})
        assert row.seq == seq
        assert row.kind == kind
        assert row.body == {"kind": kind}
        assert row.campaign_id == game_session.campaign_id
        assert row.session_id == game_session.id
        assert row.audience == "table"
        assert row.audience_pc_ids == []
        assert row.author_member_id is None
        assert row.retracted_at is None
        assert isinstance(row.created_at, datetime)
    assert isinstance(SessionEvent.__table__.c.seq.type, BigInteger)


@pytest.mark.parametrize("status", ["active", "paused"])
def test_active_and_paused_allow_append(session, game_session, status):
    game_session.status = status
    session.commit()
    assert _append(session, game_session).seq == 1


def test_ended_refused(session, game_session):
    game_session.status = "ended"
    session.commit()
    with pytest.raises(SessionEndedError, match="ended"):
        _append(session, game_session)
    assert session.exec(select(SessionEvent)).all() == []


def test_stale_ended_row_refused(engine, session, game_session):
    session_id = game_session.id
    with Session(engine) as other:
        row = other.get(GameSession, session_id)
        row.status = "ended"
        other.commit()
    assert game_session.status == "active"
    with pytest.raises(SessionEndedError):
        _append(session, game_session)


def test_unknown_kind_refused(session, game_session):
    with pytest.raises(InvalidEventKindError, match="Unknown event kind"):
        _append(session, game_session, kind="unknown")
    assert session.exec(select(SessionEvent)).all() == []


def test_nonexistent_session_refused(session, game_session):
    missing = GameSession(campaign_id=game_session.campaign_id)
    with pytest.raises(ValueError, match="does not exist"):
        _append(session, missing)


def test_pcs_campaign_scope_and_sorted_ids(session, game_session):
    other = Campaign(name="Other")
    session.add(other)
    session.flush()
    local = PlayerCharacter(
        campaign_id=game_session.campaign_id, character_name="Local"
    )
    second = PlayerCharacter(
        campaign_id=game_session.campaign_id, character_name="Second"
    )
    foreign = PlayerCharacter(campaign_id=other.id, character_name="Foreign")
    session.add_all([local, second, foreign])
    session.flush()
    for ids in [(foreign.id,), (local.id, foreign.id), (str(uuid4()),)]:
        with pytest.raises(InvalidEventAudienceError, match="belong to this campaign"):
            _append(session, game_session, audience=Audience("pcs", frozenset(ids)))
    assert session.exec(select(SessionEvent)).all() == []
    row = _append(
        session,
        game_session,
        audience=Audience("pcs", frozenset([second.id, local.id])),
    )
    assert row.audience == "pcs"
    assert row.audience_pc_ids == sorted([local.id, second.id])
    assert row.seq == 1


def test_conflicting_author_provenance_refused(session, game_session):
    with pytest.raises(
        InvalidEventAudienceError, match="Conflicting author provenance"
    ):
        _append(
            session,
            game_session,
            audience=Audience("dm", author_member_id=str(uuid4())),
        )


def test_sequence_is_per_session_and_rollback_has_no_gap(session, game_session):
    assert _append(session, game_session).seq == 1
    session.commit()
    assert _append(session, game_session).seq == 2
    session.rollback()
    assert _append(session, game_session).seq == 2
    other = GameSession(campaign_id=game_session.campaign_id, status="ended")
    session.add(other)
    session.flush()
    other.status = "active"
    assert _append(session, other).seq == 1
    session.commit()
    assert [
        row.seq
        for row in session.exec(
            select(SessionEvent)
            .where(SessionEvent.session_id == game_session.id)
            .order_by(SessionEvent.seq)
        )
    ] == [1, 2]


def test_append_never_commits(session, game_session, monkeypatch):
    def no_commit():
        pytest.fail("append_event must leave transaction ownership to the caller")

    monkeypatch.setattr(session, "commit", no_commit)
    row_id = _append(session, game_session).id
    session.rollback()
    assert session.get(SessionEvent, row_id) is None


def test_lock_is_for_update_and_refreshes_identity_map(
    session, game_session, monkeypatch
):
    original_exec = session.exec
    statements = []

    def capture(statement, *args, **kwargs):
        statements.append(statement)
        return original_exec(statement, *args, **kwargs)

    monkeypatch.setattr(session, "exec", capture)
    _append(session, game_session)
    assert str(statements[0].compile(dialect=postgresql.dialect())).endswith(
        "FOR UPDATE"
    )
    assert statements[0].get_execution_options()["populate_existing"] is True


def test_integrity_error_not_hidden_or_retried(session, game_session, monkeypatch):
    calls = []
    failure = IntegrityError("insert", {}, Exception("missing serialization"))
    original_flush = session.flush

    def broken_flush(*args, **kwargs):
        if not any(isinstance(row, SessionEvent) for row in session.new):
            return original_flush(*args, **kwargs)
        calls.append(1)
        raise failure

    monkeypatch.setattr(session, "flush", broken_flush)
    with pytest.raises(IntegrityError) as caught:
        _append(session, game_session)
    assert caught.value is failure
    assert calls == [1]


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, False),
        ("false", False),
        ("true", True),
        ("junk", False),
        ("TRUE", False),
        (" true ", False),
        ("1", False),
        ("", False),
    ],
)
def test_play_flag_exact_literal(monkeypatch, value, expected):
    monkeypatch.delenv("GRIMOIRE_PLAY_ENABLED", raising=False)
    if value is not None:
        monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", value)
    assert play_enabled() is expected


def test_play_flag_reads_at_call_time(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    assert play_enabled() is True
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "false")
    assert play_enabled() is False


def test_dependency_404_and_enabled(monkeypatch):
    monkeypatch.delenv("GRIMOIRE_PLAY_ENABLED", raising=False)
    with pytest.raises(HTTPException) as caught:
        require_play_enabled()
    assert caught.value.status_code == 404
    assert caught.value.detail == "Not found"
    app = FastAPI()

    @app.get("/play", dependencies=[Depends(require_play_enabled)])
    def play():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/play").status_code == 404
        monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
        assert require_play_enabled() is None
        assert client.get("/play").status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "unknown"},
        {"audience": "unknown"},
        {"seq": 0},
        {"seq": -1},
        {"audience": "pcs", "audience_pc_ids": []},
        {"audience": "table", "audience_pc_ids": ["pc"]},
        {"audience": "dm", "audience_pc_ids": ["pc"]},
        {"audience_pc_ids": {}},
        {"audience_pc_ids": None},
        {"body": null()},
    ],
)
def test_model_checks_reject_invalid_storage(session, game_session, changes):
    values = {
        "campaign_id": game_session.campaign_id,
        "session_id": game_session.id,
        "seq": 1,
        "kind": "action",
        "audience": "table",
    }
    values.update(changes)
    session.add(SessionEvent(**values))
    with pytest.raises(IntegrityError):
        session.flush()


def test_model_default_values_are_independent_and_unique_seq(session, game_session):
    values = {
        "campaign_id": game_session.campaign_id,
        "session_id": game_session.id,
        "seq": 1,
        "kind": "action",
        "audience": "dm",
    }
    first = SessionEvent(**values)
    second = SessionEvent(**values)
    assert first.body == {}
    assert first.audience_pc_ids == []
    assert first.body is not second.body
    assert first.audience_pc_ids is not second.audience_pc_ids
    assert first.id != second.id
    session.add_all([first, second])
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize(
    "formula",
    ["0d20", "101d6", "d1001", "2d20adv", "4d6kh0", "abc", "d6" * 33],
)
def test_dice_rejects_invalid_and_unbounded_formulas(formula):
    from grimoire.dice import roll

    with pytest.raises(ValueError):
        roll(formula)


@pytest.mark.parametrize(
    "formula,kept,total",
    [
        ("4d6kh3+2", [6, 4, 3], 15),
        ("4d6kl2-1", [1, 3], 3),
        ("1d20adv+3", [6], 9),
        ("1d20dis", [1], 1),
        ("2d6+3", [1, 6], 10),
    ],
)
def test_dice_keeps_correct_values_and_applies_modifier(formula, kept, total):
    from grimoire.dice import roll

    class Fixed:
        def __init__(self):
            self.values = iter([1, 6, 3, 4])

        def randint(self, lower, upper):
            value = next(self.values)
            assert lower <= value <= upper
            return value

    result = roll(formula, Fixed())
    assert result["kept"] == kept
    assert result["total"] == total


@pytest.mark.parametrize("roller", ["player_a", "player_b"])
@pytest.mark.parametrize(
    "visibility,visible",
    [
        ("table", {"dm", "player_a", "player_b", "no_character"}),
        ("dm", {"dm", "player_a"}),
        ("self", {"dm", "player_a"}),
    ],
)
def test_player_roll_is_server_generated_and_audience_scoped(
    http_harness, visibility, visible, roller
):
    h, client = http_harness
    if visibility != "table":
        visible = {"dm", roller}
    response = client.post(
        _url(h, "/rolls"),
        headers=h.headers(roller),
        json={"formula": "1d20", "label": "Search for traps", "visibility": visibility},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["kind"] == "roll"
    assert 1 <= result["body"]["total"] <= 20
    assert result["body"]["total"] == sum(result["body"]["kept"])
    for viewer in ROLES:
        poll = client.get(_url(h, "/events"), headers=h.headers(viewer))
        if viewer in {"outsider", "other_campaign"}:
            assert poll.status_code in (403, 404)
        else:
            ids = {event["id"] for event in poll.json()}
            assert (result["id"] in ids) == (viewer in visible)


def test_roll_rejects_supplied_total_and_ended_session(http_harness):
    h, client = http_harness
    before = h.snapshot()
    response = client.post(
        _url(h, "/rolls"),
        headers=h.headers("player_a"),
        json={"formula": "1d20", "total": 20},
    )
    assert response.status_code == 422
    assert h.snapshot() == before
    client.patch(
        _url(h, "/status").removesuffix("/status"),
        headers=h.headers("dm"),
        json={"status": "ended"},
    )
    before = h.snapshot()
    response = client.post(
        _url(h, "/rolls"), headers=h.headers("player_a"), json={"formula": "1d20"}
    )
    assert response.status_code == 409
    assert h.snapshot() == before


def _roll(h, client, viewer="player_a", **changes):
    body = {"formula": "4d6kh3+2", "label": "  Initiative  "}
    body.update(changes)
    return client.post(_url(h, "/rolls"), headers=h.headers(viewer), json=body)


@pytest.mark.parametrize(
    "viewer,visibility,audience,member_key,visible_to",
    [
        ("player_a", "dm", "dm", "member_player_a", {"dm", "player_a"}),
        ("player_a", "self", "pcs", "member_player_a", {"dm", "player_a"}),
        (
            "player_a",
            "table",
            "table",
            "member_player_a",
            {"dm", "player_a", "player_b", "no_character"},
        ),
        (
            "player_a",
            None,
            "table",
            "member_player_a",
            {"dm", "player_a", "player_b", "no_character"},
        ),
        ("dm", None, "dm", "member_dm", {"dm"}),
        ("dm", "self", "dm", "member_dm", {"dm"}),
        (
            "no_character",
            "table",
            "table",
            "member_no_character",
            {"dm", "player_a", "player_b", "no_character"},
        ),
        (
            "no_character",
            None,
            "table",
            "member_no_character",
            {"dm", "player_a", "player_b", "no_character"},
        ),
    ],
)
def test_roll_audience_and_seeded_body(
    http_harness, viewer, visibility, audience, member_key, visible_to
):
    h, client = http_harness
    client.app.dependency_overrides[get_dice_rng] = lambda: random.Random(42)
    response = _roll(h, client, viewer, visibility=visibility)
    assert response.status_code == 200, response.text
    event_id = response.json()["id"]
    row = h.session.get(SessionEvent, event_id)
    assert row.kind == "roll"
    assert row.seq == 9
    assert row.author_member_id == h.rows[member_key].id
    assert row.audience == audience
    assert row.audience_pc_ids == (
        [h.rows["character_a"].id] if audience == "pcs" else []
    )
    expected = {
        "formula": "4d6kh3+2",
        "total": 15,
        "rolls": [6, 1, 1, 6],
        "kept": [6, 6, 1],
        "modifier": 2,
        "label": "Initiative",
        "visibility": visibility or ("dm" if viewer == "dm" else "table"),
    }
    assert row.body == expected
    assert response.json()["body"] == expected
    assert response.json()["author_member_id"] == h.rows[member_key].id
    for poller in ("dm", "player_a", "player_b", "no_character"):
        response = client.get(_url(h, "/events"), headers=h.headers(poller))
        assert response.status_code == 200
        h.assert_no_leak(response, poller)
        assert (event_id in {event["id"] for event in response.json()}) == (
            poller in visible_to
        )


@pytest.mark.parametrize("visibility", ("dm", "self"))
def test_characterless_restricted_roll_is_refused_before_formula(
    http_harness, visibility
):
    h, client = http_harness
    before = h.snapshot()
    response = _roll(
        h, client, "no_character", visibility=visibility, formula="invalid"
    )
    assert response.status_code == 422
    assert (
        response.json()["detail"] == "players without a character may roll only table"
    )
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "formula,reason",
    [
        ("notdice", "expected NdM"),
        ("101d6", "dice count"),
        ("1d6" + " " * 62, "at most 64"),
        ("1d6+1001", "modifier magnitude"),
    ],
)
def test_invalid_roll_formula_is_400_without_writes(http_harness, formula, reason):
    h, client = http_harness
    before = h.snapshot()
    response = _roll(h, client, formula=formula)
    assert response.status_code == 400
    assert response.json()["detail"].startswith("invalid formula: ")
    assert reason in response.json()["detail"]
    assert h.snapshot() == before


@pytest.mark.parametrize("viewer", ("outsider", "other_campaign"))
@pytest.mark.parametrize(
    "body",
    [
        {"formula": "notdice"},
        {},
        {"formula": None},
        {"formula": 123},
        {"formula": "1d1", "label": "x" * 201},
    ],
)
def test_roll_nonmember_404_before_formula_validation(http_harness, viewer, body):
    h, client = http_harness
    before = h.snapshot()
    response = client.post(_url(h, "/rolls"), headers=h.headers(viewer), json=body)
    assert response.status_code == 404
    assert h.snapshot() == before


def test_roll_session_mismatch_404_before_formula_validation(http_harness):
    h, client = http_harness
    before = h.snapshot()
    response = client.post(
        _url(h, "/rolls", game_session=h.rows["other_session"].id),
        headers=h.headers("player_a"),
        json={"formula": "notdice"},
    )
    assert response.status_code == 404
    assert h.snapshot() == before


@pytest.mark.parametrize(
    "changes",
    [{"seed": 42}, {"total": 20}, {"visibility": "pcs"}, {"label": "x" * 201}],
)
def test_roll_request_validation_without_writes(http_harness, changes):
    h, client = http_harness
    before = h.snapshot()
    assert _roll(h, client, **changes).status_code == 422
    assert h.snapshot() == before


def test_roll_label_boundary_and_optional_label(http_harness):
    h, client = http_harness
    response = _roll(h, client, label="  " + "x" * 200 + "  ")
    assert response.status_code == 200
    assert response.json()["body"]["label"] == "x" * 200
    response = client.post(
        _url(h, "/rolls"), headers=h.headers("player_a"), json={"formula": "1d1"}
    )
    assert response.status_code == 200
    assert response.json()["body"]["label"] is None


def test_ended_session_cannot_accept_roll(http_harness):
    h, client = http_harness
    h.rows["campaign_session"].status = "ended"
    h.session.commit()
    before = h.snapshot()
    response = _roll(h, client)
    assert response.status_code == 409
    assert h.snapshot() == before


@pytest.mark.parametrize("viewer", ("dm", "player_a", "player_b", "no_character"))
def test_generic_event_route_refuses_forged_roll(http_harness, viewer):
    h, client = http_harness
    before = h.snapshot()
    response = _post(h, client, viewer, kind="roll", body={"total": 20})
    assert response.status_code == 403
    assert response.json() == {"detail": "rolls require the server-side roller"}
    assert h.snapshot() == before
