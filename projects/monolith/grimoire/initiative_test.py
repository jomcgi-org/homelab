"""Initiative order: DM-only writes, player projection, turn events."""

import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from grimoire.initiative import InitiativeSetRequest
from grimoire.models import SessionEvent, SessionInitiative
from grimoire.testing.leak_harness import sqlite_harness

SECRET_LABEL = "Vampire Spawn Zx9"
SECRET_INITIATIVE = 7351


@pytest.fixture
def http_harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    with sqlite_harness(tmp_path / "initiative.db") as h:
        h.prepare("play")
        # The shared fixture seeds an order for the leak matrix; start clean.
        h.session.delete(h.rows["initiative"])
        h.session.commit()
        with TestClient(h.app()) as client:
            yield h, client


def _url(h, suffix="", *, campaign=None):
    return (
        f"/api/grimoire/campaigns/{campaign or h.rows['campaign'].id}"
        f"/sessions/{h.rows['campaign_session'].id}/initiative{suffix}"
    )


def _entries(h, *, hidden_initiative=SECRET_INITIATIVE):
    return [
        {
            "label": "Aria",
            "player_character_id": h.rows["character_a"].id,
            "initiative": 15,
        },
        {
            "label": SECRET_LABEL,
            "initiative": hidden_initiative,
            "hidden": True,
        },
        {"label": "Goblin", "initiative": 10},
    ]


def _put(h, client, viewer="dm", *, entries=None, **changes):
    body = {"entries": _entries(h) if entries is None else entries, **changes}
    return client.put(_url(h), headers=h.headers(viewer), json=body)


def _advance(h, client, direction, viewer="dm"):
    return client.post(
        _url(h, "/advance"), headers=h.headers(viewer), json={"direction": direction}
    )


def _delete(h, client, viewer="dm"):
    return client.delete(_url(h), headers=h.headers(viewer))


def _row(h):
    h.session.expire_all()
    return h.session.exec(select(SessionInitiative)).one_or_none()


def _turn_events(h):
    h.session.expire_all()
    return h.session.exec(
        select(SessionEvent)
        .where(SessionEvent.kind == "turn")
        .order_by(SessionEvent.seq)
    ).all()


def _ints(value):
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _ints(item)
    elif isinstance(value, list):
        for item in value:
            yield from _ints(item)


def _assert_hidden(payload):
    assert SECRET_LABEL.casefold() not in json.dumps(payload).casefold()
    assert SECRET_INITIATIVE not in set(_ints(payload))


def _assert_no_secret_anywhere(h, client):
    for viewer in ("player_a", "player_b", "no_character"):
        response = client.get(_url(h), headers=h.headers(viewer))
        assert response.status_code == 200, response.text
        _assert_hidden(response.json())
    for viewer in ("dm", "player_a"):
        events = client.get(
            f"/api/grimoire/campaigns/{h.rows['campaign'].id}/sessions/"
            f"{h.rows['campaign_session'].id}/events",
            headers=h.headers(viewer),
        )
        assert events.status_code == 200, events.text
        _assert_hidden(events.json())
    for event in _turn_events(h):
        _assert_hidden(event.body)


@pytest.mark.parametrize("mode", ["mask", "omit"])
def test_hidden_npc_never_reaches_players_or_turn_events(http_harness, mode):
    h, client = http_harness
    assert _put(h, client, hidden_display=mode, active_index=1).status_code == 200
    _assert_no_secret_anywhere(h, client)
    for step in (
        lambda: _advance(h, client, "next"),
        lambda: _advance(h, client, "next"),
        lambda: _advance(h, client, "previous"),
        lambda: _delete(h, client),
    ):
        assert step().status_code == 200
        _assert_no_secret_anywhere(h, client)

    events = _turn_events(h)
    assert [event.body["action"] for event in events] == [
        "set",
        "next",
        "next",
        "previous",
        "end",
    ]
    assert all(event.audience == "table" for event in events)
    assert events[-1].body == {
        "round": 1,
        "active_index": None,
        "entries": [],
        "action": "end",
    }
    # The projection is real, not an empty shell: visible entries survive.
    assert [e["label"] for e in events[0].body["entries"]][0] == "Aria"
    if mode == "mask":
        assert [e["label"] for e in events[0].body["entries"]] == [
            "Aria",
            "???",
            "Goblin",
        ]
        assert events[0].body["entries"][1] == {
            "label": "???",
            "player_character_id": None,
            "initiative": None,
            "hidden": True,
        }
    else:
        assert [e["label"] for e in events[0].body["entries"]] == ["Aria", "Goblin"]


