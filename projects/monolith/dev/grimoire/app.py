"""Loopback-only Grimoire playground with signed synthetic identities.

This entrypoint is launched explicitly by dev/grimoire/run.py. Production module
registration never imports it or registers its identity or embedding endpoints.

One origin serves everything, as the friends gateway does in production: the
built web app and the invitation landing under /grimoire, and the API under
/api/grimoire. ``/__local/login/{role}`` stands in for the gateway's login: it
sets the signed ID token in the ``grimoire-id-token`` cookie, which the backend
accepts because run.py sets GRIMOIRE_BROWSER_ORIGIN to this origin.
"""

import hashlib
import json
import math
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import jwt
from auth.api import AuthError, auth_error_handler
from auth.browser_cookie import BROWSER_COOKIE
from auth.platform.models import PlatformGrant, PlatformIdentity, PlatformUser
from auth.platform.router import router as platform_router
from core.db import get_engine
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from grimoire import register as register_grimoire
from grimoire.models import (
    AppUser,
    Campaign,
    CampaignMember,
    CharacterSheetVersion,
    Entity,
    EntityNpc,
    GameSession,
    PlayerCharacter,
    SessionEvent,
)
from grimoire.sheets import CharacterSheetV1, derive_sheet
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session, SQLModel, select

EMBEDDING_DIM = 1024
PEOPLE = {"dm": "Rowan", "a": "Elowen", "b": "Bram"}


def seed_local_profiles(engine, issuer):
    """Expose the existing demo seats to the read-only profile, without grants."""
    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__ for model in (PlatformUser, PlatformIdentity, PlatformGrant)
        ],
    )
    with Session(engine) as session:
        for role, name in PEOPLE.items():
            identity = session.exec(
                select(PlatformIdentity).where(
                    PlatformIdentity.issuer == issuer,
                    PlatformIdentity.subject == role,
                )
            ).first()
            if identity is not None:
                continue
            user = PlatformUser(
                username=role, email=f"{role}@example.test", display_name=name
            )
            session.add(user)
            session.flush()
            session.add(PlatformIdentity(user_id=user.id, issuer=issuer, subject=role))
        session.commit()


def register_local_profile(app):
    """Keep the production private GET handler, excluding every management route."""
    local = APIRouter()
    local.routes.extend(
        route
        for route in platform_router.routes
        if route.path == "/api/auth/platform/self" and route.methods == {"GET"}
    )
    app.include_router(local)


class EmbeddingRequest(BaseModel):
    input: list[str]


def local_vector(text: str) -> list[float]:
    """Deterministic stand-in for the voyage model: shared words score closer.

    Each word adds weight to one hashed dimension. The constant first
    dimension keeps every vector non-zero, so cosine distance stays defined.
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = 0.1
    for word in re.findall(r"\w+", text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        vector[1 + int.from_bytes(digest[:4], "big") % (EMBEDDING_DIM - 1)] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


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
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS platform_auth"))
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
    seed_local_profiles(engine, issuer)
    people = PEOPLE
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
            # The batch reveal uses Tessa and Orrin. Ilsa's secret is
            # searchable only while a full grant reveals it.
            secrets = {
                "Mapmaker Tessa": "DM_ONLY_BATCH_SECRET",
                "Ferryman Orrin": "DM_ONLY_BATCH_SECRET",
                "Harbourmaster Ilsa": "HARBOUR_LEDGER_SECRET",
            }
            npcs = [
                Entity(
                    entity_type="npc",
                    name=name,
                    source_type="homebrew",
                    is_global=False,
                    created_in_session=history.id,
                )
                for name in secrets
            ]
            session.add_all(npcs)
            session.flush()
            session.add_all(
                [
                    EntityNpc(entity_id=person.id, description=secrets[person.name])
                    for person in npcs
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
            session.flush()
            # Invented extraction output gives the real search/dispute UI
            # something to exercise without enabling any model or job drainer.
            # The existing writer still validates the evidence and viewer.
            from grimoire.character_facts import write_character_facts

            evidence = SessionEvent(
                campaign_id=campaign.id,
                session_id=history.id,
                seq=1,
                kind="narration",
                audience="table",
                body={
                    "text": "The party saw a glass comet, a silver compass and a copper key."
                },
            )
            session.add(evidence)
            session.flush()
            for viewer, statement in (
                (
                    characters[0].id,
                    "FACT_A_SILVER_COMPASS: Elowen remembers a silver compass.",
                ),
                (characters[1].id, "FACT_B_COPPER_KEY: Bram remembers a copper key."),
                ("party", "FACT_PARTY_GLASS_COMET: The party saw a glass comet."),
            ):
                result = write_character_facts(
                    session,
                    campaign.id,
                    history.id,
                    viewer,
                    "local-rehearsal-v1",
                    [{"statement": statement, "evidence_event_ids": [evidence.id]}],
                )
                if result.rejections or len(result.written) != 1:
                    raise RuntimeError("Local fact fixture failed normal validation")
            session.commit()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="local-playground", alg="RS256", use="sig")
    app = FastAPI(title="Grimoire local playground")
    app.add_exception_handler(AuthError, auth_error_handler)
    # The production registration: the API, the invitation landing and the
    # web app, in that order. GRIMOIRE_WEB_DIR names the bundle run.py built.
    if not (Path(os.environ["GRIMOIRE_WEB_DIR"]) / "index.html").is_file():
        raise RuntimeError("The playground requires the built Grimoire web app")
    register_grimoire(app)
    register_local_profile(app)

    @app.get("/__local/jwks")
    def jwks():
        return {"keys": [public]}

    # run.py points EMBEDDING_URL here, so the real embedding client, the
    # knowledge search route and the play embedder run without a model.
    @app.post("/__local/v1/embeddings")
    def embeddings(body: EmbeddingRequest):
        return {
            "data": [
                {"index": index, "embedding": local_vector(text)}
                for index, text in enumerate(body.input)
            ]
        }

    # Production embeds play state every five minutes; rehearsals run that
    # same job on demand instead of waiting for the schedule.
    @app.post("/__local/embed-play", status_code=204)
    async def embed_play():
        from grimoire.jobs import grimoire_embed_play

        await grimoire_embed_play(None)

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
                "preferred_username": role,
                "platform_user_type": "external",
                "exp": int(time.time()) + 86400,
            },
            key,
            algorithm="RS256",
            headers={"kid": "local-playground"},
        )
        # The gateway's session cookie: host-only, HttpOnly, sent to the app
        # and the API alike. It is not Secure because this origin is http.
        response = RedirectResponse("/grimoire", status_code=303)
        response.set_cookie(
            BROWSER_COOKIE, token, path="/", httponly=True, samesite="lax"
        )
        return response

    return app
