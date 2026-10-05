"""Existing JWT sessions cannot bypass live platform grants or account status."""

from uuid import uuid4

from core.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from grimoire.access import get_authenticated_identity
from grimoire.models import AppUser, Campaign, CampaignInvitation, CampaignMember
from grimoire.router import router
from sqlmodel import Session, SQLModel, create_engine, select

from auth.api import Authority, Principal, PrincipalKind
from auth.platform.enrollment import Completion, activate
from auth.platform.models import PlatformApplicationUser, PlatformInvitation, now
from auth.platform.service import command


def test_username_login_preserves_campaign_ids_and_rechecks_grants(
    tmp_path, monkeypatch
):
    from datetime import timedelta

    monkeypatch.setenv("PLATFORM_AUTH_MANAGEMENT_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_AUTH_ENROLLMENT_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_AUTH_ENFORCEMENT_ENABLED", "true")
    issuer = "https://idp.test/grimoire/"
    monkeypatch.setenv("AUTH_AUTHENTIK_ISSUER", issuer)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'grants.db'}",
        execution_options={
            "schema_translate_map": {"platform_auth": None, "grimoire": None}
        },
    )
    tables = [
        table
        for table in SQLModel.metadata.sorted_tables
        if table.schema == "platform_auth"
    ]
    # Match Grimoire's SQLite fixtures before ORM aliases are cached. Restore
    # every schema through monkeypatch so collecting this test is harmless.
    for table in SQLModel.metadata.tables.values():
        if table.schema == "grimoire":
            monkeypatch.setattr(table, "schema", None)
    SQLModel.metadata.create_all(
        engine,
        tables=tables
        + [
            AppUser.__table__,
            Campaign.__table__,
            CampaignMember.__table__,
            CampaignInvitation.__table__,
        ],
    )
    operator = Principal(
        subject="operator",
        issuer=issuer,
        email="owner@example.test",
        username="owner",
        user_type="internal",
        authority=Authority.STANDING,
        kind=PrincipalKind.HUMAN,
        groups=("operators",),
        actor=(),
        scope=(),
    )
    player = Principal(
        subject="player",
        issuer=issuer,
        email=None,
        username="player",
        user_type="external",
        authority=Authority.STANDING,
        kind=PrincipalKind.HUMAN,
        groups=(),
        actor=(),
        scope=(),
    )
    with Session(engine) as session:
        command(
            session,
            operator,
            "bootstrap",
            request_id=uuid4().hex,
            reason="Explicit operator import",
        )
        invitation = PlatformInvitation(
            recipient_label="Friend",
            issued_by="operator",
            status="pending",
            token_digest="a" * 64,
            expires_at=now() + timedelta(days=1),
        )
        session.add(invitation)
        session.commit()
        platform_user = activate(
            session,
            Completion(
                issuer,
                player.subject,
                player.username,
                invitation.id,
                invitation.token_digest,
                str(uuid4()),
            ),
        )
        legacy = AppUser(
            issuer=issuer, subject=player.subject, email="previous-contact@example.test"
        )
        session.add(legacy)
        session.commit()
        app_id = legacy.id

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_authenticated_identity] = lambda: player

    def connection():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = connection

    def manage(action, **arguments):
        with Session(engine) as session:
            return command(
                session,
                operator,
                action,
                request_id=uuid4().hex,
                reason="Explicit app grant change",
                user_id=platform_user["id"],
                **arguments,
            )

    with TestClient(app) as client:
        assert client.get("/api/grimoire/lobby").status_code == 403
        manage("grant", permission="grimoire.access")
        lobby = client.get("/api/grimoire/lobby")
        assert lobby.status_code == 200
        assert lobby.json()["user"]["id"] == app_id
        assert lobby.json()["user"]["email"] == "@player"
        assert lobby.json()["can_create_game"] is False
        assert (
            client.post("/api/grimoire/campaigns", json={"name": "Denied"}).status_code
            == 403
        )
        manage("grant", permission="grimoire.create_game")
        created = client.post("/api/grimoire/campaigns", json={"name": "Our game"})
        assert created.status_code == 200
        campaign_id = created.json()["id"]
        manage("revoke_grant", permission="grimoire.create_game")
        assert (
            client.post(
                "/api/grimoire/campaigns", json={"name": "Denied again"}
            ).status_code
            == 403
        )
        manage("set_active", active=False)
        assert client.get("/api/grimoire/lobby").status_code == 403
        with Session(engine) as session:
            assert session.get(Campaign, campaign_id).owner_app_user_id == app_id
            link = session.exec(select(PlatformApplicationUser)).one()
            assert (
                link.user_id == platform_user["id"]
                and link.application_user_id == app_id
            )
            assert len(session.exec(select(AppUser)).all()) == 1
    engine.dispose()