def test_dm_get_and_mutation_responses_show_real_label(http_harness):
    h, client = http_harness
    assert _put(h, client, hidden_display="omit").status_code == 200
    for response in (
        client.get(_url(h), headers=h.headers("dm")),
        _advance(h, client, "next"),
    ):
        body = response.json()
        assert [e["label"] for e in body["entries"]] == ["Aria", SECRET_LABEL, "Goblin"]
        assert SECRET_INITIATIVE in set(_ints(body))
        assert body["hidden_display"] == "omit"
    # ...while a player on the same order sees neither.
    _assert_hidden(client.get(_url(h), headers=h.headers("player_a")).json())


def test_get_without_row_is_an_empty_order(http_harness):
    h, client = http_harness
    for viewer in ("dm", "player_a"):
        response = client.get(_url(h), headers=h.headers(viewer))
        assert response.status_code == 200
        assert response.json()["entries"] == []
        assert response.json()["round"] == 1
        assert response.json()["active_index"] is None
    assert client.get(_url(h), headers=h.headers("outsider")).status_code == 404


@pytest.mark.parametrize(
    "viewer,status",
    [
        ("player_a", 403),
        ("player_b", 403),
        ("no_character", 403),
        ("outsider", 404),
        ("other_campaign", 404),
    ],
)
def test_only_the_dm_can_mutate(http_harness, viewer, status):
    h, client = http_harness
    assert _put(h, client).status_code == 200
    before = h.snapshot()
    events_before = len(_turn_events(h))
    for response in (
        _put(h, client, viewer, entries=[{"label": "Evil", "initiative": 1}]),
        _advance(h, client, "next", viewer),
        _delete(h, client, viewer),
    ):
        assert response.status_code == status, response.text
        assert h.snapshot() == before
    assert len(_turn_events(h)) == events_before
    assert _row(h) is not None


def test_round_wraps_and_previous_underflows(http_harness):
    h, client = http_harness
    assert _put(h, client, active_index=2).status_code == 200

    body = _advance(h, client, "next").json()
    assert (body["active_index"], body["round"]) == (0, 2)
    assert (_row(h).active_index, _row(h).round) == (0, 2)

    body = _advance(h, client, "next").json()
    assert (body["active_index"], body["round"]) == (1, 2)

    body = _advance(h, client, "previous").json()
    assert (body["active_index"], body["round"]) == (0, 2)

    body = _advance(h, client, "previous").json()
    assert (body["active_index"], body["round"]) == (2, 1)

    # Round 1, index 0 is the floor: previous changes nothing.
    assert _put(h, client, active_index=0).status_code == 200
    before = (_row(h).active_index, _row(h).round)
    body = _advance(h, client, "previous").json()
    assert (body["active_index"], body["round"]) == (0, 1) == before
    assert (_row(h).active_index, _row(h).round) == (0, 1)


def test_omit_mode_remaps_active_index(http_harness):
    h, client = http_harness

    def player_active(active_index, mode):
        assert (
            _put(h, client, hidden_display=mode, active_index=active_index).status_code
            == 200
        )
        view = client.get(_url(h), headers=h.headers("player_a")).json()
        event = _turn_events(h)[-1].body
        assert event["active_index"] == view["active_index"]
        return view["active_index"], len(view["entries"])

    assert player_active(0, "omit") == (0, 2)
    assert player_active(1, "omit") == (None, 2)
    assert player_active(2, "omit") == (1, 2)
    # Mask keeps positions, so nothing shifts and the hidden turn stays addressable.
    assert player_active(1, "mask") == (1, 3)
    assert player_active(2, "mask") == (2, 3)


