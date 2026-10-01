"""Character seating uses campaign roles and leaves PC-owned data intact."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from grimoire.models import CampaignMember, PlayerCharacter
from grimoire.testing.leak_harness import sqlite_harness


@pytest.fixture
def harness(tmp_path):
    with sqlite_harness(tmp_path / "assignment.db") as h:
        with TestClient(h.app()) as client:
            yield h, client


def assign(h, client, body, *, caller="dm", member="member_no_character"):
    return client.put(
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}"
        f"/members/{h.rows[member].id}/character",
        headers=h.headers(caller),
        json=body,
    )


def create_self(h, client, *, caller="no_character", body=None):
    return client.post(
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}/characters/self",
        headers=h.headers(caller),
        json=body if body is not None else {"name": "  Elowen  "},
    )


def test_dm_creates_and_assigns_character(harness):
    h, client = harness
    response = assign(h, client, {"new": {"name": "  Elowen  "}})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["character_name"] == "Elowen"
    character = h.session.get(PlayerCharacter, result["player_character_id"])
    assert character is not None
    assert character.campaign_id == h.rows["campaign"].id
    assert character.character_name == "Elowen"
    h.session.expire_all()
    assert (
        h.session.get(CampaignMember, result["id"]).player_character_id == character.id
    )


def test_self_creates_once(harness):
    h, client = harness
    response = create_self(h, client)
    assert response.status_code == 200, response.text
    character = h.session.get(PlayerCharacter, response.json()["id"])
    assert character.campaign_id == h.rows["campaign"].id
    assert character.character_name == "Elowen"
    h.session.expire_all()
    assert h.rows["member_no_character"].player_character_id == character.id
    before = h.snapshot()
    again = create_self(h, client)
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "player already has a character"
    assert h.snapshot() == before
    assert len(h.session.exec(select(PlayerCharacter)).all()) == 4
