"""Journal HTTP scope, pagination and the shared #6607 canary fixture."""

import base64
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event
from sqlmodel import select

from grimoire.models import GameSession, SessionEvent
from grimoire.testing.leak_harness import MEMBERS, ROLES, sqlite_harness


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    with sqlite_harness(tmp_path / "journal.db") as h:
        h.prepare("play")
        # Make the seeded audience/body canaries contribute to Received.
        rows = h.session.exec(select(SessionEvent)).all()
        for row in rows:
            row.kind = "handout"
        h.session.commit()
        with TestClient(h.app()) as client:
            yield h, client


def url(h, session=False, campaign=None, session_id=None):
    prefix = f"/api/grimoire/campaigns/{campaign or h.rows['campaign'].id}"
    if session:
        prefix += f"/sessions/{session_id or h.rows['campaign_session'].id}"
    return prefix + "/journal"


@pytest.mark.parametrize("session_route", [False, True])
@pytest.mark.parametrize("view", ["mine", "party"])
def test_both_routes_pass_shared_leak_canaries(harness, session_route, view):
    h, client = harness
    for viewer in ROLES:
        response = client.get(
            url(h, session_route), params={"view": view}, headers=h.headers(viewer)
        )
        assert response.status_code == (200 if viewer in MEMBERS else 404), (
            response.text
        )
        h.assert_no_leak(response, viewer)
        if viewer in MEMBERS:
            data = (
                response.json()
                if session_route
                else response.json()["sessions"][0]["journal"]
            )
            assert len(data["received"]) == (
                1
                if view == "party" or viewer == "no_character"
                else 4
                if viewer == "dm"
                else 2
            )
            assert data["learned"] == []
            assert (
                all(item["audience"] == "table" for item in data["received"])
                if view == "party"
                else True
            )
            assert all(item["body"] is not None for item in data["received"])


@pytest.mark.parametrize("session_route", [False, True])
def test_play_off_is_404(harness, monkeypatch, session_route):
    h, client = harness
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "false")
    response = client.get(
        url(h, session_route),
        params={"view": "bogus", "limit": "abc"},
        headers=h.headers("dm"),
    )
    assert response.status_code == 404


@pytest.mark.parametrize("viewer", ["outsider", "other_campaign"])
@pytest.mark.parametrize("session_route", [False, True])
@pytest.mark.parametrize(
    "query", [{"view": "bogus"}, {"limit": 0}, {"limit": "abc"}, {"cursor": "bad"}]
)
def test_nonmembers_404_before_query_validation(harness, viewer, session_route, query):
    h, client = harness
    response = client.get(
        url(h, session_route), params=query, headers=h.headers(viewer)
    )
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("viewer", ROLES)
@pytest.mark.parametrize("session_id", ["bad", "not-a-uuid", str(uuid4())])
def test_session_scope_404_including_malformed_ids(harness, viewer, session_id):
    h, client = harness
    response = client.get(
        url(h, True, session_id=session_id),
        params={"view": "bogus"},
        headers=h.headers(viewer),
    )
    assert response.status_code == 404, response.text


def test_other_campaign_session_is_404(harness):
    h, client = harness
    response = client.get(
        url(h, True, session_id=h.rows["other_session"].id), headers=h.headers("dm")
    )
    assert response.status_code == 404


