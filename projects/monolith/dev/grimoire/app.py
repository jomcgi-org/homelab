"""Loopback-only Grimoire playground with signed synthetic identities.

This entrypoint is launched explicitly by dev/grimoire/run.py. Production module
registration never imports it or registers its identity endpoints.
"""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import jwt
from auth.api import AuthError, auth_error_handler
from core.db import get_engine
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from grimoire.models import (
    AppUser,
    Campaign,
    CampaignMember,
    CharacterSheetVersion,
    Entity,
    EntityNpc,
    GameSession,
    PlayerCharacter,
)
from grimoire.router import router
from grimoire.sheets import CharacterSheetV1, derive_sheet
from sqlalchemy import text
from sqlmodel import Session, SQLModel, select


def build_app() -> FastAPI:
    if os.environ.get("GRIMOIRE_LOCAL_PLAYGROUND") != "true":
        raise RuntimeError("Use dev/grimoire/run.py to launch the playground")
    if (
        os.environ.get("DATABASE_URL")
        != "postgresql://grimoire_local@127.0.0.1:55477/postgres"
    ):
        raise RuntimeError("The playground requires its isolated local database")
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS grimoire"))
        # Ending a session queues content-free extraction jobs even when off.
        # Use the existing queue migration in this isolated rehearsal database;
        # no drainer runs here, and production still owns its migration rollout.
        if (
            connection.scalar(text("SELECT to_regclass('claude_agent.routine_jobs')"))
            is None
        ):
            migration = (
                Path(__file__).resolve().parents[2]
                / "chart/migrations/20260507120000_agent_tables.sql"
            )
            connection.execute(text(migration.read_text()))
    SQLModel.metadata.create_all(
        engine,
        tables=[t for t in SQLModel.metadata.tables.values() if t.schema == "grimoire"],
    )
    issuer = os.environ["GRIMOIRE_AUTH_ISSUER"]
    people = {"dm": "Rowan", "a": "Elowen", "b": "Bram"}
    with Session(engine) as session:
        campaign = session.exec(select(Campaign)).first()
        if campaign is None:
            users = [
                AppUser(
                    email=f"{role}@example.test",
                    issuer=issuer,
                    subject=role,
                    display_name=name,
                )
                for role, name in people.items()
            ]
            session.add_all(users)
            session.flush()
            campaign = Campaign(
                name="The Lantern at the Crossroads",
                dm_name="Rowan",
                owner_app_user_id=users[0].id,
            )
            session.add(campaign)
            session.flush()
            characters = [
                PlayerCharacter(
                    campaign_id=campaign.id,
                    character_name="Elowen",
                    class_name="Ranger",
                    level=1,
                ),
                PlayerCharacter(
                    campaign_id=campaign.id,
                    character_name="Bram",
                    class_name="Fighter",
                    level=1,
                ),
            ]
            session.add_all(characters)
            session.flush()
            session.add_all(
                [
                    Entity(entity_type="race", name="Human", source_type="homebrew"),
                    Entity(
                        entity_type="class",
                        name="Ranger",
                        source_type="homebrew",
                        detail={"hit_die": "d10", "saves": "strength, dexterity"},
                    ),
                    Entity(
                        entity_type="class",
                        name="Fighter",
                        source_type="homebrew",
                        detail={"hit_die": "d10", "saves": "strength, constitution"},
                    ),
                ]
            )
            session.flush()
            now = datetime.now(UTC)
            approved = []
            for character, user in zip(characters, users[1:]):
                sheet = CharacterSheetV1(
                    ancestry="Human",
                    class_name=character.class_name,
                    level=1,
                    ability_scores={
                        "strength": 14,
                        "dexterity": 14,
                        "constitution": 12,
                        "intelligence": 10,
                        "wisdom": 12,
                        "charisma": 10,
                    },
                )
                approved.append(
                    CharacterSheetVersion(
                        campaign_id=campaign.id,
                        player_character_id=character.id,
                        version=1,
                        status="approved",
                        sheet=sheet.model_dump(),
                        derived=derive_sheet(session, campaign.id, character.id, sheet),
                        created_by_email=user.email,
                        submitted_at=now,
                        decided_at=now,
                        decided_by_email=users[0].email,
                    )
                )
            session.add_all(approved)
            history = GameSession(campaign_id=campaign.id, status="ended", ended_at=now)
            session.add(history)
            session.flush()
            innkeeper = Entity(
                entity_type="npc",
                name="Mara, the innkeeper",
                source_type="homebrew",
                is_global=False,
                created_in_session=history.id,
            )
            session.add(innkeeper)
            session.flush()
            session.add(
                EntityNpc(
                    entity_id=innkeeper.id,
                    description="DM_ONLY_INNKEEPER_SECRET",
                    occupation="Keeps the Lantern Inn",
                )
            )
            batch_people = [
                Entity(
                    entity_type="npc",
                    name=name,
                    source_type="homebrew",
                    is_global=False,
                    created_in_session=history.id,
                )
                for name in ("Mapmaker Tessa", "Ferryman Orrin")
            ]
            session.add_all(batch_people)
            session.flush()
            session.add_all(
                [
                    EntityNpc(entity_id=person.id, description="DM_ONLY_BATCH_SECRET")
                    for person in batch_people
                ]
            )
            session.add_all(
                [
                    CampaignMember(
                        campaign_id=campaign.id, app_user_id=users[0].id, role="dm"
                    ),
                    CampaignMember(
                        campaign_id=campaign.id,
                        app_user_id=users[1].id,
                        role="player",
                        player_character_id=characters[0].id,
                    ),
                    CampaignMember(
                        campaign_id=campaign.id,
                        app_user_id=users[2].id,
                        role="player",
                        player_character_id=characters[1].id,
                    ),
                ]
            )
            session.commit()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="local-playground", alg="RS256", use="sig")
    app = FastAPI(title="Grimoire local playground")
    app.add_exception_handler(AuthError, auth_error_handler)
    app.include_router(router)

    @app.get("/__local/jwks")
    def jwks():
        return {"keys": [public]}

    @app.get("/__local", response_class=HTMLResponse)
    def home():
        return """<!doctype html><title>Grimoire local table</title>
        <h1>Choose a seat</h1><p>Open each seat in a separate browser profile.</p>
        <ul><li><a href='/__local/login/dm'>Rowan, DM</a></li>
        <li><a href='/__local/login/a'>Elowen, player</a></li>
        <li><a href='/__local/login/b'>Bram, player</a></li></ul>"""

    @app.get("/__local/login/{role}")
    def login(role: str):
        if role not in people:
            raise HTTPException(404, "Unknown seat")
        token = jwt.encode(
            {
                "iss": issuer,
                "aud": "grimoire-local",
                "sub": role,
                "email": f"{role}@example.test",
                "email_verified": True,
                "name": people[role],
                "exp": int(time.time()) + 86400,
            },
            key,
            algorithm="RS256",
            headers={"kid": "local-playground"},
        )
        response = RedirectResponse(os.environ["GRIMOIRE_LOCAL_FRONTEND"] + "/grimoire")
        response.set_cookie("grimoire-id-token", token, httponly=True, samesite="lax")
        return response

    return app