def test_character_ids_reach_only_the_dm_and_the_owner(http_harness):
    h, client = http_harness
    entries = [
        *_entries(h),
        {
            "label": "Bram",
            "player_character_id": h.rows["character"].id,
            "initiative": 8,
        },
    ]
    assert _put(h, client, entries=entries).status_code == 200
    assert _advance(h, client, "next").status_code == 200
    aria = str(UUID(str(h.rows["character_a"].id)))
    bram = str(UUID(str(h.rows["character"].id)))

    def ids(viewer):
        body = client.get(_url(h), headers=h.headers(viewer)).json()
        return [e["player_character_id"] for e in body["entries"]]

    assert ids("dm") == [aria, None, None, bram]
    assert ids("player_a") == [aria, None, None, None]
    assert ids("player_b") == [None, None, None, bram]
    assert ids("no_character") == [None] * 4
    # Turn events are table-wide, so they carry no character ids at all.
    for event in _turn_events(h):
        assert all(e["player_character_id"] is None for e in event.body["entries"])
    for viewer, own, other in (("player_a", aria, bram), ("player_b", bram, aria)):
        events = client.get(
            f"/api/grimoire/campaigns/{h.rows['campaign'].id}/sessions/"
            f"{h.rows['campaign_session'].id}/events",
            headers=h.headers(viewer),
        )
        assert events.status_code == 200, events.text
        # A player's own PC id legitimately rides in `audience_pc_ids` narration
        # addressed to them, so only table-wide turn events must carry neither
        # id, while the other player's id must never appear anywhere.
        turn_text = json.dumps(
            [e for e in events.json() if e["kind"] == "turn"]
        ).lower()
        assert own not in turn_text and other not in turn_text
        assert other not in events.text.lower()


def test_invalid_orders_are_rejected(http_harness):
    h, client = http_harness
    before = h.snapshot()
    other_pc = {
        "label": "Spy",
        "player_character_id": h.rows["other_character"].id,
        "initiative": 3,
    }
    hidden_pc = {
        "label": "Aria",
        "player_character_id": h.rows["character_a"].id,
        "initiative": 3,
        "hidden": True,
    }
    for body in (
        {"entries": [other_pc]},
        {"entries": [hidden_pc]},
        {"entries": _entries(h), "active_index": 3},
        {"entries": [], "active_index": 1},
        {"entries": _entries(h), "round": 0},
        {"entries": _entries(h), "hidden_display": "show"},
        {"entries": [{"label": "   ", "initiative": 1}]},
        {"entries": [{"label": "x" * 81, "initiative": 1}]},
        {"entries": [{"label": f"n{i}", "initiative": i} for i in range(51)]},
    ):
        response = client.put(_url(h), headers=h.headers("dm"), json=body)
        assert response.status_code == 422, (body, response.text)
        assert h.snapshot() == before
    assert (
        client.post(
            _url(h, "/advance"), headers=h.headers("dm"), json={"direction": "left"}
        ).status_code
        == 422
    )
    assert _row(h) is None


def test_empty_order_cannot_advance_and_end_needs_a_row(http_harness):
    h, client = http_harness
    before = h.snapshot()
    assert _advance(h, client, "next").status_code == 409
    assert _delete(h, client).status_code == 404
    assert h.snapshot() == before
    assert _put(h, client, entries=[]).status_code == 200
    before = h.snapshot()
    assert _advance(h, client, "next").status_code == 409
    assert h.snapshot() == before


def test_ended_session_rejects_every_mutation(http_harness):
    h, client = http_harness
    assert _put(h, client).status_code == 200
    h.rows["campaign_session"].status = "ended"
    h.session.commit()
    before = h.snapshot()
    for response in (
        _put(h, client, entries=[{"label": "Late", "initiative": 1}]),
        _advance(h, client, "next"),
        _delete(h, client),
    ):
        assert response.status_code == 409, response.text
        assert h.snapshot() == before
    assert client.get(_url(h), headers=h.headers("dm")).status_code == 200


def test_put_replaces_the_order_and_appends_one_event_each(http_harness):
    h, client = http_harness
    assert _put(h, client).status_code == 200
    assert (
        _put(h, client, entries=[{"label": "Solo", "initiative": 4}]).status_code == 200
    )
    assert len(h.session.exec(select(SessionInitiative)).all()) == 1
    assert [e["label"] for e in _row(h).entries] == ["Solo"]
    assert len(_turn_events(h)) == 2


def test_request_model_normalises_labels():
    parsed = InitiativeSetRequest(entries=[{"label": "  Orc  ", "initiative": 2}])
    assert parsed.entries[0].label == "Orc"
