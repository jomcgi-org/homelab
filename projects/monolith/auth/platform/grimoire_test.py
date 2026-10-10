"""Existing JWT sessions cannot bypass live platform grants or account status."""

from uuid import uuid4

import pytest
from core.db import get_session
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from grimoire.access import get_authenticated_identity
from grimoire.accounts import sync_user
from grimoire.join_links import issue_link
from grimoire.models import (
    AppUser,
    Campaign,
    CampaignInvitation,
    CampaignJoinLink,
    CampaignMember,
)
from grimoire.router import InviteRequest, invite_registered_player, router
from sqlmodel import Session, SQLModel, create_engine, select

from auth.api import Authority, Principal, PrincipalKind
from auth.platform.enrollment import Completion, activate
from auth.platform.models import (
    PlatformApplicationUser,
    PlatformInvitation,
    PlatformUser,
    now,
)
from auth.platform.service import command


def test_optional_email_cannot_claim_an_unbound_legacy_account(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_AUTH_ENFORCEMENT_ENABLED", "true")
    for table in SQLModel.metadata.tables.values():
        if table.schema == "grimoire":
            monkeypatch.setattr(table, "schema", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}",
        execution_options={"schema_translate_map": {"grimoire": None}},
    )
    SQLModel.metadata.create_all(engine, tables=[AppUser.__table__])
    principal = Principal(
        subject="new-player",
        issuer="https://idp.test/grimoire/",
        email="victim@example.test",
        email_verified=True,
        username="player",
        user_type="external",
        authority=Authority.STANDING,
        kind=PrincipalKind.HUMAN,
        groups=(),
        actor=(),
        scope=(),
    )
    with Session(engine) as session:
        legacy = AppUser(email=principal.email)
        session.add(legacy)
        session.commit()
        legacy_id = legacy.id
        with pytest.raises(HTTPException) as denied:
            sync_user(session, principal)
        assert denied.value.status_code == 409
        preserved = session.get(AppUser, legacy_id)
        assert preserved.issuer is None and preserved.subject is None
        assert len(session.exec(select(AppUser)).all()) == 1
    engine.dispose()


def test_username_invite_resolves_registered_identity_with_contact_email(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PLATFORM_AUTH_ENFORCEMENT_ENABLED", "true")
    platform_tables = [
        table
        for table in SQLModel.metadata.sorted_tables
        if table.schema == "platform_auth"
    ]
    for table in SQLModel.metadata.tables.values():
        if table.schema == "grimoire":
            monkeypatch.setattr(table, "schema", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'username-invite.db'}",
        execution_options={"schema_translate_map": {"platform_auth": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=platform_tables
        + [
            AppUser.__table__,
            Campaign.__table__,
            CampaignMember.__table__,
            CampaignJoinLink.__table__,
            CampaignInvitation.__table__,
        ],
    )
    with Session(engine) as session:
        user = PlatformUser(
            username="friend", email="contact@example.test", display_name="Friend"
        )
        owner = AppUser(
            email="owner@example.test", issuer="https://idp.test/", subject="owner"
        )
        player = AppUser(email=user.email, issuer="https://idp.test/", subject="friend")
        campaign = Campaign(name="Our game", owner_app_user_id=owner.id)
        mapping = PlatformApplicationUser(
            user_id=user.id, application="grimoire", application_user_id=player.id
        )
        membership = CampaignMember(
            campaign_id=campaign.id, app_user_id=owner.id, role="dm"
        )
        session.add_all([user, owner, player, campaign, mapping, membership])
        session.commit()
        invitation = invite_registered_player(
            campaign.id,
            InviteRequest(email="@friend"),
            email=owner.email,
            session=session,
        )
        assert session.get(CampaignInvitation, invitation.id).invitee_id == player.id
        link = issue_link(session, campaign.id, owner, "@friend")
        assert session.get(CampaignJoinLink, link.id).recipient_id == player.id
        assert link.invitee_email == "@friend"
        user.active = False
        session.commit()
        with pytest.raises(HTTPException) as denied:
            issue_link(session, campaign.id, owner, "@friend")
        assert denied.value.status_code == 404
    engine.dispose()


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
