"""Unit tests for grimoire/router.py: campaign/character/grant/session CRUD.

In-memory SQLite + a minimal FastAPI app mounting only the grimoire router,
mirroring the schema-stripping + ``app.dependency_overrides[get_session]``
pattern in hikes/router_test.py.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from grimoire.models import Entity
from grimoire.router import router


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite can't span schemas, so strip the Postgres-only schema= overrides so
    # SQLModel.metadata.create_all() lands every table in the default schema.
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _create_campaign(client, name="Curse of Strahd", dm_name="Joe"):
    r = client.post("/api/grimoire/campaigns", json={"name": name, "dm_name": dm_name})
    assert r.status_code == 200
    return r.json()


def _create_character(client, campaign_id, character_name="Elowen"):
    r = client.post(
        f"/api/grimoire/campaigns/{campaign_id}/characters",
        json={"character_name": character_name},
    )
    assert r.status_code == 200
    return r.json()


def _seed_entity(session, entity_id, name="Strahd", entity_type="npc"):
    """Insert an Entity row directly (there is no create-entity route: entities
    come from the extraction pipeline). Grants FK entity_id, so create_grant now
    requires the target to exist."""
    session.add(Entity(id=entity_id, entity_type=entity_type, name=name))
    session.commit()


class TestCampaigns:
    def test_create_list_get(self, client):
        created = _create_campaign(client)
        assert created["name"] == "Curse of Strahd"
        assert created["dm_name"] == "Joe"

        listed = client.get("/api/grimoire/campaigns").json()
        assert len(listed) == 1
        assert listed[0]["id"] == created["id"]

        fetched = client.get(f"/api/grimoire/campaigns/{created['id']}")
        assert fetched.status_code == 200
        fetched_body = fetched.json()
        assert fetched_body["id"] == created["id"]

    def test_get_unknown_campaign_404(self, client):
        r = client.get("/api/grimoire/campaigns/does-not-exist")
        assert r.status_code == 404


class TestCharacters:
    def test_create_and_list(self, client):
        campaign = _create_campaign(client)
        character = _create_character(client, campaign["id"], "Elowen")
        assert character["campaign_id"] == campaign["id"]
        assert character["character_name"] == "Elowen"

        listed = client.get(
            f"/api/grimoire/campaigns/{campaign['id']}/characters"
        ).json()
        assert len(listed) == 1
        assert listed[0]["id"] == character["id"]

    def test_create_character_unknown_campaign_404(self, client):
        r = client.post(
            "/api/grimoire/campaigns/does-not-exist/characters",
            json={"character_name": "Elowen"},
        )
        assert r.status_code == 404


class TestGrants:
    def test_create_duplicate_and_pc_mismatch(self, client, session):
        campaign = _create_campaign(client)
        other_campaign = _create_campaign(client, name="Storm King's Thunder")
        character = _create_character(client, campaign["id"])
        entity_id = "entity-strahd"
        _seed_entity(session, entity_id)

        created = client.post(
            f"/api/grimoire/campaigns/{campaign['id']}/grants",
            json={
                "entity_id": entity_id,
                "player_character_id": character["id"],
                "grant_scope": "partial",
                "revealed_details": {"note": "a pale nobleman"},
            },
        )
        assert created.status_code == 200
        created_body = created.json()
        assert created_body["grant_scope"] == "partial"
        assert created_body["revealed_details"] == {"note": "a pale nobleman"}

        # Duplicate (entity_id, player_character_id) is a 409.
        duplicate = client.post(
            f"/api/grimoire/campaigns/{campaign['id']}/grants",
            json={
                "entity_id": entity_id,
                "player_character_id": character["id"],
                "grant_scope": "full",
            },
        )
        assert duplicate.status_code == 409

        # A PC from a different campaign is rejected.
        mismatched = client.post(
            f"/api/grimoire/campaigns/{other_campaign['id']}/grants",
            json={
                "entity_id": "entity-other",
                "player_character_id": character["id"],
                "grant_scope": "full",
            },
        )
        assert mismatched.status_code == 404

        listed = client.get(f"/api/grimoire/campaigns/{campaign['id']}/grants").json()
        assert len(listed) == 1

    def test_patch_partial_then_full(self, client, session):
        campaign = _create_campaign(client)
        character = _create_character(client, campaign["id"])
        _seed_entity(session, "entity-strahd")
        created = client.post(
            f"/api/grimoire/campaigns/{campaign['id']}/grants",
            json={
                "entity_id": "entity-strahd",
                "player_character_id": character["id"],
                "grant_scope": "name_only",
            },
        ).json()

        patched = client.patch(
            f"/api/grimoire/campaigns/{campaign['id']}/grants/{created['id']}",
            json={"grant_scope": "partial", "revealed_details": {"note": "a count"}},
        )
        assert patched.status_code == 200
        patched_body = patched.json()
        assert patched_body["grant_scope"] == "partial"
        assert patched_body["revealed_details"] == {"note": "a count"}

        promoted = client.patch(
            f"/api/grimoire/campaigns/{campaign['id']}/grants/{created['id']}",
            json={"grant_scope": "full"},
        )
        assert promoted.status_code == 200
        promoted_body = promoted.json()
        assert promoted_body["grant_scope"] == "full"
        # revealed_details untouched by an update that omits it.
        assert promoted_body["revealed_details"] == {"note": "a count"}

    def test_patch_unknown_grant_404(self, client):
        campaign = _create_campaign(client)
        r = client.patch(
            f"/api/grimoire/campaigns/{campaign['id']}/grants/does-not-exist",
            json={"grant_scope": "full"},
        )
        assert r.status_code == 404

    def test_delete_grant_then_gone(self, client, session):
        campaign = _create_campaign(client)
        character = _create_character(client, campaign["id"])
        _seed_entity(session, "entity-strahd")
        created = client.post(
            f"/api/grimoire/campaigns/{campaign['id']}/grants",
            json={
                "entity_id": "entity-strahd",
                "player_character_id": character["id"],
                "grant_scope": "full",
            },
        ).json()

        deleted = client.delete(
            f"/api/grimoire/campaigns/{campaign['id']}/grants/{created['id']}"
        )
        assert deleted.status_code == 204
        assert (
            client.get(f"/api/grimoire/campaigns/{campaign['id']}/grants").json() == []
        )

        # A second delete of the now-missing grant is a 404.
        again = client.delete(
            f"/api/grimoire/campaigns/{campaign['id']}/grants/{created['id']}"
        )
        assert again.status_code == 404

    def test_create_grant_unknown_entity_404(self, client):
        # entity_id is a FK; a missing entity must be a 404, not the 500 the
        # Postgres IntegrityError would otherwise produce (the SQLite fixture
        # does not enforce the FK, so the router's own guard is what is tested).
        campaign = _create_campaign(client)
        character = _create_character(client, campaign["id"])
        r = client.post(
            f"/api/grimoire/campaigns/{campaign['id']}/grants",
            json={
                "entity_id": "no-such-entity",
                "player_character_id": character["id"],
                "grant_scope": "full",
            },
        )
        assert r.status_code == 404
        assert r.json().get("detail") == "entity not found"


class TestSessions:
    def test_create_second_active_conflicts_then_succeeds_after_end(self, client):
        campaign = _create_campaign(client)

        first = client.post(f"/api/grimoire/campaigns/{campaign['id']}/sessions")
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["status"] == "active"
        assert first_body["ended_at"] is None

        second = client.post(f"/api/grimoire/campaigns/{campaign['id']}/sessions")
        assert second.status_code == 409

        session_id = first_body["id"]
        ended = client.patch(
            f"/api/grimoire/campaigns/{campaign['id']}/sessions/{session_id}",
            json={"status": "ended"},
        )
        assert ended.status_code == 200
        ended_body = ended.json()
        assert ended_body["status"] == "ended"
        assert ended_body["ended_at"] is not None

        third = client.post(f"/api/grimoire/campaigns/{campaign['id']}/sessions")
        assert third.status_code == 200

    def test_pause_then_resume(self, client):
        campaign = _create_campaign(client)
        created = client.post(
            f"/api/grimoire/campaigns/{campaign['id']}/sessions"
        ).json()

        paused = client.patch(
            f"/api/grimoire/campaigns/{campaign['id']}/sessions/{created['id']}",
            json={"status": "paused"},
        )
        assert paused.status_code == 200
        paused_body = paused.json()
        assert paused_body["status"] == "paused"
        assert paused_body["ended_at"] is None

        # A paused (non-ended) session still blocks creating a new one.
        blocked = client.post(f"/api/grimoire/campaigns/{campaign['id']}/sessions")
        assert blocked.status_code == 409

    def test_patch_unknown_session_404(self, client):
        campaign = _create_campaign(client)
        r = client.patch(
            f"/api/grimoire/campaigns/{campaign['id']}/sessions/does-not-exist",
            json={"status": "ended"},
        )
        assert r.status_code == 404
