"""Disabled defaults, identity boundaries, and mocked enrollment failures."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from auth.api import Authority, Principal, PrincipalKind
from core.db import get_session
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire import invitation_provider, join_links
from grimoire.access import get_authenticated_identity
from grimoire.models import CampaignJoinLink, CampaignMember
from grimoire.router import router

ISSUER = "https://auth.example/application/o/grimoire/"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_INVITATION_LINKS_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_AUTH_ISSUER", ISSUER)
    monkeypatch.delenv("GRIMOIRE_INVITATION_ENROLLMENT_ENABLED", raising=False)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'links.db'}", connect_args={"check_same_thread": False}
    )
    schemas = {table: table.schema for table in SQLModel.metadata.tables.values()}
    for table in schemas:
        table.schema = None
    SQLModel.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(router)

    def session():
        with Session(engine) as value:
            yield value

    app.dependency_overrides[get_session] = session

    def identity(request: Request):
        subject = request.headers.get("test-subject")
        if not subject:
            raise HTTPException(403, "missing identity")
        return Principal(
            subject=subject,
            issuer=request.headers.get("test-issuer", ISSUER),
            display_name=subject,
            email=request.headers.get("test-email", f"{subject}@example.test"),
            email_verified=False,
            actor=(),
            scope=(),
            groups=("operators",) if subject == "admin" else (),
            kind=PrincipalKind.HUMAN,
            authority=Authority.STANDING,
        )

    app.dependency_overrides[get_authenticated_identity] = identity
    try:
        with TestClient(app) as client:
            for user in ("owner", "player", "other", "admin"):
                assert (
                    client.get("/api/grimoire/lobby", headers=h(user)).status_code
                    == 200
                )
            response = client.post(
                "/api/grimoire/campaigns", headers=h("owner"), json={"name": "A table"}
            )
            assert response.status_code == 200
            yield client, engine, response.json()["id"]
    finally:
        for table, schema in schemas.items():
            table.schema = schema
        engine.dispose()


def h(user):
    return {"test-subject": user}


def issue(client, cid, user="owner", email="player@example.test", **options):
    return client.post(
        f"/api/grimoire/campaigns/{cid}/join-links",
        headers=h(user),
        json={"email": email, **options},
    )


def capability(client, action, token, user=None):
    return client.post(
        f"/api/grimoire/join-links/{action}",
        headers=h(user) if user else {},
        json={"token": token},
    )


def test_disabled_by_default_and_routes_fail_closed(setup, monkeypatch):
    client, _, cid = setup
    monkeypatch.delenv("GRIMOIRE_INVITATION_LINKS_ENABLED")
    assert issue(client, cid).status_code == 503
    assert capability(client, "inspect", "x" * 43).status_code == 503
    lobby = client.get("/api/grimoire/lobby", headers=h("owner")).json()
    assert not lobby["invitation_links_enabled"]
    assert not lobby["invitation_enrollment_enabled"]


def test_issue_inspect_join_replay_and_secret_projection(setup):
    client, engine, cid = setup
    created = issue(client, cid)
    assert created.status_code == 200, created.text
    link = created.json()
    assert len(link["token"]) == 43
    assert datetime.fromisoformat(link["expires_at"]) > datetime.now(
        timezone.utc
    ) + timedelta(days=6)
    for _ in range(2):
        metadata = capability(client, "inspect", link["token"])
        assert metadata.status_code == 200
        assert link["token"] not in metadata.text
        assert metadata.json()["status"] == "pending"
    listed = client.get(f"/api/grimoire/campaigns/{cid}/join-links", headers=h("owner"))
    assert link["token"] not in listed.text
    assert "token_digest" not in listed.text and "enrollment_id" not in listed.text
    with Session(engine) as session:
        row = session.get(CampaignJoinLink, link["id"])
        assert row.token_digest == join_links._digest(link["token"])
        assert link["token"] not in repr(row)
        assert len(session.exec(select(CampaignMember)).all()) == 1
    assert capability(client, "redeem", link["token"]).status_code == 403
    assert capability(client, "redeem", link["token"], "other").status_code == 403
    for _ in range(2):
        assert capability(client, "redeem", link["token"], "player").json() == {
            "campaign_id": cid,
            "status": "accepted",
        }
    assert issue(client, cid).status_code == 409


def test_non_owner_cross_campaign_and_admin_cannot_bypass_ownership(setup):
    client, _, cid = setup
    for user in ("player", "admin", "other"):
        assert issue(client, cid, user=user).status_code in (403, 404)
    link = issue(client, cid).json()
    other = client.post(
        "/api/grimoire/campaigns", headers=h("owner"), json={"name": "Other"}
    ).json()["id"]
    assert (
        client.delete(
            f"/api/grimoire/campaigns/{other}/join-links/{link['id']}",
            headers=h("owner"),
        ).status_code
        == 404
    )
    assert capability(client, "inspect", link["token"]).status_code == 200


def test_duplicate_submit_revoke_replacement_and_expiry(setup):
    client, engine, cid = setup
    link = issue(client, cid).json()
    assert issue(client, cid).status_code == 409
    assert (
        client.delete(
            f"/api/grimoire/campaigns/{cid}/join-links/{link['id']}", headers=h("owner")
        ).status_code
        == 204
    )
    assert capability(client, "inspect", link["token"]).status_code == 409
    fresh = issue(client, cid).json()
    assert fresh["token"] != link["token"]
    with Session(engine) as session:
        row = session.get(CampaignJoinLink, fresh["id"])
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(row)
        session.commit()
    assert capability(client, "inspect", fresh["token"]).status_code == 410
    assert capability(client, "redeem", fresh["token"], "player").status_code == 410


@pytest.mark.parametrize(
    "body",
    [
        {"token": {"secret": "sensitive-capability"}},
        {"token": "sensitive-capability", "extra": "sensitive-capability"},
        ["sensitive-capability"],
        {"token": None},
    ],
)
def test_malformed_bodies_never_echo_capabilities(setup, body):
    client, _, _ = setup
    for action in ("inspect", "enroll", "redeem"):
        response = client.post(
            f"/api/grimoire/join-links/{action}", headers=h("player"), json=body
        )
        assert response.status_code == 400
        assert "sensitive-capability" not in response.text


def test_new_accounts_require_owner_and_operator_and_separate_flag(setup, monkeypatch):
    client, _, cid = setup
    monkeypatch.setenv("GRIMOIRE_INVITATION_ENROLLMENT_ENABLED", "true")
    assert (
        issue(client, cid, email="new@example.test", allow_enrollment=True).status_code
        == 404
    )
    admin_cid = client.post(
        "/api/grimoire/campaigns", headers=h("admin"), json={"name": "Admin table"}
    ).json()["id"]
    assert (
        issue(client, admin_cid, user="admin", email="new@example.test").status_code
        == 404
    )
    assert (
        issue(
            client,
            admin_cid,
            user="admin",
            email="new@example.test",
            allow_enrollment=True,
        ).status_code
        == 503
    )  # no credentials configured
    monkeypatch.delenv("GRIMOIRE_INVITATION_ENROLLMENT_ENABLED")
    assert (
        issue(
            client,
            admin_cid,
            user="admin",
            email="new@example.test",
            allow_enrollment=True,
        ).status_code
        == 503
    )


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_INVITATION_ENROLLMENT_ENABLED", "true")
    state = SimpleNamespace(
        active=set(), created=[], fail_delete=False, fail_create=False
    )

    class MockProvider:
        def exists(self, pk):
            return pk in state.active

        def create(self, **kwargs):
            if state.fail_create:
                raise HTTPException(503, "Temporarily unavailable.")
            state.created.append(kwargs)
            pk = str(uuid4())
            state.active.add(pk)
            return pk

        def revoke(self, pk):
            if state.fail_delete:
                raise HTTPException(503, "Cleanup needs retry.")
            state.active.discard(pk)

        def enrollment_url(self, pk):
            return (
                f"https://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/?itoken={pk}"
            )

    monkeypatch.setattr(join_links, "InvitationProvider", MockProvider)
    return state


def test_enrollment_retry_uses_stable_identity_and_local_revoke_survives_provider_failure(
    setup, provider
):
    client, engine, _ = setup
    cid = client.post(
        "/api/grimoire/campaigns", headers=h("admin"), json={"name": "Admin table"}
    ).json()["id"]
    link = issue(
        client, cid, user="admin", email="new@example.test", allow_enrollment=True
    ).json()
    assert provider.created == []
    provider.fail_create = True
    assert capability(client, "enroll", link["token"]).status_code == 503
    assert capability(client, "inspect", link["token"]).json()["status"] == "pending"
    provider.fail_create = False
    for _ in range(2):
        assert capability(client, "enroll", link["token"]).status_code == 200
    assert len(provider.created) == 1
    provider.active.clear()  # Authentik consumed token before signup finished.
    assert capability(client, "enroll", link["token"]).status_code == 200
    assert len(provider.created) == 2
    assert provider.created[0] == provider.created[1]
    with Session(engine) as session:
        assert session.get(CampaignJoinLink, link["id"]).status == "pending"
    provider.fail_delete = True
    path = f"/api/grimoire/campaigns/{cid}/join-links/{link['id']}"
    assert client.delete(path, headers=h("admin")).status_code == 503
    assert capability(client, "enroll", link["token"]).status_code == 409
    metadata = client.get(
        f"/api/grimoire/campaigns/{cid}/join-links", headers=h("admin")
    ).json()[0]
    assert metadata["status"] == "revoked" and metadata["enrollment_cleanup_pending"]
    provider.fail_delete = False
    assert client.delete(path, headers=h("admin")).status_code == 204
    assert not provider.active


def test_existing_user_cannot_turn_link_into_signup(setup, provider):
    client, _, cid = setup
    link = issue(client, cid, allow_enrollment=True).json()
    assert not capability(client, "inspect", link["token"]).json()["can_enroll"]
    assert capability(client, "enroll", link["token"]).status_code == 403
    assert not provider.created


def test_provider_allowlist_single_use_expiry_and_redacted_errors(monkeypatch, caplog):
    monkeypatch.setenv("GRIMOIRE_INVITATION_ENROLLMENT_ENABLED", "true")
    monkeypatch.setenv("GRIMOIRE_INVITATION_API_TOKEN", "test-only-secret")
    flow = str(uuid4())
    monkeypatch.setenv("GRIMOIRE_INVITATION_FLOW_ID", flow)
    sent = []
    pk = str(uuid4())

    class Transport:
        def __init__(self, **kwargs):
            assert kwargs == {"trust_env": False}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def handle_request(self, request):
            sent.append(request)
            return httpx.Response(
                201, json={"pk": pk, "flow": flow, "single_use": True}
            )

    monkeypatch.setattr(httpx, "HTTPTransport", Transport)
    provider = invitation_provider.InvitationProvider()
    expires = datetime.now(timezone.utc) + timedelta(days=1)
    assert (
        provider.create(
            link_id=str(uuid4()),
            email="a@example.test",
            username="fixed",
            expires=expires,
        )
        == pk
    )
    import json

    payload = json.loads(sent[0].content)
    assert payload["flow"] == flow and payload["single_use"] is True
    assert payload["fixed_data"] == {"email": "a@example.test", "username": "fixed"}
    assert payload["expires"] == expires.isoformat()
    assert (
        str(sent[0].url)
        == "https://auth.jomcgi.dev/api/v3/stages/invitation/invitations/"
    )
    assert "test-only-secret" not in caplog.text and pk not in caplog.text


def test_provider_id_persistence_error_is_sanitized(
    setup, provider, monkeypatch, caplog
):
    from sqlalchemy.exc import StatementError

    client, engine, _ = setup
    cid = client.post(
        "/api/grimoire/campaigns", headers=h("admin"), json={"name": "Admin"}
    ).json()["id"]
    link = issue(
        client, cid, user="admin", email="new@example.test", allow_enrollment=True
    ).json()
    real_commit = Session.commit
    secret = []

    def fail_provider_update(session):
        for row in session.dirty:
            if isinstance(row, CampaignJoinLink) and row.enrollment_id:
                secret.append(row.enrollment_id)
                raise StatementError(
                    "test database error",
                    "UPDATE invitation",
                    {"enrollment_id": row.enrollment_id},
                    RuntimeError("failure"),
                )
        return real_commit(session)

    monkeypatch.setattr(Session, "commit", fail_provider_update)
    response = capability(client, "enroll", link["token"])
    assert response.status_code == 503
    assert secret and secret[0] not in response.text and secret[0] not in caplog.text
    with Session(engine) as session:
        row = session.get(CampaignJoinLink, link["id"])
        assert row.status == "pending" and row.enrollment_id is None
