"""Voice-map ACLs, validation, persistence, and narration projection."""

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import select

from grimoire.models import CampaignVoice, SessionEvent
from grimoire.testing.leak_harness import sqlite_harness
from grimoire.voices import speaker_ref, validate_speaker_key


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    with sqlite_harness(tmp_path / "voices.db") as h:
        h.prepare("play")
        with TestClient(h.app()) as client:
            yield client, h


def path(h, key=None):
    base = f"/api/grimoire/campaigns/{h.rows['campaign'].id}/voices"
    return base if key is None else f"{base}/{key}"


def events_path(h):
    return (
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}"
        f"/sessions/{h.rows['campaign_session'].id}/events"
    )


def test_dm_upsert_creates_then_updates_same_row(setup):
    client, h = setup
    key = "The Captain"
    created = client.put(path(h, key), headers=h.headers("dm"), json={})
    assert created.status_code == 200, created.text
    assert created.json()["voice_hint"] == {"lang": None, "names": []}
    assert created.json()["rate"] == created.json()["pitch"] == 1
    row = h.session.exec(
        select(CampaignVoice).where(CampaignVoice.speaker_key == key)
    ).one()
    row_id = row.id
    updated = client.put(
        path(h, key),
        headers=h.headers("dm"),
        json={
            "voice_hint": {"lang": "en-GB", "names": ["English"]},
            "rate": 1.5,
            "pitch": 0.5,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["rate"] == 1.5
    assert updated.json()["pitch"] == 0.5
    assert updated.json()["voice_hint"] == {"lang": "en-GB", "names": ["English"]}
    assert updated.json()["updated_at"] >= created.json()["updated_at"]
    h.session.expire_all()
    rows = h.session.exec(
        select(CampaignVoice).where(CampaignVoice.speaker_key == key)
    ).all()
    assert len(rows) == 1
    assert rows[0].id == row_id
    assert rows[0].rate == 1.5
    assert rows[0].pitch == 0.5
    assert rows[0].voice_hint == updated.json()["voice_hint"]


def test_dm_delete_returns_204_and_removes_row(setup):
    client, h = setup
    response = client.delete(path(h, "narrator"), headers=h.headers("dm"))
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert (
        h.session.exec(
            select(CampaignVoice).where(CampaignVoice.speaker_key == "narrator")
        ).first()
        is None
    )
    assert (
        client.delete(path(h, "narrator"), headers=h.headers("dm")).status_code == 404
    )


@pytest.mark.parametrize(
    "body",
    [
        {"rate": 0.49},
        {"rate": 2.01},
        {"pitch": -0.01},
        {"pitch": 2.01},
        {"voice_hint": {"names": ["voice"] * 9}},
        {"voice_hint": {"names": ["x" * 65]}},
        {"voice_hint": {"names": [""]}},
        {"voice_hint": {"names": [123]}},
        {"voice_hint": {"lang": "x" * 36}},
        {"voice_hint": {"secret": "unexpected"}},
        {"unexpected": True},
    ],
)
def test_invalid_presets_are_rejected_without_mutation(setup, body):
    client, h = setup
    before = h.snapshot()
    response = client.put(path(h, "narrator"), headers=h.headers("dm"), json=body)
    assert response.status_code == 422, response.text
    assert h.snapshot() == before


@pytest.mark.parametrize("key", ["ref:opaque", "x" * 65, "bad!label"])
def test_invalid_speaker_keys_are_rejected(setup, key):
    client, h = setup
    before = h.snapshot()
    assert client.put(path(h, key), headers=h.headers("dm"), json={}).status_code == 422
    assert client.delete(path(h, key), headers=h.headers("dm")).status_code == 422
    assert h.snapshot() == before


@pytest.mark.parametrize("key", [".", "..", "..."])
def test_dot_only_labels_are_rejected(key):
    # HTTP clients normalize /voices/.. away before routing, so check directly.
    with pytest.raises(HTTPException) as error:
        validate_speaker_key(None, "campaign", key)
    assert error.value.status_code == 422


@pytest.mark.parametrize("key_name", ["foreign", "class"])
def test_foreign_campaign_and_non_npc_entity_keys_are_rejected(setup, key_name):
    client, h = setup
    key = h.rows[key_name].id
    before = h.snapshot()
    response = client.put(path(h, key), headers=h.headers("dm"), json={})
    assert response.status_code == 404, response.text
    assert h.snapshot() == before


def test_unknown_uuid_is_rejected(setup):
    client, h = setup
    assert (
        client.put(path(h, str(uuid4())), headers=h.headers("dm"), json={}).status_code
        == 404
    )


@pytest.mark.parametrize("rate,pitch", [(0.5, 0), (2, 2)])
def test_preset_boundaries_are_accepted(setup, rate, pitch):
    client, h = setup
    response = client.put(
        path(h, "narrator"),
        headers=h.headers("dm"),
        json={
            "rate": rate,
            "pitch": pitch,
            "voice_hint": {"lang": "x" * 35, "names": ["x" * 64] * 8},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["rate"] == rate
    assert response.json()["pitch"] == pitch


def test_entity_voice_upsert_and_narration_share_canonical_key(setup):
    client, h = setup
    # Use a granted NPC without a preset to exercise entity-key creation.
    key = h.rows["a_only"].id
    for spelling, rate in ((key, 1.2), (key.lower(), 1.4)):
        response = client.put(
            path(h, spelling), headers=h.headers("dm"), json={"rate": rate}
        )
        assert response.status_code == 200, response.text
        assert response.json()["speaker_key"] == str(UUID(key))
    rows = h.session.exec(
        select(CampaignVoice).where(CampaignVoice.speaker_key == str(UUID(key)))
    ).all()
    assert len(rows) == 1 and rows[0].rate == 1.4
    created = client.post(
        events_path(h),
        headers=h.headers("dm"),
        json={"kind": "narration", "audience": "table", "body": {"speaker_key": key}},
    )
    assert created.status_code == 200, created.text
    voices = client.get(path(h), headers=h.headers("player_a"))
    events = client.get(events_path(h), headers=h.headers("player_a"))
    event = next(row for row in events.json() if row["id"] == created.json()["id"])
    assert event["body"]["speaker_key"] in {row["speaker_key"] for row in voices.json()}
    assert event["body"]["speaker_key"] == speaker_ref(h.rows["campaign"].id, key)


def test_existing_entity_preset_handles_uuid_case_without_duplicates(setup):
    client, h = setup
    key = h.rows["private"].id
    original = h.session.exec(
        select(CampaignVoice).where(CampaignVoice.speaker_key == key)
    ).one()
    original_id = original.id
    response = client.put(
        path(h, key.lower()), headers=h.headers("dm"), json={"rate": 1.5}
    )
    assert response.status_code == 200, response.text
    h.session.expire_all()
    voices = h.session.exec(select(CampaignVoice)).all()
    assert len(voices) == 2
    updated = next(row for row in voices if row.id == original_id)
    assert updated.speaker_key == str(UUID(key))
    assert updated.rate == 1.5
    response = client.delete(path(h, key), headers=h.headers("dm"))
    assert response.status_code == 204, response.text
    assert h.session.get(CampaignVoice, original_id) is None


def test_delete_entity_preset_finds_original_uuid_spelling(setup):
    client, h = setup
    key = h.rows["private"].id
    original_id = h.session.exec(
        select(CampaignVoice.id).where(CampaignVoice.speaker_key == key)
    ).one()
    response = client.delete(path(h, key.lower()), headers=h.headers("dm"))
    assert response.status_code == 204, response.text
    assert h.session.get(CampaignVoice, original_id) is None


def test_retracted_narration_does_not_expose_speaker_key(setup):
    client, h = setup
    event_id = h.rows["event_table"].id
    retracted = client.post(
        f"{events_path(h)}/{event_id}/retract", headers=h.headers("dm")
    )
    assert retracted.status_code == 200, retracted.text
    response = client.get(events_path(h), headers=h.headers("player_a"))
    assert response.status_code == 200
    h.assert_no_leak(response, "player_a")
    event = next(row for row in response.json() if row["id"] == event_id)
    assert event["body"] is None


@pytest.mark.parametrize("viewer", ["dm", "player_a", "player_b", "no_character"])
def test_members_can_get_voices_with_viewer_specific_keys(setup, viewer):
    client, h = setup
    response = client.get(path(h), headers=h.headers(viewer))
    assert response.status_code == 200, response.text
    h.assert_no_leak(response, viewer)
    keys = {row["speaker_key"] for row in response.json()}
    raw_key = h.rows["private"].id
    assert "narrator" in keys
    assert (
        raw_key if viewer == "dm" else speaker_ref(h.rows["campaign"].id, raw_key)
    ) in keys
    if viewer != "dm":
        assert raw_key.casefold() not in response.text.casefold()


@pytest.mark.parametrize("viewer", ["player_a", "player_b", "no_character"])
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_players_cannot_write_voice_map(setup, viewer, method):
    client, h = setup
    before = h.snapshot()
    response = client.request(
        method,
        path(h, "narrator"),
        headers=h.headers(viewer),
        json={} if method == "PUT" else None,
    )
    assert response.status_code == 403, response.text
    assert h.snapshot() == before


@pytest.mark.parametrize("viewer", ["outsider", "other_campaign"])
def test_nonmembers_cannot_read_voices(setup, viewer):
    client, h = setup
    response = client.get(path(h), headers=h.headers(viewer))
    assert response.status_code == 404
    h.assert_no_leak(response, viewer)


@pytest.mark.parametrize("viewer", ["player_a", "player_b", "no_character", "dm"])
def test_narration_key_matches_voice_map_and_never_mutates_storage(setup, viewer):
    client, h = setup
    before = h.snapshot()
    voices = client.get(path(h), headers=h.headers(viewer))
    events = client.get(events_path(h), headers=h.headers(viewer))
    assert voices.status_code == events.status_code == 200
    h.assert_no_leak(events, viewer)
    event = next(row for row in events.json() if row["id"] == h.rows["event_table"].id)
    key = event["body"]["speaker_key"]
    assert key in {row["speaker_key"] for row in voices.json()}
    raw_key = h.rows["private"].id
    assert key == (
        raw_key if viewer == "dm" else speaker_ref(h.rows["campaign"].id, raw_key)
    )
    assert h.snapshot() == before
    assert h.rows["event_table"].body["speaker_key"] == raw_key


@pytest.mark.parametrize(
    "key", ["narrator", "Unmapped voice", "Old Captain's-voice_1.", "private", None]
)
def test_valid_narration_keys_and_absent_key_are_accepted(setup, key):
    client, h = setup
    if key == "private":
        key = h.rows["private"].id
    body = {"text": "The captain speaks"}
    if key is not None:
        body["speaker_key"] = key
    response = client.post(
        events_path(h),
        headers=h.headers("dm"),
        json={"kind": "narration", "audience": "table", "body": body},
    )
    assert response.status_code == 200, response.text
    if key is not None:
        expected = str(UUID(key)) if key == h.rows["private"].id else key
        assert response.json()["body"]["speaker_key"] == expected
    else:
        assert "speaker_key" not in response.json()["body"]
    stored = h.session.get(SessionEvent, response.json()["id"])
    assert stored.body == response.json()["body"]


@pytest.mark.parametrize(
    "key", ["ref:opaque", "bad!label", "x" * 65, "foreign", None, 3, []]
)
def test_invalid_narration_speaker_keys_are_rejected(setup, key):
    client, h = setup
    if key == "foreign":
        key = h.rows["foreign"].id
    before = h.snapshot()
    response = client.post(
        events_path(h),
        headers=h.headers("dm"),
        json={"kind": "narration", "audience": "table", "body": {"speaker_key": key}},
    )
    assert response.status_code == (404 if key == h.rows["foreign"].id else 422), (
        response.text
    )
    assert h.snapshot() == before


def test_speaker_refs_are_canonical_and_campaign_scoped():
    campaign, other, npc = str(uuid4()), str(uuid4()), str(uuid4())
    ref = speaker_ref(campaign, npc)
    assert ref.startswith("ref:") and len(ref) == 24
    assert speaker_ref(campaign.upper(), npc.upper()) == ref
    assert speaker_ref(other, npc) != ref
    assert speaker_ref(campaign, "narrator") == "narrator"
    assert speaker_ref(campaign, "Free label") == "Free label"


@pytest.mark.parametrize("session_route", [False, True])
@pytest.mark.parametrize("viewer", ["dm", "player_a"])
def test_journal_event_bodies_use_the_same_speaker_projection(
    setup, session_route, viewer
):
    client, h = setup
    row = h.rows["event_table"]
    row.kind = "handout"
    h.session.commit()
    prefix = f"/api/grimoire/campaigns/{h.rows['campaign'].id}"
    if session_route:
        prefix += f"/sessions/{h.rows['campaign_session'].id}"
    response = client.get(f"{prefix}/journal", headers=h.headers(viewer))
    assert response.status_code == 200, response.text
    h.assert_no_leak(response, viewer)
    data = (
        response.json() if session_route else response.json()["sessions"][0]["journal"]
    )
    received = next(item for item in data["received"] if item["id"] == row.id)
    key = h.rows["private"].id
    assert received["body"]["speaker_key"] == (
        key if viewer == "dm" else speaker_ref(h.rows["campaign"].id, key)
    )
    assert row.body["speaker_key"] == key