@pytest.mark.parametrize("session_route", [False, True])
def test_reveal_snapshot_visibility_and_silent_revocation_on_routes(
    harness, session_route
):
    h, client = harness
    entity = h.rows["partial"]
    name_only = h.rows["name_only"]
    pc = h.rows["character_a"].id
    reveal = SessionEvent(
        campaign_id=h.rows["campaign"].id,
        session_id=h.rows["campaign_session"].id,
        seq=9,
        kind="reveal",
        audience="pcs",
        audience_pc_ids=[pc],
        author_member_id=h.rows["member_dm"].id,
        body={
            "reveals": [
                {
                    "entity_id": entity.id,
                    "name": entity.name,
                    "entity_type": entity.entity_type,
                    "grant_scope": "partial",
                    "entity": {
                        "id": entity.id,
                        "name": entity.name,
                        "entity_type": entity.entity_type,
                        "revealed_details": h.rows["grant_partial"].revealed_details,
                    },
                }
            ]
        },
    )
    narration = SessionEvent(
        campaign_id=h.rows["campaign"].id,
        session_id=h.rows["campaign_session"].id,
        seq=10,
        kind="narration",
        audience="table",
        audience_pc_ids=[],
        author_member_id=h.rows["member_dm"].id,
        body={"entity_ids": [name_only.id, h.rows["private"].id, "invalid", {}]},
    )
    h.session.add_all([reveal, narration])
    h.session.commit()

    def read(viewer):
        response = client.get(url(h, session_route), headers=h.headers(viewer))
        assert response.status_code == 200
        h.assert_no_leak(response, viewer)
        return (
            response.json()
            if session_route
            else response.json()["sessions"][0]["journal"]
        )

    learned = read("player_a")
    assert (
        learned["learned"][0]["entity"]["revealed_details"]
        == h.rows["grant_partial"].revealed_details
    )
    assert {row["id"] for row in learned["people_and_places"]} == {
        entity.id,
        name_only.id,
    }
    for viewer in ("player_b", "no_character"):
        assert read(viewer)["learned"] == []
        assert read(viewer)["people_and_places"] == []
    h.session.delete(h.rows["grant_partial"])
    h.session.commit()
    revoked = read("player_a")
    assert revoked["learned"] == []
    assert {row["id"] for row in revoked["people_and_places"]} == {name_only.id}


@pytest.mark.parametrize(
    "query",
    [
        {"view": "bogus"},
        {"limit": 0},
        {"limit": 51},
        {"limit": "abc"},
        {"cursor": "bad"},
        {"cursor": "x" * 257},
    ],
)
def test_member_invalid_campaign_queries_are_422(harness, query):
    h, client = harness
    response = client.get(url(h), params=query, headers=h.headers("dm"))
    assert response.status_code == 422


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        [None, 1],
        ["2026-10-03T00:00:00", str(uuid4())],
        ["2026-10-03T00:00:00Z", "bad"],
    ],
)
def test_malformed_cursor_payloads_are_422(harness, value):
    h, client = harness
    cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    response = client.get(url(h), params={"cursor": cursor}, headers=h.headers("dm"))
    assert response.status_code == 422


def test_campaign_pagination_ties_and_one_filtered_event_query(harness):
    h, client = harness
    timestamp = datetime(2026, 10, 3, tzinfo=timezone.utc)
    h.rows["campaign_session"].started_at = timestamp
    extra = [
        GameSession(
            campaign_id=h.rows["campaign"].id,
            status="ended",
            started_at=timestamp if index < 3 else timestamp - timedelta(days=index),
        )
        for index in range(6)
    ]
    h.session.add_all(extra)
    h.session.commit()
    expected = [
        row.id
        for row in sorted(
            [h.rows["campaign_session"], *extra],
            key=lambda row: (row.started_at, row.id),
            reverse=True,
        )
    ]
    queries = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.lstrip().startswith("SELECT")
            and "FROM session_event" in statement
        ):
            queries.append(statement)

    sql_event.listen(h.session.get_bind(), "before_cursor_execute", record)
    seen, cursor = [], None
    try:
        while True:
            params = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            queries.clear()
            response = client.get(url(h), params=params, headers=h.headers("player_a"))
            assert response.status_code == 200, response.text
            h.assert_no_leak(response, "player_a")
            page = response.json()
            assert len(page["sessions"]) <= 2
            assert len(queries) == 1, queries
            assert "campaign_id" in queries[0] and "audience" in queries[0]
            seen.extend(row["session_id"] for row in page["sessions"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == expected
    finally:
        sql_event.remove(h.session.get_bind(), "before_cursor_execute", record)
